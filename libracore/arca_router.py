"""La pantalla de configuración de ARCA, igual en los productos que facturan.

Nace de tener el mismo formulario escrito de tres formas distintas: Contalibra
y Restolibra en `web/api/config.py` bajo `/api/config/arca`, Gestiolibra,
MedLibra, VentaLibra y LibraClub en `routers/billing.py` bajo `/config/arca`, y
[[libracargo]] con el suyo propio en `/api/arca`. Mismo criterio que
`config_router`: **el paquete arma el router, el producto lo monta con su
dependencia de rol**, porque el vocabulario de roles no es el mismo en los seis.

    app.include_router(build_arca_router(), dependencies=[Depends(require_admin)])

## Los dos defectos que este router cierra, y que ninguno tenía solo

1. 🔴 **Se subía el certificado sin mirarlo.** Contalibra y Restolibra escriben
   los bytes que lleguen: subir el `.csr` —el pedido— en vez del `.crt` que ARCA
   devuelve se acepta en pantalla y falla recién al emitir el primer
   comprobante, con un error de ARCA que no habla de la causa. Acá el par pasa
   por `arca_certificados` **antes** de tocar el disco.

2. 🔴 **Cuatro productos no tenían dónde subirlo.** Gestiolibra, MedLibra,
   VentaLibra y LibraClub reciben del cliente un `certificado_path` y un
   `clave_path` —una ruta del filesystem del servidor, que alguien tiene que
   haber dejado ahí a mano— y los guardan tal cual. Además de que el alta no se
   podía hacer desde el navegador, es un campo que el admin escribe y el
   servidor abre. Acá los paths los pone el servidor y no se aceptan por API.

## Por qué `CERTS_DIR` se lee en cada request

`config_manager.CERTS_DIR` se resuelve **al importar**, desde `DATA_DIR`. Si el
router lo capturara al armarse, los tests —que montan varias apps en el mismo
proceso, cada una con su `tmp_path`— escribirían todos en el mismo directorio, y
el primero en correr definiría dónde. Se lee adentro de cada endpoint.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from libracore import arca_certificados, arca_wsaa, config_manager
from libracore.db import arca_config as db_arca_config

#: Los nombres con los que se guardan. Son fijos a propósito: `resolve_cert_paths`
#: cae a estos dos si el path guardado quedó obsoleto (ej. una migración de
#: volumen), y ese rescate no funciona si cada instancia los llama distinto.
NOMBRE_CERTIFICADO = "certificado.crt"
NOMBRE_CLAVE = "clave_privada.key"

AMBIENTES = ("homologacion", "produccion")


class ArcaPayload(BaseModel):
    """Lo que la pantalla edita. **Los paths no están acá a propósito**: los
    pone el servidor al recibir el archivo, no el cliente en un JSON."""

    #: 🔴 Vacío y no `"default"`: con `"default"` como valor del campo, "no lo
    #: mandaron" y "lo mandaron como default" son indistinguibles, y el router
    #: no puede caer en la fila que ya existe ni en el slug del producto. Ver
    #: `empresa_por_defecto` en `build_arca_router`.
    empresa: str = ""
    cuit: str = ""
    punto_venta: int = Field(default=1, ge=1)
    ambiente: str = "homologacion"
    alias: str = ""


def _resolver(empresa: str) -> dict | None:
    """La configuración sobre la que opera la pantalla.

    ⚠️ Con `empresa` vacío devuelve **la primera activa**, no la que se llama
    "default". Es lo que hacen hoy los seis productos (`configs[0]`), y cambiarlo
    por una búsqueda del slug "default" dejaría sin configuración a toda
    instancia cuya fila se dio de alta con la razón social como nombre — que es
    el caso de Contalibra en producción.
    """
    if empresa:
        return db_arca_config.obtener_arca_config(empresa)
    activas = db_arca_config.obtener_todas_arca_configs()
    return activas[0] if activas else None


def _certs_dir() -> str:
    return config_manager.CERTS_DIR


def _paths(cfg: dict | None) -> tuple[str, str]:
    cfg = cfg or {}
    return config_manager.resolve_cert_paths(
        cfg.get("certificado_path", ""), cfg.get("clave_path", "")
    )


def _existe(path: str) -> bool:
    return bool(path) and os.path.exists(path)


def _guardar_path(empresa: str, *, certificado_path=None, clave_path=None) -> dict:
    """Escribe el path en la fila, creándola si todavía no está."""
    existente = db_arca_config.obtener_arca_config(empresa)
    if existente:
        db_arca_config.actualizar_arca_config(
            empresa,
            certificado_path=certificado_path,
            clave_path=clave_path,
        )
    else:
        db_arca_config.crear_arca_config(
            empresa=empresa, cuit="", punto_venta=1,
            clave_path=clave_path or "", certificado_path=certificado_path or "",
        )
    return db_arca_config.obtener_arca_config(empresa)


def build_arca_router(
    *,
    prefix: str = "/config/arca",
    empresa_por_defecto: str = "default",
) -> APIRouter:
    """El router de configuración de ARCA. Sin gate propio: lo pone el producto.

    `prefix` existe porque los productos ya publicaron rutas distintas y un
    cambio de prefijo rompe el frontend desplegado. La normalización de la ruta
    se hace producto por producto, no de prepo desde acá.

    ## `empresa_por_defecto`, y la falla muda que cierra

    🔴 **Cuatro productos leen su configuración de facturación con un slug
    FIJO** —`negocio` en Gestiolibra, `consultorio` en MedLibra, `venta` en
    VentaLibra, `complejo` en LibraClub—, porque son de instancia única y no
    tienen lista de empresas.

    En una instancia que todavía no facturó no hay fila, y el primer `PUT` la
    crea. Sin este parámetro la creaba como `default`: el `PUT` contesta 200, la
    pantalla dice "Guardado", y el servicio de facturación de esos cuatro **no
    lee esa fila nunca**. Se descubre al emitir el primer comprobante, con un
    "ARCA no está configurado" sobre una pantalla que muestra el certificado
    cargado.

    Se resuelve acá y no en cada llamador a propósito: la pantalla compartida ya
    manda el slug, pero un script, el backoffice o un `curl` no tienen por qué
    saberlo. El default correcto es del producto, y el producto lo declara una
    vez al montar el router.
    """
    router = APIRouter(prefix=prefix, tags=["arca"])

    @router.get("")
    def obtener(empresa: str = ""):
        """La configuración actual, o `null` si la instancia todavía no facturó.

        Devuelve los paths porque la pantalla muestra *si hay* archivo cargado,
        pero el que decide qué path se escribe es el servidor.
        """
        cfg = _resolver(empresa)
        if not cfg:
            return None
        cert_path, clave_path = _paths(cfg)
        return {
            "empresa":          cfg.get("empresa", ""),
            "cuit":             cfg.get("cuit", ""),
            "punto_venta":      cfg.get("punto_venta", 1),
            "ambiente":         cfg.get("ambiente", "homologacion"),
            "alias":            cfg.get("alias", "") or "",
            "certificado_path": cert_path,
            "clave_path":       clave_path,
            "tiene_certificado": _existe(cert_path),
            "tiene_clave":       _existe(clave_path),
        }

    @router.put("")
    def guardar(payload: ArcaPayload):
        empresa = payload.empresa.strip() or _empresa_de("", empresa_por_defecto)
        ambiente = payload.ambiente if payload.ambiente in AMBIENTES else "homologacion"
        existente = db_arca_config.obtener_arca_config(empresa)
        if existente:
            db_arca_config.actualizar_arca_config(
                empresa, cuit=payload.cuit, punto_venta=payload.punto_venta,
                ambiente=ambiente, alias=payload.alias,
            )
        else:
            db_arca_config.crear_arca_config(
                empresa=empresa, cuit=payload.cuit, punto_venta=payload.punto_venta,
                clave_path="", certificado_path="", ambiente=ambiente,
                alias=payload.alias,
            )
        return obtener(empresa)

    @router.post("/certificado")
    async def subir_certificado(archivo: UploadFile = File(...), empresa: str = ""):
        """Sube el `.crt`. **Se valida antes de escribirlo.**

        Y si ya hay una clave cargada, se chequea que sean pareja: cambiar una
        de las dos mitades es la forma habitual de romper el par sin darse
        cuenta.
        """
        contenido = await archivo.read()
        try:
            datos = arca_certificados.leer_certificado(contenido)
        except arca_certificados.ArchivoInvalido as e:
            raise HTTPException(422, f"El certificado {e}") from None

        empresa = _empresa_de(empresa or "", empresa_por_defecto)
        _, clave_path = _paths(_resolver(empresa))
        if _existe(clave_path):
            with open(clave_path, "rb") as f:
                if not arca_certificados.son_pareja(contenido, f.read()):
                    raise HTTPException(
                        422,
                        "Este certificado no es pareja de la clave privada que ya "
                        "está cargada. Subí las dos mitades del mismo par.",
                    )

        os.makedirs(_certs_dir(), exist_ok=True)
        destino = os.path.join(_certs_dir(), NOMBRE_CERTIFICADO)
        with open(destino, "wb") as f:
            f.write(contenido)
        _guardar_path(empresa, certificado_path=destino)
        return {**obtener(empresa), "vence": datos.vence.strftime("%d-%m-%Y"),
                "dias_para_vencer": datos.dias_para_vencer}

    @router.post("/clave")
    async def subir_clave(archivo: UploadFile = File(...), empresa: str = ""):
        """Sube el `.key`. Mismas dos validaciones, del otro lado."""
        contenido = await archivo.read()
        try:
            arca_certificados.leer_clave(contenido)
        except arca_certificados.ArchivoInvalido as e:
            raise HTTPException(422, f"La clave privada {e}") from None

        empresa = _empresa_de(empresa or "", empresa_por_defecto)
        cert_path, _ = _paths(_resolver(empresa))
        if _existe(cert_path):
            with open(cert_path, "rb") as f:
                if not arca_certificados.son_pareja(f.read(), contenido):
                    raise HTTPException(
                        422,
                        "Esta clave privada no es pareja del certificado que ya "
                        "está cargado. Subí las dos mitades del mismo par.",
                    )

        os.makedirs(_certs_dir(), exist_ok=True)
        destino = os.path.join(_certs_dir(), NOMBRE_CLAVE)
        with open(destino, "wb") as f:
            f.write(contenido)
        _guardar_path(empresa, clave_path=destino)
        return obtener(empresa)

    @router.delete("/credenciales")
    def borrar_credenciales(empresa: str = ""):
        """Sacar el par tiene que ser posible desde la pantalla.

        Se borran los dos archivos **y** se vacían los paths de la fila: dejar
        el path apuntando a un archivo que ya no está haría que
        `resolve_cert_paths` caiga al nombre estándar y **reviva un certificado
        viejo** que quedó en el volumen.
        """
        cfg = _resolver(empresa)
        if not cfg:
            raise HTTPException(404, "Esta instancia no tiene configuración de ARCA.")
        cert_path, clave_path = _paths(cfg)
        for path in (cert_path, clave_path):
            if _existe(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        db_arca_config.actualizar_arca_config(
            cfg["empresa"], certificado_path="", clave_path="",
        )
        return obtener(cfg["empresa"])

    @router.get("/estado")
    def estado(empresa: str = ""):
        """Si la instancia puede facturar, y hasta cuándo.

        🔑 `dias_para_vencer` es el dato que evita la falla silenciosa: los
        certificados de ARCA duran dos años y el día que vencen la facturación
        deja de andar sin que nadie haya tocado nada.
        """
        cfg = _resolver(empresa)
        if not cfg:
            return {"configurado": False, "ambiente": "", "cuit": "",
                    "tiene_certificado": False, "tiene_clave": False}
        cert_path, clave_path = _paths(cfg)
        tiene_cert, tiene_clave = _existe(cert_path), _existe(clave_path)
        salida = {
            "configurado":      tiene_cert and tiene_clave,
            "ambiente":         cfg.get("ambiente", ""),
            "cuit":             cfg.get("cuit", ""),
            "tiene_certificado": tiene_cert,
            "tiene_clave":       tiene_clave,
        }
        if tiene_cert:
            try:
                datos = arca_certificados.leer_certificado_de_archivo(cert_path)
            except arca_certificados.ArchivoInvalido as e:
                salida["error_certificado"] = str(e)
            else:
                salida.update(
                    vence=datos.vence.strftime("%d-%m-%Y"),
                    dias_para_vencer=datos.dias_para_vencer,
                    vencido=datos.vencido,
                    sujeto=datos.sujeto,
                )
        return salida

    @router.get("/certificado-info")
    def certificado_info(empresa: str = ""):
        """La forma vieja del dato, que el frontend de Contalibra ya consume.

        Se mantiene para no romperlo mientras las pantallas se normalizan;
        `GET /estado` es la que trae todo junto y la que usan las nuevas.
        """
        cfg = _resolver(empresa)
        if not cfg:
            raise HTTPException(404, "Sin configuracion")
        cert_path, _ = _paths(cfg)
        return arca_wsaa.info_certificado(cert_path)

    @router.post("/probar")
    async def probar(empresa: str = ""):
        """Autentica de verdad contra WSAA. Es el único chequeo que dice que el
        certificado además está **habilitado para el servicio** en ARCA.

        Leer los archivos no alcanza: un par perfecto al que nadie le dio de
        alta la relación con `wsfe` en el Administrador de Relaciones pasa toda
        validación local y lo rechaza ARCA.
        """
        cfg = _resolver(empresa)
        if not cfg:
            raise HTTPException(400, "ARCA no está configurado.")
        cert_path, clave_path = _paths(cfg)

        errores = arca_certificados.revisar_par_de_archivos(cert_path, clave_path)
        if errores:
            raise HTTPException(400, " | ".join(errores))

        ambiente = cfg.get("ambiente", "homologacion")
        try:
            await arca_wsaa.autenticar(cert_path, clave_path, ambiente)
        except Exception as e:
            # El texto de ARCA va tal cual: es el que dice si el problema es el
            # certificado, la relación con el servicio o la hora del servidor.
            raise HTTPException(502, f"ARCA rechazó la autenticación: {e}") from None

        info = arca_wsaa.info_certificado(cert_path)
        return {"ok": True, "ambiente": ambiente, "cuit": cfg.get("cuit", ""),
                "certificado": info}

    return router


def _empresa_de(empresa: str, por_defecto: str = "default") -> str:
    """La empresa sobre la que operar cuando el request no la nombró.

    La de la fila activa si hay una —para no crear una segunda fila al lado de
    la que la instancia ya venía usando— y el default del producto si no hay
    ninguna. Ver `empresa_por_defecto` en `build_arca_router`.
    """
    if empresa.strip():
        return empresa.strip()
    activas = db_arca_config.obtener_todas_arca_configs()
    return activas[0]["empresa"] if activas else por_defecto
