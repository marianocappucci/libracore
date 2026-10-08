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
