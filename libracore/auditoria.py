"""La costura de auditoría de escrituras del motor (ADR-043).

Quién creó, editó o borró qué, y qué cambió, **para las tablas que escribe este
motor** (hoy `clients`). El motor no sabe dónde ni cómo se guarda el log: el
producto le enchufa un *registrador*, un callable `(conn, cambio) -> None`, igual
que enchufa `configure_resolver_receta` o `configure()` de `db/core.py`.

```python
from libraauth.auditoria import registrador_dbapi
from libracore import auditoria

auditoria.configurar_auditoria(registrador_dbapi({"clients": "cliente"}, atributos=...))
```

**Por qué una costura y no el log adentro.** El log tiene un formato (columnas
tapadas, etiqueta, diff, usuario del request) que vive en `libraauth.auditoria`, y
este motor no depende de `libraauth` —ni al revés—. Con el registrador como
callable, el formato se define una sola vez y el motor sólo dice *qué pasó*:
`Cambio(tabla, accion, id, antes, despues)`.

**Es TRANSACCIONAL, y es a propósito.** `registrar()` se llama **dentro del mismo
`with get_connection()`** que escribe la fila, con la conexión de esa escritura. Si
el INSERT del log falla, la excepción sube, el `with` hace rollback y **se cae la
escritura entera** (el cliente, su espejo en `parties`, el asiento del libro): no
puede haber un cambio sin su registro. Es lo contrario de `al_cambiar` del router
de ARCA (`arca_router`), que corre *después* del cambio y es *best-effort* (si falla, lo
loguea y el cambio queda): ahí el aviso es un accesorio de algo que ya se aplicó; acá
el hecho **es** la escritura, y un log incompleto se ve igual que uno completo.

**Sin registrador, no hace nada y no cuesta nada**: `registrar()` vuelve sin tocar
la conexión, y quien instrumenta pregunta `auditando()` antes de leer el estado
anterior, así que sin registrador no hay ningún `SELECT` de más. Un producto que no
audita (o un test) ve exactamente el comportamiento de antes.

**Idempotente y global por proceso**, como el resto de los ganchos de `db/core.py`:
`configurar_auditoria(x)` dos veces deja `x`; `configurar_auditoria(None)` lo quita.
Las suites que arman varias apps en el mismo proceso lo limpian entre tests.
"""
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: Las tres acciones. Son las mismas palabras que usa `libraauth.auditoria`, para que
#: el registrador no tenga que traducir.
CREAR = "crear"
EDITAR = "editar"
BORRAR = "borrar"


@dataclass(frozen=True)
class Cambio:
    """Una escritura sobre una fila, tal como la ve el registrador.

    - `tabla`: la tabla del motor (`"clients"`).
    - `accion`: `CREAR`, `EDITAR` o `BORRAR`.
    - `id`: el id de la fila.
    - `antes` / `despues`: la fila entera como `{columna: valor}`, `None` donde no
      existía (`antes` en el alta, `despues` en la baja). Con las **columnas de la
      base**, no las del producto: renombrarlas es cosa del registrador.

    El registrador decide qué hacer con un `EDITAR` donde nada cambió: el de
    `libraauth` no deja fila.
    """

    tabla: str
    accion: str
    id: Any
    antes: Mapping[str, Any] | None = None
    despues: Mapping[str, Any] | None = None


#: `(conn, cambio) -> None`. Escribe con la `conn` que recibe y **no confirma**.
Registrador = Callable[[Any, Cambio], None]

_lock = threading.Lock()
_registrador: Registrador | None = None


def configurar_auditoria(registrador: Registrador | None) -> None:
    """Fija (o, con `None`, quita) el registrador del proceso."""
    global _registrador
    with _lock:
        _registrador = registrador


def auditando() -> bool:
    """Si hay un registrador. Lo pregunta quien instrumenta una escritura **antes** de
    leer el estado anterior: sin registrador ese `SELECT` no se hace."""
    return _registrador is not None


def registrar(conn, tabla: str, accion: str, id, antes: Mapping[str, Any] | None = None,
              despues: Mapping[str, Any] | None = None) -> None:
    """Le pasa el cambio al registrador, con la conexión de la escritura.

    Va **dentro** del `with get_connection()` de la escritura y después de la última
    sentencia, para que el registro entre y salga con ella. No captura nada: si el
    registrador falla, se cae todo (ver el docstring del módulo).
    """
    registrador = _registrador
    if registrador is None:
        return
    registrador(conn, Cambio(tabla, accion, id, antes, despues))
