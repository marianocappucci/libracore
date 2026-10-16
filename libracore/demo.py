"""Qué parece real en una instancia de demo, y por qué una demo no habla con ARCA (ADR-061).

El 2026-10-09 la demo pública de LibraDesk mostraba los datos fiscales de un cliente. ADR-038 cerró esa puerta para
`config.json` (`config_manager.load()` pisa los datos de la empresa con la ficticia), pero **ningún test impedía que
otra puerta se abriera**: el emisor que sale de `arca_config`, un certificado de producción cargado a mano, un CUIT real
en la semilla de un producto. Este módulo hace dos cosas:

1. **Define qué es un documento ficticio** (`clasificar_documento`) con una regla que se puede verificar, no con una
   lista de lo que «ya sabemos que es real».
2. **Audita una instancia** (`auditar_datos_fiscales`): devuelve lo que en una demo parece real, para que la corrida
   nocturna de la demo y el test de cada producto fallen en vez de mostrarlo.

## Qué es «ficticio»

Un CUIT es ficticio si tiene **dígito verificador válido** (así ARCA lo toma por bien formado y la demo puede emitir
contra homologación) y su cuerpo está en el **rango reservado**: los ocho dígitos del medio empiezan con `99999`
(`30-99999900-6`, el de `EMPRESA_DEMO_POR_DEFECTO`; hay mil por prefijo). Un DNI es ficticio si tiene ocho dígitos y
empieza con `99999`.

Lo que **no** se usa, a propósito:

- **Una lista negra** de CUIT reales. Nadie debe escribir en el código los de sus clientes, y lo que no está en la lista
  pasaría igual.
- **«Tiene verificador válido, entonces es real»**. Un CUIT inventado a mano acierta el verificador una de cada once
  veces; los inventados que no lo aciertan no pueden existir en el padrón de ARCA (`invalido`), y el que sí lo acierta
  no se puede distinguir de uno real sin consultar el padrón. Por eso el que cierra el verificador **fuera del rango
  reservado** es `parece_real`: se reemplaza por uno del rango, que cuesta una línea.

El producto puede **declarar** los que usa (`ficticios=`): es una lista blanca y por lo tanto una deuda a la vista, no
la meta. La semilla de un producto que la usa para pasar el test no está probando nada sobre sus datos.

## Qué se audita

Se lee **la base y el `config.json` tal cual están**, no a través de `config_manager.load()` ni de `config_del_emisor()`,
que en una demo ya esconden lo real (ADR-061): lo que se quiere saber es qué hay guardado.

- La empresa: `empresa_cuit` de `config.json`, el `cuit` de cada fila de `arca_config` y las columnas que el producto
  declare (`empresa=`, p. ej. `configuracion_empresa.cuit` de LibraCargo).
- ARCA: ambiente de producción, certificado o clave de producción (y, como dudoso, los de homologación), credenciales de
  servicios (`arca_credenciales_servicio`) de producción.
- MercadoPago: una credencial que no está confirmada como de **prueba** (`mp_config_router.ambiente_de`).
- Los terceros: `clients`, `proveedores`, remitos, presupuestos, facturas, recibos, comprobantes pendientes y la bandeja de
  MercadoPago, más las columnas que el producto declare (`terceros=`).
- El correo (dudoso): servidor SMTP o remitente cargados.
- El **texto** de una semilla (`auditar_literales`): los CUIT de forma real escritos en el archivo, que no depende de qué rutas
  corrieron al sembrar.

Cada hallazgo tiene **gravedad**: `alta` es lo que parece real y es lo que hace fallar a quien audita; `baja` es lo que
no se puede afirmar (un CUIT que no cierra el verificador, un DNI, un certificado de homologación, un SMTP). Sin
`incluir_dudosos=True` sólo salen las altas.

🔴 **Un hallazgo nunca lleva el dato completo.** Los CUIT salen enmascarados (`30714*****1`), de ningún dato se imprime
un nombre ni un secreto, y la fila se nombra por su `id`: la salida va a logs y a avisos.

## La corrida nocturna

`python -m libracore.demo auditar --producto <prefijo>` audita las bases de la instancia (la de LibraCore y, si va aparte, la del
dominio; o `--url`/`DATABASE_URL` para una sola) con `DEMO_MODE=1` y sale
con **0** si no hay nada, **3** si hay hallazgos, **4** si la instancia no es una demo (no se audita lo que no lo es) y
**2** ante un error de uso. Lo corre adentro del contenedor el reset nocturno de cada demo, después de sembrar.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

from libracore import config_manager
from libracore.arca_wsfe import cuit_con_verificador_valido
from libracore.cuit import solo_digitos
from libracore.db import arca_config as db_arca

#: Los dos primeros dígitos de un CUIT/CUIL: personas (20, 23, 24, 27) y empresas (30, 33, 34).
PREFIJOS_DE_CUIT = ("20", "23", "24", "27", "30", "33", "34")

#: Con qué empiezan los **ocho dígitos del medio** de un CUIT ficticio, y los de un DNI ficticio. ADR-038 lo eligió
#: (`30-99999900-6`) por ser un rango que ARCA no asigna; **no está medido contra el padrón**, que acá no se consulta.
PREFIJO_RESERVADO = "99999"

# Cómo se clasifica un documento. El valor es lo que ve quien llama.
VACIO = "vacio"
FICTICIO = "ficticio"
PARECE_REAL = "parece_real"
INVALIDO = "invalido"
DNI_FICTICIO = "dni_ficticio"
DNI = "dni"
OTRO = "otro"

#: Gravedades. `alta` falla la corrida; `baja` sólo sale con `incluir_dudosos=True`.
ALTA = "alta"
BAJA = "baja"

#: Las columnas de terceros que el motor conoce, `(tabla, columna)` o `(tabla, columna, condicion)`. La condición es
#: SQL escrito por quien mantiene esta lista, nunca algo que llegue de afuera.
TERCEROS_DEL_MOTOR = (
    ("clients", "cuit_dni"),
    ("proveedores", "cuit_dni"),
    ("remitos", "client_cuit"),
    ("presupuestos", "client_cuit"),
    ("facturas", "cliente_cuit"),
    ("recibos", "cliente_cuit"),
    ("comprobantes_pendientes", "cliente_cuit"),
    ("mp_pagos", "payer_id_number"),
    ("mp_movimientos", "payer_id_number"),
    ("facturacion_alias", "valor", "tipo = 'cuit'"),
)

_IDENTIFICADOR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Salidas de `main`.
SALIDA_LIMPIA, SALIDA_USO, SALIDA_HALLAZGOS, SALIDA_NO_ES_DEMO = 0, 2, 3, 4


@dataclass(frozen=True)
class Hallazgo:
    """Algo que en una demo parece real. Sin el dato completo: ver el docstring del módulo."""

    codigo: str
    gravedad: str
    donde: str
    detalle: str = ""
    cantidad: int = 1

    def __str__(self) -> str:
        resto = f" ({self.cantidad}): {self.detalle}" if self.detalle else f" ({self.cantidad})"
        return f"[{self.gravedad}] {self.codigo} en {self.donde}{resto}"


def clasificar_documento(valor, ficticios=frozenset()) -> str:
    """Qué es un CUIT/CUIL/DNI cargado: `FICTICIO`, `PARECE_REAL`, `INVALIDO`, `DNI_FICTICIO`, `DNI`, `OTRO` o `VACIO`.

    `ficticios` son los que el producto declara como inventados (sólo dígitos). Ver el docstring del módulo para la
    regla, que es una sola y vive acá.
    """
    digitos = solo_digitos(valor)
    if not digitos:
        return VACIO
    if digitos in ficticios:
        return FICTICIO
    if len(digitos) == 11:
        if digitos[:2] not in PREFIJOS_DE_CUIT or not cuit_con_verificador_valido(digitos):
            return INVALIDO
        return FICTICIO if digitos[2:2 + len(PREFIJO_RESERVADO)] == PREFIJO_RESERVADO else PARECE_REAL
    if len(digitos) in (7, 8):
        return DNI_FICTICIO if len(digitos) == 8 and digitos.startswith(PREFIJO_RESERVADO) else DNI
    return OTRO


def es_cuit_ficticio(valor) -> bool:
    """¿Es un CUIT de 11 dígitos, con verificador válido y del rango reservado? Lo que exige `usar_empresa_demo`."""
    return len(solo_digitos(valor)) == 11 and clasificar_documento(valor) == FICTICIO


def enmascarar(valor) -> str:
    """El documento sin los dígitos del medio (`30714*****1`): lo bastante para encontrarlo, no para usarlo."""
    digitos = solo_digitos(valor)
    if len(digitos) < 4:
        return "*" * len(digitos)
    return digitos[:max(1, len(digitos) - 6)] + "*****" + digitos[-1]


def _resumen(filas: list[tuple[str, object]], tope: int = 3) -> str:
    """`ej.: 30714*****1 (fila 3), …` de los primeros `tope` valores."""
    ejemplos = [f"{enmascarar(v)} ({ref})" for ref, v in filas[:tope]]
    mas = f", y {len(filas) - tope} más" if len(filas) > tope else ""
    return "ej.: " + ", ".join(ejemplos) + mas


def _revisar_documentos(filas: list[tuple[str, object]], donde: str, rol: str,
                        ficticios: frozenset) -> list[Hallazgo]:
    """Clasifica `(referencia, valor)` y agrupa: un hallazgo por (dónde, código), no uno por fila."""
    por_codigo: dict[tuple[str, str], list[tuple[str, object]]] = {}
    for ref, valor in filas:
        clase = clasificar_documento(valor, ficticios)
        if clase in (VACIO, FICTICIO, DNI_FICTICIO):
            continue
        if clase == PARECE_REAL:
            clave = (f"{rol}_cuit_parece_real", ALTA)
        else:
            clave = (f"{rol}_cuit_sin_verificar" if rol == "empresa" else "tercero_documento_sin_verificar", BAJA)
        por_codigo.setdefault(clave, []).append((ref, valor))
    salida = [Hallazgo(codigo, gravedad, donde, _resumen(grupo), len(grupo))
              for (codigo, gravedad), grupo in por_codigo.items()]
    return salida


def _columnas(conn, tabla: str) -> set[str]:
    """Las columnas de `tabla`, o vacío si no existe. `PRAGMA table_info` anda en SQLite y en PostgreSQL (el adaptador
    la traduce) y **no aborta la transacción** si la tabla falta, que es lo que haría un `SELECT` en PostgreSQL."""
    return {fila[1] for fila in conn.execute(f"PRAGMA table_info({tabla})").fetchall()}


def _par(especificacion) -> tuple[str, str, str]:
    """`(tabla, columna[, condicion])` validado: los nombres van dentro del SQL, así que tienen que ser identificadores."""
    tabla, columna, *resto = especificacion
    if not _IDENTIFICADOR.match(tabla) or not _IDENTIFICADOR.match(columna):
        raise ValueError(f"Nombre de tabla o columna inválido: {tabla!r}.{columna!r}")
    return tabla, columna, (resto[0] if resto else "")


def _leer_columna(conn, tabla: str, columna: str, condicion: str = "") -> list[tuple[str, object]]:
    """`(referencia, valor)` de las filas con la columna cargada. Si la tabla o la columna no existen, nada."""
    columnas = _columnas(conn, tabla)
    if columna not in columnas:
        return []
    con_id = "id" in columnas
    where = f"{columna} IS NOT NULL AND {columna} <> ''" + (f" AND ({condicion})" if condicion else "")
    filas = conn.execute(
        f"SELECT {'id' if con_id else 'NULL'}, {columna} FROM {tabla} WHERE {where}"
    ).fetchall()
    return [(f"{tabla}#{f[0]}" if f[0] is not None else tabla, f[1]) for f in filas]


def _auditar_arca(conn, ficticios: frozenset) -> list[Hallazgo]:
    """`arca_config` y las credenciales por servicio. Se lee con SQL: los lectores del motor no devuelven nada en una demo."""
    salida: list[Hallazgo] = []
    if _columnas(conn, "arca_config"):
        filas = [dict(f) for f in conn.execute("SELECT * FROM arca_config").fetchall()]
        salida += _revisar_documentos(
            [(f"arca_config#{f['id']}", f.get("cuit")) for f in filas if f.get("cuit")],
            "arca_config.cuit", "empresa", ficticios)

        def _ids(seleccion):
            return ", ".join(f"arca_config#{f['id']}" for f in seleccion[:5]) + (
                f", y {len(seleccion) - 5} más" if len(seleccion) > 5 else "")

        en_produccion = [f for f in filas if str(f.get("ambiente") or "").strip().lower() == "produccion"]
        if en_produccion:
            salida.append(Hallazgo(
                "arca_en_produccion", ALTA, "arca_config.ambiente",
                "el selector está en producción: " + _ids(en_produccion), len(en_produccion)))
        # Los pares salen del mapa del motor (`COLUMNAS_POR_AMBIENTE`), no de los nombres de columna: el de producción
        # vive en las columnas sin sufijo y esa asimetría se nombra en un solo lugar.
        for ambiente, columnas in db_arca.COLUMNAS_POR_AMBIENTE.items():
            con_par = [f for f in filas if any(f.get(c) for c in columnas)]
            if not con_par:
                continue
            if ambiente == "produccion":
                en_disco = sum(1 for f in con_par if any(f.get(c) and os.path.exists(f[c]) for c in columnas))
                salida.append(Hallazgo(
                    "arca_certificado_de_produccion", ALTA, "arca_config (par de producción)",
                    f"{_ids(con_par)}; con archivo en disco: {en_disco}", len(con_par)))
            else:
                salida.append(Hallazgo(
                    "arca_certificado_de_homologacion", BAJA, "arca_config (par de homologación)",
                    "una demo no tiene certificado, ni siquiera de homologación: " + _ids(con_par), len(con_par)))
    if _columnas(conn, "arca_credenciales_servicio"):
        filas = conn.execute(
            "SELECT id, servicio FROM arca_credenciales_servicio WHERE ambiente = 'produccion' "
            "AND (certificado_path <> '' OR clave_path <> '')").fetchall()
        if filas:
            servicios = sorted({f[1] for f in filas})
            salida.append(Hallazgo(
                "arca_servicio_de_produccion", ALTA, "arca_credenciales_servicio",
                "servicios: " + ", ".join(servicios), len(filas)))
    return salida


def _auditar_config(cfg: dict, ficticios: frozenset) -> list[Hallazgo]:
    """Lo que el `config.json` (y el almacén de secretos) tiene guardado: la empresa, MercadoPago y el correo."""
    from libracore.mp_config_router import PRUEBA
    from libracore.mp_config_router import ambiente_de as ambiente_de_mp

    salida = _revisar_documentos(
        [("config.json", cfg.get("empresa_cuit"))], "config.json empresa_cuit", "empresa", ficticios)
    if (cfg.get("mp_access_token") or "").strip():
        ambiente, _ = ambiente_de_mp(cfg)
        if ambiente != PRUEBA:
            salida.append(Hallazgo(
                "mp_credencial_no_es_de_prueba", ALTA, "config mp_access_token",
                f"hay un token de MercadoPago y su ambiente es «{ambiente}», no «{PRUEBA}»: una demo no tiene cuenta "
                "de MercadoPago, y con una real la bandeja se llena con los pagadores reales"))
    correo = [c for c in ("email_smtp_host", "email_smtp_user", "email_smtp_password", "email_from")
              if (cfg.get(c) or "").strip()]
    if correo:
        salida.append(Hallazgo(
            "correo_cargado", BAJA, "config " + "/".join(correo),
            "la demo puede mandar mails desde una cuenta real y mostrar su remitente", len(correo)))
    return salida


def auditar_datos_fiscales(conn, config: dict | None = None, *, terceros=(), empresa=(), ficticios=(),
                           incluir_dudosos: bool = False, conn_producto=None) -> list[Hallazgo]:
    """Lo que en esta instancia de demo parece un dato fiscal real. `[]` es una demo limpia. Ver el docstring del módulo.

    `conn` es una conexión del motor (SQLite o PostgreSQL). `config` es el contenido de `config.json` **sin** el pisado
    de la demo; sin él se lee `config_manager.load_sin_demo()`.

    `terceros` y `empresa` son las columnas **propias del producto** que guardan documentos de terceros o de la empresa:
    `[("clientes", "cuit"), ("choferes", "dni")]`. Lo que el motor ya conoce no hace falta declararlo. `ficticios` son los
    documentos que el producto declara como inventados (lista blanca; ver el docstring del módulo).
    `incluir_dudosos` suma las de gravedad `baja`.

    🔑 **`conn_producto` es la base del dominio cuando va aparte de la de LibraCore.** GestioLibra, MedLibra, LibraCargo y
    LibraClub llevan el core en otra base (`url_de_instancia.comparte_base_con_el_dominio`): `patients`, `terceros` o
    `clientes` no están en `conn`. Las columnas declaradas con `terceros=` y `empresa=` se leen de `conn_producto`
    (por omisión, `conn`); lo que es del motor, siempre de `conn`.
    """
    cfg = config if config is not None else config_manager.load_sin_demo()
    fict = frozenset(d for d in (solo_digitos(x) for x in ficticios) if d)
    hallazgos = _auditar_config(cfg, fict) + _auditar_arca(conn, fict)
    del_producto = conn_producto if conn_producto is not None else conn
    for especificacion in empresa:
        tabla, columna, condicion = _par(especificacion)
        hallazgos += _revisar_documentos(
            _leer_columna(del_producto, tabla, columna, condicion), f"{tabla}.{columna}", "empresa", fict)
    for origen, especificaciones in ((conn, TERCEROS_DEL_MOTOR), (del_producto, terceros)):
        for especificacion in especificaciones:
            tabla, columna, condicion = _par(especificacion)
            hallazgos += _revisar_documentos(
                _leer_columna(origen, tabla, columna, condicion), f"{tabla}.{columna}", "tercero", fict)
    if not incluir_dudosos:
        hallazgos = [h for h in hallazgos if h.gravedad == ALTA]
    return sorted(hallazgos, key=lambda h: (h.gravedad != ALTA, h.codigo, h.donde))


_CUIT_EN_TEXTO = re.compile(r"(?<!\d)(\d{2})[-. ]?(\d{8})[-. ]?(\d)(?!\d)")


def auditar_literales(texto: str, donde: str = "", ficticios=()) -> list[Hallazgo]:
    """Los CUIT de forma real que están **escritos** en un texto (la semilla de un producto, un fixture).

    Complementa a `auditar_datos_fiscales`: aquélla mira lo que quedó en la base después de sembrar y no ve lo que la
    semilla escribe por una ruta que no corrió (la bandeja de MercadoPago sólo existe con `DEMO_MODE` al importar, y
    ese es el caso de dos productos). Esta mira el texto, sin armar nada: cada secuencia de 11 dígitos con la forma de un
    CUIT (`30-71234567-9`, `30712345679`) que se clasifica `parece_real`. No mira DNI: ocho dígitos sueltos son
    cualquier número.

    Una línea de `ficticios` (sólo dígitos) la acepta, igual que en `auditar_datos_fiscales`. Un hallazgo por CUIT,
    enmascarado y con su número de línea.
    """
    fict = frozenset(d for d in (solo_digitos(x) for x in ficticios) if d)
    salida = []
    for numero, linea in enumerate(texto.splitlines(), 1):
        for m in _CUIT_EN_TEXTO.finditer(linea):
            digitos = "".join(m.groups())
            if clasificar_documento(digitos, fict) == PARECE_REAL:
                salida.append(Hallazgo(
                    "cuit_escrito_parece_real", ALTA, f"{donde or 'texto'}:{numero}", f"ej.: {enmascarar(digitos)}"))
    return salida


# ── La corrida nocturna ────────────────────────────────────────────────────


def _pares(valores: list[str], opcion: str) -> list[tuple[str, str]]:
    salida = []
    for valor in valores:
        tabla, punto, columna = valor.partition(".")
        if not punto or not tabla or not columna:
            raise ValueError(f"{opcion} espera tabla.columna, no {valor!r}")
        salida.append((tabla, columna))
    return salida


def main(argv: list[str] | None = None) -> int:
    """`python -m libracore.demo auditar [--url U] [--tercero tabla.columna] [--empresa tabla.columna] ...`."""
    from libracore.db.core import conectar

    parser = argparse.ArgumentParser(prog="python -m libracore.demo", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="orden", required=True)
    auditar = sub.add_parser("auditar", help="audita los datos fiscales de la demo (salida 0 limpia, 3 hallazgos)")
    auditar.add_argument("--producto", default="",
                         help="prefijo del producto (contalibra, medlibra...): resuelve la base de LibraCore y la del "
                              "dominio con `url_de_instancia`, como las resuelve la app")
    auditar.add_argument("--url", default=os.environ.get("DATABASE_URL", ""),
                         help="la base de LibraCore (por omisión DATABASE_URL; con --producto, la de él)")
    auditar.add_argument("--tercero", action="append", default=[], metavar="TABLA.COLUMNA",
                         help="columna del producto con documentos de terceros (repetible)")
    auditar.add_argument("--empresa", action="append", default=[], metavar="TABLA.COLUMNA",
                         help="columna del producto con el CUIT de la empresa (repetible)")
    auditar.add_argument("--ficticio", action="append", default=[], metavar="CUIT",
                         help="documento que el producto declara inventado (repetible; deuda a la vista)")
    auditar.add_argument("--dudosos", action="store_true", help="incluye los hallazgos de gravedad baja")
    try:
        args = parser.parse_args(argv)
    except SystemExit as salida:
        return SALIDA_USO if salida.code else SALIDA_LIMPIA
    if not config_manager.es_demo():
        print("ABORTA: la instancia no tiene DEMO_MODE=1; no se audita lo que no es una demo.", file=sys.stderr)
        return SALIDA_NO_ES_DEMO
    url, url_producto = args.url, ""
    if args.producto:
        from libracore.db.url_de_instancia import comparte_base_con_el_dominio, url_de_instancia

        url_producto = url_de_instancia(args.producto)
        url = url_de_instancia(args.producto, core=True) or (url_producto if comparte_base_con_el_dominio(args.producto) else "")
    if not url:
        print("ABORTA: falta la base (--producto, --url o DATABASE_URL).", file=sys.stderr)
        return SALIDA_USO
    try:
        terceros, empresa = _pares(args.tercero, "--tercero"), _pares(args.empresa, "--empresa")
    except ValueError as e:
        print(f"ABORTA: {e}", file=sys.stderr)
        return SALIDA_USO
    conn = conectar(url)
    conn_producto = conectar(url_producto) if url_producto and url_producto != url else None
    try:
        hallazgos = auditar_datos_fiscales(
            conn, terceros=terceros, empresa=empresa, ficticios=args.ficticio, incluir_dudosos=args.dudosos,
            conn_producto=conn_producto)
    finally:
        conn.close()
        if conn_producto is not None:
            conn_producto.close()
    if not hallazgos:
        print("auditoría fiscal de la demo: sin hallazgos")
        return SALIDA_LIMPIA
    print(f"ERROR auditoría fiscal de la demo: {len(hallazgos)} hallazgo(s)")
    for h in hallazgos:
        print(f"  {h}")
    return SALIDA_HALLAZGOS


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
