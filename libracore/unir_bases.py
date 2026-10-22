"""Unir la base «core» de un producto dentro de la base de su dominio (ADR-078).

    libracore-unir-bases --prefijo libraclub verificar --informe verificacion.json
    libracore-unir-bases --prefijo libraclub unir      --informe union.json
    libracore-unir-bases --prefijo libraclub revertir  --informe union.json

Los productos de la familia nacieron en dos formas: **una sola base** (ContaLibra, RestoLibra, VentaLibra, LibraDesk: las
tablas del motor y las del producto conviven) y **dos bases** (LibraCargo hasta el 2026-10-05, LibraClub, GestioLibra, MedLibra:
el dominio en una, el motor en `<producto>_core`). Con dos bases no hay clave foránea entre el dominio y el motor ni una
transacción que cubra las dos (la razón de ADR-029 de LibraCargo y de la regla «un vínculo entre dos motores vive en el
producto»), y el nodo de LibraEdge necesita una sola. Esta herramienta hace **lo que se hizo a mano con LibraCargo el 2026-10-05**
(`/root/union-libracargo.sh`), pero con la parte difícil —copiar los datos— hecha y verificada, porque LibraClub y los que
siguen **sí tienen datos en el core**; LibraCargo sólo tenía semillas.

## Los tres comandos

- **`verificar`** (sólo lectura): dice si se puede unir y por qué no. Nada se escribe en ninguna de las dos bases.
- **`unir`** (con la app detenida): crea en el dominio las tablas del motor con sus cadenas de migración y **copia las filas del
  core conservando los ids**, en una transacción. Deja un `union.json` con lo que se copió y una marca en el dominio.
- **`revertir`**: devuelve al core las filas que el dominio recibió *después* de la unión, mientras el core viejo exista intacto.

## Qué se decide acá (y por qué)

1. **`usuarios`, `auth_log` y las demás tablas de `libraauth` no se copian: el dominio manda.** Son las únicas que declaran dos
   motores (`libracore.db.schema` las crea con columnas de texto; `libraauth` las mapea con tipos reales) y, como en ContaLibra
   y VentaLibra, queda **una** tabla, la que ya estaba en el dominio (`CREATE TABLE IF NOT EXISTS` del motor no la pisa). Lo
   que sí se exige es que las filas del core **apunten a usuarios que existen en el dominio y son los mismos** (mismo id,
   mismo `username`): una `usuario_id` colgada, o que en el dominio sea otra persona, impide unir. El `auth_log` del core no se
   lleva (es el log de accesos del motor viejo): si tiene filas se avisa y queda sólo en el respaldo.
2. **`actividad_log` es auditoría compartida: manda la forma del dominio y las filas del core se agregan.** Cuando está en las dos
   bases cada una la creó un escritor distinto (`libraauth` en el dominio: `ts TIMESTAMP`, `entidad_id VARCHAR`; `libracommerce` en
   el core: `ts TEXT`, `entidad_id INTEGER`). Las filas del core se **agregan** al dominio convirtiendo los tipos (el `COPY` de
   texto parsea igual que `USING ts::timestamp` de la `0012_actividad_log` de LibraClub), **sin conservar el `id`** (nada lo
   referencia: `verificar` corta si una clave foránea apunta a la tabla) y dentro de la misma transacción. `verificar` dice cuántas
   se agregan y corta si una conversión falla (un `ts` que no se parsea, un valor que no entra en `varchar(n)`, un NULL contra
   un NOT NULL, una columna del core que el dominio no tiene). Se compara por conteo: lo que tenía el dominio más lo del core.
   `revertir` **no devuelve auditoría**, como `auth_log`.
3. **Cualquier otro nombre de tabla repetido entre las dos bases impide unir.** El caso real es el de `modulos` de GestioLibra
   y MedLibra, o `depositos` de LibraDesk: una tabla del producto con el nombre de una del motor. Se renombra en el producto
   *antes*, no se resuelve acá.
4. **Una tabla de versión de Alembic con el mismo nombre impide unir** cuando son de cadenas distintas. LibraClub usa
   `alembic_version` para su cadena y el motor también: el producto la renombra a `alembic_version_<producto>` primero (como
   LibraCargo con su revisión `0015`). Si la del dominio ya es de la cadena del motor (se migró a mano antes), no hay choque.
5. **El core tiene que estar en la cabeza** de `libracore` (y de `libracommerce`, si usa su cadena): con el mismo esquema en
   las dos puntas se copia columna por columna sin traducir nada. Si no lo está, `libracore-migrar upgrade` sobre el core.
6. **«Sólo semillas».** Al crear las tablas del motor en el dominio, la baseline siembra tres: `cajas` («Caja Principal»),
   `depositos` («Depósito Principal») y `categorias_egreso` (diez). Una tabla del destino se vacía antes de copiar si y sólo si
   (a) es una de esas tres, (b) **todas** sus filas son del conjunto sembrado (por `nombre`), y (c) ninguna tabla hija del
   destino tiene filas que la referencien. Cualquier otra fila en el destino es un dato: la unión se corta, no se pisa.
7. **Una marca en el dominio** (`libracore_union_bases`, una fila): `iniciada` con la foto de las tablas que el dominio tenía
   *antes* (para decidir después qué tabla es «del dominio» aunque una corrida cortada haya creado las del motor) y `unida` con
   el informe. Es lo que hace que `unir` sea idempotente y lo diga.
8. **Los ids se conservan** (salvo los de `actividad_log`, punto 2). Se copia con `COPY` (texto exacto: ni redondeo de `NUMERIC` ni reescritura de fechas), en orden
   topológico por claves foráneas (una autorreferencia como `cc_asientos.contrapartida_de` no pide orden: la clave foránea se
   comprueba al terminar el `COPY`). Después `setval` de cada secuencia a `max(id)`.

## Lo que no hace

No toca el compose, no detiene la app, no hace el respaldo ni borra el core: son pasos del procedimiento (README, «Unir las dos
bases de un producto»). No sirve con SQLite (la familia de dos bases corre sobre PostgreSQL). No migra datos del producto: sus
tablas ya están en el dominio.

## Contraseñas

Las URL salen de las variables de entorno de la instancia (`<PREFIJO>_DATABASE_URL`, `<PREFIJO>_LIBRACORE_DATABASE_URL`, como
`libracore-migrar`), o de `--core`/`--dominio` (`env:NOMBRE` lee otra variable; una URL en la línea de comandos queda en el
historial y en `ps`, así que no se recomienda). Nada de lo que se imprime lleva usuario ni contraseña: sólo `host:puerto/base`.

Salida: 0 todo bien; 1 algo impide unir (o la unión se deshizo: la transacción no deja nada a medias); 2 error de uso o de
conexión.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

#: Versión del formato de `union.json`.
VERSION_DEL_INFORME = 1

#: La marca que `unir` deja en el dominio.
TABLA_MARCA = "libracore_union_bases"

#: Tablas que declaran `libracore` y `libraauth` a la vez: si el dominio ya las tiene, **manda el dominio** y las del core no se
#: copian (decisión 1 del módulo). `usuarios` y `auth_log` son las dos que el motor crea; el resto las crea sólo `libraauth`.
TABLAS_DE_AUTH = (
    "usuarios",
    "auth_log",
    "password_reset_tokens",
    "smtp_settings",
    "demo_codigos",
    "aceptaciones_terminos",
)

#: La tabla de versión de cada cadena que esta herramienta sabe crear en el dominio.
TABLA_DE_VERSION = {"libracore": "alembic_version", "libracommerce": "alembic_version_libracommerce"}
#: La de `libraauth`: la maneja `libraauth-migrar` y vive donde viva `usuarios`.
TABLA_DE_VERSION_AUTH = "alembic_version_libraauth"

#: Lo que la baseline de `libracore` siembra en una base nueva, por `nombre` (decisión 5). Un test lo compara contra una base
#: recién migrada: si el motor siembra algo más, se pone en rojo y hay que sumarlo acá.
SEMILLAS = {
    "cajas": ("nombre", frozenset({"Caja Principal"})),
    "depositos": ("nombre", frozenset({"Depósito Principal"})),
    "categorias_egreso": (
        "nombre",
        frozenset({
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
        }),
    ),
}


#: La contabilidad de la cadena numerada vieja de `libracommerce` (`db/migrations.py`, congelada): una fila por migración aplicada,
#: que la baseline de Alembic de `libracommerce` vuelve a escribir al crear las tablas en el dominio. Es una tabla de versión, no
#: un dato: no se copia, y se exige que el dominio tenga aplicadas todas las versiones que tiene el core (ensayado contra
#: `libracommerce` real: sin esto la corrida cortaba por «el destino tiene filas que no son semillas»). Nombre → columna.
TABLAS_DE_CADENA_NUMERADA = {"schema_migrations": "version"}


#: Tablas de **auditoría compartida**: el producto y el motor escriben en la misma, y cuando están en las dos bases cada una la
#: creó un escritor distinto con una forma distinta (ADR-078, punto 4). Hoy es `actividad_log`: en el dominio la crea el listener
#: de `libraauth` (`ts TIMESTAMP`, `entidad_id VARCHAR`) y en el core `libracommerce` (`ts TEXT`, `entidad_id INTEGER`). Manda la
#: forma del dominio; las filas del core se **agregan** (sin conservar el `id`, que nadie referencia) convirtiendo los tipos.
TABLAS_DE_AUDITORIA = ("actividad_log",)


class ErrorDeUso(Exception):
    """Falta una URL, la URL no es de PostgreSQL, o el informe no sirve: no es un problema de los datos."""


class ErrorDeConexion(Exception):
    """No se pudo abrir una de las bases. El mensaje no lleva contraseña."""


class ErrorDeUnion(Exception):
    """La unión (o la reversión) se cortó: la transacción se deshizo y la base quedó como estaba."""


# --- URL -----------------------------------------------------------------------------------------------------------------


def _libpq(url: str) -> str:
    """La URL tal como la entiende libpq: sin el `+psycopg` de SQLAlchemy y con el esquema largo."""
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://"):]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def es_postgres(url: str) -> bool:
    return _libpq(url).startswith("postgresql://")


def ocultar(url: str) -> str:
    """`host:puerto/base`, sin usuario ni contraseña: lo único que se imprime de una URL."""
    partes = urlsplit(_libpq(url))
    host = partes.hostname or "?"
    puerto = f":{partes.port}" if partes.port else ""
    return f"{host}{puerto}/{partes.path.lstrip('/')}"


def _misma_base(a: str, b: str) -> bool:
    pa, pb = urlsplit(_libpq(a)), urlsplit(_libpq(b))
    return (
        (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and (pa.port or 5432) == (pb.port or 5432)
        and pa.path.strip("/") == pb.path.strip("/")
    )


def _sin_secretos(texto: str, *urls: str) -> str:
    """Saca de un mensaje de error cualquier contraseña de las URL que se usaron."""
    for url in urls:
        clave = urlsplit(_libpq(url)).password
        if clave:
            texto = texto.replace(clave, "***")
    return texto


def resolver_url(valor: str | None, prefijo: str, *, core: bool, entorno=None) -> str:
    """La URL de una de las dos bases: explícita (`--core`), `env:NOMBRE`, o la de la instancia según el prefijo."""
    env = os.environ if entorno is None else entorno
    lado = "core" if core else "dominio"
    if valor:
        if valor.startswith("env:"):
            nombre = valor[4:].strip()
            url = (env.get(nombre) or "").strip()
            if not url:
                raise ErrorDeUso(f"--{lado} {valor}: la variable {nombre} no está definida (o está vacía).")
            return url
        return valor
    from .db.url_de_instancia import nombres_aceptados, url_de_instancia

    url = url_de_instancia(prefijo, core=core, entorno=env)
    if not url:
        raise ErrorDeUso(
            f"No hay URL de la base {lado} de '{prefijo}': definí " + " o ".join(nombres_aceptados(prefijo, core=core))
            + f" en el entorno, o pasá --{lado}."
        )
    return url


def conectar(url: str, *, solo_lectura: bool = False, repeatable: bool = False):
    if not es_postgres(url):
        raise ErrorDeUso("Las dos bases tienen que ser PostgreSQL: esta herramienta no sirve con SQLite.")
    try:
        conn = psycopg.connect(_libpq(url), autocommit=False)
    except psycopg.OperationalError as e:
        raise ErrorDeConexion(f"No se pudo conectar a {ocultar(url)}: {_sin_secretos(str(e).strip(), url)}") from None
    if solo_lectura:
        conn.read_only = True
    if repeatable:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    return conn


# --- Lectura del esquema -------------------------------------------------------------------------------------------------


def _ident(nombre: str):
    return sql.Identifier(nombre)


def _lista(nombres):
    return sql.SQL(", ").join(sql.Identifier(n) for n in nombres)


def _regclass(tabla: str) -> str:
    return '"' + tabla.replace('"', '""') + '"'


def tablas_de(conn) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
            "AND c.relkind IN ('r', 'p') AND NOT c.relispartition ORDER BY 1"
        ).fetchall()
    ]


@dataclass(frozen=True)
class Columna:
    nombre: str
    tipo: str
    generada: bool


def columnas_de(conn, tabla: str) -> list[Columna]:
    filas = conn.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attgenerated <> '' "
        "FROM pg_attribute a WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
        (_regclass(tabla),),
    ).fetchall()
    return [Columna(*f) for f in filas]


def clave_primaria_de(conn, tabla: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
            "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary ORDER BY array_position(i.indkey::int2[], a.attnum)",
            (_regclass(tabla),),
        ).fetchall()
    ]


def secuencias_de(conn, tabla: str, columnas: list[Columna]) -> list[tuple[str, str]]:
    """`(columna, secuencia)` de las columnas que llenan una secuencia (serial o identity)."""
    salida = []
    for c in columnas:
        fila = conn.execute("SELECT pg_get_serial_sequence(%s, %s)", (_regclass(tabla), c.nombre)).fetchone()
        if fila and fila[0]:
            salida.append((c.nombre, fila[0]))
    return salida


@dataclass(frozen=True)
class ClaveForanea:
    nombre: str
    tabla: str
    columnas: tuple[str, ...]
    destino: str
    columnas_destino: tuple[str, ...]


def claves_foraneas_de(conn) -> list[ClaveForanea]:
    filas = conn.execute(
        """
        SELECT con.conname, cl.relname, cf.relname,
               ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, o)
                     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n ORDER BY k.o),
               ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
                     JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n ORDER BY k.o)
        FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        JOIN pg_class cf ON cf.oid = con.confrelid
        WHERE con.contype = 'f' AND cl.relnamespace = 'public'::regnamespace AND cf.relnamespace = 'public'::regnamespace
        ORDER BY cl.relname, con.conname
        """
    ).fetchall()
    return [ClaveForanea(f[0], f[1], tuple(f[3]), f[2], tuple(f[4])) for f in filas]


def version_de(conn, tabla: str) -> str | None:
    """La revisión que guarda una tabla de versión de Alembic, o `None` si está vacía."""
    fila = conn.execute(f"SELECT version_num FROM {_regclass(tabla)} LIMIT 1").fetchone()
    return fila[0] if fila else None


def conexiones_ajenas(conn) -> int:
    """Cuántas otras conexiones de clientes hay abiertas a esta base: la señal de que la app no está detenida."""
    return conn.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() AND backend_type = 'client backend'"
    ).fetchone()[0]


# --- Cadenas de migración ------------------------------------------------------------------------------------------------


@dataclass
class Cadena:
    """Una cadena de Alembic que `unir` sabe correr sobre el dominio."""

    nombre: str
    tabla: str
    revisiones: frozenset
    cabeza: str
    subir: object  # callable(url) -> None


def _cadena_desde(nombre: str, tabla: str, directorio, subir) -> Cadena:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(directorio))
    guion = ScriptDirectory.from_config(cfg)
    return Cadena(nombre, tabla, frozenset(r.revision for r in guion.walk_revisions()), guion.get_current_head(), subir)


def cadena_libracore() -> Cadena:
    from . import migrar

    return _cadena_desde("libracore", TABLA_DE_VERSION["libracore"], migrar.DIRECTORIO, _subir_libracore)


def cadena_libracommerce() -> Cadena | None:
    """La cadena de `libracommerce`, o `None` si el entorno no lo tiene instalado (no es dependencia del motor)."""
    try:
        from libracommerce import migrar as m
    except ModuleNotFoundError:
        return None
    return _cadena_desde("libracommerce", TABLA_DE_VERSION["libracommerce"], m.DIRECTORIO, m.upgrade)


def _subir_libracore(url: str) -> None:
    """`libracore-migrar upgrade` sobre el dominio, sin dejar `libracore.db.core` apuntando a él.

    El `env.py` del motor llama a `core.configure(destino)`: es estado global del proceso, y esta herramienta no es la app.
    """
    from . import migrar
    from .db import core

    guardado = (core._db_path, core._database_url, core._timeout, core._extra_pragmas)
    try:
        migrar.upgrade(_libpq(url))
    finally:
        core._db_path, core._database_url, core._timeout, core._extra_pragmas = guardado


# --- Resúmenes -----------------------------------------------------------------------------------------------------------


def _dinero(valor) -> str:
    return "0" if valor is None else format(Decimal(valor), "f")


def resumen_de(conn, tabla: str, columnas: list[Columna], *, hasta_id: int | None = None) -> dict:
    """Conteo, sumas de dinero, `max(id)` y una huella de contenido de una tabla.

    La **huella** es la suma de 60 bits de `md5(fila::text)` de cada fila: no depende del orden (no hay `ORDER BY` que cambie
    con el *collation* de cada base) ni de la memoria, y dos tablas con las mismas filas la dan igual. No es una defensa
    contra alguien que la falsifique: es para ver que lo copiado es lo mismo.
    """
    propias = [c for c in columnas if not c.generada]
    dinero_del_motor = _dinero_del_motor()
    con_suma = [c.nombre for c in propias if c.tipo.startswith("numeric") or (tabla, c.nombre) in dinero_del_motor]
    nombres = sorted(c.nombre for c in propias)
    fila_texto = sql.SQL("ROW({})::text").format(_lista(nombres)) if nombres else sql.SQL("''")
    tiene_id = any(c.nombre == "id" and c.tipo in ("bigint", "integer", "smallint") for c in propias)
    partes = [sql.SQL("count(*)")]
    partes += [sql.SQL("SUM({}::numeric)").format(_ident(c)) for c in con_suma]
    partes.append(
        sql.SQL("COALESCE(SUM((('x' || substr(md5({}), 1, 15))::bit(60))::bigint), 0)").format(fila_texto)
    )
    partes.append(sql.SQL("MAX(id)") if tiene_id else sql.SQL("NULL"))
    consulta = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(partes), _ident(tabla))
    if hasta_id is not None and tiene_id:
        consulta += sql.SQL(" WHERE id <= {}").format(sql.Literal(hasta_id))
    fila = conn.execute(consulta).fetchone()
    n = len(con_suma)
    return {
        "filas": fila[0],
        "sumas": {c: _dinero(v) for c, v in zip(con_suma, fila[1:1 + n], strict=True)},
        "huella": str(fila[1 + n]),
        "max_id": fila[2 + n],
    }


def _dinero_del_motor() -> frozenset:
    from .db.schema import COLUMNAS_DE_DINERO

    return frozenset(COLUMNAS_DE_DINERO)


# --- Orden de copia ------------------------------------------------------------------------------------------------------


def orden_topologico(tablas: list[str], fks: list[ClaveForanea]) -> list[str]:
    """Las tablas con sus padres primero. Una autorreferencia no cuenta (el `COPY` es una sentencia); un ciclo entre tablas, sí."""
    tablas_set = set(tablas)
    padres = {t: set() for t in tablas}
    for fk in fks:
        if fk.tabla in tablas_set and fk.destino in tablas_set and fk.tabla != fk.destino:
            padres[fk.tabla].add(fk.destino)
    orden: list[str] = []
    pendientes = dict(padres)
    while pendientes:
        listas = sorted(t for t, p in pendientes.items() if not (p - set(orden)))
        if not listas:
            raise ErrorDeUnion("Hay un ciclo de claves foráneas entre tablas: " + ", ".join(sorted(pendientes)))
        for t in listas:
            orden.append(t)
            del pendientes[t]
    return orden


# --- Hallazgos y verificación --------------------------------------------------------------------------------------------


@dataclass
class Hallazgo:
    nivel: str  # "error" | "aviso"
    codigo: str
    mensaje: str


@dataclass
class Verificacion:
    prefijo: str
    core: str
    dominio: str
    hallazgos: list[Hallazgo] = field(default_factory=list)
    misma_base: bool = False
    ya_unida: bool = False
    marca: dict | None = None
    cadenas: dict = field(default_factory=dict)
    a_copiar: list[str] = field(default_factory=list)
    no_copiadas: dict = field(default_factory=dict)
    a_vaciar: list[str] = field(default_factory=list)
    tablas: dict = field(default_factory=dict)
    dominio_previo: dict = field(default_factory=dict)
    auditoria: dict = field(default_factory=dict)
    previas: list[str] = field(default_factory=list)
    conexiones: dict = field(default_factory=dict)

    def error(self, codigo: str, mensaje: str) -> None:
        self.hallazgos.append(Hallazgo("error", codigo, mensaje))

    def aviso(self, codigo: str, mensaje: str) -> None:
        self.hallazgos.append(Hallazgo("aviso", codigo, mensaje))

    @property
    def errores(self) -> list[Hallazgo]:
        return [h for h in self.hallazgos if h.nivel == "error"]

    @property
    def avisos(self) -> list[Hallazgo]:
        return [h for h in self.hallazgos if h.nivel == "aviso"]

    @property
    def ok(self) -> bool:
        return not self.errores


def leer_marca(conn) -> dict | None:
    if TABLA_MARCA not in tablas_de(conn):
        return None
    fila = conn.execute(
        f"SELECT prefijo, estado, tablas_previas, iniciada_en, unida_en, informe FROM {_regclass(TABLA_MARCA)} WHERE id = 1"
    ).fetchone()
    if not fila:
        return None
    return {
        "prefijo": fila[0],
        "estado": fila[1],
        "tablas_previas": json.loads(fila[2]),
        "iniciada_en": fila[3],
        "unida_en": fila[4],
        "informe": json.loads(fila[5]) if fila[5] else None,
    }


def _estado_del_destino(dom, tabla: str, fks_dom: list[ClaveForanea]) -> str:
    """`vacia`, `semillas` (se puede vaciar) o `con_datos` (se corta la unión). La tabla tiene que existir."""
    filas = dom.execute(sql.SQL("SELECT count(*) FROM {}").format(_ident(tabla))).fetchone()[0]
    if filas == 0:
        return "vacia"
    semilla = SEMILLAS.get(tabla)
    if semilla is None:
        return "con_datos"
    columna, conocidos = semilla
    nombres = {r[0] for r in dom.execute(sql.SQL("SELECT {} FROM {}").format(_ident(columna), _ident(tabla))).fetchall()}
    if not nombres <= conocidos:
        return "con_datos"
    for fk in fks_dom:
        if fk.destino == tabla and fk.tabla != tabla and fk.tabla in set(tablas_de(dom)):
            hijas = dom.execute(sql.SQL("SELECT count(*) FROM {}").format(_ident(fk.tabla))).fetchone()[0]
            if hijas:
                return "con_datos"
    return "semillas"


def verificar(core_url: str, dominio_url: str, prefijo: str, *, conexiones_impiden: bool = False) -> Verificacion:
    """Sólo lectura. Junta todos los impedimentos (no se detiene en el primero) y los datos del informe.

    Las conexiones ajenas a las bases son un aviso para quien sólo mira, y un impedimento para `unir` y `revertir`."""
    v = Verificacion(prefijo, ocultar(core_url), ocultar(dominio_url))
    if _misma_base(core_url, dominio_url):
        v.misma_base = True
        v.aviso("misma_base", "El core y el dominio son la misma base: no hay nada que unir.")
        return v
    with contextlib.closing(conectar(core_url, solo_lectura=True, repeatable=True)) as core, contextlib.closing(
        conectar(dominio_url, solo_lectura=True, repeatable=True)
    ) as dom:
        _verificar(v, core, dom)
        v.conexiones = {"core": conexiones_ajenas(core), "dominio": conexiones_ajenas(dom)}
    for lado, n in v.conexiones.items():
        if n:
            (v.error if conexiones_impiden else v.aviso)(
                "conexiones_abiertas", f"Hay {n} conexión(es) abiertas a la base {lado}: ¿la app está detenida?"
            )
    return v


def _verificar(v: Verificacion, core, dom) -> None:  # noqa: C901 - es una lista de chequeos, no una lógica anidada
    t_core, t_dom = set(tablas_de(core)), set(tablas_de(dom))
    v.marca = leer_marca(dom)
    if v.marca and v.marca["estado"] == "unida":
        v.ya_unida = True
        v.aviso("ya_unida", f"El dominio ya está unido ({v.marca['unida_en']}): no hay nada que hacer.")
        return
    previas = set(v.marca["tablas_previas"]) if v.marca else set(t_dom)
    v.previas = sorted(previas)
    t_dom_propias = t_dom - {TABLA_MARCA}

    # 1. Cadenas de migración y tablas de versión.
    cadenas = {"libracore": cadena_libracore()}
    cadenas["libracommerce"] = cadena_libracommerce() if TABLA_DE_VERSION["libracommerce"] in t_core else None
    versiones_core = {t for t in t_core if t.startswith("alembic_version")}
    versiones_dom = {t for t in t_dom_propias if t.startswith("alembic_version")}
    numeradas = set(TABLAS_DE_CADENA_NUMERADA) & t_core
    for t in sorted(numeradas & t_dom_propias):
        col = TABLAS_DE_CADENA_NUMERADA[t]
        faltan = {r[0] for r in core.execute(sql.SQL("SELECT {} FROM {}").format(_ident(col), _ident(t))).fetchall()} - {
            r[0] for r in dom.execute(sql.SQL("SELECT {} FROM {}").format(_ident(col), _ident(t))).fetchall()
        }
        if faltan:
            v.error(
                "version_repetida",
                f"La tabla {t} del dominio no tiene aplicadas las versiones {sorted(faltan)} que sí tiene la del core: "
                "no es la cadena numerada de libracommerce, o está atrasada.",
            )
    for nombre, cadena in cadenas.items():
        tabla = TABLA_DE_VERSION[nombre]
        en_core = version_de(core, tabla) if tabla in t_core else None
        en_dom = version_de(dom, tabla) if tabla in t_dom_propias else None
        v.cadenas[nombre] = {
            "tabla": tabla,
            "core": en_core,
            "dominio": en_dom,
            "cabeza": cadena.cabeza if cadena else None,
        }
        if nombre == "libracommerce" and tabla in t_core and cadena is None:
            v.error(
                "falta_libracommerce",
                f"El core tiene la cadena de libracommerce ({tabla}={en_core}) y este entorno no tiene libracommerce "
                "instalado: no hay con qué crear sus tablas en el dominio. Corré la herramienta desde la imagen del producto.",
            )
            continue
        if cadena is None:
            continue
        if nombre == "libracore" and tabla not in t_core:
            v.error(
                "core_sin_version",
                f"El core no tiene {tabla}: no se sabe en qué revisión está. Corré `libracore-migrar upgrade` sobre el core.",
            )
        elif en_core is not None and en_core != cadena.cabeza:
            v.error(
                "core_desactualizado",
                f"El core de {nombre} está en {en_core} y la cabeza es {cadena.cabeza}: con esquemas distintos no se copia. "
                f"Subí el core primero ({'libracore-migrar' if nombre == 'libracore' else 'libracommerce-migrar'} upgrade).",
            )
        if tabla in t_dom_propias and (en_dom is None or en_dom not in cadena.revisiones):
            v.error(
                "version_repetida",
                f"La tabla {tabla} del dominio no es de la cadena de {nombre} (guarda {en_dom!r}): choca con la del motor. "
                f"El producto tiene que renombrar la suya (p. ej. alembic_version_{v.prefijo}) antes de unir.",
            )
    dominio_con_cadena = v.cadenas["libracore"]["dominio"] in cadenas["libracore"].revisiones
    conocidas = set(TABLA_DE_VERSION.values())
    for tabla in sorted(versiones_core - conocidas):
        if tabla == TABLA_DE_VERSION_AUTH and tabla in versiones_dom:
            v.aviso("version_auth_en_los_dos", f"{tabla} está en las dos bases: manda la del dominio.")
        else:
            v.error(
                "cadena_desconocida",
                f"El core tiene la tabla de versión {tabla}, de una cadena que esta herramienta no sabe crear en el dominio.",
            )

    # 2. Choques de nombres.
    comunes = (t_core & t_dom_propias) - versiones_core - versiones_dom - numeradas
    dominio_manda, tolerados, auditoria = [], [], []
    for t in sorted(comunes):
        if t in TABLAS_DE_AUTH and t in previas:
            dominio_manda.append(t)
        elif t in TABLAS_DE_AUDITORIA and t in previas:
            auditoria.append(t)
        elif t in previas and not dominio_con_cadena:
            v.error(
                "choque_de_tablas",
                f"La tabla {t} existe en el core y en el dominio, y no es una de las compartidas con libraauth "
                f"({', '.join(TABLAS_DE_AUTH[:2])}…): es una del producto con el nombre de una del motor. Renombrala en el producto.",
            )
        else:
            # Ya estaba por una corrida cortada o por un `libracore-migrar` previo: se mira qué hay adentro (paso 5).
            tolerados.append(t)

    # 3. Qué se copia, en qué orden.
    a_copiar = sorted(t_core - versiones_core - numeradas - set(dominio_manda) - set(auditoria))
    fks_core = claves_foraneas_de(core)
    try:
        v.a_copiar = orden_topologico(a_copiar, fks_core)
    except ErrorDeUnion as e:
        v.a_copiar = a_copiar
        v.error("ciclo_de_fks", str(e))
    for t in dominio_manda:
        filas = core.execute(sql.SQL("SELECT count(*) FROM {}").format(_ident(t))).fetchone()[0]
        v.no_copiadas[t] = {"motivo": "dominio_manda", "filas_core": filas}
        if filas and t != "usuarios":
            v.aviso("no_se_copia", f"{t}: {filas} fila(s) del core no se copian (manda la del dominio); quedan en el respaldo.")

    # 3b. Auditoría compartida: se agrega al dominio, convirtiendo tipos.
    for t in auditoria:
        _verificar_auditoria(v, core, dom, t, fks_core)

    # 4. Claves foráneas del core que apuntan a una tabla que no se copia.
    _verificar_claves_foraneas(v, core, dom, fks_core, dominio_manda)

    # 5. El destino: qué hay en las tablas del dominio que se van a llenar.
    fks_dom = claves_foraneas_de(dom)
    for t in tolerados:
        if t in v.a_copiar:
            estado = _estado_del_destino(dom, t, fks_dom)
            if estado == "con_datos":
                v.error(
                    "destino_con_datos",
                    f"La tabla {t} del dominio ya tiene filas que no son semillas del motor: no se pisa. "
                    "Si es una unión a medias, restaurá el respaldo del dominio y empezá de nuevo.",
                )
            elif estado == "semillas":
                v.a_vaciar.append(t)

    # 6. Conteos y sumas del core (y conteos del dominio, que no se toca).
    for t in v.a_copiar:
        v.tablas[t] = resumen_de(core, t, columnas_de(core, t))
    for t in sorted(t_dom_propias - set(v.a_copiar) - {TABLA_MARCA}):
        if t in versiones_dom or t in dominio_manda or t in numeradas or t in auditoria:
            continue
        v.dominio_previo[t] = dom.execute(sql.SQL("SELECT count(*) FROM {}").format(_ident(t))).fetchone()[0]


def _reglas_de_columnas(conn, tabla: str) -> dict:
    """`{columna: (tipo, not_null, tiene_default)}` de las columnas propias (no generadas) de una tabla."""
    filas = conn.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, a.atthasdef FROM pg_attribute a "
        "WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = '' ORDER BY a.attnum",
        (_regclass(tabla),),
    ).fetchall()
    return {f[0]: (f[1], f[2], f[3]) for f in filas}


_LARGO = re.compile(r"^(?:character varying|character|varchar|char)\((\d+)\)$")
_CON_HUSO = r"[0-9]:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$"


def _verificar_auditoria(v: Verificacion, core, dom, tabla: str, fks_core: list[ClaveForanea]) -> None:
    """Una tabla de auditoría que está en las dos bases: qué se agrega, y si cada conversión de tipo entra.

    Se corta por lo que haría fallar el `COPY` (o, peor, lo dejaría pasar cambiado), no al final con la transacción deshecha.
    """
    en_core, en_dom = _reglas_de_columnas(core, tabla), _reglas_de_columnas(dom, tabla)
    propias = [c for c in en_core if c != "id"]
    info = {
        "filas_core": core.execute(sql.SQL("SELECT count(*) FROM {}").format(_ident(tabla))).fetchone()[0],
        "filas_dominio": dom.execute(sql.SQL("SELECT count(*) FROM {}").format(_ident(tabla))).fetchone()[0],
        "columnas": propias,
        "conversiones": {},
    }
    v.auditoria[tabla] = info
    # Nadie tiene que referenciarla: sin conservar los ids, una FK hacia acá quedaría apuntando a otra fila.
    for fk in fks_core + claves_foraneas_de(dom):
        if fk.destino == tabla:
            v.error("auditoria_referenciada", f"{fk.tabla}.{','.join(fk.columnas)} tiene una clave foránea hacia {tabla}: sus ids no se conservan.")
    sobran = [c for c in propias if c not in en_dom]
    if sobran:
        v.error("auditoria_incompatible", f"{tabla}: el core tiene columnas que el dominio no tiene ({', '.join(sobran)}).")
    faltan = [c for c, (_, no_nula, con_default) in en_dom.items() if c != "id" and c not in en_core and no_nula and not con_default]
    if faltan:
        v.error("auditoria_incompatible", f"{tabla}: el dominio exige columnas que el core no tiene ({', '.join(faltan)}).")
    for c in (c for c in propias if c in en_dom):
        tipo_core, tipo_dom, no_nula = en_core[c][0], en_dom[c][0], en_dom[c][1]
        col = _ident(c)
        if tipo_core != tipo_dom:
            info["conversiones"][c] = f"{tipo_core} → {tipo_dom}"
        if no_nula:
            nulas = core.execute(sql.SQL("SELECT count(*) FROM {} WHERE {} IS NULL").format(_ident(tabla), col)).fetchone()[0]
            if nulas:
                v.error("auditoria_no_entra", f"{tabla}.{c}: {nulas} fila(s) del core tienen NULL y en el dominio la columna es NOT NULL.")
        if tipo_core == tipo_dom:
            continue
        largo = _LARGO.match(tipo_dom)
        if largo:
            largos = core.execute(
                sql.SQL("SELECT count(*), max(length({}::text)) FROM {} WHERE length({}::text) > %s").format(col, _ident(tabla), col),
                (int(largo.group(1)),),
            ).fetchone()
            if largos[0]:
                v.error(
                    "auditoria_no_entra",
                    f"{tabla}.{c}: {largos[0]} fila(s) del core no entran en {tipo_dom} (la más larga tiene {largos[1]} caracteres).",
                )
        elif tipo_dom != "text":
            malas, ejemplos = _no_convertibles(core, tabla, c, tipo_dom)
            if malas:
                v.error(
                    "conversion_fallida",
                    f"{tabla}.{c}: {malas} valor(es) del core no se pueden convertir a {tipo_dom} (p. ej. {ejemplos}).",
                )
            elif tipo_dom.startswith("timestamp"):
                con_huso = core.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE {}::text ~ %s").format(_ident(tabla), col), (_CON_HUSO,)
                ).fetchone()[0]
                if con_huso:
                    v.aviso(
                        "auditoria_con_huso",
                        f"{tabla}.{c}: {con_huso} valor(es) traen huso horario; al pasar a {tipo_dom} se descarta y queda la hora escrita.",
                    )
    if info["filas_core"]:
        v.aviso(
            "auditoria_se_agrega",
            f"{tabla}: se agregan {info['filas_core']} fila(s) del core a las {info['filas_dominio']} del dominio (ids nuevos).",
        )


def _no_convertibles(conn, tabla: str, columna: str, tipo: str) -> tuple[int, str]:
    """Cuántos valores de una columna no entran en `tipo`, y unos ejemplos. Sin `pg_input_is_valid` (PostgreSQL < 16), se prueba
    la conversión entera y, si falla, se informa el error del servidor."""
    col = _ident(columna)
    try:
        with conn.transaction():
            malas = conn.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE {} IS NOT NULL AND NOT pg_input_is_valid({}::text, %s)").format(
                    _ident(tabla), col, col
                ),
                (tipo,),
            ).fetchone()[0]
            ejemplos = conn.execute(
                sql.SQL("SELECT {}::text FROM {} WHERE {} IS NOT NULL AND NOT pg_input_is_valid({}::text, %s) LIMIT 3").format(
                    col, _ident(tabla), col, col
                ),
                (tipo,),
            ).fetchall()
        return malas, ", ".join(repr(e[0]) for e in ejemplos)
    except psycopg.errors.UndefinedFunction:
        pass
    try:
        with conn.transaction():
            conn.execute(
                sql.SQL("SELECT count({}::{}) FROM {}").format(col, sql.SQL(tipo), _ident(tabla))
            ).fetchone()
    except psycopg.Error as e:
        return 1, " ".join(str(e).split())[:120]
    return 0, ""


def _verificar_claves_foraneas(v: Verificacion, core, dom, fks_core, dominio_manda) -> None:
    """Las filas del core que apuntan a algo que no va a estar en el dominio (o que en el dominio es otra cosa)."""
    copiadas = set(v.a_copiar)
    referidos: dict[str, set] = {}
    for fk in fks_core:
        if fk.tabla not in copiadas:
            continue
        no_nulas = sql.SQL(" AND ").join(sql.SQL("c.{} IS NOT NULL").format(_ident(c)) for c in fk.columnas)
        if fk.destino in copiadas:
            donde = sql.SQL(" AND ").join(
                sql.SQL("d.{} = c.{}").format(_ident(b), _ident(a)) for a, b in zip(fk.columnas, fk.columnas_destino, strict=True)
            )
            n = core.execute(
                sql.SQL("SELECT count(*) FROM {} c WHERE {} AND NOT EXISTS (SELECT 1 FROM {} d WHERE {})").format(
                    _ident(fk.tabla), no_nulas, _ident(fk.destino), donde
                )
            ).fetchone()[0]
            if n:
                v.error("fk_colgada", f"{fk.tabla}.{','.join(fk.columnas)} → {fk.destino}: {n} fila(s) del core sin padre.")
        elif fk.destino in dominio_manda:
            if len(fk.columnas) != 1:
                v.error("fk_no_soportada", f"{fk.tabla}.{fk.nombre}: clave foránea compuesta hacia {fk.destino}.")
                continue
            col, col_dest = fk.columnas[0], fk.columnas_destino[0]
            valores = [
                r[0]
                for r in core.execute(
                    sql.SQL("SELECT DISTINCT {} FROM {} WHERE {} IS NOT NULL ORDER BY 1").format(
                        _ident(col), _ident(fk.tabla), _ident(col)
                    )
                ).fetchall()
            ]
            if not valores:
                continue
            referidos.setdefault(fk.destino, set()).update(valores)
            existen = {
                r[0]
                for r in dom.execute(
                    sql.SQL("SELECT {} FROM {} WHERE {} = ANY(%s)").format(_ident(col_dest), _ident(fk.destino), _ident(col_dest)),
                    (valores,),
                ).fetchall()
            }
            faltan = [x for x in valores if x not in existen]
            if faltan:
                filas = core.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE {} = ANY(%s)").format(_ident(fk.tabla), _ident(col)), (faltan,)
                ).fetchone()[0]
                ejemplo = ", ".join(str(x) for x in faltan[:5]) + ("…" if len(faltan) > 5 else "")
                v.error(
                    "fk_colgada",
                    f"{fk.tabla}.{col} → {fk.destino}.{col_dest}: {filas} fila(s) apuntan a {len(faltan)} id(s) que no existen "
                    f"en el dominio (p. ej. {ejemplo}).",
                )
    # Que el id sea la misma persona: mismo `username` en el core y en el dominio.
    for tabla, ids in referidos.items():
        if tabla != "usuarios":
            continue
        ids = sorted(ids)
        en_core = dict(core.execute("SELECT id, username FROM usuarios WHERE id = ANY(%s)", (ids,)).fetchall())
        en_dom = dict(dom.execute("SELECT id, username FROM usuarios WHERE id = ANY(%s)", (ids,)).fetchall())
        distintos = [i for i in ids if i in en_core and i in en_dom and en_core[i] != en_dom[i]]
        if distintos:
            ejemplo = ", ".join(f"{i}: {en_core[i]!r} en el core, {en_dom[i]!r} en el dominio" for i in distintos[:3])
            v.error("usuario_distinto", f"{len(distintos)} id(s) de usuarios son otra persona en el dominio ({ejemplo}).")
    if "usuarios" in dominio_manda:
        huerfanos = core.execute("SELECT id, username FROM usuarios ORDER BY id").fetchall()
        sin_destino = [
            (i, u)
            for i, u in huerfanos
            if i not in referidos.get("usuarios", set())
            and dom.execute("SELECT 1 FROM usuarios WHERE username = %s", (u,)).fetchone() is None
        ]
        if sin_destino:
            v.aviso(
                "usuarios_sin_destino",
                f"{len(sin_destino)} usuario(s) del core ({', '.join(u for _, u in sin_destino[:5])}) no existen en el dominio "
                "y nadie los referencia: no se copian.",
            )


# --- Informe -------------------------------------------------------------------------------------------------------------


def _ahora() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def escribir_json(ruta: Path, datos: dict) -> None:
    """Atómico: o queda el archivo viejo o queda el nuevo entero."""
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ruta.parent, prefix=ruta.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def leer_informe(ruta: Path) -> dict:
    try:
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ErrorDeUso(f"No existe el informe {ruta}.") from None
    except ValueError as e:
        raise ErrorDeUso(f"El informe {ruta} no es un JSON válido: {e}") from None
    if datos.get("tipo") != "union" or datos.get("version") != VERSION_DEL_INFORME:
        raise ErrorDeUso(f"{ruta} no es un informe de unión de esta versión (tipo={datos.get('tipo')!r}).")
    return datos


def _datos_de_verificacion(v: Verificacion) -> dict:
    return {
        "tipo": "verificacion",
        "version": VERSION_DEL_INFORME,
        "prefijo": v.prefijo,
        "verificado_en": _ahora(),
        "core": v.core,
        "dominio": v.dominio,
        "ok": v.ok,
        "errores": [{"codigo": h.codigo, "mensaje": h.mensaje} for h in v.errores],
        "avisos": [{"codigo": h.codigo, "mensaje": h.mensaje} for h in v.avisos],
        "cadenas": v.cadenas,
        "tablas_a_copiar": v.a_copiar,
        "tablas_a_vaciar": v.a_vaciar,
        "no_copiadas": v.no_copiadas,
        "tablas": v.tablas,
        "dominio_previo": v.dominio_previo,
        "auditoria": v.auditoria,
    }


def imprimir(v: Verificacion, salida=None) -> None:
    out = salida or sys.stdout
    p = lambda *a: print(*a, file=out)
    p(f"libracore-unir-bases — prefijo {v.prefijo}")
    p(f"  core:    {v.core}")
    p(f"  dominio: {v.dominio}")
    if v.misma_base:
        p("  Son la misma base: nada que unir.")
        return
    if v.ya_unida:
        p(f"  Ya unida el {v.marca['unida_en']}.")
        return
    p("")
    p("Cadenas de migración")
    for nombre, c in v.cadenas.items():
        p(f"  {nombre:<14} core={c['core'] or '-'}  dominio={c['dominio'] or '-'}  cabeza={c['cabeza'] or '?'}")
    p("")
    p(f"Tablas a copiar ({len(v.a_copiar)}), en este orden:")
    for t in v.a_copiar:
        r = v.tablas.get(t)
        if r is None:
            continue
        sumas = "  ".join(f"{c}={s}" for c, s in r["sumas"].items())
        marca = "  (vaciar semillas)" if t in v.a_vaciar else ""
        p(f"  {t:<28} {r['filas']:>8} fila(s)  {sumas}{marca}")
    for t, a in v.auditoria.items():
        conv = "; ".join(f"{c}: {d}" for c, d in a["conversiones"].items()) or "sin conversiones"
        p(f"Se agregan al dominio (auditoría, ids nuevos): {t}: {a['filas_core']} fila(s) del core a las {a['filas_dominio']} "
          f"del dominio ({conv})")
    if v.no_copiadas:
        p("No se copian (manda el dominio): " + ", ".join(f"{t} ({d['filas_core']} fila(s) en el core)" for t, d in v.no_copiadas.items()))
    p("")
    for h in v.hallazgos:
        p(f"  [{'ERROR' if h.nivel == 'error' else 'AVISO'}] {h.codigo}: {h.mensaje}")
    if v.ok:
        p("[OK] Se puede unir." if not v.avisos else f"[OK] Se puede unir ({len(v.avisos)} aviso(s)).")
    else:
        p(f"[ERROR] No se puede unir: {len(v.errores)} impedimento(s).")


# --- Unir ----------------------------------------------------------------------------------------------------------------


def _crear_marca(url: str, prefijo: str, previas: list[str]) -> None:
    """La marca `iniciada`, con la foto de las tablas del dominio de antes de tocarlo. Una sola fila."""
    with contextlib.closing(conectar(url)) as dom:
        dom.execute(
            f"CREATE TABLE IF NOT EXISTS {_regclass(TABLA_MARCA)} ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), prefijo TEXT NOT NULL, estado TEXT NOT NULL, "
            "tablas_previas TEXT NOT NULL, iniciada_en TEXT NOT NULL, unida_en TEXT, informe TEXT)"
        )
        dom.execute(
            f"INSERT INTO {_regclass(TABLA_MARCA)} (id, prefijo, estado, tablas_previas, iniciada_en) "
            "VALUES (1, %s, 'iniciada', %s, %s) ON CONFLICT (id) DO NOTHING",
            (prefijo, json.dumps(previas), _ahora()),
        )
        dom.commit()


def _preparar_destino(dominio_url: str, cadenas: dict[str, Cadena | None], salida) -> None:
    """Crea las tablas del motor (y de libracommerce) en el dominio con sus cadenas. Idempotente."""
    for nombre in ("libracore", "libracommerce"):
        cadena = cadenas.get(nombre)
        if cadena is None:
            continue
        print(f"  {cadena.nombre}: upgrade en el dominio…", file=salida)
        cadena.subir(dominio_url)
    with contextlib.closing(conectar(dominio_url, solo_lectura=True)) as dom:
        for nombre, cadena in cadenas.items():
            if cadena is None:
                continue
            en_dom = version_de(dom, cadena.tabla) if cadena.tabla in tablas_de(dom) else None
            if en_dom != cadena.cabeza:
                raise ErrorDeUnion(f"Tras el upgrade, {cadena.tabla} del dominio dice {en_dom!r} y debería ser {cadena.cabeza}.")


def _esquemas_iguales(core, dom, tablas: list[str]) -> None:
    for t in tablas:
        a = [(c.nombre, c.tipo) for c in columnas_de(core, t) if not c.generada]
        b = [(c.nombre, c.tipo) for c in columnas_de(dom, t) if not c.generada]
        if sorted(a) != sorted(b):
            raise ErrorDeUnion(
                f"La tabla {t} no tiene las mismas columnas en el core y en el dominio: "
                f"solo en el core {sorted(set(a) - set(b))}, solo en el dominio {sorted(set(b) - set(a))}."
            )


def _copiar_tabla(origen, destino, tabla: str, nombres: list[str], donde=None, ordenar_por: str | None = None) -> int:
    """`COPY` de una tabla de una base a la otra, con los ids como vienen. Devuelve las filas copiadas.

    `donde` (opcional, con la tabla como `t`) deja sólo algunas filas: lo usa `revertir`. Una autorreferencia
    (`cc_asientos.contrapartida_de`) no pide ningún orden: la clave foránea se comprueba al terminar la sentencia, y un `COPY` es
    una sola (un test guarda la contrapartida *antes* que el asiento que corrige).
    """
    cols = sql.SQL(", ").join(sql.SQL("t.{}").format(_ident(n)) for n in nombres)
    consulta = sql.SQL("SELECT {} FROM {} t").format(cols, _ident(tabla))
    if donde is not None:
        consulta = consulta + sql.SQL(" WHERE ") + donde
    if ordenar_por:
        consulta = consulta + sql.SQL(" ORDER BY t.{}").format(_ident(ordenar_por))
    cuerpo = sql.SQL("COPY ({}) TO STDOUT").format(consulta)
    entrada = sql.SQL("COPY {} ({}) FROM STDIN").format(_ident(tabla), _lista(nombres))
    with origen.cursor() as co, destino.cursor() as cd:
        with co.copy(cuerpo) as salida, cd.copy(entrada) as llegada:
            for bloque in salida:
                llegada.write(bloque)
        return cd.rowcount


def _ajustar_secuencias(conn, tabla: str, columnas: list[Columna]) -> dict:
    """`setval` de cada secuencia de la tabla a `max(columna)`. Devuelve el próximo valor de cada una."""
    salida = {}
    for col, seq in secuencias_de(conn, tabla, columnas):
        conn.execute(
            sql.SQL("SELECT setval(%s::regclass, COALESCE(MAX({c}), 1), MAX({c}) IS NOT NULL) FROM {t}").format(
                c=_ident(col), t=_ident(tabla)
            ),
            (seq,),
        )
        maximo = conn.execute(sql.SQL("SELECT MAX({}) FROM {}").format(_ident(col), _ident(tabla))).fetchone()[0]
        salida[col] = 1 if maximo is None else maximo + 1
    return salida


def _huerfanas(conn, fk: ClaveForanea) -> int:
    no_nulas = sql.SQL(" AND ").join(sql.SQL("c.{} IS NOT NULL").format(_ident(c)) for c in fk.columnas)
    donde = sql.SQL(" AND ").join(
        sql.SQL("d.{} = c.{}").format(_ident(b), _ident(a)) for a, b in zip(fk.columnas, fk.columnas_destino, strict=True)
    )
    return conn.execute(
        sql.SQL("SELECT count(*) FROM {} c WHERE {} AND NOT EXISTS (SELECT 1 FROM {} d WHERE {})").format(
            _ident(fk.tabla), no_nulas, _ident(fk.destino), donde
        )
    ).fetchone()[0]


def _validar_claves_foraneas(conn, tablas: list[str]) -> None:
    for fk in claves_foraneas_de(conn):
        if fk.tabla in tablas or fk.destino in tablas:
            n = _huerfanas(conn, fk)
            if n:
                raise ErrorDeUnion(f"Tras copiar, {fk.tabla}.{','.join(fk.columnas)} → {fk.destino} tiene {n} fila(s) sin padre.")


def _cadenas_a_correr(v: Verificacion) -> dict[str, Cadena | None]:
    cadenas: dict[str, Cadena | None] = {"libracore": cadena_libracore(), "libracommerce": None}
    if v.cadenas.get("libracommerce", {}).get("core") is not None:
        cadenas["libracommerce"] = cadena_libracommerce()
    return cadenas


def _error_de_base(e: Exception, *urls: str) -> str:
    return _sin_secretos(" ".join(str(e).split()), *urls)


def unir(core_url: str, dominio_url: str, prefijo: str, informe: Path, *, aunque_haya_conexiones: bool = False, salida=None) -> int:
    """Une el core en el dominio. Devuelve el código de salida (0 bien, 1 impedimento o unión deshecha)."""
    out = salida or sys.stdout
    informe = Path(informe)
    v = verificar(core_url, dominio_url, prefijo, conexiones_impiden=not aunque_haya_conexiones)
    imprimir(v, out)
    if v.misma_base:
        return 0
    if v.ya_unida:
        # Idempotente: no se toca nada. Sólo, si falta el archivo del informe, se vuelve a escribir el que guardó la marca.
        if not informe.exists() and v.marca.get("informe"):
            escribir_json(informe, v.marca["informe"])
            print(f"  (se reescribió {informe} desde la marca del dominio)", file=out)
        print("[OK] Ya estaba unida: no se cambió nada.", file=out)
        return 0
    if not v.ok:
        return 1

    print("\nUnión", file=out)
    if v.marca is None:
        _crear_marca(dominio_url, prefijo, v.previas)
    cadenas = _cadenas_a_correr(v)
    try:
        _preparar_destino(dominio_url, cadenas, out)
    except Exception as e:  # noqa: BLE001 - alembic puede levantar casi cualquier cosa
        print(
            f"[ERROR] No se pudieron crear las tablas del motor en el dominio: {type(e).__name__}: {_error_de_base(e, core_url, dominio_url)}\n"
            "        No se copió nada. Las tablas del motor que hayan quedado creadas están vacías; se puede reintentar.",
            file=sys.stderr,
        )
        return 1
    try:
        resultado = _copiar_todo(core_url, dominio_url, v, prefijo, out)
    except ErrorDeUnion as e:
        print(f"[ERROR] La unión se deshizo, las tablas del dominio quedaron como estaban: {e}", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(
            f"[ERROR] La unión se deshizo, las tablas del dominio quedaron como estaban: {_error_de_base(e, core_url, dominio_url)}",
            file=sys.stderr,
        )
        return 1
    total = sum(t["filas"] for t in resultado["tablas"].values())
    try:
        escribir_json(informe, resultado)
    except OSError as e:
        print(
            f"[ERROR] La unión se hizo y está confirmada ({total} fila(s)), pero no se pudo escribir {informe}: {e}\n"
            "        Correr `unir` de nuevo no cambia nada y reescribe el informe desde la marca del dominio.",
            file=sys.stderr,
        )
        return 1
    print(f"[OK] Unida: {len(resultado['tablas'])} tabla(s), {total} fila(s). Informe: {informe}", file=out)
    return 0


def _copiar_todo(core_url, dominio_url, v: Verificacion, prefijo: str, out) -> dict:
    """La transacción: vaciar semillas, copiar, secuencias, claves foráneas, comparar y marcar `unida`."""
    with contextlib.closing(conectar(core_url, solo_lectura=True, repeatable=True)) as core, contextlib.closing(
        conectar(dominio_url)
    ) as dom:
        en_dominio = set(tablas_de(dom))
        faltan = [t for t in v.a_copiar if t not in en_dominio]
        if faltan:
            raise ErrorDeUnion("El upgrade no creó en el dominio: " + ", ".join(faltan))
        con_lock = v.a_copiar + list(v.auditoria)
        if con_lock:
            # Nadie escribe en estas tablas mientras dura la unión (la app está detenida). Si alguien las tiene tomadas,
            # se espera un rato y se corta: una unión colgada del candado de otro es peor que una que dice por qué no pudo.
            dom.execute("SET LOCAL lock_timeout = '30s'")
            dom.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(_lista(con_lock)))
        _esquemas_iguales(core, dom, v.a_copiar)
        numeradas = {}
        for t, col in TABLAS_DE_CADENA_NUMERADA.items():
            if t in set(tablas_de(core)):
                en_core = sorted(r[0] for r in core.execute(sql.SQL("SELECT {} FROM {}").format(_ident(col), _ident(t))).fetchall())
                en_dom = sorted(r[0] for r in dom.execute(sql.SQL("SELECT {} FROM {}").format(_ident(col), _ident(t))).fetchall())
                if not set(en_core) <= set(en_dom):
                    raise ErrorDeUnion(f"Tras el upgrade, {t} del dominio no tiene las versiones {sorted(set(en_core) - set(en_dom))} del core.")
                numeradas[t] = {"core": en_core, "dominio": en_dom}
        fks_dom = claves_foraneas_de(dom)
        # El destino se mira ahora, con las tablas ya creadas: en la verificación todavía no existían.
        vaciar = []
        for t in v.a_copiar:
            estado = _estado_del_destino(dom, t, fks_dom)
            if estado == "con_datos":
                raise ErrorDeUnion(f"La tabla {t} del dominio tiene filas que no son semillas del motor: no se pisa.")
            if estado == "semillas":
                vaciar.append(t)

        print(f"  copiando {len(v.a_copiar)} tabla(s)…", file=out)
        for t in reversed(v.a_copiar):
            if t in vaciar:
                dom.execute(sql.SQL("DELETE FROM {}").format(_ident(t)))
        tablas = {}
        for t in v.a_copiar:
            cols_core = columnas_de(core, t)
            nombres = [c.nombre for c in cols_core if not c.generada]
            copiadas = _copiar_tabla(core, dom, t, nombres)
            cols_dom = columnas_de(dom, t)
            secuencias = _ajustar_secuencias(dom, t, cols_dom)
            en_core, en_dom = resumen_de(core, t, cols_core), resumen_de(dom, t, cols_dom)
            if copiadas != en_core["filas"] or en_core != en_dom:
                raise ErrorDeUnion(f"{t}: lo copiado no coincide con el core (core {en_core}, dominio {en_dom}, copiadas {copiadas}).")
            tablas[t] = {**en_core, "secuencias": secuencias}
        auditoria = _agregar_auditoria(core, dom, list(v.auditoria), out)
        _validar_claves_foraneas(dom, v.a_copiar)

        en_dominio = set(tablas_de(dom))
        cadenas = {
            nombre: {
                "core": c["core"],
                "dominio": version_de(dom, c["tabla"]) if c["tabla"] in en_dominio else None,
                "cabeza": c["cabeza"],
            }
            for nombre, c in v.cadenas.items()
        }
        informe = {
            "tipo": "union",
            "version": VERSION_DEL_INFORME,
            "herramienta": "libracore-unir-bases",
            "prefijo": prefijo,
            "unido_en": _ahora(),
            "core": v.core,
            "dominio": v.dominio,
            "cadenas": cadenas,
            "cadenas_numeradas": numeradas,
            "tablas": tablas,
            "auditoria_agregada": auditoria,
            "tablas_vaciadas": sorted(vaciar),
            "no_copiadas": v.no_copiadas,
            "dominio_previo": v.dominio_previo,
        }
        dom.execute(
            f"UPDATE {_regclass(TABLA_MARCA)} SET estado = 'unida', unida_en = %s, informe = %s WHERE id = 1",
            (informe["unido_en"], json.dumps(informe, ensure_ascii=False)),
        )
        dom.commit()
        return informe


def _agregar_auditoria(core, dom, tablas: list[str], out) -> dict:
    """Agrega al dominio las filas de las tablas de auditoría del core, con ids nuevos y los tipos del dominio.

    El `COPY` de texto convierte solo (`'2026-10-01 10:00:00'` a `timestamp`, `12` a `varchar`): es el mismo parseo que
    `USING ts::timestamp` de la `0012_actividad_log` de LibraClub. La comparación es por conteo: lo que tenía el dominio más
    lo que tenía el core, y las filas nuevas son exactamente las del core (ids por encima del máximo de antes).
    """
    informe = {}
    for t in tablas:
        reglas_core, reglas_dom = _reglas_de_columnas(core, t), _reglas_de_columnas(dom, t)
        nombres = [c for c in reglas_core if c != "id"]
        tabla = _ident(t)
        del_core = core.execute(sql.SQL("SELECT count(*) FROM {}").format(tabla)).fetchone()[0]
        antes = dom.execute(sql.SQL("SELECT count(*) FROM {}").format(tabla)).fetchone()[0]
        con_id = "id" in reglas_dom
        maximo = dom.execute(sql.SQL("SELECT COALESCE(MAX(id), 0) FROM {}").format(tabla)).fetchone()[0] if con_id else None
        print(f"  agregando {del_core} fila(s) de {t}…", file=out)
        copiadas = _copiar_tabla(core, dom, t, nombres, ordenar_por="id" if "id" in reglas_core else None)
        despues = dom.execute(sql.SQL("SELECT count(*) FROM {}").format(tabla)).fetchone()[0]
        nuevas = (
            dom.execute(sql.SQL("SELECT count(*) FROM {} WHERE id > %s").format(tabla), (maximo,)).fetchone()[0] if con_id else del_core
        )
        if copiadas != del_core or despues != antes + del_core or nuevas != del_core:
            raise ErrorDeUnion(
                f"{t}: lo agregado no coincide (core {del_core}, copiadas {copiadas}, dominio antes {antes} y después {despues})."
            )
        informe[t] = {
            "filas_core": del_core,
            "filas_dominio_antes": antes,
            "filas_dominio_despues": despues,
            "max_id_antes": maximo,
            "conversiones": {c: f"{reglas_core[c][0]} → {reglas_dom[c][0]}" for c in nombres if reglas_core[c][0] != reglas_dom[c][0]},
        }
    return informe


# --- Revertir ------------------------------------------------------------------------------------------------------------


def _tiene_id(columnas: list[Columna]) -> bool:
    return any(c.nombre == "id" and c.tipo in ("bigint", "integer", "smallint") for c in columnas)


def revertir(core_url: str, dominio_url: str, prefijo: str, informe: Path, *, aunque_haya_conexiones: bool = False, salida=None) -> int:
    """Devuelve al core las filas que el dominio recibió después de la unión. Ver «Límites» en el README."""
    out = salida or sys.stdout
    datos = leer_informe(informe)
    if datos["prefijo"] != prefijo:
        raise ErrorDeUso(f"El informe es del prefijo {datos['prefijo']!r}, no de {prefijo!r}.")
    if _misma_base(core_url, dominio_url):
        raise ErrorDeUso("El core y el dominio son la misma base: no hay nada que devolver.")
    print(f"libracore-unir-bases revertir — prefijo {prefijo}\n  core:    {ocultar(core_url)}\n  dominio: {ocultar(dominio_url)}", file=out)
    try:
        with contextlib.closing(conectar(dominio_url, solo_lectura=True, repeatable=True)) as dom, contextlib.closing(
            conectar(core_url)
        ) as core:
            marca = leer_marca(dom)
            if not marca or marca["estado"] != "unida":
                raise ErrorDeUnion("El dominio no tiene la marca de unión: no es una base unida con esta herramienta.")
            if not aunque_haya_conexiones:
                ajenas = {"core": conexiones_ajenas(core), "dominio": conexiones_ajenas(dom)}
                if any(ajenas.values()):
                    raise ErrorDeUnion(f"Hay conexiones abiertas ({ajenas}): detené la app o pasá --aunque-haya-conexiones.")
            tablas = list(datos["tablas"])
            t_core = set(tablas_de(core))
            faltan = [t for t in tablas if t not in t_core]
            if faltan:
                raise ErrorDeUnion("Al core le faltan tablas del informe: " + ", ".join(faltan))
            # El core viejo tiene que estar como estaba: lo que tenía hasta el último id de la unión, idéntico.
            for t in tablas:
                cols = columnas_de(core, t)
                esperado = datos["tablas"][t]
                if _tiene_id(cols):
                    tope = esperado["max_id"] if esperado["max_id"] is not None else 0
                    actual = resumen_de(core, t, cols, hasta_id=tope)
                    if actual != {k: esperado[k] for k in ("filas", "sumas", "huella", "max_id")}:
                        raise ErrorDeUnion(f"El core cambió desde la unión ({t}): hay que revisar a mano, no se devuelve nada.")
            fks_dom = claves_foraneas_de(dom)
            orden = orden_topologico(tablas, fks_dom)
            devueltas = {}
            for t in orden:
                cols_dom = columnas_de(dom, t)
                nombres = [c.nombre for c in cols_dom if not c.generada]
                esperado = datos["tablas"][t]
                if _tiene_id(cols_dom):
                    tope = esperado["max_id"] if esperado["max_id"] is not None else 0
                    ya = {r[0] for r in core.execute(sql.SQL("SELECT id FROM {} WHERE id > %s").format(_ident(t)), (tope,)).fetchall()}
                    nuevos = [
                        r[0]
                        for r in dom.execute(sql.SQL("SELECT id FROM {} WHERE id > %s ORDER BY id").format(_ident(t)), (tope,)).fetchall()
                        if r[0] not in ya
                    ]
                    donde = sql.SQL("t.id = ANY({})").format(sql.Literal(nuevos)) if nuevos else None
                    clave = ["id"]
                else:
                    clave = clave_primaria_de(dom, t)
                    if not clave:
                        devueltas[t] = {"filas": 0, "aviso": "sin clave primaria: no se puede saber qué es nuevo"}
                        continue
                    kc = sql.SQL(", ").join(_ident(c) for c in clave)
                    ya = {tuple(r) for r in core.execute(sql.SQL("SELECT {} FROM {}").format(kc, _ident(t))).fetchall()}
                    nuevos = [tuple(r) for r in dom.execute(sql.SQL("SELECT {} FROM {}").format(kc, _ident(t))).fetchall() if tuple(r) not in ya]
                    donde = (
                        sql.SQL("({}) IN ({})").format(
                            sql.SQL(", ").join(sql.SQL("t.{}").format(_ident(c)) for c in clave),
                            sql.SQL(", ").join(
                                sql.SQL("({})").format(sql.SQL(", ").join(sql.Literal(x) for x in fila)) for fila in nuevos
                            ),
                        )
                        if nuevos
                        else None
                    )
                if donde is None:
                    devueltas[t] = {"filas": 0}
                    continue
                copiadas = _copiar_tabla(dom, core, t, nombres, donde=donde)
                _ajustar_secuencias(core, t, columnas_de(core, t))
                devueltas[t] = {"filas": copiadas}
            _validar_claves_foraneas(core, tablas)
            core.commit()
    except ErrorDeUnion as e:
        print(f"[ERROR] No se devolvió nada: {e}", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(
            "[ERROR] No se devolvió nada (la transacción en el core se deshizo): " + _error_de_base(e, core_url, dominio_url),
            file=sys.stderr,
        )
        return 1
    total = sum(d["filas"] for d in devueltas.values())
    for t, a in datos.get("auditoria_agregada", {}).items():
        print(f"  {t:<28} no se devuelve: la auditoría agregada ({a['filas_core']} fila(s) del core + las nuevas) queda en el dominio", file=out)
    for t, d in devueltas.items():
        if d["filas"] or d.get("aviso"):
            print(f"  {t:<28} {d['filas']:>8} fila(s) devueltas {d.get('aviso', '')}".rstrip(), file=out)
    datos.setdefault("reversiones", []).append({"revertido_en": _ahora(), "filas": total, "tablas": devueltas})
    escribir_json(Path(informe), datos)
    print("[OK] Nada nuevo que devolver: el core ya tiene todo." if not total else f"[OK] Devueltas {total} fila(s) al core.", file=out)
    return 0


# --- Línea de comandos ---------------------------------------------------------------------------------------------------


def _opciones_comunes() -> argparse.ArgumentParser:
    """Las opciones que valen antes y después del subcomando. Un parser nuevo por uso: si dos parsers comparten las mismas
    acciones, el `set_defaults` de uno pisa el `SUPPRESS` del otro y el valor que se dio antes del subcomando se pierde."""
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--prefijo", default=argparse.SUPPRESS, help="producto (libraclub, gestiolibra…): resuelve las URL de la instancia")
    c.add_argument("--core", default=argparse.SUPPRESS, help="URL del core, o env:NOMBRE (default: <PREFIJO>_LIBRACORE_DATABASE_URL)")
    c.add_argument("--dominio", default=argparse.SUPPRESS, help="URL del dominio, o env:NOMBRE (default: <PREFIJO>_DATABASE_URL)")
    c.add_argument("--informe", default=argparse.SUPPRESS, help="union.json: lo que escribe `unir` y lee `revertir`")
    return c


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="libracore-unir-bases",
        description="Une la base core de un producto dentro de la base de su dominio (ADR-078).",
        parents=[_opciones_comunes()],
    )
    p.set_defaults(prefijo=None, core=None, dominio=None, informe=None)
    sub = p.add_subparsers(dest="accion", required=True)
    sub.add_parser("verificar", parents=[_opciones_comunes()], help="sólo lectura: ¿se puede unir?")
    for nombre, ayuda in (("unir", "con la app detenida: crea las tablas y copia las filas"), ("revertir", "devuelve al core las filas nuevas")):
        s = sub.add_parser(nombre, parents=[_opciones_comunes()], help=ayuda)
        s.add_argument("--aunque-haya-conexiones", action="store_true", help="no cortar por conexiones abiertas a las bases")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.prefijo:
            raise ErrorDeUso("Falta --prefijo (el producto: libraclub, gestiolibra…).")
        prefijo = args.prefijo.strip().lower()
        core_url = resolver_url(args.core, prefijo, core=True)
        dominio_url = resolver_url(args.dominio, prefijo, core=False)
        extra = getattr(args, "aunque_haya_conexiones", False)
        if args.accion == "verificar":
            v = verificar(core_url, dominio_url, prefijo)
            imprimir(v)
            if args.informe and not v.misma_base and not v.ya_unida:
                ruta = Path(args.informe)
                if ruta.exists() and json.loads(ruta.read_text(encoding="utf-8") or "{}").get("tipo") == "union":
                    raise ErrorDeUso(f"{ruta} es un informe de unión: no se pisa con una verificación. Usá otro archivo.")
                escribir_json(ruta, _datos_de_verificacion(v))
            return 0 if v.ok else 1
        if not args.informe:
            raise ErrorDeUso("Falta --informe <union.json>.")
        funcion = unir if args.accion == "unir" else revertir
        return funcion(core_url, dominio_url, prefijo, Path(args.informe), aunque_haya_conexiones=extra)
    except (ErrorDeUso, ErrorDeConexion) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
