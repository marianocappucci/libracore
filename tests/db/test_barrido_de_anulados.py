"""Ninguna consulta de plata sobre `caja_movimientos` se olvida de los anulados.

🔴 **Lee los FUENTES, no la base.** Lo que hay que impedir no es que una consulta
de hoy este mal ---esas tienen sus tests--- sino que **la proxima nazca sin el
filtro**. Eso se ve barriendo el modulo, y solo si alguien se acuerda de barrer.

Es el mismo tipo de guard que `test_schema_congelado`: la lista de excepciones se
mueve **a mano**, y que haya que moverla es la señal.

## De donde salio

Hasta el 2026-08-28 solo `get_resumen_turno_caja` filtraba. Un movimiento anulado
seguia contando en el arqueo de [[contalibra]] y [[restolibra]], en lo cobrado de
una factura, en el saldo de una cuenta corriente, en los reportes y en el
tablero: el operador anulaba, veia la fila marcada, **y el total no se movia**.
Peor que borrar, porque parece que funciono.
"""

from __future__ import annotations

import re
from pathlib import Path

MODULOS = Path(__file__).resolve().parents[2] / "libracore" / "db"

#: Las consultas que NO filtran, **a proposito**, con el motivo.
#:
#: 🔑 La regla: los NUMEROS filtran, las listas no ---para poder mostrar el
#: anulado con su marca---. Y la excepcion de la regla: una lista que alimenta un
#: documento o un vistazo que no puede marcar nada, filtra igual (el recibo, y
#: los seis movimientos del tablero).
SIN_FILTRO_A_PROPOSITO = {
    ("caja.py", "SELECT COUNT(*) FROM caja_movimientos WHERE caja_id=?"):
        "cuenta las filas que referencian una caja, para no borrarla con "
        "movimientos colgados. Un anulado sigue siendo una fila.",
    ("caja.py", "SELECT id FROM caja_movimientos WHERE referencia=? AND factura_id=? LIMIT 1"):
        "idempotencia por referencia. Si NO viera el anulado, un reintento del "
        "webhook volveria a crear el movimiento que alguien dio de baja.",
    ("caja.py", "SELECT id FROM caja_movimientos WHERE referencia=? AND factura_id IS NULL LIMIT 1"):
        "idem, para el movimiento sin factura.",
    ("caja.py", "FROM caja_movimientos cm"):
        "`get_caja_movimientos`: es LA lista que ve el operador, y la que tiene "
        "que mostrar los anulados con su marca.",
    ("logs.py", "FROM caja_movimientos cm"):
        "el log de actividad es historia: muestra lo que paso, no lo que quedo.",
    ("turnos.py", "FROM caja_movimientos WHERE turno_id=? ORDER BY id"):
        "la lista del arqueo, que los muestra marcados. El TOTAL de al lado si "
        "filtra --- son las dos mitades de `get_resumen_turno_caja`.",
}


#: Archivos donde UN fragmento cubre varias consultas, porque se arma en una
#: variable y se interpola en todas. Es **mejor** que repetirlo: dos copias del
#: mismo criterio pueden divergir, una no.
FILTRO_COMPARTIDO = {
    "facturas.py": (
        2,
        "`_cc_excl` se arma una vez y se usa en las dos: la columna "
        "`total_cobrado` y el filtro `solo_sin_cobrar`. Si fueran dos "
        "fragmentos distintos, una factura podria listarse como impaga y a la "
        "vez mostrar el total cobrado completo.",
    ),
}


def _consultas() -> list[tuple[str, str, str]]:
    """(archivo, linea, texto) de cada `FROM|DELETE FROM caja_movimientos`."""
    encontradas = []
    for archivo in sorted(MODULOS.glob("*.py")):
        for n, linea in enumerate(archivo.read_text(encoding="utf-8").splitlines(), 1):
            # 🔴 Los comentarios NO son consultas. El guard contaba como lectura
            # un comentario que explicaba por que una consulta se armaba de
            # cierta forma ---y nombraba la tabla--- y pedia un filtro para el.
            # Tercera vez que este guard se afina a si mismo: primero por buscar
            # el filtro "cerca", despues por contarlo dos veces, y ahora por esto.
            if linea.strip().startswith("#"):
                continue
            # 🔑 Solo LECTURAS. Un `INSERT` o el `UPDATE` del relleno no tienen
            # nada que filtrar, y meterlos hace que el guard pida un filtro
            # donde no va --- que es como una lista de excepciones se llena de
            # ruido y deja de leerse.
            if not re.search(r"FROM\s+caja_movimientos", linea):
                continue
            if re.search(r"DELETE\s+FROM\s+caja_movimientos", linea):
                continue
            if True:
                encontradas.append((archivo.name, n, linea.strip()))
    return encontradas


def _clave(archivo: str, texto: str) -> tuple[str, str] | None:
    for (arch, fragmento) in SIN_FILTRO_A_PROPOSITO:
        if arch == archivo and fragmento in texto:
            return (arch, fragmento)
    return None


def test_toda_consulta_de_plata_excluye_los_anulados():
    """El barrido, medido y no recordado.

    🔴 **Se CUENTA, no se busca alrededor.** La primera version de este guard
    miraba si aparecia `sql_no_anulado` en los 900 caracteres alrededor de la
    consulta, y eso le daba falsos negativos: sacarle el filtro a una consulta
    pasaba en verde porque **la de al lado** tenia el suyo. Lo delato la
    mutacion, no la lectura.

    Ahora es una cuenta por archivo: cada lectura que no este en las excepciones
    tiene que tener **su** `sql_no_anulado`. Si un archivo tiene tres lecturas
    que lo necesitan y dos filtros, falta uno --- sin importar donde este.
    """
    faltan = []
    for archivo in sorted({a for a, _, _ in _consultas()}):
        cuerpo = (MODULOS / archivo).read_text(encoding="utf-8")
        # Los usos del fragmento en este archivo, sin contar la definicion.
        filtros = len(re.findall(r"sql_no_anulado\(", cuerpo))
        if archivo == "caja.py":
            filtros -= len(re.findall(r"def sql_no_anulado\(", cuerpo))
        # 🔴 Cuenta TODAS las lecturas que piden filtro, incluidas las que lo
        # llevan en la misma linea. Sacar esas del lado izquierdo y contarlas
        # igual del derecho es un DOBLE CONTEO: dejaba pasar que a otra consulta
        # del mismo archivo le sacaran el suyo. Lo delato la mutacion, dos veces.
        necesitan = [
            (n, t) for a, n, t in _consultas()
            if a == archivo and not _clave(archivo, t)
        ]
        # `turnos.py` no usa el fragmento: filtra con `anulado=0` escrito a mano
        # porque su consulta se armo antes de que el fragmento existiera. Cuenta
        # igual --- lo que importa es que filtre, no como se escribe.
        filtros += len(re.findall(r"anulado\s*=\s*0", cuerpo))
        cubiertas, _motivo = FILTRO_COMPARTIDO.get(archivo, (1, ""))
        if filtros * cubiertas < len(necesitan):
            faltan.append(
                f"{archivo}: {len(necesitan)} lecturas piden filtro y hay "
                f"{filtros} --- "
                + "; ".join(f"linea {n}" for n, _ in necesitan)
            )

    assert faltan == [], (
        "Falta excluir los anulados en alguna consulta de plata sobre "
        "`caja_movimientos`. Si es un NUMERO, agregarle `sql_no_anulado()`. Si "
        "es una lista que los muestra con su marca, agregarla a "
        "SIN_FILTRO_A_PROPOSITO **con el motivo**:\n  " + "\n  ".join(faltan)
    )


def test_el_control_del_guard():
    """🔴 Sin esto, los dos asserts de arriba pasarian con un barrido que no
    encuentra nada --- un `glob` roto, un rename del modulo, un regex que dejo de
    matchear. Es la forma en que este guard fallaria en silencio."""
    consultas = _consultas()
    assert len(consultas) >= 20, f"el barrido encontro solo {len(consultas)}"
    assert any(a == "caja.py" for a, _, _ in consultas)
    assert any(a == "turnos.py" for a, _, _ in consultas)
    # Y que las excepciones sigan existiendo: una que quede huerfana quiere decir
    # que la consulta se movio o se borro, y nadie actualizo la lista.
    for (archivo, fragmento), motivo in SIN_FILTRO_A_PROPOSITO.items():
        assert any(a == archivo and fragmento in t for a, _, t in consultas), (
            f"la excepcion «{archivo}: {fragmento[:50]}» ya no matchea ninguna "
            f"consulta --- se movio o se borro, y la lista quedo vieja"
        )
        assert motivo.strip(), "toda excepcion lleva su motivo"

    # Y lo mismo para el filtro compartido: si el archivo dejo de tener esa
    # cantidad de consultas, la entrada quedo vieja y el guard se ablando sin
    # que nadie lo decida.
    for archivo, (cubiertas, motivo) in FILTRO_COMPARTIDO.items():
        lecturas = [t for a, _, t in consultas if a == archivo]
        assert len(lecturas) >= cubiertas, (
            f"{archivo} declara que un fragmento cubre {cubiertas} consultas y "
            f"quedan {len(lecturas)}"
        )
        assert motivo.strip(), "todo filtro compartido lleva su motivo"
