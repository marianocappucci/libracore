"""
Schema componible del motor de datos: las 32 tablas core compartidas por
todos los productos (Contalibra, Restolibra y, a futuro, Citalibra). Cada
producto llama `init_core_schema(conn)` al principio de su propio
`init_db()`, y después agrega sus propias tablas de extensión (ej. el
módulo restaurant de Restolibra) — nunca al revés, y nunca hay tablas
core condicionadas a qué producto está corriendo.

`facturacion_alias` (resolución de cliente por alias de CUIT/email en
pagos MP) y las columnas `productos.estacion`/`productos.vendible` nacieron
en un solo producto pero son genéricas — promovidas a core acá para que
ambos productos las tengan disponibles (ver wiki/entities/libracore.md,
sección "Split de Restolibra", hallazgos de sync). Un producto que no las
usa simplemente no las expone en su UI; no le cuesta nada tenerlas.

Extraído verbatim de `database.py` de Contalibra (el schema canónico de
origen, confirmado columna por columna idéntico a Restolibra salvo estos
dos casos) como parte de la Fase 3 de LibraCore — ver
wiki/entities/libracore.md.
"""
import re
import sqlite3

from .core import Conexion, is_postgres


#: El "ahora" que estampan los `created_at`/`updated_at` de este schema.
#:
#: 🔴 **Es `-3 hours`, no `'localtime'`, y no es un capricho.** `datetime('now')`
#: de SQLite es UTC, y el adaptador de PostgreSQL lo traduce a UTC a propósito
#: para que las dos bases guarden el mismo texto. El resultado era que **todas**
#: las columnas con este DEFAULT quedaban 3 h adelantadas: un comprobante creado
#: a las 22:00 de Argentina se guardaba con fecha del día siguiente. Se midió en
#: la instancia `compulibra` de Contalibra el 2026-08-29 — las 112 filas de
#: `caja_movimientos` y las 81 de `facturas`, no sólo las del cron nocturno.
#:
#: `'localtime'` (que es lo que usa `auth_log.ts`) arregla el reloj **si** el
#: entorno está bien puesto: en SQLite lee la TZ del proceso y en PostgreSQL la
#: de la sesión del servidor. Son dos perillas distintas, las dos fáciles de
#: perder — la del servidor se escribe en el `initdb` y `TZ` no la mueve
#: (2026-08-23). El offset fijo no depende de ninguna de las dos y es
#: exactamente el mismo que `_ar_now()` (`timezone(timedelta(hours=-3))`):
#: Argentina no aplica DST desde 2009.
#:
#: La forma la vigila `tests/db/test_created_at_en_hora_de_argentina.py`: si
#: una tabla nueva nace con el DEFAULT viejo, la suite lo dice.
AHORA_AR = "datetime('now','-3 hours')"


#: Una DECLARACION DE COLUMNA cuyo DEFAULT estampa la hora, en cualquiera de las
#: formas que usa la familia.
#:
#: Se busca la CATEGORIA ("este DEFAULT tiene un reloj adentro") y no el patron
#: viejo: buscar `datetime('now')` dejaria pasar una columna nueva escrita como
#: `DEFAULT CURRENT_TIMESTAMP`, que es lo que usa LibraCommerce y que tiene el
#: mismo problema con otra cara.
#:
#: 🔴 Y se exige el **nombre y el tipo** de la columna delante, no el `DEFAULT`
#: suelto. Sin eso el barrido se cuenta a si mismo: los comentarios y docstrings
#: que explican la convencion nombran las dos formas —estan pegados a quien la
#: implementa, que es de donde salen— y aparecian como hallazgos.
_DEFAULT_CON_RELOJ = re.compile(
    r"^\s*\"?\w+\"?\s+(?:TEXT|TIMESTAMP|DATETIME|INTEGER|NUMERIC|VARCHAR)\b[^,]*?"
    r"DEFAULT\s*(?:\(\s*(?:datetime|date)\s*\(\s*'now'|CURRENT_TIMESTAMP)",
    re.IGNORECASE,
)

#: Las formas que SI estampan hora de Argentina. `'localtime'` entra porque es
#: lo que usa `auth_log.ts`, cuya columna la crea el modelo de libraauth y no
#: este DDL (ver `db/logs.py`).
_FORMAS_AR = (AHORA_AR, "datetime('now','localtime')", "datetime('now', 'localtime')")


def defaults_con_reloj(texto_sql: str) -> list[str]:
    """Las lineas de un DDL que declaran un DEFAULT con la hora adentro.

    Devuelve las lineas normalizadas (un espacio entre palabras), para que el
    llamador pueda listarlas en el mensaje de error y, sobre todo, para que
    pueda comprobar que el barrido **encontro algo**: una lista vacia pasaria
    por verde y el control no diria nada nunca mas.
    """
    return [
        " ".join(linea.split())
        for linea in texto_sql.splitlines()
        if _DEFAULT_CON_RELOJ.search(linea)
    ]


def defaults_fuera_de_hora_ar(texto_sql: str) -> list[str]:
    """De las anteriores, las que estampan una hora que no es la de Argentina.

    Es el chequeo que usan las suites del motor y de los productos: vive aca y
    no copiado en cada repo, por la misma razon por la que `_ar_now()` vive en
    un solo lugar. Un DDL sano devuelve `[]`.
    """
    return [
        linea for linea in defaults_con_reloj(texto_sql)
        if not any(forma in linea for forma in _FORMAS_AR)
    ]


#: Las columnas de texto del esquema actual. La consulta es la misma para los
#: dos tipos de conexion; lo que cambia es como se la ejecuta.
_SQL_COLUMNAS_DE_TEXTO = (
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND data_type = 'text'"
)


def _columnas_de_texto(conexion) -> set[tuple[str, str]]:
    """Acepta un `bind` de SQLAlchemy (las revisiones de Alembic) o una
    `Conexion` de esta casa (las migraciones propias de LibraCommerce).

    Son dos APIs distintas para lo mismo y las dos aparecen en los cinco repos
    que corren este arreglo, asi que la diferencia se resuelve aca y no en cada
    llamador.
    """
    if hasattr(conexion, "dialect"):
        import sqlalchemy as sa

        filas = conexion.execute(sa.text(_SQL_COLUMNAS_DE_TEXTO))
    else:
        filas = conexion.execute(_SQL_COLUMNAS_DE_TEXTO).fetchall()

    # 🔴 En modo OFFLINE (`alembic upgrade --sql`, que es como se RENDERIZA la
    # cadena sin base) el `bind` no ejecuta nada y devuelve `None`. Sin esta
    # linea la revision explota con *"'NoneType' object is not iterable"* y se
    # lleva puesto el render entero -- lo encontro `test_alembic.py` de
    # LibraDesk el 2026-08-29, que renderiza la cadena como parte de su suite.
    #
    # Sin columnas que mirar no se emite ningun ALTER: el script renderizado
    # queda SIN estos `SET DEFAULT`, y eso esta anotado en el docstring de cada
    # revision. Es la limitacion normal de una migracion que depende del estado
    # de la base; el deploy de esta familia corre `upgrade` en linea, no `--sql`.
    if filas is None:
        return set()

    return {(fila[0], fila[1]) for fila in filas}


def _es_postgres(conexion) -> bool:
    """Si LA CONEXION habla PostgreSQL, y no si el proceso esta configurado asi.

    🔴 La version anterior preguntaba `is_postgres()`, que es global del proceso,
    y se rompia justo donde los dos no coinciden: la suite de un producto
    configura una URL de PostgreSQL y despues corre las migraciones de
    LibraCommerce sobre una `sqlite3.Connection` de memoria. El global decia que
    si, la consulta a `information_schema` salia contra SQLite y moria con
    *"no such table: information_schema.columns"*. Lo encontro el CI de
    VentaLibra el 2026-08-29.

    El wrapper de PostgreSQL de esta casa no es una `sqlite3.Connection`, asi que
    la pregunta sobre el objeto alcanza y no depende de ningun estado de afuera.
    """
    if hasattr(conexion, "dialect"):
        return conexion.dialect.name == "postgresql"
    return not isinstance(conexion, sqlite3.Connection)


def alters_para_hora_ar(conexion, columnas, expresion: str = AHORA_AR) -> list[str]:
    """Los `ALTER TABLE ... SET DEFAULT` que pasan a hora de Argentina las
    `columnas` —pares `(tabla, columna)`— de la base a la que apunta `conexion`.

    Lo usan la revision `0003` del motor y las revisiones equivalentes de los
    cuatro productos con DDL propio. Vive aca por las dos partes que tienen que
    decir lo mismo en los cinco repos, y que no son obvias:

    1. **La expresion exacta.** Sale de la misma traduccion que usa el adaptador
       en cada consulta. Estas columnas son TEXT, hay codigo que las parsea con
       `strptime` y los rangos de fecha se comparan lexicograficamente: el
       formato tiene que ser identico, byte por byte, al que escribe el DEFAULT.

    2. 🔴 **Saltear las columnas que no son TEXT.** El DEFAULT nuevo es texto
       (`to_char(...)`): ponerselo a una columna `timestamp` corta con *"default
       expression is of type text"* y **aborta el `upgrade` entero**. No es
       hipotetico — LibraDesk llego al motor desde sus propios modelos de
       SQLAlchemy y en sus seis bases `depositos`, `proveedores` y `usuarios`
       tienen `created_at` como `timestamp` (medido en el VPS el 2026-08-29).
       Esas columnas ademas no necesitan el arreglo: `CURRENT_TIMESTAMP` sale de
       la zona de la sesion, y los 21 servidores estan en hora de Argentina
       desde el 2026-08-24.

    Devuelve `[]` en SQLite, que no tiene `ALTER COLUMN ... SET DEFAULT`: alla
    el DEFAULT nuevo llega al crear la tabla, no al migrarla.
    """
    if not _es_postgres(conexion):
        return []

    from ._postgres import _paramstyle

    sql_default = _paramstyle(f"SELECT {expresion}").removeprefix("SELECT ")
    de_texto = _columnas_de_texto(conexion)
    return [
        f'ALTER TABLE "{tabla}" ALTER COLUMN "{columna}" SET DEFAULT {sql_default}'
        for tabla, columna in columnas
        if (tabla, columna) in de_texto
    ]


def init_core_schema(conn: Conexion):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            address       TEXT,
            cuit_dni      TEXT,
            email         TEXT,
            phone         TEXT,
            iva_condition TEXT DEFAULT '',
            created_at    TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS remitos (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            number         TEXT NOT NULL UNIQUE,
            date           TEXT NOT NULL,
            client_id      INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            client_name    TEXT NOT NULL,
            client_address TEXT,
            client_cuit    TEXT,
            client_email   TEXT,
            client_phone   TEXT,
            items          TEXT NOT NULL,
            subtotal       REAL NOT NULL,
            tax_rate       REAL NOT NULL DEFAULT 0.21,
            tax_amount     REAL NOT NULL,
            total          REAL NOT NULL,
            observations   TEXT,
            pdf_path       TEXT,
            created_at     TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS presupuestos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            number          TEXT NOT NULL UNIQUE,
            date            TEXT NOT NULL,
            valid_until     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pendiente',
            client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            client_name     TEXT NOT NULL,
            client_address  TEXT,
            client_cuit     TEXT,
            client_email    TEXT,
            client_phone    TEXT,
            items           TEXT NOT NULL,
            subtotal        REAL NOT NULL,
            tax_rate        REAL NOT NULL DEFAULT 0.21,
            tax_amount      REAL NOT NULL,
            total           REAL NOT NULL,
            observations    TEXT,
            pdf_path        TEXT,
            remito_id       INTEGER REFERENCES remitos(id),
            created_at      TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS facturas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo            INTEGER NOT NULL,
            punto_venta     INTEGER NOT NULL,
            numero          INTEGER NOT NULL,
            fecha           TEXT NOT NULL,
            cliente_cuit    TEXT,
            cliente_razon   TEXT,
            cliente_iva_cond INTEGER,
            items           TEXT NOT NULL,
            subtotal        REAL NOT NULL,
            iva_amount      REAL NOT NULL,
            total           REAL NOT NULL,
            concepto        INTEGER NOT NULL DEFAULT 1,
            cae             TEXT,
            cae_vto         TEXT,
            observaciones   TEXT,
            pdf_path        TEXT,
            created_at      TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS cajas (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre      TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            medios_pago TEXT NOT NULL DEFAULT '[]',
            activo      INTEGER NOT NULL DEFAULT 1,
            es_default  INTEGER NOT NULL DEFAULT 0,
            -- La sucursal a la que pertenece este mostrador. Sin FK: las
            -- sucursales viven en la base del PRODUCTO. Ver la nota de las
            -- migraciones defensivas, más abajo.
            sucursal_id INTEGER,
            created_at  TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS caja_movimientos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha       TEXT NOT NULL,
            tipo        TEXT NOT NULL,
            concepto    TEXT NOT NULL,
            monto       REAL NOT NULL,
            referencia  TEXT DEFAULT '',
            factura_id  INTEGER,
            created_at  TEXT DEFAULT (datetime('now','-3 hours')),
            turno_id    INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL,
            anulado     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS mp_pagos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mp_payment_id   TEXT NOT NULL UNIQUE,
            status          TEXT,
            monto           REAL,
            payer_email     TEXT,
            payer_name      TEXT,
            factura_id      INTEGER,
            created_at      TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS mp_movimientos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mp_movement_id  TEXT NOT NULL UNIQUE,
            tipo            TEXT,
            monto           REAL,
            fecha           TEXT,
            descripcion     TEXT,
            origen_nombre   TEXT,
            origen_banco    TEXT,
            origen_cbu      TEXT,
            payer_email     TEXT,
            payer_name      TEXT,
            payer_id_type   TEXT,
            payer_id_number TEXT,
            estado_factura  TEXT DEFAULT 'pendiente',
            factura_id      INTEGER,
            created_at      TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS facturacion_alias (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo        TEXT NOT NULL CHECK (tipo IN ('cuit', 'email')),
            valor       TEXT NOT NULL,
            cliente_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            activo      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now','-3 hours')),
            UNIQUE (tipo, valor)
        );

        CREATE TABLE IF NOT EXISTS arca_config (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa         TEXT NOT NULL UNIQUE,
            cuit            TEXT NOT NULL,
            punto_venta     INTEGER NOT NULL,
            clave_path      TEXT NOT NULL,
            certificado_path TEXT NOT NULL,
            ambiente        TEXT DEFAULT 'homologacion',
            activo          INTEGER DEFAULT 1,
            alias           TEXT,
            created_at      TEXT DEFAULT (datetime('now','-3 hours')),
            updated_at      TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS usuarios (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            nombre        TEXT NOT NULL,
            email         TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'operador',
            -- 🔴 BOOLEAN, y es la unica columna `activo` del core que lo es.
            -- `usuarios` es la unica tabla que declaran DOS motores: esta y el
            -- modelo SQLAlchemy de libraauth, que la mapea a `Boolean`. En
            -- SQLite el desacuerdo no se nota -- el tipo declarado no se
            -- aplica --, pero en PostgreSQL gana el que crea la tabla primero y
            -- el otro escribe encima: medido el 2026-08-09, VentaLibra contra
            -- PostgreSQL daba 224 rojos, todos *"column activo is of type
            -- integer but expression is of type boolean"*.
            --
            -- Se alinea ESTE lado y no el modelo porque las tres instancias
            -- PostgreSQL vivas (las de LibraDesk, una de cliente) ya la tienen
            -- `boolean`: asi no se toca ninguna base en produccion. Al reves
            -- habria que ALTERarlas.
            --
            -- `DEFAULT TRUE` y no `DEFAULT 1` porque PostgreSQL no acepta un
            -- entero como default de un booleano. SQLite entiende las dos y
            -- guarda 1 igual (verificado, no supuesto).
            --
            -- ⚠️ Sin punto y coma en estos comentarios, ni siquiera entre
            -- comillas: el `executescript()` del adaptador PostgreSQL parte el
            -- script por ese caracter y cortaria la sentencia al medio. Se
            -- descubrio escribiendo este mismo bloque, dos veces.
            --
            -- Las instancias SQLite que ya existen conservan su `INTEGER`
            -- declarado. Es inocuo -- SQLite no aplica el tipo, y libraauth ya
            -- les escribe True/False hoy -- y no vale un rebuild de tabla.
            activo        BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS modulos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            modulo     TEXT NOT NULL UNIQUE,
            habilitado INTEGER NOT NULL DEFAULT 1,
            plan       TEXT NOT NULL DEFAULT 'estandar'
        );

        CREATE TABLE IF NOT EXISTS productos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo       TEXT UNIQUE,
            nombre       TEXT NOT NULL,
            descripcion  TEXT DEFAULT '',
            precio_venta REAL NOT NULL DEFAULT 0,
            precio_costo REAL NOT NULL DEFAULT 0,
            unidad       TEXT NOT NULL DEFAULT 'u',
            categoria    TEXT DEFAULT '',
            activo       INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT DEFAULT (datetime('now','-3 hours')),
            stock_minimo REAL NOT NULL DEFAULT 0,
            estacion     TEXT DEFAULT '',
            vendible     INTEGER NOT NULL DEFAULT 1,
            tipo         TEXT NOT NULL DEFAULT 'producto' CHECK (tipo IN ('producto', 'servicio'))
        );

        CREATE TABLE IF NOT EXISTS depositos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre      TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            activo      INTEGER NOT NULL DEFAULT 1,
            es_default  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS categorias_producto (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS categorias_egreso (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS proveedores (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre        TEXT NOT NULL,
            cuit_dni      TEXT DEFAULT '',
            email         TEXT DEFAULT '',
            phone         TEXT DEFAULT '',
            address       TEXT DEFAULT '',
            iva_condition TEXT DEFAULT '',
            created_at    TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS egresos (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha            TEXT NOT NULL,
            proveedor_id     INTEGER REFERENCES proveedores(id) ON DELETE SET NULL,
            proveedor_nombre TEXT NOT NULL DEFAULT '',
            tipo_comprobante TEXT NOT NULL DEFAULT 'otro',
            numero           TEXT DEFAULT '',
            categoria        TEXT DEFAULT '',
            concepto         TEXT NOT NULL,
            monto_neto       REAL NOT NULL DEFAULT 0,
            iva_pct          REAL NOT NULL DEFAULT 0,
            iva_monto        REAL NOT NULL DEFAULT 0,
            total            REAL NOT NULL,
            estado           TEXT NOT NULL DEFAULT 'pendiente',
            observaciones    TEXT DEFAULT '',
            usuario_id       INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            created_at       TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS egresos_pagos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            egreso_id   INTEGER NOT NULL REFERENCES egresos(id) ON DELETE CASCADE,
            fecha       TEXT NOT NULL,
            monto       REAL NOT NULL,
            caja_id     INTEGER REFERENCES cajas(id) ON DELETE SET NULL,
            medio_pago  TEXT DEFAULT '',
            referencia  TEXT DEFAULT '',
            usuario_id  INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            created_at  TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS turnos_caja (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id             INTEGER NOT NULL REFERENCES usuarios(id),
            apertura               TEXT NOT NULL,
            cierre                 TEXT,
            monto_inicial          REAL NOT NULL DEFAULT 0,
            monto_declarado_cierre REAL,
            monto_esperado_cierre  REAL,
            estado                 TEXT NOT NULL DEFAULT 'abierto',
            notas                  TEXT DEFAULT '',
            created_at             TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS movimientos_stock (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
            tipo        TEXT NOT NULL,
            cantidad    REAL NOT NULL,
            referencia  TEXT DEFAULT '',
            venta_id    INTEGER REFERENCES ventas(id) ON DELETE SET NULL,
            usuario_id  INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            fecha       TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS ventas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            numero          TEXT NOT NULL UNIQUE,
            fecha           TEXT NOT NULL,
            cliente_id      INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            cliente_nombre  TEXT DEFAULT '',
            items           TEXT NOT NULL,
            subtotal        REAL NOT NULL DEFAULT 0,
            descuento       REAL NOT NULL DEFAULT 0,
            total           REAL NOT NULL DEFAULT 0,
            estado          TEXT NOT NULL DEFAULT 'cobrada',
            factura_id      INTEGER REFERENCES facturas(id) ON DELETE SET NULL,
            remito_id       INTEGER REFERENCES remitos(id) ON DELETE SET NULL,
            usuario_id      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            observaciones   TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now','-3 hours')),
            turno_id        INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL,
            mp_order_id     TEXT DEFAULT '',
            mp_payment_id   TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS ventas_pagos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            venta_id   INTEGER NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
            medio      TEXT NOT NULL,
            monto      REAL NOT NULL,
            referencia TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','-3 hours')),
            estado     TEXT NOT NULL DEFAULT 'aprobado'
                       CHECK (estado IN ('pendiente','aprobado','rechazado','vencido'))
        );

        CREATE TABLE IF NOT EXISTS cuentas_tesoreria (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre        TEXT NOT NULL,
            tipo          TEXT NOT NULL DEFAULT 'banco',
            banco         TEXT DEFAULT '',
            numero        TEXT DEFAULT '',
            descripcion   TEXT DEFAULT '',
            saldo_inicial REAL NOT NULL DEFAULT 0,
            activa        INTEGER NOT NULL DEFAULT 1,
            orden         INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS movimientos_tesoreria (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha             TEXT NOT NULL,
            cuenta_id         INTEGER NOT NULL REFERENCES cuentas_tesoreria(id) ON DELETE CASCADE,
            tipo              TEXT NOT NULL,
            monto             REAL NOT NULL,
            concepto          TEXT NOT NULL DEFAULT '',
            referencia        TEXT DEFAULT '',
            cuenta_destino_id INTEGER REFERENCES cuentas_tesoreria(id) ON DELETE SET NULL,
            transferencia_id  INTEGER,
            usuario_id        INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            created_at        TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS auth_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            evento     TEXT NOT NULL,
            username   TEXT NOT NULL,
            ip         TEXT,
            detalle    TEXT
        );

        CREATE TABLE IF NOT EXISTS listas_precio (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre      TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            es_default  INTEGER NOT NULL DEFAULT 0,
            activa      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS lista_precio_items (
            lista_id    INTEGER NOT NULL REFERENCES listas_precio(id) ON DELETE CASCADE,
            producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
            precio      REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (lista_id, producto_id)
        );

        CREATE TABLE IF NOT EXISTS cc_pagos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            monto       REAL NOT NULL,
            fecha       TEXT NOT NULL,
            concepto    TEXT DEFAULT '',
            referencia  TEXT DEFAULT '',
            medio_pago  TEXT DEFAULT 'efectivo',
            caja_id     INTEGER REFERENCES cajas(id) ON DELETE SET NULL,
            usuario_id  INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            created_at  TEXT DEFAULT (datetime('now','-3 hours'))
        );

        -- Deuda que NO nace de una venta de esta base. Existe porque
        -- VentaLibra tiene sus ventas en el archivo SQLite de LibraCommerce
        -- (acá viven sólo caja y facturas), así que ningún JOIN las alcanza:
        -- registra el débito explícitamente al confirmar la venta fiada.
        -- Simétrica a `cc_pagos`, del signo contrario. Queda vacía en los
        -- productos que no la usan, así que su saldo no cambia.
        CREATE TABLE IF NOT EXISTS cc_debitos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            monto       REAL NOT NULL,
            fecha       TEXT NOT NULL,
            concepto    TEXT DEFAULT '',
            referencia  TEXT DEFAULT '',
            usuario_id  INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            created_at  TEXT DEFAULT (datetime('now','-3 hours'))
        );

        CREATE TABLE IF NOT EXISTS cc_resumenes_enviados (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            fecha        TEXT NOT NULL,
            periodo_desde TEXT NOT NULL,
            periodo_hasta TEXT NOT NULL,
            saldo        REAL NOT NULL DEFAULT 0,
            email        TEXT DEFAULT '',
            estado       TEXT NOT NULL DEFAULT 'ok',
            detalle      TEXT DEFAULT '',
            automatico   INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT DEFAULT (datetime('now','-3 hours'))
        );

        -- El comprobante de que se recibió plata. Hasta acá el recibo era un
        -- PDF que `pdf_generator.generate_pdf_recibo()` armaba en el momento a
        -- partir de los cobros vigentes: sin número, sin registro y **distinto
        -- cada vez que se lo pedía**, porque un cobro posterior sobre la misma
        -- factura cambiaba el papel que el cliente ya se había llevado. Esta
        -- tabla lo convierte en documento.
        --
        -- `pagos` es un snapshot JSON, no un JOIN: es justamente lo que hace
        -- que reimprimirlo devuelva el mismo papel. Mismo criterio que
        -- `facturas.items`. Cada entrada guarda además el `caja_movimiento_id`
        -- que la originó, que es como `libracore.recibos` sabe qué cobros ya
        -- están cubiertos y no los mete en un segundo recibo.
        --
        -- No es fiscal: no lleva CAE ni pasa por ARCA, así que la numeración es
        -- interna y `punto_venta` la elige el producto (default 1). Un recibo
        -- no se borra nunca — se anula, igual que una factura.
        CREATE TABLE IF NOT EXISTS recibos (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            punto_venta       INTEGER NOT NULL DEFAULT 1,
            numero            INTEGER NOT NULL,
            fecha             TEXT NOT NULL,
            cliente_id        INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            cliente_razon     TEXT NOT NULL,
            cliente_cuit      TEXT DEFAULT '',
            cliente_domicilio TEXT DEFAULT '',
            -- 'factura' | 'venta' | 'cc_pago': qué operación se está recibiendo.
            origen_tipo       TEXT NOT NULL,
            origen_id         INTEGER,
            -- Lo que dice el "en concepto de" del PDF. Se guarda armado porque
            -- depende del origen, y el origen puede anularse después.
            concepto          TEXT DEFAULT '',
            total             REAL NOT NULL,
            pagos             TEXT NOT NULL DEFAULT '[]',
            observaciones     TEXT DEFAULT '',
            anulado           INTEGER NOT NULL DEFAULT 0,
            anulado_motivo    TEXT DEFAULT '',
            anulado_at        TEXT DEFAULT '',
            usuario_id        INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            created_at        TEXT DEFAULT (datetime('now','-3 hours'))
        );

        -- La bandeja de lo que **otro producto de la familia** dejó para
        -- facturar acá: cuotas de un contrato de alquiler, un ticket cobrable,
        -- un remito. Nace del puente LibraDesk → Contalibra (ver
        -- wiki/analyses/libradesk-contalibra-puente-facturacion.md).
        --
        -- 🔴 **Existe porque no hay facturas en borrador.** `create_factura()`
        -- se llama con un número ya tomado de ARCA y termina con un CAE: no
        -- hay un estado previo donde algo espere a que una persona lo mire. Si
        -- el productor escribiera directo en `facturas`, cada ítem que mande
        -- consumiría numeración fiscal antes de que nadie lo apruebe, y los
        -- rechazados dejarían huecos en la secuencia. Esta tabla es ese estado
        -- previo, y **no tiene numeración a propósito**.
        --
        -- `origen_id` es TEXT y no INTEGER porque es un id de **otro sistema**:
        -- la bandeja no puede suponer su forma. `origen_producto` está desde el
        -- día uno aunque hoy haya un solo productor — es de la familia, no de
        -- un producto.
        --
        -- `items` es JSON con la misma forma que `facturas.items`, para que
        -- facturar un pendiente sea un prefill y no una traducción.
        CREATE TABLE IF NOT EXISTS comprobantes_pendientes (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            origen_producto   TEXT NOT NULL,
            origen_instancia  TEXT NOT NULL DEFAULT '',
            -- 'cuota_contrato' | 'incidencia' | 'remito' | 'presupuesto'
            origen_tipo       TEXT NOT NULL,
            origen_id         TEXT NOT NULL,
            cliente_id        INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            cliente_cuit      TEXT DEFAULT '',
            cliente_razon     TEXT NOT NULL,
            cliente_domicilio TEXT DEFAULT '',
            fecha_sugerida    TEXT DEFAULT '',
            -- Las fechas de servicio del período que se está cobrando. Importan
            -- en los alquileres, donde la factura de agosto se emite en
            -- septiembre y el período tiene que decir agosto.
            periodo_desde     TEXT DEFAULT '',
            periodo_hasta     TEXT DEFAULT '',
            concepto          TEXT DEFAULT '',
            condicion_venta   TEXT DEFAULT '',
            observaciones     TEXT DEFAULT '',
            items             TEXT NOT NULL DEFAULT '[]',
            -- Derivado de `items` por `libracore.db.comprobantes_pendientes`,
            -- nunca recibido del productor: un solo escritor, sin deriva.
            total             REAL NOT NULL DEFAULT 0,
            estado            TEXT NOT NULL DEFAULT 'pendiente',
            factura_id        INTEGER REFERENCES facturas(id) ON DELETE SET NULL,
            motivo_descarte   TEXT DEFAULT '',
            resuelto_at       TEXT DEFAULT '',
            resuelto_por      TEXT DEFAULT '',
            created_at        TEXT DEFAULT (datetime('now','-3 hours'))
        );

        -- 🔴 **La constraint que hace seguro el reenvío.** El modo de falla
        -- normal entre dos contenedores es el corte a mitad de camino, y la
        -- reacción normal es reintentar. Sin este UNIQUE el reintento duplica
        -- el comprobante y el cliente recibe la misma cuota dos veces.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_comprobantes_pendientes_origen
            ON comprobantes_pendientes(origen_producto, origen_instancia,
                                       origen_tipo, origen_id);
        -- La consulta caliente: la bandeja abre en los pendientes.
        CREATE INDEX IF NOT EXISTS idx_comprobantes_pendientes_estado
            ON comprobantes_pendientes(estado, created_at);
    """)

    if is_postgres():
        # SQLite permite declarar FKs hacia tablas que aparecen más adelante;
        # PostgreSQL exige que la tabla referenciada ya exista. El adaptador
        # omite estas dos FKs durante el script y las agrega acá.
        for constraint, table, column in (
            ("fk_caja_movimientos_turno", "caja_movimientos", "turno_id"),
            ("fk_movimientos_stock_venta", "movimientos_stock", "venta_id"),
        ):
            constraint_exists = conn.execute(
                "SELECT 1 FROM pg_constraint WHERE conname=?", (constraint,)
            ).fetchone()
            if not constraint_exists:
                conn.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
                    f"FOREIGN KEY ({column}) REFERENCES "
                    f"{'turnos_caja' if table == 'caja_movimientos' else 'ventas'}(id) ON DELETE SET NULL"
                )

    # Migraciones defensivas (columnas agregadas después del CREATE original en
    # instancias ya existentes) — se mantienen por compatibilidad con bases de
    # datos que ya corrieron versiones previas de este mismo schema.

    # La caja como mostrador de UNA SUCURSAL.
    #
    # 🔑 **Es opcional y por eso no toca a nadie.** Los cinco productos que usan
    # `cajas` y no tienen sucursales siguen trabajando con la caja por default,
    # exactamente como antes. La estrena LibraClub, que sí las tiene.
    #
    # 🔴 **No lleva FK, y no es un olvido**: las sucursales viven en la base del
    # PRODUCTO y las cajas en la de LibraCore. Es el mismo caso que
    # `reservas.factura_id` de LibraClub, y por el mismo motivo: no hay
    # integridad referencial que declarar entre dos bases.
    #
    # ⚠️ **`turnos_caja.caja_id` ya existe** —lo agrega un ALTER defensivo más
    # abajo, con `ON DELETE SET NULL`, y rellena las filas viejas con la caja por
    # default—. Lo que faltaba no era la columna sino que `create_turno` la
    # escribiera; eso se arregla en `db/turnos.py`, no acá.
    cols_cajas = [r[1] for r in conn.execute("PRAGMA table_info(cajas)").fetchall()]
    if "sucursal_id" not in cols_cajas:
        conn.execute("ALTER TABLE cajas ADD COLUMN sucursal_id INTEGER")
    # El punto de venta de ARCA de este mostrador. **Nullable a propósito**: hasta
    # hoy había uno solo por instancia —el de `arca_config`— y las instancias que
    # sigan así no cambian en nada: una caja sin punto de venta propio usa el de
    # la empresa. Ver `resolver_punto_venta()` en `db/cajas.py`.
    #
    # Existe porque un cliente con varios POS necesita numeración fiscal separada
    # por mostrador: ARCA numera por (tipo, punto de venta), así que dos cajas
    # compartiendo punto de venta comparten la serie y compiten por el próximo
    # número — con el agravante de que el choque lo detecta ARCA, no nosotros.
    if "punto_venta" not in cols_cajas:
        conn.execute("ALTER TABLE cajas ADD COLUMN punto_venta INTEGER")

    # 🔴 **Un pago puede existir y no haber entrado.** Hasta acá una línea de
    # `ventas_pagos` no tenía estado: existía, y por lo tanto contaba. El POS de
    # Contalibra crea la venta con la línea de MercadoPago cargada por el total
    # y el estado sale `cobrada` en el acto, antes de que nadie escanee el QR —
    # y `crear_venta_directa` escribe además el movimiento de caja, así que una
    # venta que nadie paga mete plata en la caja que no entró.
    # Ver `libracore/pagos.py` y el plan en el wiki.
    #
    # 🔑 **El default `'aprobado'` es para las filas que YA existen**, y ése es
    # todo su motivo: lo que está guardado hoy ya cobró, así que el backfill no
    # puede mover un solo número. La contracara es que un `INSERT` que se olvide
    # la columna también queda en `aprobado` — el default peligroso que
    # `pagos.estado_de()` se niega a tomar. **Lo que cierra ese hueco es el
    # camino de escritura**, en `db/ventas.py`, donde el estado pasa a ser
    # obligatorio; no se puede cerrar acá sin romper la migración de las filas
    # viejas.
    #
    # El `CHECK` sí es de acá: un estado inventado no entra ni por error de
    # tipeo, y el vocabulario queda declarado en la base.
    cols_pagos = [r[1] for r in conn.execute("PRAGMA table_info(ventas_pagos)").fetchall()]
    if "estado" not in cols_pagos:
        conn.execute(
            "ALTER TABLE ventas_pagos ADD COLUMN estado TEXT NOT NULL DEFAULT 'aprobado' "
            "CHECK (estado IN ('pendiente','aprobado','rechazado','vencido'))"
        )

    cols = [r[1] for r in conn.execute("PRAGMA table_info(clients)").fetchall()]
    if "iva_condition" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN iva_condition TEXT DEFAULT ''")
    if "activo" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN activo INTEGER DEFAULT 1")
    if "auto_facturar" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN auto_facturar INTEGER NOT NULL DEFAULT 0")
    if "cc_resumen_auto" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN cc_resumen_auto INTEGER NOT NULL DEFAULT 0")
    if "cc_resumen_frecuencia" not in cols:
        conn.execute(
            "ALTER TABLE clients ADD COLUMN cc_resumen_frecuencia TEXT NOT NULL DEFAULT 'mensual' "
            "CHECK (cc_resumen_frecuencia IN ('semanal', 'quincenal', 'mensual'))"
        )
    if "cc_resumen_ultimo_envio" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN cc_resumen_ultimo_envio TEXT DEFAULT ''")
    if "external_ref" not in cols:
        # Quién es este cliente en el producto que lo dio de alta (ej.
        # `party-7`). Lo necesita un producto cuyos clientes viven en otra
        # base y que igual quiere llevarles cuenta corriente acá: sin esto no
        # hay forma de volver a encontrar al mismo deudor en la segunda venta
        # fiada. Queda NULL en Contalibra/Restolibra, donde `clients` ES la
        # tabla de clientes.
        conn.execute("ALTER TABLE clients ADD COLUMN external_ref TEXT")

    fact_cols = [r[1] for r in conn.execute("PRAGMA table_info(facturas)").fetchall()]
    if "cliente_domicilio" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN cliente_domicilio TEXT DEFAULT ''")
    if "fch_serv_desde" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN fch_serv_desde TEXT DEFAULT ''")
    if "fch_serv_hasta" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN fch_serv_hasta TEXT DEFAULT ''")
    if "fch_vto_pago" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN fch_vto_pago TEXT DEFAULT ''")
    if "cbte_asoc_tipo" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN cbte_asoc_tipo INTEGER DEFAULT 0")
    if "cbte_asoc_pv" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN cbte_asoc_pv INTEGER DEFAULT 0")
    if "cbte_asoc_nro" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN cbte_asoc_nro INTEGER DEFAULT 0")
    if "condicion_venta" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN condicion_venta TEXT DEFAULT ''")
    if "usuario_id" not in fact_cols:
        conn.execute("ALTER TABLE facturas ADD COLUMN usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL")

    prod_cols = [r[1] for r in conn.execute("PRAGMA table_info(productos)").fetchall()]
    if "stock_minimo" not in prod_cols:
        conn.execute("ALTER TABLE productos ADD COLUMN stock_minimo REAL NOT NULL DEFAULT 0")
    if "estacion" not in prod_cols:
        conn.execute("ALTER TABLE productos ADD COLUMN estacion TEXT DEFAULT ''")
    if "vendible" not in prod_cols:
        conn.execute("ALTER TABLE productos ADD COLUMN vendible INTEGER NOT NULL DEFAULT 1")
    if "tipo" not in prod_cols:
        conn.execute(
            "ALTER TABLE productos ADD COLUMN tipo TEXT NOT NULL DEFAULT 'producto' "
            "CHECK (tipo IN ('producto', 'servicio'))"
        )

    remito_cols = [r[1] for r in conn.execute("PRAGMA table_info(remitos)").fetchall()]
    if remito_cols and "usuario_id" not in remito_cols:
        conn.execute("ALTER TABLE remitos ADD COLUMN usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL")

    pres_cols = [r[1] for r in conn.execute("PRAGMA table_info(presupuestos)").fetchall()]
    if pres_cols and "usuario_id" not in pres_cols:
        conn.execute("ALTER TABLE presupuestos ADD COLUMN usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL")

    caja_cols = [r[1] for r in conn.execute("PRAGMA table_info(caja_movimientos)").fetchall()]
    if caja_cols and "usuario_id" not in caja_cols:
        conn.execute("ALTER TABLE caja_movimientos ADD COLUMN usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL")

    mp_cols = [r[1] for r in conn.execute("PRAGMA table_info(mp_pagos)").fetchall()]
    if mp_cols and "estado_factura" not in mp_cols:
        conn.execute("ALTER TABLE mp_pagos ADD COLUMN estado_factura TEXT DEFAULT NULL")
    if mp_cols and "payment_type" not in mp_cols:
        conn.execute("ALTER TABLE mp_pagos ADD COLUMN payment_type TEXT DEFAULT NULL")
    if mp_cols and "payment_method" not in mp_cols:
        conn.execute("ALTER TABLE mp_pagos ADD COLUMN payment_method TEXT DEFAULT NULL")
    if mp_cols and "descripcion_mp" not in mp_cols:
        conn.execute("ALTER TABLE mp_pagos ADD COLUMN descripcion_mp TEXT DEFAULT NULL")
    if mp_cols and "payer_id_type" not in mp_cols:
        conn.execute("ALTER TABLE mp_pagos ADD COLUMN payer_id_type TEXT DEFAULT NULL")
    if mp_cols and "payer_id_number" not in mp_cols:
        conn.execute("ALTER TABLE mp_pagos ADD COLUMN payer_id_number TEXT DEFAULT NULL")

    # Caja principal por defecto
    if conn.execute("SELECT COUNT(*) FROM cajas").fetchone()[0] == 0:
        import json as _json

        from libracore import medios_pago as _medios

        # 🔴 De la lista canónica, **no de una copia escrita acá**. Era la
        # séptima declaración del mismo vocabulario, y la que decidía con qué
        # medios nace toda instancia nueva: agregar uno a `caja.py` y olvidarse
        # de este `INSERT` dejaba el medio existiendo y no ofrecido.
        _todos_medios = _json.dumps(list(_medios.ELEGIBLES))
        cur = conn.execute(
            "INSERT INTO cajas (nombre, descripcion, medios_pago, es_default) VALUES (?,?,?,1)",
            ("Caja Principal", "Caja por defecto del sistema", _todos_medios),
        )
        _default_caja_id = cur.lastrowid
    else:
        _row = conn.execute("SELECT id FROM cajas WHERE es_default=1 LIMIT 1").fetchone()
        _default_caja_id = _row[0] if _row else conn.execute(
            "SELECT id FROM cajas ORDER BY id LIMIT 1"
        ).fetchone()[0]

    cm_cols = [r[1] for r in conn.execute("PRAGMA table_info(caja_movimientos)").fetchall()]
    if cm_cols and "caja_id" not in cm_cols:
        conn.execute("ALTER TABLE caja_movimientos ADD COLUMN caja_id INTEGER REFERENCES cajas(id) ON DELETE SET NULL")
    if cm_cols and "medio_pago" not in cm_cols:
        conn.execute("ALTER TABLE caja_movimientos ADD COLUMN medio_pago TEXT DEFAULT ''")
    # Las filas que ya estaban nacen como NO anuladas, que es lo que eran.
    if cm_cols and "anulado" not in cm_cols:
        conn.execute(
            "ALTER TABLE caja_movimientos ADD COLUMN anulado INTEGER NOT NULL DEFAULT 0"
        )
    # El arqueo se cuenta sobre la caja, no sobre las ventas: con `turno_id`
    # el resumen de un turno sale de `caja_movimientos` directo.
    # Contalibra/Restolibra lo derivan hoy de `ventas.turno_id`, que solo
    # sirve si el producto usa la tabla `ventas` de LibraCore -- VentaLibra
    # no la usa (sus ventas viven en LibraCommerce) y sin esto no tenia forma
    # de arquear. Nullable a proposito: un movimiento que no nace de un turno
    # (ajuste, egreso fuera de caja) sigue siendo valido sin turno.
    if cm_cols and "turno_id" not in cm_cols:
        conn.execute(
            "ALTER TABLE caja_movimientos ADD COLUMN turno_id INTEGER "
            "REFERENCES turnos_caja(id) ON DELETE SET NULL"
        )

    tc_cols = [r[1] for r in conn.execute("PRAGMA table_info(turnos_caja)").fetchall()]
    if tc_cols and "caja_id" not in tc_cols:
        conn.execute("ALTER TABLE turnos_caja ADD COLUMN caja_id INTEGER REFERENCES cajas(id) ON DELETE SET NULL")

    # 🔴 **El relleno va FUERA del `if`, y ese es el arreglo.** Estaba adentro
    # —corría sólo cuando la columna se acababa de crear—, así que en toda base
    # donde `caja_id` **ya existía** las filas viejas se quedaban en `NULL` para
    # siempre. La promesa de "las filas viejas quedan con la caja por defecto"
    # se cumplía sólo en bases nuevas, que son justo las que no tienen filas
    # viejas.
    #
    # Lo destapó LibraClub el 2026-08-28: su pantalla mostraba *"Turno abierto —
    # sin caja asignada"* sobre turnos de una semana antes, en una base donde la
    # columna venía de una versión anterior. Lo reportó el humano.
    #
    # Idempotente y barato: el `WHERE ... IS NULL` no toca ninguna fila cuando
    # no quedan, que es el caso normal a partir de la primera corrida.
    conn.execute(
        "UPDATE caja_movimientos SET caja_id=? WHERE caja_id IS NULL",
        (_default_caja_id,),
    )
    conn.execute(
        "UPDATE turnos_caja SET caja_id=? WHERE caja_id IS NULL",
        (_default_caja_id,),
    )

    ms_cols = [r[1] for r in conn.execute("PRAGMA table_info(movimientos_stock)").fetchall()]
    if ms_cols and "deposito_id" not in ms_cols:
        conn.execute("ALTER TABLE movimientos_stock ADD COLUMN deposito_id INTEGER REFERENCES depositos(id) ON DELETE SET NULL")

    # Depósito principal por defecto
    if conn.execute("SELECT COUNT(*) FROM depositos").fetchone()[0] == 0:
        cur = conn.execute(
            "INSERT INTO depositos (nombre, descripcion, es_default) VALUES (?,?,1)",
            ("Depósito Principal", "Depósito por defecto del sistema"),
        )
        default_id = cur.lastrowid
        conn.execute(
            "UPDATE movimientos_stock SET deposito_id=? WHERE deposito_id IS NULL",
            (default_id,),
        )

    # Índices mínimos sobre las tablas de mayor tráfico (reportes, filtros por
    # fecha/cliente) — hallazgo cruzado desde la auditoría de Restolibra.
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_clients_cuit_norm ON clients(REPLACE(cuit_dni, '-', ''));
        CREATE INDEX IF NOT EXISTS idx_facturas_fecha ON facturas(fecha);
        CREATE INDEX IF NOT EXISTS idx_facturas_cliente_cuit ON facturas(cliente_cuit);
        CREATE INDEX IF NOT EXISTS idx_ventas_fecha ON ventas(fecha);
        CREATE INDEX IF NOT EXISTS idx_caja_movimientos_fecha ON caja_movimientos(fecha);
        CREATE INDEX IF NOT EXISTS idx_cc_pagos_cliente ON cc_pagos(cliente_id);
        CREATE INDEX IF NOT EXISTS idx_cc_debitos_cliente ON cc_debitos(cliente_id);
        -- Parcial: la referencia es opcional (un débito cargado a mano no
        -- tiene ninguna), pero cuando existe identifica la venta que lo
        -- originó y no puede repetirse -- es lo que hace que reintentar un
        -- cobro no fíe dos veces lo mismo.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cc_debitos_referencia
            ON cc_debitos(referencia) WHERE referencia != '';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_external_ref
            ON clients(external_ref) WHERE external_ref IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_cc_resumenes_cliente ON cc_resumenes_enviados(cliente_id, fecha);
        CREATE INDEX IF NOT EXISTS idx_movimientos_stock_producto ON movimientos_stock(producto_id);
        CREATE INDEX IF NOT EXISTS idx_recibos_cliente ON recibos(cliente_id, fecha);
        -- Buscar por origen es la consulta caliente del módulo: es la que
        -- responde "¿esta factura/venta/pago ya tiene recibo?" antes de emitir.
        CREATE INDEX IF NOT EXISTS idx_recibos_origen ON recibos(origen_tipo, origen_id);
        -- La numeración no puede repetirse. A diferencia de facturas, esta tabla
        -- nace con el índice puesto (no hay instancias previas con duplicados
        -- posibles), así que va acá y no en el bloque defensivo de abajo.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_recibos_numero_unico
            ON recibos(punto_venta, numero);
    """)

    # UNIQUE aparte (no en el executescript de arriba): si por algún motivo ya
    # existieran duplicados de tipo+punto_venta+numero en una instancia (no
    # debería, pero es defensivo), que falle solo esto sin tumbar el resto de
    # init_db al arrancar la app. Cierra la race condition de numeración
    # (hallazgo cruzado desde la auditoría de Restolibra) junto con el retry
    # en `create_factura()`.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_numero_unico "
            "ON facturas(tipo, punto_venta, numero)"
        )
    except sqlite3.Error as e:
        print(f"[WARN] No se pudo crear idx_facturas_numero_unico (¿hay duplicados "
              f"de tipo+punto_venta+numero?): {e}")

    # Categorías de egreso: seed inicial, solo inserta las que no existen aún.
    _CATEGORIAS_EGRESO_DEFAULT = [
        "Mercadería / Materias primas",
        "Alquiler",
        "Servicios (luz, gas, internet)",
        "Sueldos y honorarios",
        "Impuestos y tasas",
        "Transporte y logística",
        "Mantenimiento y reparaciones",
        "Publicidad y marketing",
        "Bancarios y financieros",
        "Otros",
    ]
    for cat in _CATEGORIAS_EGRESO_DEFAULT:
        conn.execute("INSERT OR IGNORE INTO categorias_egreso (nombre) VALUES (?)", (cat,))
