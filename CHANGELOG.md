# Changelog — LibraCore

La versión real la determina el tag de Git (`vX.Y.Z`, vía `hatch-vcs` — ver
`README.md`, sección Versionado). Este archivo no la reemplaza: registra QUÉ
cambió en cada minor, para que un consumidor sepa si tiene que correr una
migración antes de actualizar el pin. Se empieza a mantener con esta entrada;
las versiones anteriores están en la historia de Git y en la bitácora del wiki
del ecosistema.


## [Unreleased] — El motor se puede importar en Windows (propuesta: v1.186.2)

**Sin migración.** El nodo de LibraEdge corre el motor en Windows (instalador E7, ADR-010 de libraedge), y
`candados.py` y `arca_wsaa.py` importaban `fcntl` al cargarse: en la VM de prueba, `libracore-migrar` caía con
`ModuleNotFoundError` y arrastraba 35 módulos (todo ARCA, `db.schema`). `libracore.bloqueo_de_archivo` (nuevo) es la
única costura con el sistema operativo: `intentar(fd)` / `soltar(fd)` con `flock` en POSIX y `msvcrt.locking` en
Windows (probado en Windows 10: excluye entre dos aperturas y suelta). Un test vigila que ningún módulo del motor vuelva
a importar `fcntl`, `pwd`, `grp`, `termios` ni `resource`.

## [Unreleased] — La bandeja no deja descartar a mano una venta del nodo (propuesta: v1.186.1)

Completa el ADR-074. **Sin migración.** `POST /api/comprobantes-pendientes/{id}/descartar` contesta 409 si la fila es
una venta cobrada sin conexión en un nodo (`origen_tipo = "venta"`): descartarla la dejaría cobrada y sin factura. Se
factura desde la venta o sola al volver la conexión. La tanda del nodo sigue descartando las ventas anuladas (llama a
`db.descartar` directo). La pantalla ya oculta el botón (contalibra#526).
## [Unreleased] — Facturar en un nodo offline: con conexión como siempre; sin conexión, ticket y bandeja (propuesta: v1.186.0)

ADR-074. **Sin migración y sin cambio de schema.** Ítem E4 de la fase 1 de LibraEdge.

- `libracore.nodo_offline` (nuevo): `ConexionDelNodo`, `FUERA_DE_NODO`, `conexion_desde_entorno()` (`LIBRAEDGE_NODE_ID`, `LIBRAEDGE_ESTADO`), `esta_en_linea()` / `en_linea_segun_estado()` (la que `libracommerce.erp.offline` pasa a importar), `SinConexion`, `FacturaPendiente`, `NodoSinArca`, y el ticket prestado: `ta_para_el_nodo()` (central) y `numerador_con_ta()` (nodo). En `numerador_con_ta`, sólo un corte (red, tiempo agotado, 5xx) manda la venta a la bandeja; un rechazo del central (`NodoSinArca`, `RechazoDelCentral` o `rechazo_del_central = True`; el `TicketRechazado` de libraedge, traducido por el producto) es 422 `nodo_sin_arca`.
- `PuertoDeVentas` suma `conexion`, `producto` y `al_facturar` (defaults: nada cambia). En un nodo sin conexión, `facturar_venta` deja la venta en la bandeja (`origen_tipo = "venta"`) y levanta `FacturaPendiente`; con conexión y ARCA sin número, igual (nunca numeración local en un nodo); sin ARCA espejada, `NodoSinArca`.
- `facturar_pendientes_del_nodo()` y `arrancar_facturacion_al_volver(…, en_cada_vuelta=)`: la bandeja se factura sola al volver, desde la venta; `en_cada_vuelta` (al arrancar y después de cada vuelta) es la costura del reconciliador de LibraCommerce. `pendiente_de_facturar()` y `ventas_a_facturar()`.
- La factura al volver lleva **la fecha de la venta** si ARCA la admite (5 días para atrás en productos, 10 en servicios; si no, la del día) y nunca antes que el último comprobante de su serie: `fecha_de_la_factura()`.
- **Candado por venta en `facturar_venta`** (también fuera del nodo): dos intentos simultáneos emiten una factura; el segundo, `FacturacionEnCurso` → 409.
- API: `POST /api/ventas/{id}/facturar` 409 `factura_pendiente` / 422 `nodo_sin_arca`; `mp-qr` y `mp-status` 409 `sin_conexion` sin llamar a MercadoPago; `build_facturacion_del_nodo_router`: `GET /api/nodo/conexion` y `POST /api/nodo/facturar-pendientes`. La bandeja rechaza armar el formulario de una venta (409 `se_factura_desde_la_venta`) y la ingesta rechaza `origen_tipo = "venta"` (422).
- `libracore.facturas_del_nodo` (nuevo): `payload_factura()` y `aplicar_factura_del_nodo()` (idempotente, `FacturaEnConflicto`); `db.facturas.registrar_del_nodo()`; `db.comprobantes_pendientes.ORIGEN_VENTA`, `get_por_origen()`, `list_pendientes_de()`.
- El ticket de venta con `factura_pendiente` dice «NO VÁLIDO COMO FACTURA».

### Para los productos
- RestoLibra, VentaLibra y ContaLibra, en su `PuertoDeVentas`: `conexion=nodo_offline.conexion_desde_entorno(nodo=lambda: nodo_actual("<producto>"))`, `producto="<producto>"` y `al_facturar` (encolar la subida con `payload_factura`); en el nodo, `numerar_comprobante=nodo_offline.numerador_con_ta(...)` cuando exista el transporte del ticket; montar `build_facturacion_del_nodo_router` con el gate del cobro; llamar `arrancar_facturacion_al_volver(PUERTO)` al arrancar; marcar `factura_pendiente` en el ticket. Espejar `arca_config:id` (y `cajas:id` si resuelven el punto de venta por caja).
- Fuera de un nodo, subir el pin alcanza; lo visible es el 409 si dos facturan la misma venta a la vez.

## [Unreleased] — La nota de débito de una factura a cuenta corriente hereda la cuenta (propuesta: v1.185.0)

ADR-073. **Sin migración y sin cambio de schema.** Cierra el punto (1) que el ADR-066 dejó abierto (decisión del humano, 2026-10-18).

- Antes: `POST /api/facturas/{id}/nota-debito` sobre una factura a cuenta corriente armaba una nota sin `condicion_venta` y sin débito en la cuenta: se veía pendiente (ADR-066) pero sólo se cobraba por caja.
- Ahora: la nota lleva `condicion_venta = 'Cuenta Corriente'` y **asienta su débito en la cuenta del cliente** (la misma marca de `POST /api/facturas`: ingreso «Cuenta Corriente» con `factura_id` de la nota, que el libro `cc_asientos` lleva al saldo) **en la misma transacción que la nota**. Entra a `get_facturas_pendientes_cc` y se paga con un recibo (`/api/cuenta-corriente/{id}/pagar` con `facturas`), con su tope y su candado. «Cobrar» la cobra en la caja y abona la cuenta, como a la factura; después no se cobra dos veces (409 / 422).
- Sin CAE (ARCA rechaza), como la factura a crédito: la nota queda **con** su débito y fuera de lo que se paga desde la cuenta hasta que se autorice; anularla o borrarla saca la deuda.
- La nota de una factura **al contado** no cambia. `armar_nota` no cambia (la condición se agrega en el router). Las notas ya emitidas no se tocan.

### Para los productos
- Contalibra, Restolibra y LibraClub (montan `build_comprobantes_router`): subir el pin alcanza. Visible: la nota de débito de una factura a crédito sube el saldo del cliente y aparece en sus movimientos y en lo que se paga desde la cuenta.
- LibraCargo emite su nota con `notas_de_debito.emitir_nota_de_debito` y sus costuras: no cambia. Si quiere lo mismo, su `registrar` escribe la condición y el débito en su transacción.
- Un `registrar_cobro` propio (el que reemplaza la escritura de «Cobrar») ya tiene que abonar la cuenta de un comprobante con `condicion_venta = 'Cuenta Corriente'`: ahora eso incluye a estas notas.
- La pantalla (libra-ui) no necesita cambios: el detalle ya muestra la condición y «Registrar cobro» para una nota de débito, y la cuenta corriente lista lo que manda el motor.

## [Unreleased] — Tokens por instancia: `${VAR:?mensaje}` se lee del `.env` (propuesta: v1.184.1)

Corrige ADR-071. **Sin migración.** `tokens_instancia` sólo reconocía `${VAR}`, `${VAR-x}` y
`${VAR:-x}`: las instancias de LibraDesk, LibraCargo y LibraClub declaran
`LIBRA_SERVICE_TOKEN=${LIBRA_SERVICE_TOKEN:?falta en el .env}`, y `leer()` devolvía el texto literal
(mensaje incluido) como si fuera el token. `token-servicio --rotar` se frenaba solo («el valor para
`LIBRA_SERVICE_TOKEN_ANTERIOR` tiene caracteres que no se escriben») y el backoffice entraba por el
respaldo del token global. Ahora `${VAR:?…}` y `${VAR?…}` son referencias al `.env` (lectura, `donde()`
y escritura), y el texto después del `?` nunca es un valor. Pasó en las demos el 2026-10-18.

## [1.184.0] — MercadoPago: los cobros del propio titular nacen ignorados

ADR-072. **Sin migración y sin cambio de schema.**

- Antes: una transferencia, un DEBIN o un pago entre cuentas del propio titular entraba a la bandeja como `pendiente` y alguien lo tenía que ignorar a mano (en Compulibra, 91 de 141 pendientes).
- Ahora: si el CUIT del pagador (sólo dígitos) es el de **un emisor de la instancia** —el `empresa_cuit` de la config o el `cuit` de cualquier `arca_config` activa—, el cobro nace `ignorado`. Lo aplican `db_mp.create_mp_movimiento` y `db_mp.create_mp_pago` (vía `estado_al_nacer`), así que cubre el cron, la sincronización de la bandeja, el webhook y cualquier alta de un producto. Sólo cambia `pendiente`: `facturado` y el `None` de un pago no aprobado quedan como vienen. Sin CUIT de emisor cargado, nada cambia.
- `mp_sync.ingerir` no devuelve esos cobros entre los `nuevos` (no se auto-facturan aunque exista un cliente con ese CUIT y la bandera prendida) y el webhook corta antes de resolver cliente.
- La bandeja agrega `motivo_ignorado` a cada fila del historial (`"cuit_propio"` o `None`), **derivado** del CUIT, sin columna nueva.
- `db_mp.cuits_propios()`, `es_cuit_propio()`, `estado_al_nacer()` y `MOTIVO_CUIT_PROPIO` (nuevos). `db_mp.ignorar_pendientes_de_cuit_propio()` aplica la regla a los pendientes que ya existen; es idempotente y **no se corre sola**.
- `libracore.admin.services.clientes_dir()` (pública): el directorio de instancias. `_clientes_dir` queda como alias.

### Para los productos
- Subir el pin alcanza para los cobros nuevos. Los que ya están pendientes no se tocan; para limpiarlos en una instancia, `python -c "from libracore.db import mp; print(mp.ignorar_pendientes_de_cuit_propio())"` con la base de esa instancia configurada.
- La pantalla de la bandeja (libra-ui) puede mostrar `motivo_ignorado` en el historial; sin cambio, el ignorado se ve como cualquier otro.
- El backoffice (`inventario.py`) puede pasar de `services._clientes_dir()` a `services.clientes_dir()`.

## [Unreleased] — Credenciales de máquina por instancia: servicio e integración (propuesta: v1.183.0)

ADR-071. **Sin migración y sin cambio de schema.** Segundo PR de la serie de libraauth v0.52.0 (su ADR-025).

- **Alta** (`provisioning/nuevo_cliente.py`): la instancia nace con un `LIBRA_SERVICE_TOKEN` y un `LIBRA_INTEGRACION_TOKEN` **propios** (`secrets.token_hex(32)`, como `LIBRA_PANEL_TOKEN`). Antes `LIBRA_SERVICE_TOKEN` se copiaba del entorno del proceso que corría el alta (el backoffice) y por eso era uno por producto; desde la CLI sin la variable, la instancia nacía sin él. Ninguno de los dos vuelve en el resultado del alta ni pasa por `log()`: quedan en el compose.
- **`libracore.provisioning.tokens_instancia`** (nuevo): `leer(dir, var)`, `donde(dir, var)`, `escribir(dir, cambios, sello=, junto_a=)`, `nuevo_token()`, `huella()`, `entorno_para_compose()`. Lee el valor literal del `environment:` (lista o mapa) o el `${VAR}` resuelto contra el `.env` del directorio; escribe donde vivía (`.env` o compose), con respaldo `<archivo>.antes-token-<fecha>` y reemplazo atómico, los dos en modo 600.
- **`libracore.admin.services.token_de_servicio(slug)`** (nuevo): el `LIBRA_SERVICE_TOKEN` de una instancia leído de su directorio, o `None`. Nunca loguea el valor.
- **`panel_admin.py`**, comandos nuevos:
  - `token-servicio <slug> [--rotar | --cerrar]` y `token-integracion <slug> [--rotar | --cerrar]`: sin flags muestran el estado (propio/ausente, si hay `_ANTERIOR`, huella de 8 y si coincide con el del entorno del proceso) **sin imprimir valores**; `--rotar` genera uno nuevo, pasa el vigente a `<VAR>_ANTERIOR` y recrea; `--cerrar` borra `_ANTERIOR` y recrea. Rotar con una rotación abierta se rechaza.
  - `vincular-integracion --origen <dir> --destino <dir> --url-var <VAR> --token-var <VAR> --url <url>`: copia el `LIBRA_INTEGRACION_TOKEN` del destino al compose/.env del origen bajo los nombres que el origen ya lee (MedLibra: `CONTALIBRA_URL` / `CONTALIBRA_SERVICE_TOKEN`) y lo recrea. Idempotente.
- `panel_admin.compose()` pasa a `compose_en(dir, ...)` y **corre `docker compose` sin las cuatro variables de credencial del entorno del proceso**: `docker compose` resuelve `${VAR}` primero contra el entorno, y el del backoffice traía su propio `LIBRA_SERVICE_TOKEN`.
- Docstring de `comprobantes_router`: la ingesta va con `json_api_require_integracion_o_servicio` (libraauth v0.52.0).

### Para los productos
- **El backoffice tiene que dejar de mandar el `LIBRA_SERVICE_TOKEN` de su entorno** y pedir el de cada instancia con `libracore.admin.services.token_de_servicio(slug)` (`libra-backoffice/backend/libra_backoffice/inventario.py` y los clientes HTTP de Usuarios/SMTP). Sin eso, las instancias nacidas con este motor le contestan 401.
- Las instancias existentes conservan el token del producto hasta que se rote con `panel_admin.py token-servicio <slug> --rotar` (el estado sin flags avisa si todavía es el del producto). Primero el backoffice nuevo, después la rotación.
- La ingesta de comprobantes (ContaLibra) pasa al gate `json_api_require_integracion_o_servicio` y el depositante (MedLibra) recibe la credencial con `vincular-integracion`. El motor no pinea libraauth: el pin v0.52.0 va en cada producto.

## [Unreleased] — Un solo reloj para «hoy» y «ahora»: el de Argentina (propuesta: v1.182.0)

ADR-070. **Sin migración y sin cambio de schema.**

- Antes: el motor tenía **dos relojes**. `datetime.date.today()` (el del proceso: depende de la `TZ` del contenedor) fechaba el cobro, la factura, la nota, el recibo, el egreso, el movimiento de stock, los reportes y los libros IVA; `_ar_now()` (UTC-3) fechaba el turno y el cierre. Con el contenedor en UTC, entre las 21:00 y la medianoche de Argentina daban días distintos: un cobro con fecha de mañana dentro de un turno de hoy, un cierre que no veía el cobro.
- Ahora: `libracore.fechas.hoy()` (un `date`) y `libracore.fechas.ahora()` (un `datetime` con zona UTC-3) son la fuente única, y `_ar_now()` sale de ellas. Los 25 `date.today()` de producción (20 archivos) pasaron a `fechas.hoy()`. Lo único que lee el reloj del sistema es `fechas._ahora_utc()`: pisarla fija el instante en un test.
- El huso es **un desfase fijo de -3 h** (`fechas.ZONA_AR`), no `zoneinfo`: Argentina no tiene horario de verano y así el motor no depende de que la imagen traiga la base de zonas (la razón que ya documenta libracommerce). `errores`, `resguardo_enlace`, `arca_pedidos` y `arca_wscpe` dejan de definir su propio `timezone(timedelta(hours=-3))` y usan `ZONA_AR`.
- Se quedan con el reloj del proceso, a propósito, las marcas técnicas: nombre de los respaldos y de los volcados (`respaldo`, `admin/services`, `provisioning/panel_admin`), `mtime` de archivos, etiqueta de versión y `built-at` de la imagen, `desplegado_at` del panel, y la antigüedad de la copia externa (`resguardo_estado`, `provisioning/resguardo_externo`). La lista está en `tests/test_un_solo_reloj.py::MARCAS_TECNICAS`.
- Test nuevo `tests/test_un_solo_reloj.py`: con `TZ=UTC` y las 23:30 de Argentina (02:30 UTC del día siguiente), el cobro, el turno y el cierre caen el mismo día; y una guardia recorre `libracore/` y `tests/` buscando `date.today()` / `datetime.now()` sin zona. `tests/fechas.py` (libracore#412) ahora delega en `libracore.fechas.hoy()`.

### Para los productos
- **Subir el pin cambia el «hoy» de los endpoints del motor** en un contenedor sin `TZ` de Argentina: pasa a ser el de Argentina (antes, el de UTC). En las instancias que ya corren con `TZ=America/Argentina/Buenos_Aires` no cambia nada.
- Un producto con su propio `date.today()` / `datetime.now()` de negocio sigue con el reloj del proceso: pasarlo a `libracore.fechas.hoy()` / `ahora()` (LibraClub tiene `app.tiempo.hoy()`, el panel su `fechas.py`, libracommerce `hoy_argentina`: se pueden borrar en favor del motor).

## [Unreleased] — `HEAD` contesta como `GET` en la SPA y en la salud del backoffice (propuesta: v1.181.0)

ADR-069. **Sin migración y sin cambio de schema.**

- Antes: `HEAD /`, `HEAD` a cualquier ruta de la SPA y `HEAD /health` daban **405** (FastAPI no deriva el `HEAD` del `GET`): los monitores de uptime (uptime-kuma) y algunos navegadores y proxies veían la instancia caída estando sana. Ahora `libracore.spa.montar_spa` declara el catch-all con `methods=["GET", "HEAD"]` y `libracore.admin.app` hace lo mismo con `/health`: `HEAD` devuelve el mismo estado y las mismas cabeceras que `GET`, sin cuerpo. `HEAD /api/lo-que-sea` sigue dando 404 (ADR-020).
- Un endpoint de la API declarado sólo con `GET` sigue sin contestar `HEAD`: ahora cae al catch-all y da 404 (antes 405).

### Para los productos
- Un producto que llama `montar_spa` (hoy VentaLibra, por el re-export de su `app/spa.py`) lo recibe subiendo el pin. **No cambia lo propio de cada producto**: su ruta `/health` o `/salud` (`@router.get` / `@app.get` a secas) y los catch-all que armaron a mano en `app/asgi.py` o en su `app.py` siguen dando 405 a `HEAD` hasta pasarlos a `api_route(..., methods=["GET", "HEAD"])`.

## [Unreleased] — `create_remito(conn=...)`: el remito entra en la transacción de quien lo pide (propuesta: v1.180.0)

ADR-068. **Sin migración y sin cambio de schema.** Sin `conn` no cambia nada.

- `libracore.db.remitos_presupuestos.create_remito(..., conn=None)`: con `conn` escribe por esa conexión **sin commitear ni cerrar** (el commit y el rollback son de quien la abrió); sin `conn`, igual que siempre. Mismo patrón que `create_cc_pago` (`contextlib.nullcontext(conn) if conn is not None else get_connection()`). El remito es una sola fila (los ítems van en la columna JSON `items`), así que no hay más funciones que tocar.
- **Desde una sesión de SQLAlchemy** (la misma base PostgreSQL que `libracore.db.core`): `create_remito(..., conn=conexion_libracore(sesion.connection()))`, con `conexion_libracore` de `libracore.db.migraciones`. Remito y vínculo con su origen quedan en un solo commit; si la sesión se cae antes, no queda ninguno de los dos.

### Para los productos
- LibraDesk puede **borrar su copia del INSERT** (`_insertar_remito`, `_COLUMNAS_REMITO` y el test que compara las dos filas): al subir el pin pasa a `rp.create_remito(..., conn=...)` dentro de la transacción del vínculo.
- Un producto con dos bases (SQLAlchemy por un lado, `libracore.db.core` por otro) no tiene transacción común: `conn=` no se lo resuelve.

## [Unreleased] — La nota de crédito sin CAE recuerda el destino de la plata y la mueve al autorizarse (propuesta: v1.179.0)

ADR-067 (cierra el hueco que dejó el ADR-065). **Migración aditiva `0027_reintegros_pendientes`** (tabla nueva, `IF NOT EXISTS`; `init_core_schema` la crea sola en el arranque, así que una instancia que sube el pin la tiene aunque no corra `alembic`). Sin `destino` en la nota, o con la nota autorizada en el acto, no cambia nada.

- Antes: si la nota de una factura al contado cobrada salía con `destino` y ARCA no le daba CAE, la plata no se movía y **el destino se perdía**: autorizada después con `/autorizar`, había que hacer el movimiento a mano. Ahora el destino (y el medio ya resuelto) queda anotado en **`reintegros_pendientes`**, una fila por nota, y `POST /api/facturas/{id}/autorizar` mueve la plata al obtener el CAE: **una sola vez** (misma marca `nc:factura:<id>:<nota_id>` y misma `aplicar`, con la factura bloqueada), en una transacción que además borra la anotación. Borrar la nota borra la anotación (`ON DELETE CASCADE`).
- `reintegro.motivo` de la respuesta de la nota sin CAE ahora dice qué va a pasar («… cuando la autorices van a salir $ 1.000,00 de la caja (Efectivo)» / «… van a quedar $ 1.000,00 a favor del cliente …») y suma `pendiente: true`.
- `/autorizar` suma `reintegro` a su respuesta cuando movió (o intentó mover) plata, y **recibe `usuario_actual`** (el turno de la devolución es el de quien autoriza). Lo que impide mover la plata frena **antes** de pedirle el CAE a ARCA, como el alta: **409** si el producto exige turno y no hay uno abierto, **422** si había que dejar a favor y la factura ya no tiene cliente. Si el CAE salió y la plata no se pudo mover, la request no falla (la nota ya está autorizada): `reintegro.movido: false`, la anotación queda y volver a llamar a `/autorizar` sobre la nota lo reintenta.
- API nueva: `libracore.db.reintegros_pendientes` (`anotar`, `de_nota`, `borrar`), `reintegro_de_nota.dejar_pendiente`, `planear_pendiente`, `aplicar_pendiente`.

### Para los productos
- Un producto con su **propia** ruta para autorizar notas (no la del factory `build_comprobantes_router`) tiene que llamar a `planear_pendiente` antes del CAE y a `aplicar_pendiente` después; si no, la anotación queda sin consumir y la plata se mueve a mano, como antes.
- La pantalla ya muestra el `motivo` de `reintegro` cuando `movido` es `false` (libra-ui `avisoDeReintegro`): sin cambios obligatorios. Opcional: mostrar `reintegro` también en la respuesta de «Reintentar CAE».
- Bajar la migración (`downgrade 0026`) borra las anotaciones de las notas que siguen sin CAE: esas notas vuelven a resolverse a mano.

## [Unreleased] — Lo pendiente de una nota de débito se ve donde se ve lo de una factura (propuesta: v1.178.0)

ADR-066. **Sin migración y sin cambio de schema.** Cierra lo que el ADR-064 dejó abierto.

- **Una nota de débito sin cobrar aparece como pendiente**: el detalle (`GET /api/facturas/{id}`: `cobros`, `total_cobrado`, `pendiente`) deja de dar `pendiente = 0` y la pantalla puede ofrecer «Cobrar»; la columna `pendiente` del listado (`vista=nd`); la vista `sin_cobrar`; `resumen.sin_cobrar` (cantidad y monto) y el tablero (`facturas_sin_cobrar`). Lo que se ve es exactamente lo que el tope acepta cobrar. Las notas de crédito siguen afuera (restan).
- **Una sola definición**: `db.facturas.sql_es_cobrable(alias)` (y `tipos.COBRABLES` en Python) reemplaza las listas `tipos.FACTURAS` de esas cinco consultas. Un test recorre los 18 tipos de comprobante contra todas las superficies a la vez.
- **`facturado`, `comprobantes`, `facturado_mes` y `cant_facturas_mes` no cambian**: siguen siendo de las facturas. La nota suma a lo que se debe, no a lo facturado.
- **Tablero**: cada fila de `facturas_sin_cobrar` trae `tipo_label` («Nota de Débito C»), aditivo, para distinguir la nota de su factura; y `letra` sale de `tipos.LETRA` (antes una copia de tres tipos: la FCE y las notas daban `""`).
- **Cuenta corriente**: `get_facturas_pendientes_cc` incluye la nota de débito **sólo si ella misma** es `condicion_venta = 'Cuenta Corriente'`. La que emite el motor sobre una factura a crédito no lo es (no asienta su débito en la cuenta; pagarla desde ahí dejaría el saldo a favor): se ve pendiente y se cobra con «Cobrar», sin mover la cuenta.

### Para los productos
- 🔴 **Cambia cifras visibles al actualizar el pin**: las notas de débito emitidas y nunca cobradas figuran de golpe en «Sin cobrar», en el resumen y en el tablero. Mirar las instancias con datos reales antes de desplegar.
- `libra-ui`: el detalle de una nota de débito tiene que mostrar «Cobrar»/estado de cobro, y las filas de «Sin cobrar» y del tablero rotularse con el tipo del comprobante (hoy suponen factura).
- Un producto con **su propia** lista de pendientes o tablero (que no sale de `get_facturas_filtradas`, `get_resumen_core` ni `get_dashboard_data`) tiene que usar `sql_es_cobrable`/`tipos.COBRABLES` para seguir la misma regla.

## [Unreleased] — Una instancia nueva nace con 2 workers y 2 CPU (propuesta: v1.177.0)

**Sin migración.** Sólo el alta de cliente (`provisioning/nuevo_cliente.py`): el compose de una instancia nueva lleva
`WEB_CONCURRENCY=2` y `cpus: 2.0` (antes 1 proceso y 1.0). Las instancias existentes no cambian por subir el pin:
`pinear_image` sólo toca la imagen. Decisión del dueño del 2026-10-17, ya aplicada a mano en dev, demos y clientes
(`docs/varios-procesos.md`). Test: `tests/provisioning/test_alta_con_dos_workers.py`.

## [Unreleased] — La nota de crédito de una factura al contado cobrada pregunta qué hacer con la plata (propuesta: v1.176.0)

ADR-065. **Sin migración y sin cambio de schema.** Sin `destino` no cambia nada.

- `POST /api/facturas/{id}/nota-credito` acepta **`destino`** (`"devolver"` | `"a_favor"`) y **`medio_pago`**. `devolver` escribe un egreso de caja (por el turno abierto de quien emite si el producto lleva `exigir_turno`/`usuario_del_turno`; 409 sin turno, antes del CAE); `a_favor` un abono en la cuenta corriente del cliente (422 sin cliente identificado). Los dos con `referencia = nc:factura:<id>:<nota_id>`. Sin medio, el único con el que se cobró (422 si hubo varios). En una factura a cuenta corriente el destino da 422.
- Se mueve lo que la nota deja **cobrado de más** (`reintegro_de_nota.reintegrable`), nunca lo que no entró; lo que excede queda en la respuesta (`excedente`) y en el concepto. **Sólo con la nota autorizada**: sin CAE no se mueve nada y la respuesta (`reintegro.movido: false`) lo dice.
- `GET /api/facturas/{id}/nota-credito/destino?importe=`: lo que una pantalla pregunta antes de emitir.
- API nueva: `libracore.reintegro_de_nota` (`planear`, `ofrecer`, `aplicar`, `reintegrable`), `notas_de_credito.devuelto_por_notas`, `NotaNoPermitida.DESTINO` (422) y `SIN_TURNO` (409), `reintegrar_devolucion(…, concepto=, factura_id=)`, `build_nota_de_credito_router(…, exigir_turno=, usuario_del_turno=)`.

### Para los productos
- 🔴 **libracommerce (`anular_venta`)**: restar `devuelto_por_notas(conn, factura_id) + cc_acreditado_por_notas(conn, factura_id)` de lo que revierte de la caja; si no, anular una venta cuya nota ya devolvió saca la plata dos veces. Hasta entonces, no subir el pin en Contalibra, Restolibra ni VentaLibra junto con libra-ui v0.158.0.
- Un producto con caja por turno pasa `exigir_turno`/`usuario_del_turno` también a `build_nota_de_credito_router`.

## [Unreleased] — El pago a cuenta corriente toma el candado del cobro, y la nota de débito tiene tope (propuesta: v1.175.0)

ADR-063 y ADR-064. **Sin migración y sin cambio de schema.**

**ADR-063: dos pagos simultáneos no pueden cubrir la misma factura.** `POST /api/cuenta-corriente/{id}/pagar` con `facturas` leía lo pendiente y escribía el cobro en conexiones separadas y sin candado: dos pagos a la misma factura pasaban los dos el tope y la cobraban dos veces (medido: 2.000 cobrados sobre una factura de 1.000). Ahora lee, reparte, llama al gancho `validar_pago` y escribe (abono, cobro por factura y pago suelto) en **una transacción**, con las facturas pedidas bloqueadas —el mismo candado del cobro de una factura— **en orden de id ascendente** (sin interbloqueos entre dos pagos con las mismas facturas en distinto orden). Quien llega segundo ve lo que dejó el primero: factura ya cobrada, el 422 de siempre («Factura sin cobro pendiente en esta cuenta»); cobrada a medias, se le aplica sólo lo que falta y el resto queda como pago a cuenta suelto. Un pago que falla a mitad ya no deja el abono sin sus cobros.

API nueva: `cobros.bloquear_facturas(conn, ids)` y `get_facturas_pendientes_cc(cliente_id, *, conn=None)`.

**ADR-064: una nota de débito no se cobra de más.** `tipos_comprobante.COBRABLES` (facturas y notas de débito, la FCE incluida) manda `estado_de_cobro["con_tope"]`: `pendiente = total − cobrado − acreditado`, con los mismos fragmentos SQL que la factura. Cobrar de más una nota de débito pasa de 200 a **409** («… supera lo que falta cobrar de la nota de débito»). Las notas de crédito siguen sin tope.

### Para los productos
- Un `validar_pago` propio (`OpcionesCuentaCorriente`) corre ahora con las facturas bloqueadas, dentro de la transacción del pago: tiene que ser de sólo lectura.
- **Abierto**: la pantalla (detalle, columna `pendiente`, «sin cobrar», resumen, tablero) y `get_facturas_pendientes_cc` siguen sin incluir las notas de débito; sumarlas cambia cifras visibles en todos los productos (ver ADR-064).

## [Unreleased] — Una caja nueva puede nacer en la sucursal predeterminada del producto (propuesta: v1.174.0)

ADR-062. **Sin migración y sin cambio de schema.** Sin la opción nueva no cambia nada.

- **`OpcionesCajas.sucursal_por_defecto: Callable[[], int | None] | None = None`.** En `POST /api/cajas`, si el pedido no trae `sucursal_id`, la caja nace con lo que devuelva la función (sin argumentos; el producto abre su propia conexión, como `resolver_sucursal_nombre` del cierre diario). Un `sucursal_id` explícito se respeta y no la llama; si devuelve `None`, la caja queda sin sucursal como siempre. Corre después de `validar_alta`, que ve el pedido tal como llegó.
- **La edición no cambia:** `PUT /api/cajas/{id}` nunca escribió la sucursal, así que no deja una caja en NULL ni la pisa.

### Para los productos
- **ContaLibra** (sucursal única «Casa central», libracommerce v0.63.0): `OpcionesCajas(sucursal_por_defecto=...)` con una función que lee la fila `is_default` de `branches`; cierra el hueco de una caja creada por la API que quedaba en NULL hasta el próximo arranque.

## [Unreleased] — Una demo no habla con ARCA ni muestra un emisor de la base, y `libracore.demo` audita lo que parece real (propuesta: v1.173.0)

ADR-061 (renumerar). **Sin migración y sin cambio de schema.** Fuera de una demo (`DEMO_MODE` distinto de `1`/`true`) no cambia nada. Con `DEMO_MODE=1`:

- **Una demo no tiene ARCA.** Los lectores de `arca_config` (`obtener_arca_config`, `obtener_todas_arca_configs`, `obtener_arca_config_por_id`, `config_del_emisor_en`, y por ellos `config_del_emisor` y `config_por_cuit`) devuelven `None`/`[]` aunque la tabla tenga filas; `arca_credenciales.paths_en_disco` y `paths_en_disco_de_servicio` devuelven `("", "")` (ni producción ni homologación: `get_next_numero_with_arca` numera local y no sale a la red); `build_arca_router` contesta 403 a todo lo que no sea lectura.
- **El emisor del PDF es la empresa ficticia.** `emisor_del_pdf.emisor_para` no lee `arca_config`, el resolvedor ni `empresa=`: devuelve lo que `config_manager.load()` ya pisó (ADR-038).
- **`usar_empresa_demo` rechaza un CUIT que no sea ficticio** (11 dígitos, verificador válido, cuerpo `99999xxx`).

API nueva: **`libracore.demo`** — `clasificar_documento`, `es_cuit_ficticio`, `enmascarar`, `Hallazgo`, **`auditar_datos_fiscales(conn, config=None, *, terceros=(), empresa=(), ficticios=(), incluir_dudosos=False, conn_producto=None)`** (CUIT de empresa, ARCA en producción, certificados, MercadoPago que no es de prueba y CUIT de terceros en las columnas del motor y del producto; `[]` es una demo limpia), `auditar_literales(texto)` (los CUIT de forma real escritos en una semilla) y `python -m libracore.demo auditar --producto <prefijo>` (salida 0 limpia, 3 hallazgos, 4 no es una demo). `config_manager.load_sin_demo()` devuelve lo guardado sin el pisado de la demo.

### Para los productos
- **Sumar el test de la semilla**: correrla sobre una base vacía y exigir `auditar_datos_fiscales(conn) == []` más `auditar_literales(texto_de_la_semilla) == []`. Hoy darían rojo **RestoLibra** (`scripts/seed_demo.py:649`, el pagador de una transferencia) y **LibraCargo** (`scripts/seed_demo.py:172`, la empresa de la demo): un CUIT con verificador válido y fuera del rango reservado no se distingue de uno real. Se arregla con uno `99999xxx` (hay mil por prefijo).
- **LibraCargo** registra su empresa ficticia con `usar_empresa_demo` si quiere que sus PDF de demo no digan «Empresa Demo SRL».
- El reset nocturno de cada demo corre `python3 -m libracore.demo auditar --producto <p>` dentro del contenedor después de sembrar (`operaciones/scripts-demo/auditar_demo.sh` del wiki).

## [Unreleased] — Las notas de crédito restan de lo pendiente en todo el motor, y el cobro entra por el turno (propuesta: v1.172.0)

ADR-059 y ADR-060. **Sin migración y sin cambio de schema.**

**ADR-059: `pendiente = total − cobrado − acreditado`, una sola definición.** `libracore.db.facturas.sql_cobrado`, `sql_acreditado` (notas de crédito **con CAE y vigentes**, mismo emisor y ambiente), `sql_pendiente`, `sql_con_pendiente` y `sql_pendiente_positivo`. Los usan: el tope del cobro (`cobros.estado_de_cobro`), el detalle del comprobante, la vista `sin_cobrar` del listado, `get_resumen_core`, `get_dashboard_data`, `get_facturas_pendientes_cc` (y con él `cuenta_corriente_router._repartir`) y el rótulo «Pago parcial»/«Cancelación» del recibo. Nunca es negativo (cobrada y después acreditada = 0; el saldo a favor sigue siendo sólo de la cuenta corriente, que no cambia: la nota abona la cuenta una vez y el `pendiente` no la descuenta otra).

**Visible en los productos:**
- La vista «sin cobrar» y los contadores **sacan** las facturas acreditadas por completo; una acreditada por completo **no se puede cobrar** (409).
- `resumen.sin_cobrar` y el tablero (`facturas_sin_cobrar`) ahora **cuentan las cobradas a medias** (antes las sacaba cualquier cobro) y el monto es **lo que falta**, no el total. Tampoco cuentan ya como cobro un movimiento anulado.
- `get_facturas_pendientes_cc`: una nota **parcial** deja la factura con lo que se debe (antes la sacaba entera).
- Campos nuevos, aditivos: `total_acreditado` en el detalle; `total_acreditado` y `pendiente` por fila del listado; `pendiente` por fila del tablero.
- `get_cobrado_y_acreditado` (nueva) acepta `conn=`; `saldo_de_cobro(…, acreditado=)`; `emitir_recibo_factura(…, get_acreditado=)`.

**ADR-060: caja por turno sin reemplazar el cobro.** `build_comprobantes_router(exigir_turno=, usuario_del_turno=, validar_cobro=)`: con `exigir_turno=True` el cobro del motor —con su tope y su candado— entra por el turno abierto de quien cobra (`turno_id`, la caja del turno y su dueño; `fecha` y `caja_id` del pedido se ignoran); sin turno, **409** «No hay una caja abierta. Abrí el turno antes de cobrar.». `registrar_cobro_factura(…, turno_de=, exigir_turno=)` y `CobroNoPermitido.SIN_TURNO`. `cobros.transaccion_de_cobro(factura_id, monto, conn=None)`: el `with` con tope y candado para el cobro propio de un producto. `tests/pg_descartable.py` reintenta el `DROP DATABASE … FORCE` (permiso del autovacuum).

### Para los productos
- **`libra-ui` tiene que rotular la factura acreditada** (la API ya dice `total_acreditado` y `pendiente`): en `Facturas.tsx` (estado: «Acreditada» si `total_acreditado >= total`; «Cobrada» si `pendiente <= 0`; el botón de cobrar con `pendiente > 0`, no `cobrado < total`), en `FacturaDetalle.tsx` («Acreditada por nota de crédito» en vez de «Cobrado completo — $ 0,00» cuando `pendiente <= 0` y `total_acreditado > 0`), y en los tableros `comercio/Dashboard.tsx` y el de Contalibra (mostrar `f.pendiente` en vez de `f.total`). Sin eso, una factura acreditada figura «Sin cobrar» y su botón de cobrar da 409.
- **LibraClub:** borrar `_cobrar_del_turno` y pasar `exigir_turno=True, usuario_del_turno=lambda u: u["id_dominio"], validar_cobro=…` (sus medios y la regla de cuenta corriente). Su `POST /api/caja/cobros` con `factura_id` tiene que escribir dentro de `cobros.transaccion_de_cobro(factura_id, monto)` (y `registrar_movimiento` aceptar `conn=`); el cobro con QR, no.
- Un producto que inyecta `crear_movimiento`/`crear_cc_pago` en `registrar_cobro_factura` (sólo tests) tiene que aceptar `conn=`.

## [Unreleased] — Un cobro de factura no puede superar lo que falta: `cobros.exigir_cobro_posible` (propuesta: v1.171.0)

ADR-058. **Sin migración y sin cambio de schema.** `POST /api/facturas/{id}/cobrar` aceptaba un cobro mayor que lo pendiente (9.999 sobre 3.500: 200, «Cobrado completo — $ 10.999,00» y un ingreso de 9.999 en la caja; medido en el e2e de Contalibra). Ahora es **409** con el mensaje en castellano que dice cuánto falta, y no se escribe nada.

- **`libracore.cobros`**: `CobroNoPermitido` (`codigo`: `excede`, `sin_saldo`, `anulada`, `no_existe`), `centavos`, `saldo_de_cobro` (pura), `estado_de_cobro` y `exigir_cobro_posible(factura_id, monto, *, conn=None)`. `pendiente = total − cobrado` en centavos enteros; **tolerancia de medio centavo** (3.500,004 entra, 3.500,01 sobre 3.500,00 no). Lo que no puede superar lo pendiente es la **suma** de los medios del cobro.
- **Transaccional y con candado**: `registrar_cobro_factura` abre una sola transacción (o usa la de `conn=`), bloquea la fila de la factura (`FOR UPDATE` en PostgreSQL; en SQLite un `UPDATE` sin efecto) y escribe movimientos y abono de cuenta corriente ahí. Dos cobros simultáneos que juntos superan lo pendiente: entra uno. `db.caja.get_cobros_factura` acepta `conn=`.
- **`POST /api/caja`** con `factura_id` e `ingreso` (salvo la marca de cuenta corriente) tiene el mismo tope (409; factura inexistente, 404).
- El `pendiente` del detalle del comprobante sale de la misma cuenta (`saldo_de_cobro`): el valor no cambia, es una sola definición para la pantalla y para el tope.

### Para los productos
- Contalibra, Restolibra y LibraClub (montan `build_comprobantes_router`): un cobro de más pasa de 200 a **409**; el diálogo de `libra-ui` ya precarga el `pendiente` y muestra el `detail` del error. Un test de producto que cobre de más a propósito va a fallar.
- Quien pasa `registrar_cobro=` recibe el cobro ya validado, **sin candado**: para frenar la carrera, `exigir_cobro_posible(factura_id, monto, conn=conn)` en la transacción donde escribe el movimiento. Un producto con su propio cobro de factura (LibraClub, `POST /api/caja/cobros` con `factura_id`) tiene que llamarlo: el motor no lo ve.
- Quien inyecte `crear_movimiento` / `crear_cc_pago` en `registrar_cobro_factura` (sólo tests, hoy) tiene que aceptar `conn=`.
- **Abierto**: las notas de crédito no restan de lo pendiente en ningún cálculo del motor (ni en este tope), y una nota de débito no tiene tope. Ver ADR-058.


## [Unreleased] — Un cliente con cuenta corriente no se puede borrar: `delete_client` la mira (propuesta: v1.170.0)

ADR-057. **Sin migración y sin cambio de schema.** `libracore.db.clients.delete_client` bloqueaba la baja sólo por remitos y presupuestos; ahora también por movimientos de cuenta corriente: `cc_pagos`, `cc_debitos`, `recibos` y asientos del libro de clientes (`cc_asientos` con `rol='cliente'`). El `ValueError` dice cuántos hay y que el cliente se puede desactivar.

### Para los productos
- Un producto que borre clientes en sus tests o semillas con movimientos de cuenta corriente ahora recibe `ValueError`: desactivarlo en vez de borrarlo.

## [Unreleased] — Un producto suelta una FK de la baseline que declara sin ella: `fk_de_producto.soltar_fk` (propuesta: v1.169.0)

ADR-056. **Sin migración y sin cambio de schema** (`test_schema_congelado` no se mueve). `libracore/db/fk_de_producto.py`: `soltar_fk(conn, tabla, columna, *, hacia) -> list[str]` quita la FK de una columna hacia una tabla (`DROP CONSTRAINT IF EXISTS`, idempotente, sin tocar filas; devuelve los nombres soltados). En SQLite no hace nada. Es la costura que LibraDesk necesita desde que `libracore-migrar` corre antes de su arranque: la baseline crea `cc_debitos`, `cc_pagos`, `recibos`, `remitos`, `presupuestos` y `egresos` con FK hacia `clients`/`proveedores` (dos con `ON DELETE CASCADE`) que LibraDesk declara sin ellas, y una base nueva (la demo cada noche, un alta, un restore) quedaba distinta de una migrada. La baseline no cambia: los demás productos conservan esas FK.

### Para los productos

- **Sólo LibraDesk** la llama (`comercial.ensure_schema()`, una vez por columna). Los demás no declaran esas tablas por su cuenta y no tienen nada que alinear; subir el pin es la regla de la familia, no una necesidad.
- Medido, y **abierto**: en los demás productos `cc_pagos`/`cc_debitos` siguen con `ON DELETE CASCADE` hacia `clients`, y `delete_client` no bloquea la baja por cuenta corriente (sí por remitos y presupuestos). Ver las consecuencias del ADR-056.

## [Unreleased] — La app de cada instancia puede correr sin root: `provisioning.sin_root` (propuesta: v1.168.0)

ADR-055. **Sin migración.** `panel_admin actualizar` (y `rollback`) entrega `data/` a `10001:10001` antes de migrar y otra vez pegado al `up -d`, **sólo si la imagen nueva no corre como root** (`imagen_sin_root`); con una imagen root no cambia nada. El alta (`nuevo_cliente`) entrega `data/` antes de arrancar. `pinear_image` y la plantilla del alta suman `security_opt: [no-new-privileges:true]` al servicio de la app (no al sidecar de PostgreSQL). `data/backups` nace con el dueño de `data/`.

### Para los productos
- Pasar el Dockerfile a `USER` 10001 (modelo de LibraCargo) **después** de que el `.venv-scripts` del VPS tenga este motor: el `chown` lo hace el motor que corre el deploy, no el de la imagen.

## [Unreleased] — El esquema se crea de a un proceso por vez: `esperar_candado_de_transaccion` (propuesta: v1.168.0)

ADR-053. **Sin migración y sin cambio de schema** (el gate `test_schema_congelado` no se mueve). Con `uvicorn --workers 2`, `init_core_schema()` —que cada producto corre al arrancar— daba `deadlock detected` sobre una base ya armada y `duplicate key value violates unique constraint "pg_class_relname_nsp_index"` / `"pg_type_typname_nsp_index"` sobre una vacía (visto en el CI de medlibra#373 y gestiolibra: `test_dos_workers_arrancan_y_el_seed_queda_una_sola_vez`, intermitente). El deadlock: los dos procesos corren el `UPDATE caja_movimientos … WHERE caja_id IS NULL` (`ROW EXCLUSIVE` hasta el `commit`) y después `CREATE INDEX IF NOT EXISTS idx_caja_movimientos_fecha` (`SHARE`), y cada uno espera al otro.

- **`candados.esperar_candado_de_transaccion(conn, nombre, *, espera=120) -> bool`**: API nueva (por eso minor). El candado bloqueante, atado a la transacción de `conn`: `pg_try_advisory_xact_lock` reintentado; `TimeoutError` si pasan `espera` s; `False` (no hace nada) fuera de PostgreSQL.
- **`db.schema.init_core_schema`** la llama como **primera sentencia** (`CANDADO_DEL_ESQUEMA`): el segundo proceso espera al `commit` del primero y corre sobre lo que dejó. Cubre a todos los que la llaman: el `configure()` de cada producto y las revisiones de Alembic que la usan.
- **`libracore.testing.esquema_en_paralelo`** y `tests/test_esquema_en_paralelo.py`: N procesos de verdad arrancando el esquema a la vez (base armada y base vacía).

### Para los productos

- **Subir el pin** de libracore a v1.168.0 en los seis que llaman a `init_core_schema()` al arrancar (MedLibra, Gestiolibra, VentaLibra, LibraClub, Contalibra, Restolibra). No hay que tocar código: el `configure()` queda seguro con N workers.
- **No lo cubre** el DDL propio del producto: el `Base.metadata.create_all(get_engine())` de LibraGenda de MedLibra y Gestiolibra (`app/main.py`), que sobre una base vacía puede dar la clave duplicada del catálogo, y el `init_schema()` de LibraCommerce (VentaLibra, Contalibra, Restolibra). Si corren al arrancar, van dentro de `una_vez_al_arrancar(...)`.

## [Unreleased] — El Swagger de la API sólo se sirve en desarrollo: `cerrar_docs_de_la_api` (propuesta: v1.168.0)

ADR-054. **Sin migración.** `libracore/docs_api.py`: `/docs`, `/redoc`, `/openapi.json` y `/docs/oauth2-redirect` dan 404 salvo con `ENV=development` o `LIBRA_DOCS_API=1`. Medido el 2026-10-15: daban 200 sin sesión en clientes reales, demos, backoffices y panel. La demo también queda cerrada. Se decide en cada pedido y no al armar la app. `create_admin_app` suma `openapi_url=None` (los backoffices Jinja2 de Contalibra y Restolibra publicaban `/openapi.json`).

### Para los productos

- **Una línea**, justo después de crear la app y antes de montar la SPA: `from libracore.docs_api import cerrar_docs_de_la_api` y `cerrar_docs_de_la_api(app)` tras `app = FastAPI(...)`. Y un `tests/test_docs_cerrados.py` que mida contra la app de verdad (los dos casos: producción 404, desarrollo 200).
- `libra-backoffice` y `libra-panel`: igual, y sacan `docs_url=None, redoc_url=None` (la guarda ya los cierra, y en `ENV=development` los deja ver).
- Contalibra y Restolibra: además del producto, su backoffice (`admin/app.py`) queda cerrado sólo con subir el pin.
- La suite de un producto ya corre con `ENV=development`, así que los tests que leen `/openapi.json` (VentaLibra, LibraCommerce) siguen andando.
- Quien necesite ver el Swagger de una instancia real o demo: `LIBRA_DOCS_API=1` en su compose (el panel no la escribe).

## [Unreleased] — La lectura del tope de sucursales trae su texto: `mensaje_de_limite` (propuesta: v1.167.0)

ADR-052. **Sin migración.** `GET`/`PUT /api/limites/sucursales` devuelven una clave más: `{contratadas, activas, mensaje_de_limite}`, con el texto del 409 del alta («Tu plan incluye N sucursal(es). Para sumar otra, comunicate con nosotros.») cuando hay tope y `null` cuando no. Aditivo: las claves de antes no cambian y `libra-backoffice` (que lee `contratadas` y `activas` con `.get`) no se entera.

### Para los productos

- Nada que hacer: el router ya está montado. La pantalla de Sucursales del kit (`libra-ui` 0.152.0) muestra ese texto al llegar al tope en lugar de tener su copia.

## v1.166.1 — El asistente de alta no corta con un solo plan

**Sin migración.** `nuevo_cliente.py` (alta interactiva) preguntaba el plan con «basico» por defecto y salía con «Plan inválido» si no estaba en `plans.PLANES`: en un producto de plan único (VentaLibra, LibraCargo) Enter cortaba el alta. Ahora `preguntar_plan(plans)`: con un solo plan vigente lo usa sin preguntar; con varios, ofrece el primero y resuelve la respuesta con `resolver_plan` (un plan retirado entra con su reemplazo; uno desconocido, error). El alta no interactiva (`crear_cliente`) ya resolvía así.

## v1.166.0 — El listado de comprobantes filtra por envío por mail

ADR-051. **Sin migración.** `GET /api/facturas?envio=enviada|sin_enviar` (y `get_facturas_filtradas(..., envio=)`): `enviada` = salió al menos un mail; `sin_enviar` = ningún mail salió (un intento fallido cuenta como no enviado) y sólo comprobantes con CAE. Otro valor, o vacío, no filtra. Se combina con la vista, las fechas y la búsqueda, y el total y la paginación lo respetan.
## [Unreleased] — Un proceso sin el almacén de secretos dice la causa: `secretos_fuera_de_alcance()` (propuesta: v1.165.0)

**Sin migración y sin cambio de schema.** Del 2026-09-18 al 2026-10-14 el cron de MercadoPago de Contalibra (`docker exec … scripts/sync_mp_auto.py`) no facturó ningún abono: el almacén cifrado lo enchufa el arranque de la app, el script no pasa por ahí, `load()` devolvía el token vacío y el log decía «No hay Access Token» todas las noches.

- **`config_manager.MARCA_SECRETOS_EN_ALMACEN`** (`"secretos_en_almacen"` en `config.json`): la escriben `migrar_secretos_al_almacen()` —también cuando no hay nada que limpiar, así las instancias ya migradas la reciben en el próximo arranque, y no si cifrar falló— y `save()` con almacén enchufado.
- **`config_manager.secretos_fuera_de_alcance() -> bool`**: hay marca y este proceso no enchufó el almacén. `load()` no cambia: sigue sin tratarlo como error, por `panel_admin` y el provisioning.
- **`mp_sync.sincronizar_y_facturar`**: con los secretos fuera de alcance devuelve `{"error": "sin_almacen_de_secretos"}` y loguea que falta `usar_almacen_de_secretos()`, en lugar de `sin_token`.

### Para los productos

- **Todo script que corra fuera de la app y lea un secreto** (el cron de MP, el envío de correo) tiene que enchufar el almacén igual que el arranque, antes de llamar al motor. Con el pin nuevo, el cron que no lo haga lo dice en su log en vez de fallar callado.

## [Unreleased] — El rastro de los envíos por mail: `envios_email` (propuesta: v1.164.0)

ADR-050. **Migración `0026_envios_email`** (tabla nueva, idempotente, sin tocar filas): corre sola con `libracore-migrar upgrade` al arrancar el contenedor. Cada `POST …/enviar-email` de comprobantes (los dos factories de `facturas_router`) y de presupuestos (`presupuestos_router`) anota el intento, logrado o fallido, con destinatario, usuario y motivo. Lectura: `GET …/{id}/envios`, `envios_email` en el detalle y `envio_email` (resumen o `null`) en cada fila del listado. `_registrar_enviar_email` recibe `usuario_actual` (opcional). Los envíos anteriores no dejaron rastro y no se reconstruyen.

## [Unreleased] — La nota de débito por importe, del motor: `libracore.notas_de_debito` (propuesta: v1.163.0)

ADR-049. **Sin migración y sin cambio de schema.** La nota de débito vivía en `facturas_router._crear_nota`: metida en un router, atada a la tabla `facturas` y **copiando la factura entera** (`POST /api/facturas/{id}/nota-debito` no recibe importe ni motivo). Lo que se necesita en la práctica —cobrar un monto nuevo sobre una factura ya emitida: intereses, un gasto, una diferencia— no se podía pedir. LibraCargo (facturación directa, 2026-10-13) es el primer consumidor.

- **`libracore.notas_de_debito` (nuevo)**: `tipo_de_nota_de_debito` (`TIPO_ND`: la letra y la FCE se heredan), `validar_nota_de_debito(original, importe, previas=())`, `armar_nota_de_debito` y `emitir_nota_de_debito(original, *, clave, importe, numerar, registrar, pedir_cae, cargar_previas=None, hoy=None, motivo="", conn=None) -> NotaEmitida`, con las mismas costuras y el mismo orden *previas → numerar → registrar → pedir CAE* que la nota de crédito. Va **siempre por un importe** (con IVA, hasta dos decimales; el IVA sale con la alícuota de la factura y `neto + iva == importe`), **sin tope** (una factura admite varias y puede superar su total), exige que **el original tenga CAE** (`NotaNoPermitida.ORIGINAL_SIN_CAE`, 409 sugerido) y frena si una nota de débito previa quedó sin CAE. El receptor se valida como en la nota de crédito. Guía: `docs/notas-de-debito.md`.
- **`notas_de_credito`, aditivo**: `importe_valido(importe)` (extraído de `validar_nota_de_credito`, mismos rechazos), `armar_nota(..., leyenda=None, por_importe=False)` (la de débito no copia la factura aunque el importe coincida) y `una_nota_a_la_vez(clave, conn, *, clase="crédito")` (sólo cambia el texto del 409). Sin los parámetros nuevos, la nota de crédito sale exactamente igual.
- **No cambia** `POST /api/facturas/{id}/nota-debito` del router de facturas (Contalibra, Restolibra, LibraClub): sigue copiando la factura. Pasarlo a este núcleo es una decisión aparte, porque cambia lo que esos tres productos emiten.
- Tests: `tests/test_notas_de_debito.py` (tipos, guardas, armado, candado —incluido el de la base con dos conexiones—, producto de prueba con otro modelo y la nota de crédito sin cambios).

### Para los productos

- Un producto que quiera nota de débito por importe: `emitir_nota_de_debito` con su `clave` (distinta de la de la nota de crédito del mismo comprobante, así una no frena a la otra), sus tres costuras y `conn=` con la conexión de la transacción donde escribe. Traducir `codigo` a HTTP como la nota de crédito (`original_sin_cae` 409).

## [Unreleased] — Adicionales fuera de la escalera de planes: `libracore.adicionales` y su router (propuesta: v1.162.0)

ADR-048. **Sin migración y sin cambio de schema** (la `0026` queda libre). El motor de planes no podía expresar un módulo que se vende aparte y se cuenta por unidades (el offline de RestoLibra, por sucursal con offline). Ahora hay un catálogo, el dato vive en la instancia y el abono se arma por líneas. Guía: `docs/adicionales.md`.

- **`libracore.adicionales` (nuevo)**: `catalogo(plans)` (`{clave: Adicional}`; sin `plans.ADICIONALES` se sintetiza desde `ADDONS` y `PLAN_UNIDADES_ADICIONALES`: compatibilidad total), `modulos_de_adicionales(plans)` (superconjunto de `ADDONS`), `adicionales_del_plan`, `resolver(plans, plan, contratados) -> {modulos, limites, lineas_abono, incompatibles}`, `incompatibles(...)`, `calcular_abono_total(base, lineas) -> {base, lineas, extra, total, a_cotizar}` (`precio=None` = «a cotizar»; `calcular_abono` queda), `cantidad_contratada` / `fijar_cantidad` (`config.json` `adicionales: {clave: n}`; `sucursal` sigue en `limite_sucursales`), `estado_de`, `contratados_de_la_instancia`, y para hacer cumplir `verificar_alta(plans, clave, usadas_antes)` / `verificar_por_limite` (`AdicionalNoContratado`, `AdicionalExcedido`).
- **`libracore.adicionales_router.build_adicionales_router(guard_lectura, guard_escritura, medir={...}, *, plans, leer_modulos, escribir_modulo)`**: `GET /api/adicionales` y `PUT /api/adicionales/{clave}` (`{"habilitado"}` o `{"cantidad"}`, `null` = baja). Escribir sólo con el token de servicio. `/api/limites/sucursales` queda como alias.
- **`apply_plan` y `apply_plan_modules` no tocan ningún módulo de adicional** (ni `ADDONS` ni los de `ADICIONALES`): subir o bajar de plan conserva el offline y su cantidad. 🔴 Además `db.modulos.apply_plan` escribía `1`/`0`: contra la `modulos.habilitado` BOOLEAN (LibraDesk, GestioLibra, MedLibra) moría; ahora usa el tipo real de la columna.
- **`admin.services`**: `adicionales_info()`, `planes_info()[i]["adicionales"]` (con las `incluidas` de ese plan; `adicional` queda), `set_plan(slug, plan, contratados=None)` con `AdicionalIncompatibleError` (el backoffice responde 409 con la lista), `set_adicional` / `adicionales_de_instancia` (respaldo por `docker exec` para los sí/no de una instancia sin el router), `registrar_copia_de_adicionales` / `sincronizar_copia_de_adicionales` (`cliente.json` `adicionales`, copia informativa: gana la instancia) y `crear_cliente(..., adicionales=)`.
- **Alta**: `nuevo_cliente.crear_cliente(adicionales={clave: True | n})` valida contra el catálogo y el plan, siembra las cantidades en el `config.json` antes de levantar el contenedor y prende los módulos con `provisioning.aplicar_modulos_de_adicionales` tras aplicar el plan (`AltaIncompleta` si no puede).
- Tests: `tests/test_adicionales.py` (catálogo sintetizado y declarado, tabla del abono con `None`, basura en el `config.json`, router y guardas: el admin de la instancia lee y no escribe), `tests/db/test_adicionales_postgres.py` (las dos formas de `habilitado`, cambio de plan que conserva el adicional) y casos en `tests/admin/test_services.py`, `tests/provisioning/test_nuevo_cliente.py` y `test_apply_plan_modules.py`.

### Para los productos

- Sin hacer nada, el catálogo se sintetiza de lo que ya declaran. Para un adicional nuevo: `ADICIONALES = {...}` en el `plans.py` y, si cuenta unidades, `build_adicionales_router(...)` con el `medir` que sabe contarlas. Los de SQLAlchemy pasan `leer_modulos` / `escribir_modulo` de su `app.database`. Un producto con una guarda de alta llama a `verificar_alta` desde ahí.

## [Unreleased] — Un alta sin casilla de correo lo dice, y `panel_admin.py casilla` la crea o la repara; se retira `get_cc_movimientos_periodo` (propuesta: v1.161.0)

ADR-046. **Sin migración y sin cambio de schema.** El alta por la CLI del host (`scripts/nuevo_cliente.py`) llama a la misma `crear_cliente` que el backoffice, pero el host no tiene `LIBRA_MAIL_ADMIN_SSH` ni `LIBRA_MAIL_DOMINIO` (los backoffices las reciben por su compose): `configurado()` daba `False`, `smtp_ok` salía `None` y la instancia nacía sin SMTP sin que nadie se enterara.

- **`mail_cuentas` lee su configuración de un archivo del host cuando el entorno no la tiene**: `/root/secretos/correo-<container_prefix>.env` (o `LIBRA_MAIL_ENV_FILE`), `CLAVE=valor` con `LIBRA_MAIL_ADMIN_SSH`, `LIBRA_MAIL_DOMINIO` y `LIBRA_MAIL_SMTP_HOST`. **El entorno del proceso gana** (el backoffice no cambia) y sólo esas tres claves se leen. Nuevos: `archivo_del_host()`, `motivo_sin_configurar()`.
- **El alta ya no es silenciosa.** `crear_cliente` devuelve `smtp_motivo` (por qué no hay casilla; `""` si la hay) y loguea `[WARN] Sin casilla de correo: …` cuando no se intentó. La CLI cierra, después de las credenciales, con `[ADVERTENCIA] La instancia quedó sin casilla de correo: <motivo>` y el comando para crearla, y **sale con código 3** (`nuevo_cliente.SALIDA_SIN_CASILLA`; el alta salió bien, por eso no es 1).
- **`panel_admin.py casilla <slug> [--recrear]` (nuevo, idempotente)**: crea la casilla de una instancia existente, escribe las seis `LIBRAAUTH_SMTP_*` en el `environment:` de la app y la recrea (`up -d`). Con el compose ya cargado, no hace nada. Si el servidor ya tiene la casilla y el compose no, se detiene y avisa (la contraseña no se lee de vuelta; recrear borra el buzón): `--recrear` la borra y la crea de nuevo, también para rotar la contraseña. Si algo falla después de crearla, la borra. Nunca imprime la contraseña. Nuevos en `mail_cuentas`: `CasillaYaExiste`, `smtp_en_el_compose`, `con_cuenta_en_compose`; `env_para_compose(cuenta, sangria=…)`.
- **Se retira `libracore.db.cuenta_corriente.get_cc_movimientos_periodo`** (lo usaba el resumen por mail que salió en v1.155.0). Sin llamadores en `origin/develop` ni `origin/main` de los doce repos de la familia; no deja helpers huérfanos. Se sacaron sólo las líneas que lo ejercitaban en `tests/db/test_cc_desde_el_libro.py`, `test_cuenta_corriente_origenes.py`, `test_cuenta_corriente_external_ref.py` y `test_libro_de_clientes.py`.
- Tests: `tests/provisioning/test_casilla_de_correo.py` (archivo del host, edición del compose, el comando contra un servidor de correo simulado que contesta «ya existe» como el wrapper) y casos en `tests/provisioning/test_nuevo_cliente.py` (`smtp_motivo`, alta con el archivo sin variables de entorno, aviso y código de salida de la CLI). `tests/conftest.py` aísla toda la suite de `LIBRA_MAIL_*` y del archivo del host.

### Para los productos

- 🔴 **Contalibra y LibraDesk**: antes de subir el pin, borrar `get_cc_movimientos_periodo = partial(…)` (`app/db_cuenta_corriente.py`) y `movimientos_periodo = partial(…)` (`app/services/cuenta_corriente.py`). Nadie las llama, pero apuntan a una función que ya no existe y el import falla.
- Host del VPS: un `/root/secretos/correo-<producto>.env` (600) por producto con las tres variables de su backoffice; `~/.ssh/config` de `root` tiene que resolver el alias de `LIBRA_MAIL_ADMIN_SSH`. Sin eso, el alta por CLI sigue naciendo sin casilla, pero ahora lo dice.
- Un backoffice que quiera mostrar el motivo lee `smtp_motivo` de la respuesta del alta (campo nuevo; si su `response_model` filtra, hay que agregarlo).

## [Unreleased] — El concepto de la factura a cuenta corriente deja de decir «Factura» dos veces (propuesta: v1.160.0)

ADR-047. **Migración de datos `0025_concepto_sin_factura_doble`; sin cambio de schema.** La emisión a crédito escribía `Factura Factura C 0001-00000001 — Juan` (la etiqueta del tipo ya trae «Factura»). Ahora escribe `Factura C 0001-00000001 — Juan`, y la migración corrige las filas viejas.

- **`facturas_router`**: el concepto del movimiento de caja es `f"{tipo_label} {pv}-{num} — {cliente}"`. El libro de clientes lo copia al asentar, así que también sale bien.
- **`0025`**: `substr(concepto, 9)` en `caja_movimientos` y en `cc_asientos`, sólo en las filas que empiezan exactamente con `Factura Factura ` (no usa `LIKE`: en SQLite no distingue mayúsculas). No toca montos, fechas ni vínculos; idempotente; una base sin alguna de las dos tablas la saltea; escribe a stderr cuántas filas tocó por tabla. **No se baja** (restaurar el backup).
- 🔴 **Backup antes de correrla**, como toda migración de datos. El saldo de ningún cliente cambia.
- Tests: `tests/db/test_concepto_factura_sin_duplicar.py` (base en la `0024` con filas viejas y filas que no se tocan, en las dos tablas, columna por columna; idempotencia; SQLite y PostgreSQL) y, en `tests/test_facturas_router.py`, el formato nuevo en la caja y en el libro para las letras A, B y C.

### Para los productos

- Nada que cambiar en el código. Al subir el pin, correr la migración (`libracore-migrar upgrade`) con backup. Quien tenga un test que fije `Factura Factura …` (se buscó con `git grep` en los diez repos: ninguno) lo actualiza.

## [Unreleased] — Registro de errores propio: `libracore.errores` y las variables que identifican a cada instancia (propuesta: v1.159.0)

ADR-045. **Sin migración y sin cambio de schema.** Un 500 iba al stdout de uvicorn y se perdía en cada recreate del contenedor. Ahora cada error queda como una línea JSON en `DATA_DIR/errores/errores.jsonl`, con un `ref` que el usuario puede dictar, sin datos personales ni secretos. Guía completa: `docs/errores.md`.

- **`libracore.errores` (nuevo)**: `instalar_registro_de_errores(app, producto, *, usuario_id=None, endpoint_cliente=True)` (una línea por producto), `capturar(exc, *, request=None, status=None, usuario_id=None, tags=None, **contexto) -> ref` (fallas manejadas; nunca lanza), `build_errores_cliente_router(producto=None)` (`POST /api/errores-cliente`, se monta solo con la línea de arriba), y `limpiar_texto` / `limpiar_query`.
- **El handler de `Exception`** responde `500 {"detail": "Error interno", "ref": "<8 hex>"}` y registra. **No** registra `HTTPException`, `RequestValidationError` ni `ClientDisconnect`. Uvicorn sigue escribiendo el traceback en su stdout.
- **Evento**: `ts` (UTC-3), `producto`, `instancia`, `entorno`, `release`, `metodo`, `ruta` (la plantilla `/api/clientes/{cid}`, no la URL), `status`, `tipo`, `mensaje`, `traceback`, `donde` (`archivo:función`), `firma` (sha1 de tipo + ruta + `archivo:función` del frame propio más profundo, sin números de línea), `ref`, `usuario_id`, `query`, `user_agent`. Rotación 5 MB × 3. Si no puede escribir, la línea va al stdout y la respuesta sale igual.
- 🔴 **Limpieza por lista negra, en el origen**: nunca body, cookies, `Authorization` ni cabeceras salvo `user-agent`; query con `token|password|clave|secret|cuit|cert|key|dni|cbu…` en `***`; CUIT/DNI (7 a 11 dígitos), mails, PEM, `usuario:clave@`, JWT, el `DETAIL: Key (…)=(…)` de psycopg y los `[parameters: …]` de SQLAlchemy enmascarados; sin variables locales; 8 KB. Lo que no está en la lista pasa.
- **`POST /api/errores-cliente`** (público a propósito): mismo origen, ≤ 4 KB, 20 pedidos / 10 min por IP y 300 / hora global en memoria, la misma limpieza, `origen: "cliente"`.
- **`create_admin_app` lo instala** (sin el endpoint del navegador): los 8 backoffices registran sus 500 como `<producto>-admin`.
- **`provisioning.errores_env` (nuevo)**: el alta escribe `LIBRA_INSTANCIA`, `LIBRA_ENTORNO` (`demo` para el slug `demo`, `cliente` para el resto) y `LIBRA_VERSION` en el compose, y `pinear_image` las mantiene en cada `actualizar`, `rollback` y repineo, de forma idempotente y sin pisar un `LIBRA_ENTORNO` fijado a mano. **La versión es el tag pineado**: ningún producto toca su Dockerfile. Un compose sin `environment:` en lista sólo cambia la imagen.
- **`respaldo.CARPETAS_FUERA_DEL_ZIP`** (`backups`, `errores`): `data/errores/` no entra al ZIP del respaldo.
- Tests: `tests/test_errores.py` (500 + ref + línea, 4xx sin registrar, limpieza, firma estable ante un cambio de línea, disco que no escribe, rotación, topes del endpoint), `tests/provisioning/test_errores_env.py` (alta, pin idempotente, sidecar primero, compose irreconocible) y casos en `tests/admin/test_app.py` y `tests/test_respaldo.py`.

### Para los productos

- Backend: `instalar_registro_de_errores(app, "<producto>")` junto al `SecurityHeadersMiddleware`. Un test de humo: una ruta que lanza ⇒ 500 con `ref` y una línea en `DATA_DIR/errores/errores.jsonl`.
- Compose de `-dev`: `LIBRA_ENTORNO=dev`, `LIBRA_INSTANCIA=<producto>-dev`, `LIBRA_VERSION=${LIBRA_VERSION:-dev}`. Las instancias de cliente y demo las reciben del panel en su próximo `actualizar`.
- Frontend (con el `libra-ui` que traiga `ErrorBoundary` e `instalarReporteDeErrores`): el wrap en `main.tsx`.

## [Unreleased] — El motor queda listo para varios procesos por instancia (propuesta: v1.158.0)

ADR-044. **Sin migración y sin cambio de schema; todo opt-in.** Ningún producto corre con más de un worker todavía, pero el motor ya no se rompe cuando lo haga.

- **`libracore.candados` (nuevo)**: `candado_de_base(conn, nombre)` (no bloqueante, entrega `True`/`False`; PostgreSQL `pg_try_advisory_xact_lock`, SQLite `flock` en `DATA_DIR`, sin `conn` un candado del proceso) y `una_vez_al_arrancar(nombre, *, destino=None, espera=120)` (bloqueante, conexión propia, `pg_try_advisory_lock`; serializa un hook de arranque entre workers: el segundo espera y ve lo que dejó el primero). 🔴 El candado de PostgreSQL dura **hasta que termina la transacción de `conn`**.
- **`notas_de_credito.una_nota_a_la_vez(clave, conn=None)` y `emitir_nota_de_credito(..., conn=None)`**: con `conn`, además del candado en memoria, un candado de la base. Con N workers, 20 pedidos simultáneos de nota para la misma factura emiten **1**, no una por proceso. Sin `conn` no cambia nada. El router de facturas del motor lo pasa solo (conexión aparte, sólo en PostgreSQL). Los docstrings que decían «alcanza porque un solo proceso» se corrigieron.
- **`config_manager`**: `save()` y la limpieza de `migrar_secretos_al_almacen()` escriben de forma atómica (temporal + `fsync` + `os.replace`, con los permisos del archivo). Antes un lector veía el `config.json` vacío mientras otro lo escribía, y `load()` devolvía los defaults sin avisar.
- **Restore en caliente**: `respaldo_postgres.vigilar_pools` y `respaldo.restaurar` documentan que con más de un worker hay que reiniciar el contenedor tras el restore; los engines del producto llevan `pool_pre_ping=True`. El motor no crea engines de aplicación.
- **`libracore.testing.varios_procesos` y `libracore.testing.app_varios_procesos`**: levantan `uvicorn --workers 2` de verdad contra PostgreSQL; los usa `tests/test_varios_procesos.py` (marca `varios_procesos`, ~15 s) y los puede usar el test de un producto. `uvicorn>=0.30` entra al extra `dev`.
- Docs: `docs/varios-procesos.md`.

### Para los productos

- Envolver los hooks de arranque («si no existe, crear») en `una_vez_al_arrancar("<nombre>")`; pasar `conn=` a la nota de crédito con la conexión de su transacción (y no hacer `commit`/`rollback` hasta terminar); `UsadosEnBase` del captcha de `libraauth` (v0.50.0, migración `0003`); `pool_pre_ping=True` en sus engines; `--workers ${WEB_CONCURRENCY:-1}` en el compose, con `uvicorn>=0.30`.

## [Unreleased] — La auditoría de escrituras vive en el motor: `libracore.auditoria` y el CRUD de clientes que la emite (propuesta: v1.156.0)

ADR-043. **Sin migración y sin cambio de schema.** El motor no dejaba rastro de quién creaba, editaba o borraba un cliente, y LibraDesk no podía adoptar `db/clients.py` sin perder el log de `libraauth`. Ahora hay una costura y las escrituras de `clients` la usan.

- **`libracore.auditoria` (nuevo)**: `Cambio` (dataclass congelada con `tabla`, `accion`, `id`, `antes`, `despues`), `configurar_auditoria(registrador | None)` (global por proceso, idempotente), `auditando()` y `registrar(conn, tabla, accion, id, antes, despues)`. El registrador es un callable `(conn, cambio) -> None`; el de `libraauth` es `registrador_dbapi(...)` (v0.49.0), pero **el motor no lo importa** ni al revés. Sin registrador, `registrar` no hace nada y no se lee ninguna fila de más.
- 🔴 **Es transaccional**: se llama dentro del mismo `with get_connection()` que escribe, con la misma conexión. Si el registrador falla, se cae la escritura entera (el cliente, su `parties` espejo, el libro). Un log roto bloquea las altas y ediciones de clientes en lugar de perderse en silencio; contrasta con `al_cambiar` de ARCA, que es best-effort.
- **`db/clients.py`** emite un `Cambio` en todas sus escrituras: `create_client`, `update_client`, `desactivar_cliente`, `activar_cliente` (edición `{activo: [1, 0]}`), `toggle_auto_facturar`, `resolver_cliente_externo` y `delete_client` (la fila se lee antes del `DELETE`). Su comportamiento no cambia: `None` sigue siendo «no tocar» en `update_client`, y el `NULL` → `""` de `empresa`/`ciudad`/`observaciones`/`tipo_facturacion` sigue igual. `delete_client` sigue bloqueando con remitos o presupuestos y en ese caso no deja registro.
- **Nuevas `crear_cliente(datos: dict) -> int` y `actualizar_cliente(id, cambios: dict) -> bool`**: escriben exactamente las claves recibidas (`None` = NULL) y aceptan cualquier columna de la instancia —`price_list_id` de LibraDesk, `activo` en el alta—; una columna desconocida (o `id`) es `ValueError` antes de escribir. Mantienen el espejo en `parties`, el libro y el aviso; no validan CUIT duplicado (`validar_cuit_no_duplicado` ya está aparte). `actualizar_cliente` devuelve `False` si el id no existe.
- **Logs de Contalibra y Restolibra**: `logs.PARTE_AUDITORIA` suma las filas de `actividad_log` a `PARTES_CORE` y `PARTES_LIBRACORE` (y, por `libracommerce.erp.actividad.partes_de_comercio()`, a la línea de tiempo de los dos productos) sin pantalla nueva. Se saca sola si la tabla no existe. `logs_router.TIPO_META` suma `actividad` («Cambios»): la SPA le pone el ícono genérico hasta que `libra-ui` le dé uno.
- Tests: `tests/db/test_auditoria_de_clientes.py` (SQLite y PostgreSQL: sin registrador nada cambia, antes/después de cada escritura, el registrador que falla deja todo como estaba, `crear_cliente`/`actualizar_cliente`, la baja bloqueada, la línea de tiempo) y `tests/test_auditoria_toda_escritura_de_clients.py` (guarda de AST: una función del motor que escriba `clients` sin emitir el cambio no pasa el CI).

### Para los productos

- LibraDesk, VentaLibra, Contalibra y Restolibra: crear `actividad_log` donde escribe la conexión del motor si no la tienen (los que usan `libracommerce` ya la tienen), `configurar_auditoria(registrador_dbapi({"clients": "cliente"}, ...))` al arrancar y `configurar_auditoria(None)` en la limpieza de los tests. LibraDesk además pasa su CRUD de clientes por `crear_cliente`/`actualizar_cliente`.

## [Unreleased] — Se retiran los restos de datos del resumen de cuenta corriente por mail (propuesta: v1.155.0)

ADR-042. **Trae la migración `0024_retirar_cc_resumen`**, que **borra datos**: la tabla `cc_resumenes_enviados` (con su índice `idx_cc_resumenes_cliente` y su FK a `clients`) y las columnas `clients.cc_resumen_auto`, `clients.cc_resumen_frecuencia` (con su CHECK `clients_cc_resumen_frecuencia_check`) y `clients.cc_resumen_ultimo_envio`. Idempotente (`IF EXISTS` en PostgreSQL; en SQLite pregunta por las columnas), y no se baja: restaurar el backup. Es la segunda mitad del retiro de la v1.154.0, que sacó el módulo `cc_resumen` y dejó los datos a propósito; el dueño decidió el 2026-10-12 borrarlos con una migración.

🔴 **Primero el pin, después la migración.** El `update_client` de la v1.154.0 y anteriores escribe `cc_resumen_auto` y `cc_resumen_frecuencia` en cada edición de un cliente: con las columnas ya borradas, esa edición revienta. El deploy tiene que reconstruir la imagen con el pin nuevo y recién ahí correr la `0024` (`scripts/migrar_instancias.sh`); al revés, el contenedor viejo sigue sirviendo con un `update_client` que ya no tiene dónde escribir.

- **Se van de `libracore.db.clients`**: `CC_RESUMEN_FRECUENCIAS`, `toggle_cc_resumen_auto`, `get_clients_cc_resumen_auto` y los kwargs `cc_resumen_auto` y `cc_resumen_frecuencia` de `update_client`. **Ojo con los argumentos posicionales:** al sacar esos dos, `empresa`, `ciudad`, `observaciones` y `tipo_facturacion` se corren dos lugares hacia adelante. Ningún consumidor conocido llama a `update_client` con más de `client_id` posicional (relevado el 2026-10-12 en `origin/develop` de los 12 repos de producto: Contalibra y Restolibra lo llaman con kwargs); quien lo haga tiene que pasar los campos por nombre.
- **Se van de `libracore.db.cuenta_corriente`**: `registrar_resumen_enviado` y `get_resumenes_enviados`. `get_cc_movimientos_periodo` **se queda** (es una lectura de la cuenta que ya tiene otros llamadores).
- **Se van de `libracore.pdf_generator`**: `generate_pdf_resumen_cc`, `ResumenCCPDF`, `_draw_movimientos_cc` y `RESUMENES_CC_PDF_DIR`. Ningún helper quedó huérfano: los que usaban (`_draw_header_block`, `_draw_emisor_cliente`, `_draw_no_fiscal_notice`, `_wrap_text`, `_rrect`) los comparten los demás documentos.
- **Se van de `config_manager.DEFAULTS`** las seis claves `cc_resumen_*`. La config vive en el `config.json` de cada instancia, no en una tabla: la migración no la toca. Si una instancia las tiene guardadas quedan ahí, sin que nadie las lea; borrarlas a mano es cosmético.
- **`init_core_schema()`** deja de crear la tabla, el índice y las columnas, y `COLUMNAS_DE_DINERO` pierde `("cc_resumenes_enviados", "saldo")`. La `0003` (`created_at` en hora de Argentina) todavía nombra la tabla en su lista, y sigue andando sobre una base que no la tiene: `alters_para_hora_ar` sólo emite el `ALTER` de las columnas que existen. Las dos fixtures del schema congelado se regeneraron (`schema_sqlite.txt`, `schema_postgres.txt`): lo único que cambia es lo retirado.
- Los tests del resumen en PDF se sacaron de los archivos donde estaban (`test_pdf_generator`, `test_margenes_pdf`, `test_emisor_del_pdf`, `test_pdf_fecha_de_creacion`, `test_pdf_texto_fuera_de_latin1`) sin tocar los casos de los otros documentos. `tests/db/test_retirar_cc_resumen.py` prueba la `0024` en SQLite y en PostgreSQL: una base con los restos queda sin ellos y con sus clientes, repetirla no falla, y una base nueva atraviesa toda la cadena.

### Para los productos

- **LibraDesk** tiene su propio modelo de `clients` y su propia cadena de Alembic, con las tres columnas y la tabla: las saca en su propio PR, con su propia migración, y no depende de este pin.
- El resto no hace nada más que subir el pin y correr la `0024`.

## [Unreleased] — `libracore.cuit.solo_digitos`: «sólo los dígitos del CUIT» es una función del motor

**Sin migración y sin cambio de comportamiento.** `solo_digitos(valor) -> str` (`re.sub(r"\D", "", str(valor or ""))`) es la regla única: quita guiones, puntos, espacios y todo lo que no es dígito; `None` y vacío dan `""`; lo que no es texto no lanza. `arca_wsfe.cuit_del_receptor` (y con ella `problema_del_receptor` y `solicitar_cae`) se apoya en ella y devuelve lo mismo que antes. Los productos dejan de tener su helper propio: LibraCargo tenía cinco copias.

También se migran las cuatro copias que vivían en el motor (`arca_wsfecred._digitos`, `arca_wscpe._digitos`, `db.arca_config._digitos` y el `join` de `facturas_router.fce_corresponde`); un test falla si vuelve un `join(... isdigit())` en `libracore/`. Único matiz: `arca_wsfecred` y `arca_wscpe` hacían `str(cuit)` sin el `or ""`, así que un `0` numérico daba `"0"` y ahora da `""`; en ambos casos no es un CUIT de 11 dígitos y se rechaza igual.

## [Unreleased] — Se retira `libracore.cc_resumen` (código muerto)

**Sin migración y sin cambio de schema.** Se borran `libracore/cc_resumen.py` (el envío periódico del resumen de cuenta corriente por mail) y sus dos archivos de tests: ningún producto, script ni router de la familia lo importaba (relevado el 2026-10-12 en `origin/develop` de contalibra, restolibra, ventalibra, gestiolibra, medlibra, libradesk, libracargo, libraclub, libra-panel, libra-backoffice, libracommerce, libra-ui, libraauth, libraedge, libragenda y este motor). Un consumidor que lo importara dejaría de poder hacerlo; no hay ninguno conocido. Las columnas `clients.cc_resumen_*`, la tabla `cc_resumenes_enviados`, las claves `cc_resumen_*` del `config.json` y `pdf_generator.generate_pdf_resumen_cc` **se quedan**: sacarlas es otra decisión (tocan schema).

## [Unreleased] — Sucursales contratadas: el abono y el tope (propuesta: v1.153.0)

ADR-041. **Sin migración y sin cambio de schema.** El plan único de VentaLibra cobra $39.900 con UNA sucursal incluida y $19.950 por cada una más. Hasta acá era sólo un precio comercial: nada contaba sucursales. Ahora la cantidad **contratada** es un dato de la instancia, el abono se calcula y pasarse **bloquea el alta**. Sin dato cargado no hay límite: ninguna instancia existente cambia.

- **`libracore.abono.calcular_abono(precio_base, incluidas, precio_adicional, contratadas)`**: puro. Devuelve `{contratadas, incluidas, base, adicionales, precio_adicional, extra, total}`; `contratadas=None` (o por debajo de lo incluido) da `total == base`.
- **`libracore.limites`**: la cantidad contratada vive en la clave `limite_sucursales` del `config.json` de la instancia (el mismo mecanismo que el tema). `sucursales_contratadas()` (un valor ilegible se lee como «sin dato»; nunca lanza), `fijar_sucursales_contratadas(n | None)`, `verificar_alta_de_sucursal(activas_actuales)` -> `LimiteExcedido` con el mensaje «Tu plan incluye N sucursal(es). Para sumar otra, comunicate con nosotros.», y `build_limites_router(guard_lectura=, guard_escritura=, medir_activas=, prefix="/api/limites")` con `GET`/`PUT /api/limites/sucursales` (`{contratadas, activas}`; el `PUT` recibe `{"contratadas": entero 1..1000 | null}`, 422 si no, y los booleanos no pasan, ADR-013). Las **guardas las inyecta el producto**: leer es de admin **o** del token de servicio del backoffice; **escribir, sólo del token de servicio** (el admin del cliente no puede subirse su propio tope).
- **`admin.services.planes_info()`** suma `adicional`: `{"unidad", "incluidas", "precio"}` si el `plans.py` del producto declara `PLAN_UNIDADES_ADICIONALES[plan]`, y `None` si no (`getattr`: los demás productos no la tienen). Llega al backoffice por `GET /api/planes`.
- **`admin.services.plan_vigente(plan)`**: el plan de `plans.py` que rige para el que dice un `cliente.json` (un plan retirado se resuelve a su reemplazo; nunca lanza). Para buscar el plan de una instancia vieja en `planes_info()`.
- **`admin.services.crear_cliente(..., sucursales_contratadas=None)` y `provisioning.nuevo_cliente.crear_cliente(...)`**: con un número (entero 1..1000, `ClienteError` si no) la instancia **nace con `limite_sucursales` en su `config.json`**, antes de levantar el contenedor. Sin número no se escribe nada ni se pasa el argumento.

### Para los productos

- **VentaLibra**: declarar `PLAN_UNIDADES_ADICIONALES`, montar `build_limites_router` con sus guardas (lectura admin-o-servicio, escritura sólo servicio) y su medidor de sucursales activas, y llamar a `verificar_alta_de_sucursal` desde los ganchos de alta y de reactivación. Los demás productos no hacen nada.
- **Backoffice**: subir el pin; `GET /api/planes` ya trae `adicional`.

## [Unreleased] — La FCE puede cobrar en varios CBU (propuesta: v1.152.0)

ADR-040. **Trae la migración `0023_fce_varios_cbu`** (columnas `arca_config.fce_cbus` y `comprobantes_pendientes.fce_cbu`; hay que correrla antes de subir el pin). Un cliente puede cargar varias cuentas para la Factura de Crédito Electrónica y elegir en cuál cobrar cada una. `arca_config.fce_cbu` se conserva y pasa a ser **el CBU predeterminado**: quien lo lee sigue andando.

- **`arca_config.fce_cbus`** (`TEXT NOT NULL DEFAULT ''`): JSON `[{"cbu", "alias", "etiqueta"}]`. La migración rellena con `[{"cbu": fce_cbu, "alias": "", "etiqueta": ""}]` las filas que tenían `fce_cbu` y no lista. No se baja (restaurar el backup).
- **`libracore.db.arca_config`**: `cbus_fce(config)` (la lista, siempre con las tres claves; sin lista cae a `fce_cbu`; JSON inválido es `[]`), `cbu_para_fce(config, pedido)` (el predeterminado sin `pedido`; con `pedido`, el CBU **o el alias** de una fila, y siempre devuelve el CBU; fuera de la lista, `ValueError`) y `actualizar_arca_config(..., fce_cbus=None)` (`None` no la toca).
- **`PUT/GET /config/arca`**: `fce_cbus: [{cbu, alias?, etiqueta?}]`. CBU de 22 dígitos sin espacios ni guiones; alias en minúsculas de 6 a 20 caracteres (`a-z`, `0-9`, `.`, `-`); etiqueta de hasta 60. 422 con CBU o alias repetidos, o con un `fce_cbu` predeterminado que no está en la lista. Sin `fce_cbus` en el cuerpo (pantalla vieja) se comporta como antes, y el CBU que no estaba se agrega a la lista.
- **Pre factura**: `comprobantes_pendientes.fce_cbu TEXT` (nullable; **también en la `0023`**) guarda la cuenta elegida. `POST/PUT /api/pre-facturas` aceptan `fce_cbu` (CBU o alias de la config del emisor de esa pre factura; 422 si no está; se ignora si el tipo no es FCE; en el PUT, sin mandarlo no se toca y `""` vuelve a la predeterminada). Toda respuesta trae `fce_cuenta` (`{cbu, alias, etiqueta}` o `null`) y el PDF imprime «Cobro en: CBU … (alias …)» con la cuenta guardada o la predeterminada. Cambiar el emisor con una cuenta que el nuevo no tiene es 422.
- **`POST /api/facturas`**: `fce_cbu` opcional (CBU o alias de una cuenta cargada). Sin elegir sale el predeterminado; uno fuera de la lista es 422 antes de pedir el número. A ARCA va sólo el CBU (opcional 2101).

### Para los productos

- **Correr la `0023` y subir el pin.** Una pantalla que no conozca `fce_cbus` sigue funcionando. Para ofrecer la elección, leer `fce_cbus` del `GET /config/arca` y mandar `fce_cbu` en el alta de la FCE.
- Quien lea `arca_config["fce_cbu"]` para emitir debería pasar por `cbu_para_fce(cfg, None)`.
- Al facturar una pre factura FCE, pasar su `fce_cbu` (si no es `null`) al alta de la factura.

## [Unreleased] — Los códigos de acceso de las demos sobreviven al reset nocturno (propuesta: v1.150.0)

ADR-039. **Sin migración y sin cambio de schema.** Seis demos perdían todos los códigos entregados cada noche (el reset recrea la base, y `demo_codigos` vive ahí); sólo LibraCargo y LibraClub los preservaban, con un bloque bash propio.

- **`libracore.provisioning.demo_codigos`**: `guardar(sidecar, archivo, base=None)` vuelca `demo_codigos` (`pg_dump --data-only`, archivo 0600) y devuelve cuántas filas hay, o `None` si la tabla todavía no existe; `devolver(...)` lo carga con `psql -v ON_ERROR_STOP=1`, devuelve el conteo y borra el archivo, y si `psql` falla levanta `NoSePudieronDevolver` **sin borrarlo**. Las variables `$POSTGRES_USER`/`$POSTGRES_DB` se resuelven dentro del sidecar.
- **CLI `libracore-demo-codigos guardar|devolver --sidecar S --archivo F [--base B]`**, con las mismas líneas de salida que el bloque bash. Código de salida 0 ok (incluye «nada que preservar»), 1 si `devolver` falla (el archivo queda), 2 si falla Docker/`psql` o los argumentos no sirven.

### Para los productos

- **Subir el pin y cambiar `scripts/reset_demo.sh`**: `libracore-demo-codigos guardar` antes del `DROP SCHEMA` y `devolver` con la app ya arriba (la tabla la crea libraauth al arrancar), desde el `.venv-scripts`. LibraCargo y LibraClub sacan su bloque bash.

## [Unreleased] — Los rangos de fecha se miden por día (propuesta: v1.148.0)

ADR-037. **Sin migración y sin cambio de schema.** Las columnas de fecha son TEXT libre y pueden traer hora (`2026-10-09 13:00:00`, `2026-10-09T13:00`): `fecha <= '2026-10-09'` dejaba esas filas afuera y el último día del rango desaparecía del listado, del reporte o del libro IVA sin error.

- **`libracore.fechas.rango_por_dia(columna, desde, hasta)`** devuelve `(condiciones, params)` con `?`: `desde` como `>=` del día y `hasta` como `<` del día siguiente. Sólo comparaciones de texto, igual en SQLite y PostgreSQL. Un extremo que no es fecha ISO se usa tal cual (`>=` / `<=`); vacío o `None`, sin condición. `dia_iso` lee el día de un texto.
- **Lo usan todos los filtros de rango del motor**: `egresos`, `facturas`, `recibos`, `ventas`, `stock`, `tesoreria` (sin el parche `+ " 23:59:59"`, que fallaba con la `T`), `reportes`, `resumen`, `dashboard`, `caja` (`BETWEEN`), `libros_iva`, `logs` y `libro_de_terceros`. Mismos parámetros de entrada; para fechas sin hora, el mismo resultado.
- `libro_de_terceros.extracto`: el saldo anterior es el de antes del **día** de `desde`.

### Para los productos

- **Subir el pin** para que los totales de un día incluyan las filas con hora. Los números de esos días **suben**: son los correctos.
- **libracommerce** (`erp/margen.py`, `_filtro_de_ventas`) y **Restolibra** reemplazan sus filtros propios por `libracore.fechas.rango_por_dia`.

## [Unreleased] — El pedido de certificado de ARCA se genera en el servidor (propuesta: v1.147.0)

ADR-036. **Sin migración y sin cambio de schema.** Para una empresa nueva: la clave privada nace dentro del servidor y no sale nunca; de ahí sólo sale el `.csr`.

- **`arca_certificados.generar_pedido(cuit, razón social, alias)`** devuelve la clave (RSA 2048, PKCS#8, sin passphrase) y el `.csr` con el sujeto que pide ARCA (`C=AR, O, CN=<alias>, serialNumber=CUIT <n>`). `datos_del_pedido` normaliza y valida (`PedidoInvalido`). La clave no aparece en el `repr`.
- **`libracore.arca_pedidos`**: la clave **pendiente** se guarda aparte de la vigente, en `CERTS_DIR` (`pedido-{servicio}-{ambiente}-{huella}.key` en 0600 y `.json`), un pedido por (servicio, ambiente, empresa). Pedir uno no pisa el par que está facturando.
- **Rutas**, para la facturación (`{prefix}/pedido`) y para cada servicio (`{prefix}/servicios/{servicio}/pedido`), con `?ambiente=` obligatorio: `POST` (genera y devuelve el `.csr`; 409 si ya hay uno salvo `reemplazar: true`), `GET` (estado), `GET …/pedido.csr` (descarga) y `DELETE` (descarta pedido y clave).
- **Al subir el `.crt` por `…/certificado`**: si empareja con la clave pendiente, pasa a vigente (par instalado, pedido borrado); si empareja con la vigente, como siempre; si no empareja con ninguna, 422 que nombra el pedido.
- Los estados que ya existían suman `pedido` dentro del par del ambiente **sólo cuando hay un pedido pendiente**. `GET {prefix}/servicios` suma `admite_pedido: true` en cada servicio: es la marca con la que el kit sabe que este motor tiene las rutas.
- **`ACCIONES`** suma `pedido` y `descartar_pedido`; la promoción deja `certificado` y `clave` con `desde_pedido`. Ningún asiento, respuesta ni log lleva la clave.
- `docs/guia-certificado-arca.md` y `docs/facturacion-arca.md` describen el flujo nuevo.

### Para los productos

- **Subir el pin** de libracore y el de libra-ui (ADR-041 del kit): el botón «Generar pedido de certificado» aparece en la pantalla de ARCA, sin cambios en el producto. Con un LibraCore viejo el kit no muestra el botón.
- Un hook de auditoría que mapee `ACCIONES` a mano recibe dos acciones nuevas (`pedido`, `descartar_pedido`); el de LibraCargo ya registra lo desconocido como modificación.
- Si el producto pasa `razonSocial` a `ArcaCard`, el diálogo la trae prellenada (opcional).

## [Unreleased] — El catálogo geográfico suma el resto del Mercosur (propuesta: v1.146.0)

Sin migración ni cambio de schema. **Por omisión todo sigue siendo Argentina.**

- **`libracore/datos/mercosur.json`**: 6.621 lugares poblados de Brasil (5.882), Chile (305), Paraguay (151), Bolivia (148) y Uruguay (135), con sus 89 divisiones de primer nivel. Fuente: **GeoNames** (`cities1000`, más de 1.000 habitantes), licencia **CC-BY 4.0**, citada en el archivo. Se regenera con `scripts/generar_mercosur.py`.
- **`geografia.paises()`** y el parámetro **`pais`** en `provincias`, `localidades` y `buscar` (por omisión `"AR"`; `None` es todo el Mercosur, con Argentina primero). `localidad(id)` encuentra cualquier país: los ids de afuera son `{PAÍS}-{geonameid}` y nunca chocan con los códigos censales.
- Cada provincia y cada localidad trae **`pais`** (clave nueva en los diccionarios).
- Router: `GET /api/geo/paises` y `?pais=AR|BR|CL|PY|BO|UY|todos` en `/provincias` y `/localidades`.

### Para los productos

- **LibraCargo**: busca destinos en todo el Mercosur.
- **El resto**: nada, salvo un test que compare un diccionario de localidad entero, que ahora tiene `pais`.

## [Unreleased] — La localidad del catálogo por su id (propuesta: v1.145.0)

Sin migración ni cambio de schema.

- **`libracore.geografia.localidad(id)`** devuelve la localidad del catálogo por su código censal de 8 dígitos (o `None`), y **`GET /api/geo/localidades/{id}`** la sirve (404 si no está). Sirve para que un producto **vincule** su maestro editable de localidades con el catálogo: el nombre se escribe de muchas maneras, el id no.

### Para los productos

- **LibraCargo**: lo usa para vincular sus localidades al catálogo y cargar parajes como excepción.
- **El resto**: nada.

## [Unreleased] — Emitir la Carta de Porte Electrónica (propuesta: v1.144.0)

ADR-035. **Sin migración y sin cambio de schema.**

- **`libracore.arca_wscpe.emitir_cpe(cuit_representada, token, sign, SolicitudCpe(...))`**: pide el último número, autoriza el siguiente y devuelve la `CartaDePorte` con el CTG y el PDF. El representado tiene que ser el solicitante, y hay un cerrojo entre procesos por sucursal. **Si ARCA no contesta al autorizar no reintenta**: consulta por el número pedido y devuelve la carta, informa que no se emitió, o levanta `EmisionIncierta`.
- `SolicitudCpe` (con `OrigenPlanta`/`OrigenCampo`, `DestinoSolicitud` y `TransporteSolicitud`), con `problemas()` para los rangos del esquema y `SolicitudInvalida`.
- Catálogos: `tipos_grano`, `localidades`, `plantas` y `ultimo_nro_orden`. `anular_cpe`.
- `RespuestaNoSoap`, subclase de `RuntimeError`, para un 502 del balanceador.

### Para los productos

- **LibraCargo**: subir el pin para emitir desde la orden (fase 4).
- **El resto**: nada.

## [Unreleased] — Leer la Carta de Porte Electrónica de ARCA (propuesta: v1.143.0)

ADR-034. **Sin migración y sin cambio de schema.** Ningún producto cambia si no importa el módulo nuevo.

- **`libracore.arca_wscpe`** (nuevo, sólo lectura): `consultar_cpe(cuit_representada, token, sign, *, ctg=…)` (o por `tipo_cpe` + `sucursal` + `nro_orden`) devuelve una `CartaDePorte` con estado, kilos de carga y de descarga, transporte (chofer, dominios, km, tarifa, pagador del flete), origen, destino, intervinientes, el PDF en bytes y la respuesta archivable sin el PDF. `provincias()` prueba una representación; `cuits_habilitados(ticket)` dice por quién deja operar el ticket; `consultar_por_ctg(empresa, ambiente, *, cuit_representada, ctg)` hace todo con el par de `arca_credenciales_servicio`.
- Errores: `CpeNoEncontrada` (`800`), `CuitNoRelacionado` (falta la delegación de `wscpe` al alias), `ErrorWscpe` y `SinCredenciales`.
- **El CUIT representado no tiene valor por defecto** en ninguna función.

### Para los productos

- **LibraCargo**: subir el pin para empezar a traer CPE por CTG (fase 3 del plan de CTG del wiki).
- **El resto**: nada.

## [Unreleased] — `init_core_schema` no siembra un depósito en la tabla `depositos` de un producto (propuesta: v1.142.1)

ADR-033. **Sin migración y sin cambio de schema** (las fixtures de `test_schema_congelado` no se mueven).

- **`init_core_schema()` siembra «Depósito Principal» sólo si `depositos.es_default` es entera**, que es como la declara el motor. Si la tabla es de un producto (LibraDesk: `activo` y `es_default` BOOLEAN, su migración `0005_depositos`) no la toca. Antes, sobre una base **vacía**, `libracore-migrar upgrade --prefijo libradesk` moría con `DatatypeMismatch: column "es_default" is of type boolean but expression is of type integer` (alta de un cliente, reset de la demo, restaurar un backup); sobre una base con depósitos no pasaba.
- Las siembras de `cajas` y `categorias_egreso` no cambian: ningún producto declara esas tablas con otro tipo.

### Para los productos

- **LibraDesk**: subir el pin a v1.142.1 y sacar el `xfail` de la base vacía. Una base nueva de LibraDesk sigue naciendo **sin depósitos**.
- **El resto**: nada. Todos usan la `depositos` del motor y se siembra como siempre.

## [Unreleased] — Las credenciales de ARCA por servicio (propuesta: v1.142.0)

**Migración `0022_credenciales_por_servicio`**. ADR-032. Crea una tabla vacía: no toca filas ni `arca_config`. **Sin cambio para quien no pase `servicios=`**: las rutas, las respuestas y las columnas de la facturación son las de v1.141.0.

- **`libracore.arca_servicios`** (nuevo): el catálogo de servicios de ARCA que el motor sabe configurar (`wsfe`, «Facturación electrónica»; `wscpe`, «CTG y Carta de Porte»), `dummy(servicio, ambiente)` (el estado de los tres servidores de ARCA, sin autenticación; sólo `wscpe`) y `traducir_error_wsaa(texto)` (los errores de WSAA en castellano, con el texto de ARCA al final).
- **Tabla `arca_credenciales_servicio`** y **`libracore.db.arca_credenciales_servicio`**: un par certificado/clave por `(empresa, servicio, ambiente)` para los servicios que no son la facturación. `paths_de_servicio(empresa, servicio, ambiente)` devuelve `("", "")` si no hay; un ambiente desconocido no tiene credenciales y no cae a producción. **`arca_credenciales.paths_en_disco_de_servicio`** es su equivalente de `paths_en_disco` (rescata una ruta vieja por nombre dentro de `CERTS_DIR`).
- **`build_arca_router(..., servicios=("wsfe",))`**: con `servicios=("wsfe", "wscpe")` suma, por servicio que no es la facturación, `GET {prefix}/servicios/{servicio}/estado`, `POST .../certificado` y `.../clave`, `DELETE .../credenciales` y `POST .../probar`, todos con `?ambiente=` obligatorio y `?empresa=`. Mismas validaciones, mismo gate y mismo `al_cambiar` que la facturación (el `detalle` lleva `servicio`). `probar` se autentica por WSAA para ese servicio, reusa el ticket de la caché y explica `coe.notAuthorized`, `cms.cert.untrusted`, `cms.cert.expired` y la hora corrida.
- **`GET {prefix}/servicios`** existe siempre: lista los servicios del producto con `etiqueta`, `ayuda`, `configurado` y `pares` por ambiente (`tiene_certificado`, `tiene_clave`, `completo`, `vence`, `dias_para_vencer`, `vencido`, `sujeto`, `cuit_certificado`, `error_certificado`). Con la facturación sola lista un bloque, con su estado real.
- **`DatosDelCertificado.cuit`**: el CUIT del titular (`serialNumber=CUIT n` del sujeto), o `""`.
- Los helpers de validación del upload (`_certificado_valido`, `_clave_valida`, `_exigir_pareja_*`) se compartieron entre la facturación y los servicios nuevos; los mensajes de error de la facturación son los mismos.

### Para los productos

- **Contalibra, Restolibra, LibraClub y el resto**: nada. Suben el pin y no cambia nada visible.
- **LibraCargo**: `build_arca_router(servicios=("wsfe", "wscpe"), ...)` y subir `libra-ui` a v0.120.0, cuya tarjeta de ARCA pinta un bloque por servicio cuando `GET /servicios` lista más de uno. Aplicar la migración `0022` antes de arrancar. El certificado de `wscpe` se carga por ambiente desde la pantalla; el CUIT representado de cada llamada sigue siendo del producto.

## [Unreleased] — El emisor del PDF se resuelve por documento (propuesta: v1.141.0)

ADR-031. **Sin migración y sin cambio para quien no use un resolvedor ni `emisor_id`**: los bytes de cada PDF son los mismos que en v1.140.0.

- **`libracore.emisor_del_pdf`** (nuevo): `emisor_para(documento, *, resolvedor=None, empresa=None, conn=None)` arma el `empresa` de un PDF por capas: la configuración de la instancia, `nombre` y `cuit` del `arca_config` del `emisor_id` del documento (también si está inactiva; un id que no existe levanta `EmisorDesconocido`), el resolvedor del producto y `empresa=`. `registrar_resolvedor(fn)` registra el resolvedor `(documento) -> dict | None` para todos los PDF del proceso; una clave en `None` no pisa. Lo que el resolvedor levante, sube.
- **`pdf_generator`**: `generate_pdf_factura` (factura, notas, FCE), `generate_pdf_recibo`, `generate_pdf_recibo_doc`, `generate_pdf_presupuesto`, `generate_pdf` (remito), `generate_pdf_pre_factura` y `generate_pdf_resumen_cc` toman el emisor de ahí y aceptan `resolvedor=` (sólo por nombre). `generate_pdf_factura` ya no ignora `facturas.emisor_id`. `generate_pdf_pre_factura` conserva `empresa=` y suma `conn=`.
- **`pdf_generator`: el `empresa` de un PDF acepta `logo_bytes`** (el contenido de la imagen) además de `logo_path`; si vienen los dos, gana el contenido (libracore#361). Lo necesita un producto que guarda el logo en su base y no en el disco del contenedor: LibraCargo, cuyos PDF salían con el cuadrito de iniciales aunque la instancia tenía el logo cargado (reportado por el humano en Suitrans, 2026-10-06). Sin cambios para quien pasa `logo_path` o usa el de `config_manager`.
- **`build_comprobantes_pdf_router`** (nuevo, `facturas_router`): sólo `GET {prefix}/{id}/pdf` y `POST {prefix}/{id}/enviar-email`, para un producto con su propia emisión (LibraCargo). Parámetros: `usuario_actual`, `prefix`, `emisor_del_pdf`, `puede_ver(comprobante) -> bool` (lo que no pasa da 404), `smtp_config`, `donde_configurar_smtp`.
- **`build_comprobantes_router` y `build_nota_de_credito_router` aceptan `emisor_del_pdf=`**: el PDF que guardan al emitir, autorizar o hacer una nota, el borrador y el mail salen con ese emisor. El borrador lleva el `emisor_id` elegido; uno que no existe es 422. El mail lo firma el emisor del comprobante (`enviar_comprobante_por_mail(..., empresa_nombre=None)`).
- **Pre factura**: el `emisor_id` ahora lo resuelve el mismo punto. Un `emisor` inyectado ya no lo reemplaza: lo pisa clave por clave.
- **El PDF de una factura lleva el `id` en el nombre de archivo** (`factura_{id}_{pv}_{numero}.pdf`). Antes `factura_{pv}_{numero}.pdf` hacía que una nota de crédito pisara a la factura del mismo número, y dos razones sociales con la misma numeración, entre sí. Los `pdf_path` ya guardados no cambian; sin `id` (el borrador) el nombre es el de siempre.
- El PDF guardado en `pdf_path` no se regenera aunque cambie el emisor: sólo se rearma si el archivo se perdió.

### Para los productos

- **Contalibra, Restolibra, LibraClub y el resto**: nada. Suben el pin y sus PDF salen igual.
- **Un producto con varias razones sociales** (LibraCargo), para que sus PDF lleven la razón social y el logo correctos:
  1. Al arrancar: `emisor_del_pdf.registrar_resolvedor(fn)`, con `fn(documento) -> {"direccion": ..., "iva_condition": ..., "iibb": ..., "inicio_actividades": ..., "logo_bytes": ...}` armado desde su base a partir del `emisor_id` del documento (`documento.get("emisor_id")`; leé con `.get`: el documento también puede ser un recibo o un remito). `nombre` y `cuit` ya los pone el motor desde `arca_config`: sólo pasalos si la razón social del producto se llama distinto.
  2. Para ver y mandar el PDF de sus comprobantes emitidos: `app.include_router(build_comprobantes_pdf_router(usuario_actual=..., puede_ver=<filtro por ambiente o razón social>, smtp_config=..., emisor_del_pdf=<el mismo resolvedor, opcional si lo registró>))`. Sus rutas de emisión, anulación y notas de crédito siguen siendo suyas: al autorizar, que guarde `pdf_path` con `generate_pdf_factura(factura)`.
  3. La pre factura no cambia: su `emisor_del_pdf` sigue andando y ahora, además, recibe de base el nombre y el CUIT del `emisor_id`.
- Quien guarde la ruta del PDF y la reconstruya con `factura_{pv}_{numero}.pdf` en vez de leer `facturas.pdf_path` deja de encontrarla en los PDF nuevos.

## [Unreleased] — La pre factura: un comprobante por facturar que el cliente ve antes (propuesta: v1.140.0)

**Migración `0021_pre_factura`**. ADR-030. Agrega ocho columnas vacías a `comprobantes_pendientes` y un índice único parcial: no toca filas. Sin cambio de comportamiento para quien no use la pre factura.

- **`libracore.pre_facturas`**: la bandeja de comprobantes por facturar, con número interno (`PF-0001`, correlativo por `origen_producto` + `origen_instancia`, no se reusa), emisor (`emisor_id`, ADR-021), tipo de comprobante (1/6/11, 201/206/211), vencimiento de pago (FCE) y el ciclo `pendiente → enviado → aceptado → facturado`, con `descartado` como anulación. Todo con `conn=` (ADR-025).
  - `crear`, `editar`, `marcar_enviada`, `marcar_aceptada`, `anular`, `marcar_facturada`, `get`, `listar`, `contar_por_estado`, `pdf` y `enviar_por_correo`.
  - **Editar una enviada o aceptada la devuelve a `pendiente`** (y borra el rastro de envío y aceptación) si cambió algo. `facturado` y `descartado` son finales.
  - Una fila de la bandeja que no tiene número (la de Contalibra y LibraDesk) no es una pre factura: el módulo no la toca.
- **`pdf_generator.generate_pdf_pre_factura`**: el aspecto de la factura con el sello «PRE FACTURA — NO VÁLIDA COMO COMPROBANTE FISCAL» (en el cuerpo y en el pie de cada página), el número interno y **sin** punto de venta, CAE ni QR. La fecha es la del documento, así que reimprimir da los mismos bytes.
- **`libracore.pre_facturas_router.build_pre_facturas_router`** (prefijo `/api/pre-facturas`): listar, detalle, crear, editar (`PUT`, parcial), `GET .../{id}/pdf`, `POST .../{id}/enviar-email`, `.../aceptar` y `.../anular`. `origen_producto` y `origen_instancia` los fija el producto al armar el router; el gate de auth y el `usuario_actual` también. Ganchos `al_crear`, `al_editar` y `al_anular`, que reciben `(conn, pre_factura, datos)` en la misma transacción que el cambio.
- **`email_sender.enviar_documento` acepta `pdf_bytes`**: manda un PDF que está en memoria, sin pasarlo por un archivo. Lo anterior no cambia.
- **`db.comprobantes_pendientes`**: nuevos estados `enviado` y `aceptado` (`ESTADOS_ABIERTOS` y `ESTADOS_FINALES`) y origen `pre_factura`. `marcar_facturado` y `descartar` mueven cualquier estado abierto (antes sólo `pendiente`) y aceptan `conn=`; `get_comprobante`, `get_comprobantes` y `list_por_estado` también. `upsert_comprobante` sigue sin pisar nada que no sea `pendiente`.

### Para los productos

- **Contalibra, LibraDesk y el resto**: nada. Suben el pin, la migración agrega columnas vacías y su bandeja sigue igual.
- **Quien quiera la pre factura** (LibraCargo es el primero):
  1. Monta `build_pre_facturas_router(origen_producto="...", origen_instancia=..., usuario_actual=..., dependencies=[Depends(<su gate>)], smtp_resolver=..., emisor_del_pdf=..., al_crear=..., al_editar=..., al_anular=...)`.
  2. En `al_crear`, `al_editar` y `al_anular` reserva o libera sus órdenes con la `conn` que recibe: lo que escriba ahí se confirma o se deshace junto con la pre factura. Las claves propias del cuerpo (`orden_ids`...) llegan en `datos`.
  3. Para facturar, emite con su camino de siempre y en la misma transacción llama a `pre_facturas.marcar_facturada(id, factura_id, usuario, conn=conn)`.
- Un producto que numera con otra instancia por base debe pasar `origen_instancia` distinto por instancia: la numeración `PF-` es por producto e instancia.
- Un comprobante clase C (11, 211) va con `iva_rate` 0 en todos los ítems; se rechaza si no.

## [Unreleased] — El libro es la única lectura de la cuenta de clientes (propuesta: v1.139.0)

ADR-029, etapa B4. **Sin migración.** Se retira el cálculo: la cuenta de clientes se lee siempre de `cc_asientos`.

- **`get_cc_saldo`, `get_cc_movimientos`, `get_cc_movimientos_periodo` y `get_clientes_con_saldo_cc` leen siempre del libro**, con la forma de siempre (las mismas claves y el mismo orden). `get_facturas_pendientes_cc` no cambia.
- **Se retiran**: el interruptor `LIBRACORE_CC_DESDE_EL_LIBRO` (la variable queda sin efecto; se puede sacar del entorno), `libro_de_clientes.lee_del_libro` y `VARIABLE_LECTURA`, las funciones `get_cc_saldo_calculado`, `get_cc_movimientos_calculados` y `get_clientes_con_saldo_calculado` (nuevas en v1.138.0; ningún producto las importa), `libro_de_clientes.comparar`, y las consultas de las cuatro patas del cálculo.
- **`origen: OrigenVentas`** se sigue aceptando en las cuatro lecturas, con el mismo default, pero **ya no decide el saldo**: sólo le dice al libro dónde buscar el número de cada venta fiada para completar el concepto (`Venta #POS-7`). `OrigenVentas` y las constantes `VENTAS_*` no cambian: el libro las usa para asentar las ventas fiadas.
- **Una base sin `cc_asientos` tira `RuntimeError`** en las cuatro lecturas (falta la tabla del libro: migración `0020` o `init_core_schema`). `sincronizar` y `reconstruir` siguen sin hacer nada sin la tabla.
- **El tipo y el signo de cada movimiento salen de la fila del hecho**, no de la columna del asiento: un `cc_debito` de monto negativo (LibraDesk registra así la anulación de un remito) se lee como `debito` con monto negativo, y un `cc_pago` negativo como `credito` con monto negativo, igual que el cálculo. El saldo no cambia; sí la lista y los totales del período. No cambia cómo se asienta.
- Los borrados de pagos y débitos siguen siendo físicos: el libro ya guarda la reversión.

### Para los productos

- **Si cargan la cuenta con los escritores del motor** (`create_cc_pago`, `create_cc_debito`, `delete_cc_*`, `add_venta_pago`, `create_caja_movimiento`, el router de cuenta corriente): nada. Suben el pin y no cambia nada.
- **Sus tests que cargan con SQL crudo** (`INSERT` en `cc_pagos`, `cc_debitos`, `ventas_pagos` o `caja_movimientos`, o `DELETE` de esas tablas) y después leen el saldo o los movimientos: tienen que llamar `libro_de_clientes.reconstruir(origen)` después de cargar (es lo que hace un deploy), o usar los escritores. Si no, el saldo da cero y la lista viene vacía.
- **Si su base no tiene `cc_asientos`** (LibraDesk arma a mano las tablas del motor): tienen que agregarla antes de subir el pin, y correr `reconstruir(origen)` una vez. Sin la tabla, las lecturas fallan.
- Si importaban `get_cc_saldo_calculado` y hermanas, o `libro_de_clientes.comparar` o `lee_del_libro`: ya no existen.

## [Unreleased] — La cuenta de clientes se lee del libro, si la instancia lo enciende (propuesta: v1.138.0)

ADR-028, etapa B3. **Sin migración y sin cambio por defecto**: apagado, todo se lee calculado como en v1.137.

- **`LIBRACORE_CC_DESDE_EL_LIBRO`** (`1`, `true`, `si` o `sí`) pasa las cuatro lecturas al libro `cc_asientos`: `get_cc_saldo`, `get_cc_movimientos`, `get_cc_movimientos_periodo` y `get_clientes_con_saldo_cc`, con la misma forma que el cálculo (las mismas claves y el mismo orden). `libro_de_clientes.lee_del_libro()` dice cuál es el caso; una base sin `cc_asientos` lee calculado aunque esté encendida.
- **Un hecho revertido no se muestra** en los movimientos: ni el asiento original ni su contrapartida, como el cálculo, que no ve lo borrado ni lo anulado.
- **`get_facturas_pendientes_cc` no cambia**: sigue por factura.
- Nuevas en `libro_de_clientes`: `lee_del_libro`, `saldo_de`, `movimientos_de` y `clientes_con_saldo`. En `cuenta_corriente` el cálculo queda como `get_cc_saldo_calculado`, `get_cc_movimientos_calculados` y `get_clientes_con_saldo_calculado`; `comparar()` mide contra ese cálculo aunque el interruptor esté encendido.

## [Unreleased] — El libro de clientes no rompe una base sin `cc_asientos` (propuesta: v1.137.1)

- **`libro_de_clientes.sincronizar`, `reconstruir` y `saldos_del_libro` no hacen nada en una base sin `cc_asientos`.** LibraDesk arma a mano las tablas del motor que usa, sin el libro, y con v1.137.0 su pago, su débito y su venta fiada fallaban con `relation "cc_asientos" does not exist` (lo encontró el CI de libradesk#482).

## [v1.137.0] — La cuenta de clientes también como libro

**Migración `0020_origen_del_asiento`**. ADR-027. Agrega una columna vacía y su índice: no toca filas. **La lectura del saldo no cambia**: sigue calculada.

- **`cc_asientos.origen`**: el hecho que originó el asiento, `tabla:id` (`cc_pago:12`, `cc_debito:3`, `caja_mov:34`, `venta_pago:5`). `libro_de_terceros.asentar` lo acepta y `contraasentar` lo copia del original.
- **`libracore.db.libro_de_clientes`** lleva la cuenta corriente de clientes también en el libro de terceros, con rol `cliente` (opción B, etapa B1):
  - `sincronizar(origen)`: asienta el hecho, lo revierte con la fecha del original si dejó de contar, o lo vuelve a asentar si cambió el importe. Idempotente.
  - `sincronizar_cliente(id)`: asienta lo que hoy resuelve a ese cliente y no tenía cliente (una factura de su CUIT, una venta de su party). Lo ya asentado no se mueve: el cliente de una deuda se fija la primera vez.
  - `reconstruir()`: sincroniza todos los hechos. Se corre al desplegar y se puede repetir.
  - `comparar()`: los clientes cuyo saldo en el libro no es el calculado. Vacío es lo que se busca.
  - `registrar_origen_de_ventas(origen)`: dónde están las ventas del producto. `build_cuenta_corriente_router` lo registra solo.
- **Los escritores del motor asientan en su transacción**: `create_cc_pago`, `delete_cc_pago`, `create_cc_debito`, `delete_cc_debito`, `create_caja_movimiento` (ingreso con factura), `anular_caja_movimiento`, `delete_caja_movimiento`, `anular_movimientos_de_cc_pago`, `anular_factura`, `delete_factura`, `ventas.add_venta_pago`, y el alta y el cambio de CUIT de un cliente.
- Quien escribe esas tablas con SQL propio llama a `caja.al_libro_de_clientes(conn, [ids])` o a `libro_de_clientes.al_libro_venta_pago(conn, id, medio)`. Lo que no, lo encuentra `comparar` y lo arregla `reconstruir`.

## [v1.136.1] — La `0019` deja los relojes en hora de Argentina

- **La `0019_libro_de_terceros` pasa `cc_asientos.created_at` y `cierres_diarios.created_at` a hora de Argentina** con `alters_para_hora_ar`, como la `0003` con las demás. El DDL ya nacía así, pero ninguna revisión las nombraba: la guarda de Contalibra y Restolibra (que vuelve todas las columnas con reloj a UTC y corre la cadena) las encontraba en UTC. Ahora el motor tiene su propia versión de esa guarda (`test_la_cadena_deja_toda_columna_con_reloj_en_hora_de_argentina_postgres`).

## [v1.136.0] — El libro de cuenta corriente de terceros

**Migración `0019_libro_de_terceros`**. ADR-026. Crea una tabla vacía: ningún producto la usa todavía.

- **Libro de cuenta corriente de terceros (ADR-026)**, opcional y aparte de la cuenta corriente de clientes, que sigue calculada como siempre.
  - Tabla `cc_asientos`: asientos de debe y haber por `(tercero_id, rol)`. El tercero y el rol son del producto, y no hay FK al tercero. El dinero va en `NUMERIC` en PostgreSQL.
  - Funciones de `libracore.db.libro_de_terceros`: `asentar`, `corregir` (en el lugar, sólo los campos de `CORREGIBLES`), `borrar` (no uno con contrapartida), `contraasentar` (columnas invertidas, con la fecha del original por defecto y una sola vez), `saldo`, `extracto` (con saldo anterior y corrido) y `saldos`.
  - Todas aceptan `conn=` para asentar dentro de la transacción del producto (ADR-025).
  - El primero en usarlo va a ser LibraCargo (etapa 5 del diseño).

## [v1.135.0] — Emitir en la transacción del producto, y el dinero exacto en PostgreSQL

**Migración `0018_dinero_exacto`**. ADR-024 y ADR-025.

- **El dinero se guarda exacto (ADR-024).** En PostgreSQL, las 33 columnas de dinero del motor (`COLUMNAS_DE_DINERO`) pasan de `DOUBLE PRECISION` a `NUMERIC` sin escala fija: no se redondea nada. Las alícuotas y las cantidades quedan como estaban.
  - **La lectura no cambia**: el adaptador sigue devolviendo `float`, así que ningún producto toca su aritmética.
  - Sólo se convierte una columna que hoy es `double precision` o `real`; una que llegó con otro tipo se respeta.
  - En SQLite no cambia nada.
- **Emitir dentro de la transacción del producto (ADR-025).** Las funciones del comprobante aceptan `conn=` con el idioma de todo `libracore.db`: con `conn` trabajan en la transacción de quien llama y no confirman nada. Son `create_factura`, `registrar_comprobante`, `get_factura`, `update_factura_cae`, `update_factura_cae_error`, `update_factura_pdf_path`, `anular_factura`, `get_next_factura_numero`, las búsquedas de notas y del original, `arca_facturacion.get_next_numero_with_arca` y `solicitar_cae`.
  - El reintento de `create_factura` ante un número repetido, y la carrera de `registrar_comprobante`, van en un `SAVEPOINT`. En PostgreSQL un error aborta la transacción entera, y sin el savepoint el producto no podría seguir.
  - Sin `conn`, nada cambia.

## [Unreleased] — Registrar un comprobante con el número tipeado (propuesta: v1.134.0, junto con las dos de abajo)

Sin migración ni cambio de esquema. ADR-023. **Nuevo:** `db.facturas.registrar_comprobante(tipo, punto_venta, numero, ..., emisor_id=None, cae="", cae_vto="", **opcionales)`, para un comprobante **cuyo número viene de afuera**: el operador lo tipea porque se emitió en otro lado. Puede traer el CAE.
- **Nunca cambia el número.** Si ya existe para ese emisor, tipo y punto de venta, levanta `NumeroYaRegistrado`. `create_factura`, en cambio, reintenta con el siguiente.
- Va siempre como `produccion`, al libro IVA.
- Un campo opcional desconocido es `TypeError`. Un número que no es un entero positivo es `ValueError`.
- `create_factura` no cambia: comparte el `INSERT` (`_insertar`).

## [Unreleased] — La anulación con rastro de un comprobante sin CAE (propuesta: v1.134.0, junto con la entrada de abajo)

**Migración `0017_anulacion_con_rastro`**. ADR-022. **Opcional**: el `DELETE` de un comprobante sin CAE sigue como estaba, y nada se anula si nadie llama a anular.

- **Nuevo:** `facturas.anulada_en`, `anulada_por` (FK a `usuarios`) y `anulacion_motivo`.
- **Nuevo:** `db.facturas.anular_factura(factura_id, usuario_id=None, motivo="")`. Sólo anula sin CAE y sin cobros; si no puede, levanta `ComprobanteNoAnulable` con un `codigo` (`no_existe`, `con_cae`, `ya_anulado`, `con_cobros`). En la misma transacción anula el débito de cuenta corriente que había generado el comprobante.
- **Nuevo:** `POST /api/facturas/{id}/anular` (admin), con cuerpo opcional `{"motivo": "..."}`. Devuelve el detalle, o 404 o 409 según el código.
- **Cambia:** un comprobante anulado no se autoriza, no se cobra y no admite notas (409). Su número no se reusa.
- **Cambia:** los anulados quedan fuera del libro IVA y de los totales.
  - `SOLO_FISCALES` y `sql_solo_fiscales` suman `anulada_en IS NULL`.
  - Nuevo `sql_vigente(alias)`, que usan el resumen, el tablero, el reporte resumen, las facturas pendientes de cuenta corriente y la búsqueda de notas previas.
  - Los listados los siguen mostrando, con su marca.

## [Unreleased] — El emisor de cada comprobante: varias razones sociales en una instancia (propuesta: v1.134.0)

**Migración `0016_emisor_del_comprobante`** (correr `alembic upgrade head` en el deploy, como siempre). ADR-021. **Para los productos de un solo emisor no cambia nada**: no pasan emisor, sus comprobantes quedan con `emisor_id` en `NULL` y se emite con la primera configuración activa, como siempre.

- **Nuevo:** `facturas.emisor_id` (FK opcional a `arca_config.id`). `arca_config.config_del_emisor(emisor_id=None)` es el único lugar que elige con qué configuración se emite: reemplaza los diez `configs[0]` del motor. Con un id que no existe o está dado de baja levanta `EmisorDesconocido` (422 en el router), nunca cae a otra fila. `config_por_cuit(cuit)` es la guarda de un producto con varias razones sociales y levanta `ArcaAmbiguo` si hay dos filas activas del mismo CUIT.
- **Router de comprobantes:** `emisor_id` opcional en el alta (`POST /api/facturas`), en `GET /tipos`, donde da el punto de venta de ese emisor, y en `GET /fce/corresponde`. Las notas de crédito y de débito heredan el emisor de su original; `autorizar` usa el par del emisor con el que se numeró.
- **Numeración:** `get_next_factura_numero`, `create_factura` y `get_next_numero_with_arca` reciben `emisor_id`. El índice único pasa de `(tipo, punto_venta, numero)` a `(COALESCE(emisor_id, 0), ambiente, tipo, punto_venta, numero)` (`idx_facturas_numeracion`).
- **Arreglo de paso:** el índice viejo no incluía el ambiente, aunque la numeración sí lo separaba. Una factura de homologación y una real con el mismo número chocaban, y `create_factura` reintentaba con el mismo número hasta fallar.
- **Búsquedas:** `get_nc_de_factura`, `get_nd_de_factura`, `get_notas_de_factura` y `get_factura_por_tipo_pv_nro` reciben `emisor_id` y `ambiente` opcionales. Sin ellos, filtran por «sin emisor» y no filtran por ambiente, que es el comportamiento de siempre para la familia.

## [Unreleased] — La SPA es del motor; `/api` desconocido da 404 (propuesta: v1.133.0)

Sin migración. ADR-020. **Nuevo:** `libracore.spa` (`montar_spa`, `archivo_publico`, `AssetsInmutables`, `SIN_CACHE`, `PARA_SIEMPRE`, `TIPOS_PROPIOS`, `PREFIJOS_API`, `es_de_la_api`), el `app/spa.py` que seis productos tenían copiado igual. **Cambia el comportamiento** para quien lo adopte: una ruta `/api/...` que no existe contesta **404 JSON** en vez del `index.html` con 200. Adopción: reemplazar `app/spa.py` por `from libracore.spa import *` (o importar de ahí).

## [Unreleased] — ¿Corresponde FCE? El aviso antes de emitir (propuesta: v1.132.0, junto con la entrada de abajo)

Sin migración. ADR-019, decisión 2. **Nuevo:** `arca_wsfecred.corresponde_fce(cfg, cuit_receptor, total, fecha)` → `{disponible, corresponde, obligado, monto_desde}`; **nunca levanta** (sin ARCA, certificado sin `wsfecred` o ARCA caído → `disponible: false` con un motivo que dice qué hacer). Y en el router de comprobantes **`GET /api/facturas/fce/corresponde?cuit=&total=&fecha=`** (fecha opcional, hoy), que suma `fce_habilitada` (si el emisor ya cargó CBU y modalidad). Es un **aviso para el formulario, antes de emitir**: ARCA no lo frena y una factura emitida no se cambia. La emisión no cambia. Probado contra ARCA de homologación: gran empresa por 4.000.000 → corresponde (`montoDesde` 3.958.316); por 100.000 → no; receptor no obligado → no.

## [Unreleased] — `libracore.arca_wsfecred`: las consultas al registro de FCE (propuesta: v1.132.0)

Sin migración ni cambio de esquema. **Nuevo módulo, nadie lo usa todavía** (ADR-019, paso 1). Consultas de sólo lectura a WSFECRED, el registro de FCE de ARCA: `monto_obligado(cuit_empresa, cuit_receptor, fecha, token, sign, ambiente)` → `MontoObligado(obligado, monto_desde)` con `corresponde(total)`; `estado_de_fce(cuit_empresa, tipo, pto_vta, numero, …)` → `EstadoFce` (cuenta corriente, estado de la FCE y de la cuenta, importe inicial, notas, **saldo**, `rechazada`); `historial(...)`; `dummy(ambiente)`. Autentica con `arca_wsaa.autenticar(..., servicio=arca_wsfecred.SERVICIO)`: **el certificado tiene que tener `wsfecred` autorizado**. Errores: `ErrorWsfecred` y `FceNoRegistrada` (`1102`/`1105`). Tests contra respuestas **reales** de homologación, anonimizadas (`tests/fixtures_wsfecred/`), y probado contra ARCA de homologación el 2026-10-05.

## [Unreleased] — Diseño de la FCE completa (WSFECRED), sin código

Sólo documentación: `docs/fce.md` y ADR-019 (propuesta). Lo medido en ARCA de homologación sobre el registro de FCE y el diseño de `libracore.arca_wsfecred`. Nada cambia para los consumidores.

## [Unreleased] — Una FCE no admite la nota de crédito total (propuesta: v1.131.0)

Sin migración ni cambio de esquema. **Arreglo:** `validar_nota_de_credito` sin `importe` (la nota **total**) sobre una **FCE** (201, 206, 211) ahora levanta `NotaNoPermitida.IMPORTE` (422 en `POST /api/facturas/{id}/nota-credito`), antes de pedirle el número a ARCA. Hasta v1.130.0 sólo se frenaba la nota **con** importe; la total pasaba la validación y la rechazaba ARCA (`10184`: supera el saldo; anularla por completo exige que el comprador la rechace, `10154`). La nota de una FCE va siempre por un importe menor que el saldo.

## [Unreleased] — La nota de crédito PARCIAL y el tope acumulado (propuesta: v1.130.0)

Sin migración ni cambio de esquema. Detalle en ADR-018. **Una factura se puede acreditar en varias notas, y la suma nunca supera su total.**

**Nuevo:** `emitir_nota_de_credito(..., importe=...)` y `POST /api/facturas/{id}/nota-credito` con cuerpo opcional `{"importe": 4000.00}`: el monto a acreditar, con IVA, de hasta dos decimales. Sin `importe` la nota sigue siendo **total** (sin cambios). El neto sale del importe con la alícuota de la factura y el IVA es la **resta** (`neto + iva == importe`, sin un centavo perdido); un comprobante C no discrimina IVA. La nota parcial lleva **un solo ítem** (qué acredita), no copia los del original. `validar_nota_de_credito(original, previas, importe=None)` valida el **tope acumulado** (nuevos códigos `NotaNoPermitida.IMPORTE`, 422, y `SUPERA_SALDO`, 409); `acreditado()`, `saldo_acreditable()` y `repartir_importe()` quedan como API para los productos. Una nota previa **sin CAE** frena a cualquier nota nueva, como antes. Una **FCE** sólo admite una nota **por menos que su saldo** (medido: `10184`).

**Cuenta corriente:** el abono de una factura a crédito es ahora **el importe de cada nota** (antes siempre el de la factura) y la marca es **por nota**: `nc:factura:<id>:<nota_id>` (la forma `nc:factura:<id>` de v1.128.0 sigue valiendo). Nuevo `cc_acreditado_por_notas(conn, factura_id)`; `cc_acreditada_por_nota` queda como «¿abonó algo?».

**Compatible hacia atrás:** un producto que no informa `total` en sus notas previas (los de la fase 1) las cuenta como la factura entera, o sea que sigue bloqueando como antes. **Medido contra ARCA de homologación (2026-10-04):** autoriza la nota parcial con el IVA hasta a 15 centavos del exacto, sin observaciones.

## [Unreleased] — Un router que sólo ofrece la nota de crédito (propuesta: v1.129.0)

Sin migración ni cambio de comportamiento. Detalle en ADR-017.

**Nuevo:** `facturas_router.build_nota_de_credito_router(usuario_actual, solo_admin, prefix="/api/facturas")`: **una sola ruta**, `POST {prefix}/{factura_id}/nota-credito`, para los productos que facturan desde otra pantalla (la venta) y no tienen pantallas de facturas, como VentaLibra. Es **el mismo código** que usa `build_comprobantes_router` (se extrajo a `_registrar_nota_de_credito`): mismas guardas, mismos códigos HTTP, mismo abono marcado a la cuenta corriente. Contalibra, Restolibra y LibraClub no cambian.

## [Unreleased] — La nota de crédito deja su marca en la cuenta corriente (propuesta: v1.128.0)

Sin migración ni cambio de esquema. Detalle en ADR-016.

**Nuevo:** `notas_de_credito.referencia_cc_de_nota(factura_id)` y `notas_de_credito.cc_acreditada_por_nota(conn, factura_id)`. El abono que la nota de una factura **a cuenta corriente** deja al cliente (`POST /api/facturas/{id}/nota-credito`) ahora lleva en `cc_pagos.referencia` la marca `nc:factura:<id>` (antes quedaba vacía). Es lo que le permite a `anular_venta` de libracommerce **no acreditar la misma deuda por segunda vez** cuando la venta tiene una factura con nota. No cambia el importe ni el concepto del abono.

## [Unreleased] — El webhook de MercadoPago no da 500 ante un cuerpo raro y una guardia lista los cuerpos sin tipar (propuesta: v1.127.0)

Sin migración ni cambio de esquema. Detalle en ADR-015.

**`mp_webhook` (corrección):** un cuerpo que no era lo esperado daba **500** (excepción sin atrapar) y MercadoPago reintenta ante un 500. Medido con un test de HTTP al router real: un JSON que no es un
objeto (`[]`, `[1]`, `1`, `true`, `null`, `"x"`) y un `data` que no es un dict (`null`, `[]`, `"x"`, `5`). Ahora un JSON que no es un objeto da **400** `invalid json` (igual que un JSON roto), y un `data`
que no es un dict o sin un `id` utilizable da **400** `no payment id` (igual que un id vacío). Un `id` que es un booleano, un dict o una lista tampoco es un id: antes seguía de largo (`{"id": true}` llegaba a
la API de MercadoPago como `"True"`), ahora da 400 `no payment id` y no llega a la firma. **No cambia** lo que MercadoPago ya recibe: un `type` que no es `"payment"` (también un no-string) sigue dando 200
`ignored`; un `id` numérico o de texto sigue siendo válido (`str(id)`); la firma, la idempotencia y el 200 «not configured» son los de siempre.

**Qué se agrega:** `libracore.testing.cuerpos_sin_tipar(app, *, ignorar=frozenset())`, la guardia hermana de `campos_numericos_que_aceptan_booleano`: lista `(método y ruta, campo, tipo)` de los campos de
entrada de tipo `dict`/`Mapping`/`list[dict]`/`Any`/`object`/`JsonValue` o de un modelo con `extra="allow"`, y de los endpoints `POST`/`PUT`/`PATCH`/`DELETE` que declaran `Request` y no tienen cuerpo tipado
(`request-sin-cuerpo-tipado`). Es **informativa**, para revisión humana (puede dar falsos positivos): no se afirma `== []`, se fija el conjunto conocido con un comentario por entrada. Sobre las factories de
libracore da 5 entradas, todas conocidas (`FacturaPayload` con `extra="allow"` en dos rutas, `CobroPayload.pagos`, el webhook y el enlace de resguardo). El recorrido de rutas que comparte con la guardia de
booleanos se extrajo a `libracore/testing/_recorrido.py` (interno); el comportamiento de esa guardia no cambia.

**Lo que puede romper al subir el pin:** nada que MercadoPago envíe hoy. Un test de un producto que le mande al webhook un JSON que no es un objeto y espere 500 (ninguno debería).

## [Unreleased] — Un booleano ya no es un número en los cuerpos de los routers (propuesta: v1.125.0)

Sin migración ni cambio de esquema. **Cambia el comportamiento de 60 campos de entrada** de los routers del motor: `true` y `false` en un campo numérico daban 200 (pydantic los convertía en `1` y `0`:
`{"monto": true}` era un pago de 1 peso y `{"caja_id": true}` la caja 1); ahora dan **422** con «<campo> tiene que ser un número, no un booleano» y no escriben nada. Un número y un texto numérico
(`"2"`) siguen pasando igual. Detalle y relevamiento en ADR-013.

**Qué se agrega:** `libracore.validacion.sin_booleanos(*campos)` (el helper canónico, el mismo código y mensaje que `libracommerce.web._validacion`) y
`libracore.testing.campos_numericos_que_aceptan_booleano(app, *, ignorar=...)` (la guardia: instancia el modelo real con `True`/`False` en cada hoja numérica y devuelve los campos que lo aceptan; cada producto la
corre sobre su `create_app()` y afirma `== []`).

**Qué se arregla:** `caja_router` (movimientos, cajas, turnos, cierre diario), `cuenta_corriente_router` (pagar), `egresos_router` (alta y pago), `tesoreria_router` (cuentas, movimiento, transferencia),
`arca_router` (`punto_venta`), `facturas_router` (`tipo`, `concepto` y `punto_venta`, que viajan al comprobante que se pide a ARCA, más `client_id`, `tax_rate`, `items[]` y el `caja_id` de cobrar), `comprobantes_router` (ingesta, prefill y marcar), `remitos_router`, `presupuestos_router` y `mp_bandeja_router` (`dias` y la siembra de la demo).

**Sin pendientes en los routers del motor:** la guardia sobre todas las factories da `[]`. Además, el barrido de cuerpos sin tipar (`dict`, `list[dict]`, `Any`, `request.json()`) encontró uno, con dinero:
`CobroPayload.pagos` de `POST /api/facturas/{id}/cobrar`, donde `{"pagos": [{"monto": true}]}` registraba un cobro de 1 peso. Ahora da 422 («pagos[].monto tiene que ser un número, no un booleano»; también
`medio_id`), sin cambiar el contrato (mismas claves, filas vacías y texto numérico). `libracore.validacion.rechazar_booleanos(valor, campos, donde)` es el helper para dicts sin tipar, y
`cobros.registrar_cobro_factura` levanta `ValueError` ante un booleano en `monto` o `medio_id`, antes de escribir, por si otro llamador le pasa el dict directo.

**Lo que puede romper al subir el pin:** una fixture de un producto que mande un booleano en uno de esos campos (se corrige el cuerpo de la fixture). Los productos que suman un campo numérico propio sin el
helper lo ven con la guardia. `libracommerce` reexporta el helper y la guardia de acá en un release aparte.

## [Unreleased] — La nota de crédito es una sola, del motor (fase 1)

Sin migración ni cambio de esquema. **Módulo nuevo `libracore.notas_de_credito`**: el núcleo de la nota de
crédito, igual para todos los productos (decisión del humano, 2026-10-04; ADR-014; `docs/notas-de-credito.md`).
Pone el tipo de nota, las guardas (la factura no se acredita dos veces, la nota sin CAE no se duplica, el
receptor sirve, un solo pedido a la vez por comprobante), el armado de la nota (importes copiados, **fecha de hoy**,
comprobante asociado, marca de la FCE) y el orden previas → numerar → registrar → pedir el CAE; el producto aporta
sólo costuras. `emitir_nota_de_credito` es la entrada; levanta `NotaNoPermitida` con un `codigo`.

**`facturas_router` pasa a ser un consumidor:** `POST /api/facturas/{id}/nota-credito` usa el núcleo y **su
comportamiento no cambia** (mismos códigos 400/409, mismos mensajes; los 66 tests del router siguen verdes). La
lista de nombres de tipo pasa a `tipos_comprobante.NOMBRE` (el router conserva `TIPO_LABEL` apuntando a ella). El
candado y la consulta de nota previa salen del router y viven en el núcleo. La nota de **débito** usa el mismo
`armar_nota`, sin guardas propias (una factura admite varias).

**Lo que puede notar un consumidor:** el 422 por receptor inválido ahora llega **antes** de numerar, también en
`nota-credito` (antes llegaba dentro de `solicitar_cae`).

## [Unreleased] — La guarda del CUIT no bloquea las notas (corrige v1.124.0)

Sin migración. **Corrige un error de `v1.124.0`:** `problema_del_receptor` aplicaba la regla del dígito
verificador también a las **notas de crédito y de débito**. Una nota hereda el receptor de una factura que
ARCA ya autorizó; si esa factura salió a un CUIT que no existe (en una clase A ARCA la autoriza con el aviso
`10238`: «tenés que emitir una Nota de Crédito o anular la operación»), la guarda **impedía justamente la
nota que ARCA pide**. **Medido en homologación el 2026-10-04:** la nota de crédito A al mismo CUIT
inexistente, asociada a esa factura, ARCA la autoriza (CAE con el mismo aviso). Ahora el verificador se
exige sólo a las **facturas**; las notas de clase A y FCE siguen exigiendo CUIT de 11 dígitos. Ningún
producto con emisión real desplegó `v1.124.0` antes de esta corrección.

## [Unreleased] — La guarda del CUIT del receptor vive en el motor

Sin migración ni cambio de esquema. **Cambia el comportamiento de `arca_wsfe.solicitar_cae`**
(todos los productos que emiten por ARCA): antes de cualquier llamada a ARCA rechaza, con un
mensaje que dice qué cliente y qué cargar, un receptor que no sirve. Se expone además
`problema_del_receptor(factura)`, para que un producto conteste con un 422 **antes de pedir el
número**, y `cuit_con_verificador_valido` y `cuit_del_receptor`.

**Qué exige:** CUIT de 11 dígitos en las clases **A** y en toda **FCE**; y, si el CUIT tiene 11
dígitos, que el dígito verificador cierre, **en cualquier clase**. Una B o una C sin CUIT, o con uno
que no es de 11 dígitos, siguen saliendo a consumidor final.

**Medido en homologación (2026-10-03):** una Factura A con CUIT `1` vuelve `[10013]` y `[10015]`;
un CUIT de 11 dígitos con el verificador mal vuelve `[10015]` («no se encuentra registrado en los
padrones») en una **B**, pero en una **A ARCA autoriza con CAE** y sólo avisa (`10238`: «la CUIT
receptora que ingresaste no existe, tenés que emitir una Nota de Crédito o anular la operación»).
Por eso la guarda **bloquea también la A**: sin ella se emite una factura a un receptor que no
existe para anularla después.

**También corrige** que un CUIT con puntos (`30.70933285.2`) no se reconocía como CUIT y el
comprobante salía con DocTipo 99; ahora se normaliza a dígitos. Y reemplaza la guarda parcial
«la FCE exige el CUIT del receptor» por esta, con otro mensaje.

**Lo que puede romper al subir el pin:** los tests de un producto que pasen por el
`solicitar_cae` real con un CUIT inventado (la fixture de este repo, `20123456789`, tampoco
cerraba). Se arreglan con un CUIT que cierre, p. ej. `20123456786`; no con una excepción.

**Por qué acá y no en cada producto:** es lógica fiscal que todos comparten (regla del 2026-10-03:
el arreglo de fondo vive siempre en el motor). LibraCargo la había escrito en su repo
([libracargo#243](https://github.com/marianocappucci/libracargo/pull/243)) y se muda acá.

## [Unreleased] — Una factura se acredita una sola vez

Sin migración ni cambio de esquema. **Cambia el comportamiento de
`POST /api/facturas/{id}/nota-credito`** (Contalibra y Restolibra): si la factura **ya tiene
una nota de crédito** —incluida una que quedó sin CAE— contesta `409` con el nombre de esa
nota, y dos pedidos simultáneos sobre la misma factura emiten una sola.

**Por qué:** la nota copia el original entero, ARCA no lleva el saldo de una factura común
(medido en homologación el 2026-10-03: acepta una segunda nota total sin una observación) y
cada nota registra un abono por el importe completo. Una factura a cuenta corriente de
14.000 quedaba en saldo 0 con una nota y en **−14.000** con la segunda. Un doble clic o dos
admins a la vez bastaban.

**Cómo:** consulta de las notas existentes más un candado en memoria por factura (el motor
corre con un solo proceso por instancia). Se descartó una restricción única en la base: una
base con duplicados históricos no podría crearla. Una nota sin CAE se autoriza
(`/autorizar`) o se borra; no se pide otra encima. Las notas de débito no cambian.

**Lo que no cubre:** facturas que **ya** tienen más de una nota; la guarda no las corrige ni las
detecta (hay que mirar cada producto). Tampoco hay nota parcial: la nota sigue copiando el
original.

## [v1.123.0] — El directorio de instancias deja de estar fijo en `repo_root/clientes`

Sin migración ni cambio de esquema. **El default no cambia**: un producto que no pase
nada ni defina la variable usa `repo_root / "clientes"`, igual que hasta ahora.

### Agregado

- `configure(clientes_dir=None)` y la variable de entorno `LIBRA_CLIENTES_DIR`.
  `ProductConfig.clientes_dir` resuelve, de mayor a menor: el parámetro, la variable
  (vacía cuenta como no definida) y `repo_root / "clientes"`. La variable se lee en cada
  acceso, no al configurar; usar una ruta absoluta.
- `ProductConfig.clientes_dir_override` (el valor que fijó `configure()`) y la constante
  `provisioning.CLIENTES_DIR_ENV`.

### Cambiado

- 🔑 **Una sola fuente de verdad.** `admin.services` leía `panel_admin.CLIENTES_DIR`, la
  constante que cada producto calculaba por su cuenta, mientras que `panel_admin` y el alta
  leían `get_config().clientes_dir`: con la ubicación configurable, el cron y el
  backoffice habrían mirado carpetas distintas. Ahora `services._clientes_dir()` usa
  `get_config().clientes_dir`. `CLIENTES_DIR` sigue en los scripts de los productos por
  compatibilidad, pero el backoffice ya no lo lee.
- `tests/provisioning/test_clientes_dir.py`: precedencia, default idéntico, panel y
  backoffice sobre el mismo directorio, y un barrido que falla si aparece
  `repo_root / "clientes"` fuera de la propiedad.

### ⚠️ Al subir el pin

- **Un producto que pase `clientes_dir=` a `configure()` exige este pin en TODO proceso que
  importe su `scripts/panel_admin.py`**, incluido el contenedor de `libra-backoffice`
  (pineado en una libracore vieja): con una anterior, `configure()` revienta con
  `TypeError: unexpected keyword argument 'clientes_dir'` (mismo mecanismo que
  `backup_zip`, 2026-08-12). Para mover la carpeta sin tocar código, usar la variable de
  entorno.
- La variable sólo surte efecto en los procesos que corren esta versión o una posterior:
  un backoffice con un motor viejo seguiría leyendo `repo_root/clientes`.

## [v1.121.0] — La clave privada de ARCA se guarda en 0600

### Corregido

- 🔴 **La clave privada que sube la pantalla quedaba en 644** —legible por cualquiera
  dentro del contenedor—, porque `arca_router` la escribía con
  `open(destino, "wb")` y la umask del proceso. Medido en la instancia dev de
  LibraCargo el 2026-10-02 (clave de homologación); por cómo está el código, lo
  mismo en todos los productos. El certificado es público; la clave es la
  identidad fiscal del cliente.
- `arca_certificados.escribir_clave_privada()`: escribe a un temporal creado ya
  con 0600 y reemplaza, así **no hay instante con la clave abierta** y una clave
  previa en 644 deja de existir en vez de conservar su modo.
- **Las instancias vivas se corrigen solas:** `arca_credenciales.paths_en_disco()`,
  por donde pasa toda emisión, deja en 0600 una clave que estaba abierta
  (`cerrar_permisos_de_la_clave`). También cubre lo que reescribe la clave sin pasar
  por la pantalla (restaurar un ZIP de respaldo). **Nunca levanta**: si el archivo
  es de otro usuario o el volumen no deja, la emisión sigue como estaba.
- ⚠️ Si un producto lee la clave con **otro usuario** del contenedor que el dueño
  del archivo, 0600 se la corta. Con los productos de la familia no pasa (un solo
  usuario), pero es lo primero a mirar si una emisión empieza a fallar al leerla.

## [v1.120.0] — El rechazo de ARCA deja de tragarse

Migración de Alembic: `0015_cae_error_en_facturas`.

### Corregido

- 🔴 `arca_facturacion.solicitar_cae` tragaba el rechazo de ARCA —sólo
  `logger.error`— y devolvía la factura numerada, cobrada y **sin CAE, sin que
  nadie lo viera**. Medido en producción el 2026-10-02: hoy no hay ninguna así,
  pero el día que ARCA exija algo (pasó con `CondicionIVAReceptorId` en
  homologación) quedarían todas en silencio.
- **No levanta la excepción, a propósito**: en los tres caminos que lo llaman
  (alta manual y notas, ventas, MercadoPago) el comprobante ya está numerado y se
  sigue con el cobro o el vínculo a la venta; relanzar dejaría un cobro sin
  factura o una factura huérfana. Lo que hace es **guardar el motivo en la
  factura**: `facturas.cae_error`, que viaja en el comprobante (alta, detalle,
  listado) y lo ve cualquier pantalla. Lo borra un CAE obtenido; el reintento
  (`POST /api/facturas/{id}/autorizar`) también lo deja anotado si vuelve a fallar.
- Con ARCA configurada y sin ticket (falló la autenticación o el pedido del
  número, y se numeró local) la factura queda con un motivo genérico
  (`MOTIVO_SIN_TICKET`); el concreto sigue en el log. Una instancia **sin** ARCA no
  muestra error: no hay CAE que pedir.

## [v1.119.0] — Facturación con ARCA: condición del receptor, ticket, letra y FCE MiPyME

Migración de Alembic: `0014_fce_mipyme`. Cuatro cambios, cada uno medido contra ARCA
homologación el 2026-10-02: lo que sigue es cada uno, del más nuevo al más viejo.

### Factura de Crédito Electrónica MiPyME (FCE)

Migración de Alembic: `0014_fce_mipyme`.

#### Agregado

- **FCE A, B y C (201, 206, 211) y sus notas de débito y crédito** (202/203,
  207/208, 212/213), por el mismo camino que ya existía: `facturas_router`,
  `arca_facturacion` y `arca_wsfe`. **Sin pantallas nuevas.** Probado contra
  ARCA homologación el 2026-10-02: alta, nota de crédito y nota de débito con CAE
  por el router real.
- `libracore.tipos_comprobante`: los tipos (facturas, notas, FCE, clase C, la
  letra y de qué factura sale qué nota) **en un solo lugar**. Reemplaza las
  copias de `(1, 6, 11)` / `(3, 8, 13)` de `db.facturas`, `db.resumen`,
  `db.dashboard`, `db.cuenta_corriente`, `libros_iva` y `pdf_generator`: una FCE
  que falta en una de esas consultas desaparece de ese listado sin error.
- `arca_config.fce_cbu` y `fce_transmision` (`SCA` o `ADC`), editables por
  `PUT /config/arca` (`None` = no lo toqués, `""` = borralo: un cliente de la API
  que no conoce la FCE no borra el CBU). `facturas` suma `fce_cbu`,
  `fce_transmision`, `fce_anulacion` y `cbte_asoc_fecha`.
- `GET /api/facturas/tipos` suma las FCE al selector **sólo si el emisor cargó su
  CBU y su modalidad**. `es_monotributista` no cambia.

#### Lo que ARCA exige y se midió

- La FCE manda `FchVtoPago` **aunque el concepto sea Productos** (10163), el CUIT
  del receptor (con consumidor final, 10015), el CBU de 22 dígitos (opcional 2101)
  y la modalidad de transmisión (opcional 27, 10216).
- Una nota de FCE manda **la fecha del comprobante asociado** (10158) y **sólo** el
  opcional 22 (`S`/`N`): con el CBU o el 27, 10172. La nota sale con `N`; `S` sólo
  lo acepta ARCA si el comprador rechazó la factura (10154).
- Todo lo que falta se valida **antes** de pedir el número (422 en el alta, o un
  error de `arca_wsfe`): el número es fiscal y no se devuelve.

#### Cambia

- ⚠️ Las FCE se emiten sólo desde `POST /api/facturas`; sus notas, desde la
  factura. `POST /api/facturas` con el tipo de una nota de FCE da 422.
### La letra de la factura corresponde al receptor

#### Corregido

- 🔴 Un emisor inscripto le emitía **Factura B a un receptor Monotributista**, y
  ARCA la rechaza (10243): medido en homologación el 2026-10-02, una A al mismo
  receptor sale con CAE. `arca_facturacion.tipo_de_comprobante()` es ahora la
  regla única: A a inscriptos y monotributistas, B a todos los demás, C si el
  emisor es monotributista. La usan `venta_facturacion` y `mp_facturacion` (que
  daba B siempre a un emisor inscripto).
- La emisión manual (`POST /api/facturas`) responde **422** con el motivo si la
  letra no corresponde al receptor, en vez de dejar un comprobante numerado y sin
  CAE. Si la condición del cliente es desconocida no se opina.

### Caché del ticket de WSAA

#### Corregido

- 🔴 `arca_wsaa.autenticar` pedía un ticket nuevo en cada llamada, y WSAA no
  entrega otro mientras haya uno vigente (`coe.alreadyAuthenticated`): se podía
  emitir **una factura cada 12 horas** por certificado y servicio. Ahora el
  ticket se guarda en `ARCA_TA_DIR` (por defecto `$DATA_DIR/arca_ta`), con
  permisos 0600, **una entrada por ambiente + servicio + huella del
  certificado**, y se reusa hasta 5 minutos antes de vencer.
- Es compartida entre workers (archivo, no memoria) y un `flock` evita dos
  logins simultáneos. Si ARCA contesta `alreadyAuthenticated` y el ticket no
  está en la caché, el error lo explica en vez de repetir el de ARCA.
- Sin directorio escribible se emite sin caché, como antes.
- La firma de `autenticar` no cambia; el login crudo pasó a `_pedir_ticket`.
### WSFE manda la condición de IVA del receptor (RG 5616)

#### Corregido

- 🔴 `arca_wsfe.solicitar_cae` no mandaba `CondicionIVAReceptorId`, y ARCA
  rechaza el comprobante sin él (error 10246; medido en homologación el
  2026-10-02). Ahora sale de `cliente_iva_cond` con
  `arca_wsfe.condicion_iva_receptor_id()`: traduce el código de la base al de
  ARCA (el `3` «No Responsable» pasa a `15` «No Alcanzado»), acepta los ids de
  ARCA tal cual, y **sin condición falla con un mensaje claro** —sólo infiere
  consumidor final cuando el comprobante no lleva CUIT y no es un A—. No hay
  valor por defecto silencioso.
- ⚠️ Un comprobante con CUIT del receptor y sin condición cargada, que antes se
  emitía, ahora no obtiene CAE hasta cargarla en la ficha del cliente.

## [v1.117.0] — Un pago a cuenta se aplica a facturas y se da de baja limpio

Migración `0013`: agrega `caja_movimientos.cc_pago_id` (nullable, sin FK). **No baja** (patrón de la
generación `0004`-`0008`): perder el vínculo cobro → pago dejaría los pagos aplicados sin baja limpia; para atrás,
restaurar el backup. El gate del schema congelado pasa a **376 columnas** (las dos fixtures).

### Qué llega

- `POST /api/cuenta-corriente/{id}/pagar` acepta `facturas=[ids]`: reparte el pago (la más vieja primero) y
  escribe **un cobro por factura** —lo que la marca "Cobrada"— sin un segundo abono. Sin `facturas` se comporta
  como antes. `GET /{id}` devuelve `facturas_pendientes`. Caso: Municipalidad de Suipacha, FC 74 y 75.
- `DELETE /pagos/{id}` **anula en el motor** todos los movimientos de caja del pago (`cc_pago_id`), igual en
  todos los productos. Antes sólo lo hacía el gancho `al_eliminar_pago` de VentaLibra; Contalibra y Restolibra
  dejaban la plata en el arqueo. El gancho sigue decidiendo si la baja procede.
  Los pagos anteriores a la migración no tienen movimientos ligados: en Contalibra y Restolibra se dan de baja
  como siempre (sin anular nada, como antes).
  ⚠️ **VentaLibra: no quitar todavía la búsqueda por referencia (`cc-pago-<id>`) ni el anulado de su gancho.**
  Es la única forma de encontrar el movimiento de sus pagos viejos, que no tienen `cc_pago_id`; con el motor
  anulando también no chocan (es idempotente), pero sacarlos ahora dejaría esa plata en el arqueo. Para
  normalizarlo: al subir el pin, backfill de `cc_pago_id` en VentaLibra a partir de esa referencia, y recién
  después simplificar el gancho.
- `db.caja.create_caja_movimiento(..., cc_pago_id=)` y `db.caja.anular_movimientos_de_cc_pago()`.
- `db.cuenta_corriente.get_facturas_pendientes_cc()`.

## [v1.115.0] — Recibos y consulta de CUIT como factories de router

Sin migración. Dos routers nuevos, extraídos de Contalibra (el mismo código, salvo el auth de cada producto).

### Qué llega

- `recibos_router.build_recibos_router(usuario_actual, solo_admin, get_venta=None)`: listar, detalle, PDF, emitir de
  factura/venta/cobranza y anular (gateado a `solo_admin`), sobre `libracore.db.recibos` y `libracore.recibos`. El único
  gancho es `get_venta` — de qué tabla sale una venta de mostrador (`ventas` del propio esquema o `sales` de LibraCommerce,
  según el producto); `emitir_recibo_factura`/`emitir_recibo_cobranza` no necesitan ninguno, ya resuelven contra tablas que
  todo producto comparte (`facturas`, `caja_movimientos`, `clients`, `cc_pagos`). Sin `require_module` fijo: un recibo nace
  de tres módulos distintos, así que el gate real vive en el botón que lo emite.
- `consultar_cuit_router.build_consultar_cuit_router(usuario_actual)`: `GET /api/consultar-cuit/{cuit}` contra el padrón de
  ARCA (WSPadron), usando `libracore.arca_credenciales`/`arca_wsaa`/`arca_wspadron` y la config ya cargada
  (`libracore.db.arca_config`). Sin certificados configurados, 503 en vez de fallar.

### Para quien ya usa `arca_credenciales.paths_en_disco`

El barrido `test_el_par_en_disco_es_una_sola_llamada.py::test_los_tres_call_sites_del_motor_la_usan` ahora espera **cuatro**
call sites (se sumó `consultar_cuit_router.py`); es de mantenimiento del propio repo, no afecta a un consumidor.

## [v1.113.0] — Los reportes de caja pueden dejar afuera la cuenta corriente (`sin_fiado`)

Sin migración. Aditivo: sin `sin_fiado` el comportamiento es **exactamente** el de siempre (el de Contalibra y Restolibra).

### Qué llega

`build_reportes_router(..., sin_fiado=False)`, `build_reportes_export_router(..., sin_fiado=False)` y `db.reportes.get_reporte_caja`,
`get_reporte_caja_medios` y `get_reporte_resumen` con `sin_fiado`: con `True` los reportes de **caja** dejan afuera las marcas de
cuenta corriente (`sql_no_es_cuenta_corriente`). **Fiar no es cobrar:** un producto cuya venta escribe un movimiento de caja por cada medio,
cuenta corriente incluido (la capa ERP de LibraCommerce), sumaría el fiado como ingreso, y el reporte dejaría de coincidir con
`get_caja_resumen` y con el arqueo del turno (VentaLibra, ADR-027). El `resumen` del puerto lo decide el producto
(`libracommerce.erp.reportes.puerto_de_reportes(sin_fiado=...)`).

> **Para Contalibra y Restolibra:** con la capa ERP sus reportes de caja tienen el mismo defecto (cuentan la cuenta corriente como
> ingreso). Se activa pasando `sin_fiado=True` al montar el router; no se hizo acá porque cambia números que hoy ven sus usuarios.

## [v1.112.0] — Cajas y turnos aceptan las variantes de un producto con sucursales (`OpcionesCajas`, `validar_apertura`, `enriquecer`)

Sin migración. Aditivo: sin `opciones=` ni ganchos el comportamiento es el de siempre (el de Contalibra y
Restolibra); los 12 tests previos de `test_caja_router.py` pasan sin tocarlos.

### Qué llega

- `build_cajas_router(..., opciones=OpcionesCajas(...))` con seis ganchos opcionales: `autorizar_escritura` (quién puede crear, editar, predeterminar y borrar: una
  `Depends(...)`, como `autorizar_cierre`), `validar_alta`,
  `validar_edicion(payload, actual)`, `al_desactivar(actual)`, `predeterminar(caja_id)` (reemplaza a
  `set_default_caja`, que desmarca **todas**: un producto con sedes la quiere por sucursal) y
  `enriquecer(caja)`. Cada gancho decide con una `HTTPException`: el motor no sabe qué código le toca a la regla
  de cada producto.
- `CajaPayload.sucursal_id` (sólo al crear) y `GET /api/cajas?sucursal_id=`; cada caja de la respuesta trae
  `tiene_turno_abierto` (el POS no ofrece una caja ocupada). `db.turnos.turno_abierto_de_caja` y
  `cajas_con_turno_abierto` son las consultas.
- `build_turnos_router(..., validar_apertura=, enriquecer=)`: el primero corre antes de abrir (sin él, abrir con
  un turno abierto devuelve ese turno, como siempre); el segundo agrega campos a cada turno de la respuesta
  (la caja y la sucursal).
- `GET /api/turnos/actual`: el turno abierto de quien pregunta con su resumen, o `{"turno": null}`.
- **Arreglo:** abrir un turno en un día cerrado (`DiaCerradoError`) contestaba 500; ahora es 409.

## [v1.111.0] — El router de cuenta corriente acepta las variantes de un producto (`OpcionesCuentaCorriente`)

Sin migración. Aditivo: sin `opciones=` el comportamiento es **exactamente** el de siempre (el de Contalibra y
Restolibra); los 8 tests previos del router pasan sin tocarlos.

### Qué llega

`build_cuenta_corriente_router(..., opciones=OpcionesCuentaCorriente(...))` con tres ganchos opcionales, para
que un producto cuyo cobro tiene reglas propias las declare en vez de reescribir el router (VentaLibra, que
tiene el arqueo por turno, ADR-027, adopta el router del motor con estos tres):

- `validar_pago(payload, user) -> CobroAprobado`: se llama antes de registrar el pago; puede rechazarlo
  (`HTTPException`: 409 sin turno, 422 con una caja ajena, medio no cobrable...) y decide `caja_id`,
  `turno_id` y la plantilla `referencia_movimiento` (`"cc-pago-{pago_id}"`) del movimiento de caja.
- `cajas(user) -> list[dict]`: qué cajas ofrece `GET /cajas` (por defecto, todas).
- `al_eliminar_pago(pago_id, user)`: se llama al dar de baja un pago, **antes** de anular sus recibos y de
  borrarlo: es donde un producto anula el movimiento de caja que el pago generó; si levanta, no se toca nada.

`GET /cajas` ahora lleva la dependencia `usuario_actual` (los productos ya montan el router detrás de su
gate de sesión: no cambia quién puede llamarlo).

## [v1.110.0] — El POS de MercadoPago vive en la caja, con fallback a config para una sola caja

Migración `0012`: agrega `cajas.mp_pos_id` (nullable). **No baja** (patrón de la
generación `0004`-`0008`, caso hermano exacto de `0004_punto_venta_por_caja`):
volver a compartir el POS reabre el defecto que esta versión cierra; para atrás,
restaurar el backup. El gate del schema congelado pasa a **375 columnas**.

### Qué llega

- `cajas.mp_pos_id`: el `external_id` del POS de MercadoPago deja de ser dato de
  instancia y pasa a vivir en cada caja. Con dos cajas compartiendo el POS, el
  modelo de QR escribe el **monto** en el POS: la última venta pisa el monto de
  la anterior y el cliente paga otra cosa.
- CRUD de cajas: campo `mp_pos_id` alfanumérico (`^[a-zA-Z0-9]+$` — MercadoPago
  no acepta guiones ni espacios), vacío → NULL, error 422 propio
  (`ExternalIdMercadoPagoInvalido`) al lado del 409 de punto de venta repetido.
- Cobro QR: `mp_pos_id_con_fallback()` resuelve usuario → turno abierto → caja →
  `mp_pos_id`. Si la caja activa no tiene POS y hay **exactamente una** caja, cae
  al `mp_pos_id` de config: las instancias de una sola caja siguen cobrando sin
  tocar nada. Con más de una caja no hay fallback → 422 con mensaje accionable.
- `mp_user_id` y access token siguen a nivel instancia.

### Antes de actualizar el pin del consumidor

- Instancia con **una sola caja**: nada que hacer, el fallback cubre.
- Instancia con **varias cajas**: configurar `mp_pos_id` por caja (la pantalla de
  Cajas de libra-ui `0.74.0` lo edita). Mientras una caja activa sin POS conviva
  con otras, el cobro por QR responde 422 con el mensaje de qué falta.

## [v1.109.0] — La copia externa sale cifrada, y el botón de backup arma el mismo ZIP que el cron

Sin migración de Alembic.

### ⚠️ Antes de actualizar el `.venv-scripts` de un host

- **Crear la passphrase del parque**: `/root/secretos/resguardo_cifrado.key`,
  `0600`, de 32 caracteres o más (o la ruta de `RESGUARDO_CIFRADO_CLAVE_ARCHIVO`).
  Sin ella `panel_admin.py resguardo-externo` **no sube**: falla cerrado, el error
  queda en `.externo.json` y `estado-externo` se pone rojo. **Anotarla fuera del
  servidor**: si se pierde, lo ya subido es irrecuperable. **No se rota**: rotarla
  deja ilegible todo lo subido con la anterior.

### Cambiado

- `provisioning.resguardo_externo.subir`: la copia externa sale cifrada con
  `rclone crypt`, **o no sale**. El remoto cifrado se arma por variables de
  entorno en cada llamada —la passphrase no entra al `rclone.conf` del enlace ni
  al argv—, con `filename_encryption = off` para que la verificación y la
  retención por nombre sigan funcionando. Suma una verificación de contenido con
  `cryptcheck`, exigiendo por `--match` que el archivo se haya comparado. El
  estado suma `cifrado` y `huella_clave` (`sha256[:8]`).
- `resguardo_estado.esta_al_dia`: una copia que subió sin `cifrado: true` ya no
  está al día. `resumen()` suma `detalle.cifrado`.
- `config_router.build_backup_router`: el botón de la pantalla suma, por request,
  las carpetas de `directorios_de_datos(<padre de backups_dir>)` a las que declara
  el producto —la misma regla que el cron del host y el restore por CLI—. Sólo si
  `backups_dir` se llama `backups`. En VentaLibra, Gestiolibra y LibraClub el ZIP
  del botón pasa a llevar `arca_certs/`, y restaurar desde el botón un ZIP del cron
  repone todas sus carpetas.

### Para los consumidores

- **El subidor corre en el host**, desde el `.venv-scripts` de cada producto: el
  pin de los productos **no** lo mueve. Hay que actualizar los ocho, no sólo los
  que hoy tienen resguardo: el bloqueo de subir en claro está en este código.
- `esta_al_dia` y el botón viajan con el pin de cada producto, sin cambios en su
  código.

## [v1.108.0] — Los secretos de `config.json` salen del archivo en texto plano

Sin migración de Alembic propia. Requiere libraauth `v0.46.0`, que trae la
tabla `secretos_instancia` en la revisión `0002` de su cadena.

### Agregado

- `config_manager.usar_almacen_de_secretos(almacen)`: enchufa un almacén
  cifrado para `mp_access_token`, `mp_webhook_secret` y
  `email_smtp_password` (`config_manager.CLAVES_SECRETAS`). El almacén es
  cualquier objeto con `get(clave)` y `set(clave, valor)`; en la familia es
  `libraauth.secretos.SecretosRepository`. **LibraCore no importa
  libraauth**: el producto, que tiene los dos, lo inyecta.
- `config_manager.migrar_secretos_al_almacen()`: saca de `config.json` los
  secretos que quedaron en claro. Idempotente, pensada para correr en cada
  arranque. Devuelve un informe con **nombres de claves, nunca valores**.
- `config_manager.almacen_de_secretos()`: el almacén enchufado, o `None`.

### Cambiado

- Con almacén enchufado, `load()` trae los tres secretos del almacén (con el
  JSON como respaldo mientras el almacén esté vacío) y `save()` los escribe
  ahí y los deja **vacíos en el JSON**. Para los consumidores no cambia nada:
  `load()` sigue devolviendo el secreto en claro bajo la misma clave.

### ⚠️ Al subir el pin

- 🔴 **Subir el pin solo no cambia nada.** Sin `usar_almacen_de_secretos()`,
  `config_manager` se comporta exactamente como antes y sigue escribiendo
  el secreto en el JSON. El producto tiene que enchufar el almacén sobre el
  mismo session factory que `UserRepository`, y llamar a
  `migrar_secretos_al_almacen()` en el arranque **después** de
  `exigir_schema_al_dia()` (la tabla sale de la revisión `0002` de
  libraauth). Llamarla sin almacén enchufado levanta `RuntimeError`: falla
  ruidosa a propósito.
- Si cifrar falla (instancia sin `SECRET_KEY`), la migración **no toca el
  `config.json`**: la instancia sigue funcionando con la credencial que
  tiene, y la clave queda listada en `fallaron`.

## [v1.118.0] — El tema de la suite de una instancia: `GET /api/tema` público y `PUT /api/tema` detrás del gate

Sin migración. Fase 2 de 4 del tema por suite (ADR-012; ADR-007 de `libra-ui`).

### Agregado

- `libracore.tema_router`: `build_tema_router()` (`GET /api/tema` → `{"tema": {clave: "#rrggbb"}}`, **sin gate** y con
  `Cache-Control: no-cache`: el login también va con los colores de la suite) y `build_tema_admin_router()` (`PUT /api/tema`, el tema
  COMPLETO; `{}` lo borra). El producto monta la escritura con su gate de admin, **que tiene que aceptar también el token de servicio del backoffice** (en VentaLibra,
  `requiere_o_servicio("config")`; con `requiere("config")` a secas el backoffice no entra).
- El tema vive en la clave `tema` del `config.json` de la instancia. Una instancia, un tema: sigue andando igual si el backoffice no responde.
- Sólo se valida la **forma** (clave de hasta 40 letras y números, valor `#rgb`/`#rrggbb` normalizado a `#rrggbb`, máximo 32 colores;
  422 si no). La lista de colores editables y el contraste viven en `libra-ui/tema`: no se copian acá.

## [v1.122.0] — `libracore.testing.pg_por_worker`: una base por worker de xdist, restaurada desde plantillas

Sin migración ni cambio de runtime: sólo se importa desde tests.

### Agregado

- `libracore.testing.pg_por_worker.base_por_worker(clave, url)`: crea (y borra al
  salir) la base de PostgreSQL de **este** worker de pytest-xdist, y devuelve un
  objeto con `.url` y `.restaurar(plantilla, construir)`. `restaurar` arma la
  plantilla la primera vez con `construir(url)` y deja la base del worker como una
  copia nueva con `CREATE DATABASE ... TEMPLATE` (~0,1 s contra ~1,5 s de rearmar
  el schema, medido en VentaLibra y LibraCommerce).
- Es idempotente por proceso: un `conftest.py` importado dos veces, o el proceso
  que lanza a los workers, no recrean ni comparten la base.

### ⚠️ Al subir el pin

- No cambia nada si no se usa. Para usarlo hace falta PostgreSQL >= 13 (`DROP
  DATABASE ... WITH (FORCE)`) y un rol con CREATEDB. Receta y criterios en
  `reglas/ci.md` del wiki.

## [v1.107.0] — Reabrir día: un admin puede anular un cierre diario, con motivo

Migración de Alembic: `0011_reabrir_cierre_diario`.

### Agregado

- `libracore.db.cierre_diario.reabrir_dia(cierre_id, usuario_id, motivo)`:
  anula un cierre (no lo borra — le marca `anulado_en`/`anulado_por`/
  `motivo_anulacion`) y libera el día para volver a abrir turnos. Motivo
  obligatorio; rechaza un cierre inexistente, ya anulado, o si la sucursal
  tiene un cierre ACTIVO de una fecha posterior (dejaría un día suelto
  detrás de uno ya cerrado). El cierre anulado conserva su `numero`; volver
  a cerrar el mismo día arma uno nuevo, con el siguiente.
- Endpoint `POST /api/cierre-diario/{id}/reabrir` (`build_cierre_diario_router`),
  body `{motivo}`. 404 / 409 (ya anulado, cierre posterior) / 422 (motivo
  vacío).
- `cierres_diarios` suma `anulado_en`, `anulado_por` (FK a `usuarios`,
  `ON DELETE SET NULL`) y `motivo_anulacion`. El UNIQUE de fecha
  (`sucursal, fecha`) pasa a ser un índice PARCIAL (`WHERE anulado_en IS
  NULL`) — un cierre anulado ya no bloquea volver a cerrar ese día. El de
  `numero` no cambia.

### ⚠️ Al subir el pin

- 🔴 **`POST /{id}/reabrir` sólo se monta si el producto pasa
  `autorizar_reabrir`** (parámetro nuevo, opcional). Anular un cierre es más
  sensible que cerrarlo y el pedido es "sólo admin"; el motor no sabe cómo se
  llama ese rol en cada producto, y la protección de módulo deja pasar al
  cajero en VentaLibra y LibraClub. Sin el parámetro la ruta **no existe**:
  subir el pin sin tocar `main.py` es seguro y no cambia nada. Para ofrecer
  la reapertura, `autorizar_reabrir=Depends(require_role("admin"))` (o el
  helper de rol del producto).
- Los mensajes de `DiaCerradoError`/`DiaYaCerradoError` ahora muestran la
  fecha en `dd-mm-aaaa` (antes ISO). Ningún test de los 4 consumidores
  (VentaLibra, LibraClub, Contalibra, Restolibra) asserteaba el texto viejo
  al momento de este cambio — revisar igual si algo nuevo lo hace.
- `cierre_diario.crear_tablas()` sigue siendo `CREATE TABLE/INDEX IF NOT
  EXISTS` puro — no migra una tabla existente (eso es trabajo exclusivo de
  la `0011`, a propósito: LibraClub la llama en cada arranque, y un `ALTER`
  ahí adentro desharía un `alembic downgrade` en el próximo boot — ver el
  docstring de la migración).

## [v1.106.1] — El restore no deja pools muertos, frena si no puede migrar la base restaurada y dice por qué falló

Sin migraciones de Alembic. Corrige el motor de restore de v1.106.0, que no llegó a ningún producto: los tests de restore de los productos lo encontraron en los PRs de bump.

### Corregido

- 🔴 **Pools muertos después del intercambio** (#285). El intercambio termina las conexiones de la base viva y los pools de SQLAlchemy no se enteraban: la primera request después de restaurar moría con `AdminShutdown`. Pasar `reabrir_conexiones` no alcanzaba —dos productos pasaban `engine.dispose` y tenían otro engine—. Ahora `respaldo_postgres.vigilar_pools()` escucha `connect`/`checkout` en la clase `Pool`: toda conexión abierta antes del último intercambio se descarta al pedirla y el pool abre otra. Alcanza a todos los pools del proceso, también a los creados antes, sin registro ni cambios en los productos. Un engine a otra base sólo se reconecta. Alcance: el proceso que restaura.
- 🔴 **Una base que ninguna variable de entorno nombra ya no se restaura** (#285). La reescritura por valor no encontraba qué cambiar y las migraciones corrían contra otra cosa. Con migraciones declaradas, `bases_sin_variable` frena **antes del backup previo** con un mensaje que nombra la base.
- **`errores_de_stderr` trae la excepción de Python** (`...Error: ...`, `psycopg.errors.X: ...`) y la sentencia `[SQL: ...]`, y descarta el link *"(Background on this error at: ...)"*, que era lo único que sobrevivía de un traceback de alembic (#285).

### ⚠️ Al subir el pin

- Un test de producto que le pase la URL de la base a la app **sólo en proceso** va a recibir 422 con *"ninguna variable de entorno apunta a …"* al restaurar con migraciones. Pasa en LibraDesk: su conftest tiene que poner la URL en el entorno.
- Un fixture que arme la base con `create_all`, sin tabla de versión de Alembic, no se puede restaurar con migraciones: `alembic upgrade head` intenta construirla de cero. Pasa en LibraCargo (`DuplicateObject: type "accion_auditoria" already exists`). En producción las bases están migradas.

## [v1.106.0] — Un solo motor de restore, y un backup sin sus bases ya no sale en silencio

Sin migraciones de Alembic.

### Cambiado

- 🔴 **Un solo motor de restore: `libracore.respaldo.restaurar(...)`** (#281). La
  pantalla de Configuración (`restaurar_backup`, misma firma) y `panel_admin.py
  restore-db` lo llaman sin lógica propia. En PostgreSQL:
  - El dump se restaura en `<base>__restore`, una base nueva; las migraciones
    declaradas corren **contra esa base**, y recién al final se intercambian los
    nombres, todas las bases o ninguna. Antes, `pg_restore --clean` sobre la viva
    dejaba vivas las tablas que no estaban en el dump, y eso rompía las
    migraciones posteriores.
  - Cualquier falla antes del intercambio deja la base viva intacta.
  - La viva queda como `<base>__antes_restore`; la del restore anterior se
    reemplaza recién después de un intercambio exitoso. El resultado trae
    `antes_restore` y `como_volver`.
  - Se valida `pg_restore` ≤ la major del servidor antes de tocar nada.
  - Las migraciones salen de `get_config().migraciones` de la imagen
    (`respaldo.migraciones_de_la_imagen`, que importa `scripts/panel_admin.py`).
    **Si no se pueden leer, no se restaura.**
- **`panel_admin.py restore-db` es un envoltorio del motor** (#281): para sólo la
  app, corre `python -m libracore.respaldo restaurar` en un contenedor efímero de
  la misma imagen y la vuelve a levantar aunque falle. **Sólo acepta ZIP.** Se
  retiró su rama SQLite.
- `principal_primero` y `directorios_de_datos` pasan de `provisioning.panel_admin`
  a `respaldo` (quedan alias con el nombre viejo).

### Corregido

- 🔴 **Un backup sin sus bases ya no sale en silencio** (#282). VentaLibra bajaba
  desde su pantalla ZIPs con 0 entradas:
  - `Instancia` rechaza una URL (`scheme://`) en `bases`, con un mensaje que manda
    a `postgres_url`/`postgres_extra`.
  - `_copiar_base`: una base declarada que no existe levanta `BackupInvalido`.
  - `crear_backup` borra el ZIP a medio armar si algo falla.
  - `POST /backups` y `GET /backup-ahora` verifican el ZIP (`verificar_backup`)
    antes de guardarlo o entregarlo; si no pasa, lo borran y contestan 500 con el
    motivo.
- Los errores de `pg_dump`/`pg_restore` traen todas las líneas de error de stderr,
  no la última (#281).

### ⚠️ Al subir el pin

- **Un producto que pase URLs en `bases=` no arranca**, si arma la `Instancia` en
  `create_app`. Verificado en `origin/develop` de los ocho: VentaLibra ya lo
  corrigió (#269).
- **El restore necesita `scripts/` en la imagen.** LibraCargo (#211) y LibraClub
  (#260) ya lo copian; sin eso su restore aborta antes de tocar.
- El usuario de la instancia tiene que poder `CREATE DATABASE`,
  `pg_terminate_backend` y `ALTER DATABASE ... RENAME`. Medido en los 21 sidecars
  del VPS: superuser en todos.

## [v1.105.0] — El alta corre las migraciones de la imagen, y el movimiento sin caja toma la del turno

Sin migraciones.

### Corregido

- **`create_caja_movimiento` sin `caja_id` toma la caja del turno** (#277).
  LibraCommerce registra los movimientos de venta con `turno_id` y sin
  `caja_id`, así que en una instancia con varias cajas todo quedaba en la caja
  default. El arqueo y el cierre diario no se veían afectados (van por
  `turnos_caja.caja_id`); los reportes filtrados por caja sí.

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
