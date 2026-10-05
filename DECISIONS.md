# Decisiones arquitectónicas — LibraCore

Registro ADR. Las decisiones no se borran; si dejan de aplicar, se marcan como
reemplazadas. Las fechas y el motivo salen del código y de la historia registrada
en el wiki del ecosistema (entidad `libracore` y sus bitácoras).

## ADR-001 — LibraCore es un motor común, no un producto

- Estado: aceptada
- Fecha: 2026-07-13
- Contexto: Contalibra y Restolibra compartían auth, PDF, configuración, ARCA,
  MercadoPago, provisioning y acceso a datos, cada uno con su copia.
- Decisión: extraer lo transversal a un paquete interno versionado (`libracore`)
  que los productos consumen y **componen**; LibraCore no expone una app propia.
- Consecuencias: una sola implementación probada para todos.
- **Enmienda (2026-10-03, decisión del humano):** el criterio de qué sube al motor ya no se
  discute caso por caso. **Sube todo lo que otro producto comparte o podría compartir**, y el
  **arreglo de fondo vive siempre en el motor, nunca en un producto**: el producto aporta
  costuras (hooks) y lo propio de su vertical. Si falta la costura se agrega acá; si un producto
  necesita el arreglo antes, se hace acá igual y el producto sube el pin. La redacción original
  («caso por caso, no por conveniencia») queda como historia: la reemplaza esta enmienda.

## ADR-002 — Integración por configuración inyectada, mínima huella en el consumidor

- Estado: aceptada
- Fecha: 2026-07-13
- Contexto: los productos ya tenían ~200 call sites de acceso a datos y su propio
  modelo de usuarios; reescribirlos para adoptar el motor sería inviable.
- Decisión: LibraCore se integra por configuración inyectada y callbacks, no
  reescribiendo call sites. `db.core.configure(db_path)` se llama una vez al
  arrancar y `get_connection()` sigue sin argumentos; el schema de usuarios y la
  lógica de recetas entran por callback (`configure_resolver_receta`).
- Consecuencias: un producto adopta o retira una pieza del motor sin tocar el
  resto de su código; es el principio que atraviesa todo el paquete.

## ADR-003 — Una sola capa de acceso a datos, dual SQLite/PostgreSQL

- Estado: aceptada
- Fecha: 2026-08 (capa `db/_postgres.py`)
- Contexto: la familia migró a PostgreSQL, pero el motor tiene que poder abrir
  también SQLite (bases legadas, la base de LibraEdge, el `schema_dump`).
- Decisión: mantener **una** capa de acceso (`libracore.db.*`) escrita en estilo
  `sqlite3` (placeholders `?`, `Row`, excepciones de `sqlite3`) y un wrapper
  (`db/_postgres.py`) que traduce SQL, placeholders y errores contra PostgreSQL.
- Consecuencias: los consumidores escriben SQL una vez; la capa dual no es deuda,
  es lo que permite `schema_dump` y la compatibilidad. Se paga con la
  complejidad del traductor (737 líneas).

## ADR-004 — La restricción PostgreSQL-only vive en el producto, la capacidad en el motor

- Estado: aceptada
- Fecha: 2026-08-25
- Contexto: la familia decidió PostgreSQL-only en producción/dev/tests
  (2026-08-12), pero el motor necesita seguir abriendo SQLite para
  `schema_dump`, migraciones sobre bases viejas y el nodo LibraEdge.
- Decisión: `db.core.configure()` sigue siendo **neutral** (acepta ruta SQLite o
  URL PostgreSQL); la guarda que rechaza SQLite se pone en el **arranque de cada
  producto**, no dentro del motor.
- Consecuencias: la regla "este producto no habla con otro motor" es del
  producto; el motor conserva la capacidad que necesitan las herramientas.

## ADR-005 — El alias `Conexion` y la traducción de errores por nombre, no por comportamiento

- Estado: aceptada
- Fecha: 2026-08-25
- Contexto: las funciones de `db/*` estaban anotadas `sqlite3.Connection` y contra
  PostgreSQL reciben el wrapper; leer la anotación llevó a escribir un arreglo con
  dialecto PostgreSQL que habría roto la corrida SQLite (incidente en VentaLibra).
- Decisión: introducir el alias `Conexion = sqlite3.Connection | ConnectionWrapper`
  y traducir las excepciones de psycopg a las de `sqlite3` **por nombre**
  (`_errores_como_sqlite3`).
- Consecuencias: la API tiene la misma forma en los dos motores. Pero la
  traducción **no** cubre el comportamiento transaccional: en PostgreSQL un error
  aborta la transacción y en SQLite no, así que los reintentos se auditan caso por
  caso (se encontró uno roto el 2026-08-25).

## ADR-006 — Las migraciones viajan dentro del wheel

- Estado: aceptada
- Fecha: 2026-08-25 (`v1.53.0`)
- Contexto: `migrations/` vivía en la raíz del repo, fuera de `packages`, así que
  no viajaba al contenedor: de 14 bases con schema de LibraCore, 7 no tenían
  `alembic_version` y 2 quedaron en `0001_baseline`.
- Decisión: mover `migrations/` **adentro** de `libracore/` y exponer el runner
  como console script `libracore-migrar` y como API (`from libracore.migrar
  import upgrade`), resolviendo el destino con `url_de_core`, no con
  `DATABASE_URL` a secas.
- Consecuencias: un consumidor instalado con pip aplica las migraciones sin clonar
  el repo; el deploy puede correrlas.

## ADR-007 — Un solo criterio para "esto es una URL, no un archivo"

- Estado: aceptada
- Fecha: 2026-08-09
- Contexto: el provisioning, el panel de admin y los scripts de backup recibían
  "la base" como un string que podía ser ruta o URL, y cada uno decidía por su
  cuenta — de ahí salieron el defecto del backup (trataba el nombre como ruta) y
  el del plan de módulos.
- Decisión: centralizar el criterio en `db.core.es_url_postgres()` y abrir sin
  estado global con `db.core.conectar(destino)`, del que `get_connection()`
  delega.
- Consecuencias: el código que trabaja sobre una instancia ajena (provisioning,
  backup) funciona igual contra SQLite y PostgreSQL.

## ADR-008 — El provisioning se consume por atributo; F401 se tolera en los re-exports

- Estado: aceptada
- Fecha: 2026-09-02 (unificación de ruff, E2)
- Contexto: `libracore.admin.services` hace `import panel_admin as pa` y llama
  `pa.find_client(...)`; un `ruff --fix` de F401 sobre los backoffices borraría
  esos re-exports "sin usar" y rompería la carga por atributo.
- Decisión: dejar F401 en el ignore para los re-exports del backoffice en vez de
  reescribir el patrón de consumo.
- Consecuencias: el lint no rompe el mecanismo; a cambio, esos módulos no reciben
  el chequeo de imports sin uso.

## ADR-009 — Hora de Argentina fijada en el motor y en el sidecar, no sólo con `TZ`

- Estado: aceptada
- Fecha: 2026-08-23
- Contexto: todo sistema de la familia arranca en UTC-3 fijo; pero `TZ` en la
  imagen sólo cambia el `date` del contenedor, no el `now()` del servidor
  PostgreSQL (se graba en el `initdb`, una sola vez).
- Decisión: estampar los timestamps del backend con `_ar_now()` de LibraCore y
  fijar la zona en el **sidecar** (`postgres -c timezone=...`), midiendo con
  `select now()` y no con `docker exec date`.
- Consecuencias: base y proceso quedan en la misma hora; la presentación en
  `dd-mm-aaaa` es una capa aparte.

## ADR-010 — El hook de recetas es opcional e inyectado

- Estado: aceptada
- Fecha: 2026-07 (extracción inicial)
- Contexto: Restolibra descuenta stock por receta (ingredientes) y Contalibra
  descuenta el producto vendido; `descontar_stock_venta` es común.
- Decisión: exponer `configure_resolver_receta(resolver)` — `None` = comportamiento
  simple (Contalibra); Restolibra inyecta un callable `(producto_id) -> receta`.
- Consecuencias: el motor no conoce el concepto "receta"; el vertical que lo
  necesita lo aporta, sin que el otro lo arrastre.

## ADR-011 — Cierre diario: acto registrado por sucursal, guarda en `create_turno`, sin autorización propia

- Estado: aceptada
- Fecha: 2026-09-13
- Contexto: VentaLibra y LibraClub necesitan cerrar el día operativo de una
  sucursal (todas sus cajas y turnos) con comprobante impreso, de forma
  numerada y no repetible. Las sucursales viven en la base del PRODUCTO;
  `cajas.sucursal_id` ya era un entero sin FK desde antes (ver la nota de
  `schema.py`).
- Decisión, en sus partes:
  - **Es un acto registrado**: `cierres_diarios` guarda una FOTO (por turno,
    por caja y de la sucursal) tomada al momento del cierre — reimprimir no
    vuelve a leer `caja_movimientos`, así que un movimiento anulado después
    no le cambia el comprobante ya entregado.
  - **La unicidad es por EXPRESIÓN** (`COALESCE(sucursal_id,0), fecha` y
    `..., numero`), no por columna lisa: ni SQLite ni PostgreSQL consideran
    que dos `NULL` colisionen, y una sucursal-NULL (turnos sin caja, o cajas
    sin sucursal — el caso de datos viejos, o de cualquier producto que
    todavía no terminó de asignar cajas a sucursales) es un caso real que
    tiene que poder cerrar una vez y no dos.
  - **El día operativo es el de la APERTURA del turno**, en hora Argentina
    (`_ar_now()`/`_AR_TZ`): un turno de 22:00 a 03:00 pertenece al día en que
    abrió. Se filtra con `substr(apertura,1,10)` — `apertura` es TEXT, no una
    columna de fecha, mismo criterio que `PARTE_TURNOS` de `db/logs.py`.
  - **La guarda de apertura vive en `turnos.create_turno()`**, no en cada
    producto: es el camino común de VentaLibra y LibraClub (los dos abren
    turnos llamando a esta función), así que los cubre a los dos sin que
    ninguno haya tocado su alta de turnos. Para un producto sin sucursales la
    tabla `cierres_diarios` queda vacía y la guarda es un no-op. Si la tabla
    todavía no existe (código desplegado antes de correr la migración —
    "el deploy no corre migraciones solo" es una regla ya vigente de esta
    familia), la guarda no revienta: se comporta como si nunca se hubiera
    cerrado nada, igual que antes de esta versión.
  - **La numeración correlativa por sucursal reintenta con una conexión
    nueva por intento** (mismo patrón que `facturas.crear_factura`): en
    PostgreSQL un error aborta la transacción, así que no se puede seguir
    escribiendo sobre la misma conexión después de un `IntegrityError`. Cada
    reintento repite las validaciones desde cero, así que una colisión real
    (el día ya se cerró) no se reintenta a ciegas: se convierte en el error
    correcto en el siguiente intento.
  - **El motor recibe `usuario_id` y NO autoriza.** Quién puede cerrar el día
    lo decide el producto — hoy (2026-09-13) la respuesta es "admin o
    cajero", pero con el nombre de rol que tenga cada uno. La factory de
    router (`caja_router.build_cierre_diario_router`) expone un parámetro
    `autorizar_cierre` inyectable (una dependencia de FastAPI) para el
    endpoint de cierre en particular; no hay ningún `require_admin` fijo
    adentro del motor, y sin ese parámetro el endpoint no impone ningún rol
    propio.
  - **El motor no conoce sucursales por nombre.** Los tickets y el router
    reciben `sucursal_nombre` (y, para el ticket de turno, también lo resuelve
    el producto) porque esa tabla vive del lado del dominio; `caja_nombre` sí
    lo resuelve el motor, porque `cajas` es suya.
  - **Las tres tablas nuevas NO entran a `init_core_schema()`** (congelada
    desde la `0001`): su DDL vive en `db/cierre_diario.crear_tablas()` y lo
    aplica la migración `0009_cierre_diario` — mismo criterio que toda
    revisión posterior a la baseline.
  - **El ticket de un turno se imprime EN VIVO, antes de que exista cualquier
    cierre diario** — es el caso normal: el cajero cierra su turno y lo
    imprime en el momento. `arqueo_turno_en_vivo()` calcula el mismo arqueo
    que la foto, con la MISMA función de medios y de diferencia
    (`_medios_de_turno`/`_diferencia`) — no una segunda cuenta. El endpoint
    (`GET /turno/{turno_id}/ticket`, por el id REAL del turno, no por el de
    la foto) resuelve solo cuál de los dos casos aplica: si el turno ya entró
    a una foto la usa, si no lo calcula en vivo, y devuelve 409 si el turno
    sigue abierto.
  - **`sucursal_id=None` es SIEMPRE "sin sucursal"**, en las cuatro funciones
    del módulo por igual. La primera versión de esta ADR dejaba
    `listar_cierres()` como la excepción (`None` = "todas", por calco de
    `db.caja.get_all_cajas()`) — una misma llamada sin argumentos quería decir
    cosas distintas según qué función la recibiera. "Todas" ahora es
    `listar_cierres(todas=True)`, explícito.
  - **El `except` de la tabla ausente es angosto por mensaje, no por clase
    sola.** `dia_cerrado()` atrapa `OperationalError`/`ProgrammingError` de
    `sqlite3` y sólo se los traga si el TEXTO dice "no such table" o "does
    not exist" mencionando `cierres_diarios` — porque la traducción de
    PostgreSQL (`_postgres._equivalente_sqlite3`) sube el MRO de
    `UndefinedTable` (SQLSTATE 42P01) y no aterriza en `OperationalError`
    sino en `ProgrammingError`, sin conservar el SQLSTATE original. Un
    `ProgrammingError` real por otro motivo (una conexión cerrada, por
    ejemplo) no menciona la tabla y se propaga.
- Consecuencias: los productos cablean su UI y su selección de sucursal sobre
  esta API sin haber tenido que cambiar su alta de turnos ni su modelo de
  autorización. El costo es una tabla más para auditar (`cierres_diarios_medios`,
  con tres niveles distinguidos por qué FK llevan puesta) y un caso adicional
  de "tabla que puede no existir todavía" que el motor tiene que tolerar en su
  camino más transitado.

## ADR-012 — El ticket de venta imprime las promociones aplicadas, opcional y sin doble conteo

- Estado: aceptada
- Fecha: 2026-09-29
- Contexto: VentaLibra aplica promociones («llevá N pagá M» y combos, ADR-014 de `libracommerce`) y suma su
  ahorro al `descuento` de la venta, anotando cuáles se aplicaron en `sale_promotions`. El ticket
  (`generar_ticket_venta`) sólo sabía imprimir un `Descuento` genérico, así que el cliente veía un monto sin
  saber de qué era. El generador es de este motor y lo comparten todos los productos.
- Decisión: `venta` acepta una clave opcional `promociones` (`[{nombre, veces, ahorro}]`). Se imprime una
  fila `Promo <nombre> [xN]` con su ahorro por cada una, antes del total. **El `descuento` de la venta ya las
  incluye**, así que con promociones la fila `Descuento` muestra sólo lo que quede (un descuento manual) y
  desaparece si no queda nada: el mismo ahorro no sale impreso dos veces.
- Consecuencias: sin la clave, o con una lista vacía o `None`, el papel sale byte a byte igual que hasta hoy
  (Contalibra y Restolibra no la mandan); lo fija un test. El producto arma la lista desde su propio registro
  de promociones: el motor no sabe de dónde sale.

## ADR-012 — El tema de la suite se guarda en cada instancia y se lee de ella, no del backoffice

- Estado: aceptada (decisión del humano, 2026-10-01); fase 2 de 4
- Fecha: 2026-10-01 (`v1.118.0`)
- Contexto: el humano pidió poder cambiar ciertos colores desde el backoffice de cada suite, para todas sus instancias. El backoffice es un
  plano de control: no abre las bases de las instancias y les habla por HTTP con el token de servicio.
- Decisión: cada instancia **guarda su tema** (`tema` en el `config.json`) y lo sirve en `GET /api/tema`, público. El backoffice lo
  **empuja** con `PUT /api/tema` (como ya hace con el correo y los usuarios), con el token de servicio: la guarda del producto tiene que aceptarlo. La SPA lee de su propia instancia al arrancar.
  Se descartó que las SPAs lean del backoffice: ataría cada instancia de cliente a que el control plane esté arriba.
- La validación es **sólo de forma** (ver `tema_router`): el catálogo de colores y el contraste viven en `libra-ui/tema`, que es la única
  lista. Copiarla a Python sería una segunda copia que se desactualiza sola.
- Consecuencias: una instancia caída o dada de alta después queda sin el tema hasta que el backoffice lo reintente (fase 3); una clave
  que el kit no conoce se guarda y la SPA la ignora, así agregar un color al kit no obliga a tocar este router.

## ADR-013 — Un booleano no es un número en los cuerpos de los routers: `sin_booleanos` y su guardia viven en el motor

- Estado: aceptada (regla del humano, 2026-10-03: el arreglo de fondo vive siempre en el motor)
- Fecha: 2026-10-04 (propuesta: `v1.125.0`)
- Contexto: pydantic, en el modo laxo que usa FastAPI, convierte `true` en `1` y `false` en `0` en cualquier campo `int`/`float` **antes** de que el servicio lo valide: `{"monto": true}` entraba como un
  pago de 1 peso y `{"caja_id": true}` como la caja 1. Medido (2026-10-04, `tests/test_routers_booleanos.py` contra los routers de `develop`): esos cuerpos daban 200 y escribían. `libracommerce`
  lo cerró en su motor con `web/_validacion.sin_booleanos` (ADR-026/027) y le sumó una guardia reutilizable (`libracommerce.testing.campos_numericos_que_aceptan_booleano`, ADR-028); corrida sobre la
  app completa de VentaLibra, esa guardia encontró 23 campos **de este motor** que seguían aceptando el booleano, y los 23 mueven plata o ids (cajas, turnos, cuenta corriente, egresos, tesorería, ARCA).
- Decisión:
  - **El helper canónico vive acá:** `libracore/validacion.py::sin_booleanos(*campos)`, un `field_validator(mode="before")` que rechaza `bool` (suelto, dentro de una lista y dentro de un diccionario, claves y
    valores) con el mensaje «<campo> tiene que ser un número, no un booleano» (422). Mismo código y mismo mensaje que el de `libracommerce`, que pasará a reexportar éste en otro release. Un `0` numérico,
    un entero, un texto numérico, `None` y un `Decimal` pasan como antes.
  - **La guardia canónica también:** `libracore.testing.campos_numericos_que_aceptan_booleano(app, *, ignorar=frozenset())` (`libracore/testing/booleanos.py`, reexportada desde `libracore.testing`), con el
    contrato de la de `libracommerce` (recorre las rutas con `fastapi.routing.iter_route_contexts`, instancia el modelo real con `True`/`False` en cada hoja numérica y devuelve las que lo aceptan). Cada producto
    la corre sobre su `create_app()` completa y afirma `== []`.
  - **Se aplica sólo donde un 1 o un 0 cambian algo del negocio** (plata, ids, cantidades, porcentajes, puntos de venta). Los `bool` de verdad (`activo`, `auto_facturar`) y los `Decimal` (pydantic ya los
    rechaza) no lo necesitan. Un modelo que hereda (`CajaUpdatePayload` de `CajaPayload`) conserva el validador del padre.
- Relevamiento (guardia sobre una app con **todas** las factories de `libracore/`, con las opciones que suman rutas prendidas: la siembra de la bandeja de MercadoPago sólo existe en una demo y `/reabrir` del cierre
  diario sólo con `autorizar_reabrir`). 60 campos aceptaban el booleano y se arreglan todos (los 23 de la guardia de `libracommerce` y 37 más del relevamiento):

  | Router | Ruta | Campos | Arreglado |
  |---|---|---|---|
  | `caja_router` | `POST /api/caja` | `monto`, `caja_id`, `factura_id` | sí (no estaba entre los 23) |
  | `caja_router` | `POST /api/cajas`, `PUT /api/cajas/{cid}` | `punto_venta`, `sucursal_id` | sí |
  | `caja_router` | `POST /api/turnos/abrir` | `monto_inicial`, `caja_id` | sí |
  | `caja_router` | `POST /api/turnos/{tid}/cerrar` | `monto_declarado` | sí |
  | `caja_router` | `POST /api/cierre-diario/cerrar` | `sucursal_id` | sí |
  | `cuenta_corriente_router` | `POST /{cliente_id}/pagar` | `monto`, `caja_id`, `facturas[]` | sí |
  | `egresos_router` | `POST /api/egresos` | `proveedor_id`, `monto_neto`, `iva_pct` | sí |
  | `egresos_router` | `POST /api/egresos/{eid}/pagar` | `monto`, `caja_id` | sí |
  | `tesoreria_router` | `POST /cuentas`, `PUT /cuentas/{cid}` | `saldo_inicial` | sí |
  | `tesoreria_router` | `POST /cuentas/{cid}/movimiento` | `monto` | sí |
  | `tesoreria_router` | `POST /transferencia` | `cuenta_origen_id`, `cuenta_destino_id`, `monto` | sí |
  | `arca_router` | `PUT /config/arca` | `punto_venta` (con `ge=1`: `false` ya fallaba, pero por el rango; `true` pasaba como el punto de venta 1) | sí |
  | `comprobantes_router` | `POST /api/comprobantes-pendientes` | `cliente_id`, `items[].qty`, `items[].unit_price`, `items[].iva_rate` (`true` era una alícuota del 100%) | sí (nuevo) |
  | `comprobantes_router` | `POST /facturar-prefill`, `POST /marcar-facturado` | `ids[]`, `factura_id` | sí (nuevo) |
  | `remitos_router` | `POST /api/remitos` | `client_id`, `items[].qty` | sí (nuevo) |
  | `presupuestos_router` | `POST /api/presupuestos`, `PUT /{pres_id}` | `client_id`, `tax_rate`, `items[].qty`, `items[].unit_price` | sí (nuevo) |
  | `mp_bandeja_router` | `POST /sincronizar` | `dias` | sí (nuevo) |
  | `mp_bandeja_router` | `POST /demo/sembrar` (sólo en demos) | `items[].monto` | sí (nuevo) |
  | `facturas_router` | `POST /api/facturas`, `POST /api/facturas/borrador-pdf` | `tipo`, `concepto`, `punto_venta` (viajan al comprobante que se pide a ARCA: `tipo: true` pedía una Factura A), `client_id`, `tax_rate`, `items[].qty`, `items[].unit_price` | sí (nuevo) |
  | `facturas_router` | `POST /api/facturas/{factura_id}/cobrar` | `caja_id` | sí (nuevo) |

  Sin campos numéricos de entrada que acepten el booleano (medidos, no supuestos; query y path llegan como texto y pydantic no convierte «true» en número): `clientes_router`, `config_router` (empresa y respaldo),
  `consultar_cuit_router`, `dashboard_router`, `libros_iva_router`, `logs_router`, `mp_config_router`, `mp_webhook`, `recibos_router`, `reportes_router`, `resumen_router`, `smtp_router`, `tema_router`,
  `ventas_cobro_router`, `geografia`, `feriados`, `resguardo_enlace`. El backoffice (`libracore/admin`) recibe todo como `str` de formulario y no se mide.
- **Sin pendientes en los routers de libracore:** la guardia sobre las 37 factories da `[]` sin excepciones (`ignorar` vacío). `facturas_router` se arregló en un segundo paso, cuando la sesión que
  trabajaba en él (la guarda del CUIT, `v1.124.0`) terminó y se mergeó. `arca_wsfe` no declara modelos de entrada HTTP: no tiene campos que listar y no se tocó.
- Lo que la guardia no mide (heredado de la de `libracommerce`): un `Any`, una tupla de largo fijo, una dataclass, un cuerpo leído a mano con `request.json()` (hoy no hay ninguno en `libracore/`), y una ruta que
  el producto no monta. **Cuerpos sin tipar (barrido de todos los routers, 2026-10-04):** se buscaron `dict`, `list[dict]`, `Any`, `object`, `Json`, `Body(...)` sin modelo, `await request.json()` y `float()/int()/Decimal()` sobre el cuerpo.
  Un solo acierto, y es dinero: `CobroPayload.pagos` (`list[dict]`, `facturas_router.py`), donde `cobros.registrar_cobro_factura` hace `float(pago["monto"])` y `{"monto": true}` registraba un cobro de 1 peso. Se
  conserva el contrato (es `list[dict]` a propósito: la pantalla manda filas vacías con `""`, sin `monto` o con claves de más, y el motor las ignora; un modelo tipado con `monto: float` habría dado 422 a un `""`),
  así que el arreglo es un `field_validator("pagos", mode="before")` que usa el helper nuevo `libracore.validacion.rechazar_booleanos(valor, campos, donde)` (dict o lista de dicts) para rechazar un booleano en `monto`
  («pagos[].monto tiene que ser un número, no un booleano») y en `medio_id` («un texto»), **y** la misma defensa en el borde de `registrar_cobro_factura` (un `ValueError` antes de escribir nada), por si otro
  llamador pasa el dict directo. Un producto que reemplaza la escritura con su `registrar_cobro=` queda cubierto por el validador del modelo. Descartados por el barrido, sin cambio: `tema_router` (`dict[str, str]`,
  sólo colores), `mp_config_router`/`config_router` (modelos de `str` y `bool`; `mp_iva_rate` es `str` y pydantic no convierte un booleano en texto), `mp_webhook` (lee a mano el cuerpo de MercadoPago:
  `type` y `data.id` sólo se usan como texto, el monto sale de la API de MP, y un `id` booleano termina en una consulta que da 404; línea 148-155) y `FacturaPayload` con `extra="allow"` (`facturas_router.py:331`, `822`:
  los campos de más de cada producto viajan sin tocar al hook `al_emitir`; si el hook los convierte en número, el helper `rechazar_booleanos` es el que tiene que usar el producto). Como el barrido también es
  una medida, no una prueba, la guardia sigue sin ver lo que no está tipado: lo nuevo con `dict`/`Any` y un número adentro hay que revisarlo a mano.
- Consecuencias:
  - **Para los productos:** un cuerpo con `true`/`false` en uno de esos campos ahora da **422** (antes 200 con un 1 o un 0). Un cliente que mande el número —o el texto numérico— no nota nada. Al subir el pin,
    cada producto corre su suite: si alguna fixture manda un booleano por accidente, se pone roja ahí y se corrige el cuerpo, no el helper. Los productos con routers propios que heredan estos payloads
    conservan el validador; los que suman un campo numérico nuevo sin `sin_booleanos` lo ven con `assert campos_numericos_que_aceptan_booleano(app) == []`, que conviene agregar a su suite.
  - **Para `libracommerce`:** su `web/_validacion.sin_booleanos` y su `testing.campos_numericos_que_aceptan_booleano` pasan a reexportar las de acá en un release aparte (hoy son copias fieles: mismo código, mismo mensaje).
  - `libracore.testing` ahora importa FastAPI y pydantic, que ya eran dependencias del motor (no hay extra nuevo).

## ADR-014 — La nota de crédito es una sola, del motor, y los productos aportan costuras

- Estado: aceptada
- Fecha: 2026-10-04 (decisión del humano)
- Contexto: el motor ya emitía notas de crédito para Contalibra, Restolibra y LibraClub (`facturas_router`), pero
  la lógica estaba atada a su tabla `facturas`, no tenía guardas (una factura se podía acreditar dos veces: ARCA lo
  acepta, y la cuenta corriente quedaba en −total) y LibraCargo, VentaLibra, GestioLibra y MedLibra no tenían nada.
  La alternativa era que cada producto escribiera la suya.
- Decisión: **una sola implementación en el motor**, `libracore.notas_de_credito`, igual para todos. El motor pone
  el tipo de nota, las guardas, el armado de la nota y el orden de las operaciones; el producto aporta como
  **costuras** (callables) lo que depende de su modelo: cargar las notas previas, numerar, registrar, pedir el CAE y
  lo suyo después. El router del motor es sólo el primer consumidor. Si a un producto le falta una costura, se
  agrega al motor.
- Consecuencias: una guarda o un arreglo llega a los siete productos con un bump de pin; hay un solo lugar donde
  mirar cuando una nota sale mal; y un producto que adopte la nota no puede divergir en las reglas. Costo: el
  núcleo no puede conocer ningún modelo de producto, así que habla en diccionarios y en callables.


## ADR-015 — El webhook de MercadoPago no da 500 ante un cuerpo raro, y una guardia informativa lista los cuerpos sin tipar

- Estado: aceptada
- Fecha: 2026-10-04 (propuesta: `v1.127.0`; sin cambio de esquema ni migración)
- Contexto: dos hallazgos del cierre de ADR-013, una misma familia (un cuerpo que el tipo no restringe).
  1. `mp_webhook._procesar` hacía `json.loads(body)` y después `payload.get(...)` y `payload.get("data", {}).get("id", "")`. El endpoint es **público** (lo llama MercadoPago, pero también cualquiera con
     la URL) y no sabe qué forma tiene lo que le llega. Medido el 2026-10-04 con un test de HTTP contra el router real (`TestClient(raise_server_exceptions=False)`): un JSON que no es un objeto (`[]`,
     `[1]`, `1`, `true`, `null`, `"x"`) y un `data` que no es un dict (`null`, `[]`, `"x"`, `5`) daban **500** (`AttributeError` sin atrapar), y MercadoPago reintenta ante un 500 (la regla 3 del módulo
     ya decía que un error propio no puede ser un 500). Además dos ids pasaban por `str()` y seguían de largo como si fueran un id: `{"id": true}` (llegaba a la API de MercadoPago como `"True"`) y
     `{"id": {"a": 1}}` (como `"{'a': 1}"`).
  2. `campos_numericos_que_aceptan_booleano` sólo ve campos **tipados**: un `dict`, un `list[dict]`, un `Any`, un modelo con `extra="allow"` o un endpoint que lee `request.json()` a mano reciben
     cualquier cosa, con números adentro que nadie valida. Así quedó `CobroPayload.pagos` (`{"pagos": [{"monto": true}]}` era un cobro de 1 peso), que se encontró leyendo el código.
- Decisión:
  - **`mp_webhook`**: un JSON que no es un objeto da 400 `{"ok": false, "error": "invalid json"}` (lo mismo que un JSON roto: el reintento tampoco lo va a poder leer). Un `type` que no es `"payment"`
    (incluido un no-string, `["payment"]`) da 200 `ignored`, **como hoy**. Un `data` que no es un dict, o sin un `id` utilizable, da 400 `{"ok": false, "error": "no payment id"}` (lo que ya daba un
    id vacío o ausente). Un id utilizable es un `int`, un `float` o un `str` no vacío, pasado por `str()` como siempre (MercadoPago manda un número); un `bool` **no** es un id, ni un dict ni una lista.
    El id se valida **antes** de usarlo en la firma: un id inválido ni siquiera llega a `verificar_firma`. La firma, la idempotencia, el 200 «not configured» y el resto del flujo no cambian: **los cuerpos
    válidos y los códigos que MercadoPago recibía no se tocan**; sólo cambian los que hoy daban 500 (ahora 400) y los dos ids basura (ahora 400 en vez de 200 o de un pedido a la API).
  - **`libracore.testing.cuerpos_sin_tipar(app, *, ignorar=frozenset()) -> list[tuple[str, str, str]]`** (`libracore/testing/sin_tipar.py`, reexportada desde `libracore.testing`), con el mismo
    recorrido que la guardia de booleanos, que se extrajo a `libracore/testing/_recorrido.py` (rutas con `iter_route_contexts`, `Mount`, `Depends`, `ignorar`; **el comportamiento de la guardia de
    booleanos no cambia**: sus 14 tests siguen igual). Devuelve `(método y ruta, campo, tipo)`.
    - **Ve:** un campo de entrada (cuerpo, query, formulario, path, header, cookie) de tipo `dict`/`Mapping`, `list`/`set`/`tuple` sin tipo de elemento, `Any`, `object`, `JsonValue` o que los contiene
      (`list[dict]`, `dict[str, Any]`, `X | None`); un modelo con `extra="allow"` (el nombre del tipo informado lo dice: `FacturaPayload(extra="allow")`), bajando por los modelos anidados; y un endpoint
      `POST`/`PUT`/`PATCH`/`DELETE` que declara `Request` él mismo y no tiene ningún cuerpo tipado (candidato a leer `request.json()` a mano), con el tipo `request-sin-cuerpo-tipado`.
    - **No ve:** qué hace el endpoint con lo que recibe (sólo mira tipos: un `request.json()` o `request.body()` en un endpoint que además tiene un cuerpo tipado no aparece); una dependencia que recibe
      `Request`; un `GET` con `Request`; un texto que después se parsea como JSON, `bytes`, `UploadFile`, `dataclass` y `TypedDict` (tienen forma); un validador `mode="before"` que acepta cualquier cosa; un
      endpoint que no es de FastAPI. `dict[str, int]` **no** se informa (sus valores tienen tipo y los mide la guardia de booleanos).
    - **Es informativa, no un `assert == []`**: da falsos positivos a propósito (un `dict[str, Any]` puede ser el contrato real, un `Request` puede servir sólo para armar una URL). Se usa fijando el conjunto
      conocido en un test con un comentario por entrada que diga por qué es aceptable (como hace `tests/test_guardia_sin_tipar.py`), o con `ignorar` y un comentario por excepción: un cuerpo sin tipar
      **nuevo** rompe el test y obliga a mirarlo.
- Resultado sobre la app con todas las factories de `libracore` (5 entradas, todas fijadas con su porqué en el test):

  | Ruta | Campo | Tipo | Por qué se acepta |
  |---|---|---|---|
  | `POST /api/facturas`, `POST /api/facturas/borrador-pdf` | `payload` | `FacturaPayload(extra="allow")` | `extra="allow"` a propósito: cada producto suma campos propios; los que usa el motor están tipados y con `sin_booleanos` |
  | `POST /api/facturas/{factura_id}/cobrar` | `pagos` | `list[dict]` | el hueco de ADR-013: validado con `rechazar_booleanos` y en `cobros.registrar_cobro_factura`; el tipo sigue libre |
  | `POST /webhooks/mercadopago` | `request` | `request-sin-cuerpo-tipado` | lo lee a mano a propósito; tolera cualquier forma (este ADR) y no cree el contenido: el estado sale de la API |
  | `POST /api/config/resguardo-externo/enlace/{proveedor}` | `request` | `request-sin-cuerpo-tipado` | falso positivo: usa `Request` sólo para armar la URL de retorno del OAuth |

  No apareció ningún cuerpo sin tipar nuevo: el barrido manual de ADR-013 había encontrado el único real (`CobroPayload.pagos`).
- Consecuencias: el webhook deja de poder dar 500 por la forma del cuerpo; un producto que monta su propio router puede correr la guardia sobre su `create_app()` y fijar lo que acepta. Costo: la guardia no
  entiende lo que un endpoint hace con el cuerpo, así que el comentario de cada entrada lo escribe una persona. `libracommerce` puede reexportarla igual que la de booleanos, en un release aparte.

## ADR-016 — La nota de crédito marca su abono en la cuenta corriente

**Estado:** aceptada (2026-10-04). **Contexto:** anular una venta cuya factura tiene CAE deja la factura vigente en ARCA; la salida es la nota de crédito del motor (ADR-014). Pero la nota (de una factura a cuenta corriente) y `anular_venta` de libracommerce **acreditan los dos la misma deuda** y ninguna sabe que la otra ya lo hizo: el saldo queda en −total (relevamiento del 2026-10-04; ningún test lo cubría). Se decidió (opción A del diseño `nota-de-credito-en-ventas-diseno` del wiki) que anular una venta con factura CAE **exija la nota antes**, y que la anulación no acredite de nuevo.

- Decisión 1 — **el abono de la nota lleva una marca**: `cc_pagos.referencia = "nc:factura:<id>"`, función única `referencia_cc_de_nota`. El texto del concepto y el importe no cambian.
- Decisión 2 — **la pregunta «¿ya abonó la nota?» es del motor**: `cc_acreditada_por_nota(conn, factura_id)`. Los productos y libracommerce no reescriben la regla ni parsean el concepto.
- Lo que **no** resuelve: una venta anulada *antes* de este cambio y con factura CAE a la que después se le emita la nota (acreditaría dos veces); no se midió que exista alguna. Tampoco la nota parcial.
- Consecuencias: sin migración. El abono de notas ya emitidas queda sin marca (referencia vacía), así que `cc_acreditada_por_nota` da `False` para ellas.

## ADR-017 — `build_nota_de_credito_router`: la nota de crédito se puede montar sola

**Estado:** aceptada (2026-10-04). **Contexto:** con `anular_venta` exigiendo la nota antes (ADR-032 de libracommerce), un producto con ventas facturadas necesita un camino para emitirla. `build_comprobantes_router` son doce endpoints (alta manual, borrador, cobro, email, borrado…); VentaLibra no tiene esas pantallas y lleva la caja por turno (el cobro por defecto entraría sin `turno_id`). Montarlo entero exponía superficie sin uso.

- Decisión 1 — **la ruta de la nota se extrae a `_registrar_nota_de_credito(router, …)`** y la usan los dos factories. Hay una sola implementación; el router completo conserva exactamente sus rutas y su orden.
- Decisión 2 — **`build_nota_de_credito_router`** monta sólo `POST {prefix}/{factura_id}/nota-credito` con el gate `solo_admin`. La nota no toca la caja, así que no depende del turno.
- Lo que **no** hace: no ofrece listado ni detalle de facturas (el producto que lo monte tiene que sacar el `factura_id` de su venta).

## ADR-018 — La nota de crédito parcial y el tope acumulado son del motor

**Estado:** aceptada (2026-10-04). **Contexto:** la fase 1 (ADR-014) sólo emite la nota **total**: sirve para «me equivoqué, rehago», no para una diferencia de kilos, un descuento o una bonificación. ARCA no lleva el saldo de una factura común (acepta de más y dos veces: medido el 2026-10-03), así que el tope es nuestro.

- Decisión 1 — **`importe` es el monto a acreditar, con IVA incluido**, de hasta dos decimales y mayor que cero. Sin `importe`, nota total (nada cambia para quien ya la usa).
- Decisión 2 — **tope acumulado:** lo acreditado por las notas **con CAE** más el importe no puede superar el total de la factura (`SUPERA_SALDO`, 409). La nota total sólo se admite sobre una factura sin notas; con parciales, se pide una nota por el saldo (el mensaje dice cuánto).
- Decisión 3 — **el reparto es del motor:** el neto sale del importe con la alícuota de la factura (el motor arma un solo bloque de IVA por comprobante) y el IVA es la resta. Medido en homologación: ARCA tolera el IVA a hasta 15 centavos del exacto. Una C no discrimina IVA.
- Decisión 4 — **la nota parcial acredita un monto, no unas líneas:** un solo ítem que dice qué acredita; no copia los del original.
- Decisión 5 — **FCE:** sólo por menos que el saldo (medido: una nota sin anulación sobre el saldo entero da `10184`; revertirla entera exige que el comprador la rechace).
- Decisión 6 — **cuenta corriente por nota:** el abono es el de cada nota y la marca `nc:factura:<id>:<nota_id>` (la de la fase 1 sigue valiendo). El motivo sigue sin guardarse en el motor.
- Compatibilidad: una nota previa que no informa `total` cuenta como la factura entera (el lado seguro, igual que la fase 1).
- Lo que **no** resuelve: los totales y conciliaciones de cada producto con notas parciales (LibraCargo: ver el diseño en el wiki), la pantalla para elegir el importe y `anular_venta` con varias notas (libracommerce).

## ADR-019 — La FCE completa: el motor consulta el registro de ARCA (WSFECRED)

**Estado:** propuesta (2026-10-05), sin código. **Contexto:** el motor emite la FCE y su nota parcial por WSFE, pero no
habla con el registro de FCE (WSFECRED). Medido en homologación (2026-10-05): ARCA lleva una cuenta corriente con el
**saldo** de cada FCE (igual al `saldo_acreditable` del motor tras una nota parcial); **WSFE autoriza una FCE a un
receptor no obligado**; y la anulación total (`S`) exige que el **comprador** la rechace, cosa que sólo se ve en WSFECRED.
Detalle en `docs/fce.md`.

- Decisión 1 — ✅ *(hecho, v1.132.0 propuesta)* **`libracore.arca_wsfecred`, sólo consultas** (`monto_obligado`, `estado_de_fce`, `historial`), con el
  mismo WSAA (servicio `wsfecred`) y la misma configuración de ARCA. Aceptar y rechazar son del comprador: no se
  implementan.
- Decisión 2 — ✅ *(hecho: `corresponde_fce` y `GET /api/facturas/fce/corresponde`)* **al emitir, sugerir FCE** cuando `consultarMontoObligadoRecepcion` diga que el receptor está obligado y
  el total llegue a `montoDesde`. Sugerencia y no bloqueo, salvo que se mida que WSFE rechaza la factura común.
- Decisión 3 — **la nota total de una FCE sólo con `Rechazado`** en ARCA: entonces sale con anulación `S`. Si no, sigue
  la regla de v1.131.0. Pendiente de medir con un segundo certificado que haga de comprador.
- Decisión 4 — **el saldo de ARCA controla y no manda**: el tope sigue siendo el del motor; una diferencia se avisa.
- Decisión 5 — **WSFECRED caído o sin autorizar no frena la emisión**; la nota total de FCE queda frenada (lado seguro).
- Decisión 6 — **para toda la familia** por el pin; emitir FCE desde la venta (VentaLibra, `libracommerce`) es aparte.
- Fuera de alcance: recibir FCE como comprador, el agente de depósito colectivo, `informarCancelacionTotalFECred` y
  `obtenerRemitos`.

## ADR-020 — La SPA es del motor, y una ruta de la API que no existe da 404

**Contexto.** Seis productos (VentaLibra, Contalibra, Restolibra, Libradesk, GestioLibra y MedLibra) tenían el mismo `app/spa.py`, **byte a byte** (md5 igual, 2026-10-05): el catch-all que sirve el frontend, sus cabeceras de caché (`index.html` revalida siempre, los assets con hash se cachean para siempre) y `archivo_publico` (que el manifest y el service worker no contesten HTML). Ese catch-all contestaba **`index.html` con 200 a cualquier ruta, también a `/api/loquesea`**: un endpoint mal escrito o que todavía no existe en esa versión devolvía HTML con 200. En el wiki del ecosistema, los deploys de VentaLibra del 2026-10-04/05 reportaron «`/api/health` 200» durante dos días y era el `index.html`; el chequeo real es `/health`.

**Decisión.** `libracore.spa`: el mismo módulo (con sus tests), más una guarda: si el primer tramo de la ruta es un prefijo de la API (`PREFIJOS_API = ("api",)`, ampliable con `montar_spa(app, dist, prefijos_api=...)`), el catch-all contesta **404 `{"detail": "Not Found"}` en JSON**, la misma forma que el 404 de FastAPI. Las rutas de la API que existen no cambian: se montan antes que el catch-all y se resuelven antes. `/apis`, `/api-docs` o `/configuracion/api` siguen siendo de la SPA (sólo cuenta el primer tramo exacto).

**Adopción.** Cada producto reemplaza su `app/spa.py` por un re-export de `libracore.spa` cuando sube el pin; el test que verifica que su `app/asgi.py` llama a `montar_spa` queda en el producto. VentaLibra primero (pedido del humano: «arreglá que las rutas /api devuelvan 404»); los otros cinco, cuando el humano lo pida.

**Límites.** Las rutas propias de un producto fuera de `/api` (`/auth/...`, `/settings/...`, `/logs` en algunos) siguen cayendo en la SPA si no existen, salvo que el producto sume su prefijo. Sin migración.

## ADR-021 — El emisor de cada comprobante es opcional y vive en `facturas`

**Contexto.** El motor suponía un emisor por instancia: diez lugares tomaban `configs[0]` y `facturas` no decía con qué configuración de ARCA se emitió cada comprobante. LibraCargo factura con varias razones sociales (Suitrans: la agencia y el transporte) y por eso tenía su propio modelo de comprobantes, con su numeración, sus notas y su anulación. El humano pidió un solo modelo (2026-10-05): «que haya cosas que use libracargo y que los demás no usen, no quiero que sean modelos separados». El diseño completo está en el wiki del ecosistema (`wiki/analyses/libracargo-modelo-normalizado-diseno.md`); esta es su etapa 1, M1.

**Decisión.** `facturas.emisor_id`, FK **opcional** a `arca_config.id`. `NULL` es «el emisor único de la instancia».
- `arca_config.config_del_emisor(emisor_id=None)` es el único lugar que elige configuración. Sin emisor, la primera activa, como siempre. Con emisor, esa fila o `EmisorDesconocido`, nunca otra: caer a otra fila sería emitir con el CUIT equivocado.
- La numeración y la unicidad son por emisor **y por ambiente**, como en ARCA. El índice usa `COALESCE(emisor_id, 0)`, porque en un UNIQUE dos `NULL` no son iguales y los comprobantes de toda la familia quedarían sin unicidad.
- Una nota es siempre del emisor de su original. La búsqueda de notas y del original filtra por emisor y, cuando se tiene el original, por ambiente.

**Consecuencias.** Los productos de un solo emisor no cambian de comportamiento ni de datos. La migración no toca filas, sólo agrega la columna y cambia el índice por uno más laxo, que no puede fallar donde el viejo existía. La condición de IVA del emisor sigue saliendo de `config_manager`, que es una sola por instancia; si dos razones sociales tienen distinta condición, hará falta llevarla a `arca_config`; se verifica en Suitrans antes de la etapa de LibraCargo. Siguen, en etapas aparte del mismo diseño: la anulación con rastro y el registro manual con número tipeado (M3 y M6), emitir dentro de la transacción del producto (M2) y el dinero en `NUMERIC` (M4).

