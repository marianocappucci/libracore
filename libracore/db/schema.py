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
import sqlite3


def init_core_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            address       TEXT,
            cuit_dni      TEXT,
            email         TEXT,
            phone         TEXT,
            iva_condition TEXT DEFAULT '',
            created_at    TEXT DEFAULT (datetime('now'))
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
            created_at     TEXT DEFAULT (datetime('now'))
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
            created_at      TEXT DEFAULT (datetime('now'))
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
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS cajas (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre      TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            medios_pago TEXT NOT NULL DEFAULT '[]',
            activo      INTEGER NOT NULL DEFAULT 1,
            es_default  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS caja_movimientos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha       TEXT NOT NULL,
            tipo        TEXT NOT NULL,
            concepto    TEXT NOT NULL,
            monto       REAL NOT NULL,
            referencia  TEXT DEFAULT '',
            factura_id  INTEGER,
            created_at  TEXT DEFAULT (datetime('now')),
            turno_id    INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS mp_pagos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mp_payment_id   TEXT NOT NULL UNIQUE,
            status          TEXT,
            monto           REAL,
            payer_email     TEXT,
            payer_name      TEXT,
            factura_id      INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
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
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS facturacion_alias (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo        TEXT NOT NULL CHECK (tipo IN ('cuit', 'email')),
            valor       TEXT NOT NULL,
            cliente_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            activo      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
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
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS usuarios (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            nombre        TEXT NOT NULL,
            email         TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'operador',
            activo        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT DEFAULT (datetime('now'))
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
            created_at   TEXT DEFAULT (datetime('now')),
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
            created_at  TEXT DEFAULT (datetime('now'))
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
            created_at    TEXT DEFAULT (datetime('now'))
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
            created_at       TEXT DEFAULT (datetime('now'))
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
            created_at  TEXT DEFAULT (datetime('now'))
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
            created_at             TEXT DEFAULT (datetime('now'))
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
            created_at  TEXT DEFAULT (datetime('now'))
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
            created_at      TEXT DEFAULT (datetime('now')),
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
            created_at TEXT DEFAULT (datetime('now'))
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
            created_at    TEXT DEFAULT (datetime('now'))
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
            created_at        TEXT DEFAULT (datetime('now'))
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
            created_at  TEXT DEFAULT (datetime('now'))
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
            created_at  TEXT DEFAULT (datetime('now'))
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
            created_at  TEXT DEFAULT (datetime('now'))
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
            created_at   TEXT DEFAULT (datetime('now'))
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
            created_at        TEXT DEFAULT (datetime('now'))
        );
    """)

    # Migraciones defensivas (columnas agregadas después del CREATE original en
    # instancias ya existentes) — se mantienen por compatibilidad con bases de
    # datos que ya corrieron versiones previas de este mismo schema.
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
        _todos_medios = _json.dumps([
            "efectivo", "transferencia", "mercadopago",
            "cuenta_dni", "billetera", "cuenta_corriente",
        ])
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
        conn.execute("UPDATE caja_movimientos SET caja_id=? WHERE caja_id IS NULL", (_default_caja_id,))
    if cm_cols and "medio_pago" not in cm_cols:
        conn.execute("ALTER TABLE caja_movimientos ADD COLUMN medio_pago TEXT DEFAULT ''")
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
        conn.execute("UPDATE turnos_caja SET caja_id=? WHERE caja_id IS NULL", (_default_caja_id,))

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
