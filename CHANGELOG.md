# Changelog — LibraCore

La versión real la determina el tag de Git (`vX.Y.Z`, vía `hatch-vcs` — ver
`README.md`, sección Versionado). Este archivo no la reemplaza: registra QUÉ
cambió en cada minor, para que un consumidor sepa si tiene que correr una
migración antes de actualizar el pin. Se empieza a mantener con esta entrada;
las versiones anteriores están en la historia de Git y en la bitácora del wiki
del ecosistema.

## [Sin publicar]

Sin migraciones.

### Corregido

- 🔴 **El alta (`nuevo_cliente.py`) corre las migraciones del commit de la imagen,
  no las del checkout.** Gemelo del arreglo de `actualizar` en v1.104.0: el alta
  corría `get_config().migraciones` —las del `scripts/nuevo_cliente.py` del
  checkout del VPS, en `develop`— sobre una imagen de `main`. Y el alta no
  siempre construye: `version_para_cliente_nuevo` reusa la última imagen. Por
  eso se leen **de la imagen que se pinea**: `migraciones_de_la_imagen()` toma
  su label `org.libra.commit`, hace `git show <commit>:scripts/nuevo_cliente.py`
  (con un `fetch` si el commit no está) y lo parsea literal, sin ejecutarlo. Si
  el checkout declara otras, lo avisa y manda la de la imagen.

  **Falla cerrado:** sin label, sin commit, sin script o con un valor no literal,
  el alta levanta `ClienteError` antes de escribir el compose y hace rollback.
  Medido en el VPS: la imagen más reciente de los ocho productos trae el label
  y su commit está en el repo.

## [v1.104.0] — `actualizar` corre las migraciones del commit que construye

Sin migraciones.

### Corregido

- 🔴 **`panel_admin.py actualizar` corre las migraciones del commit que construye,
  no las del checkout.** La imagen salía de un clon limpio de `main`, pero
  `migraciones=` se tomaba de `get_config()`, o sea del `scripts/panel_admin.py`
  que se ejecuta: el del checkout del VPS, que vive en `develop`. Se corría la
  lista de `develop` sobre la imagen de `main`. Pasó el 2026-09-16 en LibraDesk:
  el deploy a producción de un hotfix corrió `libraauth-migrar` en las tres
  instancias antes de que esa adopción se promoviera.

  Ahora `build_image_tagged(..., al_materializar=)` le pasa el árbol materializado
  antes del `docker build`, y `cmd_actualizar` lee de ahí las migraciones con
  `provisioning.migraciones_declaradas()` —por `ast`, sin ejecutar el script—.
  Si el checkout declara otras, lo avisa y manda la del commit. Si no se pueden
  leer (no hay script, no es un literal, hay más de un `configure`), **no se
  construye ni se despliega nada**: caer a las del checkout es el defecto.
  `--dry-run` muestra las del ref.

  Leído contra los ocho productos, en `main` y en `develop`: las ocho
  declaraciones son literales y se leen igual que las carga el panel.

  ⚠️ **No cubre `nuevo_cliente.py`**, que sigue corriendo las del checkout sobre
  la imagen que reusa o construye para el alta.

## [v1.103.0] — `libracore-migrar` no cae al dominio en los productos de core aparte

Sin migraciones.

### Corregido

- 🔴 **`libracore-migrar` deja de caer al dominio en los productos de core
  aparte.** Sin la variable del core, `url_de_core` caía a la base del dominio
  con la idea de que esa ausencia señalaba un producto de una sola base. Eso vale
  para Contalibra, Restolibra, VentaLibra y LibraDesk, pero **no** para
  Gestiolibra, MedLibra, LibraCargo y LibraClub, que llevan el core aparte: ahí la
  caída migraba el dominio con el schema de LibraCore **sin fallar**. Ahora la
  lista de base única se **nombra** (`url_de_instancia._UNA_SOLA_BASE`, con
  `comparte_base_con_el_dominio()`) y un prefijo fuera de ella falla, igual que
  un producto nuevo que no se haya agregado. Cambia la decisión del 2026-08-25,
  con confirmación del humano.

  **No cambia a dónde migra ninguna instancia viva**: verificado en los 17
  contenedores del VPS, cada uno resuelve la misma base que antes. Lo que se
  cierra es el caso de la variable del core faltante.

- **`DATABASE_URL` como nombre histórico del dominio de LibraCargo y LibraClub.**
  La tabla se escribió para seis productos y estos dos llegaron después usando
  `DATABASE_URL` a secas; cada app lo había parcheado en su `app/config.py`. Lo
  destapó `libraauth-migrar --prefijo libracargo --base dominio`, que no hace ese
  fallback y dejó el `-dev` sin arrancar. **Sólo del lado del dominio**: en estos
  dos `DATABASE_URL` nunca puede resolver el core.

  Las dos cosas van juntas a propósito: sumar el histórico **sin** endurecer el
  paso 3 habría abierto la caída al dominio en LibraCargo y LibraClub. Lo detectó
  `test_un_producto_fuera_de_la_convencion_FALLA_en_vez_de_adivinar`, que hasta
  ese día pasaba por casualidad.

## [v1.102.0] — `list-backups` y `restore-db` dejan de mentir sobre los respaldos

Sin migraciones. Es un cambio de la CLI de provisioning, que corre **desde el venv
del host** (`.venv-scripts` de cada producto) y no desde la imagen: para que
llegue al servidor hay que actualizar ese venv, no alcanza con subir el pin.

### Corregido

- 🔴 **`list-backups` mira también la carpeta donde caen los ZIP.** Completa el
  arreglo de abajo, que se quedó corto y se vio al desplegar: los `.dump`/`.db`
  van a `<cliente>/backups/`, pero el camino de `backup_zip` —el que tienen
  prendido las instancias reales— escribe en `<cliente>/data/backups/`. Mirando
  una sola carpeta, el listado mostraba el respaldo de hacía un mes como si
  fuera el vigente, con el de esa madrugada invisible en la otra. **Peor que el
  bug original**, porque parecía actualizado. Ahora junta los tres formatos de
  las dos carpetas, ordena por fecha real y marca con `!` los que no son el
  formato vivo. Y la extensión "que sirve" ya no la decide el motor solo: con
  `backup_zip` es el ZIP, sea cual sea el motor. `restore-db` manda a la
  pantalla de Configuración del producto cuando el respaldo vivo es un ZIP.

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
