# Changelog — LibraCore

La versión real la determina el tag de Git (`vX.Y.Z`, vía `hatch-vcs` — ver
`README.md`, sección Versionado). Este archivo no la reemplaza: registra QUÉ
cambió en cada minor, para que un consumidor sepa si tiene que correr una
migración antes de actualizar el pin. Se empieza a mantener con esta entrada;
las versiones anteriores están en la historia de Git y en la bitácora del wiki
del ecosistema.

## [Sin publicar]

### Corregido

- 🔴 **`panel_admin restore-db` y `list-backups` ven el motor de la instancia.**
  `cmd_backup` distingue PostgreSQL de SQLite desde el 2026-08-10, pero los dos
  comandos que **leen** esos respaldos habían quedado con el glob de `*.db`.
  Contra una instancia migrada:
  - `list-backups` decía *«Sin backups de DB»* sobre una instancia
    perfectamente respaldada, porque sus respaldos son los `.dump` de
    `pg_dump`. Ahora lista los dos formatos, dice con qué motor corre la
    instancia y marca los que no le sirven.
  - `restore-db` copiaba un `.db` sobre `data/<db>.db` —un archivo que en esa
    instancia no lee nadie—, imprimía `[OK] DB restaurada` y no cambiaba un
    solo dato. Ahora **se niega**, con dos señales independientes (el
    contenedor declara una URL PostgreSQL, o no existe el archivo SQLite), y
    explica cuál es el camino real.

  Restaurar un `.dump` con `pg_restore` **sigue sin estar automatizado**: la
  decisión de adaptar el comando o retirarlo está abierta. Lo que se cierra
  acá es el camino silencioso al desastre.

  De paso: la selección interactiva indexaba sobre una lista distinta de la que
  imprimía, así que al listar los dos formatos el número elegido habría
  apuntado a otra fila. Ahora es la misma lista.

## [v1.101.0] — Tres huecos del cobro por QR de ventas (plan ERP de VentaLibra)

### Agregado

- `ventas_cobro_router.build_cobro_de_ventas_router` recibe `facturacion_habilitada: Callable[[], bool]`
  (default `True`, lo de hoy). VentaLibra tiene un módulo `facturacion` que se puede apagar,
  independiente de `mp_auto_facturar_ventas`: con la automática prendida y el módulo apagado,
  `GET /{vid}/mp-status` facturaba igual al aprobarse el QR. Con `False`: `mp-status` acredita el
  pago pero no factura (ninguna de las dos ramas, la del pago recién aprobado y la del
  `mp_payment_id` ya seteado), y `POST /{vid}/facturar` contesta **403** en vez de emitir.

### Corregido (para todos — Contalibra y Restolibra incluidos)

- `POST /{vid}/mp-qr` ya no genera una orden nueva sobre una venta anulada ni sobre una que ya fue
  cobrada por QR (`mp_payment_id` ya seteado): devuelve **409** sin llamar a MercadoPago. Antes
  reabría el cartel de cobro pidiendo de nuevo una plata que ya había entrado, o por algo que ya
  no existía. **No** rechaza una venta con un pago electrónico ya declarado `aprobado` pero sin
  `mp_payment_id`: ese es el flujo de "Cobrar con QR" del detalle de venta
  (`libra-ui/src/comercio/VentaDetalle.tsx`, `puedeCobrarConQr`), que registra el pago antes de
  poner el QR y necesita esa fila para sellar la referencia después.
- `GET /{vid}/mp-status` ya no responde `"approved"` ni factura cuando la venta está anulada.
  Desde `libracommerce` v0.16.1 `erp.ventas.acreditar_pago_qr` no toca nada en ese caso —la plata
  entró en MercadoPago pero hay que devolverla a mano—, y el router no miraba esa señal: ahora
  responde `{"status": "anulada", "payment_id": ..., "message": ...}` en las dos ramas (la del pago
  recién encontrado aprobado y la del `mp_payment_id` ya seteado).

### Para los consumidores

- **Sin migración y sin cambio de comportamiento para Contalibra y Restolibra** en el caso feliz:
  ninguna de las dos instancias tiene ventas anuladas con QR pendiente ni un módulo de
  facturación propio (`facturacion_habilitada` queda en su default `True`). Las dos correcciones
  "para todos" solo cambian algo que ya era un error (pedir de nuevo una plata que entró, o
  facturar una venta anulada).

## [v1.100.0] — Cuenta corriente cuando el party no es el cliente (plan ERP de VentaLibra)

### Agregado

- `libracore.db.cuenta_corriente.VENTAS_LIBRACOMMERCE_POR_EXTERNAL_REF`: origen de ventas para
  cuando `sales.customer_party_id` NO coincide con `clients.id`. Es el caso de VentaLibra, que da
  de alta el cliente como party primero y enlaza el cliente de LibraCore por
  `clients.external_ref = 'party-<id>'`. Resuelve el cliente de cada venta por esa referencia.
  `get_cc_saldo`, `get_cc_movimientos` y `get_clientes_con_saldo_cc` lo aceptan como cualquier
  otro origen, y `build_cuenta_corriente_router` ya lo recibía por parámetro.
- `cc_resumen.calcular_periodo`, `enviar_resumen` y `enviar_resumenes_pendientes` reciben
  `origen`. El default es el de hoy, así que el resumen por mail de un producto así cuenta la
  deuda del cliente correcto.

### Para los consumidores

- **Sin migración.** Nada cambia para quien no pase el origen nuevo: Contalibra y Restolibra
  siguen con `VENTAS_LIBRACOMMERCE`, donde `clients.id == parties.id` es un invariante del motor.

## [v1.99.0] — El vuelto en `ventas_pagos` (plan ERP de VentaLibra, F2)

### Agregado

- Migración `0010_recibido_en_ventas_pagos`: columna `ventas_pagos.recibido`,
  lo que entregó el cliente por un pago (para el vuelto). Nullable y sin
  default: `NULL` = no se registró, que es el estado de todas las filas
  anteriores y de lo que escriban los productos que no la usan. LibraCore no
  la escribe; la escribe LibraCommerce sólo cuando viene un valor.

### Para los consumidores

- **Correr la `0010`** (`libracore-migrar upgrade`, que ya corren el arranque
  de los `-dev` y `panel_admin.py actualizar`). Sin ella, nada se rompe en un
  producto que no escriba `recibido`.
- Es Alembic puro, **fuera de `init_core_schema()`**, y tiene `downgrade` real:
  el patrón de la `0002`, no el de la `0004`–`0009`.

## [v1.98.0] — Cierre diario (Fase 1: motor)

### Agregado

- `libracore.db.cierre_diario`: el cierre diario como acto registrado, por
  sucursal — `cerrar_dia()`, `preview_cierre_dia()`, `dia_cerrado()`,
  `verificar_dia_abierto()`, `listar_cierres()`, `get_cierre()`,
  `get_cierre_turno()`, `sucursal_de_caja()`. Guarda una foto (por turno, por
  caja y de la sucursal) que no se recalcula al reimprimir.
- `arqueo_de_turno()`/`arqueo_turno_en_vivo()`: el arqueo de un turno para
  imprimir AL CERRARLO, antes de que exista cualquier cierre diario — misma
  cuenta que usa la foto (`_medios_de_turno`/`_diferencia`), no una copia.
  `sucursal_id=None` es "sin sucursal" en las cuatro funciones de destino del
  módulo por igual; `listar_cierres(todas=True)` es explícito para "todas".
- `libracore.db.turnos.create_turno()` ahora resuelve la sucursal de la caja
  del turno y rechaza la apertura si esa sucursal ya cerró el día — cubre a
  todo producto que abra turnos por esta función (VentaLibra, LibraClub; sin
  efecto en los que no usan sucursales).
- `libracore.ticket_generator.generar_ticket_cierre_turno()` y
  `generar_ticket_cierre_diario()`: tickets térmicos de 80 mm (PDF),
  reutilizando las piezas públicas del módulo.
- `libracore.caja_router.build_cierre_diario_router()`: preview, cerrar,
  listar, ver un cierre y los dos tickets PDF. El ticket de turno es
  `GET /turno/{turno_id}/ticket` (por el id real del turno, no por el de una
  foto): usa la foto si ya existe, si no lo calcula en vivo, y da 409 si el
  turno sigue abierto. `autorizar_cierre` es una dependencia inyectable para
  el endpoint de cierre — el motor no decide quién puede cerrar el día, eso
  lo resuelve cada producto.
- `libracore.db.logs`: nuevo tipo `cierre_diario` en la línea de tiempo de
  `get_actividad_log()`/`PARTES_LIBRACORE`/`PARTES_CORE`.

### Migración

- Nueva revisión de Alembic **`0009_cierre_diario`**: crea `cierres_diarios`,
  `cierres_diarios_turnos` y `cierres_diarios_medios` (no tocan
  `init_core_schema()`, que sigue congelada desde la `0001`).
- 🔴 **Los consumidores tienen que correr esta migración.** El deploy de esta
  familia NO corre migraciones solo (ver `README.md`, sección Migraciones) —
  el guard nuevo de `create_turno()` tolera que la tabla todavía no exista
  (se comporta como si nunca se hubiera cerrado nada), pero el cierre diario
  en sí no funciona hasta que la `0009` corra contra la base del producto:

  ```
  LIBRACORE_REF=v1.98.0 DATABASE_URL=postgresql://user:pass@host/db \
    ./scripts/run_migrations.sh
  ```

Ver `DECISIONS.md`, ADR-011, para el detalle de las decisiones de diseño.
