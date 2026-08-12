"""Los dos routers de la pantalla de Configuracion que son iguales en los seis
productos: **datos de la empresa con logo** y **Datos / Backup**.

Nacen de [[contalibra]], que es el unico que los tenia, al ir a repetirlos en
los otros cuatro (items 1 y 4 de los pendientes transversales del 2026-08-04).
Mismo criterio que `libraauth.build_logs_router`: el paquete arma el router, el
producto lo monta con **su** dependencia de rol.

## Por que el gate lo pone el producto

El vocabulario de roles no es el mismo en los seis, y meterlo aca obligaria a
este paquete a conocerlos todos. El pedido dice *"solo administradores"* y eso
lo cumple el producto:

    app.include_router(build_empresa_router(), dependencies=[Depends(require_admin)])

⚠️ **Menos el `GET`**, que en LibraDesk ya es abierto a cualquier usuario
logueado (`GET /api/config-empresa` existe desde antes y lo usa el generador de
PDF). Por eso los datos de empresa salen en **dos** routers con el mismo
prefijo: uno de lectura y otro de escritura. Es el mismo patron que
`smtp_router` en Contalibra, y por el mismo motivo — FastAPI evalua las
dependencias del router antes que las de la ruta, asi que no alcanza con
ponerle un guard distinto a cada endpoint.
"""
import os
from typing import Callable

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from libracore import config_manager
from libracore.resguardo_estado import resumen as resumen_resguardo
from libracore.respaldo import (
    BackupInvalido,
    Instancia,
    crear_backup,
    listar_backups,
    restaurar_backup,
)

# Lo que el navegador puede mostrar y los generadores de PDF/ticket saben
# incrustar. `.webp` queda afuera a proposito: `resolve_logo_path` lo acepta
# como fallback historico, pero fpdf2 no lo dibuja — dejarlo subir daria un
# logo que se ve en la pantalla y revienta el comprobante.
_EXTS_LOGO = (".png", ".jpg", ".jpeg")


class EmpresaPayload(BaseModel):
    """Los 8 campos que ya estan en `config_manager.DEFAULTS`. Se declaran acá
    y no se acepta un dict libre para que un `PUT` con una clave de mas no
    pueda escribir en `config.json` cualquier cosa — ahi tambien viven el token
    de MercadoPago y la contrasena de SMTP."""

    empresa_nombre: str = ""
    empresa_direccion: str = ""
    empresa_cuit: str = ""
    empresa_telefono: str = ""
    empresa_email: str = ""
    empresa_iibb: str = ""
    empresa_iva_condition: str = ""
    empresa_inicio_actividades: str = ""


def _solo_empresa(cfg: dict) -> dict:
    """Lo que se devuelve al frontend.

    **No es `config_manager.load()` entero**, que es lo que hace Contalibra
    hoy: ese dict trae `mp_access_token`, `email_smtp_password` y el resto de
    los secretos de la instancia. Los devuelve porque su pantalla de
    Configuracion los edita todos en el mismo lugar; acá el router es solo de
    empresa, asi que mandarlos seria filtrarlos a una pantalla que no los pide.
    """
    return {k: cfg.get(k, "") for k in EmpresaPayload.model_fields}


def build_empresa_router(*, prefix: str = "/api/config") -> APIRouter:
    """Lectura de los datos de empresa y del logo. **Sin gate propio**: el
    producto decide si lo abre a cualquier usuario logueado (LibraDesk ya lo
    hace, porque el PDF lo necesita) o lo cierra a admin."""
    router = APIRouter(prefix=prefix, tags=["config"])

    @router.get("/empresa")
    def obtener_empresa():
        return _solo_empresa(config_manager.load())

    @router.get("/empresa/logo", include_in_schema=False)
    def obtener_logo():
        path = config_manager.resolve_logo_path()
        if not path or not os.path.exists(path):
            raise HTTPException(404, "No hay logo cargado.")
        ext = os.path.splitext(path)[1].lower()
        return FileResponse(path, media_type="image/png" if ext == ".png" else "image/jpeg")

    return router


def build_empresa_admin_router(*, prefix: str = "/api/config") -> APIRouter:
    """Escritura de los datos de empresa y subida del logo. Va montado con
    `require_admin` (o el equivalente del producto)."""
    router = APIRouter(prefix=prefix, tags=["config"])

    @router.put("/empresa")
    def actualizar_empresa(payload: EmpresaPayload):
        cfg = config_manager.load()
        cfg.update(payload.model_dump())
        config_manager.save(cfg)
        return _solo_empresa(config_manager.load())

    @router.post("/empresa/logo")
    async def subir_logo(logo: UploadFile = File(...)):
        ext = os.path.splitext(logo.filename or "")[1].lower()
        if ext not in _EXTS_LOGO:
            raise HTTPException(422, "El logo debe ser PNG o JPG.")
        os.makedirs(config_manager.LOGO_DIR, exist_ok=True)

        # Se borran los logos anteriores en vez de dejarlos convivir. Sin esto,
        # subir un `.png` sobre un `.jpg` deja los dos archivos y
        # `resolve_logo_path` elige **por fecha de modificacion** cuando el
        # `logo_path` guardado no existe — o sea que el logo viejo puede volver
        # solo despues de una migracion de rutas.
        for viejo in os.listdir(config_manager.LOGO_DIR):
            if viejo.lower().startswith("logo") and viejo.lower().endswith(_EXTS_LOGO):
                try:
                    os.unlink(os.path.join(config_manager.LOGO_DIR, viejo))
                except OSError:
                    pass

        destino = os.path.join(config_manager.LOGO_DIR, f"logo{ext}")
        with open(destino, "wb") as f:
            f.write(await logo.read())

        cfg = config_manager.load()
        cfg["logo_path"] = destino
        config_manager.save(cfg)
        return _solo_empresa(config_manager.load())

    @router.delete("/empresa/logo")
    def borrar_logo():
        """Sacar el logo tiene que ser posible desde la pantalla. Sin esto, el
        unico modo de volver a un comprobante sin logo es entrar al volumen."""
        for viejo in os.listdir(config_manager.LOGO_DIR) if os.path.isdir(config_manager.LOGO_DIR) else []:
            if viejo.lower().startswith("logo") and viejo.lower().endswith(_EXTS_LOGO):
                try:
                    os.unlink(os.path.join(config_manager.LOGO_DIR, viejo))
                except OSError:
                    pass
        cfg = config_manager.load()
        cfg["logo_path"] = ""
        config_manager.save(cfg)
        return {"ok": True}

    return router


def build_backup_router(
    instancia: Instancia | Callable[[], Instancia],
    backups_dir,
    *,
    prefix: str = "/api/config",
    cerrar_conexiones=None,
    reabrir_conexiones=None,
) -> APIRouter:
    """La pestana "Datos / Backup": listar, bajar, subir y restaurar.

    `instancia` acepta un callable para el producto que arma sus rutas recien
    en `create_app()` (los tests montan varias apps en el mismo proceso, con
    un `tmp_path` distinto cada una).

    🔴 **Pasar `cerrar_conexiones`/`reabrir_conexiones`.** Sin ellos el restore
    devuelve `ok` y **no tiene efecto** hasta que alguien reinicie el
    contenedor: el proceso sigue con el archivo viejo abierto. Ver el docstring
    de `respaldo.restaurar_backup`. En los productos con SQLAlchemy alcanza con
    pasar `engine.dispose` en los dos.
    """
    router = APIRouter(prefix=prefix, tags=["config"])
    _resolver = instancia if callable(instancia) else (lambda: instancia)

    @router.get("/backups")
    def listar():
        return listar_backups(backups_dir)

    @router.post("/backups")
    def crear():
        """Backup a pedido. Es el boton "Backup rapido" que Contalibra tiene
        siempre visible al lado de las pestanas: el cliente lo aprieta antes de
        hacer algo que lo pone nervioso."""
        destino = crear_backup(_resolver(), backups_dir, motivo="manual")
        return {"ok": True, "filename": os.path.basename(destino)}

    @router.get("/backups/{filename}")
    def descargar(filename: str):
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(400, "Nombre de archivo invalido.")
        path = os.path.join(backups_dir, filename)
        if not os.path.exists(path):
            raise HTTPException(404, "Ese backup ya no esta.")
        return FileResponse(path, media_type="application/zip", filename=filename)

    @router.get("/backup-ahora")
    def bajar_ahora():
        """Genera y devuelve el backup en la misma request, sin dejarlo en el
        servidor. Es lo que el cliente quiere el 90% de las veces —"dame una
        copia"— y evita que cada descarga sume un archivo al disco del VPS."""
        inst = _resolver()
        destino = crear_backup(inst, backups_dir, motivo="descarga")
        return FileResponse(
            destino, media_type="application/zip", filename=os.path.basename(destino),
        )

    @router.get("/resguardo-externo")
    def estado_externo():
        """Que paso con la copia a la nube del cliente.

        Lee el `.externo.json` que deja el subidor del host. La app **no sube
        nada** ni ve la credencial de la nube: sólo cuenta el estado.

        `contratado: false` cuando no hay archivo — para la pantalla eso es "no
        tenés el add-on", no "está fallando". La diferencia importa: mostrarle
        una alarma a quien no contrató el servicio es ruido.
        """
        return resumen_resguardo(backups_dir)

    @router.post("/restore")
    async def restaurar(backup_file: UploadFile = File(...)):
        try:
            return restaurar_backup(
                _resolver(), await backup_file.read(), backups_dir,
                cerrar_conexiones=cerrar_conexiones,
                reabrir_conexiones=reabrir_conexiones,
            )
        except BackupInvalido as exc:
            # 422 y no 500: el archivo se leyo perfecto, lo que no sirve es su
            # contenido. Y el mensaje va tal cual a la pantalla.
            raise HTTPException(422, str(exc))

    return router
