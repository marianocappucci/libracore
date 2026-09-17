"""Lo que el restore de una instancia PostgreSQL hace contra el servidor.

Lo usa `libracore.respaldo.restaurar`, que es la unica puerta: la pantalla de
Configuracion de los productos y `panel_admin.py restore-db` la llaman sin
logica propia. Aca vive solo la mecanica — bases temporales, migraciones contra
ellas y el intercambio por nombre—; validar el ZIP, el backup previo y los
directorios de datos siguen en `respaldo`.

## Por que restaurar en una base aparte y no encima de la viva

Hasta el 2026-09-17 el restore era `pg_restore --clean --if-exists` sobre la
base viva. `--clean` borra y recrea **los objetos que estan en el dump**; una
tabla que exista en la base y no en el dump **sobrevive**. Eso tenia dos
consecuencias, y la segunda es la que obligo a cambiarlo:

1. No se cumplia "se va lo de despues": una tabla creada despues del backup
   quedaba ahi, con sus datos.
2. **Correr las migraciones despues del restore fallaba.** Un backup de la
   revision 0040 vuelve `alembic_version` a 0040; si la 0041 creo una tabla,
   esa tabla sobrevivio al `--clean`, y `alembic upgrade head` choca con
   `relation ... already exists`.

Ahora el dump se restaura en `<base>__restore`, una base recien creada; las
migraciones declaradas corren contra ESA base; y recien cuando todo salio bien
se intercambian los nombres. La base queda **exactamente** como el dump mas las
migraciones, y cualquier falla antes del intercambio deja la viva sin tocar.

La base que estaba viva no se borra: queda como `<base>__antes_restore`, que es
la vuelta atras. Se conserva la ultima, y la anterior se reemplaza **recien
despues** de que el intercambio nuevo salio bien — nunca antes, o un restore que
falla a mitad de camino se llevaria la unica red que quedaba.

## Requisitos del servidor

El usuario de la instancia tiene que poder `CREATE DATABASE`,
`pg_terminate_backend` y `ALTER DATABASE ... RENAME`. Medido el 2026-09-17 en
los 21 sidecars del VPS: en todos es superuser (`POSTGRES_USER` de la imagen
oficial).
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

SUFIJO_TEMPORAL = "__restore"
SUFIJO_ANTERIOR = "__antes_restore"
#: Nombre de paso mientras dura el intercambio: la `__antes_restore` del restore
#: ANTERIOR sigue existiendo hasta que este termine bien.
SUFIJO_ANTERIOR_NUEVA = "__antes_restore_nueva"

_ESQUEMAS = ("postgresql://", "postgres://", "postgresql+psycopg://")


class ErrorDeRestore(Exception):
    """Algo del lado del servidor impidio restaurar. `respaldo` lo traduce a
    `BackupInvalido` para la pantalla."""


def es_url_postgres(valor: str) -> bool:
    return valor.startswith(_ESQUEMAS)


def _normal(url: str) -> str:
    """La URL que entiende libpq: sin el `+psycopg` de SQLAlchemy."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def base_de(url: str) -> str:
    return urlsplit(_normal(url)).path.lstrip("/")


def _servidor_de(url: str) -> tuple[str, int]:
    partes = urlsplit(_normal(url))
    return (partes.hostname or "", partes.port or 5432)


def con_base(url: str, base: str) -> str:
    """La misma URL —mismo esquema, usuario, host y parametros— apuntando a
    otra base del mismo servidor."""
    partes = urlsplit(url)
    return urlunsplit((partes.scheme, partes.netloc, f"/{base}", partes.query, partes.fragment))


def _conectar_admin(url: str):
    """Conexion a la base de mantenimiento del mismo servidor, en autocommit:
    `CREATE/ALTER/DROP DATABASE` no pueden correr dentro de una transaccion."""
    import psycopg

    return psycopg.connect(_normal(con_base(url, "postgres")), autocommit=True)


def _ident(nombre: str):
    from psycopg import sql

    return sql.Identifier(nombre)


def _existe(conn, base: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (base,)).fetchone() is not None


def version_servidor(url: str) -> int:
    """La major del servidor, de `server_version_num` (160014 -> 16)."""
    with _conectar_admin(url) as conn:
        return int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000


def version_pg_restore() -> int:
    """La major del `pg_restore` que va a correr.

    🔴 **No es un chequeo de cortesia.** Del 2026-08-09 al 08-12 los
    contenedores traian `pg_restore` 17 contra sidecars 16: el 17 abre la sesion
    con `SET transaction_timeout = 0;`, que el 16 no conoce, y con
    `--single-transaction` el restore abortaba entero, en las siete instancias.
    Nadie lo vio porque nadie restauro. Ahora se pregunta antes de tocar nada.
    """
    try:
        r = subprocess.run(["pg_restore", "--version"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ErrorDeRestore(
            "falta `pg_restore` en esta imagen (paquete postgresql-client)"
        ) from exc
    m = re.search(r"(\d+)(?:\.\d+)?", r.stdout or "")
    if r.returncode != 0 or not m:
        raise ErrorDeRestore(f"no se pudo leer la version de pg_restore: {(r.stdout or r.stderr).strip()}")
    return int(m.group(1))


def crear_temporal(url: str) -> str:
    """Crea `<base>__restore`, vacia, con la codificacion y el locale de la viva.

    Si quedo una de un restore anterior que se corto, se borra: es basura por
    definicion, nunca la base de nadie.
    """
    base = base_de(url)
    temporal = base + SUFIJO_TEMPORAL
    with _conectar_admin(url) as conn:
        fila = conn.execute(
            "SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding), datcollate, datctype "
            "FROM pg_database WHERE datname = %s",
            (base,),
        ).fetchone()
        if fila is None:
            raise ErrorDeRestore(f"la base {base} no existe en el servidor")
        duenio, codificacion, collate, ctype = fila
        from psycopg import sql

        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(_ident(temporal)))
        conn.execute(
            sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 OWNER {} ENCODING {} LC_COLLATE {} LC_CTYPE {}"
            ).format(
                _ident(temporal), _ident(duenio), sql.Literal(codificacion),
                sql.Literal(collate), sql.Literal(ctype),
            )
        )
    return con_base(url, temporal)


def borrar_temporal(url: str) -> None:
    """Borra `<base>__restore` si existe. Nunca borra otra cosa: se exige el
    sufijo, para que un error de cableado no pueda llevarse la viva."""
    base = base_de(url)
    if not base.endswith(SUFIJO_TEMPORAL):
        raise ValueError(f"{base} no es una base temporal de restore")
    from psycopg import sql

    with _conectar_admin(url) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(_ident(base)))


def entorno_para_temporales(pares: list[tuple[str, str]], entorno: dict | None = None) -> dict:
    """El entorno de las migraciones, con cada URL viva cambiada por su temporal.

    Las cadenas (`alembic`, `libraauth-migrar`, `libracore-migrar`...) leen la
    base de sus variables de entorno, cada una con su nombre. Se reemplaza por
    VALOR —servidor, puerto y base— y no por nombre, por la misma razon que
    `panel_admin._urls_postgres_del_contenedor`: el nombre de la variable cambia
    de producto a producto.

    🔴 Si una migracion viera la URL viva, escribiria en la base que el restore
    promete no tocar hasta el final. Hay un test que lo mide.
    """
    salida = dict(os.environ if entorno is None else entorno)
    destinos = {
        (_servidor_de(viva), base_de(viva)): base_de(temporal) for viva, temporal in pares
    }
    for clave, valor in list(salida.items()):
        if not es_url_postgres(valor):
            continue
        clave_destino = (_servidor_de(valor), base_de(valor))
        if clave_destino in destinos:
            salida[clave] = con_base(valor, destinos[clave_destino])
    return salida


def bases_sin_variable(urls: list[str], entorno: dict | None = None) -> list[str]:
    """Las bases que ninguna variable de entorno nombra, con el mismo criterio
    —servidor, puerto y base— que usa `entorno_para_temporales` para reescribir.

    🔴 **Si una base no tiene variable, la migracion no ve la temporal.** La
    reescritura no encuentra que cambiar y la cadena corre contra lo que tenga
    por defecto: otra base, el `sqlalchemy.url` del ini, o nada. Paso el
    2026-09-17 con la suite de LibraDesk, que le pasa la URL a la app en el
    proceso y no en el entorno: alembic fallo contra otra cosa, y lo unico que
    impidio un restore sin migrar fue que ademas fallara. Por eso se pregunta
    antes de tocar nada, y no se espera a ver que hace la cadena.
    """
    entorno = os.environ if entorno is None else entorno
    nombradas = {
        (_servidor_de(valor), base_de(valor)) for valor in entorno.values() if es_url_postgres(valor)
    }
    return [base_de(url) for url in urls if (_servidor_de(url), base_de(url)) not in nombradas]


def correr_migraciones(migraciones, pares: list[tuple[str, str]]) -> None:
    """Corre cada cadena declarada contra las bases temporales, en orden."""
    entorno = entorno_para_temporales(pares)
    for comando in migraciones:
        comando = list(comando)
        try:
            r = subprocess.run(comando, env=entorno, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise ErrorDeRestore(f"no se encontro la migracion `{comando[0]}` en esta imagen") from exc
        if r.returncode != 0:
            raise ErrorDeRestore(
                f"la migracion `{' '.join(comando)}` fallo (codigo {r.returncode}) "
                f"contra el backup restaurado: {errores_de_stderr(r.stderr or r.stdout)}"
            )


def errores_de_stderr(stderr: str, tope: int = 8) -> str:
    """Las lineas que dicen **que** fallo, no la ultima.

    🔴 Hasta el 2026-09-17 se mostraba solo la ultima linea de stderr. En
    `pg_restore` esa linea suele ser `Command was: ...`, que describe la
    sentencia y no el error: en agosto el restore roto de toda la familia se
    reporto como *"Command was: SET transaction_timeout = 0;"*, y la causa real
    (`unrecognized configuration parameter`) estaba dos lineas arriba.
    """
    lineas = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lineas:
        return "sin detalle en stderr"
    utiles = [
        ln for ln in lineas
        if _LINEA_DE_ERROR.search(ln) and not ln.startswith(_RUIDO_DE_SQLALCHEMY)
    ]
    elegidas = utiles[:tope] if utiles else lineas[-1:]
    return " | ".join(elegidas)


#: Las lineas que dicen que fallo. La segunda mitad es la de una excepcion de
#: Python (`sqlalchemy.exc.ProgrammingError: ...`, `psycopg.errors.X: ...`) y la
#: sentencia que la causo (`[SQL: ...]`).
#:
#: 🔴 Hasta el 2026-09-17 solo estaba la primera mitad, y `\berror\b` no matchea
#: `ProgrammingError`: de un traceback de alembic sobrevivia unicamente
#: *"(Background on this error at: https://sqlalche.me/e/20/f405)"*, que es un
#: link y no una causa. El restore de LibraCargo fallaba en el CI y el mensaje
#: no decia por que. La mitad de Python distingue mayusculas a proposito:
#: `self._handle_dbapi_exception(` es una linea del traceback, no el error.
_LINEA_DE_ERROR = re.compile(
    r"(?i:\berror\b|\bFATAL\b|\bDETAIL\b|\bDETALLE\b|\bHINT\b)"
    r"|^[\w.]*(?:Error|Exception|errors\.\w+)\b"
    r"|^\[SQL: "
)
_RUIDO_DE_SQLALCHEMY = "(Background on this error at:"


# ── Los pools de la app despues del intercambio ─────────────────────────────

_CLAVE_GENERACION = "libracore_restore_generacion"
_generacion = 0
_vigilando = False


def vigilar_pools() -> None:
    """Hace que todo pool de SQLAlchemy del proceso descarte, al pedirla, una
    conexion abierta antes del ultimo intercambio.

    🔴 **El intercambio termina las conexiones de la base viva**, y los pools no
    se enteran: la conexion sigue en el pool, y la proxima request que la toma
    muere con *"terminating connection due to administrator command"*. Esperar
    que cada producto pase un `reabrir_conexiones` que deseche **todos** sus
    engines no alcanzo: el 2026-09-17 fallaron los tests de restore de cuatro
    productos, dos de ellos pasando `engine.dispose` — tenian otro engine mas
    (el de auth) que nadie desechaba.

    Por eso el motor no depende de un registro: escucha `connect` y `checkout`
    en la **clase** `Pool`, que alcanza a todos los pools del proceso, tambien
    a los creados antes. Una conexion de una generacion vieja levanta
    `DisconnectionError` al pedirla; SQLAlchemy la invalida y abre otra, sin
    que la request lo note. Las conexiones abiertas despues del restore no
    pagan nada: la comparacion es un entero.

    Alcanza a **este proceso**. Con varios workers de uvicorn, los otros no se
    enteran del restore; hoy los productos corren con uno.

    Si SQLAlchemy no esta instalado no hay pools que cuidar.
    """
    global _vigilando
    if _vigilando:
        return
    try:
        from sqlalchemy import event, exc
        from sqlalchemy.pool import Pool
    except ImportError:
        return

    def _al_conectar(dbapi_connection, connection_record):
        connection_record.info[_CLAVE_GENERACION] = _generacion

    def _al_pedir(dbapi_connection, connection_record, connection_proxy):
        if connection_record.info.get(_CLAVE_GENERACION, 0) < _generacion:
            raise exc.DisconnectionError("conexion abierta antes de un restore")

    event.listen(Pool, "connect", _al_conectar)
    event.listen(Pool, "checkout", _al_pedir)
    _vigilando = True


def descartar_conexiones_anteriores() -> None:
    """Marca como viejas todas las conexiones abiertas hasta ahora. Se llama
    despues de intentar el intercambio, salga bien o mal: si fallo a mitad de
    camino, igual se terminaron conexiones de la viva."""
    global _generacion
    _generacion += 1


def _cerrar_la_puerta(conn, base: str) -> None:
    """Nadie mas entra a `base` y los que estaban se van.

    Primero `ALLOW_CONNECTIONS false` y DESPUES terminar: al reves, un pool que
    reconecta entra de nuevo entre el `terminate` y el `RENAME`, y el rename
    falla con *"is being accessed by other users"*.
    """
    from psycopg import sql

    conn.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(_ident(base)))
    conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (base,),
    )


def _abrir_la_puerta(conn, base: str) -> None:
    from psycopg import sql

    if _existe(conn, base):
        conn.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(_ident(base)))


def _renombrar(conn, desde: str, hacia: str, intentos: int = 20) -> None:
    """`ALTER DATABASE ... RENAME`, reintentando mientras terminan de irse las
    conexiones: `pg_terminate_backend` manda la señal pero no espera."""
    import psycopg
    from psycopg import sql

    for intento in range(intentos):
        try:
            conn.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(_ident(desde), _ident(hacia)))
            return
        except psycopg.errors.ObjectInUse:
            if intento == intentos - 1:
                raise
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (desde,),
            )
            time.sleep(0.25)


def _intercambiar_una(conn, viva: str) -> None:
    """viva -> viva__antes_restore_nueva, viva__restore -> viva."""
    from psycopg import sql

    temporal = viva + SUFIJO_TEMPORAL
    anterior_nueva = viva + SUFIJO_ANTERIOR_NUEVA
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(_ident(anterior_nueva)))
    _cerrar_la_puerta(conn, viva)
    _cerrar_la_puerta(conn, temporal)
    _renombrar(conn, viva, anterior_nueva)
    _renombrar(conn, temporal, viva)
    _abrir_la_puerta(conn, viva)
    _abrir_la_puerta(conn, anterior_nueva)


def _revertir_una(conn, viva: str) -> None:
    """Deshace `_intercambiar_una` desde el punto donde haya quedado.

    Se decide por lo que EXISTE en el servidor y no por hasta donde se creia
    haber llegado: si la falla fue entre los dos renames, lo unico cierto es el
    estado de los nombres.
    """
    temporal = viva + SUFIJO_TEMPORAL
    anterior_nueva = viva + SUFIJO_ANTERIOR_NUEVA
    if _existe(conn, anterior_nueva):
        if _existe(conn, viva):
            _cerrar_la_puerta(conn, viva)
            _renombrar(conn, viva, temporal)
        _cerrar_la_puerta(conn, anterior_nueva)
        _renombrar(conn, anterior_nueva, viva)
    _abrir_la_puerta(conn, viva)


def intercambiar(pares: list[tuple[str, str]]) -> list[str]:
    """Pone las temporales en lugar de las vivas, **todas o ninguna**.

    Con dos bases el orden importa: si la segunda falla, la primera vuelve
    atras. Dejarlas de momentos distintos es peor que no restaurar — o volves
    el dominio y te quedan usuarios de otro momento, o al reves.

    Recien cuando las N salieron bien se reemplaza la `__antes_restore` del
    restore anterior. Devuelve los nombres de las que quedaron como vuelta
    atras.
    """
    from psycopg import sql

    vivas = [base_de(viva) for viva, _ in pares]
    url_admin = pares[0][0]
    with _conectar_admin(url_admin) as conn:
        tocadas: list[str] = []
        try:
            for viva in vivas:
                tocadas.append(viva)
                _intercambiar_una(conn, viva)
        except Exception as exc:
            for viva in reversed(tocadas):
                _revertir_una(conn, viva)
            raise ErrorDeRestore(f"no se pudo poner la base restaurada en lugar de la viva: {exc}") from exc

        anteriores = []
        for viva in vivas:
            anterior = viva + SUFIJO_ANTERIOR
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(_ident(anterior)))
            _renombrar(conn, viva + SUFIJO_ANTERIOR_NUEVA, anterior)
            anteriores.append(anterior)
    return anteriores


def como_volver(anteriores: list[str]) -> str:
    """El texto que va en el reporte: que base quedo y como volver a ella."""
    pasos = []
    for anterior in anteriores:
        viva = anterior[: -len(SUFIJO_ANTERIOR)]
        pasos.append(
            f'ALTER DATABASE "{viva}" RENAME TO "{viva}__descartada"; '
            f'ALTER DATABASE "{anterior}" RENAME TO "{viva}";'
        )
    return (
        "Con la app parada, desde la base `postgres` del mismo servidor: "
        + " ".join(pasos)
    )
