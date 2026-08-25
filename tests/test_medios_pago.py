"""El vocabulario de medios de pago.

🔴 **Lo que hay que proteger no es la lista, es la asimetría entre las dos.**
`ELEGIBLES` puede crecer sin que pase nada malo; `HISTORICOS` no puede
*encoger*, porque sacar una grafía de ahí no borra la fila que la tiene — la
deja sin etiqueta, y un cierre de caja con un bucket sin nombre es peor que uno
con un nombre viejo.

Eso no lo protege ningún tipo ni ningún linter, así que va acá.
"""
import json

import pytest

from libracore import medios_pago
from libracore.db import caja, core


# ── Las dos listas ─────────────────────────────────────────────────────────

def test_los_seis_de_siempre_siguen_siendo_elegibles():
    """🔴 El test que impide una regresión silenciosa. Estos seis vienen de
    antes de que existiera este módulo y hay instancias en producción con cajas
    configuradas sobre ellos: sacar uno dejaría un medio guardado que la caja ya
    no ofrece, y el cobro pasa a no poder registrarse."""
    for medio in ("efectivo", "transferencia", "mercadopago",
                  "cuenta_dni", "billetera", "cuenta_corriente"):
        assert medios_pago.es_elegible(medio), medio


def test_la_tarjeta_va_partida_en_debito_y_credito():
    """No es cosmético: son dos condiciones de venta distintas ante ARCA, y un
    `tarjeta` a secas obliga a adivinar cuál."""
    assert medios_pago.es_elegible("tarjeta_debito")
    assert medios_pago.es_elegible("tarjeta_credito")
    # 🔴 Y el `tarjeta` viejo NO se puede elegir: se lee, no se escribe.
    assert not medios_pago.es_elegible("tarjeta")


def test_ningun_historico_es_elegible():
    """🔴 El control que mantiene separadas a las dos listas. Si un histórico se
    colara en `ELEGIBLES`, el selector volvería a ofrecer la grafía vieja y la
    divergencia que este módulo existe para cerrar se reabriría desde adentro."""
    coladas = [m for m in medios_pago.HISTORICOS if m in medios_pago.ELEGIBLES]
    assert coladas == [], f"históricos ofrecidos como elegibles: {coladas}"


def test_las_grafias_que_ya_estan_en_bases_reales_se_siguen_pudiendo_leer():
    """🔴 **Este test es un trinquete.** Cada una de estas grafías está en filas
    de bases reales, puestas por un producto que las declaraba por su cuenta:

    - `tarjeta` — LibraDesk, LibraClub, MedLibra, Gestiolibra
    - `debito` / `credito` — el enum de LibraClub
    - `qr` — sólo dentro de un `WHERE ... IN (...)`, nunca declarado
    - `otro` — los enums de LibraCargo y LibraClub
    - `cuenta corriente` (con espacio) — los movimientos de la emisión

    Sacar cualquiera de acá pone en rojo este test **a propósito**. Si algún día
    se migran los datos, primero se migran y después se saca la grafía; nunca al
    revés.

    `mercado_pago` estaba en esta lista y **salió el 2026-08-25**, recorriendo
    ese orden. El test que documenta la baja, con la medición que la habilitó,
    es el de acá abajo.
    """
    for medio in ("tarjeta", "debito", "credito",
                  "qr", "otro", "cuenta corriente"):
        assert medios_pago.label(medio) not in ("", medio), (
            f"«{medio}» quedó sin etiqueta: hay filas con ese valor"
        )


def test_mercado_pago_salio_de_historicos_y_no_puede_volver_por_la_ventana():
    """🔴 La única grafía que se retiró, y por qué se pudo.

    `mercado_pago` la escribía VentaLibra. El 2026-08-24 se recorrió el orden
    completo antes de tocar nada:

    1. **Medido** sobre las 24 instancias PostgreSQL del VPS y los 35 archivos
       `.db`, recorriendo TODAS las columnas de texto y no sólo las que se
       llaman "medio": **3 filas, todas en `ventalibra-dev`**
       (`caja_movimientos`, `cc_pagos` y el JSON de `recibos.pagos`). Cero en
       los otros cinco productos.
    2. **Migradas**, con `app/normalizacion_medios.py` de VentaLibra, que además
       corre en **cada arranque** — así una base restaurada desde un backup
       anterior vuelve a quedar canónica sola. Hace falta: los archivos de
       rollback pre-PostgreSQL de ese producto todavía la tienen.
    3. **Verificado cero** después de desplegar.

    Se saca de las TRES listas y no sólo de `HISTORICOS`: una grafía que el
    motor ya no sabe nombrar y sigue apareciendo en un `IN (...)` es ruido que
    el próximo lector interpreta como que todavía existe.
    """
    assert "mercado_pago" not in medios_pago.HISTORICOS
    assert "mercado_pago" not in medios_pago.CONOCIDOS
    assert "mercado_pago" not in medios_pago.EQUIVALENTE_CANONICO
    assert "mercado_pago" not in medios_pago.MEDIOS_ELECTRONICOS
    # Y tampoco se cuela como elegible: la baja es una baja, no un traslado.
    assert not medios_pago.es_elegible("mercado_pago")
    # El control: la grafía BUENA sigue en su lugar en las tres. Sin esto, un
    # módulo vacío pasaría todos los asserts de arriba.
    assert medios_pago.es_elegible("mercadopago")
    assert "mercadopago" in medios_pago.MEDIOS_ELECTRONICOS
    assert medios_pago.label("mercadopago") == "Mercado Pago"


# ── label() ────────────────────────────────────────────────────────────────

def test_un_medio_desconocido_se_devuelve_tal_cual_y_no_vacio():
    """🔴 Y no `"-"`. Un medio que este motor no conoce sólo se descubre si
    alguien lo ve escrito; taparlo con un guión lo esconde justo en la pantalla
    donde alguien podría notarlo."""
    assert medios_pago.label("un-medio-inventado") == "un-medio-inventado"
    assert medios_pago.label("") == ""


def test_la_grafia_retirada_sale_tal_cual_y_eso_es_lo_que_se_quiere():
    """Hasta el 2026-08-25 este test afirmaba que las dos grafías de MercadoPago
    decían lo mismo. Ya no: `mercado_pago` salió de `HISTORICOS`, así que
    `label()` la devuelve cruda.

    🔴 **No es una regresión, es el punto.** Con los datos migrados, ver
    `mercado_pago` escrito en una pantalla significa que apareció una fila nueva
    con la grafía vieja — o sea, que algo la volvió a escribir. Taparla con una
    etiqueta linda sería esconder justamente esa señal, que es la misma razón
    por la que `label()` nunca devuelve `"-"`.
    """
    assert medios_pago.label("mercado_pago") == "mercado_pago"
    assert medios_pago.label("mercadopago") == "Mercado Pago"


# ── canonico() ─────────────────────────────────────────────────────────────

def test_los_historicos_agrupan_con_su_equivalente():
    """Sin esto, un reporte muestra `tarjeta` y `tarjeta_credito` como dos
    filas distintas de la misma cosa."""
    assert medios_pago.canonico("tarjeta") == "tarjeta_credito"
    assert medios_pago.canonico("debito") == "tarjeta_debito"
    assert medios_pago.canonico("cuenta corriente") == "cuenta_corriente"


def test_un_elegible_es_su_propio_canonico():
    """🔴 El control: sin esto, "devolver siempre `tarjeta_credito`" pasaría el
    test de arriba."""
    for medio in medios_pago.ELEGIBLES:
        assert medios_pago.canonico(medio) == medio


def test_qr_y_otro_no_tienen_equivalente_a_proposito():
    """Elegirles uno sería inventar información que la fila no tiene: `otro` es
    un cajón de sastre y `qr` no dice con qué billetera se pagó."""
    assert medios_pago.canonico("qr") == "qr"
    assert medios_pago.canonico("otro") == "otro"


# ── validar() ──────────────────────────────────────────────────────────────

def test_un_medio_inventado_no_valida():
    with pytest.raises(medios_pago.MedioDePagoInvalido) as e:
        medios_pago.validar("tarjeta_regalo")
    # El mensaje dice cuáles SÍ: sin eso, quien integra tiene que adivinar.
    assert "efectivo" in str(e.value)


def test_un_historico_tampoco_valida():
    """🔴 Es la mitad que hace que la normalización avance en vez de quedarse.
    Leer `tarjeta` sí; escribirlo, no — si no, los productos que hoy lo mandan
    nunca migrarían."""
    with pytest.raises(medios_pago.MedioDePagoInvalido):
        medios_pago.validar("tarjeta")


def test_un_elegible_valida_y_se_devuelve():
    """🔴 El control: sin esto, "rechazar siempre" pasaría los dos de arriba."""
    assert medios_pago.validar("tarjeta_debito") == "tarjeta_debito"


# ── para_selector() ────────────────────────────────────────────────────────

def test_el_selector_de_cobro_no_ofrece_cuenta_corriente():
    """La cuenta corriente no es un medio de cobro: es la marca de que la
    operación se hizo a crédito. Ofrecerla en una pantalla que cobra deja
    registrar un cobro que no cobra nada."""
    ids = [m["id"] for m in medios_pago.para_selector(incluir_cuenta_corriente=False)]
    assert "cuenta_corriente" not in ids
    assert "efectivo" in ids, "el control: la lista no vino vacía"


def test_el_selector_completo_si_la_ofrece():
    ids = [m["id"] for m in medios_pago.para_selector()]
    assert "cuenta_corriente" in ids
    assert len(ids) == len(medios_pago.ELEGIBLES)


def test_el_selector_no_ofrece_ninguna_grafia_vieja():
    ids = {m["id"] for m in medios_pago.para_selector()}
    assert not (ids & set(medios_pago.HISTORICOS))


# ── El alias de compatibilidad ─────────────────────────────────────────────

def test_el_nombre_viejo_sigue_funcionando_y_trae_la_lista_nueva():
    """Seis productos importan `db.caja.MEDIOS_PAGO_LABELS` desde hace meses, y
    varios arman su caja por defecto con `list(...)` de eso. El alias es lo que
    hace que hereden los medios nuevos sin tocar una línea."""
    assert caja.MEDIOS_PAGO_LABELS is medios_pago.ELEGIBLES
    assert "tarjeta_debito" in caja.MEDIOS_PAGO_LABELS


# ── El SQL de medios electrónicos ──────────────────────────────────────────

def test_el_fragmento_sql_nombra_mercadopago_y_ya_no_la_grafia_vieja():
    """🔴 El `IN (...)` inline original no tenía **ninguna** de las dos, así que
    en VentaLibra la referencia del pago de MercadoPago nunca se actualizaba — y
    no se notaba, porque el pago entra igual.

    Desde el 2026-08-25 lleva sólo la canónica: la vieja salió junto con su
    entrada en `HISTORICOS`, después de verificar que no quedaran filas.
    """
    sql = medios_pago.sql_es_electronico("medio")
    assert "'mercadopago'" in sql
    assert "'mercado_pago'" not in sql
    assert sql.startswith("medio IN (")


def test_el_fragmento_sql_respeta_la_columna_que_le_pasan():
    assert medios_pago.sql_es_electronico("vp.medio").startswith("vp.medio IN (")


def test_el_efectivo_no_es_un_medio_electronico():
    """El control: si el fragmento incluyera todo, actualizaría la referencia de
    MercadoPago sobre un pago en efectivo."""
    assert "'efectivo'" not in medios_pago.sql_es_electronico()


# ── La caja que nace con la instancia ──────────────────────────────────────

def test_una_instancia_nueva_nace_con_todos_los_medios_elegibles(tmp_path, crear_schema):
    """🔴 El seed de "Caja Principal" tenía **su propia copia** de la lista, con
    los seis de siempre escritos a mano. Era la séptima declaración del mismo
    vocabulario y la que decide con qué medios nace toda instancia nueva: sumar
    uno a la canónica y olvidarse de este `INSERT` dejaba el medio existiendo y
    no ofrecido en ninguna caja del mundo.

    ⚠️ Esto vale para instancias **nuevas**. Una que ya tiene su caja creada
    conserva los medios que eligió: el movimiento se registra igual —
    `create_caja_movimiento` no valida contra esa lista— y el cierre lo agrupa
    bien, pero el selector de esa caja no ofrece los nuevos hasta que alguien
    los agregue desde Cajas. Es configuración del comercio, no algo que se pise
    solo: un consultorio que no cobra con tarjeta no tiene por qué verla.
    """
    core.configure(str(tmp_path / "instancia.db"))
    conn = core.get_connection()
    try:
        crear_schema(conn)
        fila = conn.execute(
            "SELECT medios_pago FROM cajas WHERE es_default=1"
        ).fetchone()
    finally:
        conn.close()

    medios = json.loads(fila["medios_pago"])
    assert set(medios) == set(medios_pago.ELEGIBLES), (
        "la caja por defecto no nace con la lista canónica"
    )
    # El control positivo: si el JSON viniera vacío, el `set()` de arriba
    # tampoco coincidiría, pero conviene decir qué se esperaba encontrar.
    assert "tarjeta_debito" in medios
