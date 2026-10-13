"""
Cuentas de correo saliente en el servidor de mail: una por instancia.

**El problema que resuelve.** Hasta ahora una instancia nacía sin correo:
`smtp_settings` vacía y `LIBRAAUTH_SMTP_*` sin definir, así que
`/auth/forgot-password` contestaba `503` hasta que alguien entrara al
backoffice a cargarle un servidor a mano. Ninguna instancia del parque lo
tenía cargado. Acá el alta le crea la casilla y le deja la credencial puesta,
y la instancia manda correo desde su primer arranque.

**Por qué por entorno y no por la API de la instancia.** `resolver_smtp_config`
de libraauth resuelve `base > entorno`: si nadie guardó nada por pantalla, vale
lo que diga `LIBRAAUTH_SMTP_*`. Escribir esas variables en el compose que
genera el alta hace que la instancia nazca configurada **sin una sola llamada
HTTP**, y por lo tanto sin esperar a que el contenedor esté sano ni manejar el
502 de una instancia que todavía no levantó — el alta ya tarda 24 s con la
emisión del certificado. Y no le saca nada al backoffice: su pantalla de SMTP
escribe la fila en la base, que sigue ganando sobre esto.

**Una credencial por instancia, nunca una compartida.** Si una instancia se
compromete, su credencial sólo puede mandar como ella misma. El servidor de
correo ata el remitente al usuario autenticado (`smtpd_sender_login_maps`), así
que `from_email` es la dirección de la cuenta y no un valor libre.

**Opt-in por ausencia, y apaga exactamente una cosa.** Sin
`LIBRA_MAIL_ADMIN_SSH` y `LIBRA_MAIL_DOMINIO` (en el entorno del proceso o en el
archivo del host, ver abajo), `configurado()` es `False`, no se crea ninguna
casilla y el compose sale sin las variables. No toca ningún otro camino del
alta: ni el proxy, ni el plan, ni las migraciones. Pero **ya no es silencioso**
(ADR-046): quien da de alta recibe el motivo en `estado()` y lo muestra.

**De dónde lee la configuración (ADR-046).** El backoffice las recibe por el
`environment:` de su compose; el host del VPS no tiene ese entorno, y un alta
por la CLI (`scripts/nuevo_cliente.py`) nacía sin casilla sin avisar. Por eso
cada variable se busca primero en el entorno del proceso (gana siempre) y, si
falta, en un archivo del host `KEY=valor` por producto:
`/root/secretos/correo-<container_prefix>.env`, o el que diga
`LIBRA_MAIL_ENV_FILE`. El archivo no tiene secretos —un alias de SSH y dos
nombres de dominio; la clave privada vive en `~/.ssh`— y de él sólo se leen las
tres variables de abajo. Ejemplo (`/root/secretos/correo-libradesk.env`):

    LIBRA_MAIL_ADMIN_SSH=vps-mail
    LIBRA_MAIL_DOMINIO=libradesk.com.ar
    LIBRA_MAIL_SMTP_HOST=mail.libradesk.com.ar

`LIBRA_MAIL_SMTP_PORT` sigue siendo sólo de entorno.
"""
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Segundos. El alta entera tiene presupuesto de minuto y medio y esto es una
# sola orden remota: si el servidor de correo no contesta en 20 s, no contesta.
_TIMEOUT = 20

# La dirección que se le manda al wrapper del servidor de correo. El slug ya
# viene de `slugify()` (sólo `[a-z0-9-]`), pero esto se valida igual **antes**
# de cruzar el SSH: es el único dato de esta orden que se deriva de algo que
# escribió un humano en un formulario, y del otro lado hay un shell.
_DIRECCION_VALIDA = re.compile(r"^[a-z0-9][a-z0-9._-]*@[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")


class MailError(Exception):
    """No se pudo operar sobre el servidor de correo.

    **Nunca aborta un alta.** `crear_cliente` la atrapa y sigue con
    `smtp_ok=False`, igual que hace con NPM: una instancia sin correo es una
    instancia a la que le falta una pantalla de configuración, no una instancia
    rota. Que el alta entera fallara por esto sería peor que el problema.
    """


class CasillaYaExiste(MailError):
    """El servidor de correo ya tiene esa casilla.

    Es un caso aparte porque pide otra respuesta que el resto de los fallos: la
    contraseña de una casilla no se puede leer de vuelta, así que si el compose
    de la instancia no la tiene, la única salida es borrarla y crearla de nuevo
    (`panel_admin.py casilla <slug> --recrear`), y eso **borra su buzón**: no se
    decide solo.
    """


@dataclass(frozen=True)
class CuentaDeCorreo:
    """Lo que hay que saber para que una instancia mande correo.

    `password` es la única copia en claro que existe fuera del servidor de
    correo, y su destino es el `docker-compose.yml` de la instancia. **No va a
    `cliente.json` ni sale por `log()`** — mismo tratamiento que
    `LIBRA_PANEL_TOKEN`, y por el mismo motivo: esa metadata la lee
    `load_clients()` y viaja a quien pregunte por la instancia.
    """

    direccion: str
    password: str
    host: str
    port: int
    from_name: str


#: Las únicas variables que se leen del archivo del host. Cualquier otra clave
#: que alguien deje ahí se ignora: el archivo es una declaración, no un entorno.
_CLAVES_DEL_ARCHIVO = ("LIBRA_MAIL_ADMIN_SSH", "LIBRA_MAIL_DOMINIO", "LIBRA_MAIL_SMTP_HOST")

#: Carpeta de los archivos de configuración del host (la misma de
#: `resguardo_oauth.env`, ver `nuevo_cliente.RESGUARDO_OAUTH_ENV`).
_CARPETA_DEL_HOST = Path("/root/secretos")


def archivo_del_host() -> Path | None:
    """El archivo de declaración de este producto, o `None` si no se puede saber.

    `LIBRA_MAIL_ENV_FILE` lo fija a mano (los tests lo usan para no leer el del
    host real). Si no, `correo-<container_prefix>.env` en `/root/secretos`: el
    dominio de correo es **del producto**, así que el archivo también. Sin
    producto configurado (`configure()` todavía no corrió) no hay a cuál mirar.
    """
    explicito = os.environ.get("LIBRA_MAIL_ENV_FILE", "").strip()
    if explicito:
        return Path(explicito)
    from . import get_config

    try:
        prefijo = get_config().container_prefix
    except RuntimeError:
        return None
    return _CARPETA_DEL_HOST / f"correo-{prefijo}.env"


def _leer_archivo() -> dict[str, str]:
    """Las variables declaradas en el archivo del host; `{}` si no existe o no se puede leer.

    Formato `CLAVE=valor`, una por línea, `#` comenta, comillas simples o dobles
    opcionales alrededor del valor. No se ejecuta ni se expande nada.
    """
    ruta = archivo_del_host()
    if ruta is None:
        return {}
    try:
        texto = ruta.read_text(encoding="utf-8")
    except OSError:
        return {}
    valores: dict[str, str] = {}
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        clave = clave.strip().removeprefix("export ").strip()
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        if clave in _CLAVES_DEL_ARCHIVO and valor:
            valores[clave] = valor
    return valores


def _entorno(nombre: str, default: str = "") -> str:
    """El entorno del proceso primero; si falta, el archivo del host (ADR-046)."""
    del_proceso = os.environ.get(nombre, "").strip()
    if del_proceso:
        return del_proceso
    if nombre in _CLAVES_DEL_ARCHIVO:
        del_archivo = _leer_archivo().get(nombre, "")
        if del_archivo:
            return del_archivo
    return default


def configurado() -> bool:
    """Si este proceso sabe hablarle a un servidor de correo.

    Hacen falta las dos: sin destino SSH no hay a quién pedirle la casilla, y
    sin dominio no hay dirección que crear. Tener una sola es una configuración
    a medias, y devolver `True` ahí haría fallar cada alta con un `MailError`
    en vez de comportarse como el parque de hoy.
    """
    return bool(_entorno("LIBRA_MAIL_ADMIN_SSH") and _entorno("LIBRA_MAIL_DOMINIO"))


def motivo_sin_configurar() -> str:
    """Por qué `configurado()` da `False`, en una frase para el operador; `""` si está configurado.

    Nombra qué falta y dónde se declara, sin imprimir ningún valor.
    """
    faltan = [v for v in ("LIBRA_MAIL_ADMIN_SSH", "LIBRA_MAIL_DOMINIO") if not _entorno(v)]
    if not faltan:
        return ""
    ruta = archivo_del_host()
    donde = f"ni en {ruta}" if ruta else "ni en el archivo del host"
    return (
        f"este proceso no tiene {' ni '.join(faltan)} (ni en el entorno {donde}), "
        "así que no sabe a qué servidor de correo pedirle la casilla"
    )


def direccion_de(slug: str) -> str:
    """`<slug>@<dominio del producto>`.

    El dominio es **del producto**, no del cliente: `lagrace@libradesk.com.ar`,
    no `noreply@lagrace.com.ar`. Que el remitente fuera del dominio del cliente
    daría mejor imagen, pero obliga a que el cliente publique SPF y DKIM en SU
    zona — o sea, un paso manual del lado de él, que es justo lo que este
    módulo existe para no tener.
    """
    return f"{slug}@{_entorno('LIBRA_MAIL_DOMINIO')}"


def _smtp_host() -> str:
    """El host al que se conecta la instancia.

    Puede no ser el mismo string que el destino SSH: al SSH se le puede pasar
    un alias de `~/.ssh/config` o una IP, y acá hace falta el **nombre** que
    matchea el certificado TLS del servidor de correo. Si no se declara, se cae
    al destino SSH sin la parte de usuario, que es lo correcto cuando el alias
    ya es el FQDN.
    """
    declarado = _entorno("LIBRA_MAIL_SMTP_HOST")
    if declarado:
        return declarado
    return _entorno("LIBRA_MAIL_ADMIN_SSH").rpartition("@")[2]


def _smtp_port() -> int:
    crudo = _entorno("LIBRA_MAIL_SMTP_PORT", "587")
    try:
        return int(crudo)
    except ValueError:
        raise MailError(
            f"LIBRA_MAIL_SMTP_PORT={crudo!r} no es un número de puerto."
        ) from None


def _ssh(orden: list[str], entrada: str = "") -> str:
    """Una orden contra el wrapper del servidor de correo.

    Del otro lado la clave está restringida con `command=` en su
    `authorized_keys`, así que este proceso **no tiene shell** en el servidor de
    correo: sólo puede pedirle las operaciones que el wrapper implementa.

    🔴 **La contraseña viaja por stdin, nunca como argumento.** Un argumento
    queda visible en el `ps` del servidor de correo mientras dura la orden, y
    además llega del otro lado dentro de `SSH_ORIGINAL_COMMAND`, que es una
    variable de entorno y termina en cualquier log que vuelque el entorno.
    """
    destino = _entorno("LIBRA_MAIL_ADMIN_SSH")
    if not destino:
        raise MailError("LIBRA_MAIL_ADMIN_SSH no está definido.")

    cmd = [
        "ssh",
        # Sin esto, un problema de clave abre un prompt de contraseña y el alta
        # se cuelga hasta el timeout en vez de fallar con un motivo.
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={_TIMEOUT}",
        destino,
        *orden,
    ]
    try:
        r = subprocess.run(
            cmd, input=entrada, capture_output=True, text=True, timeout=_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise MailError(
            f"El servidor de correo no contestó en {_TIMEOUT}s."
        ) from None
    except OSError as e:
        raise MailError(f"No se pudo ejecutar ssh: {e}") from None

    if r.returncode != 0:
        # `stderr` del wrapper, que es quien sabe por qué falló ("ya existe",
        # "dominio no configurado"). Se recorta porque esto termina en el log
        # del alta y de ahí en la respuesta HTTP del backoffice.
        motivo = (r.stderr or r.stdout or "").strip().splitlines()
        detalle = motivo[-1][:200] if motivo else f"código {r.returncode}"
        raise MailError(f"El servidor de correo rechazó la orden: {detalle}")
    return r.stdout


def crear_cuenta(slug: str, nombre: str, password: str) -> CuentaDeCorreo:
    """Crea la casilla de una instancia y devuelve con qué conectarse.

    `password` la genera el llamador —junto al resto de los secretos del
    alta— y no este módulo, para que quede en un solo lugar de dónde salen las
    credenciales de una instancia nueva.
    """
    direccion = direccion_de(slug)
    if not _DIRECCION_VALIDA.match(direccion):
        raise MailError(
            f"La dirección {direccion!r} no tiene forma de dirección de correo. "
            "Revisá LIBRA_MAIL_DOMINIO y el slug de la instancia."
        )
    if not password:
        raise MailError("No se puede crear una casilla sin contraseña.")

    try:
        _ssh(["crear", direccion], entrada=password)
    except MailError as e:
        # El wrapper contesta «La casilla ya existe: <dirección>».
        if "ya existe" in str(e).lower():
            raise CasillaYaExiste(str(e)) from None
        raise
    return CuentaDeCorreo(
        direccion=direccion,
        password=password,
        host=_smtp_host(),
        port=_smtp_port(),
        from_name=_from_name(nombre),
    )


def borrar_cuenta(direccion: str) -> None:
    """Da de baja una casilla. La usa la baja de instancia y el rollback del alta.

    Sin esto el servidor de correo acumula casillas huérfanas **con credenciales
    vivas**: una instancia dada de baja seguiría pudiendo mandar correo si
    alguien conservó su compose.
    """
    if not _DIRECCION_VALIDA.match(direccion):
        raise MailError(f"La dirección {direccion!r} no tiene forma de dirección.")
    _ssh(["borrar", direccion])


def agregar_reenvio(slug: str, destino: str) -> None:
    """Reenvía la casilla de una instancia a un correo del cliente, sin
    dejar de recibirla localmente.

    **El caso de uso.** El cliente carga desde su panel un correo personal
    para no perderse lo que le llega a `<slug>@dominio` —el `forgot-password`,
    un aviso del sistema— sin tener que acordarse de revisar una casilla que
    no es la que usa todos los días. El servidor mantiene la copia local
    además de reenviar: si el reenvío externo rebota o el correo del cliente
    está lleno, el mensaje no se pierde, sigue estando en la casilla de la
    instancia.

    Del lado del wrapper esto se resuelve con un script Sieve por usuario y
    no con un alias de Postfix —`setup alias add` rechaza una dirección que
    ya es una casilla real—, pero acá no cambia nada: se manda `alias` igual
    que `crear_cuenta` manda `crear`, y quien traduce la orden es el wrapper.

    Idempotente sin necesitar un `del` previo: el script Sieve es un archivo
    entero por casilla, no una lista a la que se le agrega — reenviar de
    nuevo, o cambiar el destino, es reescribir el mismo archivo.
    """
    direccion = direccion_de(slug)
    if not _DIRECCION_VALIDA.match(direccion):
        raise MailError(
            f"La dirección {direccion!r} no tiene forma de dirección de correo. "
            "Revisá LIBRA_MAIL_DOMINIO y el slug de la instancia."
        )
    if not destino or not _DIRECCION_VALIDA.match(destino):
        raise MailError(
            f"El destino {destino!r} no tiene forma de dirección de correo: "
            "no hay a quién reenviarle."
        )
    _ssh(["alias", direccion, destino])


def quitar_reenvio(slug: str) -> None:
    """Saca el reenvío de una instancia. La casilla sigue recibiendo local.

    Sin `destino`, a propósito: el caso real es el cliente **limpiando el
    campo** en su panel y guardando — para cuando este módulo se entera, el
    valor viejo ya no existe en ningún lado (la instancia lo pisó por
    `null`), así que pedirlo sería un requisito que nadie puede cumplir. El
    script Sieve es un archivo entero por casilla, no una lista acumulativa
    —lo mismo que hace `agregar_reenvio` idempotente sin un `del` previo—,
    así que sacarlo no necesita saber qué decía: se borra el archivo entero.
    """
    direccion = direccion_de(slug)
    if not _DIRECCION_VALIDA.match(direccion):
        raise MailError(
            f"La dirección {direccion!r} no tiene forma de dirección de correo. "
            "Revisá LIBRA_MAIL_DOMINIO y el slug de la instancia."
        )
    _ssh(["desalias", direccion])


def _from_name(nombre: str) -> str:
    """El nombre que ve quien recibe el correo: el del cliente, no el del producto.

    🔴 Se le sacan los saltos de línea. Este valor sale de un formulario y
    termina interpolado en el `docker-compose.yml`, que se arma como texto: un
    `\\n` en el nombre del cliente escribiría una línea propia adentro del
    bloque `environment`. El resto de los caracteres raros no rompen nada
    —quedan como el valor de la variable— así que no se tocan.
    """
    return " ".join((nombre or "").split())


def env_para_compose(cuenta: CuentaDeCorreo, sangria: str = "      ") -> str:
    """Las líneas de `environment:` del compose de la instancia.

    Seis variables y no menos: `LIBRAAUTH_SMTP_FROM_EMAIL` cae a
    `LIBRAAUTH_SMTP_USER` si falta, pero escribirla explícita deja el compose
    legible para quien lo abra a mano en el host, que es el único lugar donde
    esta configuración es visible.

    🔴 **El ítem entero va entre comillas dobles, y las tres formas se
    midieron** (2026-08-29, con `yaml.safe_load` sobre las líneas generadas):

    | Forma | `LIBRAAUTH_SMTP_FROM_NAME=La Grace` | `…=Casa # 5` |
    |---|---|---|
    | `- K=valor` | ✅ `La Grace` | ❌ `Casa` — YAML lo lee como comentario |
    | `- K='valor'` (`shlex.quote`) | ❌ `'La Grace'` **con las comillas** | ❌ ídem |
    | `- "K=valor"` | ✅ `La Grace` | ✅ `Casa # 5` |

    La segunda es la trampa: `shlex.quote` cita para un **shell**, y acá el
    consumidor es YAML. Compose parte el ítem en el primer `=` y toma el resto
    literal, así que las comillas del shell terminan adentro del valor y cada
    correo saldría con el remitente entre comillas.

    Sólo `from_name` puede traer algo raro —sale de un formulario—; el resto
    son alfabetos controlados (slug, dominio, `token_urlsafe`). Se cita igual
    las seis: una regla que aplica a todas no se puede aplicar mal a una.
    """
    return "".join(f'{sangria}- "{clave}={valor}"\n' for clave, valor in _pares_escapados(cuenta))


def _pares_escapados(cuenta: CuentaDeCorreo) -> list[tuple[str, str]]:
    """`(variable, valor)` de las seis, con el valor ya escapado para YAML entre comillas dobles."""
    def v(valor) -> str:
        # Escapado de escalar YAML entre comillas dobles: la barra primero, si
        # no se re-escaparía la que introduce la comilla.
        return str(valor).replace("\\", "\\\\").replace('"', '\\"')

    return [
        (clave, v(valor))
        for clave, valor in [
            ("LIBRAAUTH_SMTP_HOST", cuenta.host),
            ("LIBRAAUTH_SMTP_PORT", cuenta.port),
            ("LIBRAAUTH_SMTP_USER", cuenta.direccion),
            ("LIBRAAUTH_SMTP_PASSWORD", cuenta.password),
            ("LIBRAAUTH_SMTP_FROM_EMAIL", cuenta.direccion),
            ("LIBRAAUTH_SMTP_FROM_NAME", cuenta.from_name),
        ]
    ]


# ── el compose de una instancia que ya existe (ADR-046) ──────────────────────

def smtp_en_el_compose(contenido: str) -> bool:
    """Si el compose ya trae la contraseña SMTP de la instancia.

    Es la señal de «esta instancia ya tiene casilla»: la contraseña es la única
    variable que no se puede reconstruir sin crear la casilla de nuevo.
    """
    return re.search(r"^[ \t]*-[ \t]*[\"']?LIBRAAUTH_SMTP_PASSWORD=", contenido, re.MULTILINE) is not None


def con_cuenta_en_compose(contenido: str, indice_imagen_de_la_app: int,
                          cuenta: CuentaDeCorreo) -> str | None:
    """El compose con las seis variables SMTP en el `environment:` de la app.

    Misma regla que `errores_env.con_variables`: `None` si el servicio no tiene
    un `environment:` en forma de lista —no se adivina un formato que no se
    conoce— y **idempotente** (las que ya estaban se reemplazan en su lugar, las
    que faltan se agregan al final de la lista). `indice_imagen_de_la_app` es la
    línea (base 0) de la `image:` del servicio de la app.
    """
    from . import errores_env as ee

    lineas = contenido.split("\n")
    inicio, fin = ee._bloque_del_servicio(lineas, indice_imagen_de_la_app)
    env = next((i for i in range(inicio, fin) if re.match(r"^    environment:[ \t]*$", lineas[i])), None)
    if env is None:
        return None
    ultimo, sangria, presentes = env, "      ", {}
    i = env + 1
    while i < fin:
        linea = lineas[i]
        if not linea.strip() or linea.lstrip().startswith("#"):
            i += 1
            continue
        m = ee._ITEM_ENV.match(linea)
        if not m:
            break
        sangria = m.group("sangria")
        presentes[m.group("clave")] = i
        ultimo = i
        i += 1
    if not presentes and i < fin and re.match(r"^\s+[A-Za-z_][A-Za-z0-9_]*:", lineas[i]):
        return None  # `environment:` en formato de mapa
    # Insertar después de `ultimo` no corre ninguna de las líneas ya halladas
    # (todas están en o antes de `ultimo`), así que `presentes` sigue valiendo.
    for clave, valor in _pares_escapados(cuenta):
        linea_nueva = f'{sangria}- "{clave}={valor}"'
        if clave in presentes:
            lineas[presentes[clave]] = linea_nueva
            continue
        lineas.insert(ultimo + 1, linea_nueva)
        ultimo += 1
    return "\n".join(lineas)
