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

**Contexto.** El motor suponía un emisor por instancia: diez lugares tomaban `configs[0]` y `facturas` no decía con qué configuración de ARCA se emitió cada comprobante. LibraCargo modela varias razones sociales por instancia (su demo ejercita dos; **Suitrans, su único cliente, tiene una sola: Suitrans**. Corrección del 2026-10-05: en el sistema viejo el dueño también facturaba a su nombre, y eso no se va a usar) y por eso tenía su propio modelo de comprobantes, con su numeración, sus notas y su anulación. El humano pidió un solo modelo (2026-10-05): «que haya cosas que use libracargo y que los demás no usen, no quiero que sean modelos separados». El diseño completo está en el wiki del ecosistema (`wiki/analyses/libracargo-modelo-normalizado-diseno.md`); esta es su etapa 1, M1.

**Decisión.** `facturas.emisor_id`, FK **opcional** a `arca_config.id`. `NULL` es «el emisor único de la instancia».
- `arca_config.config_del_emisor(emisor_id=None)` es el único lugar que elige configuración. Sin emisor, la primera activa, como siempre. Con emisor, esa fila o `EmisorDesconocido`, nunca otra: caer a otra fila sería emitir con el CUIT equivocado.
- La numeración y la unicidad son por emisor **y por ambiente**, como en ARCA. El índice usa `COALESCE(emisor_id, 0)`, porque en un UNIQUE dos `NULL` no son iguales y los comprobantes de toda la familia quedarían sin unicidad.
- Una nota es siempre del emisor de su original. La búsqueda de notas y del original filtra por emisor y, cuando se tiene el original, por ambiente.

**Consecuencias.** Los productos de un solo emisor no cambian de comportamiento ni de datos. La migración no toca filas, sólo agrega la columna y cambia el índice por uno más laxo, que no puede fallar donde el viejo existía. La condición de IVA del emisor sigue saliendo de `config_manager`, que es una sola por instancia; si dos razones sociales de una misma instancia tienen distinta condición (la demo de LibraCargo tiene una responsable inscripta y una monotributista), hará falta llevarla a `arca_config`. **Verificado el 2026-10-05: Suitrans tiene una sola razón social, responsable inscripta**, así que hoy no hace falta. Siguen, en etapas aparte del mismo diseño: la anulación con rastro y el registro manual con número tipeado (M3 y M6), emitir dentro de la transacción del producto (M2) y el dinero en `NUMERIC` (M4).

## ADR-022 — Un comprobante sin CAE se puede anular con rastro, además de borrarse

**Contexto.** Hasta acá, un comprobante sin CAE sólo se podía borrar (`DELETE`): su número desaparecía y no quedaba constancia de que existió. LibraCargo registra comprobantes a mano (su ADR-024) y los **anula** dejando el rastro y la contrapartida en la cuenta corriente. Para que use la tabla del motor necesita lo mismo (diseño `libracargo-modelo-normalizado-diseno`, M3). El humano decidió el 2026-10-05 que la anulación con rastro esté **en el motor, para todos, y sea opcional**.

**Decisión.**
- `facturas.anulada_en` (`NULL` quiere decir vigente), `anulada_por` y `anulacion_motivo`.
- `db.facturas.anular_factura` y `POST /api/facturas/{id}/anular`.
- **Sólo sin CAE**: con CAE corresponde una nota de crédito.
- **Sólo sin cobros**: esa plata entró, y primero se anulan los cobros.
- El débito de cuenta corriente que generó el comprobante se anula en la misma transacción.
- Un anulado sale de todo lo que suma o cuenta (`sql_vigente`, y dentro de `SOLO_FISCALES`), pero sigue en los listados.
- Un anulado no se autoriza, no se cobra y no admite notas. Su número no se reusa.

**Consecuencias.**
- El `DELETE` sigue igual: cada producto elige.
- Una consulta nueva que sume comprobantes tiene que usar `sql_vigente` o `sql_solo_fiscales`. Es la misma disciplina que `sql_no_anulado` en la caja.
- La cuenta corriente propia de un producto (LibraCargo) no la toca el motor: ese producto asienta su contrapartida, como hoy.

## ADR-023 — Un comprobante registrado a mano conserva su número

**Contexto.** LibraCargo registra a mano el número de los comprobantes que una razón social sin ARCA emitió en otro lado (su ADR-024). El motor sólo tenía `create_factura`: si el número ya existe, reintenta con el siguiente. Eso está bien cuando el motor calcula el número, pero con un número tipeado **registra un comprobante que no existe**. Es el M6 del diseño `libracargo-modelo-normalizado-diseno`.

**Decisión.** `db.facturas.registrar_comprobante`. El número es el dato:
- Si ya existe para el emisor (ADR-021), levanta `NumeroYaRegistrado`, también si otro lo registró entre la consulta y el `INSERT`.
- Cualquier otra violación de integridad sale tal cual.
- Siempre va como `produccion`.
- No hay endpoint: lo llama el producto desde su propio flujo, que valida y asienta lo suyo.

**Consecuencias.** No cambia el esquema ni `create_factura`. Un producto que quiera exponer el registro manual en su pantalla lo hace con su propio router.

## ADR-024 — El dinero del motor se guarda exacto en PostgreSQL; se lee como siempre

**Contexto.** Las columnas de dinero del motor eran `REAL`, que en PostgreSQL es `DOUBLE PRECISION`: una suma en la base arrastra el error de punto flotante (`0.1 + 0.2 = 0.30000000000000004`). LibraCargo guarda su dinero en `NUMERIC` y `Decimal`, y para usar la tabla `facturas` del motor no puede perder eso. El humano decidió el 2026-10-05 «NUMERIC para todos», con el alcance **guardar exacto** y no Decimal de punta a punta (diseño `libracargo-modelo-normalizado-diseno`, M4).

**Decisión.**
- En PostgreSQL, las 33 columnas de `COLUMNAS_DE_DINERO` pasan a `NUMERIC` sin escala fija (`_dinero_exacto_en_postgres`, en `init_core_schema` y la migración `0018`). No se redondea nada de lo que había.
- Quedan afuera las que no son plata: alícuotas y cantidades.
- Sólo se convierte una columna que es `double precision` o `real`: una instancia con otro tipo se respeta.
- **La lectura no cambia**: `_postgres._como_en_sqlite` sigue devolviendo `float`.
- En SQLite no cambia nada.

**Consecuencias.**
- Las sumas y comparaciones en la base son exactas.
- Lo que se escribe desde `float` guarda la expansión decimal de ese `float`, igual que antes. Lo que se escribe desde `Decimal` (LibraCargo) se guarda exacto.
- Pasar la aritmética de la familia a `Decimal` sigue siendo una decisión aparte, de los dos motores, como dice el docstring del adaptador.

## ADR-025 — Un producto puede emitir el comprobante dentro de su propia transacción

**Contexto.** Cada función del comprobante abría y confirmaba su propia conexión. LibraCargo emite el comprobante, pide el CAE, cierra sus órdenes de carga y asienta su cuenta corriente **en una sola transacción**, todo o nada (su ADR-024). Para usar la tabla del motor necesita que el motor escriba dentro de esa transacción. El diseño `libracargo-modelo-normalizado-diseno` (M2, salida A: una sola base) lo pide.

**Decisión.**
- Se usa el idioma que ya tiene `libracore.db` (`ventas`, `caja`, `stock`, `turnos`...): las funciones del comprobante aceptan `conn=`. Con `conn` trabajan en la transacción de quien llama y no confirman; sin `conn`, cada una confirma la suya, como siempre.
- Lo cubre el camino entero: numerar, crear o registrar, pedir el CAE, anular y buscar las notas.
- No es un mecanismo nuevo, como una transacción ambiente por `ContextVar`: sería implícito, y alcanzaría código que no fue escrito para eso.
- 🔴 El reintento ante un número repetido va en un `SAVEPOINT`, porque en PostgreSQL un error aborta la transacción entera.
- En SQLite se abre la transacción antes del savepoint: un `SAVEPOINT` fuera de una transacción abre una propia y su `RELEASE` la confirmaría.

**Consecuencias.**
- La transacción queda abierta mientras dura la llamada a ARCA. Es lo que LibraCargo ya hace hoy, y es el precio de que un rechazo no deje nada escrito.
- El router de comprobantes del motor no cambia.

## ADR-026 — Un libro de cuenta corriente de terceros, opcional, aparte del saldo de clientes

**Contexto.** La cuenta corriente del motor es **de clientes** y su saldo **se calcula** desde los documentos: ventas, facturas por CUIT, débitos y pagos (`db/cuenta_corriente.py`). Siete productos la usan así. LibraCargo lleva otra cosa: un **libro de asientos** de debe y haber por (tercero, rol), con clientes, fleteros y proveedores. Tiene gastos de dos patas (el proveedor al debe y el fletero al haber en la misma transacción) y asientos que se corrigen al editar el documento que los originó. Desde el 2026-10-06 el comprobante de LibraCargo es del motor (su ADR-030), y el humano pidió que tampoco su cuenta corriente sea un modelo separado. Eligió la opción A del diseño `cuenta-corriente-de-terceros-diseno` del wiki: el libro va en el motor, opcional, y los otros siete productos no cambian.

**Decisión.**
- Tabla `cc_asientos`: `fecha`, `tercero_id` y `rol`, `concepto`, `descripcion`, `debe` y `haber`, `factura_id` (FK a `facturas`), `contrapartida_de` (FK a sí misma), `origen_legado` y `usuario_id`.
- **El tercero y el rol son del producto**: `tercero_id` no tiene FK y `rol` es texto. El motor no sabe qué es un fletero. Un producto con su tabla de terceros pone la FK en su propia tabla de vínculo.
- La base sostiene que no haya signos negativos y que un asiento mueva una sola columna, salvo lo del legado.
- **Corregir no es anular.**
  - `corregir` cambia un asiento en el lugar, también de cuenta (tercero o rol). Es lo que hace un producto al editar el documento. No deja cambiar el origen en el legado ni de qué asiento es contrapartida, porque eso es otro asiento.
  - `borrar` saca un asiento cuando el documento editado deja de mover la cuenta (un cobro sin tercero, una comisión en cero), pero no uno que tenga contrapartida.
  - `contraasentar` agrega el asiento inverso. Por defecto lleva la fecha del original, y un asiento se revierte una sola vez.
- `saldo`, `extracto` (con saldo anterior y corrido) y `saldos`. Todas las funciones aceptan `conn=` (ADR-025).
- El dinero entra en `COLUMNAS_DE_DINERO` (ADR-024).

**Consecuencias.**
- Conviven dos modelos de cuenta corriente en el motor: el de clientes calculado, para quien lo usa, y el libro, para quien lo pida. Pasar el de clientes al libro es otra decisión (la opción B del diseño).
- Una factura referenciada por un asiento no se puede borrar (`ON DELETE RESTRICT`). Sólo pasa en un producto que use el libro, y el comprobante con CAE ya no se borra.


## ADR-027 — La cuenta corriente de clientes también como libro, en sombra

**Contexto.** El saldo de clientes se calcula en cada lectura desde ventas fiadas, facturas cobradas a cuenta (por CUIT), débitos y pagos, y eso hace que se mueva hacia atrás: un pago borrado, un movimiento anulado o un CUIT cambiado alteran saldos de meses anteriores sin dejar rastro. El 2026-10-06 el humano eligió la opción B del diseño `cuenta-corriente-de-terceros-diseno` del wiki: pasar la cuenta de clientes de los siete productos al libro `cc_asientos` (ADR-026). Decidió además dos cosas: primero un **modo sombra**, que escribe en el libro y compara pero sigue leyendo el cálculo; y que **el cliente de una deuda se fije la primera vez** que se asienta (diseño `libro-de-terceros-para-la-familia-diseno`).

**Decisión.**
- `cc_asientos.origen` (`tabla:id`) dice de qué hecho viene cada asiento. No es único: el original y su reversión lo comparten.
- `libro_de_clientes.sincronizar(origen)` es lo único que escribe. Compara si el hecho cuenta hoy, con el mismo criterio que `get_cc_saldo`, contra su asiento vigente, y asienta, revierte (`contraasentar`, con la fecha del original) o revierte y vuelve a asentar. Es idempotente.
- Los escritores del motor la llaman en su misma transacción. No hay un gancho por escritor con su propia lógica: hay una sola función que mira el estado. Así, un escritor nuevo o uno con SQL propio sólo tiene que decir qué hecho tocó.
- El rol es `cliente` y `tercero_id = clients.id`. Las lecturas del libro sólo miran asientos con `origen`, porque LibraCargo lleva su cuenta de clientes en la misma tabla, sin origen y sobre sus propios terceros.
- Un CUIT de factura que es de dos clientes no se asienta a ninguno (el cálculo se lo suma a los dos).
- El asiento de una factura no lleva `factura_id`. Su FK es `ON DELETE RESTRICT`, y `delete_factura` dejaría de funcionar en los productos que hoy la usan. El vínculo está en el origen (`caja_mov:<id>`).
- Las ventas fiadas se resuelven con el `OrigenVentas` que registra el producto. `build_cuenta_corriente_router` lo registra solo.

**Consecuencias.**
- Mientras dure la sombra, `comparar()` debería dar vacío. Lo que muestre es un escritor que no avisa (se arregla) o un cambio de CUIT (es la semántica nueva, y se explica).
- Cuando dé vacío en todas las instancias, las lecturas pasan al libro (etapa B3) y el cálculo se retira (B4).


## ADR-028 — Las lecturas de la cuenta de clientes pasan al libro, detrás de un interruptor

**Contexto.** El libro de clientes (ADR-027) lleva en sombra la cuenta de los siete productos y en dev, demo y clientes da cero diferencias contra el cálculo (`comparar()` vacío). Es la etapa B3 de la opción B: las lecturas pasan al libro. Se hace por instancia y reversible, para poder volver al cálculo sin desplegar.

**Decisión.**
- **Interruptor por instancia**: la variable de entorno `LIBRACORE_CC_DESDE_EL_LIBRO` (valores verdaderos `1`, `true`, `si`, `sí`, sin distinguir mayúsculas). `libro_de_clientes.lee_del_libro()` la lee. Apagada, que es el default, todo se lee calculado, como hasta v1.137.
- Una base sin `cc_asientos` lee calculado aunque esté encendida (LibraDesk arma a mano las tablas del motor que usa).
- Encendida, salen del libro `get_cc_saldo`, `get_cc_movimientos`, `get_cc_movimientos_periodo` (que se resuelve sobre los movimientos, como siempre) y `get_clientes_con_saldo_cc`, con **la misma forma**: las mismas claves, los mismos tipos, el mismo criterio de qué clientes aparecen y el mismo orden (por fecha, y a igual fecha ventas, facturas, débitos y pagos; los clientes por saldo descendente y nombre).
- Un movimiento sale de su **asiento vigente**: la fecha, el signo y el monto son los del asiento. El `origen` (`cc_pago:12`) dice qué fila leer para completar el concepto, la referencia, el medio, el usuario y los ids que usa la pantalla (`cc_pago_id`, `cc_debito_id`, `venta_id`, `factura_id`).
- **Un hecho revertido no se muestra**: ni el asiento original ni su contrapartida entran en la lista de movimientos. Así la lista coincide con la del cálculo, que no ve lo borrado ni lo anulado. El saldo no necesita la regla: el original y su reversión suman cero. La reversión sigue en el libro para reconstruir la cuenta como estaba.
- El saldo del libro se redondea al centavo; el cálculo lo devolvía con el error del flotante.
- **`get_facturas_pendientes_cc` no cambia**: sigue por factura (ADR-018), no es un saldo.
- `libro_de_clientes.comparar()` sigue midiendo contra el cálculo (`get_cc_saldo_calculado`), con el interruptor en el valor que tenga.

**Consecuencias.**
- Mientras el cálculo exista, encender o apagar una instancia no pide migración ni despliegue de código: se cambia la variable y se reinicia.
- Quien escriba las tablas de la cuenta con SQL propio sin llamar a `sincronizar` deja de verse con el interruptor encendido hasta que corra `reconstruir()`: es lo que la sombra ya mostraba en `comparar()`.
- Lo que el cálculo ve y el libro no (un hecho de importe cero, un CUIT de dos clientes, un CUIT que cambió después de asentar la deuda: ADR-027) es la semántica nueva, y `comparar()` lo mostraba.
- La etapa B4 retira el cálculo (`*_calculado`) y este interruptor: el libro pasa a ser la única lectura.


## ADR-029 — El libro es la única lectura de la cuenta de clientes

**Contexto.** La etapa B3 (ADR-028) dejó las cuatro lecturas de la cuenta de clientes detrás de `LIBRACORE_CC_DESDE_EL_LIBRO`, con el cálculo como respaldo. En todas las instancias el libro da cero diferencias contra el cálculo y el interruptor se enciende en producción. Es la etapa B4 de la opción B: ya no hay contra qué comparar ni a qué volver.

**Decisión.**
- `get_cc_saldo`, `get_cc_movimientos`, `get_cc_movimientos_periodo` y `get_clientes_con_saldo_cc` leen **siempre** del libro `cc_asientos` (`libro_de_clientes.saldo_de`, `movimientos_de` y `clientes_con_saldo`).
- Se retiran el interruptor (`lee_del_libro`, `VARIABLE_LECTURA` y la variable `LIBRACORE_CC_DESDE_EL_LIBRO`, que pasa a no hacer nada), el cálculo (`get_cc_saldo_calculado`, `get_cc_movimientos_calculados`, `get_clientes_con_saldo_calculado` y las consultas de las cuatro patas) y `libro_de_clientes.comparar` (ya no hay contra qué).
- Quedan `sincronizar`, `sincronizar_cliente`, `reconstruir`, `saldos_del_libro`, `registrar_origen_de_ventas` y las lecturas. `get_facturas_pendientes_cc` no cambia: sigue por factura (ADR-018).
- Las funciones públicas siguen aceptando `origen: OrigenVentas = VENTAS_LIBRACORE`, porque los productos lo pasan. **Ya no decide el saldo**: el libro lo usa para completar el número de cada venta fiada en los movimientos (`Venta #POS-7`; si la tabla de ventas del origen no está, sale con el concepto del asiento, `Venta POS-7`). `OrigenVentas` y sus constantes se quedan: el libro las usa para asentar las ventas fiadas.
- **Una base sin `cc_asientos` no tiene de dónde leer**: las cuatro lecturas tiran `RuntimeError`, con un mensaje que dice que falta la tabla del libro y que corra la migración `0020` (o `init_core_schema`). Leer de un libro que no existe daría saldos en cero, en silencio. `sincronizar` y `reconstruir` siguen sin hacer nada sin la tabla: un pago o un débito no pueden fallar por eso (ADR-027, v1.137.1).
- **El tipo y el signo de un movimiento salen de la fila del hecho, no de la columna del asiento.** `cc_debito:*`, `venta_pago:*` y `caja_mov:*` son débitos con el monto de su fila, y `cc_pago:*` es un crédito con el monto de su fila, con su signo. Es lo que mostraba el cálculo: LibraDesk registra la anulación de un remito como un `cc_debito` de −18150, que `_hecho` asienta al haber, y la lista (y `total_debitos`/`total_creditos` del período) lo seguía mostrando como un débito de −18150, no como un crédito de 18150. El saldo da igual de las dos formas. **No cambia cómo se asienta**, sólo la lectura. Si la fila del hecho ya no existe, el movimiento sale como lo dice el asiento.
- **Los borrados físicos de pagos y débitos no se convierten en anulaciones.** Era una opción para dejar rastro, pero el libro ya lo guarda: `delete_cc_pago` y `delete_cc_debito` llaman a `sincronizar`, que revierte el asiento con la fecha del original. Decisión tomada el 2026-10-06.

**Consecuencias.**
- **Lo cargado por fuera de los escritores del motor no se ve hasta `reconstruir`.** Un `INSERT` propio en `cc_pagos`, `cc_debitos`, `ventas_pagos` o `caja_movimientos` no asienta nada; el saldo no lo suma y el movimiento no aparece. Es lo que hace un deploy (`libro_de_clientes.reconstruir(origen)`), y lo que tiene que hacer un test de un producto que carga con SQL crudo antes de leer el saldo. Un borrado propio, igual: el asiento sigue hasta que `reconstruir` lo revierta.
- El `origen` con que se asienta una venta fiada es el del producto y es uno por base: `reconstruir(origen)` con otro origen asienta (o revierte) otras ventas. Un cliente ya asentado no se mueve (ADR-027).
- Un producto que arma a mano las tablas del motor sin `cc_asientos` (LibraDesk) tiene que agregarla antes de subir a esta versión, o sus lecturas de la cuenta de clientes fallan.
- Lo que el cálculo veía y el libro no (un hecho de importe cero, un CUIT de dos clientes, un CUIT que cambió después de asentar la deuda) es la semántica del libro desde ADR-027.
- Se pierde la forma de volver al cálculo con una variable. Para volver hay que desplegar la versión anterior.

## ADR-030 — La pre factura es la bandeja de comprobantes pendientes con número interno, y se manda al cliente antes de facturar

**Contexto.** LibraCargo necesita que, antes de facturar por ARCA, el cliente pueda ver y confirmar lo que se le va a facturar: un documento **no fiscal**, con número propio, que se genera desde las órdenes, se manda en PDF, se puede editar mientras no esté facturado, se acepta y se anula (diseño `libracargo-pre-factura-diseno` del wiki del ecosistema, aprobado el 2026-10-06). Con el registro a mano fuera de la app, el número y el punto de venta del comprobante los pone sólo ARCA. El humano pidió que el fondo vaya en el motor, para cualquier producto: `comprobantes_pendientes` ya es «un comprobante por facturar» con el cliente como foto (sin depender de `clients`), los ítems, un total que sale de los ítems y el vínculo a la factura, y los presupuestos (que sí tienen ciclo, PDF y correo) cuelgan de `clients`, que LibraCargo no usa.

**Decisión.** La pre factura **es** una fila de `comprobantes_pendientes` con ocho columnas más (migración `0021_pre_factura`; todas nacen vacías):
- `numero_interno` (`PF-0001`): correlativo **por `(origen_producto, origen_instancia)`**, asignado por el motor al crear, único por un índice parcial (`WHERE numero_interno IS NOT NULL`). Un anulado conserva su número: no se reusa. Dos altas simultáneas que calculan el mismo reintentan en un `SAVEPOINT` (como `create_factura`).
- `emisor_id` (FK opcional a `arca_config`, ADR-021: `NULL` es el emisor único de la instancia), `tipo_comprobante` (1/6/11, 201/206/211) y `fecha_vencimiento_pago` (FCE).
- `enviado_at`, `enviado_a`, `aceptado_at`, `aceptado_por`: TEXT sin default, que escribe el módulo con la hora de Argentina.

**Estados.** Los de siempre (`pendiente`, `facturado`, `descartado`) más `enviado` y `aceptado`: `pendiente → enviado → aceptado → facturado`.
- **`facturado` y `descartado` son finales**: no se editan, envían, aceptan ni anulan. Es la regla de la bandeja (una resolución la tomó una persona).
- **Aceptar se puede desde `pendiente`**, no sólo desde `enviado`: el PDF se puede mandar por otro medio, y la conformidad la marca el operador (decisión del 2026-10-06).
- **Facturar y anular se pueden desde cualquiera de los tres estados abiertos.** Contalibra y LibraDesk siguen yendo de `pendiente` a `facturado`; un producto que exige la aceptación la pide con `exigir_aceptada=True` o mirando el estado.
- **Editar una `enviado` o `aceptado` la devuelve a `pendiente` y borra `enviado_*` y `aceptado_*`** si cambió algo: el cliente aceptó otros datos, y dejar la marca de aceptada sobre un contenido distinto afirmaría una conformidad que no existe. Un `editar` que no cambia nada no mueve el estado.
- Reenviar una `aceptado` la deja aceptada (sólo actualiza a quién y cuándo), y aceptar dos veces no pisa quién la aceptó.
- `db.comprobantes_pendientes.marcar_facturado` y `descartar` pasan a mover cualquier estado abierto (antes sólo `pendiente`) y aceptan `conn=` (ADR-025). `upsert_comprobante` sigue sin pisar nada que no sea `pendiente`. Se agrega el origen `pre_factura` a la lista cerrada de orígenes: sin `origen_id`, se usa el número interno.

**El PDF no es fiscal.** `pdf_generator.generate_pdf_pre_factura` tiene el aspecto de la factura (emisor, cliente, ítems, IVA, total) con el sello **«PRE FACTURA — NO VÁLIDA COMO COMPROBANTE FISCAL»** en el cuerpo de cada página y en el pie, el número interno en lugar del número fiscal y la letra `X`; **sin** punto de venta, CAE ni QR. Un comprobante clase C va con `iva_rate` 0 (se rechaza al crear o editar, como lo haría ARCA). La fecha del PDF es la del documento: reimprimir da los mismos bytes. El emisor sale de la configuración de la instancia, con el nombre y el CUIT de `arca_config` si hay `emisor_id`; un producto con varias razones sociales pasa los datos de la que factura (`emisor=` / `emisor_del_pdf`).

**El correo** usa `email_sender.enviar_documento` (que ahora acepta `pdf_bytes`, sin pasar el PDF por un archivo) y el SMTP que resuelve `facturas_router.smtp_efectivo`, igual que el envío de comprobantes y de presupuestos. Primero se manda y después se marca `enviado`: si el SMTP falla, la pre factura queda como estaba.

**El módulo y el router.** `libracore.pre_facturas` (todo con `conn=`, ADR-025) y `libracore.pre_facturas_router.build_pre_facturas_router`, con `origen_producto` y `origen_instancia` **fijados por el producto** y no por el cuerpo del request (definen la numeración y qué se ve; lo de otro origen da 404) y el gate de auth inyectado (`dependencies`, `usuario_actual`). El producto agrega lo suyo por ganchos —`al_crear`, `al_editar` y `al_anular`, que reciben `(conn, pre_factura, datos)` **en la misma transacción** que el cambio— y por las claves de más que aceptan `CrearPayload` y `EditarPayload` (`extra="allow"`, como `FacturaPayload`: las órdenes de la pre factura). **El «facturar» no está en el motor**: lo hace el producto con su camino de emisión y después llama a `pre_facturas.marcar_facturada(..., conn=...)` en la misma transacción.

**Consecuencias.**
- Contalibra y LibraDesk no cambian: sus filas no tienen número, no son pre facturas, y el módulo no las toca (`PreFacturaNoEncontrada`). La bandeja que ya tenían tampoco: sólo agregan columnas vacías.
- La bandeja de siempre (`/api/comprobantes-pendientes`) sigue listando `pendiente`, `facturado` y `descartado`; una pre factura `enviado` o `aceptado` se ve en el router de pre facturas, no ahí.
- La migración no se baja: restaurar el backup.
- Dos cosas quedan del producto: qué órdenes forman cada pre factura (la tabla de vínculo, la reserva y la liberación) y el camino de emisión por ARCA.

## ADR-031 — El emisor de un PDF se resuelve por documento, en un solo lugar

**Contexto.** Todos los PDF del motor salen de `pdf_generator`, pero el emisor (nombre, CUIT, domicilio, condición de IVA, IIBB, inicio de actividades, logo) salía siempre de `_empresa()`, o sea de la configuración global de la instancia: un solo emisor. `generate_pdf_factura` ignoraba `facturas.emisor_id` (ADR-021), así que una instancia con varias razones sociales (LibraCargo) imprimía el CUIT de una y el membrete de otra. Sólo la pre factura (ADR-030) aceptaba el emisor inyectado, con un camino propio. El humano preguntó (2026-10-06) si la familia tenía normalizado cómo crea los comprobantes con PDF; no lo tenía.

**Decisión.** `libracore.emisor_del_pdf.emisor_para(documento, *, resolvedor=None, empresa=None, conn=None)` arma el `dict` `empresa` por capas, cada una pisa a la anterior:
1. la configuración de la instancia (`pdf_generator._empresa()`): lo de siempre;
2. `documento["emisor_id"]`, si lo tiene: `nombre` y `cuit` de esa fila de `arca_config` (`empresa` y `cuit`), **también si está inactiva** (dar de baja una razón social no cambia con quién se emitió lo que ya salió); un id que no existe levanta `EmisorDesconocido`, nunca cae a otro emisor;
3. el **resolvedor** del producto, `(documento: dict) -> dict | None`: domicilio, condición de IVA, IIBB, inicio de actividades, logo (`logo_bytes` o `logo_path`) y lo que quiera pisar. Una clave en `None` no pisa; un texto vacío sí. **Lo que levante, sube**: caer al membrete de la instancia sería imprimir los datos de otra razón social;
4. `empresa=`, lo que pasa quien llama a un generador puntual (la pre factura lo tenía desde ADR-030): gana sobre todo.

**El resolvedor se da de dos maneras.** Registrado una vez al arrancar, `emisor_del_pdf.registrar_resolvedor(fn)` (como `libro_de_clientes.registrar_origen_de_ventas`): vale para **todos** los PDF del proceso, también los que el motor genera por su cuenta (al autorizar, `venta_facturacion`, la bandeja de MercadoPago), que un parámetro de router no alcanza. Por parámetro (`resolvedor=` de cada generador, `emisor_del_pdf=` de los routers): reemplaza al registrado para esa llamada.

**Todos los generadores pasan por ahí**: `generate_pdf_factura` (factura, notas y FCE), `generate_pdf_recibo` y `generate_pdf_recibo_doc`, `generate_pdf_presupuesto`, `generate_pdf` (remito), `generate_pdf_pre_factura` y `generate_pdf_resumen_cc`, todos con `resolvedor=` (kw-only). La pre factura conserva `empresa=` y gana `conn=` para leer `arca_config` desde la transacción de quien llama. La tabla `recibos` no tiene `emisor_id`: un recibo emitido usa la instancia y el resolvedor.

**Routers.**
- `build_comprobantes_router(..., emisor_del_pdf=None)` y `build_nota_de_credito_router(..., emisor_del_pdf=None)`: el PDF que guardan al emitir, autorizar o hacer una nota, el borrador y el mail salen con ese emisor. El borrador lleva el `emisor_id` elegido (un emisor que no existe es 422).
- **`build_comprobantes_pdf_router(*, usuario_actual, prefix="/api/facturas", emisor_del_pdf=None, puede_ver=None, smtp_config=None, donde_configurar_smtp=...)`**: sólo `GET /{id}/pdf` y `POST /{id}/enviar-email`, para un producto cuyo comprobante vive en `facturas` pero que emite, anula y hace notas por su cuenta (LibraCargo). Comparte el código del router grande (`_pdf_del_comprobante`, `_registrar_enviar_email`). `puede_ver(comprobante) -> bool` deja al producto esconder comprobantes (de otro ambiente, de otra razón social): lo que no pasa da 404 en las dos rutas, igual que un id inexistente. **El router grande no gana `GET /{id}/pdf`**: Contalibra, Restolibra y LibraClub ya tienen el suyo y dos rutas con el mismo path se resuelven por orden de registro.
- El mail lo firma el emisor del comprobante (asunto y cuerpo), no el de la instancia: `enviar_comprobante_por_mail(..., empresa_nombre=None)`.

**El PDF guardado no se regenera.** `facturas.pdf_path` apunta al PDF de lo que salió, con el emisor de ese momento. Los endpoints de PDF y de mail lo sirven si el archivo está; sólo si se perdió (un redeploy que borra el disco del contenedor) lo rearman desde la fila, con el emisor de hoy, y sin volver a guardar la ruta. `generate_pdf_factura` sigue escribiendo siempre el archivo.

**Un defecto que salió al medir.** El archivo se llamaba `factura_{pv}_{numero}.pdf`: sin el tipo, el emisor ni el ambiente. Una nota de crédito 0001-00000001 y la factura 0001-00000001 compartían archivo (la nota pisaba a la factura), y dos razones sociales con la misma numeración, también. Con `pdf_path` sirviéndose desde el disco, el PDF de una razón social habría salido con el membrete de la otra. Ahora el nombre lleva el `id` del comprobante (`factura_{id}_{pv}_{numero}.pdf`); sin `id` (el borrador) es el de siempre. Los `pdf_path` ya guardados no se tocan.

**Consecuencias.**
- Quien no hace nada no cambia: sin resolvedor ni `emisor_id` los bytes de cada PDF son los mismos que en v1.140 (verificado contra el árbol de v1.140.0 con factura, remito, presupuesto, recibo, pre factura y resumen).
- Contalibra, Restolibra y LibraClub no tienen que hacer nada. Los comprobantes que tengan `emisor_id` (sólo los de un producto con varias razones sociales) salen con el nombre y el CUIT de su `arca_config`.
- Pre factura: antes, un `emisor` inyectado reemplazaba al `emisor_id`; ahora el `emisor_id` va debajo y el inyectado lo pisa clave por clave. Si un producto pasa `nombre` y `cuit`, es lo mismo; si no los pasa, salen los del `arca_config` y no los de la instancia.
- `arca_config` sigue sin guardar domicilio, condición de IVA ni logo (ADR-021): los pone el producto con el resolvedor. Llevarlos a la tabla es una decisión aparte.
- Fuera de alcance: el ticket del POS (`ticket_generator`) arma su membrete de `config_manager` directo; no es un comprobante con emisor.

## ADR-032 — Las credenciales de ARCA son por servicio, y la facturación no se mueve

**Contexto.** El motor sabía de un solo servicio de ARCA, la facturación (`wsfe`): su par certificado/clave vive en las columnas de `arca_config` (el de producción en las sin sufijo, `COLUMNAS_POR_AMBIENTE`) y la pantalla `build_arca_router` + `ArcaCard` del kit sólo mostraba ése. LibraCargo (Suitrans) sumó el CTG y la Carta de Porte Electrónica (`wscpe`): certificados propios (alias `libracargowscpehomo` / `libracargowscpeprod`), emitidos al CUIT de **una persona** que actúa en nombre de la empresa (la delegación va en cada llamada, `cuitRepresentada`). El humano (2026-10-07): «en Configuración / ARCA no tenemos ninguna pantalla que muestre los certificados y el estado de CTG y Cartas de Porte». `arca_wsaa.autenticar(..., servicio=)` ya sabía autenticar cualquier servicio y cachea el ticket por (ambiente, servicio, certificado); faltaba dónde guardar el par y dónde mostrarlo.

**Decisión.**
1. **Almacén aparte para los demás servicios.** Tabla `arca_credenciales_servicio` (migración `0022`): una fila por `(empresa, servicio, ambiente)` con `certificado_path` y `clave_path`, `UNIQUE` en esa terna y `CHECK` sobre el ambiente. Sin FK a `arca_config`: un producto puede cargar el certificado de un servicio antes de tener fila de facturación. Accesores en `libracore.db.arca_credenciales_servicio` (`paths_de_servicio`, `guardar_paths_de_servicio`, `borrar_paths_de_servicio`) y `arca_credenciales.paths_en_disco_de_servicio` (que rescata una ruta vieja por nombre dentro de `CERTS_DIR`, y no cae a nada más). **`wsfe` no se migra a esta tabla**: sus ocho lectores y `paths_de` quedan intactos.
2. **Catálogo** (`libracore.arca_servicios`): `wsfe` («Facturación electrónica») y `wscpe` («CTG y Carta de Porte»), con el nombre que WSAA espera, una línea de ayuda y, para `wscpe`, los endpoints SOAP de homologación y producción para el `dummy` (sin autenticación, informativo). Agregar un servicio es agregar una entrada.
3. **Router por opt-in**: `build_arca_router(servicios=("wsfe",))`. Por omisión es idéntico al de siempre y sólo suma `GET /servicios` (un bloque). Con `servicios=("wsfe", "wscpe")` suma, por servicio que no es la facturación, `estado`, subir `certificado` y `clave`, `DELETE credenciales` y `probar`, con las mismas validaciones, el mismo gate (lo pone el producto sobre el router entero) y el mismo `al_cambiar` (con `servicio` en el `detalle`; `ACCIONES` no cambia). `GET /servicios` lista también la facturación, con el estado calculado por la misma función, para que la pantalla pinte un bloque por servicio sin casos especiales.
4. **`ambiente` obligatorio** en subir, quitar y probar de los servicios nuevos (422 si falta o es raro): la facturación puede caer al selector de la instancia porque lo tiene; un servicio sin selector que adivinara subiría un certificado de prueba sobre el real. Sin `empresa`, la del producto (`empresa_por_defecto`), no «la primera fila de facturación».
5. **«Probar» autentica de verdad por WSAA para ese servicio** y traduce los errores conocidos (`coe.notAuthorized` → asociar el servicio en el Administrador de Relaciones al alias del certificado; `cms.cert.untrusted` y `cms.cert.expired` → el certificado no vale para ese ambiente o está vencido; hora corrida) **dejando el texto de ARCA al final**. Si hay un ticket vigente en la caché de disco, «Probar» es OK sin pedir otro. El ticket (token y sign) no sale en la respuesta.
6. **El CUIT del certificado** (`cuit_certificado`, de `serialNumber=CUIT n` del sujeto) se informa sólo en las respuestas nuevas; las de la facturación no ganan claves.

**Consecuencias.**
- Los ocho productos que no pasan `servicios` no cambian: ni rutas (salvo `GET /servicios`), ni respuestas, ni columnas; la migración sólo agrega una tabla vacía.
- La migración **sí se puede bajar** (a diferencia de las anteriores): la tabla guarda dónde están los archivos, no los archivos; se pierde la asociación y los `.crt`/`.key` quedan en `CERTS_DIR`.
- Los archivos se guardan en `CERTS_DIR` como `{servicio}-{ambiente}-{huella de la empresa}.crt|.key`, con la clave en 0600 (`escribir_clave_privada`): dos servicios, dos ambientes o dos empresas nunca comparten archivo.
- El CUIT con el que se opera `wscpe` **no sale del certificado ni de esta tabla**: es un dato de cada llamada, elegido de forma explícita (ver el plan de CTG de LibraCargo). Esta pantalla sólo dice de quién es el certificado.
- El `dummy` de `wscpe` se verificó contra ARCA con el script de la Fase 0 del wiki (2026-10-02), no desde esta suite, que no sale a la red.
- Fuera de alcance: el módulo `arca_wscpe` (consultar y emitir CPE) y la delegación en sí, que se hace en ARCA.

## ADR-033 — La siembra del depósito por defecto sólo ocurre en la tabla `depositos` del motor

**Contexto.** `init_core_schema()` siembra «Depósito Principal» (`INSERT INTO depositos (nombre, descripcion, es_default) VALUES (?,?,1)`) cuando `depositos` está vacía, y la `0001` la llama sobre cualquier base. LibraDesk tiene su propia `depositos` (su migración `0005_depositos`, con `activo` y `es_default` BOOLEAN): `libracore-migrar upgrade --prefijo libradesk` sobre una base **vacía** (alta de un cliente, reset nocturno de la demo, restaurar un backup) moría con `DatatypeMismatch: column "es_default" is of type boolean but expression is of type integer`, porque PostgreSQL no convierte un entero en booleano. Sobre una base con depósitos no pasaba, que es por lo que tardó en verse (LibraDesk ADR-012). Las otras dos siembras de la función (`cajas`, `categorias_egreso`) corren sin error en LibraDesk, que declara esas tablas con las mismas columnas enteras que el motor; `git grep` en los productos no encuentra otra tabla propia con esos nombres y tipo distinto.

**Decisión.**
1. **La siembra sólo corre si `depositos.es_default` es una columna entera**, que es como la declara el motor. El tipo se lee con `PRAGMA table_info`, que el adaptador traduce a `information_schema` en PostgreSQL: la misma pregunta, sin ramas por motor. Una tabla ajena con otro tipo (o sin `es_default`) no se siembra.
2. **No se siembra «con el tipo correcto»** (`TRUE` o `1` según el tipo). Se evaluó y se descartó: (a) el depósito sembrado es un cambio de comportamiento en el producto ajeno, cuya base nueva nace sin depósitos y cuya pantalla los crea; (b) adaptar el valor de una columna no hace conocida a la tabla: las demás columnas (`cliente_id`, `NOT NULL` propios) pueden romper el mismo `INSERT` por otro lado.
3. **La misma regla vale para quien tenga una `depositos` propia con otro tipo**: el motor no escribe en una tabla que no reconoce. Si un producto quiere un depósito inicial, lo siembra él, en su migración.

**Consecuencias.**
- La tabla del motor se siembra exactamente como antes (SQLite y PostgreSQL), y una tabla ajena con filas tampoco cambia: lo único que cambia es una `depositos` ajena **vacía**, que antes fallaba y ahora queda vacía.
- No hay cambio de schema: `test_schema_congelado` no se mueve. Es un cambio de PATCH (v1.142.1).
- Queda fuera, a propósito: las funciones de `libracore.db.productos` (`set_default_deposito`, que escribe `es_default=0/1`) siguen suponiendo la tabla del motor. LibraDesk no las usa (tiene su propio servicio de depósitos).
- La siembra de `cajas` tiene el mismo riesgo latente si algún producto declarara su `cajas` con `es_default` BOOLEAN. Hoy ninguno lo hace; si aparece, se aplica la misma guarda.

## ADR-034 — La Carta de Porte Electrónica se lee en el motor, y por quién se consulta es un dato de cada llamada

**Contexto.** LibraCargo (Suitrans, transportista de granos) necesita traer de ARCA los datos de una Carta de Porte Electrónica (CPE) —kilos de carga y de descarga, chofer, pagador del flete, origen, destino, estado y el PDF— a partir de su CTG. El servicio es `wscpe`. ADR-032 ya dejó el catálogo (`arca_servicios`, con el `dummy`), el almacén del par por servicio y la prueba de WSAA; faltaba el cliente. Medido en producción el 2026-10-07 con el certificado de Suitrans (a nombre de la persona que representa a la empresa): WSAA da el ticket con las **relaciones** del alias adentro (`<relation key="CUIT" reltype="4"/>`), una consulta con un `cuitRepresentada` que no está en ese conjunto vuelve como `soap:Fault` («no esta relacionada con el conjunto {…}»), y una CPE que no existe —o en la que ese CUIT no interviene— vuelve como error `800`. El WSDL v2.2.0 no tiene ninguna consulta por transportista ni por chofer.

**Decisión.**
1. **`libracore.arca_wscpe`, sólo lectura**, hermano de `arca_wsfecred` y con su forma: `consultar_cpe(cuit_representada, token, sign, *, ctg= | tipo_cpe=+sucursal=+nro_orden=, cuit_solicitante=, ambiente=)` devuelve una `CartaDePorte` tipada (cabecera, `Origen`, `Destino`, `Carga` con neto de carga y de descarga, `Transporte`, intervinientes, `pdf` en bytes) y la `respuesta_xml` **sin el PDF** para archivar lo que hoy no se usa. `provincias()` es la llamada autenticada más barata, para probar una representación. El `dummy` no se duplica: es `arca_servicios.dummy("wscpe", ...)`.
2. **El CUIT representado no tiene valor por defecto** en ninguna función: ni sale de `arca_config` (como en la facturación) ni del certificado. Con la persona, la empresa y cada titular que delegue, un default es consultar —y el día que se emita, firmar— por el contribuyente equivocado. `consultar_por_ctg(empresa, ambiente, *, cuit_representada, ctg)` resuelve el par con `paths_en_disco_de_servicio` y exige el CUIT por nombre.
3. **Errores tipados por lo que cambian para el operador**: `CpeNoEncontrada` (`800`), `CuitNoRelacionado` (el `soap:Fault`, con el mensaje de qué hacer: delegar `wscpe` al alias en el Administrador de Relaciones, y que una delegación nueva se ve recién con el próximo ticket), `ErrorWscpe` para el resto con los `(codigo, descripcion)` de ARCA y `SinCredenciales` si no hay par cargado. Los errores de WSAA pasan por `traducir_error_wsaa`.
4. **`cuits_habilitados(ticket)`** lee las relaciones del token: dice **antes** de consultar por quién puede operar el certificado, que es lo que la pantalla necesita para no ofrecer un CUIT que ARCA va a rechazar.
5. **Emitir no entra acá.** `autorizarCPEAutomotor`, `consultarUltNroOrden` y el ciclo de vida (arribo, desvío, anulación, contingencia) son un documento fiscal y de circulación: van en su propio ADR, con la guarda de no reintentar a ciegas tras un timeout.

**Consecuencias.**
- Sin migración y sin cambio de schema; ningún producto cambia si no lo importa. Es un cambio de MINOR (v1.143.0).
- Las CPE completas de los tests son sintéticas sobre el WSDL (no hay todavía una CPE real legible); los errores y las provincias son respuestas reales de producción, anonimizadas (`tests/fixtures_wscpe/README.md`). Cuando haya una CPE real, se graba y reemplaza a la sintética.
- Dónde guardar la CPE (tabla `cartas_porte`), vincularla a la orden de carga y refrescarla hasta la descarga es del producto (LibraCargo), con este módulo como única puerta a ARCA.
- 🔴 Una delegación nueva no se ve hasta que vence el ticket vigente (≈12 h), porque WSAA no entrega otro antes (`coe.alreadyAuthenticated`). No hay forma de forzarlo desde acá.

## ADR-035 — Emitir la Carta de Porte Electrónica en el motor, sin reintentar a ciegas

**Contexto.** Fase 4 del plan de CTG y Carta de Porte de LibraCargo: Suitrans emite cartas de porte **por delegación** de un titular (Agropecuaria Pereiro) con un software de terceros, y quiere hacerlo desde el sistema. ADR-034 dejó la lectura. Lo medido en homologación el 2026-10-08, con el certificado de la persona que representa a Suitrans:
- `consultarUltNroOrden` da `0` sin cartas emitidas.
- Los catálogos andan: 39 granos (Soja = 23) y 905 localidades en la provincia 12.
- `consultarPlantas` sin plantas da `800`.
- `anularCPE` sobre una carta que no existe da `1302`.
- **Autorizar valida contra los registros reales también en homologación**:
  - origen en campo con `esSolicitanteCampo=false` da `949`;
  - origen en campo con `true` da `1015` (el solicitante no tiene actividad de productor);
  - origen en planta da `2008` (no está activo en SISA).
- En homologación el certificado sólo opera por su propio CUIT, así que una emisión exitosa necesita un certificado de homologación del titular.

**Decisión.**
1. **En `libracore.arca_wscpe`**, con la lectura. La emisión es del protocolo, no de un producto (`reglas/producto.md`).
2. **`SolicitudCpe` tipada**, con `OrigenPlanta | OrigenCampo`, `DestinoSolicitud` y `TransporteSolicitud`. Arma el XML en el orden del esquema: los `dominio` repetidos van después del transportista y los intervinientes en el orden del WSDL, sin importar cómo lleguen. **`esSolicitanteCampo` no es un campo: sale del origen** (949 medido). `problemas()` valida los rangos del esquema antes de llamar: pesos de 1 a 88.000 kg, tara menor que el bruto, cosecha de 4 cifras, 1 a 99.999 km, 1 a 3 dominios de 6 o 7 caracteres, tarifa hasta 99.999,99, observaciones de hasta 2.000 caracteres y CUIT de 11 dígitos.
3. **`emitir_cpe` es el camino**; `autorizar_cpe` queda sin guardas:
   - **el CUIT representado tiene que ser el solicitante**: se emite *en nombre de* quien delegó;
   - **cerrojo entre procesos** por (solicitante, sucursal, tipo, ambiente), con el `flock` de `arca_wsaa`, alrededor de «último número + autorizar»;
   - 🔴 **si ARCA no contesta al autorizar** (error de transporte o respuesta que no es SOAP), **no se reintenta**. Se consulta por el número pedido: si la carta está, se devuelve; si ARCA dice que no existe, se informa que no se emitió; si tampoco contesta, `EmisionIncierta`, con la sucursal y el número. Es la regla del manual (sección 1.3) y la misma lección del CAE.
4. **Catálogos**: `tipos_grano`, `localidades(cod_provincia)`, `plantas(cuit)` (sin plantas, `[]`) y `ultimo_nro_orden`. **Anular**: `anular_cpe`, con observaciones de 1 a 100 caracteres.
5. Fuera de este ADR: desvío, contingencia, confirmación de arribo y editar. Se suman cuando el producto los use.

**Consecuencias.**
- Sin migración y sin cambio de schema. MINOR (v1.144.0).
- Los rechazos de los tests son respuestas reales de homologación, anonimizadas. La autorización exitosa es sintética (la forma de `DetalleAutomotorRespuesta`) hasta que haya un certificado de homologación de un titular con SISA.
- El test del cerrojo usa un doble de ARCA que cede el control: sin eso pasaba aunque se sacara el cerrojo (se verificó sacándolo).

## ADR-036 — El pedido de certificado de ARCA se genera en el servidor, y la clave privada no sale nunca

**Contexto.** Una empresa nueva que contrata un producto de la familia necesita un certificado de ARCA para facturar (`wsfe`) y, en LibraCargo, para el CTG y la Carta de Porte (`wscpe`). Hasta hoy el par se hacía **afuera**: quien da de alta la instancia corre `openssl genrsa` y `openssl req` en su PC (`docs/guia-certificado-arca.md`, paso 1), manda el `.csr` a ARCA y, cuando vuelve el `.crt`, **sube los dos archivos** a la pantalla de Configuración / ARCA. La clave privada —la identidad fiscal del contribuyente— viaja así por un chat, un mail o un pendrive antes de llegar al servidor. El humano (2026-10-08), mirando la sección de certificados: «¿esta parte cuándo la usamos? ¿cuando es un cliente nuevo que contrata nuestro sistema? ¿cómo genero el certificado?». Aprobó un botón **«Generar pedido de certificado»** que genere la clave dentro del servidor, ofrezca sólo el `.csr`, muestre los pasos para ARCA y, al volver el `.crt`, deje todo listo sin campo de clave.

**Qué ya existe** (se miró antes de diseñar, `reglas/producto.md`):
- `arca_certificados`: `leer_certificado`, `leer_clave`, `son_pareja`, `escribir_clave_privada` (0600, sin instante abierta; ADR de v1.121.0). Todo lo que hacía falta para **validar** y **guardar**; faltaba **generar**.
- `arca_router`: `/certificado` y `/clave` (facturación) y `/servicios/{servicio}/certificado` y `/clave` (ADR-032), con su gate puesto por el producto, `al_cambiar` sin datos secretos y `ambiente` explícito donde no hay selector que adivinar. El pedido repite esa forma.
- `db.arca_config` (`COLUMNAS_POR_AMBIENTE`, `paths_de`) y `db.arca_credenciales_servicio`: dónde se **guarda** el par vigente. No se tocan.
- `config_manager.CERTS_DIR` y el respaldo (`directorios_de_datos`): `arca_certs/` ya viaja en el ZIP del cron y del botón. Un archivo ahí se respalda sin hacer nada.
- `arca_wsfe.cuit_con_verificador_valido`: la guarda del CUIT de la familia, que el pedido reusa en vez de escribir otra.
- `cryptography` ya es dependencia (la usa `arca_certificados`): RSA y PKCS#10 no agregan nada al `pyproject.toml`.
- El sujeto que pide ARCA (`C=AR, O=<razón social>, CN=<alias>, serialNumber=CUIT <11 dígitos>`) y que se armó a mano para Suitrans y Pereiro está en `docs/guia-certificado-arca.md`.
- Buscado (`git grep` de `CertificateSigningRequest`, `genrsa`, `generate_private_key`, `openssl req` y `.csr` en `libracore` `origin/develop` y en `origin/main` de los ocho productos y de `libra-ui`, 2026-10-08): **no hay un generador de claves ni de `.csr`** en ningún repo. Sólo instrucciones para hacerlo a mano con `openssl`: la guía del motor, el tutorial `TutorialArcaCertificado` del kit (`configuracion/tutoriales.tsx`) y la página de integraciones del sitio de RestoLibra (`website/public/docs/integraciones.html`, con un sujeto `CN=CUIT n` distinto del de la guía).

**Decisión.**
1. **Funciones puras en `arca_certificados`**: `datos_del_pedido(cuit, razón social, alias)` normaliza y valida (`PedidoInvalido`) y `generar_pedido(...)` devuelve un `PedidoDeCertificado` con la **clave** (RSA 2048, PKCS#8, sin passphrase, como lo exige `leer_clave`) y el **`.csr`** (SHA-256, sujeto de ARCA en ese orden). No tocan el disco ni la base. La clave está fuera del `repr` del objeto: un `print` o un `logger.debug` no la escriben.
2. **La clave pendiente vive aparte de la vigente y no la pisa** (`libracore.arca_pedidos`). Entre que sale el `.csr` y vuelve el `.crt` pasan horas o días; si la clave nueva ocupara el lugar de la vigente, una instancia que ya factura —o que renueva un certificado por vencer— dejaría de hacerlo con un certificado que sigue siendo bueno. Dos archivos en `CERTS_DIR` por pedido, `pedido-{servicio}-{ambiente}-{huella de la empresa}.key` (0600) y `.json` (alias, CUIT, razón social, fecha y el `.csr`). **Un pedido pendiente por (servicio, ambiente, empresa)**; `wsfe` es el servicio de la facturación.
3. **Sin tabla y sin migración.** Se evaluó una tabla `arca_pedidos` (o columnas `*_pendiente` en `arca_config` y `arca_credenciales_servicio`) y se descartó: es un archivo con una vida corta que nunca se consulta por otra cosa que por su terna, `arca_certs/` ya está en el respaldo y en la restauración, y una migración sobre `arca_config` obligaría a tocar a los ocho productos que la leen. El costo: el estado del pedido es el del volumen, no el de la base. Quien restaura un respaldo trae el pedido con su clave, que es lo que se quiere.
4. **Rutas, iguales para la facturación (`{prefix}/pedido`) y para cada servicio (`{prefix}/servicios/{servicio}/pedido`)**, con `ambiente` **obligatorio** en la query (422 si falta o es raro; nunca cae a producción):
   - `POST …/pedido` (cuerpo: `cuit`, `razon_social`, `alias`, `reemplazar`): genera, guarda y devuelve el pedido **con el `.csr` en PEM**. Si ya hay uno pendiente, **409** salvo `reemplazar: true`: reemplazar destruye una clave cuyo `.csr` puede estar ya en ARCA, y el `.crt` que vuelva quedaría sin pareja.
   - `GET …/pedido`: `{pendiente, alias, cuit, razon_social, sujeto, creado}` (`dd-mm-aaaa`, hora argentina); sin pedido, `{pendiente: false}`.
   - `GET …/pedido.csr`: el `.csr` como descarga (`application/pkcs10`, `{alias}.csr`). Es público, es lo que se le manda a ARCA.
   - `DELETE …/pedido`: descarta el pedido **y su clave**; el par vigente no se toca. 404 si no había.
   Los estados que ya existían (`GET {prefix}`, `/estado`, `/servicios…`) suman la clave `pedido` **dentro del par del ambiente, sólo cuando hay un pedido pendiente**: ninguna respuesta de siempre gana una clave que sus consumidores no esperan. Lo único que ve siempre un cliente nuevo es `admite_pedido: true` en cada entrada de `GET {prefix}/servicios`: es la marca con la que el kit sabe que este motor tiene las rutas (un motor anterior no la manda, y la pantalla no muestra un botón que daría 404).
5. **La promoción ocurre al subir el `.crt` por la ruta de siempre** (`…/certificado`). Si hay un pedido pendiente: (a) empareja con **su** clave → la clave pendiente se instala como vigente (0600) junto con el certificado, en los archivos del par de ese ambiente, y el pedido se borra; (b) empareja con la **vigente** → sigue como hoy y el pedido espera; (c) no empareja con ninguna → **422** que nombra el pedido (alias y fecha) y no cambia nada. Sin pedido, todo igual que antes. El cuerpo de la respuesta es el de siempre: la pantalla no necesita un campo nuevo.
6. **Validación antes de generar**: CUIT de 11 dígitos con **dígito verificador** (`cuit_con_verificador_valido`), razón social sin tildes —`Ñ` pasa a `N`— de hasta 64 caracteres (el tope de `O` en X.509) con letras, números, espacio y `. , & ' -`, y alias de 3 a 40 letras y números (ARCA no admite guiones). Un dato malo es un 422 con la causa y **no deja nada en el disco**.
7. **Auditoría con el hook de siempre, sin datos secretos.** `ACCIONES` suma `pedido` y `descartar_pedido`. `pedido` lleva empresa, servicio, ambiente, alias, CUIT, sujeto y si reemplazó a otro; **nunca la clave ni el `.csr`**. La promoción deja los asientos de siempre —`certificado` y `clave`— con `desde_pedido: true` (y el alias en el certificado): para quien lee la bitácora, la clave cambió igual.
8. **La clave privada no sale.** Ninguna respuesta la lleva, ni la de generar, ni las de estado, ni el `detalle` de `al_cambiar`, ni el log. El único lector de la clave pendiente es `arca_pedidos.clave()`, que la usa el router para emparejarla con un `.crt` y, si empareja, instalarla.
9. **Si no hay fila de `arca_config`, generar el pedido de la facturación la crea** (con el CUIT del pedido, y el ambiente como selector), igual que lo hace subir un archivo: es el primer paso de un cliente nuevo y la pantalla no tendría de dónde leer el pedido pendiente. Si la fila existe, su CUIT no se toca (si estaba vacío se completa). Para un servicio con delegación (`wscpe`) el CUIT del pedido es el de quien firma, no el de la empresa.

**Consecuencias.**
- **Sin migración y sin cambio de schema.** MINOR (propuesta v1.147.0). Los productos que no hacen nada siguen igual: las rutas son nuevas, las respuestas de siempre no ganan claves y el `/certificado` se comporta como antes mientras no haya un pedido.
- El kit (libra-ui, ADR-041) agrega el botón y el diálogo; un producto lo recibe subiendo los dos pines. Un backend sin las rutas nuevas (LibraCore viejo) contesta 404 y el kit esconde el botón.
- `ACCIONES` crece de cuatro a seis. Un hook que mapea por esa tupla recibe dos acciones nuevas; el de LibraCargo ya registra lo desconocido como modificación (`ACCIONES.get(accion, MODIFICACION)`).
- 🔴 **Un pedido reemplazado o descartado no se recupera**: la clave se borra. Por eso el reemplazo exige confirmación explícita y el kit lo pide en un diálogo aparte.
- Un `.crt` que no sale de un pedido sigue pudiéndose subir con su clave (el campo «Clave privada (.key)» del kit queda como alternativa avanzada, para quien ya tiene un par hecho afuera), y sin pedido pendiente un `.crt` suelto se acepta como siempre.
- Fuera de alcance: **presentar el `.csr` ante ARCA** (se hace a mano, en «Administración de Certificados Digitales» o en WSASS), la relación con el servicio en el «Administrador de Relaciones de Clave Fiscal» y cualquier automatización de ARCA con clave fiscal. La renovación es el mismo flujo: pedir uno nuevo con la vigente cargada la deja facturando hasta que llega el `.crt`.
- No verificado contra ARCA: que acepte `Ñ` normalizada y el conjunto de signos de la razón social. El sujeto es el del `openssl req -subj` que ya se usó con Suitrans y Pereiro; el `.csr` se validó con una CA de prueba (firma válida, sujeto, clave pública) y no subiéndolo a ARCA.

## ADR-037 — Los rangos de fecha se miden por día, en un solo lugar (`libracore.fechas`)

**Contexto.** Las columnas de fecha del motor son `TEXT` libre (`facturas.fecha`, `egresos.fecha`, `ventas.fecha`, `caja_movimientos.fecha`, `movimientos_stock.fecha`, `movimientos_tesoreria.fecha`, `cc_asientos.fecha`, `recibos.fecha`, `cierres_diarios.fecha`, `remitos.date`, `presupuestos.date`). Los escritores internos guardan `AAAA-MM-DD`, pero los routers aceptan `fecha: str` sin validar, así que una fila puede traer hora (`2026-10-09 13:00:00` o `2026-10-09T13:00`). 🔴 Con `fecha <= '2026-10-09'` esa fila **queda afuera**: como texto es mayor que la fecha pelada. El último día del rango desaparece del listado, del reporte, del resumen o del libro IVA sin ningún error. Más de veinte consultas del motor tenían el filtro así.

**Qué ya existía** (se miró antes de arreglar):
- `db/tesoreria.get_movimientos_tesoreria` ya lo había parcheado: `hasta + " 23:59:59"`. Resuelve la hora con espacio, pero **falla con la `T`** (`'T'` > `' '`) y era el único lugar.
- `libracommerce/erp/margen.py` (`_filtro_de_ventas`) ya usa el criterio bueno: `desde` como `>=` del día y `hasta` como `<` del **día siguiente**. Es una copia local del producto; la lógica de fondo vive en el motor (ADR-001, enmienda).

**Decisión.**
1. **`libracore.fechas.rango_por_dia(columna, desde, hasta) -> (condiciones, params)`**, función pura sin base: `desde` → `columna >= 'AAAA-MM-DD'`; `hasta` → `columna < '<día siguiente>'`. Sólo comparaciones de texto con `?`: el mismo SQL en SQLite y en PostgreSQL, sin `substr`, `date()` ni `CAST` (que además taparían el índice). Para fechas sin hora el resultado es el de siempre: `[d, h]` incluye los dos días.
2. **Un extremo que no es una fecha ISO se usa tal cual**, con `>=` / `<=` como antes (`dia_iso` acepta `AAAA-MM-DD` solo o seguido de espacio o `T`): un llamador raro conserva su comportamiento. Vacío o `None`, sin condición.
3. **Todos los filtros de rango del motor la usan**: egresos, facturas, recibos, ventas, stock, tesorería (sin el parche), reportes, resumen, dashboard, caja, libro IVA, log de actividad y libro de terceros. Los parámetros de entrada de cada función no cambian.
4. **El saldo anterior de un extracto mide «antes del día de `desde`»** (`libro_de_terceros.extracto`): con la hora de `desde` en la comparación, el mismo asiento entraba en el saldo anterior y también en el período.

**Consecuencias.**
- Sin migración y sin cambio de schema. MINOR (propuesta v1.148.0). Un rango que antes dejaba afuera las filas con hora del último día ahora las incluye: los totales de esos días **suben** y son los correctos.
- **libracommerce y Restolibra pasan a usar `libracore.fechas.rango_por_dia`** al subir el pin, y sacan sus copias (la de `margen.py` y cualquier filtro de rango propio).
- Los tests de integración (`tests/db/test_rango_por_dia.py`) siembran el mismo día de tres maneras más dos vecinos que no deben entrar, y corren también contra PostgreSQL si hay `LIBRACORE_POSTGRES_URL`.
- Fuera de alcance: **validar la fecha en los routers** o normalizar lo ya guardado. Sigue siendo TEXT libre; esto sólo hace que el filtro no dependa de cómo se escribió. Los filtros que se hacen en Python sobre listas ya leídas (`get_cc_movimientos_periodo`) no usan la función y quedan como estaban.

## ADR-038 — En una demo, la empresa es siempre ficticia: la garantía la da `config_manager`, no la semilla

- Estado: aceptada
- Fecha: 2026-10-09
- Contexto: la demo pública de LibraDesk mostraba en la barra lateral, en Configuración y en
  los PDF la razón social, el CUIT, el domicilio, los teléfonos y los Ingresos Brutos **reales**
  de un cliente, y un logo con el nombre de otra empresa real. Alguien los había cargado a mano
  y el reset nocturno no los limpiaba: resiembra la base, y los datos de la empresa viven en
  `config.json` (`DATA_DIR`), al lado. Cualquiera con un código de demo los veía. Las semillas
  cargan datos por la API, desde afuera del contenedor, así que tampoco podían garantizar nada
  sobre ese archivo.
- Decisión:
  - Con `DEMO_MODE=1` (el criterio que ya usaba `mp_bandeja_router`, leído en cada llamada),
    `config_manager.load()` **pisa** los campos `CAMPOS_EMPRESA` con una empresa ficticia, y
    `resolve_logo_path()` sólo devuelve el logo de demo registrado: ni el `logo_path` guardado ni
    el «más reciente» de `LOGO_DIR`. Lo que esté en disco se ignora.
  - Costura del producto: `usar_empresa_demo(datos, logo_path)`, una vez al arrancar, con la
    empresa ficticia de su rubro y un logo que viaja **con la imagen**, no con `DATA_DIR`. Rechaza
    campos que no son de la empresa, para que un error de tipeo no pase en silencio.
  - Sin costura, `EMPRESA_DEMO_POR_DEFECTO`: «Empresa Demo SRL», CUIT `30-99999900-6` (dígito
    verificador válido, prefijo `30-999` que ARCA no asigna), correo en `.example` y sin logo.
- Consecuencias:
  - Fuera de una demo no cambia nada: los tests lo fijan.
  - Guardar datos reales en una demo sigue escribiendo el JSON, pero nunca se ven. Un test lo cubre.
  - **Lo que no cubre:** el emisor que `emisor_del_pdf` toma de la base (`arca_config` y el
    resolvedor del producto). Esos datos los rehace la semilla cada noche, así que no acumulan lo
    que alguien cargó a mano. Si un producto guarda la empresa fuera de la base y fuera de
    `config.json`, tiene que entrar por esta misma guarda.

## ADR-039 — Los códigos de acceso de una demo sobreviven al reset nocturno: el bloque vive en el motor (`libracore-demo-codigos`)

- Estado: aceptada
- Fecha: 2026-10-09
- Contexto: `demo_codigos` (libraauth: los códigos que se le entregan a un cliente potencial,
  válidos 7 días y 10 usos) vive en la misma base que la demo, y el `scripts/reset_demo.sh` de
  cada producto la recrea todas las noches con `DROP SCHEMA` dentro de un contenedor sidecar de
  PostgreSQL. 🔴 Medido en `/var/log/demo_reset.log` del 2026-10-09: en **seis** de las ocho demos
  el reset se lleva todos los códigos entregados, o sea que un código dura hasta la próxima
  medianoche y no los 7 días que promete. Sólo LibraCargo y LibraClub los preservan, con un bloque
  bash propio; el comentario de LibraCargo que decía que en las otras la tabla vivía en otra base
  ya no es cierto.
- Qué ya existía (se miró antes de diseñar): el bloque bash de LibraCargo y LibraClub (volcado
  `pg_dump --data-only --table=demo_codigos` antes del `DROP SCHEMA`, `psql -v ON_ERROR_STOP=1` al
  volver a levantar la app, que es quien crea la tabla). Es una copia a mano en dos repos, y hay
  seis repos sin ella. `libracore.provisioning` ya es el paquete del lado del servidor que corre
  Docker y está instalado en el `.venv-scripts` de cada producto.
- Decisión:
  - `libracore.provisioning.demo_codigos` con `guardar(sidecar, archivo, base=None)` y
    `devolver(sidecar, archivo, base=None)`, y la CLI `libracore-demo-codigos guardar|devolver
    --sidecar S --archivo F [--base B]`. Los ocho `reset_demo.sh` la llaman desde su
    `.venv-scripts` y se sacan el bloque propio.
  - Se conserva lo que ya funcionaba: `$POSTGRES_USER` y `$POSTGRES_DB` se resuelven dentro del
    sidecar (`sh -c` con comillas simples), así la contraseña no pasa por la línea de comandos del
    host; y la CLI imprime las mismas líneas que el bloque viejo, para que el log del cron se siga
    leyendo igual.
  - Lo que cambia: el volcado se crea 0600 (son códigos vivos); un `pg_dump` o un conteo que
    falla ya no se traga (`|| true`): sale con código 2 y no deja un volcado a medias; un
    `devolver` fallido sale con 1 y **deja el archivo** para reintentar a mano. `--base` opcional
    (identificador simple, validado) por si un producto tuviera la tabla en otra base.
  - Códigos de salida: 0 ok (incluye «nada que preservar» y «no había archivo»), 1 `devolver` no
    pudo, 2 falló Docker/psql en `guardar` o argumentos inválidos. El script llamador decide: en
    `guardar` un 2 significa que no se sabe si había códigos.
- Consecuencias:
  - Sin migración ni cambio de schema. MINOR (propuesta v1.150.0). Las demos las recogen al subir
    el pin y cambiar su `reset_demo.sh`: hasta entonces siguen perdiendo los códigos.
  - Los tests usan un ejecutor falso (sin Docker): fijan las órdenes, los permisos, que un error no
    borre el archivo y las líneas de la CLI. No hay prueba contra un PostgreSQL real.
  - Fuera de alcance: otras tablas de la base que también mueran con el reset (sólo se preserva
    `demo_codigos`) y la política de vigencia de los códigos.

## ADR-040 — La FCE puede cobrar en varios CBU: `fce_cbus` es la lista y `fce_cbu` queda como el predeterminado

- Estado: aceptada
- Fecha: 2026-10-09
- Contexto: pedido del dueño de Suitrans (2026-10-09): «el dueño puede querer que le depositen en una u otra cuenta». Hasta acá una empresa tenía **un** CBU para la Factura de Crédito Electrónica MiPyME (`arca_config.fce_cbu`, ADR de la FCE), copiado a cada FCE al emitirla. Para cobrar en otra cuenta había que cambiar la configuración antes de cada factura. Después se pidió que cada cuenta lleve además su alias bancario, que es como la conoce quien paga.
- Qué ya existía (se miró antes de diseñar): `fce_cbu` y `fce_transmision` en `arca_config`; su lectura en `facturas_router` (`_datos_de_fce`, `_tipos_fce_del_emisor`); `facturas.fce_cbu`, que guarda con qué CBU salió cada comprobante y es lo que leen WSFE (opcional `2101`) y el PDF; el patrón de columna FCE con `init_core_schema()` y una revisión que lo llama (`0014`). Los otros lectores de `fce_cbu` leen la **factura**, no la config, y no cambian.
- Decisión:
  - Columna nueva `arca_config.fce_cbus TEXT NOT NULL DEFAULT ''`: JSON `[{"cbu": "<22 dígitos>", "alias": "<alias o vacío>", "etiqueta": "<texto o vacío>"}]`. JSON en una columna y no una tabla hija: es una lista corta que se edita entera desde una pantalla y se lee siempre junto con la config, igual que el resto de lo FCE.
  - **`fce_cbu` se conserva y pasa a significar el CBU predeterminado.** Es la compatibilidad: quien lo lee (productos, pantallas viejas, tests) no se entera. La alternativa —sacarlo y que todos lean la lista— obligaba a tocar cada lector y cada instancia a la vez.
  - Tres helpers en `libracore.db.arca_config`: `cbus_fce` (la lista, con las tres claves; sin lista, la de uno armada con `fce_cbu`; JSON inválido, `[]`), `cbu_para_fce(config, pedido)` (el predeterminado, o el CBU/alias elegido; devuelve siempre el CBU; fuera de la lista, `ValueError`) y `actualizar_arca_config(fce_cbus=)`. Todo lector de la config que emita pasa por ahí.
  - Elegir se hace con el CBU **o el alias**, siempre dentro de la lista: un CBU de 22 dígitos válido pero no cargado se rechaza (422), porque aceptarlo es cobrar una FCE en una cuenta que el dueño no puso. El alias es opcional, de 6 a 20 caracteres (`a-z`, `0-9`, `.`, `-`, en minúsculas) y no se repite entre filas, como el CBU.
  - A ARCA sigue yendo sólo el CBU (opcional `2101`). El alias bancario (`2102`) **no** se agrega: es para elegir en la pantalla.
  - `PUT /config/arca`: lista y predeterminado no se desalinean. Con `fce_cbus`, `fce_cbu` tiene que estar en la lista (si no vino, el primero) y una lista vacía lo borra. Sin `fce_cbus` (pantalla vieja) rige `fce_cbu` como antes y, si no estaba en la lista, se agrega; mandarlo vacío vacía la lista, porque esa pantalla no sabe que hay más cuentas.
  - **La pre factura recuerda la cuenta**: `comprobantes_pendientes.fce_cbu TEXT` (nullable, como `fecha_vencimiento_pago`). Crear y editar aceptan `fce_cbu` (CBU o alias, resuelto con `cbu_para_fce` contra la config del `emisor_id` de esa pre factura; se guarda el CBU). `NULL` es «la predeterminada»: no se copia al elegir, así que si el predeterminado cambia antes de facturar, vale el nuevo. Sólo rige si el tipo es FCE; si no, `NULL`. En editar, sin mandarlo no se toca y `""` vuelve a `NULL` (la convención del módulo para `fecha_vencimiento_pago`). Cambiar el emisor con una cuenta que el nuevo no tiene es 422 y no un cambio silencioso de destino. Las respuestas traen `fce_cuenta` (`{cbu, alias, etiqueta}` o `null`) y el PDF imprime «Cobro en: CBU … (alias …)». Quien factura (el producto) pasa ese `fce_cbu` al alta de la factura.
  - Migración `0023_fce_varios_cbu`: llama a `init_core_schema` (que trae las dos columnas) y rellena con una lista de uno las filas que tenían `fce_cbu` y no lista (idempotente). No se baja: se perderían las cuentas cargadas además del predeterminado.
- Consecuencias:
  - MINOR (propuesta v1.152.0), **con migración**. Los productos corren la `0023` al subir el pin; una pantalla que no conozca `fce_cbus` sigue andando.
  - Una pantalla vieja que cambia el CBU predeterminado deja el anterior en la lista (sólo la nueva pantalla puede quitar cuentas).
  - Las FCE ya emitidas no cambian: `facturas.fce_cbu` guarda el CBU con el que salió cada una.
  - Fuera de alcance: la pantalla del kit y la elección en LibraCargo (consumen este contrato), y borrar una cuenta de la lista cuando hay FCE emitidas con ella (la factura conserva su CBU, así que no hace falta).


## ADR-041 — Las sucursales se cobran por lo CONTRATADO, y pasarse bloquea el alta: el dato vive en la instancia (`libracore.limites`)

- Estado: aceptada
- Fecha: 2026-10-10
- Contexto: pedido del dueño (2026-10-10). VentaLibra (plan único, ADR-072 de ese producto) cuesta **$39.900 por mes por instancia con UNA sucursal incluida**, y cada sucursal adicional en la misma instancia suma **$19.950** (precios finales, IVA incluido). Hasta acá la sucursal adicional era sólo un precio comercial: `plans.py` lo decía en un comentario y nada la contaba, la cobraba ni la limitaba.
- Qué ya existía (se miró antes de diseñar): `PLAN_PRECIOS` en el `plans.py` de cada producto, leído por `admin.services.planes_info()` y expuesto por el backoffice en `GET /api/planes`; el token de servicio (`X-Internal-Auth`) con el que el backoffice le habla a cada instancia; el patrón de config de la instancia en `config_manager` (`config.json`) con un router propio para la lectura/escritura (`tema_router`, ADR-012); la guarda inyectada de `resumen_router` (LibraCore no depende del motor de auth); `AltaIncompleta`/`crear_cliente` como único camino del alta, que ya siembra el `config.json`.
- Decisión (las tres, del dueño):
  1. **Se cobra lo contratado, no lo usado.** La cantidad de sucursales contratadas es un dato de la instancia que se carga desde el backoffice; el sistema calcula el abono `base + max(0, contratadas − incluidas) × adicional` (`libracore.abono.calcular_abono`, puro) y muestra «activas vs contratadas».
  2. **Si se supera lo contratado, se BLOQUEA el alta** (y la reactivación): con la sucursal nueva, las activas no pueden superar las contratadas. Mensaje para el cliente, en castellano: «Tu plan incluye N sucursal(es). Para sumar otra, comunicate con nosotros.». Lo que ya existe no se toca: si el contrato se achica por debajo de las activas, siguen andando; sólo se frena sumar.
  3. **Sin dato cargado, no hay límite**, para no romper ninguna instancia existente ni las demos. El backoffice lo muestra como «Sin cargar». El alta de una instancia nueva pide el número (default 1).
- Dónde vive el dato:
  - **En la instancia** (clave `limite_sucursales` de su `config.json`) y no en el `cliente.json` del host. Es lo que permite que la instancia bloquee un alta sin leer archivos del host ni llamar al backoffice (que puede estar caído: ADR-012 ya fijó que la instancia sigue funcionando sola). Fuente única: el backoffice, el cliente y el bloqueo leen el mismo número. Se descartó guardarlo en el `cliente.json` y empujarlo: serían dos copias que se desfasan.
  - Un valor ilegible (editado a mano, un texto, un cero) se lee como «sin dato»: nunca bloquea ni rompe la lectura. La clave se escribe sólo por `fijar_sucursales_contratadas` (entero 1..1000) o por el alta.
- Contrato:
  - `GET`/`PUT /api/limites/sucursales` (`build_limites_router(guard_lectura=, guard_escritura=, medir_activas=)`): `{contratadas: int | null, activas: int}`; el `PUT` recibe `{"contratadas": entero | null}` (`null` quita el dato). **Las guardas las inyecta el producto y son distintas a propósito**: leer es de admin **o** del token de servicio (el cliente puede ver «activas vs contratadas»); **escribir es sólo del token de servicio**, porque el tope es un límite comercial y un admin que pudiera cargar su propio número lo dejaría sin efecto. El medidor de activas lo pasa el producto (sabe dónde viven sus sucursales).
  - `verificar_alta_de_sucursal(activas_actuales)` levanta `LimiteExcedido` (con el mensaje); el producto la llama desde su gancho de alta **y** de reactivación y la traduce a su respuesta HTTP.
  - `planes_info()` suma `adicional` (`{"unidad", "incluidas", "precio"}` o `None`), leído con `getattr(plans, "PLAN_UNIDADES_ADICIONALES", {})`: es una costura opcional y los otros productos no la declaran.
  - `admin.services.plan_vigente(plan)` resuelve el plan de un `cliente.json` viejo (un plan retirado pasa a su reemplazo) para poder buscarlo en `planes_info()`: sin eso, una instancia que quedó guardada como `basico` aparecería como «el plan no cobra sucursales adicionales».
  - El alta (`crear_cliente(sucursales_contratadas=)`) escribe `limite_sucursales` en el `config.json` **antes** de levantar el contenedor: la instancia nace con su tope, sin la carrera de empujárselo por HTTP a una que todavía no contesta.
- Consecuencias:
  - MINOR (propuesta v1.153.0), **sin migración**. Nada cambia hasta que un producto monte el router y alguien cargue un número; los otros productos no hacen nada.
  - El tope lo hace cumplir el producto en su gancho de alta: LibraCore no conoce las sucursales de nadie. Una sucursal creada por fuera de ese gancho (un script, la base a mano) no pasa por la verificación.
  - Dos escrituras simultáneas del `config.json` (la carga del límite y otra pantalla de configuración) se pisan como siempre en este módulo (leer-modificar-guardar); es un dato que cambia pocas veces al año.
  - Fuera de alcance: facturar el abono (el sistema lo calcula y lo muestra, no cobra), un tope para otras unidades (usuarios, depósitos) y avisos al cliente al acercarse al límite.


## ADR-042 — Los restos de datos del resumen de cuenta corriente por mail se borran con una migración (`0024_retirar_cc_resumen`)

- Estado: aceptada
- Fecha: 2026-10-12
- Contexto: la v1.154.0 retiró `libracore/cc_resumen.py` (el envío periódico del resumen de cuenta corriente por mail) por ser código muerto: ningún producto, script ni router de la familia lo importaba, y su llave maestra (`cc_resumen_habilitado`) venía en `0`. Quedaron a propósito los datos —la tabla `cc_resumenes_enviados`, las tres columnas `clients.cc_resumen_*`, las claves del `config.json` y `generate_pdf_resumen_cc`—, porque tocar el schema es otra decisión. El dueño la tomó el 2026-10-12: «los borramos con una migración».
- Qué ya existía (se miró antes de diseñar): el patrón de revisión que llama a `init_core_schema()` y se apoya en que sea idempotente (`0001`, `0017`, `0023`); `alters_para_hora_ar`, que ya tolera una tabla ausente (lee las columnas reales de la base); y la regla del congelamiento de `init_core_schema` (`tests/db/test_schema_congelado.py`: un cambio a la función obliga a regenerar las fixtures y a preguntarse si no es una revisión). Para SQLite, que no tiene `DROP COLUMN IF EXISTS`, se sigue el criterio de `0010` y `0011`: preguntar por las columnas que hay antes de tocarlas.
- Decisión:
  - **Una revisión, `0024`, y no sólo sacar el DDL de `init_core_schema()`**: sacarlo evita que una base nueva tenga los restos, pero no los saca de las bases que ya los tienen, y esas son las que importan. Por eso la función y la revisión cambian juntas, y el gate del schema congelado (fixtures regeneradas) lo registra.
  - **`DROP TABLE IF EXISTS cc_resumenes_enviados`** (se lleva el índice y la FK) y **`DROP COLUMN` de las tres columnas**, con `IF EXISTS` en PostgreSQL y preguntando por las columnas en SQLite. El CHECK `clients_cc_resumen_frecuencia_check` se nombra aparte en PostgreSQL. Sin `CASCADE`: si algún objeto de una instancia (una vista a mano) dependiera de esas columnas, la migración falla en lugar de llevárselo puesto.
  - **No se baja**: el historial de envíos y las preferencias de cada cliente se pierden y recrear la estructura vacía sería fingir que volvieron. El rollback es restaurar el backup (como `0001`, `0017` y `0023`).
  - **La `0003` y la `0018` no se editan**: nombran la tabla (`created_at`, `saldo`) pero sólo actúan sobre las columnas que existen, así que una base nueva —que ya no la tiene— las atraviesa. `0003` lleva un comentario que lo dice; `COLUMNAS_DE_DINERO` pierde la fila porque es la lista viva, no un registro histórico.
  - **El `config.json` no se migra**: es un archivo por instancia, no una tabla. Las claves `cc_resumen_*` salen de `DEFAULTS` y las que haya guardadas quedan inertes.
  - **`update_client` pierde dos kwargs** (`cc_resumen_auto`, `cc_resumen_frecuencia`). Se aceptó el corrimiento de los argumentos posicionales siguientes porque ningún consumidor llama con más de `client_id` posicional; quien lo haga tiene que pasar por nombre.
- Consecuencias:
  - MINOR (propuesta v1.155.0), **con migración, y borra datos**. Orden: primero el pin con el código nuevo, después la `0024`; al revés, el `update_client` de la versión anterior escribe columnas que ya no están.
  - LibraDesk mapea las mismas columnas y crea la misma tabla en su propio modelo; las saca en su repo, con su revisión. Sobre una base que corre las dos cadenas, el `IF EXISTS` de cada una hace que no se pisen.
  - Fuera de alcance: las claves `cc_resumen_*` ya guardadas en los `config.json` de las instancias, y `get_cc_movimientos_periodo`, que sigue siendo una lectura válida de la cuenta.

## ADR-043 — La auditoría de escrituras vive en el motor: una costura transaccional (`libracore.auditoria`) y el CRUD de clientes la emite

- Estado: aceptada
- Fecha: 2026-10-12
- Contexto: el motor escribe `clients` en Contalibra, Restolibra y VentaLibra (y lo comparte LibraDesk), y **no dejaba rastro de quién la tocó**. LibraDesk comparte la tabla desde su revisión `0017` pero la escribe por su ORM, a propósito, porque el log de `libraauth` cuelga del `flush`: adoptar `db/clients.py` habría perdido la auditoría. Y `libracommerce` tenía una segunda copia del formato del log que ya había divergido (otras columnas ocultas, `created_at` ignorada, `entidad_id INTEGER`). El dueño decidió el 2026-10-12 unificar: el CRUD de clientes de LibraDesk sobre `libracore.db.clients` sin perder la auditoría, cableada en LibraDesk, VentaLibra, Contalibra y Restolibra.
- Qué ya existía (se miró antes de diseñar): el formato y los dos caminos de `libraauth.auditoria` (ADR-022 de ese repo: `registrador_dbapi`), los ganchos `configure_resolver_receta` (`db/core.py`, callable global por proceso) y `al_cambiar` del router de ARCA (best-effort, después del hecho), y `validar_cuit_no_duplicado`, ya separada de `create_client` para productos que escriben por su cuenta.
- Decisión:
  - **`libracore/auditoria.py`**: `Cambio` (dataclass congelada: `tabla`, `accion`, `id`, `antes`, `despues`), `configurar_auditoria(registrador | None)` (global por proceso e idempotente), `auditando()` y `registrar(conn, tabla, accion, id, antes, despues)`. El motor no sabe dónde se guarda el log: el producto enchufa un registrador `(conn, cambio) -> None`. **`libracore` y `libraauth` siguen sin depender uno del otro**: el contrato es un callable y un duck typing.
  - **Transaccional, a propósito.** `registrar` va dentro del mismo `with get_connection()` de la escritura, con su conexión. Si el INSERT del log falla, se cae la escritura entera, incluido el espejo en `parties` y el asiento del libro. Es lo contrario de `al_cambiar`: ahí el hecho ya ocurrió y el aviso es un accesorio; acá el hecho es la escritura, y un log incompleto se ve igual que uno completo. El precio: un log roto (tabla ausente, columna de menos) bloquea las altas y ediciones de clientes en vez de perderlas en silencio. Se prefirió que sea ruidoso.
  - **Sin registrador, ni un `SELECT` de más**: quien instrumenta pregunta `auditando()` antes de leer la fila de antes. Los productos que no auditan ven exactamente lo de antes.
  - **Se instrumentan todas las escrituras de `clients`**, con `antes` y `después` leídos en la misma conexión: `create_client`, `update_client`, `desactivar_cliente` y `activar_cliente` (una edición `{activo: [1, 0]}`), `toggle_auto_facturar`, `resolver_cliente_externo` (alta, o edición del nombre) y `delete_client` (la fila se lee **antes** del `DELETE`). Un id que no existía no se registra.
  - **`crear_cliente(datos)` y `actualizar_cliente(id, cambios)`**: escriben exactamente las claves recibidas (`None` = NULL) y aceptan cualquier columna que exista en la instancia (`_columnas_de_clients`); una desconocida —o `id`— es `ValueError` antes de escribir. Son la costura para lo propio de un producto (`price_list_id` de LibraDesk) y para `activo` en el alta. Mantienen lo que `create_client`/`update_client` hacen alrededor (espejo en `parties`, libro, aviso). **No** validan CUIT duplicado: `validar_cuit_no_duplicado` ya está aparte. `create_client` y `update_client` **no cambian de comportamiento** (ni la semántica `None` = no tocar ni el `NULL` → `""` de empresa/ciudad/observaciones/tipo_facturacion): Contalibra, Restolibra y VentaLibra no se enteran.
  - **Un test guarda el patrón** (`tests/test_auditoria_toda_escritura_de_clients.py`): recorre el AST de `libracore/` y falla si una función con `INSERT`/`UPDATE`/`DELETE` sobre `clients` no llama a `registrar`. El escáner se prueba contra fuentes de juguete para que no pueda pasar vacío.
  - **La pantalla de Logs de Contalibra y Restolibra muestra el log sin pantalla nueva**: `logs.PARTE_AUDITORIA` (un `UNION` más sobre `actividad_log`) entra en `PARTES_CORE` y `PARTES_LIBRACORE`, y `libracommerce.erp.actividad.partes_de_comercio()` la toma sola. Como la de cierres diarios, **se saca si la tabla no existe**. Tipo fijo `actividad` (no la entidad, para no chocar con `venta` ni `stock`), `ts` con `CAST` a texto (la tabla de `libracommerce` lo tiene `TEXT` y la de `libraauth` `timestamp`), usuario traducido de login a `usuarios.nombre`, y la acción dicha en la descripción (`Cliente — Ana (edición)`): el detalle del diff no se ve ahí.
- Consecuencias:
  - MINOR (propuesta v1.156.0), **sin migración**. Sin registrador configurado, el único cambio observable es la fila extra en la línea de tiempo cuando la instancia ya tiene `actividad_log` con filas (Contalibra y Restolibra la tienen por el DDL de `libracommerce`, vacía hasta que alguien audite) y la entrada «Cambios» en `tipo_meta` de `/api/logs`.
  - El producto cablea: crear la tabla donde escribe la conexión del motor (`libraauth.crear_tabla_actividad_log` si no la tiene ya), `configurar_auditoria(registrador_dbapi({"clients": "cliente"}, ...))` al arrancar (después de `core.configure`) y un `configurar_auditoria(None)` en la limpieza de sus tests.
  - Un valor se informa como lo devuelve la base: `activo` es `1`/`0`, no `True`/`False`.
  - Fuera de alcance: las demás tablas del motor (`facturas`, `remitos`, `caja_movimientos`…) no se auditan; el AST guard cubre `clients` y la regla se extiende agregando la tabla a su regex.

## ADR-044 — El motor queda listo para varios procesos por instancia: candados entre procesos (`libracore.candados`), nota de crédito con candado de base y `config.json` atómico

- Estado: aceptada
- Fecha: 2026-10-13
- Contexto: el motor suponía un proceso por instancia (`uvicorn` sin `--workers`) y tres cosas se apoyaban en eso. La nota de crédito exigía «una a la vez» con un `set` en memoria («alcanza porque un solo proceso»): con N workers un doble clic cae en dos procesos y emite **dos notas ante ARCA**. Los hooks de arranque («si no existe, crear») corren en cada worker. Y `config.json` se escribía truncando el archivo. El dueño decidió el 2026-10-13 arreglar todo lo que se rompe con N workers **sin prender más workers por ahora**: todo sigue en 1, el código queda listo para `WEB_CONCURRENCY`.
- Qué ya existía (se miró antes de diseñar): `libracommerce` ya serializa con `pg_advisory_xact_lock` (`erp/compras.py`, `erp/reposicion_ordenes.py`) con una clave derivada de SHA-256; `arca_wsaa._cerrojo` es el `flock` entre procesos de la familia; `arca_certificados` y `arca_pedidos` ya escriben con `os.replace`; `respaldo_postgres.vigilar_pools` descarta conexiones viejas tras un restore.
- Decisión:
  - **`libracore/candados.py`.** `candado_de_base(conn, nombre)` (no bloqueante, entrega si lo obtuvo) y `una_vez_al_arrancar(nombre, destino=None, espera=120)` (bloqueante con conexión propia). PostgreSQL: `pg_try_advisory_xact_lock` / `pg_try_advisory_lock`; SQLite: `flock` sobre un archivo de `DATA_DIR`; sin conexión: un candado del proceso. La clave sale de SHA-256 (56 bits), no de `hashtext()`: es la misma en cualquier driver y generaliza lo que `libracommerce` ya hace. Acepta la conexión de `libracore.db`, `psycopg` y `Session`/`Connection` de SQLAlchemy (LibraCargo es SQLAlchemy y es el consumidor real de la nota).
  - **El candado de transacción dura hasta el fin de la transacción de `conn`**, y eso es lo buscado: la nota recién creada ya es visible cuando el pedido siguiente entra. El costo es una regla para el producto: no hace `commit`/`rollback` de `conn` hasta terminar la emisión.
  - **`una_nota_a_la_vez(clave, conn=None)`** conserva el candado en memoria como primera capa y suma el de la base si hay `conn`. `emitir_nota_de_credito(..., conn=None)` se lo pasa. Sin `conn`, o con SQLite, queda lo de hoy. **El router de facturas del motor** (`_emitir_nota_de_credito`) abre una conexión aparte, sin escribir nada, durante toda la emisión (sus `db_facturas` abren y cierran la suya por llamada y el candado de transacción moriría con ella), sólo si la instancia es PostgreSQL.
  - **`config_manager._escribir_atomico`**: temporal en el mismo directorio, `fsync`, `os.replace`, conservando los permisos. Lo usan `save()` y la limpieza de `migrar_secretos_al_almacen()` (que corre en el arranque, o sea en cada worker).
  - **Restore en caliente: sin SIGHUP.** `respaldo_postgres._generacion` sólo alcanza al proceso que restauró. Se documenta (docstrings y `docs/varios-procesos.md`) que con más de un worker hay que reiniciar el contenedor después de un restore, y que los engines del producto llevan `pool_pre_ping=True`. El motor no crea engines de aplicación (el único, el de `migrations/env.py`, usa `NullPool`), así que no hay nada que cambiar acá.
  - **Un test de integración reutilizable** (`libracore.testing.varios_procesos` + `libracore.testing.app_varios_procesos`) levanta `uvicorn --workers 2` de verdad contra PostgreSQL: 20 pedidos simultáneos de nota ⇒ 1 emitida (la contraprueba sin `conn` da 2, una por proceso) y el arranque con `una_vez_al_arrancar` ⇒ 1 fila (sin él, 2). `uvicorn>=0.30` entra en el extra `dev`.
- Consecuencias:
  - MINOR (propuesta v1.158.0), **sin migración**. Todo es opt-in: sin `conn` la nota de crédito se comporta como antes, y nada llama a `una_vez_al_arrancar` hasta que un producto lo cablee.
  - Qué hace cada producto: envolver sus hooks de arranque, pasar `conn` a la nota, `UsadosEnBase` del captcha de `libraauth`, `pool_pre_ping=True` y `--workers ${WEB_CONCURRENCY:-1}` con `uvicorn>=0.30` (ver `docs/varios-procesos.md`).
  - Fuera de alcance: el *last write wins* de `config.json` entre dos admins que editan a la vez (pierde una edición, ya no corrompe el archivo); los datos que el producto guarde en archivos propios; y prender más workers.

## ADR-045 — El registro de errores es del motor: una línea JSON por error en `DATA_DIR/errores/` (`libracore.errores`)

- Estado: aceptada
- Fecha: 2026-10-12
- Contexto: **ningún repo de la familia tiene un handler común de excepciones.** Un 500 va al stdout de uvicorn y se pierde en cada recreate del contenedor (cada `actualizar` recrea la instancia): un cliente real puede tener un error recurrente durante semanas y nadie se entera hasta que llama. El dueño decidió el 2026-10-12: registro **propio** en el motor (no GlitchTip ni Sentry), avisos **sólo por Telegram** y sin datos sensibles. Esta es la fase del motor; el aviso (vigía del host) y el navegador (`libra-ui`) son aparte.
- Qué ya existía (se miró antes de diseñar): `SecurityHeadersMiddleware` (`security_headers.py`), que los 10 apps instalan con una línea y de la que salen los 8 `*-admin` por `admin/app.py`: la costura análoga. Los patrones de `DATA_DIR` (`config_manager`, `arca_wsaa._dir_de_tickets` con su `ARCA_TA_DIR`), el `env_para_compose` de `mail_cuentas` para inyectar variables en el compose generado, `pinear_image` como único punto por el que pasan `actualizar`/`rollback`, `directorios_de_datos` del respaldo, y `logs_router` (que es el log de **actividad** del negocio, no de fallas: no se reutiliza). No había nada de errores, de Sentry ni de `sys.excepthook` en el motor ni en los productos.
- Decisión:
  - **`libracore/errores.py`**: `instalar_registro_de_errores(app, producto, *, usuario_id=None, endpoint_cliente=True)`, `capturar(exc, *, request=None, status=None, usuario_id=None, tags=None, **contexto) -> ref`, `build_errores_cliente_router(producto=None)` y `limpiar_texto`/`limpiar_query`. Una línea por producto lo instala todo, y los 8 backoffices lo traen por `create_admin_app`.
  - **Handler de `Exception`**: responde `500 {"detail": "Error interno", "ref": "<8 hex>"}` y registra. No registra `HTTPException`, `RequestValidationError` ni `ClientDisconnect`: los 4xx son del cliente y taparían los errores del sistema. Uvicorn sigue imprimiendo el traceback (Starlette re-lanza después de contestar).
  - **Formato**: una línea JSON en `DATA_DIR/errores/errores.jsonl` (`LIBRA_ERRORES_DIR` lo mueve), rotación 5 MB × 3 con `RotatingFileHandler`. La forma sigue los nombres de Sentry (`release`, `entorno` ≡ `environment`, `tags`, `firma` ≡ `fingerprint`, `usuario_id` ≡ `user.id`) para que pasar a GlitchTip más adelante sea tocar sólo este módulo. **JSON por línea y no una tabla**: el handler tiene que poder escribir justo cuando la base es lo que falló.
  - **Registrar nunca rompe la respuesta.** Todo está envuelto: sin permisos o disco lleno, la línea va al stdout; si la limpieza falla, se escribe un evento mínimo. Un test lo fija para cada camino.
  - **La `firma` agrupa por lugar**: sha1 de `tipo` + `ruta` (plantilla, `route.path`, nunca la URL con el id) + `archivo:función` del frame más profundo que **no es de terceros**. `libracore`, `libraauth`, `libracommerce`, `libragenda` y `libraedge` cuentan como propios aunque estén en `site-packages`: son donde vive la mayor parte de la lógica y firmar por un frame de Starlette agruparía todo en uno. Sin números de línea: un deploy que mueve el código no parte un error en dos.
  - **Limpieza por lista negra, en el origen**: nunca body, cookies, `Authorization` ni cabeceras (salvo `user-agent`); la query con las claves `token|password|passwd|clave|secret|cuit|cert|key|dni|cbu|authorization|cookie` enmascaradas; regex sobre mensaje y traceback (CUIT/DNI de 7 a 11 dígitos con o sin separadores, mails, PEM, `usuario:clave@` de cualquier URL, JWT, `Bearer`, el `DETAIL: Key (…)=(…)` y el `Failing row` de psycopg, los `[parameters: …]` de SQLAlchemy, `clave=valor`); sin variables locales; truncado a 8 KB. Se prefirió **perder diagnóstico antes que dejar pasar un dato**: una corrida cualquiera de 7 a 11 dígitos se enmascara aunque sea un timestamp. Del traceback se sacan los frames del framework anteriores al primero propio (si no, ~20 frames de Starlette se comen el tope de 8 KB).
  - **`POST /api/errores-cliente`**, público a propósito (un error del login también importa) y defendido por lo que exige: mismo origen, ≤ 4 KB, 20 pedidos / 10 min por IP y 300 / hora global (en memoria), la misma limpieza, y se guardan sólo campos fijos —no el JSON recibido—. La IP es la **última** de `X-Forwarded-For` (la agrega NPM; lo anterior lo escribe el cliente).
  - **`LIBRA_INSTANCIA`, `LIBRA_ENTORNO` y `LIBRA_VERSION` las escribe el panel** (`provisioning.errores_env`): el alta las pone en el compose y `pinear_image` las mantiene, de manera idempotente. **La versión es el tag pineado**, no un `ARG` ni la etiqueta de la imagen: la imagen se construye antes de saber a qué instancias va, la etiqueta no se ve desde el contenedor, y `pinear_image` es por donde pasan `actualizar`, `rollback` y el repineo de un deploy fallido, así que la variable no puede quedar distinta de la imagen. `LIBRA_ENTORNO` es `demo` para el slug `demo` y `cliente` para los demás, y el pin no la pisa si ya está. Un compose sin `environment:` en lista no se toca más que en la imagen.
  - **`errores/` queda fuera del ZIP del respaldo** (`respaldo.CARPETAS_FUERA_DEL_ZIP`): `directorios_de_datos` toma todas las carpetas de `data/` salvo `backups/`, y sin esto cada ZIP de cada cliente cargaría hasta 20 MB de registro de operación, y un restore los repondría.
- Consecuencias:
  - MINOR (propuesta v1.159.0), **sin migración**. Un producto que no instala la línea no cambia en nada. El que la instala ve los 500 con cuerpo JSON (antes texto plano `Internal Server Error`) y un archivo nuevo en `data/errores/`.
  - Las instancias de cliente y demo ganan las variables en su próximo `actualizar`; hasta entonces sus líneas dicen `entorno: desconocido`. Las `-dev` las llevan en el compose de su repo.
  - Los 500 que contesta el handler no pasan por `SecurityHeadersMiddleware` (Starlette los arma desde afuera de la pila): llevan sólo `X-Content-Type-Options` y `Cache-Control: no-store`.
  - La limpieza es una lista negra: **lo que no está en ella pasa.** Un dato personal que aparezca en una línea real se corrige agregando el patrón y un caso al test.
  - Fuera de alcance: el aviso por Telegram (`operaciones/vigilar_errores.py` del wiki), el `<ErrorBoundary>` y el reporte del navegador (`libra-ui`), y el cableado de cada producto (una línea en el backend, el wrap en `main.tsx`, `LIBRA_ENTORNO=dev` en su compose de dev).

## ADR-047 — El concepto de la factura a cuenta corriente deja de decir «Factura» dos veces, y las filas viejas se corrigen con una migración (`0025_concepto_sin_factura_doble`)

- Estado: aceptada
- Fecha: 2026-10-13
- Contexto: la emisión a crédito (`facturas_router`) escribía en `caja_movimientos.concepto` `"Factura Factura C 0001-00000001 — Juan"`: la etiqueta del tipo (`TIPO_LABEL` = `tipos_comprobante.NOMBRE`, «Factura C») ya trae la palabra y el texto se la antepone otra vez. Venía de las copias de Contalibra y Restolibra y se preservó a propósito al extraer el módulo (un test lo fijaba) porque una extracción no puede cambiar lo que se escribe en la base. Quedó como pendiente abierto desde 2026-08-27 con una pregunta: qué hacer con las filas viejas. Medido en producción el 2026-10-13 (sólo lectura): Compulibra tiene 34 filas en `caja_movimientos` y 33 en `cc_asientos` con `concepto LIKE 'Factura Factura %'`; LibraDesk (dos instancias) y LibraCargo, 0. Decidió el dueño: «corregí y migrá las filas viejas».
- Qué ya existía (se miró antes de cambiar): el `f-string` está en un solo lugar (`facturas_router`, la emisión); los demás conceptos que nombran un comprobante no repiten la palabra (`cobros.concepto_de_cobro` arma `Cobro <etiqueta> …`, `mp_facturacion` `Cobro <etiqueta> … (MP)`, la nota de crédito `NC <n°> (anula <etiqueta> <n°>)`). Ningún producto de la familia (`git grep` en los diez repos) arma un concepto con `Factura {…}`. El libro de clientes (`cc_asientos`, ADR-029) **copia** el concepto del movimiento de caja al asentar (`libro_de_clientes._esperado`, rama `caja_mov`) y `sincronizar` sólo compara el importe: el asiento no se entera de que la caja cambió. La pantalla (`movimientos_de`) reconstruye el texto de la factura cuando la fila de caja existe, así que la duplicación se veía sólo en asientos sin su fila de caja y en lo que lea `cc_asientos` directo (extractos, exportaciones, la consulta de un tercero).
- Decisión:
  - **El concepto es el nombre del comprobante**: `f"{tipo_label} {pv}-{num} — {cliente}"` → `Factura C 0001-00000001 — Juan`. El test que fijaba el doble «Factura» ahora fija el formato correcto, y otro (para A, B y C) impide que vuelva, en la caja y en el libro.
  - **Migración `0025`**: `UPDATE … SET concepto = substr(concepto, 9) WHERE substr(concepto, 1, 16) = 'Factura Factura '` en `caja_movimientos` y en `cc_asientos`. Sólo las filas que empiezan **exactamente** así (distingue mayúsculas; no se usa `LIKE`, que en SQLite no las distingue), sin tocar montos, fechas ni vínculos. Idempotente. Si una base no tiene alguna de las dos tablas, la saltea. Escribe a stderr cuántas filas tocó por tabla. No se baja: sin el backup no hay forma de distinguir una fila corregida de una que ya salía bien.
  - **No se toca** lo que no empieza con el prefijo: `Cobro Factura C …`, las reversiones del libro (`Reversión: Factura Factura C …`, ver abajo) ni las FCE (`Factura FCE MiPyME C …`, ver abajo).
- Consecuencias:
  - MINOR (propuesta v1.160.0), **con migración de datos** (sin cambio de schema). Corre al subir el pin; **backup antes** (regla de migraciones). Es una corrección de texto: el saldo de ningún cliente cambia.
  - Las instancias con filas viejas son las de Compulibra; el resto no cambia nada.
  - El extracto de un cliente pasa a mostrar un solo formato. Una exportación o un reporte hecho antes de la migración que filtre por el texto viejo deja de encontrarlo.
  - Pendiente menor, no decidido acá: `contraasentar` antepone `Reversión: ` al concepto del original, así que un asiento revertido de los viejos queda `Reversión: Factura Factura C …`; y, desde que el nombre de la FCE es `FCE MiPyME C`, las filas viejas de FCE a crédito dicen `Factura FCE MiPyME C …` y las nuevas `FCE MiPyME C …`. Ninguna es una palabra duplicada y ninguna entra en la regla pedida; si se quieren unificar, es otro `UPDATE` con su prefijo exacto.

## ADR-046 — Un alta que queda sin casilla de correo lo dice: la CLI del host lee su configuración de un archivo, el aviso es explícito y `panel_admin.py casilla` la crea o la repara; se retira `get_cc_movimientos_periodo`

- Estado: aceptada
- Fecha: 2026-10-13
- Contexto: el alta crea la casilla de correo saliente de la instancia (`mail_cuentas`) **sólo si el proceso que da de alta tiene `LIBRA_MAIL_ADMIN_SSH` y `LIBRA_MAIL_DOMINIO`**. Los ocho backoffices las reciben por el `environment:` de su compose; el host del VPS no. Un alta con `.venv-scripts/bin/python3 scripts/nuevo_cliente.py` llama a la **misma** `crear_cliente`, pero `configurado()` da `False`, no se intenta nada y la respuesta trae `smtp_ok=None` («no se intentó»). `main()` ni siquiera lo miraba: la instancia nacía sin SMTP (`/auth/forgot-password` contesta 503, nada se envía) y nadie se enteraba. Visto el 2026-08-30 y anotado el 2026-09-11 como pendiente abierto.
- Qué ya existía (se miró antes de diseñar): `mail_cuentas` entero (`crear_cuenta`, `borrar_cuenta`, `env_para_compose`, `configurado`), el rollback de la casilla del alta (`_rollback_casilla`), el patrón de «archivo del host que el alta lee si puede» (`RESGUARDO_OAUTH_ENV`, `/root/secretos/…`), `errores_env.con_variables` (editar el `environment:` de la app de un compose existente) y `pinear_image`/`_image_de_la_app` (encontrar el servicio de la app sin caer en el sidecar). El wrapper del servidor de correo (`vps-correo-libra/libra-mail-cuenta`) sólo sabe `crear` (falla si la casilla existe), `borrar`, `alias` y `desalias`: **no hay cambio de contraseña**. Lo que no había: ninguna forma de que el host declarara su configuración, ningún aviso, y ningún comando para una instancia existente.
- Decisión:
  - **La configuración del host es un archivo por producto**, `/root/secretos/correo-<container_prefix>.env` (o el que diga `LIBRA_MAIL_ENV_FILE`), con `LIBRA_MAIL_ADMIN_SSH`, `LIBRA_MAIL_DOMINIO` y `LIBRA_MAIL_SMTP_HOST`. **Por producto** porque el dominio de correo y el nombre del certificado lo son; un `export` en el shell del host no puede decir ocho cosas. El entorno del proceso **gana** sobre el archivo (el backoffice no cambia), y del archivo sólo se leen esas tres claves. No tiene secretos: un alias de SSH y dos dominios; la clave privada sigue en `~/.ssh` del host.
  - **El alta ya no es silenciosa.** `crear_cliente` devuelve `smtp_motivo` (por qué no hay casilla: lo que falta, o el último renglón del `stderr` del wrapper) y escribe un `[WARN]` en el log cuando no se intentó. `main()` cierra la CLI con un aviso después de las credenciales: «la instancia quedó sin casilla de correo: <motivo>», con el comando exacto para crearla, y sale con **código 3** (no 1: el alta salió bien). El backoffice no cambia de comportamiento: la clave nueva viaja en la respuesta y el producto decide si la muestra.
  - **`panel_admin.py casilla <slug> [--recrear]`**, idempotente: si el compose ya trae `LIBRAAUTH_SMTP_PASSWORD`, no hace nada; si no, pide la casilla, escribe las seis variables en el `environment:` de la app (las mismas líneas que el alta) y recrea la instancia con `up -d`. Si el servidor ya tiene la casilla pero el compose no, **se detiene y lo dice**: la contraseña no se lee de vuelta y recrearla borra el buzón (con sus mensajes, y el reenvío al correo del cliente si lo tenía), así que sólo `--recrear` lo hace. Si algo falla después de crear la casilla (compose ilegible, disco), la borra: nada de credenciales vivas que nadie conoce. La contraseña no sale por pantalla ni por log.
  - **`get_cc_movimientos_periodo` se retira.** Lo usaba el resumen de cuenta corriente por mail, que salió en v1.155.0 (ADR-042); esa decisión la dejó «como lectura válida». Se midió con `git grep` en `origin/develop` y `origin/main` de los doce repos de la familia: ningún llamador. Sólo quedan dos alias sin uso, `get_cc_movimientos_periodo = partial(_cc.get_cc_movimientos_periodo, …)` en Contalibra (`app/db_cuenta_corriente.py`) y `movimientos_periodo` en LibraDesk (`app/services/cuenta_corriente.py`), que **rompen el import** cuando suban el pin: se borran en el mismo bump. Su cálculo era trivial sobre `get_cc_movimientos`, que se queda; no deja helpers huérfanos.
- Consecuencias:
  - MINOR (propuesta v1.161.0), **sin migración**. Sin el archivo y sin las variables todo sigue igual salvo que ahora se avisa. El defecto de fondo se cierra cuando el host tenga sus ocho archivos (`reglas/infraestructura.md` del wiki): hasta entonces un alta por CLI sigue naciendo sin casilla, pero lo dice y trae el comando.
  - **Rompe a Contalibra y LibraDesk al subir el pin** (el alias apunta a una función que ya no está) hasta que borren esa línea. Restolibra, Gestiolibra, MedLibra, VentaLibra, LibraCargo, LibraClub, LibraEdge, LibraCommerce, LibraPanel y el backoffice no la nombran.
  - El host necesita además que `~/.ssh/config` de `root` resuelva el alias de `LIBRA_MAIL_ADMIN_SSH` con la clave `id_ed25519_libra_mail`: si no, `casilla` informa el error de `ssh` (no se verificó en el VPS desde esta sesión).
  - Una instancia que ya corre sin casilla se repara con `casilla <slug>` y **recrea su contenedor** (segundos de corte): hacerlo fuera de horario en las de cliente.
  - Fuera de alcance: crear las casillas de las instancias hoy sin SMTP (la medición del parque), un comando que recorra todas, y un cambio de contraseña en el wrapper del servidor de correo (con eso `--recrear` no tendría que borrar el buzón).


## ADR-048 — Los adicionales fuera de la escalera de planes: un catálogo (`libracore.adicionales`), el dato en la instancia y el abono por líneas

- Estado: aceptada
- Fecha: 2026-10-13
- Contexto: el módulo offline de RestoLibra (LibraEdge) se vende **por sucursal con offline**, fuera de la escalera Básico/Estándar/Premium, y el motor de planes no sabía expresarlo: `libraedge_integration.py` lo resolvía con una variable de entorno (`RESTOLIBRA_EDGE_NODE_ID`, la identidad del nodo, usada además como opt-in comercial) y dejaba anotado que «si aparece un segundo adicional así, conviene resolverlo en LibraCore». Decisión del humano (2026-10-13): resolver «el motor de planes no puede expresar un adicional fuera de la escalera».
- Qué ya existía (se miró antes de diseñar): la escalera en el `plans.py` de cada producto (`PLANES`, `PLAN_LABELS`, `PLAN_PRECIOS`, `PLAN_MODULOS`; VentaLibra, plan único `unico` + `PLANES_RETIRADOS`); `provisioning.resolver_plan` y `apply_plan_modules`; `db/modulos.apply_plan` (que saltea `plans.ADDONS`) y `set_addon` (fila `plan="addon"`); `modules_gate.require_module`; los **sí/no** `plans.ADDONS` (`resguardo_externo` en los 8, `mayorista` en Contalibra, `modo_simple` en LibraDesk) con `GET/PUT /instancias/{slug}/addons` del backoffice por `docker exec`; las **unidades** del ADR-041 (`PLAN_UNIDADES_ADICIONALES`, `limites.py` con `limite_sucursales` en el `config.json`, `abono.calcular_abono`, `build_limites_router`); los Términos v1.1, cláusula 23, que ya prevé módulos opcionales por Anexo con precio propio. Faltaba **un sí/no con cantidad** (offline) y un lugar único donde declararlos.
- Decisión:
  1. **Un catálogo, `plans.ADICIONALES`** (opcional): `{clave: {label, precio, modulos, unidad, incluidas, limite, planes}}`. `precio=None` es «a cotizar»: no se fija ningún precio nuevo. **Sin declararlo, `catalogo(plans)` lo sintetiza** desde `ADDONS` (sí/no) y `PLAN_UNIDADES_ADICIONALES` (unidades): ningún producto existente cambia. Lo declarado pisa a lo sintetizado.
  2. **El dato vive en la instancia**, como el ADR-041: los sí/no en la tabla `modulos` (`plan="addon"`, lo que ya lee `require_module`) y las cantidades en `config.json` `adicionales: {clave: n}`. `sucursal` **sigue** en `limite_sucursales` (decisión 2: el número cargado es el total contratado, sin cambio). Un adicional con módulo y cantidad (offline) guarda los dos y la cantidad manda. Un valor ilegible es «sin dato», nunca un error. El `cliente.json` del host guarda sólo una **copia informativa** (`meta["adicionales"]`); si difiere, **gana la instancia**.
  3. **Un cambio de plan nunca toca un adicional.** `apply_plan` y `apply_plan_modules` excluyen `modulos_de_adicionales(plans)`, un superconjunto de `ADDONS` (el offline no está en `ADDONS`). `set_plan` valida la compatibilidad con `planes` y, si un adicional contratado no cabe en el plan nuevo, responde **409** con la lista (`AdicionalIncompatibleError`) antes de aplicar nada. Los adicionales se pueden contratar desde cualquier plan salvo que el catálogo diga lo contrario (offline: cualquiera).
  4. **El abono es por líneas** (decisión 4): `resolver(plans, plan, contratados) -> {modulos, limites, lineas_abono, incompatibles}` y `calcular_abono_total(base, lineas) -> {base, lineas, extra, total, a_cotizar}`: una línea por adicional contratado que cobra algo, y una línea sin precio suma cero y va a `a_cotizar`. Es lo que se vuelca en el **Anexo I** (plan + una línea por adicional); los Términos no cambian. `calcular_abono` queda; un test fija que 3 sucursales dan `base + 2 × 19.950` por los dos caminos.
  5. **API de la instancia**: `build_adicionales_router(guard_lectura, guard_escritura, medir={clave: callable}, *, plans, leer_modulos, escribir_modulo)`: `GET /api/adicionales` (catálogo, `habilitado`, `cantidad`, `usadas`, `limites`) y `PUT /api/adicionales/{clave}` con exactamente uno de `{"habilitado"}` / `{"cantidad"}` (`null` = baja). **Escribir es sólo del token de servicio**: un adicional es un límite comercial y el admin del cliente no puede darse uno. `/api/limites/sucursales` queda como alias (misma clave del `config.json`).
  6. **Backoffice**: el camino normal es HTTP contra la instancia (lo hace `libra-backoffice`, que tiene el cliente y el token); `libracore.admin.services` aporta `adicionales_info()`, `planes_info()[i]["adicionales"]` (con las `incluidas` de ese plan), `set_plan(..., contratados=)` y, **de respaldo para una instancia vieja sin el router**, `set_adicional` / `adicionales_de_instancia` por `docker exec` (sólo sí/no; una cantidad no se carga así y el backoffice lo dice como `sin_soporte`). El alta (`crear_cliente(adicionales={...})`) siembra las cantidades en el `config.json` antes de levantar el contenedor y prende los módulos tras aplicar el plan (`provisioning.aplicar_modulos_de_adicionales`); si no puede, `AltaIncompleta`.
  7. **Hacer cumplir lo hace el producto**, con una guarda genérica: `verificar_alta(plans, clave, usadas_antes)` levanta `AdicionalNoContratado` (módulo apagado) o `AdicionalExcedido` (sobre la cantidad); `verificar_por_limite` es el verificador puro. Un módulo prendido sin cantidad cargada no limita (el criterio del ADR-041: sin dato no hay límite).
  8. **RestoLibra, offline por sucursal** (decisión 1): la unidad es la sucursal con offline; el tope es la cantidad de **sucursales distintas** (`node_identity.branch_id`, nodos activos) y varios nodos en la misma sucursal no suman. Los nodos ya tenían sucursal (`branch_id`, no nulo), así que no hace falta columna nueva: una migración de datos del producto deja `offline = sucursales distintas con nodo` para que nadie se corte. Detalle en el ADR de RestoLibra.
- Consecuencias:
  - MINOR (propuesta v1.162.0), **sin migración del motor** (la 0026 queda libre): `modulos` y `config.json` alcanzan. Los productos suben el pin cuando quieran; VentaLibra y RestoLibra declaran `ADICIONALES` y montan el router; los demás se sintetizan solos.
  - 🔴 **`db.modulos.apply_plan` usaba `1`/`0`** contra una `modulos.habilitado` que en LibraDesk, GestioLibra y MedLibra es BOOLEAN. Lo destapó el test de este ADR; ahora usa el tipo real de la columna (`_valor_habilitado`). Esos tres productos aplican el plan por `apply_plan_modules`, que ya convertía, así que no estaba roto en producción.
  - Una instancia vieja (libracore anterior) no expone `/api/adicionales`: el backoffice cae a `docker exec` para los sí/no y marca las cantidades como «hay que actualizar la instancia».
  - Un módulo no puede estar a la vez en un plan y en un adicional (`ValueError` al primer uso del catálogo): un plan lo prendería o apagaría solo.
  - Fuera de alcance: facturar el abono, fijar precios (todo `None` = a cotizar salvo la sucursal de VentaLibra), y los avisos al cliente al acercarse al tope.


## ADR-049 — La nota de débito por importe es del motor (`libracore.notas_de_debito`), igual que la de crédito

- Estado: aceptada
- Fecha: 2026-10-13
- Contexto: LibraCargo pasa a facturar directo por ARCA y suma la nota de débito desde una factura, con motivo e importe (decisión del humano, 2026-10-13). El motor ya tenía una nota de débito, pero dentro de `facturas_router._crear_nota`: privada, atada a la tabla `facturas` y **sin importe** —copia la factura entera con el prefijo «Referencia»—, mientras que la nota de crédito tiene un núcleo reutilizable (`notas_de_credito`, ADR-014) con costuras para cualquier modelo de datos. Regla del 2026-10-03: el arreglo de fondo vive en el motor; el producto no escribe su propia versión.
- Qué ya existía (se miró antes): `TIPO_ND` y `ND` en `tipos_comprobante`, `armar_nota` / `repartir_importe` / el candado `una_nota_a_la_vez` / `NotaEmitida` en `notas_de_credito`, `arca_wsfe.problema_del_receptor` (ya conoce `tipos.ND`) y `_opcionales_fce` (ya manda la marca de anulación a toda nota de FCE, ND incluida), `POST /{id}/nota-debito` del router (copia total), `libra-ui` `FacturaDetalle` (llama a esa ruta sin cuerpo). No existía una emisión de débito por importe ni con costuras.
- Decisión:
  1. **`libracore.notas_de_debito`** reutiliza el núcleo de la de crédito: mismas costuras (`numerar`, `registrar`, `pedir_cae`, `cargar_previas`), mismo orden, mismo candado con `conn`.
  2. **Va siempre por un importe, sin tope.** Una nota de crédito acredita parte de una factura y por eso tiene saldo; una de débito agrega plata y no está acotada por la factura. Por eso no hay «nota total», ni `SUPERA_SALDO`, ni «una sola vez».
  3. **El original tiene que tener CAE** (`ORIGINAL_SIN_CAE`): ARCA exige el comprobante asociado, y asociar uno que no conoce se descubre después de tomar el número. La de crédito no lo chequea en el motor (cada producto lo hace); la de débito lo trae adentro.
  4. **Aditivo en `notas_de_credito`**: `importe_valido` (extraído), `armar_nota(leyenda=, por_importe=)` y `una_nota_a_la_vez(clase=)`. La nota de crédito no cambia.
  5. **No se toca la ruta del router de facturas.** Sigue copiando la factura entera para Contalibra, Restolibra y LibraClub; migrarla es otra decisión (cambia lo que emiten tres productos).
- Consecuencias: MINOR (propuesta v1.163.0), **sin migración**. Sin medir contra ARCA de homologación: el armado es el de la nota de crédito parcial (medida) con los tipos `2/7/12` y `202/207/212`; falta confirmar con un ensayo que ARCA acepta la marca `N` en la nota de débito de una FCE. Un producto que la consuma pasa una `clave` propia y traduce `NotaNoPermitida.codigo` a HTTP.

## ADR-050 — Cada envío por mail de un comprobante o un presupuesto deja rastro (`envios_email`)

**Estado:** aceptada (2026-10-14). Pedido del humano: en ContaLibra un cliente reclamaba facturas y no había forma de saber si se habían mandado.

**El hueco.** `POST …/enviar-email` (facturas y presupuestos) mandaba el PDF por SMTP y devolvía `ok` o un 502, sin guardar nada. Ni la fecha, ni el destinatario, ni el error. El único rastro posible era la carpeta de enviados de la casilla SMTP, fuera del sistema.

**Qué se hizo.** Tabla `envios_email` (`libracore.db.envios_email`, migración `0026`): una fila por **intento**, con `documento_tipo` (`factura` | `presupuesto`), `documento_id`, `documento_label`, `destinatario`, `estado` (`enviado` | `error`), `error`, `usuario` y `created_at`. Sin FK a propósito: apunta a dos tablas y el rastro sobrevive a un borrado (por eso guarda la etiqueta). `registrar` traga sus propios errores: si falla escribir el rastro, el mail ya salió y el usuario no tiene que ver un error por eso. Lectura: `GET {prefix}/{id}/envios`, `envios_email` en el detalle, y `envio_email` en cada fila del listado (resumen en **una** consulta por página, no N+1).

**Qué no.** Ni Bcc al remitente ni reintentos automáticos. Los envíos anteriores no se reconstruyen. El listado no filtra por envío (el resumen va por fila; filtrar pediría tocar `get_facturas_filtradas`): queda para cuando se pida.

**Tests.** `test_el_envio_por_mail_deja_rastro_enviado_o_con_error` (facturas) y `test_cada_envio_deja_rastro_y_el_listado_lo_marca` (presupuestos): un fallo y un éxito, orden, motivo, usuario y resumen del listado. El del router chico suma `GET …/envios` a sus rutas.

## ADR-051 — El listado de comprobantes filtra por «enviada» y «sin enviar»

**Estado:** aceptada (2026-10-14). Pedido del humano, a continuación del ADR-050: con el rastro de `envios_email`, poder listar de una vez qué se mandó y qué no.

**Qué se hizo.** `get_facturas_filtradas` y `GET /api/facturas` reciben `envio`. Es una condición más del `WHERE` (un `EXISTS` sobre `envios_email`), así que el total y la paginación la respetan y se combina con la vista, las fechas y la búsqueda.

- `enviada`: salió al menos un mail (`estado = 'enviado'`).
- `sin_enviar`: no salió ninguno. **Un intento fallido cuenta como no enviado**, porque el cliente no lo recibió. Se limita a los comprobantes con CAE (la misma condición de la vista `sin_cobrar`), porque un borrador sin autorizar no es algo que se le reclame a nadie.
- Otro valor o vacío no filtra, como el `estado` de presupuestos.

**Qué no.** Un comprobante enviado **antes del 2026-10-14** aparece como «sin enviar»: el rastro empieza con el ADR-050. Presupuestos no tiene este filtro: su listado ya filtra por estado, y mandarlo lo pasa a `enviado`.

**Tests.** `test_el_listado_filtra_por_enviada_y_sin_enviar` (SQLite) y `test_el_filtro_de_envio_en_postgres`: una enviada, una con envío fallido y una sin envío.

## ADR-052 — La lectura de `/api/limites/sucursales` trae el texto del tope (`mensaje_de_limite`), para que la pantalla no lo copie

**Estado:** aceptada (2026-10-14). Pendiente del humano en VentaLibra («`mensajeDeLimite` desde libracore»), que dejó el ADR de `libra-ui` 0.136.0 en «lo que no cubre».

**Contexto.** El 409 del alta de una sucursal que pasa lo contratado dice `mensaje_de_limite(contratadas)` (ADR-041). La pantalla `Sucursales` del kit (`libra-ui` 0.136.0) frena el botón «Nueva sucursal» al llegar al tope y muestra el motivo **antes** de que haya un 409, así que necesitaba el mismo texto: lo escribió a mano en `mensajeDeLimite` (`Sucursales.tsx`), «copia del motor; si cambia allá, cambia acá». Dos frases que tienen que ser una sola se desfasan en silencio; el endpoint no la devolvía.

**Qué ya existe** (se miró antes): `mensaje_de_limite` y `LimiteExcedido` (este módulo), `LimiteOut` (`{contratadas, activas}`), un único consumidor de la lectura además del kit —`libra-backoffice` (`routers/instancias.py`), que lee `respuesta.get("contratadas")` y `respuesta.get("activas")`, así que una clave más no le cambia nada—.

**Decisión.** `LimiteOut` suma `mensaje_de_limite: str | None`: el texto de `mensaje_de_limite(contratadas)` cuando hay tope y `null` cuando no (la instancia sin dato no limita). Lo traen el `GET` y el `PUT` (comparten modelo). Es **aditivo**: ninguna clave existente cambia.

**Consecuencias.**
- MINOR (propuesta **v1.167.0**), **sin migración**. Los productos no tocan nada: el router ya está montado.
- El kit lo lee de la respuesta (`libra-ui` 0.152.0, ADR-064) y deja de tener la frase. Un motor anterior a esta versión no manda la clave: el kit usa entonces la copia que conserva como respaldo hasta que los productos suban el pin (ver ese ADR).
- Si la frase cambia, cambia en `mensaje_de_limite` y las dos puntas (el 409 y la pantalla) la siguen.

**Tests.** `tests/test_limites.py`: el `GET` y el `PUT` traen el texto (1 y 3 sucursales, singular y plural), es idéntico al de `LimiteExcedido`, y sin tope es `null`. Cinco de los 41 fallan contra `develop` (los que comparan el cuerpo exacto y el nuevo).

## ADR-053 — `init_core_schema()` se serializa entre procesos con un candado de transacción bloqueante (`candados.esperar_candado_de_transaccion`)

- Estado: aceptada
- Fecha: 2026-10-15
- Contexto: el ADR-044 dejó el motor «listo para varios procesos» y el dueño decidió no prender más de un worker hasta medir **también la creación idempotente del esquema**. La medición salió mal: `tests/test_varios_procesos.py::test_dos_workers_arrancan_y_el_seed_queda_una_sola_vez` de MedLibra (CI de medlibra#373, 2026-10-13) y de Gestiolibra (los PRs del pin, 2026-10-14) dio `psycopg.errors.DeadlockDetected`, y pasaba al relanzar. Causa, medida en el motor con N procesos de verdad contra una base armada (`libracore.testing.esquema_en_paralelo`): `init_core_schema()` es **una sola transacción** de DDL y escrituras (`CREATE TABLE/INDEX IF NOT EXISTS`, `ALTER … ADD COLUMN` guardados por introspección, `UPDATE`s de relleno, seeds) que cada worker corre en su arranque. Los dos hacen `UPDATE caja_movimientos SET caja_id=? WHERE caja_id IS NULL` (aunque no toque filas pide `ROW EXCLUSIVE`, que se guarda hasta el `commit`) y más abajo `CREATE INDEX IF NOT EXISTS idx_caja_movimientos_fecha` (`SHARE`, aunque el índice exista, **antes** de mirar si existe): `SHARE` choca con el `ROW EXCLUSIVE` del otro, cada uno espera al otro y PostgreSQL mata a uno. Sobre una base vacía falla distinto: dos `CREATE TABLE IF NOT EXISTS` de la misma tabla chocan en el catálogo (`pg_type_typname_nsp_index`, `pg_class_relname_nsp_index`), y el `SELECT … FROM pg_constraint` seguido de `ADD CONSTRAINT fk_caja_movimientos_turno` es un «si no existe, crear» con ventana. Los tres son la misma causa: nada serializa a los procesos que corren esa transacción.
- Qué ya existía (se miró antes): `libracore.candados` (ADR-044) con `candado_de_base` (no bloqueante, atado a la transacción) y `una_vez_al_arrancar` (bloqueante, **conexión propia**). Los productos ya envuelven en `una_vez_al_arrancar` sus seeds y su `AuditoriaBase.metadata.create_all`, pero `init_core_schema()` corre **antes**, dentro del `configure()` de cada uno, y no podían envolverla: el DDL que importa vive en la transacción de quien llama, y un candado de otra conexión no la protege menos pero tampoco la acorta (y habría que cablearlo en seis productos).
- Decisión:
  - **`candados.esperar_candado_de_transaccion(conn, nombre, *, espera=120)`**: `pg_try_advisory_xact_lock` reintentado cada 0,1 s sobre la conexión de quien llama, `TimeoutError` a los `espera` s. Misma clave (SHA-256, 56 bits) y mismo soporte de conexiones que `candado_de_base`. Fuera de PostgreSQL devuelve `False` y no hace nada: SQLite serializa a sus escritores con el bloqueo de la base y el problema no existe ahí.
  - **`init_core_schema()` la llama como primera sentencia**, con `CANDADO_DEL_ESQUEMA`. El candado se suelta con el `commit` o el `rollback` del que llama, que es justo cuando el DDL deja de estar a medio hacer. El segundo proceso espera, y su corrida —idempotente de punta a punta— ve lo que dejó el primero. Sirve a todos los que la llaman (los seis productos al arrancar, las revisiones de Alembic) sin que ninguno cambie.
  - Se eligió **un candado dentro de la función** y no una costura para que cada producto la envuelva: el defecto está en la función, y un producto que se olvide de envolverla (o uno nuevo) lo reintroduce. Se eligió el reintento con `pg_try_…` y no `pg_advisory_xact_lock` bloqueante para tener tope de espera sin tocar `lock_timeout` de la sesión de quien llama.
  - La función sigue «congelada» (ADR de la `0001`): esto no agrega ni cambia una tabla, una columna ni un índice; es una guarda de ejecución, y `test_schema_congelado` lo confirma.
- Consecuencias:
  - MINOR (propuesta **v1.168.0**: API nueva), **sin migración**. Un producto con un solo proceso no nota nada (el candado se toma sin espera). Con N procesos, el arranque del segundo espera lo que tarde el primero en crear/verificar el esquema (medido: ≈0,1 s sobre una base armada, ≈1 s sobre una vacía).
  - Qué hace cada producto: **subir el pin**. No hay código que tocar para `init_core_schema()`.
  - Fuera de alcance, y dicho: el DDL **propio** de cada producto no pasa por esta función. `Base.metadata.create_all(get_engine())` de LibraGenda en MedLibra y Gestiolibra corre antes de cualquier candado y sobre una base **vacía** puede dar la clave duplicada del catálogo (sobre una armada no hace nada: sólo inspecciona); el `init_schema()` de LibraCommerce (VentaLibra, Contalibra, Restolibra) tiene el mismo patrón de DDL idempotente y **no se midió** acá. Ambos van dentro de `una_vez_al_arrancar` o se serializan por dentro en su propio motor.
- Tests: `tests/test_esquema_en_paralelo.py` (3 procesos arrancando el esquema a la vez sobre una base armada —3 vueltas— y sobre una vacía, más el mecanismo sin carreras: con el candado tomado por otra conexión `init_core_schema()` no crea ni una tabla hasta que ésta termina) y los de `esperar_candado_de_transaccion` en `tests/test_candados.py` (espera de verdad, tope, reentrante, `rollback`, SQLAlchemy, SQLite). Los tres de `test_esquema_en_paralelo.py` fallan sin la llamada de `init_core_schema()`.


## ADR-054 — El Swagger de la API (`/docs`, `/redoc`, `/openapi.json`) sólo se sirve en desarrollo: `libracore.docs_api`

**Estado:** propuesta (2026-10-15). Punto 6 del relevamiento de seguridad del VPS (2026-10-10), medido de nuevo el 2026-10-15: las tres rutas daban 200 sin sesión en clientes reales, demos, backoffices y panel.

**Contexto.** FastAPI publica por defecto `/openapi.json` (cada ruta, cada parámetro, cada esquema), `/docs` y `/redoc`. No filtra datos: es un mapa, y le ahorra a quien busca una falla el trabajo de descubrir qué hay. El esquema de la demo pública es el mismo que el de un cliente real. Lo medido en el código:
- Los ocho productos crean la app con `FastAPI(title=...)` sin tocar las URL del Swagger: las tres rutas, abiertas.
- `libracore.admin.create_admin_app` (los backoffices Jinja2 de Contalibra y Restolibra) y `libra-backoffice`/`libra-panel` ya pasaban `docs_url=None, redoc_url=None` pero **no** `openapi_url`: `/openapi.json` publicaba el esquema. Y `/docs` y `/redoc` ahí no eran Swagger sino el `index.html` de la SPA con 200 (el catch-all contesta 200 a lo que no existe, ver `libracore.spa`).
- `DOCS_AUTH_SECRET` **no** es un mecanismo para esto: protege `/auth/verify` y `/api/clientes-publicos`, que usa el login del sitio de manuales `/docs/` de la landing (ADR-018 de VentaLibra y equivalentes). No hay en el motor nada que autentique el Swagger.

**Qué ya existe** (se miró antes): `ENV == "development"` es la marca de «entorno de desarrollo» de toda la familia (CAE simulado en `arca_facturacion`, `pdf_generator`, admin por defecto y secreto de la cookie en libraauth). Ninguna instancia real ni demo la lleva: con ella la app emitiría comprobantes falsos. `LIBRA_ENTORNO` (`cliente`/`demo`/`dev`, ADR-045) la escribe el panel al dar de alta y `actualizar` no la pisa; una instancia vieja o una corrida local no la tiene.

**Decisión.** Un módulo del motor, `libracore/docs_api.py`, y una línea por producto justo después de crear la app:

```python
app = FastAPI(title="Contalibra")
cerrar_docs_de_la_api(app)
```

1. **Quién lo ve.** `docs_abiertos()`: sólo `ENV=development`, o `LIBRA_DOCS_API=1` puesto a mano en una instancia (un dev en el VPS, un diagnóstico). Todo lo demás —cliente, **demo**, variable ausente— da 404. Se decidió **cerrar también la demo**: es pública, su esquema es el de producción, y quien la quiera ver Swagger lo tiene en local (`ENV=development`) o con el interruptor. Se eligió abrir por una marca explícita y no cerrar por `LIBRA_ENTORNO=cliente|demo`: un criterio que se abre ante el «no sé» (variable ausente) deja abierto justo donde nadie miró.
2. **404 y no 401.** No hay sesión que dé acceso: el Swagger no es una pantalla de usuario. Un 401 además confirmaría que la ruta existe.
3. **Rutas que existen y contestan 404.** `cerrar_docs_de_la_api` reemplaza, en su lugar de la tabla, las rutas que FastAPI creó por las mismas detrás de la guarda. Con `docs_url=None` la ruta desaparecería y la atendería el catch-all de la SPA, que contesta `index.html` con **200**: un escáner seguiría viendo 200. La ruta propia, registrada antes que el catch-all, le gana, y el 404 tiene la forma del de FastAPI (`{"detail": "Not Found"}`).
4. **Se decide en cada pedido, no al armar la app.** Los productos arman la app al importar el módulo; decidirlo ahí haría depender la prueba del orden de import y del shell. Así la prueba de cada producto cambia la variable y pide.
5. **`create_admin_app` pasa `openapi_url=None`** sin condición: un backoffice no tiene API pública que documentar, ni siquiera en desarrollo.
6. **`app.openapi()` no cambia.** Lo usan los tests de rutas de los productos y `libracore.testing`; lo que se cierra es sólo la publicación por HTTP.

**Qué no.** No autentica el Swagger (descartado: hace falta un mecanismo de sesión de administrador para algo que sólo usa el desarrollo, y `DOCS_AUTH_SECRET` es server-to-server, no sirve a un navegador). No toca `/docs/` (con barra), que es el sitio de manuales y lo sirve el proxy desde la landing. No cierra ninguna otra ruta.

**Consecuencias.**
- MINOR (propuesta **v1.168.0**), **sin migración**. Hace falta **una línea por producto** (`cerrar_docs_de_la_api(app)` tras `FastAPI(...)` y su `import`) y subir el pin; `libra-backoffice` y `libra-panel` la usan también (y dejan de pasar `docs_url=None, redoc_url=None`). Contalibra y Restolibra obtienen el cierre de su backoffice (`create_admin_app`) sólo con el pin.
- Un producto que olvide la llamada sigue con el Swagger abierto y nada se lo avisa en el motor: cada producto trae su `tests/test_docs_cerrados.py`, que mide contra su app de verdad.
- Quien consulte `/docs` en un entorno que no es de desarrollo recibe 404: se pone `LIBRA_DOCS_API=1` en el compose de esa instancia (el panel no la escribe nunca).

**Tests.** `tests/test_docs_cerrados.py` (32, 18 caen con la función anulada): las cuatro rutas dan 404 en `cliente`, `demo`, vacío y sin variable; con el catch-all de la SPA montado dan 404 y no el `index.html` (más el control que muestra que `docs_url=None` daría 200); en `ENV=development` y con `LIBRA_DOCS_API=1` se ven; cualquier otro valor del interruptor o de `ENV` no abre; se decide por pedido; llamarla dos veces no rompe; `app.openapi()` sigue andando. `tests/admin/test_app.py`: `create_admin_app` no publica `/openapi.json` ni con `ENV=development` (falla sin `openapi_url=None`).

## ADR-055 — La app corre sin root y el motor le entrega `data/`: `chown` antes del `up -d`, `no-new-privileges` en el compose, y la carpeta de backups hereda el dueño (`libracore.provisioning.sin_root`)

**Estado:** aceptada (2026-10-15; el humano pidió sacar root de los seis productos). Sale del punto 8 del relevamiento de seguridad de la VPS (`wiki/analyses/relevamiento-seguridad-vps-2026-10-10.md`): seis de los ocho productos corren como `uid 0` dentro del contenedor (medido el 2026-10-15); LibraCargo y LibraClub, como `uid 10001`.

**Contexto.** El `USER` del Dockerfile lo pone cada producto, y no alcanza: la carpeta `clientes/<slug>/data` se monta con `./data:/app/data` y la crea el host como root. Docker le copia el dueño de la imagen a un volumen NUEVO y vacío; un bind mount o un volumen con datos **conserva el suyo**. Con la imagen en 10001 y `data/` de root el contenedor arranca, `/salud` da 200, y la app no puede escribir PDF, `arca_ta`, `errores/`, logos ni backups, ni leer las claves de ARCA (modo 600). Medido con una imagen de prueba (`touch`, `mkdir`, `echo >` y `cat` de un 600: `Permission denied`; con `chown -R 10001:10001`, los cuatro andan). LibraCargo lo resolvió a mano el 2026-08-22 (`chown -R` sobre `clientes/*/data`) y quedó anotado como invariante: lo que importa es el dueño del DIRECTORIO, no el de los ZIP que el cron deja como root. Nada de eso estaba en el motor, y el alta de una instancia nueva (que crea `data/` como root) tenía el mismo defecto.

**Qué ya existe** (se miró antes): el modelo de LibraCargo/LibraClub (`useradd -m -u 10001`, `chown -R /app`, `USER`); `errores_env`, que ya reescribe el compose de las instancias existentes en cada pin; `reset_demo.sh` de LibraCargo, que ya usa `docker exec --user root` para borrar lo que `docker cp` dejó de root. No existía ningún `chown`, `security_opt` ni `user:` en `libracore.provisioning`.

**Decisión.** Un módulo, `libracore/provisioning/sin_root.py`:
- `entregar_datos(data_dir)`: `chown -R 10001:10001` sin seguir symlinks, idempotente, que se niega a correr sobre algo que no sea una carpeta (archivo, symlink, raíz). Lo llama `cmd_actualizar` después del respaldo y del pin: **antes de migrar** (el contenedor efímero de cada migración corre como 10001) y **otra vez pegado al `up -d`** (recoge lo que el contenedor viejo, todavía root, escribió mientras tanto); también `cmd_rollback`. **Sólo si la imagen corre sin root** (`docker image inspect` → `Config.User`): mientras un producto no migre su imagen, su deploy no cambia. Si hace falta y no se puede, la instancia **no se mueve** y se repinea. `crear_cliente` lo llama también, después de lo último que el host escribe en `data/`.
- `asegurar_directorio`: lo que el host crea dentro de `data/` (`backups/`, en `_backup_zip` y en el restore) nace con el dueño de su madre.
- `con_security_opt`: `security_opt: [no-new-privileges:true]` en la app (no en el sidecar) de la plantilla del alta, y de los composes existentes vía `pinear_image`.

**Alternativa descartada: un entrypoint que arranca como root, hace `chown` y baja a 10001 con `setpriv`/`gosu`.** Funciona (medido: `setpriv` y `gosu` están en la imagen base y conviven con `no-new-privileges`), y se cura solo ante cualquier `data/` mal armado. Pero deja el PID 1 como root y `docker exec` entra como root por defecto (se pierde justo lo que el `USER` da), hace un `chown -R` en cada arranque, y LibraCargo/LibraClub ya eligieron `USER`. Queda como plan B si el `chown` del deploy resulta insuficiente.

**Consecuencias.**
- MINOR (propuesta), **sin migración** de base. Los productos suben el pin del motor **antes** de migrar su imagen, o a la vez; en cualquier orden el deploy es seguro (con imagen root no hay `chown`).
- La primera vez que una instancia pasa a una imagen sin root, el deploy hace el `chown` solo. Las instancias `-dev` NO pasan por `panel_admin`: su `data/` (`dev-data/`, `data-dev/`) se entrega a mano, una vez (comentado en cada compose).
- Cualquier proceso que corra como root en el host y escriba dentro de `data/` (el cron `backup-all`, `resguardo-externo`) sigue funcionando: root escribe en una carpeta de 10001, y la app borra lo de root porque borrar sólo pide permiso sobre el directorio.
- Lo que NO resuelve: los `docker cp` que dejan archivos de root en `/tmp` (sticky) —los `reset_demo.sh` de cada producto usan `--user root`—.

**Tests.** `tests/provisioning/test_sin_root.py` (43) y dos de rollback en `test_panel_admin.py`: a quién y hasta dónde entrega, qué rechaza, orden respecto del `up`, el alta, la carpeta de backups, el compose. Doce fallan sin el cableado (quitando las llamadas de `panel_admin` y `nuevo_cliente`; contra `develop` ni siquiera existe el módulo). Lo que no se puede medir con tests (el `chown` real, que el proceso arranque como 10001) se midió con Docker sobre una imagen de prueba.


## ADR-056 — Un producto que declara una tabla del motor sin una FK la suelta con `fk_de_producto.soltar_fk`; la baseline no cambia

**Estado:** aceptada (2026-10-15; el humano pidió «reparalo» sobre las seis FK de más que el ADR-015 de LibraDesk dejó como pendiente).

**Contexto.** La baseline (`init_core_schema()`, la `0001`) crea `cc_debitos`, `cc_pagos`, `recibos`, `remitos`, `presupuestos` y `egresos` con FK hacia `clients` y `proveedores`; `cc_debitos.cliente_id` y `cc_pagos.cliente_id` con `ON DELETE CASCADE`. LibraDesk declara esas tablas con su DDL, **sin** esas FK (`app/services/comercial.py`: `clients` y `proveedores` los versiona su cadena de Alembic y una FK que cruza los dos sistemas deja el `downgrade` imposible de correr). Mientras la app era la primera en crear las tablas, su `CREATE TABLE IF NOT EXISTS` mandaba. Desde el ADR-012 de LibraDesk (2026-10-07) `libracore-migrar` corre antes y, en una base **nueva** (la demo cada noche, el alta de un cliente, un restore), la baseline llega primero. Medido el 2026-10-15 volcando el schema completo (`schema_dump`) de una base nueva contra una migrada: **siete diferencias, ninguna más** —las seis FK (`CASCADE` en las dos de cuenta corriente, `SET NULL` en `recibos`, `remitos` y `presupuestos`, y la de `egresos.proveedor_id`) y el `DEFAULT` de `presupuestos.status` (`'pendiente'` del motor contra `'borrador'` de LibraDesk)—. Lo grave: borrar un cliente de una base nueva borraba sus movimientos de cuenta corriente, y `delete_client` sólo mira remitos y presupuestos.

**Qué ya existe** (se miró antes). La costura de `ventas_pagos`: `libracommerce.erp.ventas.repuntar_fk_ventas_pagos(conn)`, idempotente, que el producto llama en su arranque. `schema_dump` (y `test_schema_congelado`), que congela la baseline. Y medido: **ningún otro producto declara esas seis tablas por su cuenta** (`git grep` de `CREATE TABLE`, `ALTER TABLE`, `__tablename__` y `op.create_table` en los siete, más LibraCommerce y libraauth), y `init_core_schema()` las deja igual en todas las versiones del motor que hoy tienen instaladas (de v1.83.0 a v1.168.0, SQLite y PostgreSQL): en esos productos una base nueva y una migrada ya son iguales.

**Decisión.** Un módulo nuevo, `libracore/db/fk_de_producto.py`, con `soltar_fk(conn, tabla, columna, *, hacia) -> list[str]`: quita la FK de **una** columna hacia **una** tabla (`hacia` es obligatorio: no se puede llevar una FK que el producto quiere), con `DROP CONSTRAINT IF EXISTS` (dos procesos juntos no se pisan), sin tocar filas, devolviendo los nombres soltados (`[]` si no había). El producto la llama en el arranque, justo después de su DDL, igual que `repuntar_fk_ventas_pagos`. En SQLite no hace nada y devuelve `[]` (no se puede quitar una restricción sin reconstruir la tabla; el único producto que la necesita es sólo PostgreSQL), y está documentado así.

**Alternativas descartadas.**
- **Cambiar la baseline** para que no cree esas FK: está congelada (`test_schema_congelado`), y no cambia las bases que ya la corrieron. Los otros siete productos nacieron con esas FK y hoy una base nueva se parece a una migrada; sacárselas sólo para esta excepción rompería eso.
- **Una revisión de Alembic del motor** que las suelte: correría en todos los productos y les sacaría la integridad referencial que hoy tienen. No se decide acá.
- **Una revisión en la cadena de LibraDesk**: esa cadena corre *antes* que `libracore-migrar`, así que en una base nueva todavía no hay nada que soltar.
- **Una revisión del motor condicionada al prefijo `libradesk`**: lógica de un producto adentro del motor.

**Consecuencias.**
- MINOR (propuesta **v1.169.0**: API nueva), **sin migración y sin cambio de schema** (el gate `test_schema_congelado` no se mueve). Un producto que no la llama no nota nada: sólo LibraDesk la necesita. Subir el pin en los demás es la regla de la familia, no una necesidad.
- Una base nueva de LibraDesk queda igual a una migrada **en el primer arranque del contenedor**; entre `libracore-migrar` y ese arranque las seis FK existen (nadie escribe todavía). Las bases ya creadas con las FK (la demo) se alinean solas en el siguiente arranque, porque el llamado corre siempre.
- Soltar la FK saca el `ON DELETE` de la ecuación: con `SET NULL`, borrar un cliente deja `remitos.client_id` apuntando a un id que no existe; la integridad la sostiene el router del producto (es lo que ya pasaba en las bases migradas).
- **Lo que queda abierto, y no es de este ADR**: los otros siete productos conservan `cc_debitos`/`cc_pagos` con `ON DELETE CASCADE` hacia `clients`, y `libracore.db.clients.delete_client` sólo bloquea la baja si hay remitos o presupuestos —no mira la cuenta corriente—. Borrar un cliente con movimientos de cuenta corriente (`cc_pagos`/`cc_debitos`) en esos productos se los lleva. El libro de clientes (`cc_asientos`, ADR-027) es la fuente del saldo y no tiene FK a `clients`, pero los dos legados sí desaparecen. Decidir si `delete_client` debe bloquear por ellos (o si la baseline debe cambiar por una revisión) queda para el humano.

**Tests.** `tests/db/test_fk_de_producto.py` (11): la baseline deja las seis, `soltar_fk` las suelta con el nombre que devuelve, borrar el cliente ya no se lleva su cuenta corriente, es idempotente, no toca otra FK de la misma tabla ni la misma columna hacia otra tabla, una tabla inexistente no es error, rechaza identificadores que no son simples y en SQLite no hace nada. Cuatro fallan si el cuerpo del `for` que suelta no hace nada.

## ADR-057 — Un cliente con cuenta corriente no se puede borrar: `delete_client` la mira

**Estado:** aceptada (2026-10-15; el humano: «sí, que bloquee el borrado con cuenta corriente», sobre lo que el ADR-056 dejó abierto).

**Contexto.** En la baseline `cc_pagos.cliente_id` y `cc_debitos.cliente_id` tienen `ON DELETE CASCADE` hacia `clients`, `recibos.cliente_id` tiene `SET NULL` y el libro de clientes (`cc_asientos`, ADR-026/027) guarda `tercero_id` sin FK. `delete_client` sólo bloqueaba la baja por remitos y presupuestos: borrar un cliente con cuenta corriente en ContaLibra, RestoLibra, VentaLibra, GestioLibra, MedLibra, LibraCargo o LibraClub se llevaba sus pagos y débitos, dejaba los recibos sin cliente y el libro con asientos de un tercero que ya no existe.

**Decisión.** `delete_client` cuenta también los movimientos de `cc_pagos`, `cc_debitos`, `recibos` y `cc_asientos` con `rol='cliente'` del cliente, y si hay alguno levanta `ValueError` («… N movimiento(s) de cuenta corriente asociado(s) y no puede eliminarse. Se puede desactivar.»), en la misma consulta y con el mismo contrato que remitos y presupuestos. Un cliente con historia se desactiva.

**Alternativas descartadas.**
- **Cambiar el `CASCADE` por `RESTRICT` con una revisión de Alembic:** también protege a quien escriba SQL directo, pero el error sería de integridad y no un mensaje para el usuario, y toca el schema de los siete productos. La regla en `delete_client` alcanza: es la única baja de clientes del motor.
- **Bloquear sólo con saldo distinto de cero:** un cliente con la cuenta saldada sigue teniendo historia (recibos, comprobantes) que la baja destruiría.

**Consecuencias.** MINOR (propuesta **v1.170.0**), sin migración. Los asientos del libro de otros roles (proveedor, fletero) con el mismo número no bloquean. **Tests:** `tests/db/test_auditoria_de_clientes.py` (SQLite y PostgreSQL): uno por tabla, que la baja falla, el cliente y el movimiento quedan y no se audita nada; el mensaje suma remitos y cuenta corriente; un asiento de otro rol no bloquea. Sin el cambio fallan 10 de los 11 nuevos (el de otro rol es control).


## ADR-058 — Un cobro de factura no puede superar lo que falta: `cobros.exigir_cobro_posible`, con la factura bloqueada

**Estado:** propuesta (2026-10-16; defecto medido el 2026-10-15 en el e2e de Contalibra, `facturacion.e2e.ts`, `test.fixme` «no se puede cobrar más de lo que falta de una factura»).

**Contexto.** `POST /api/facturas/{id}/cobrar` aceptó 9.999 sobre una factura de 4.500 con 1.000 cobrados y 3.500 pendientes: 200, la factura pasó a «Cobrado completo — $ 10.999,00» y la caja asentó un ingreso de 9.999 que nadie debía. Reproducido en este motor con el router montado como lo monta un producto (`total_cobrado: 10999.0`, `pendiente: 0.0`). No había ninguna comparación entre lo que se cobra y lo que falta: `registrar_cobro_factura` sumaba lo que mandaba la pantalla y `_detalle` escondía el exceso con `max(0, …)`. Cobrar de más en una **cuenta corriente** sí es válido (el resto queda a favor, `cuenta_corriente_router`); en el cobro de una factura puntual no hay a qué asentarlo.

**Qué ya existe** (se miró antes). «Pendiente» es `total − cobrado` en todo el motor: `facturas_router._detalle`, la vista `sin_cobrar` de `db.facturas.get_facturas_filtradas`, `db.resumen`, `db.dashboard` y `db.cuenta_corriente.get_facturas_pendientes_cc`; lo cobrado es `db.caja.get_cobros_factura` (ingresos, sin anulados y sin la marca de cuenta corriente). `recibos` y el PDF tratan como «cobrada» a la que está a menos de `0.005` de su total. `cuenta_corriente_router._repartir` ya topa cada factura en su pendiente. Para la carrera: `libracommerce` serializa con `FOR UPDATE` (PostgreSQL) y un `UPDATE` sin efecto (SQLite). La regla de la nota de crédito (`SUPERA_SALDO`, 409) es el antecedente en estilo y en status. Ningún producto de la familia tiene un cobro de factura «con vuelto»: la pantalla compartida (`libra-ui`, `FacturaDetalle`) manda el monto aplicado, precargado con el `pendiente`.

**Decisión.** La regla vive en la capa de dominio, `libracore/cobros.py`, y la cumplen todas las entradas del motor que cobran una factura:
- **`saldo_de_cobro(factura, cobros)`** (pura) y **`estado_de_cobro(factura_id, conn=)`** (lee la base): `pendiente = total − cobrado`, en **centavos enteros** (`centavos()`: la mitad hacia arriba, vía `Decimal(str(x))`). La tolerancia es **medio centavo** —lo mismo que ya usan `recibos` y el PDF— y ninguna más: 3.500,004 entra, 3.500,01 sobre 3.500,00 no. El `pendiente` de `_detalle` sale de la misma función, para que el diálogo nunca ofrezca un monto que el tope rechace.
- **`exigir_cobro_posible(factura_id, monto, *, conn=None)`** levanta `CobroNoPermitido` (`codigo`: `excede`, `sin_saldo`, `anulada`, `no_existe`; el texto dice cuánto falta, en pesos al estilo de la pantalla). Se compara la **suma** de los medios del cobro, no cada uno. Con `conn` toma antes **el candado de la fila de la factura** (`FOR UPDATE` en PostgreSQL; en SQLite un `UPDATE` sin efecto, que toma el candado de escritura de la base) y lee **con esa conexión**: dos cobros simultáneos que juntos superan lo pendiente se hacen cola y el segundo ve lo que dejó el primero.
- **`registrar_cobro_factura`** abre **una** transacción (o usa la de `conn=`), bloquea, valida y escribe movimientos y abono de cuenta corriente en ella. Antes cada escritura confirmaba por su cuenta: un fallo a mitad dejaba un cobro parcial. Las escrituras inyectadas (los tests) reciben `conn=`; con las tres escrituras y `leer_estado` inyectadas no hay base.
- **Router `POST /api/facturas/{id}/cobrar`: 409** con el mensaje, como `SUPERA_SALDO` y «El comprobante está anulado»: lo que lo impide es el estado de la factura, no el formato del pedido. Un producto que **reemplaza** la escritura (`registrar_cobro=`) recibe el cobro ya validado, sin candado; para frenar la carrera llama `exigir_cobro_posible(…, conn=)` en su propia transacción.
- **`POST /api/caja`** con `factura_id` e `ingreso` (salvo la marca de cuenta corriente) también: un ingreso ligado a una factura *es* un cobro suyo (así lo cuenta `get_cobros_factura`), y la pantalla de Caja de `libra-ui` lo arma desde `/caja?factura_id=`. Se valida y se escribe en la misma transacción; factura inexistente, 404.
- Sólo **facturas** (`tipos.FACTURAS`) tienen tope: para una nota no existe en el motor la cuenta de «lo que falta». *(Desde el ADR-064 lo tienen también las notas de débito.)*

**Alternativas descartadas.**
- **Validar sólo en el router:** un producto que reemplaza la escritura, la caja manual y cualquier uso directo de `registrar_cobro_factura` la saltearían; y no hay candado posible fuera de la transacción de la escritura.
- **Validar en `create_caja_movimiento`:** es la primitiva de todo ingreso, también del cobro con QR/MercadoPago que **ya entró** (LibraClub lo registra desde el poll): rechazarlo sería perder plata real del cajón. El tope es de las acciones del operador, no de registrar un hecho.
- **Un peso de gracia o un porcentaje:** el redondeo del mostrador (cobrar de más «para redondear») es de ventas y cuenta corriente; en una factura el exceso no tiene dónde quedar.
- **Restar las notas de crédito:** hoy ningún «pendiente» del motor las resta (un test fija que una factura con su nota total sigue mostrando todo el total pendiente) y restarlas sólo en el tope haría que la pantalla ofreciera un monto y el tope aceptara otro. Queda abierto (ver abajo).

**Consecuencias.**
- MINOR (propuesta **v1.171.0**), **sin migración y sin cambio de schema**. API nueva: `CobroNoPermitido`, `centavos`, `saldo_de_cobro`, `estado_de_cobro`, `exigir_cobro_posible`, `registrar_cobro_factura(…, leer_estado=, conn=)` y `db.caja.get_cobros_factura(…, conn=)`.
- **Visible en los productos que montan `build_comprobantes_router`** (Contalibra, Restolibra, LibraClub): un cobro de más pasa de 200 a **409**, y el detalle de una factura con un cobro de más anterior sigue mostrando `pendiente: 0` (no se corrige lo ya asentado).
- Un reintento con la **misma `referencia`** ya no se absorbe en silencio: `create_caja_movimiento` devolvía el movimiento existente (y el abono de cuenta corriente se duplicaba); ahora, sin saldo, el segundo pedido es 409.
- **Lo que queda abierto, y no es de este ADR:** (1) las notas de crédito no restan de lo pendiente en ningún cálculo; se puede cobrar una factura ya acreditada por una nota total. Hacerlo bien toca el `pendiente` de la pantalla, la vista `sin_cobrar`, el resumen y el tablero, y es una decisión de producto. (2) Los cobros de factura **propios de un producto** (LibraClub `POST /api/caja/cobros` con `factura_id`, que ya existe) no pasan por el motor: tienen que llamar `exigir_cobro_posible(…, conn=)`. (3) `cuenta_corriente_router.pagar` topa por factura pero sin candado: dos pagos a cuenta simultáneos sobre la misma factura pueden cubrirla dos veces *(cerrado en el ADR-063)*. (4) Una nota de débito no tiene tope *(cerrado en el ADR-064)*.

**Tests.** `tests/test_cobro_no_supera_lo_pendiente.py` (35, **SQLite y PostgreSQL**, por HTTP): el caso medido (9.999 sobre 3.500), exacto, parcial, varios medios cuya suma supera (cada uno cabe solo), ya cobrada, dos cobros sucesivos, tolerancia de centavos y polvo de coma flotante, factura a crédito (sin abono suelto), anulada, `POST /api/caja` con y sin factura, el reemplazo del producto, el `pendiente` que ofrece la pantalla, y seis cobros simultáneos (HTTP y con la lectura lenta) de los que entra uno. `tests/test_cobros.py` (+24 casos, sin base). Sin el cambio fallan 19 de los 35; sin el candado, los cuatro de concurrencia.


## ADR-059 — «Lo que falta cobrar» es una sola cuenta y las notas de crédito (con CAE) restan: `sql_pendiente`

**Estado:** propuesta (2026-10-16; decisión del humano: «las notas de crédito restan de lo pendiente de su factura, en todo el motor y de forma coherente»).

**Contexto.** El ADR-058 puso un tope al cobro de una factura y, para no partir la definición, dejó **fuera** a las notas de crédito: en todo el motor «pendiente» era `total − cobrado`, salvo en `get_facturas_pendientes_cc`, que sacaba de la lista a la factura entera si tenía **cualquier** nota vigente (también una parcial). Medido el 2026-10-16 contra el router, sobre una factura a crédito de 1.000 con una nota por 400: el saldo del libro `cc_asientos` baja a 600 (la nota abona la cuenta, `cc_pagos` con `referencia = nc:factura:…`), pero `pendientes_cc` devolvía `[]` (se debían 600), el detalle mostraba `pendiente` 1.000, la vista «sin cobrar» la listaba, y después de cobrar los 600 el detalle seguía diciendo «pendiente 400» con la cuenta del cliente en 0. Una factura acreditada por completo seguía figurando «Sin cobrar» y con el botón de cobrar.

**Qué ya existe** (se miró antes). `notas_de_credito.acreditado`/`saldo_acreditable` (Python, sobre las notas ya cargadas, con la regla «CAE sí, `PENDIENTE` y vacío no»), `get_nc_de_factura` (emisor y ambiente, `sql_vigente`), `get_facturas_pendientes_cc` (el `NOT EXISTS` de la nota), y los fragmentos `sql_no_es_cuenta_corriente`/`sql_no_anulado`/`sql_vigente`, que ya eran el patrón: un criterio, una función, se puede grepear.

**Decisión.** `pendiente = total − cobrado − acreditado`, con **tres fragmentos en `libracore.db.facturas`** que son la única definición:
- `sql_cobrado(alias, con_cuenta_corriente=False)`: ingresos de caja de la factura, sin anulados y sin la marca de cuenta corriente (es `get_cobros_factura`). `con_cuenta_corriente=True` sólo para el tablero sin `sin_fiado`, que históricamente cuenta fiar como cobrar.
- `sql_acreditado(alias)`: lo que restan las notas de crédito **con CAE** (ni vacío ni `PENDIENTE`) y **vigentes** (no anuladas, ADR-022) de esa factura: mismo emisor y mismo ambiente (ADR-021).
- `sql_pendiente`, `sql_con_pendiente` (más de medio centavo) y `sql_pendiente_positivo` (para sumar).

Todos los cálculos del motor que dicen «cuánto falta» los usan:
1. `cobros.estado_de_cobro` / `exigir_cobro_posible` (el tope del ADR-058; `get_cobrado_y_acreditado` lee los mismos fragmentos).
2. `facturas_router._detalle` (`pendiente`, y `total_acreditado`, nuevo).
3. `db.facturas.get_facturas_filtradas`: el filtro `sin_cobrar` y las columnas por fila (`total_cobrado`, y `total_acreditado` y `pendiente`, nuevas; `pendiente` sólo en las facturas).
4. `db.resumen.get_resumen_core` (`sin_cobrar`).
5. `db.dashboard.get_dashboard_data` (`facturas_sin_cobrar`, con `pendiente` por fila).
6. `db.cuenta_corriente.get_facturas_pendientes_cc` (y por lo tanto `cuenta_corriente_router._repartir`, que aplica un pago a cuenta hasta el `pendiente` de cada factura).
7. `recibos.emitir_recibo_factura`: el rótulo «Pago parcial» / «Cancelación» descuenta lo acreditado.
Un test cruza los siete, más la versión en Python de `notas_de_credito`, sobre 13 casos y en SQLite y PostgreSQL.

**Cuando la nota supera lo pendiente.** El `pendiente` **nunca es negativo**: una factura cobrada y después acreditada, o con un cobro de más de antes del ADR-058, queda en 0. No hay «saldo a favor» de una factura puntual. **La cuenta corriente no cambia**, y se midió: la nota de una factura a crédito abona la cuenta una vez; cobrada 1.000 y acreditada 1.000 queda el saldo del cliente en −1.000 (a favor), igual que antes (hay un test que lo fija como lo que es). Para una factura al contado cobrada y después acreditada no se asienta nada (ni egreso de caja ni crédito): devolver la plata es una decisión de producto que sigue abierta. **Una nota no se descuenta dos veces de la cuenta del cliente**: estos cálculos leen las notas y **no escriben ni tocan el libro**; el saldo sale del libro, el `pendiente` es por factura. Medido: factura a crédito 1.000, nota 400, pago a cuenta de 1.000 aplicado a la factura → se aplican 600, el resto es pago a cuenta y el saldo queda −400 (el pago fue de más); cobrar los 600 por el router deja la cuenta en 0.

**Lo que se ve distinto (hay que mirarlo en los productos).**
- La vista «sin cobrar» y los contadores **sacan** a las facturas acreditadas por completo y a las cobradas-más-acreditadas.
- `resumen.sin_cobrar` y el tablero ahora cuentan una factura **cobrada a medias** (antes la sacaba cualquier cobro) y el `monto` es **lo que falta** y no el total. Antes tampoco filtraba los cobros anulados: una factura con su único cobro anulado dejaba de figurar impaga.
- `get_facturas_pendientes_cc`: una nota **parcial** deja la factura con lo que todavía se debe (antes la sacaba entera); una total la saca, como antes.
- Una factura **acreditada por completo** ya no se puede cobrar (409 «ya está acreditada por completo por notas de crédito»), y el diálogo ya no la ofrece como pendiente.
- Respuestas con campos nuevos (aditivos): `total_acreditado` en el detalle; `total_acreditado` y `pendiente` por fila del listado; `pendiente` por fila del tablero.

**Alternativas descartadas.**
- **Restar sólo en el tope** (ADR-058): la pantalla ofrecería un monto y el tope aceptaría otro.
- **Un `pendiente` calculado en Python y copiado a cada consulta:** las listas son SQL con paginación y conteo; una función Python no las filtra. Por eso el SQL es la fuente y Python (`saldo_de_cobro`) sólo es la aritmética pura de lo que el SQL ya leyó.
- **Contar las notas sin CAE o `PENDIENTE`:** todavía no acreditaron nada (ARCA puede rechazarlas); la regla es la de `notas_de_credito.acreditado`.
- **Un asiento de «saldo a favor» para la factura al contado acreditada:** es una decisión de producto, fuera de este ADR.

**Consecuencias.** MINOR (propuesta **v1.172.0**), **sin migración y sin cambio de schema**. API nueva: `sql_cobrado`, `sql_acreditado`, `sql_pendiente`, `sql_con_pendiente`, `sql_pendiente_positivo`, `get_cobrado_y_acreditado`, `saldo_de_cobro(…, acreditado=)`, `emitir_recibo_factura(…, get_acreditado=)`. **La pantalla compartida (`libra-ui`) tiene que rotular la factura acreditada** («Acreditada» en vez de «Cobrada»/«Sin cobrar» en la lista; «Acreditada por nota de crédito» en el detalle en vez de «Cobrado completo — $ 0,00») y mostrar `pendiente` en el tablero: ver el CHANGELOG. Sin eso la API es correcta y la pantalla dice «Sin cobrar» de una factura que no se puede cobrar. Queda por fuera `pdf_generator.generate_pdf_recibo` (la versión vieja, función pura de los cobros que le pasan; sólo la usan tests: `libracore.recibos` la reemplazó).

**Tests.** `tests/test_pendiente_con_notas_de_credito.py` (64 casos, SQLite y PostgreSQL): 13 casos de la tabla (nota parcial, total, dos parciales, sin CAE, `PENDIENTE`, anulada, cobro y nota que completan y que no, cobrada y acreditada, cobro anulado, marca de cuenta corriente, cobro parcial) contra cada cálculo; nota de otro emisor, de otro ambiente y de débito; el router de punta a punta; la cuenta corriente del cliente (sin doble descuento, cobrada y acreditada). `tests/test_cobros.py` (+4), `tests/db/test_resumen.py` y `tests/test_facturas_router.py` (dos que fijaban la semántica vieja, actualizados). Sin el cambio fallan 56 de los 64 (los 8 que pasan son los casos sin notas).

## ADR-060 — El cobro de una factura puede entrar por el turno abierto de quien cobra: `exigir_turno` y `usuario_del_turno`, sin reemplazar el cobro del motor

**Estado:** propuesta (2026-10-16; pedido del humano para que LibraClub borre su `registrar_cobro=_cobrar_del_turno`).

**Contexto.** LibraClub lleva la caja **por turno** (`turnos_caja`, con arqueo al cerrar) y el cobro por defecto escribe el movimiento sin `turno_id`: plata que ningún cierre cuenta. Por eso pasaba `registrar_cobro=` y reemplazaba el cobro entero (`app/routers/facturas.py`, `_cobrar_del_turno`), apoyándose en `servicios/caja.registrar_ingreso`. Con el ADR-058 ese reemplazo quedaba **sin tope y sin candado** (el router sólo podía validar antes, sin transacción). Lo que el reemplazo necesitaba, leído de su código: el turno abierto del usuario **del dominio** (el `id` que ve el motor es `None`: sus usuarios viven en otra base), 409 «No hay una caja abierta. Abrí el turno antes de cobrar.» sin turno, `turno_id` en el movimiento, la caja y el dueño del turno, la fecha de hoy (ignorando la del pedido), la `referencia` tal cual (la idempotencia es la de `create_caja_movimiento`), más dos reglas **propias del producto**: no cobrar una factura a cuenta corriente por ahí y aceptar sólo sus medios de pago.

**Decisión.** Tres opciones del factory `build_comprobantes_router` y dos de `registrar_cobro_factura`, sin una línea de LibraClub:
- `exigir_turno=True`: el cobro entra por el turno abierto (`db.turnos.get_turno_activo`, con la conexión de la transacción del cobro); sin turno, `CobroNoPermitido(SIN_TURNO)` → **409** con el mensaje de arriba, antes de escribir. `usuario_del_turno(usuario) -> id | None` dice con qué id se abrió el turno (por omisión `usuario["id"]`). Sin `exigir_turno`, pero con `usuario_del_turno`, va por el turno **si hay** y si no entra suelto como siempre. Sin ninguna de las dos, nada cambia (Contalibra, Restolibra).
- Con turno, el movimiento lleva `turno_id`, `caja_id` del turno y su dueño como `usuario_id`; el abono de cuenta corriente, la caja y el dueño (no tiene turno); `fecha` y `caja_id` del pedido **se ignoran**: la caja y el día los define el turno, no lo que mande la pantalla.
- `validar_cobro(factura, pagos, usuario)`: un hook que corre antes de todo y levanta `HTTPException` para lo que es sólo del producto (sus medios, su regla de cuenta corriente). No escribe. Rige también con `registrar_cobro`.
- Todo dentro de la **misma transacción con candado** del ADR-058: el turno se lee, el tope se valida y el movimiento se escribe en una conexión, con la factura bloqueada.
- `transaccion_de_cobro(factura_id, monto, *, conn=None)`: un `with` que abre la transacción, bloquea, valida y entrega la conexión, para el cobro **propio** de un producto que no pasa por `registrar_cobro_factura` (el `POST /api/caja/cobros` con `factura_id` de LibraClub). `caja_router.crear` ya la usa.

**Alternativas descartadas.**
- **`turno_de` por pedido en el factory:** el turno es un dato de la base y se lee **dentro** de la transacción (si no, se cierra entre que se mira y se escribe); por eso el factory recibe quién cobra y el motor arma la consulta.
- **Que el motor sepa de medios o de cuenta corriente propia de un producto:** es lógica de LibraClub; para eso está `validar_cobro`.
- **Tope en `create_caja_movimiento`:** ver el ADR-058 (el cobro con QR ya entró).

**Consecuencias.** MINOR (v1.172.0), sin migración. `registrar_cobro=` sigue existiendo (nadie más lo usa). LibraClub puede borrar su reemplazo y quedarse con `validar_cobro`. Cada cobro simultáneo en el turno se hace cola igual que en el ADR-058.

**Tests.** `tests/test_cobro_por_turno.py` (25, SQLite y PostgreSQL): el movimiento entra en el turno con su caja, su dueño y la fecha de hoy (y suma al arqueo); con el usuario del dominio; sin turno exigido (409, nada escrito) y con el turno de otro usuario o cerrado; el tope con turno; `usuario_del_turno` sin exigir; sin opciones el cobro es el de siempre con un turno abierto; factura a crédito con turno; `validar_cobro`; seis cobros simultáneos; `transaccion_de_cobro` (tope, commit, rollback). `tests/test_cobros.py` (+3, sin base).

## ADR-061 — Una demo no habla con ARCA ni muestra un emisor de la base, y una auditoría dice si algo parece real (`libracore.demo`)

**Estado:** propuesta (2026-10-16; número asignado por el orquestador: 059 y 060 son del PR #402). Pendiente: «ningún test impide que una demo vuelva a traer datos fiscales reales».

**Contexto.** El ADR-038 hizo que, con `DEMO_MODE=1`, `config_manager.load()` pise los datos de la empresa con una ficticia, y dejó dicho lo que **no** cubre: el emisor que `emisor_del_pdf` toma de la base (`arca_config` y el resolvedor del producto). Medido el 2026-10-16, por donde un dato fiscal real todavía podía llegar a una demo pública:

- **El emisor del PDF** (`emisor_del_pdf.emisor_para`, único punto de los ocho generadores): con `emisor_id` ponía el nombre y el CUIT de la fila de `arca_config`; el resolvedor del producto (LibraCargo) ponía domicilio, IVA, Ingresos Brutos y **el logo, que viaja como bytes desde la base**; `empresa=` pisaba todo.
- **`arca_config`**: `config_del_emisor()` y sus hermanos entregan CUIT, razón social, punto de venta y ambiente a la pantalla de ARCA, a `consultar_cuit_router`, a `mp_facturacion` y a `venta_facturacion`. Una fila cargada a mano, con `ambiente='produccion'` y un par de producción en disco, hacía que `get_next_numero_with_arca` se autenticara contra el WSAA **de producción** desde una demo. Lo único que lo impedía era que «una demo no tiene certificado» (comentario de las semillas de ContaLibra y RestoLibra), y `PUT /arca` + `POST /certificado` aceptaban la clave privada de cualquier visitante.
- **MercadoPago y correo** viven en `config.json`/almacén: un token real llena la bandeja con los pagadores reales (CUIT incluidos); un SMTP real muestra y usa un remitente real.
- **Terceros en la semilla**: 28 CUIT en seis de los ocho productos (MedLibra y LibraClub no tienen; MedLibra, LibraCargo y LibraClub tienen DNI de choferes y pacientes). Ninguno está en un rango reservado. **26 no cierran el dígito verificador** (no pueden existir en el padrón de ARCA); **2 sí lo cierran** y fuera de un rango reservado no se distinguen de uno real: el pagador de transferencia de la semilla de RestoLibra y la empresa de la demo de LibraCargo (`30-55667788-9`, que su semilla describe como «con dígito verificador válido»).

Y nada lo vigilaba: ningún test corría una semilla y miraba qué quedó.

**Decisión.**

1. **Una demo no tiene ARCA** (`DEMO_MODE=1`, leído en cada llamada como en el ADR-038):
   - los lectores de `arca_config` (`obtener_arca_config`, `obtener_todas_arca_configs`, `obtener_arca_config_por_id`, `config_del_emisor_en` y, por ellos, `config_del_emisor` y `config_por_cuit`) devuelven lo de una instancia sin facturar —`None` o `[]`— aunque la tabla tenga filas. Los escritores no cambian: `actualizar_arca_config` lee su fila por un camino interno que no se esconde;
   - `arca_credenciales.paths_en_disco` y `paths_en_disco_de_servicio`, por donde pasa toda emisión y toda firma, devuelven `("", "")`: ni producción ni homologación;
   - `build_arca_router` rechaza con 403 todo lo que no sea lectura (configurar, subir certificado o clave, pedido, borrar, probar);
   - **por qué rechazar y no forzar homologación**: con un CUIT real y un certificado de homologación se autenticaría igual contra el WSAA con una identidad real, y una demo con ese par sería una demo con la clave privada de alguien en un contenedor al que entra cualquiera con un código. Sin credenciales el motor numera local y no sale a la red: es exactamente el comportamiento que las semillas documentan y que las ocho demos tienen hoy. Y es la opción que no depende de que alguien recuerde poner `ambiente='homologacion'`.
2. **El emisor del PDF, en una demo, es la empresa ficticia y nada más**: `emisor_para` devuelve la capa de configuración (que `load()` ya pisó, ADR-038) sin leer `arca_config`, el resolvedor ni `empresa=`. Efecto colateral aceptado: la demo de LibraCargo imprime «Empresa Demo SRL» hasta que registre la suya con `usar_empresa_demo`.
3. **`usar_empresa_demo` rechaza un CUIT que no sea ficticio** (ver 4): el error de registrar uno real o mal escrito sale al arrancar, no en un PDF.
4. **Qué es un documento ficticio** (`libracore.demo.clasificar_documento`, una sola regla): un CUIT de 11 dígitos con prefijo 20/23/24/27/30/33/34, **verificador válido** y cuerpo **`99999xxx`** (los ocho dígitos del medio empiezan con `99999`; mil por prefijo; `30-99999900-6` es el de `EMPRESA_DEMO_POR_DEFECTO`); un DNI es ficticio si tiene ocho dígitos y empieza con `99999`. Un CUIT con verificador válido **fuera** de ese rango es `parece_real`; uno que no cierra el verificador es `invalido` (no puede ser real, pero tampoco ficticio verificable) y un DNI fuera del rango es `dni` (no se puede afirmar nada). El producto puede declarar una lista blanca (`ficticios=`): es una deuda a la vista, no la meta. **No hay lista negra**: nadie escribe en el código los CUIT de sus clientes.
5. **`libracore.demo.auditar_datos_fiscales(conn, config=None, *, terceros=(), empresa=(), ficticios=(), incluir_dudosos=False, conn_producto=None) -> list[Hallazgo]`**: lee la base y el `config.json` **tal cual están** (`config_manager.load_sin_demo()`, no `load()`; SQL directo, no los lectores que en una demo esconden lo real). Hallazgos de gravedad **alta** (los que fallan): CUIT de empresa que parece real (`config.json`, filas de `arca_config`, columnas declaradas con `empresa=`), ARCA en producción, certificado o clave de producción (con o sin archivo en disco), credencial de servicio de producción, token de MercadoPago que no está confirmado como de prueba (`mp_config_router.ambiente_de`), CUIT de tercero que parece real en las diez columnas del motor (`clients`, `proveedores`, `remitos`, `presupuestos`, `facturas`, `recibos`, `comprobantes_pendientes`, `mp_pagos`, `mp_movimientos`, `facturacion_alias`) y en las del producto (`terceros=`; en la base del dominio con `conn_producto=` cuando el core va aparte). **Baja** (sólo con `incluir_dudosos=True`): CUIT que no cierra el verificador, DNI, certificado de homologación, SMTP o remitente cargados. Cada hallazgo es agregado por (columna, código), con los documentos **enmascarados** (`30714*****1`), la fila por su `id`, y sin nombres ni secretos. `auditar_literales(texto)` hace lo mismo sobre el texto de una semilla, sin armar nada.
6. **`python -m libracore.demo auditar --producto <prefijo>`** (o `--url`) la corre contra una instancia: 0 limpia, 3 hallazgos (imprime `ERROR …`), 4 si no es una demo, 2 si se la usa mal. `--producto` resuelve las dos bases con `url_de_instancia`. Es lo que llama el reset nocturno de cada demo después de sembrar.

**Alternativas descartadas.**
- **Forzar `ambiente='homologacion'` en una demo:** ver 1; deja el certificado en el disco y la identidad real en el WSAA.
- **Que la auditoría trate «verificador válido» como «real» y lo demás como ficticio:** deja pasar los 26 inventados y marca a un CUIT ficticio que, por azar, cierra el verificador (uno de cada once). La regla útil es la contraria: lo ficticio es lo que está en el rango reservado.
- **Una lista negra de CUIT reales:** obliga a escribirlos y no ataja el próximo.
- **Esconder también MercadoPago y el correo en `load()`:** rompería una demo que, a propósito, quiera mandar un mail de prueba desde una cuenta propia. Se audita y se avisa (alta para MercadoPago, baja para el correo); bloquearlo es una decisión del humano.
- **Mover el chequeo a los productos:** el arreglo de fondo vive en el motor; cada producto sólo suma el test que corre su semilla.

**Consecuencias.** MINOR (propuesta **v1.173.0**): API nueva (`libracore.demo`, `config_manager.load_sin_demo`), sin migración y sin cambio de schema. Fuera de una demo no cambia nada (los tests lo fijan). **Para los productos:** (a) sumar el test que corre su semilla y exige `auditar_datos_fiscales(...) == []` (y `auditar_literales` sobre el texto de la semilla) — hoy darían rojo **RestoLibra** (el pagador de transferencia, `scripts/seed_demo.py:649`) y **LibraCargo** (la empresa de la demo, `scripts/seed_demo.py:172`); se arreglan cambiando un CUIT por uno `99999xxx`; (b) LibraCargo, que guarda la empresa en `configuracion_empresa`, registra la suya con `usar_empresa_demo` para que su demo no imprima «Empresa Demo SRL»; (c) el reset nocturno corre `python3 -m libracore.demo auditar` y sale con error si hay hallazgos (propuesta en el wiki, `operaciones/scripts-demo/`). **Tests:** `tests/test_demo_fiscal.py` (SQLite y PostgreSQL): la regla de «ficticio» (incluido todo el rango reservado), una demo limpia, cada código de hallazgo, las diez columnas del motor, las del producto y la base aparte, el enmascarado, el emisor del PDF con texto extraído, los lectores y las credenciales escondidos, `get_next_numero_with_arca` sin llamar al WSAA, el 403 del router, el rechazo de un CUIT real en `usar_empresa_demo` y la corrida nocturna. Sin el cambio fallan 9 (SQLite) y 8 (PostgreSQL) de los de comportamiento; los de auditoría son nuevos con el módulo.

**Lo que queda abierto.** Un certificado suelto en `CERTS_DIR` sin fila en la base no se audita. `example.com.ar` (que usan las semillas en los correos) no es un dominio reservado como `.example`: el dato no es fiscal, pero conviene revisarlo.



## ADR-062 — Una caja nueva puede nacer en la sucursal predeterminada del producto: `OpcionesCajas.sucursal_por_defecto`

**Estado:** propuesta (2026-10-16; pedido del orquestador, a raíz de la sucursal única de ContaLibra).

**Contexto.** `POST /api/cajas` crea la caja con `payload.sucursal_id`, que es `None` si el pedido no lo manda. ContaLibra adoptó el 2026-10-16 una sucursal única «Casa central» (libracommerce v0.63.0, ADR-063): su migración `0006_sucursal_unica` y su `init_db` asignan a ella las cajas con `sucursal_id IS NULL`, y sus depósitos nuevos ya caen en la predeterminada (`OpcionesDepositos(sucursal_por_defecto=True)`). Pero una caja creada **después** por la API quedaba en NULL hasta el próximo arranque: el hueco que el ADR-063 no podía cerrar desde el motor comercial, porque las cajas son de este motor. El motor no sabe de sucursales (viven en la base del producto: `branches` en LibraCommerce, `sucursales` en VentaLibra) ni depende de LibraCommerce, así que no puede ir a buscar «la predeterminada» él solo.

**Decisión.** Una opción más en `OpcionesCajas`: **`sucursal_por_defecto: Callable[[], int | None] | None = None`**.
- Sin argumentos y devolviendo el id, o `None`: la misma forma que `resolver_sucursal_nombre` del cierre diario (el producto abre su propia conexión; el motor no le pasa la suya porque la base de sucursales puede no ser la del motor).
- En `crear`, si el pedido **no trae** `sucursal_id` y la opción está, la caja nace con lo que devuelva. Si el pedido trae uno, se respeta y **no se llama** a la función. Si devuelve `None` (una instancia que todavía no tiene sucursal), la caja queda sin ella, como siempre.
- Corre **después** de `validar_alta`, que sigue viendo el pedido tal como llegó: la predeterminada la pone el producto, no hace falta que se valide contra sí mismo, y no se cambia el contrato de un gancho que ya existe (VentaLibra lo usa para exigir sucursal; con la opción puesta, esa exigencia sigue valiendo para quien lo pida).
- **Sin la opción, el comportamiento es byte a byte el de siempre.**
- **La edición no cambia.** `PUT /api/cajas/{id}` nunca escribió `sucursal_id` (`update_caja_config` no lo recibe; el campo del payload «sólo se usa al crear»: una caja no se muda de sede), así que no puede dejar una caja en NULL ni pisarla con la predeterminada. Tampoco hay `PATCH`. Una caja que ya estaba en NULL no se completa al editarla: de eso se ocupa quien adopta la sucursal (en ContaLibra, `app/sucursal_unica.py`).

**Alternativas descartadas.**
- **`sucursal_por_defecto=True`, como en los depósitos de LibraCommerce:** allá el router vive en el mismo paquete que la tabla `branches` y puede leerla; acá el motor no la conoce.
- **Que el motor lea `branches` si existe:** supone un esquema de otro paquete y falla en silencio en un producto que la llame distinto (`sucursales`).
- **Resolver antes de `validar_alta`:** el gancho vería una sucursal que el pedido no mandó y un producto no podría distinguir «la eligió el usuario» de «la puso el default».
- **Completar la sucursal también al editar:** reabriría la decisión de que una caja no se muda, y taparía una caja sin sucursal que el producto quizá quiere ver.

**Consecuencias.** MINOR (propuesta **v1.174.0**), aditivo, sin migración y sin cambio de schema. Un producto que no pasa la opción no cambia. **Para los productos:** ContaLibra la monta con una función que lee la fila `is_default` de `branches`; cualquier otro con una sucursal predeterminada puede hacer lo mismo. Lo que **no** cubre: la «Caja Principal» que siembra `init_core_schema` (nace sin sucursal por su propio camino, congelado desde la baseline); cada producto la asigna en su arranque.

**Tests.** `tests/test_caja_router.py` (+5, SQLite): la caja nace en la predeterminada y el listado por sucursal la encuentra; un `sucursal_id` explícito se respeta y no consulta la función; `None` deja la caja sin sucursal; sin la opción (o sin `opciones`) sigue en NULL; `validar_alta` ve el pedido sin la sucursal y, si rechaza, no se crea nada; el PUT no toca la sucursal. Sin el cambio fallan 4 (la opción no existe); el quinto, «sin la opción», es la guarda de que nada cambia.

## ADR-063 — El pago a cuenta corriente aplicado a facturas toma el candado del cobro: una transacción, facturas bloqueadas en orden de id

**Estado:** propuesta (2026-10-16; número asignado por el orquestador). Es el punto (3) que el ADR-058 dejó abierto.

**Contexto.** `POST /api/cuenta-corriente/{cliente_id}/pagar` con `facturas` reparte el pago entre las facturas elegidas, cada una topada en su `pendiente` (`_repartir`), y escribe un movimiento de caja por factura —ése es el «cobro» que la marca «Cobrada» (`get_cobros_factura`)—. Pero lo hacía en **cinco conexiones y sin candado**: `get_facturas_pendientes_cc` leía lo pendiente en una, `validar_pago` del producto en otra, `create_cc_pago` abría la suya, y cada `create_caja_movimiento` la suya. Entre «miré lo pendiente» y «escribí el cobro» pasaban los dos pagos: los dos veían la factura entera pendiente y los dos la cobraban. Medido con la versión anterior del router, dos pagos de 1.000 a la misma factura de 1.000, con el gancho del producto lento (`tests/test_cc_pago_con_candado.py`): `[200, 200]` en los dos (y 700 + 700 sobre 1.000 dejaba 1.400 cobrados), en SQLite y en PostgreSQL. Era el mismo defecto del ADR-058 por otra puerta. Además, una caída a mitad de camino dejaba el abono sin sus cobros (o con sólo los de las primeras facturas).

**Qué ya existe** (se miró antes). `cobros._bloquear_factura` (`FOR UPDATE` en PostgreSQL; un `UPDATE` sin efecto en SQLite) y `exigir_cobro_posible(…, conn=)` son el candado del cobro de una factura; `create_cc_pago`, `create_caja_movimiento` y `get_cobrado_y_acreditado` ya aceptan `conn=`; sólo `get_facturas_pendientes_cc` no.

**Decisión.**
- **Una sola transacción**: el router abre una conexión, y en ella (1) bloquea las facturas pedidas, (2) lee lo pendiente (`get_facturas_pendientes_cc(cliente_id, conn=)`, parámetro nuevo), (3) reparte, (4) llama al gancho `validar_pago` del producto, (5) escribe el abono (`create_cc_pago`) y los movimientos de caja de cada factura y del resto suelto, todo con `conn=`. Confirma al salir; una excepción en cualquier punto revierte todo.
- **`cobros.bloquear_facturas(conn, ids)`** (nueva): bloquea con `_bloquear_factura`, **en orden de id ascendente** y sin repetidos. El orden único evita el interbloqueo: `[7, 3]` y `[3, 7]` a la vez, tomados en el orden del pedido, se esperarían para siempre (PostgreSQL aborta uno con error); ordenados, el segundo espera en el primero.
- **Quien llega segundo ve lo que dejó el primero, y la conducta es la que ya tenía ese camino, sin inventar ninguna**: si la factura quedó cobrada por completo ya no está en los pendientes y `_repartir` contesta el **422** «Factura sin cobro pendiente en esta cuenta» (sin escribir nada); si quedó parcialmente cobrada, se le aplica **sólo lo que falta** y el resto queda como pago a cuenta suelto (700 + 700 sobre 1.000: 700 y 300 a la factura, 400 suelto). Es lo que hace el mismo pedido en dos tiempos.
- El candado es **el mismo** que el de `registrar_cobro_factura` / `exigir_cobro_posible` (la fila de la factura): un pago a cuenta y un «Registrar cobro» simultáneos a la misma factura también se hacen cola.
- El **gancho `validar_pago`** corre ahora con las facturas bloqueadas, dentro de la transacción: tiene que ser de sólo lectura (el de VentaLibra lo es: valida el pedido y lee el turno). Uno que escribiera con otra conexión esperaría, en SQLite, el candado de escritura que tiene este pago. El orden de los rechazos es el de siempre (422 de las facturas, después el gancho).
- Lo que no cambia: el recibo de cobranza se emite **después** de confirmar (si falla, el cobro queda, como antes); la respuesta se arma con lecturas posteriores.

**Caminos que aplican plata a facturas o a la cuenta corriente, y cuáles cubre:**
- **Cubierto**: `POST /api/cuenta-corriente/{id}/pagar` con `facturas` (Contalibra, Restolibra, VentaLibra con su gancho). Un pago **sin** `facturas` baja el saldo sin tocar ninguna factura: no tiene la carrera, y ahora también es atómico (abono + movimiento de caja suelto).
- **Ya tenía el candado**: `POST /api/facturas/{id}/cobrar` (`registrar_cobro_factura`) y `POST /api/caja` con `factura_id` (ADR-058).
- **No tiene la carrera** (escribe en `cc_pagos`, no cobra ninguna factura puntual ni lee un tope): la nota de crédito a cuenta (`facturas_router`, `create_cc_pago` con la marca `nc:factura:…`), la anulación de una venta fiada (`db/ventas.anular_venta`) y la devolución (`db/reversiones`). Lo que acreditan las notas lo cuida la propia emisión (`una_nota_a_la_vez` y `SUPERA_SALDO`).
- **No cubierto**: un producto con su propio pago a cuenta que no pase por este router no tiene tope por factura ni candado (ver Consecuencias).

**Alternativas descartadas.**
- **Un candado por cliente** (`SELECT … FROM clients FOR UPDATE`): serializaría todos los pagos del cliente, aun sobre facturas distintas, y no se haría cola con un cobro de factura suelto, que no toma el candado del cliente.
- **Reintentar la lectura después de escribir** (validar a posteriori): la plata ya está asentada en la caja; sólo el candado antes de leer lo evita.
- **Un nivel de aislamiento `SERIALIZABLE`**: cambia el contrato de toda la capa de acceso y obliga a reintentos en cada llamador; la fila de la factura ya es el recurso que se disputa.
- **Meter el pago en `cobros.registrar_cobro_factura`**: ese camino es de una factura con varios medios y su propio abono; éste es un pago único repartido entre varias facturas.

**Consecuencias.** MINOR (propuesta **v1.175.0**), **sin migración y sin cambio de schema**. API nueva: `cobros.bloquear_facturas` y `get_facturas_pendientes_cc(…, conn=)`. Visible: dos pagos simultáneos a la misma factura dejan de cobrarla de más (el segundo, 422 o lo que falta); un pago que falla a mitad no deja nada escrito. **Para los productos:** un `validar_pago` propio no debe escribir con otra conexión. LibraDesk (`routers/comercial.py`, `POST /cuenta-corriente/pagos`) y LibraClub (`servicios/cuenta_corriente.py`, `registrar_pago`) llaman `create_cc_pago` por su cuenta: no aplican el pago a facturas (no hay tope por factura que cuidar), así que no tienen esta carrera; si algún día lo hicieran, tienen que pasar por este router o por `bloquear_facturas`.

**Tests.** `tests/test_cc_pago_con_candado.py` (16, **SQLite y PostgreSQL**, por HTTP, un hilo con conexión propia por pedido y el gancho lento para ensanchar el hueco): dos pagos a la misma factura (`[200, 422]`, 1.000 cobrados), dos parciales (700 + 700: se aplica lo que falta y 400 queda suelto), las mismas facturas en distinto orden (con el candado frenado a propósito; en PostgreSQL falla sin el orden), un pago y un cobro simultáneos, y los secuenciales (el pago de siempre, el 422, una caída a mitad que no deja nada, el rechazo del gancho). Sin el cambio fallan 9 de los 16.


## ADR-064 — La nota de débito tiene tope de cobro: `tipos.COBRABLES` y `estado_de_cobro.con_tope`

**Estado:** propuesta (2026-10-16; número asignado por el orquestador). Es el punto (4) que el ADR-058 dejó abierto.

**Contexto.** `cobros.estado_de_cobro` decía `con_tope = tipo in tipos.FACTURAS` y `exigir_cobro_posible` salía sin mirar si no. El comentario lo justificaba: «quien cobra una nota de débito no está cobrando "lo que falta" de ningún total». Pero una nota de débito **sí tiene un total**: el monto nuevo que agrega (intereses por mora, un gasto, una diferencia; `notas_de_debito.armar_nota_de_debito`, o el total de la factura que copia `POST /{id}/nota-debito`), y se cobra por el mismo `POST /api/facturas/{id}/cobrar` o `POST /api/caja` con `factura_id`. Se le podía cobrar cualquier monto, más de su total incluido: el defecto del ADR-058 en otro tipo de comprobante.

**Decisión.** Las notas de débito (`tipos.ND`: 2, 7, 12, 202, 207, 212, la FCE incluida) tienen tope. `tipos_comprobante.COBRABLES = FACTURAS + ND` es la lista de los comprobantes con un total que cobrar, y `estado_de_cobro` la usa para `con_tope`. `pendiente = total − cobrado − acreditado`, con **los mismos** `sql_cobrado` y `sql_acreditado` (ADR-059): `sql_acreditado` mira el comprobante asociado de las notas de crédito sea del tipo que sea, así que sirve tal cual para una nota de débito. Las **notas de crédito** siguen sin tope: no se cobran, restan. Como el tope es el de la factura, también tiene su candado (ADR-058) y el del pago a cuenta (ADR-063). El mensaje dice «nota de débito» donde decía «factura» (`rotulo` en el estado).

**Qué se miró y no se cambió** (ver «Lo que queda abierto»):
- **Cuenta corriente.** `get_facturas_pendientes_cc` filtra `tipo IN FACTURAS` **y** `condicion_venta = 'Cuenta Corriente'`. La nota de débito que arma el motor (`armar_nota`) no lleva `condicion_venta` y la emisión no escribe un débito en la cuenta del cliente, así que una nota de débito del motor **no es** una deuda de cuenta corriente: sumarla a esa lista no cambiaría nada con las notas del motor, y con las de un producto que sí la fíe cambiaría qué se ofrece pagar. Se deja como está.
- **Lo que muestra la pantalla.** `_detalle` (cobros, `pendiente`) y la columna `pendiente` del listado se calculan sólo para facturas (`es_factura`, `CASE … tipo IN FACTURAS`); la vista `sin_cobrar`, `get_resumen_core` y el tablero (`facturas_sin_cobrar`) también. Alinearlos haría que **cada nota de débito emitida y nunca cobrada en el sistema** aparezca de golpe en «Sin cobrar» y en los totales de todos los productos, y que el detalle ofrezca «Cobrar» donde hoy no. Es una decisión de producto, no una consecuencia del tope.

**Alternativas descartadas.**
- **Un tope que compare con el total de la factura original:** la nota agrega plata que la factura no tenía; su tope es el suyo.
- **Alinear de paso las listas, el resumen y el tablero:** ver arriba.

**Consecuencias.** MINOR (propuesta **v1.175.0**), **sin migración y sin cambio de schema**. API nueva: `tipos_comprobante.COBRABLES`. Visible: cobrar de más una nota de débito pasa de 200 a **409** («El cobro … supera lo que falta cobrar de la nota de débito»); una nota ya cobrada o acreditada por completo no admite otro cobro. Las cobradas de más antes del cambio siguen como están (no se corrige lo asentado).

**Lo que queda abierto, y no es de este ADR:** (1) la pantalla (`_detalle`, listado, `sin_cobrar`, resumen, tablero) y la lista de pendientes de cuenta corriente no incluyen a las notas de débito: el tope existe pero el «pendiente» que se ve sigue siendo 0 en una nota; (2) el motor no deja emitir una nota de crédito sobre una de débito (`TIPO_NC` sólo mapea facturas), así que `acreditado` de una nota de débito es 0 salvo carga manual.

**Tests.** `tests/test_nota_de_debito_con_tope.py` (9 casos en 2 motores = 18, por HTTP): más que su total, exacto y después sin saldo, dos parciales, varios medios que suman de más, seis cobros simultáneos de los que entra uno, una nota de crédito sobre la de débito que resta, la cuenta del tope contra `sql_pendiente`, la nota de crédito y la factura (el control: la primera sin tope, la segunda con él) y la lista de cuenta corriente sin cambios. Sin el cambio fallan 14 de los 18.

## ADR-065 — La nota de crédito de una factura al contado cobrada pregunta qué hacer con la plata: devolver o dejar a favor (`libracore.reintegro_de_nota`)

**Estado:** propuesta (2026-10-16; decisión del humano: «Preguntar al emitir»).

**Contexto.** La nota de una factura **a cuenta corriente** abona la cuenta del cliente (`referencia_cc_de_nota`); la de una factura **al contado ya cobrada** no movía plata: ARCA quedaba al día pero la caja no registraba la devolución ni la cuenta el saldo a favor, y lo cobrado quedaba «de más» sin explicación (ADR-059: `pendiente` queda en 0, no hay saldo a favor de una factura puntual). `anular_venta` (libracommerce) revierte la caja sola, pero sólo cuando se anula la venta; una nota parcial, o una nota sin anular la venta, nunca devolvía nada.

**Decisión.**
- `POST /{factura_id}/nota-credito` acepta `destino` (`"devolver"` | `"a_favor"`) y `medio_pago`. **Sin `destino` no se mueve plata** (default compatible: ningún producto que no mande el campo cambia). Un valor fuera de la lista es 422 de pydantic.
- `devolver`: un **egreso de caja** (`reversiones.reintegrar_devolucion`, generalizada con `concepto` y `factura_id`) con `referencia = nc:factura:<id>:<nota_id>`. El medio es el pedido; sin él, el único con el que se cobró; con varios medios y sin elegir, 422. Un medio que no es plata (`cuenta_corriente`) o desconocido, 422. Rige la caja por turno del ADR-060 (`exigir_turno`, `usuario_del_turno` también en `build_nota_de_credito_router`): el egreso entra por el turno abierto de quien emite, con la caja y el dueño del turno; sin turno y con `exigir_turno`, **409** «No hay una caja abierta. Abrí el turno antes de devolver.» **antes** de pedir el CAE.
- `a_favor`: un **abono en la cuenta corriente** del cliente (misma `referencia`). Sin cliente identificado (`get_client_by_cuit`), 422, también antes del CAE. No lleva `medio_pago` ni turno.
- En una factura **a cuenta corriente** el destino se rechaza (422): la nota abona la cuenta sola, como hoy.
- **Cuánto se mueve** no es el importe a ciegas sino lo que la nota deja cobrado de más: `max(0, min(importe, cobrado_neto − max(0, total − acreditado)))`, con `cobrado_neto = cobrado − lo ya devuelto o dejado a favor por otras notas` (`reintegrable`). Cobrada completa: el importe. Cobrada a medias y nota total: lo cobrado (no se devuelve lo que no entró); `excedente` lo documenta, en la respuesta y en el concepto del movimiento. Cobrada a medias y nota que no pasa de lo que falta: 0 (el cliente sigue debiendo). Sin plata que mover el destino se ignora (`movido: false`).
- **Cuándo se escribe.** La emisión de la nota no es una transacción (cada función de `libracore.db` abre la suya) y `solicitar_cae` **no levanta** si ARCA rechaza: deja el motivo en la nota. Por eso el router escribe la plata **después** de la emisión y sólo si la nota **tiene CAE**, en una transacción propia con la factura bloqueada (`cobros._bloquear_factura`) que relee lo cobrado y lo ya devuelto. Sin CAE no se mueve nada y la respuesta lo dice (`reintegro.movido: false`, con la marca para hacerlo a mano). El destino **no se recuerda**: si la nota se autoriza después con `/autorizar`, la plata se mueve a mano. Si el proceso muere entre el CAE y el movimiento, la nota queda autorizada sin movimiento (el mismo hueco que el abono de las facturas a crédito).
- **No se devuelve dos veces**: `aplicar` busca la marca antes de escribir (y el egreso lleva `factura_id`, así que `create_caja_movimiento` también lo descarta); una segunda nota total da 409 por las guardas de siempre; dos parciales suman hasta lo cobrado, no más. `notas_de_credito.devuelto_por_notas` y `cc_acreditado_por_notas` dicen cuánto salió por cada camino.
- `GET /{factura_id}/nota-credito/destino?importe=` (`solo_admin`, no escribe): `aplica`, `monto`, `excedente`, `medios`, `medio_sugerido`, `cliente_identificado`, `sin_turno`. Es lo que la pantalla (libra-ui v0.158.0) necesita para preguntar, sin que ninguna pantalla repita la cuenta ni que un producto tenga que mandarle más datos.
- La respuesta del POST agrega `reintegro` (`destino`, `medio_pago`, `movido`, `monto`, `excedente`, `referencia`, `motivo` si no se movió) sólo cuando hubo destino: aditivo.

**Alternativas descartadas.**
- **Topar sólo por `min(importe, cobrado)`**: con 400 cobrados de 1000 y una nota por 300 devolvería 300 a un cliente que sigue debiendo 300.
- **Recordar el destino de una nota sin CAE** (columna o tabla): requiere migración; el caso (ARCA rechaza) es raro y se resuelve a mano con la marca. Si molesta, un `POST .../nota-credito/{nota_id}/reintegro` idempotente lo cierra.
- **Default `devolver`**: cambiaría a todo producto que no manda el campo.
- **Escribir antes del CAE** o en la misma transacción que la nota: la nota no tiene transacción, y plata movida por una nota que ARCA no autorizó es peor que una nota autorizada sin movimiento.

**Consecuencias.** MINOR (v1.176.0), sin migración. 🔴 **`anular_venta` de libracommerce revierte con un egreso cada pago acreditado y no mira lo que la nota ya devolvió**: con una nota que devolvió o dejó a favor y después anular la venta, la plata saldría dos veces. Antes de que un producto con `anular_venta` muestre la elección (libra-ui v0.158.0 la muestra apenas el motor la ofrece), libracommerce tiene que restar `devuelto_por_notas + cc_acreditado_por_notas` de lo que revierte. Ver «Para los productos» del CHANGELOG.

**Tests.** `tests/test_nc_contado_destino_de_la_plata.py` (SQLite y PostgreSQL).
