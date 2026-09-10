"""
Módulos habilitados por plan. Extraído de database.py de Contalibra/
Restolibra (idéntico en ambos) como parte de la migración real a
libracore.db (Fase 3 de LibraCore, ver wiki/entities/libracore.md).
"""
from libracore.db.core import get_connection, is_postgres


def _valor_habilitado(conn, habilitado: bool):
    """El valor a escribir en `modulos.habilitado`, según el tipo REAL de la columna.

    🔴 **`habilitado` no tiene el mismo tipo en toda la familia**, y no hay un
    valor que sirva para las dos mitades. Medido el 2026-09-09 contra las bases
    vivas del VPS:

    | Tipo de la columna | Instancias |
    |---|---|
    | `integer` | contalibra, restolibra, ventalibra |
    | `boolean` | libradesk, gestiolibra, medlibra |

    PostgreSQL no castea implícitamente entre los dos, así que un `1` contra la
    columna `boolean` muere con `column "habilitado" is of type boolean but
    expression is of type smallint`, y un `True` contra la `integer` muere al
    revés. La divergencia viene de que unas tablas las creó
    `init_core_schema()` (`INTEGER`) y otras el Alembic propio del producto
    (`sa.Boolean()`); no la introduce este código y arreglarla es una migración
    aparte — lo que este helper hace es que el motor funcione **hoy** contra las
    dos, en vez de andar en la mitad del parque.

    La implementación de `set_addon` vivió tres semanas en Contalibra sin que
    esto se notara porque allá la columna es `integer`. Se destapó al moverla al
    motor y probarla contra LibraDesk.

    En SQLite no hace falta preguntar: es de tipado dinámico y guarda `1`/`0`
    en cualquiera de los dos casos.
    """
    if not is_postgres():
        return int(habilitado)
    fila = conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='modulos' AND column_name='habilitado'"
    ).fetchone()
    if fila is not None and fila["data_type"] == "boolean":
        return bool(habilitado)
    return int(habilitado)


def get_modulos() -> dict[str, bool]:
    """Devuelve {modulo: habilitado} para todos los módulos registrados."""
    with get_connection() as conn:
        rows = conn.execute("SELECT modulo, habilitado FROM modulos").fetchall()
    return {r["modulo"]: bool(r["habilitado"]) for r in rows}


def set_addon(nombre: str, habilitado: bool) -> None:
    """Habilita o deshabilita un add-on (módulo suelto) en ESTA instancia.

    Un add-on (`plans.ADDONS`, ej. `mayorista` en Contalibra, `modo_simple` en
    LibraDesk) no pertenece a ningún plan, así que `apply_plan` no lo toca; se
    prende/apaga por instancia con esta función. Idempotente: crea la fila si
    falta (con `plan="addon"`, igual que el seed) y setea `habilitado`.

    NO valida que `nombre` sea un add-on real — eso lo hace quien llama
    (`libracore.admin.services.set_addon` o la CLI `panel_admin.py addon`)
    contra `plans.ADDONS`, que es quien decide qué es un add-on.

    **Vive acá y no en cada producto a propósito.** Hasta el 2026-09-09 la
    única implementación estaba en `contalibra/app/db_modulos.py`, y el
    backoffice —que la invoca por `docker exec` como `app.database.set_addon`—
    daba por sentado que todos los productos la tenían. LibraDesk adoptó
    `ADDONS` sin ella y el toggle quedó roto; ver el comentario de
    `libracore.admin.services.addons_de_instancia`. Un producto que sume
    add-ons reexporta esta, no copia otra.

    `INSERT OR IGNORE` lo traduce la capa dual a `ON CONFLICT DO NOTHING`
    (`db/_postgres.py`), así que el mismo SQL sirve en los dos motores.
    """
    with get_connection() as conn:
        on = _valor_habilitado(conn, habilitado)
        conn.execute(
            "INSERT OR IGNORE INTO modulos (modulo, habilitado, plan) VALUES (?,?,?)",
            (nombre, on, "addon"),
        )
        conn.execute("UPDATE modulos SET habilitado=? WHERE modulo=?", (on, nombre))


def apply_plan(plan: str):
    """Habilita/deshabilita módulos según el plan elegido.

    El mapeo plan→módulos vive en `plans.py` en la raíz de cada producto
    (fuente de verdad compartida con el backoffice), para que lo que se
    habilita coincida con lo que se vende. Import local (no a nivel de
    módulo): `plans.py` es específico de cada producto, no vive en
    LibraCore — `import plans` resuelve al `plans.py` del producto que
    esté llamando esta función porque cada uno agrega su propia raíz de
    repo a `sys.path` al arrancar.

    **Add-ons opcionales (`plans.ADDONS`) quedan afuera de todo plan.** Un
    add-on es un módulo pago que se habilita/deshabilita por instancia y
    NO pertenece a ningún plan; si esta función lo tocara, subir o bajar
    de plan lo apagaría solo (un adicional que se desactiva en silencio).
    Por eso las claves de `plans.ADDONS` se saltean: ni su `habilitado` ni
    su `plan` se tocan acá. Se lee con `getattr` y default `set()` para no
    exigirle a cada producto declarar el atributo: un `plans.py` sin
    `ADDONS` (Restolibra, etc.) se comporta exactamente como antes.
    """
    import plans
    activos = plans.modulos_de_plan(plan)
    addons = getattr(plans, "ADDONS", set())
    with get_connection() as conn:
        rows = conn.execute("SELECT modulo FROM modulos").fetchall()
        for r in rows:
            if r["modulo"] in addons:
                continue
            conn.execute(
                "UPDATE modulos SET habilitado=?, plan=? WHERE modulo=?",
                (1 if r["modulo"] in activos else 0, plan, r["modulo"]),
            )
