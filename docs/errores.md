# El registro de errores de la familia: uno solo, del motor

**Decidido por el humano el 2026-10-12 (ADR-045):** registro propio en el motor —no GlitchTip ni Sentry—, avisos sólo por
Telegram y sin datos sensibles. Antes de esto un 500 iba al stdout de uvicorn y se perdía en cada recreate del contenedor:
no había ningún handler común de excepciones en ningún repo.

**Qué hay:** una línea JSON por error en `DATA_DIR/errores/errores.jsonl` de cada instancia (rotación 5 MB × 3), una pantalla
«Algo falló» en el navegador que cita el `ref`, y un vigía en el host que lee esos archivos y avisa por Telegram
(`operaciones/vigilar_errores.py` del wiki, fuera de este repo).

## Qué pone el motor y qué pone el producto

| El motor (`libracore.errores`) | El producto |
|---|---|
| El handler de `Exception`, el `ref`, la limpieza, la firma, el archivo y su rotación | **Una línea** en el backend: `instalar_registro_de_errores(app, "miproducto")` |
| `POST /api/errores-cliente` (se monta solo con la línea de arriba) | `<ErrorBoundary>` y `instalarReporteDeErrores({ release })` de `libra-ui`, en `main.tsx` |
| `LIBRA_INSTANCIA`, `LIBRA_ENTORNO` y `LIBRA_VERSION` en el compose de cada cliente y demo (`provisioning.errores_env`) | `LIBRA_ENTORNO=dev` (y `LIBRA_VERSION=${LIBRA_VERSION:-dev}`) en el compose de su `-dev` |
| `capturar(exc, **contexto)` para las fallas manejadas | Llamarla donde un `except` se traga algo que importa |

## Cómo se enchufa

```python
from libracore.errores import instalar_registro_de_errores

app = FastAPI(...)
app.add_middleware(SecurityHeadersMiddleware, csp=CSP_SPA)
instalar_registro_de_errores(app, "contalibra")            # producto
# opcional: instalar_registro_de_errores(app, "contalibra", usuario_id=lambda request: request.state.uid)
```

- **Al construir la app**, antes del primer pedido: Starlette no deja sumar handlers después.
- Un `Exception` que ninguna ruta atrapa responde `500 {"detail": "Error interno", "ref": "a1b2c3d4"}` y deja una línea. La
  SPA (`ApiError`) ya muestra el `detail`; el `ref` es lo que el usuario le dicta a soporte.
- **No se registran** los `HTTPException` (4xx), los `RequestValidationError` (422) ni el `ClientDisconnect`: son del cliente
  y taparían los errores del sistema. Si un `HTTPException` llega al handler porque lo levantó un middleware, se responde
  con su código, igual que antes.
- Starlette deja que el servidor ASGI vea la excepción después de que el handler contestó: **uvicorn sigue escribiendo el
  traceback en su stdout**, como siempre.
- `usuario_id` es un callable `Request -> id | None`. Tiene que ser **barato** (nada de ir a la base) y devolver sólo el
  id. Sin él se lee `request.state.usuario_id`; si no hay, `null`.
- El 500 que sale del handler no pasa por `SecurityHeadersMiddleware` (Starlette lo arma desde afuera de la pila): lleva
  sólo `X-Content-Type-Options` y `Cache-Control: no-store`. Es JSON, sin nada que enmarcar ni ejecutar.

### Fallas manejadas

```python
from libracore.errores import capturar

try:
    cae = await pedir_cae(...)
except TimeoutError as e:
    ref = capturar(e, tags={"servicio": "wsfe"}, factura_id=factura.id)   # nunca lanza
    return avisar_al_usuario(f"ARCA no contesta (ref {ref})")
```

`tags` son etiquetas de agrupación; el resto de los argumentos con nombre es `contexto`. Las claves sensibles (`cuit`,
`token`...) quedan en `***` y los valores pasan por la limpieza. Devuelve el `ref`. **Nunca lanza**, ni con el disco lleno.

## La línea

```json
{"ts": "2026-10-12T20:58:05-03:00", "producto": "contalibra", "instancia": "contalibra-compulibra",
 "entorno": "cliente", "release": "v2026.10.12-103000", "origen": "servidor", "manejado": false,
 "metodo": "GET", "ruta": "/api/clientes/{cid}", "status": 500, "tipo": "ValueError",
 "mensaje": "...", "traceback": "...", "firma": "1243ce49…", "ref": "7750f7bf", "usuario_id": "42",
 "query": {"q": "hola", "token": "***"}, "user_agent": "Mozilla/5.0 …"}
```

| Campo | De dónde sale |
|---|---|
| `producto` | el que se pasó a `instalar_registro_de_errores` |
| `instancia`, `entorno`, `release` | `LIBRA_INSTANCIA`, `LIBRA_ENTORNO` (`cliente`/`demo`/`dev`; cualquier otra cosa o ausente ⇒ `desconocido`), `LIBRA_VERSION` |
| `ruta` | la **plantilla** (`route.path`: `/api/clientes/{cid}`), nunca la URL con el id. Sin ruta resuelta: `(sin ruta)` |
| `donde` | `archivo:función` del frame propio más profundo (el mismo que entra en la firma); lo usa el aviso del vigía, que no lleva el mensaje |
| `firma` | sha1 de `tipo` + `ruta` + `archivo:función` del frame más profundo **que no es de terceros**, sin números de línea |
| `ref` | 8 hex; el mismo que ve el usuario |
| `origen` | `servidor` o `cliente` (los del navegador) |
| `manejado` | `true` si salió de `capturar` |
| `tags`, `contexto` | sólo de `capturar` |

**La firma agrupa por lugar.** «Frame de la app» es el más profundo que no esté en `site-packages`/librería estándar;
los paquetes `libracore`, `libraauth`, `libracommerce`, `libragenda` y `libraedge` cuentan como propios aunque estén
instalados. El archivo se escribe relativo al paquete o al directorio de la app. Mover el código unas líneas no cambia
la firma; mover la función de archivo, sí.

## Lo que nunca llega al archivo

| Nunca | Cómo |
|---|---|
| Body, cookies, `Authorization`, cabeceras (salvo `user-agent`) | no se leen |
| Variables locales | el traceback se formatea sin ellas |
| Valores de la query con una clave `token`, `password`, `passwd`, `clave`, `secret`, `cuit`, `cert`, `key`, `dni`, `cbu`, `authorization`, `cookie` | se escribe `***` (la clave se busca como subcadena, sin mayúsculas) |
| CUIT/DNI | cualquier corrida de 7 a 11 dígitos, con o sin guiones/puntos ⇒ `[CUIT/DNI]` |
| Mails, bloques PEM, `usuario:clave@` de cualquier URL, JWT, `Bearer …` | `[email]`, `[PEM]`, `[credenciales]`, `[jwt]` |
| `DETAIL: Key (cuit)=(2012…)`, `Failing row contains (…)`, `invalid input syntax …: "valor"`, `[parameters: …]` de SQLAlchemy | se conserva la columna y se saca el valor |
| `clave=valor` / `clave: valor` con una clave sensible | `clave=***` (`KeyError` no cuenta como clave) |

Mensaje ≤ 2 KB, traceback ≤ 8 KB (se conservan el principio y el final), valores de contexto ≤ 200 caracteres. Del
traceback se sacan los frames del framework que van **antes** del primer frame propio: un 500 de FastAPI arrastra ~20
frames de Starlette que se comen el tope.

🔴 La limpieza es una **lista negra**: lo que no está en ella pasa. Si aparece un dato personal en una línea real, se
agrega el patrón a `libracore.errores` y un caso a `tests/test_errores.py::test_limpieza_de_texto`.

## Escribir nunca rompe la respuesta

`registrar` envuelve todo. Si no puede escribir (disco lleno, sin permisos, `DATA_DIR` de sólo lectura, el directorio
`errores` es un archivo) **la línea va al stdout** —donde iba el 100 % antes de esto— y la respuesta sale igual. Si la
limpieza misma falla, se registra un evento mínimo (producto, tipo, `ref`).

El archivo es `$LIBRA_ERRORES_DIR/errores.jsonl` si esa variable existe y `$DATA_DIR/errores/errores.jsonl` si no (el mismo
`DATA_DIR` que `arca_ta/` y `config.json`: en una instancia de cliente, `./data/errores/` del host). Rotación por tamaño:
`errores.jsonl`, `.1`, `.2`, `.3`, 5 MB cada uno (≤ 20 MB por instancia).

**La carpeta `errores/` no entra al ZIP del respaldo** (`respaldo.CARPETAS_FUERA_DEL_ZIP`): es un registro de
operación, no datos del cliente, y los ZIP rotan 14 días.

## Los errores del navegador: `POST /api/errores-cliente`

Lo mandan `instalarReporteDeErrores` (`window.onerror`, `unhandledrejection`) y el `<ErrorBoundary>` de `libra-ui` con
`navigator.sendBeacon`. Es **público a propósito** (un error del login también importa), así que lo defiende lo que exige:

| Defensa | Valor |
|---|---|
| Mismo origen | si viene `Origin`, su host tiene que ser el del pedido (`X-Forwarded-Host` o `Host`); si no, `403` |
| Tamaño | ≤ 4 KB; `413` si no (por `Content-Length` y por el cuerpo leído) |
| Tope por IP, en memoria | 20 pedidos cada 10 minutos; `429`. La IP es la **última** de `X-Forwarded-For` (la agrega NPM; lo anterior lo manda quien llama) |
| Tope global, en memoria | 300 por hora; `429` |
| Forma | un objeto JSON (`400` si no); se guardan sólo `tipo`, `mensaje`, `stack`, `pagina` y `release`, con la misma limpieza |

La página se guarda como plantilla sin query ni fragmento y con los segmentos que parecen ids como `:id`
(`/clientes/1234/editar` ⇒ `/clientes/:id/editar`). La `firma` es sha1 de `cliente` + `tipo` + `ruta` + el mensaje con los
números colapsados (el stack del bundle minificado cambia con cada build). `release_cliente` es la versión que cargó el
navegador: si difiere de `release`, el usuario tiene el bundle viejo en caché.

Responde `204` siempre que acepta. Ver `docs` de `libra-ui` para el lado del navegador.

## De dónde salen `LIBRA_INSTANCIA`, `LIBRA_ENTORNO` y `LIBRA_VERSION`

Las escribe el panel, no la aplicación (`libracore.provisioning.errores_env`):

| Variable | Valor | Cuándo |
|---|---|---|
| `LIBRA_INSTANCIA` | el contenedor (`libradesk-lagrace`) | alta, y cada pin |
| `LIBRA_ENTORNO` | `demo` si el slug es `demo`, si no `cliente` | alta; el pin **no la pisa** si ya está (por si alguien la fijó a mano) |
| `LIBRA_VERSION` | el tag de la imagen pineada (`v2026.10.12-103000`) | alta, y **cada pin** |

**La versión sale del tag que el panel pinea**, no de un `ARG` del Dockerfile ni de la etiqueta `org.libra.version`: la
imagen se construye *antes* de saber a qué instancias va, la etiqueta no se ve desde adentro del contenedor, y el compose es
el único lugar donde el panel y el contenedor coinciden. Como `pinear_image` es por donde pasan `actualizar`, `rollback` y el
repineo de un deploy fallido, la variable siempre dice lo mismo que la imagen. **Ningún producto toca su Dockerfile.**

Las instancias **ya desplegadas** ganan las variables en su próximo `actualizar` (o `rollback`): `pinear_image` las agrega a
cualquier compose con un `environment:` en lista. Si el compose no lo tiene, o lo tiene en formato de mapa, sólo se pinea la
imagen y la instancia queda con `entorno: desconocido` hasta que se corrija a mano.

Las `-dev` no pasan por el panel: se escriben en el compose del repo del producto:

```yaml
    environment:
      - LIBRA_ENTORNO=dev
      - LIBRA_INSTANCIA=contalibra-dev
      - LIBRA_VERSION=${LIBRA_VERSION:-dev}
```

## El vigía (fuera de este repo)

`operaciones/vigilar_errores.py` del wiki corre en el host por cron cada 5 minutos, recorre los `errores.jsonl*` de todas
las instancias, y avisa por Telegram: `cliente` en la primera aparición de cada firma y luego como máximo cada 6 h con el
conteo; `demo` y `dev` en un resumen diario. El aviso lleva producto, instancia, tipo, ruta, `archivo:función`, conteo y
`ref`, **no el mensaje**.

## Pasar a GlitchTip o Sentry más adelante

Se toca **sólo `libracore/errores.py`**: `capturar` tiene la forma de `capture_exception` y el evento usa los nombres de
Sentry.

| Evento | Sentry |
|---|---|
| `release` | `release` |
| `entorno` | `environment` |
| `tags` | `tags` |
| `firma` | `fingerprint` |
| `usuario_id` | `user.id` |
| `ruta`, `metodo`, `query` | `request.url` (plantilla), `request.method`, `request.query_string` |
| `contexto` | `extra` |
| `manejado` | `mechanism.handled` |

El handler seguiría llamando a la misma función; los productos no se enteran. La limpieza (`before_send`) se conserva.
