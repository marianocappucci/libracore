"""El enlace de la copia externa con la nube del cliente, hecho desde su pantalla.

## Que cambio respecto de la vuelta 2, y por que

Hasta el 2026-09-10 la cuenta de cada cliente la conectaba una persona en el
host con `rclone authorize`, y el contenedor **nunca** veia la credencial. Ese
dia el humano decidio que el enlace lo haga el propio cliente desde
Configuracion -> Datos / Backup, con OAuth **dentro de la app** (ver
`wiki/analyses/resguardo-backup-familia-libra.md`).

Lo que se resigna, dicho sin vueltas: **la app de la instancia pasa a tener el
token de la nube del cliente**. Comprometer la app da acceso a lo que ese token
alcanza. Lo acotan dos cosas:

- **Google Drive va con `drive.file`**: el token solo ve los archivos que creo
  esta integracion, no el Drive entero. Dropbox tiene que registrarse como app
  de tipo *App folder*, que solo ve su propia carpeta.
- Quien compromete la app ya tiene los datos que se respaldan. Lo que suma el
  token es escribir en esa carpeta, no leer el resto de la cuenta.

## Que sigue siendo del host

**Subir.** `provisioning.resguardo_externo` sigue corriendo en el host por
cron. Lo unico que cambia es de donde saca el destino: si la instancia tiene un
enlace, usa el `rclone.conf` que deja este modulo.

## Donde queda

En `<backups_dir>/.resguardo/`, todo `0600` y escrito por reemplazo atomico:

- `rclone.conf` — un remoto `[externo]` con el token.
- `enlace.json` — proveedor, cuenta y desde cuando. Sin secretos.
- `pendiente.json` — el `state` y el verificador PKCE de un enlace a medio
  hacer. De un solo uso, vence a los 10 minutos.

🔴 **Nunca entra al ZIP del backup**: `respaldo.crear_backup` poda cualquier
directorio `.resguardo`. Si entrara, cada backup descargado llevaria el acceso
a la nube del cliente, y un backup se manda por mail.
"""
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from libracore.resguardo_estado import ESTADO

DIRECTORIO = ".resguardo"
CONF = "rclone.conf"
ENLACE = "enlace.json"
PENDIENTE = "pendiente.json"

#: Nombre del remoto dentro del `rclone.conf` de la instancia. Uno por archivo,
#: asi que no hace falta que sea unico entre instancias.
REMOTO = "externo"

#: Segundos que tiene el cliente para volver del consentimiento.
VENCE_PENDIENTE = 600

_TIMEOUT = 15

# UTC-3 fijo, sin horario de verano: el estandar de la familia para lo que se
# muestra. El `expiry` del token va en UTC, porque lo lee rclone y no una persona.
_AR = timezone(timedelta(hours=-3))


@dataclass(frozen=True)
class Proveedor:
    clave: str
    nombre: str
    env_id: str
    env_secret: str
    auth_url: str
    token_url: str
    revoke_url: str
    scope: str
    rclone_type: str
    extra_auth: dict = field(default_factory=dict)
    extra_rclone: dict = field(default_factory=dict)

    def credenciales(self) -> tuple[str, str] | None:
        """`(client_id, client_secret)` del entorno, o `None`.

        Sin las dos, el proveedor **no se ofrece** en la pantalla: un boton que
        lleva a un error de Google es peor que no tener el boton.
        """
        cid = os.environ.get(self.env_id, "").strip()
        sec = os.environ.get(self.env_secret, "").strip()
        return (cid, sec) if cid and sec else None


PROVEEDORES = {
    "drive": Proveedor(
        clave="drive",
        nombre="Google Drive",
        env_id="RESGUARDO_GDRIVE_CLIENT_ID",
        env_secret="RESGUARDO_GDRIVE_CLIENT_SECRET",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        revoke_url="https://oauth2.googleapis.com/revoke",
        # `drive.file` y no `drive`: ademas de acotar lo que ve el token, `drive`
        # es un scope *restringido* y Google exige una auditoria de seguridad
        # para publicar una app que lo pida.
        scope="https://www.googleapis.com/auth/drive.file",
        rclone_type="drive",
        # Los dos juntos. Sin `prompt=consent`, a partir del segundo enlace de la
        # misma cuenta Google no devuelve `refresh_token`, el token de acceso
        # muere en una hora y el primer cron ya falla.
        extra_auth={"access_type": "offline", "prompt": "consent"},
        extra_rclone={"scope": "drive.file"},
    ),
    "dropbox": Proveedor(
        clave="dropbox",
        nombre="Dropbox",
        env_id="RESGUARDO_DROPBOX_APP_KEY",
        env_secret="RESGUARDO_DROPBOX_APP_SECRET",
        auth_url="https://www.dropbox.com/oauth2/authorize",
        token_url="https://api.dropboxapi.com/oauth2/token",
        revoke_url="https://api.dropboxapi.com/2/auth/token/revoke",
        scope="account_info.read files.metadata.read files.content.read files.content.write",
        rclone_type="dropbox",
        # Sin esto Dropbox entrega solo un token de 4 horas, sin refresh.
        extra_auth={"token_access_type": "offline"},
    ),
}


class EnlaceError(Exception):
    """El enlace no se pudo hacer. El mensaje va tal cual a la pantalla del
    cliente, asi que dice que paso y nunca lleva un secreto."""


# ── Archivos ─────────────────────────────────────────────────────────────────

def _dir(backups_dir) -> Path:
    return Path(backups_dir) / DIRECTORIO


def _escribir(ruta: Path, texto: str) -> None:
    """Reemplazo atomico, `0600`.

    Atomico porque del otro lado lee el rclone del host, que puede estar
    corriendo justo ahora. Y por `os.replace` y no abriendo el archivo
    existente: rclone reescribe el `rclone.conf` al refrescar el token, asi que
    puede quedar de otro usuario; reemplazarlo solo pide permiso sobre el
    directorio. `mkstemp` ya crea el archivo `0600`.
    """
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ruta.parent, prefix=f".{ruta.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(texto)
        os.replace(tmp, ruta)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _leer_json(ruta: Path) -> dict | None:
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return datos if isinstance(datos, dict) else None


def enlace_de(backups_dir) -> dict | None:
    """El enlace vigente, o `None`.

    Exige **los dos** archivos: un `enlace.json` sin `rclone.conf` es un enlace
    que el host no puede usar, y mostrarlo como conectado seria mentir.
    """
    d = _dir(backups_dir)
    datos = _leer_json(d / ENLACE)
    if not datos or not (d / CONF).is_file():
        return None
    return datos


def config_rclone(backups_dir) -> Path | None:
    """El `rclone.conf` de la instancia, para el subidor del host."""
    return _dir(backups_dir) / CONF if enlace_de(backups_dir) else None


def _token_de(conf: Path) -> dict | None:
    """El token guardado, para revocarlo. `None` si no se puede leer — por
    ejemplo porque rclone lo reescribio desde el host con otro dueño."""
    try:
        for linea in conf.read_text(encoding="utf-8").splitlines():
            clave, _, valor = linea.partition("=")
            if clave.strip() == "token":
                return json.loads(valor.strip())
    except (OSError, ValueError):
        return None
    return None


# ── El flujo ─────────────────────────────────────────────────────────────────

def _proveedor(clave: str) -> Proveedor:
    prov = PROVEEDORES.get(clave)
    if prov is None:
        raise EnlaceError(f"No se conoce el proveedor '{clave}'.")
    return prov


def _credenciales(prov: Proveedor) -> tuple[str, str]:
    cred = prov.credenciales()
    if cred is None:
        raise EnlaceError(f"{prov.nombre} todavia no esta habilitado en este servidor.")
    return cred


def _pkce() -> tuple[str, str]:
    """`(verificador, desafio)` S256. Lo soportan los dos proveedores, y hace
    que un `code` interceptado no sirva sin el verificador, que no sale del
    servidor."""
    verificador = secrets.token_urlsafe(64)
    desafio = base64.urlsafe_b64encode(
        hashlib.sha256(verificador.encode()).digest()
    ).rstrip(b"=").decode()
    return verificador, desafio


def estado(backups_dir) -> dict:
    """Lo que la pantalla necesita: que proveedores puede ofrecer y a que
    cuenta esta conectada la instancia, si lo esta."""
    return {
        "proveedores": [
            {"clave": p.clave, "nombre": p.nombre}
            for p in PROVEEDORES.values() if p.credenciales()
        ],
        "enlace": enlace_de(backups_dir),
    }


def iniciar(backups_dir, clave: str, redirect_uri: str, *, ahora: float | None = None) -> str:
    """Deja el enlace pendiente y devuelve la URL de consentimiento.

    Un solo pendiente por instancia: empezar otro invalida el anterior, que es
    lo que se quiere si el cliente cerro la pestaña de Google y volvio a
    apretar el boton.
    """
    prov = _proveedor(clave)
    client_id, _ = _credenciales(prov)
    state = secrets.token_urlsafe(32)
    verificador, desafio = _pkce()
    _escribir(_dir(backups_dir) / PENDIENTE, json.dumps({
        "state": state,
        "proveedor": prov.clave,
        "verificador": verificador,
        # Se guarda: el canje tiene que mandar EXACTAMENTE la misma, y
        # recalcularla en el callback la haria depender de los headers de otra
        # request.
        "redirect_uri": redirect_uri,
        "vence": (ahora if ahora is not None else time.time()) + VENCE_PENDIENTE,
    }))
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": prov.scope,
        "state": state,
        "code_challenge": desafio,
        "code_challenge_method": "S256",
        **prov.extra_auth,
    }
    return f"{prov.auth_url}?{urlencode(params)}"


def descartar_pendiente(backups_dir) -> None:
    (_dir(backups_dir) / PENDIENTE).unlink(missing_ok=True)


def _cuenta(prov: Proveedor, access_token: str) -> str | None:
    """El mail de la cuenta, para que la pantalla diga A QUE quedo conectada.

    Es informativo: si falla, el enlace sirve igual y no se aborta por esto.
    """
    h = {"Authorization": f"Bearer {access_token}"}
    try:
        if prov.clave == "drive":
            r = httpx.get(
                "https://www.googleapis.com/drive/v3/about",
                params={"fields": "user(emailAddress)"}, headers=h, timeout=_TIMEOUT,
            )
            return r.json()["user"]["emailAddress"] if r.status_code == 200 else None
        r = httpx.post(
            "https://api.dropboxapi.com/2/users/get_current_account", headers=h, timeout=_TIMEOUT,
        )
        return r.json()["email"] if r.status_code == 200 else None
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


def completar(backups_dir, *, state: str, code: str, carpeta: str, ahora: float | None = None) -> dict:
    """Canjea el `code` y deja el `rclone.conf` listo para el host.

    🔴 **El pendiente se borra ANTES de canjear**, pase lo que pase despues. Un
    `state` sirve una sola vez: el cliente que vuelve atras y recarga la pagina
    del callback no puede canjear dos veces, y un `state` robado no sirve si ya
    se uso.
    """
    ruta = _dir(backups_dir) / PENDIENTE
    pendiente = _leer_json(ruta)
    ruta.unlink(missing_ok=True)

    if not pendiente or not secrets.compare_digest(str(pendiente.get("state", "")), state or ""):
        raise EnlaceError(
            "La respuesta no corresponde a un enlace iniciado desde esta pantalla. Volve a intentarlo."
        )
    if (ahora if ahora is not None else time.time()) > float(pendiente.get("vence", 0)):
        raise EnlaceError("Pasaron mas de 10 minutos desde que empezaste. Volve a intentarlo.")

    prov = _proveedor(pendiente.get("proveedor", ""))
    client_id, client_secret = _credenciales(prov)
    r = httpx.post(prov.token_url, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pendiente["redirect_uri"],
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": pendiente["verificador"],
    }, timeout=_TIMEOUT)
    if r.status_code != 200:
        # El cuerpo NO va al mensaje: puede traer el `code` de vuelta.
        raise EnlaceError(f"{prov.nombre} rechazo el enlace (codigo {r.status_code}). Volve a intentarlo.")
    tok = r.json()
    if not tok.get("access_token") or not tok.get("refresh_token"):
        # Sin refresh token la copia sube una noche y despues falla para
        # siempre: mejor no enlazar que enlazar algo que vence solo.
        raise EnlaceError(
            f"{prov.nombre} no dio un permiso permanente. Volve a intentarlo y acepta todos los permisos."
        )

    expira = datetime.now(UTC) + timedelta(seconds=int(tok.get("expires_in") or 3600))
    token_rclone = {
        "access_token": tok["access_token"],
        "token_type": tok.get("token_type") or "Bearer",
        "refresh_token": tok["refresh_token"],
        "expiry": expira.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    lineas = [
        f"[{REMOTO}]",
        f"type = {prov.rclone_type}",
        f"client_id = {client_id}",
        # rclone lo necesita para refrescar el token: sin el, el enlace dura
        # lo que dure el primer token de acceso.
        f"client_secret = {client_secret}",
        *(f"{k} = {v}" for k, v in prov.extra_rclone.items()),
        f"token = {json.dumps(token_rclone, separators=(',', ':'))}",
    ]
    d = _dir(backups_dir)
    _escribir(d / CONF, "\n".join(lineas) + "\n")

    enlace = {
        "proveedor": prov.clave,
        "nombre": prov.nombre,
        "cuenta": _cuenta(prov, tok["access_token"]),
        "carpeta": carpeta,
        "desde": datetime.now(_AR).isoformat(timespec="seconds"),
    }
    _escribir(d / ENLACE, json.dumps(enlace, ensure_ascii=False, indent=2))
    # El estado de la subida anterior describe OTRO destino. Dejarlo haria que
    # la pantalla muestre "al dia" para una cuenta a la que todavia no se subio
    # nada.
    (Path(backups_dir) / ESTADO).unlink(missing_ok=True)
    return enlace


def _revocar(prov: Proveedor, token: dict) -> bool:
    try:
        if prov.clave == "drive":
            # Revocar el refresh token de Google revoca el permiso entero.
            r = httpx.post(prov.revoke_url, data={"token": token["refresh_token"]}, timeout=_TIMEOUT)
        else:
            r = httpx.post(
                prov.revoke_url,
                headers={"Authorization": f"Bearer {token['access_token']}"},
                timeout=_TIMEOUT,
            )
        return r.status_code == 200
    except (httpx.HTTPError, KeyError):
        return False


def desvincular(backups_dir) -> dict:
    """Borra el enlace. Intenta revocarlo del lado del proveedor.

    La revocacion es **de buena fe**: si no se puede (el token ya vencio, el
    archivo lo reescribio el host con otro dueño, no hay red), el enlace se
    borra igual — lo que el cliente pidio es que no se suba mas, y eso se
    cumple sin el proveedor. `revocado: false` le avisa a la pantalla que el
    permiso puede seguir figurando en su cuenta.
    """
    d = _dir(backups_dir)
    enlace = _leer_json(d / ENLACE) or {}
    prov = PROVEEDORES.get(enlace.get("proveedor", ""))
    token = _token_de(d / CONF)
    revocado = bool(prov and token and _revocar(prov, token))
    for nombre in (CONF, ENLACE, PENDIENTE):
        (d / nombre).unlink(missing_ok=True)
    (Path(backups_dir) / ESTADO).unlink(missing_ok=True)
    return {"revocado": revocado}


# ── HTTP ─────────────────────────────────────────────────────────────────────

def build_resguardo_enlace_router(
    backups_dir,
    *,
    prefix: str = "/api/config",
    carpeta: str = "Resguardo Libra",
    volver_a: str = "/configuracion?seccion=datos",
    url_base: str | None = None,
):
    """Conectar, ver y desconectar la nube del cliente.

    Se monta como el resto de Configuracion, con el gate que pone el producto,
    **mas el del modulo del plan**:

        app.include_router(
            build_resguardo_enlace_router(BACKUPS_DIR, carpeta="Resguardo Contalibra"),
            dependencies=[Depends(require_admin), Depends(require_module("resguardo_externo"))],
        )

    El callback queda detras del mismo gate **a proposito**: la cookie de
    sesion es `SameSite=Lax`, asi que viaja en la redireccion que hace Google
    o Dropbox de vuelta. Un callback abierto dependeria solo del `state`.

    `url_base` (o `RESGUARDO_OAUTH_URL_BASE`) fija el origen publico con el que
    se arma el `redirect_uri`, que tiene que coincidir **exactamente** con uno
    registrado en la consola del proveedor. Sin eso se deduce de la request
    (`X-Forwarded-Proto` + `Host`), que detras de Nginx Proxy Manager da el
    dominio del cliente.
    """
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import RedirectResponse

    router = APIRouter(prefix=f"{prefix}/resguardo-externo/enlace", tags=["config"])

    def _redirect_uri(request: Request) -> str:
        base = url_base or os.environ.get("RESGUARDO_OAUTH_URL_BASE", "").strip()
        if not base:
            proto = request.headers.get("x-forwarded-proto") or request.url.scheme
            base = f"{proto}://{request.headers.get('host') or request.url.netloc}"
        return f"{base.rstrip('/')}{router.prefix}/callback"

    def _volver(resultado: str, detalle: str | None = None):
        sep = "&" if "?" in volver_a else "?"
        params = {"resguardo": resultado, **({"detalle": detalle} if detalle else {})}
        return RedirectResponse(f"{volver_a}{sep}{urlencode(params)}", status_code=303)

    @router.get("")
    def ver():
        return estado(backups_dir)

    @router.post("/{proveedor}")
    def conectar(proveedor: str, request: Request):
        try:
            return {"url": iniciar(backups_dir, proveedor, _redirect_uri(request))}
        except EnlaceError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/callback", include_in_schema=False)
    def callback(state: str = "", code: str = "", error: str = ""):
        if error:
            descartar_pendiente(backups_dir)
            # El `error` que manda el proveedor NO se refleja tal cual: viene en
            # la URL, y cualquiera puede armar una con el texto que quiera.
            if error == "access_denied":
                return _volver("error", "Cancelaste el permiso, asi que no se conecto nada.")
            return _volver("error", "El proveedor no completo el enlace. Volve a intentarlo.")
        try:
            completar(backups_dir, state=state, code=code, carpeta=carpeta)
        except EnlaceError as exc:
            return _volver("error", str(exc))
        except httpx.HTTPError:
            return _volver("error", "No se pudo hablar con el proveedor. Volve a intentarlo en un rato.")
        return _volver("ok")

    @router.delete("")
    def desconectar():
        return {"ok": True, **desvincular(backups_dir)}

    return router
