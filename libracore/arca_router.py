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
#:
#: 🔑 **Salen del mapa de `config_manager`, no de un literal.** Estaban escritos
#: dos veces —acá y adentro del rescate— y las dos copias tienen que decir lo
#: mismo o el rescate busca un archivo que el upload nunca escribió. Con dos
#: definiciones, cambiar una deja la otra en silencio.
NOMBRE_CERTIFICADO, NOMBRE_CLAVE = config_manager.ARCHIVOS_POR_AMBIENTE["produccion"]

AMBIENTES = ("homologacion", "produccion")


def _nombres_de(ambiente: str) -> tuple[str, str]:
    """Con qué nombre se guarda el par de este ambiente."""
    return config_manager.ARCHIVOS_POR_AMBIENTE[ambiente]


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


def _ambiente_de(cfg: dict | None, pedido: str = "") -> str:
    """Sobre qué ambiente opera esta llamada.

    Sin `ambiente` explícito, el **selector** de la config: es el par que la
    instancia está usando y el que la pantalla muestra por defecto.
    """
    valor = (pedido or (cfg or {}).get("ambiente") or "").strip().lower()
    return valor if valor in AMBIENTES else "homologacion"


def _paths(cfg: dict | None, ambiente: str = "") -> tuple[str, str]:
    """El par en disco del ambiente pedido.

    🔴 **El `ambiente` viaja hasta el rescate**, y no es un detalle: sin él,
    `resolve_cert_paths` cae al nombre de producción y repone las credenciales
    reales que `paths_de` acababa de negar. Medido el 2026-09-01.
    """
    cfg = cfg or {}
    amb = _ambiente_de(cfg, ambiente)
    return config_manager.resolve_cert_paths(
        *db_arca_config.paths_de(cfg, amb), ambiente=amb
    )


def _existe(path: str) -> bool:
    return bool(path) and os.path.exists(path)


def _estado_del_par(cfg: dict | None, ambiente: str) -> dict:
    """Qué hay cargado para un ambiente, con el vencimiento si se puede leer.

    Se devuelve por ambiente y no una vez, porque el momento que esta pantalla
    tiene que cubrir es justamente **el de la transición**: el operador está
    probando contra homologación y necesita ver, sin cambiar el selector, que el
    par de producción ya está y hasta cuándo dura.

    🔑 Se informa `completo` y no sólo las dos mitades: un par a medias no
    factura, y "certificado cargado ✓" al lado de "clave cargada ✗" se lee como
    "falta poco" cuando en realidad no funciona nada.
    """
    cert_path, clave_path = _paths(cfg, ambiente)
    tiene_cert, tiene_clave = _existe(cert_path), _existe(clave_path)
    salida = {
        "ambiente":          ambiente,
        "tiene_certificado": tiene_cert,
        "tiene_clave":       tiene_clave,
        "completo":          tiene_cert and tiene_clave,
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


def _guardar_path(empresa: str, ambiente: str, *,
                  certificado_path=None, clave_path=None) -> dict:
    """Escribe el path en la columna del ambiente, creando la fila si no está.

    🔑 A qué columna va lo decide `paths_de`/`COLUMNAS_POR_AMBIENTE`, no este
    archivo: el par de producción vive en las columnas sin sufijo y esa
    asimetría tiene un solo dueño.
    """
    campo_cert, campo_clave = db_arca_config.COLUMNAS_POR_AMBIENTE[ambiente]
    valores = {}
    if certificado_path is not None:
        valores[campo_cert] = certificado_path
    if clave_path is not None:
        valores[campo_clave] = clave_path

    if db_arca_config.obtener_arca_config(empresa):
        db_arca_config.actualizar_arca_config(empresa, **valores)
    else:
        # La fila nueva se crea vacía y después se le escribe el par: así el
        # alta no tiene que saber qué columna corresponde a qué ambiente.
        db_arca_config.crear_arca_config(
            empresa=empresa, cuit="", punto_venta=1,
            clave_path="", certificado_path="", ambiente=ambiente,
        )
        if valores:
            db_arca_config.actualizar_arca_config(empresa, **valores)
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
            # 🔑 El estado de LOS DOS pares, no sólo el del selector. La pantalla
            # tiene que poder decir "ya tenés cargado el de producción" mientras
            # el operador sube el de homologación: sin eso, mover la llave es un
            # salto a ciegas.
            "pares":            {a: _estado_del_par(cfg, a) for a in AMBIENTES},
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

    def _ambiente_del_pedido(cfg: dict | None, ambiente: str) -> str:
        """El ambiente al que va este upload, validado.

        🔴 **Un valor raro NO cae a producción.** El destino de un upload es un
        archivo que se sobrescribe: equivocar el ambiente acá pisa la credencial
        real del cliente. Ante algo que no reconocemos, 422 y no adivinar.
        """
        pedido = (ambiente or "").strip().lower()
        if pedido and pedido not in AMBIENTES:
            raise HTTPException(
                422,
                f"Ambiente desconocido: {ambiente!r}. Los válidos son "
                + " y ".join(AMBIENTES) + ".",
            )
        return _ambiente_de(cfg, pedido)

    @router.post("/certificado")
    async def subir_certificado(archivo: UploadFile = File(...), empresa: str = "",
                                ambiente: str = ""):
        """Sube el `.crt` **del ambiente indicado**. Se valida antes de escribirlo.

        Y si ya hay una clave cargada **para ese mismo ambiente**, se chequea que
        sean pareja: cambiar una de las dos mitades es la forma habitual de
        romper el par sin darse cuenta.

        🔴 Sin `ambiente`, el del selector. Cada ambiente escribe **su propio
        archivo**: hasta el 2026-09-01 los dos iban a `certificado.crt` y subir
        el de homologación pisaba el de producción.
        """
        contenido = await archivo.read()
        try:
            datos = arca_certificados.leer_certificado(contenido)
        except arca_certificados.ArchivoInvalido as e:
            raise HTTPException(422, f"El certificado {e}") from None

        empresa = _empresa_de(empresa or "", empresa_por_defecto)
        cfg = _resolver(empresa)
        amb = _ambiente_del_pedido(cfg, ambiente)
        _, clave_path = _paths(cfg, amb)
        if _existe(clave_path):
            with open(clave_path, "rb") as f:
                if not arca_certificados.son_pareja(contenido, f.read()):
                    raise HTTPException(
                        422,
                        "Este certificado no es pareja de la clave privada que ya "
                        f"está cargada para {amb}. Subí las dos mitades del mismo par.",
                    )

        os.makedirs(_certs_dir(), exist_ok=True)
        destino = os.path.join(_certs_dir(), _nombres_de(amb)[0])
        with open(destino, "wb") as f:
            f.write(contenido)
        _guardar_path(empresa, amb, certificado_path=destino)
        return {**obtener(empresa), "vence": datos.vence.strftime("%d-%m-%Y"),
                "dias_para_vencer": datos.dias_para_vencer}

    @router.post("/clave")
    async def subir_clave(archivo: UploadFile = File(...), empresa: str = "",
                          ambiente: str = ""):
        """Sube el `.key` del ambiente indicado. Mismas validaciones, del otro lado."""
        contenido = await archivo.read()
        try:
            arca_certificados.leer_clave(contenido)
        except arca_certificados.ArchivoInvalido as e:
            raise HTTPException(422, f"La clave privada {e}") from None

        empresa = _empresa_de(empresa or "", empresa_por_defecto)
        cfg = _resolver(empresa)
        amb = _ambiente_del_pedido(cfg, ambiente)
        cert_path, _ = _paths(cfg, amb)
        if _existe(cert_path):
            with open(cert_path, "rb") as f:
                if not arca_certificados.son_pareja(f.read(), contenido):
                    raise HTTPException(
                        422,
                        "Esta clave privada no es pareja del certificado que ya "
                        f"está cargado para {amb}. Subí las dos mitades del mismo par.",
                    )

        os.makedirs(_certs_dir(), exist_ok=True)
        destino = os.path.join(_certs_dir(), _nombres_de(amb)[1])
        with open(destino, "wb") as f:
            f.write(contenido)
        _guardar_path(empresa, amb, clave_path=destino)
        return obtener(empresa)

    @router.delete("/credenciales")
    def borrar_credenciales(empresa: str = "", ambiente: str = ""):
        """Saca el par **de un ambiente**. Sin `ambiente`, el del selector.

        Se borran los dos archivos **y** se vacían los paths de la fila: dejar
        el path apuntando a un archivo que ya no está haría que
        `resolve_cert_paths` caiga al nombre estándar y **reviva un certificado
        viejo** que quedó en el volumen.

        🔑 Borra un ambiente y **no toca el otro**. Es lo que hace segura la
        prueba: terminado el acompañamiento, se saca el par de homologación y el
        de producción sigue donde estaba. Antes había un solo par y borrar era
        borrar todo.
        """
        cfg = _resolver(empresa)
        if not cfg:
            raise HTTPException(404, "Esta instancia no tiene configuración de ARCA.")
        amb = _ambiente_del_pedido(cfg, ambiente)
        cert_path, clave_path = _paths(cfg, amb)
        for path in (cert_path, clave_path):
            if _existe(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        campo_cert, campo_clave = db_arca_config.COLUMNAS_POR_AMBIENTE[amb]
        db_arca_config.actualizar_arca_config(
            cfg["empresa"], **{campo_cert: "", campo_clave: ""},
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
                    "tiene_certificado": False, "tiene_clave": False,
                    "pares": {a: {"ambiente": a, "tiene_certificado": False,
                                  "tiene_clave": False, "completo": False}
                              for a in AMBIENTES}}
        pares = {a: _estado_del_par(cfg, a) for a in AMBIENTES}
        # 🔑 El estado plano es el del par **del selector**, derivado del mismo
        # cálculo que los de `pares`. Repetirlo acá era tener el patrón escrito
        # dos veces: los dos bloques tienen que decir lo mismo, y con dos copias
        # arreglar uno deja el otro respondiendo lo viejo en silencio.
        propio = pares[_ambiente_de(cfg)]
        return {
            "configurado":      propio["completo"],
            "ambiente":         cfg.get("ambiente", ""),
            "cuit":             cfg.get("cuit", ""),
            "tiene_certificado": propio["tiene_certificado"],
            "tiene_clave":       propio["tiene_clave"],
            "pares":            pares,
            **{k: v for k, v in propio.items()
               if k in ("vence", "dias_para_vencer", "vencido", "sujeto",
                        "error_certificado")},
        }

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
