"""La pantalla de configuración de ARCA, igual en los productos que facturan.

Nace de tener el mismo formulario escrito de tres formas distintas: Contalibra
y Restolibra en `web/api/config.py` bajo `/api/config/arca`, Gestiolibra,
MedLibra, VentaLibra y LibraClub en `routers/billing.py` bajo `/config/arca`, y
[[libracargo]] con el suyo propio en `/api/arca`. Mismo criterio que
`config_router`: **el paquete arma el router, el producto lo monta con su
dependencia de rol**, porque el vocabulario de roles no es el mismo en los seis.

    app.include_router(build_arca_router(), dependencies=[Depends(require_admin)])

## Quién subió la clave privada

Este router no escribía **ningún** registro de quién cambiaba qué, y es la
pantalla donde se sube una clave privada. LibraCargo sí lo hacía, con su router
propio, y al normalizar contra éste lo perdió — lo que puso el hueco a la vista.

`al_cambiar(accion, detalle, usuario)` corre **después** de cada cambio, con el
usuario que devuelve la dependencia `usuario_actual` del producto. Las dos son
opcionales: los seis productos que ya lo montan siguen andando sin pasarlas, y
el que quiera auditoría la conecta.

    app.include_router(
        build_arca_router(
            usuario_actual=get_current_user,
            al_cambiar=lambda accion, detalle, usuario: ...,
        ),
        dependencies=[Depends(require_admin)],
    )

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

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, field_validator

from libracore import (
    arca_certificados,
    arca_credenciales,
    arca_pedidos,
    arca_servicios,
    arca_wsaa,
    arca_wsfe,
    config_manager,
)
from libracore.db import arca_config as db_arca_config
from libracore.db import arca_credenciales_servicio as db_servicio
from libracore.validacion import sin_booleanos

logger = logging.getLogger(__name__)

#: Lo que puede pasarle `al_cambiar` como `accion`. Está acá para que un
#: consumidor pueda mapearlas sin repetir literales, y para que agregar una sea
#: un cambio visible en vez de un string nuevo suelto en un endpoint.
#:
#: 🔑 **`probar` no está**, y no es un olvido: autentica contra ARCA y **no
#: cambia nada**. Un log de auditoría que registre lecturas se llena de ruido y
#: esconde las cuatro líneas que importan.
#:
#: `pedido` y `descartar_pedido` son el pedido de certificado (ADR-036): se genera una clave
#: nueva en el servidor, o se tira la que estaba esperando su `.crt`. Cuando el `.crt` llega y la
#: clave pendiente pasa a vigente, quedan los asientos de siempre —`certificado` y `clave`—, con
#: `desde_pedido` en el detalle.
ACCIONES = ("configurar", "certificado", "clave", "borrar", "pedido", "descartar_pedido")


def _sin_identidad() -> None:
    """La dependencia de usuario por omisión: no hay quién, y está bien.

    Un producto que no pasa `usuario_actual` recibe `None` en el hook. No se
    exige junto con `al_cambiar` a propósito: un backoffice o un script pueden
    querer el registro del cambio aunque no haya sesión de por medio, y
    obligarlos a inventar un usuario sería peor que un `None` explícito.
    """
    return None

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


class CbuFce(BaseModel):
    """Una cuenta donde se puede cobrar una FCE: su CBU, su alias bancario y un nombre para elegirla."""

    cbu: str
    #: Opcional. Se guarda en minúsculas, como lo normaliza la banca.
    alias: str = ""
    etiqueta: str = ""

    @field_validator("cbu")
    @classmethod
    def _cbu(cls, v):
        v = db_arca_config.normalizar_cbu(v)
        if not (len(v) == 22 and v.isdigit()):
            raise ValueError("El CBU tiene 22 dígitos.")
        return v

    @field_validator("alias")
    @classmethod
    def _alias(cls, v):
        v = db_arca_config.normalizar_alias(v)
        if v and not db_arca_config.ALIAS_VALIDO.match(v):
            raise ValueError(
                "El alias tiene de 6 a 20 caracteres: letras sin tildes ni ñ, números, punto o guion.")
        return v

    @field_validator("etiqueta")
    @classmethod
    def _etiqueta(cls, v):
        v = v.strip()
        if len(v) > 60:
            raise ValueError("La etiqueta tiene hasta 60 caracteres.")
        return v


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
    #: FCE MiPyME: el CBU del emisor (22 dígitos) y la modalidad de transmisión.
    #: 🔑 `None` es «no lo toqués» y `""` es «borralo»: una pantalla que no conoce
    #: la FCE no manda el campo, y no tiene que dejar el CBU en blanco.
    #:
    #: `fce_cbu` es el CBU **predeterminado**. `fce_cbus` (ADR-040) es la lista de
    #: cuentas entre las que se elige al emitir; `None` es «no la toqués», así que
    #: una pantalla que sólo conoce `fce_cbu` sigue andando (ver `_cbus_a_guardar`).
    fce_cbu: str | None = None
    fce_transmision: str | None = None
    fce_cbus: list[CbuFce] | None = None

    _no_son_booleanos = sin_booleanos("punto_venta")

    @field_validator("fce_cbu")
    @classmethod
    def _cbu(cls, v):
        v = None if v is None else v.strip()
        if v and not (len(v) == 22 and v.isdigit()):
            raise ValueError("El CBU tiene 22 dígitos.")
        return v

    @field_validator("fce_transmision")
    @classmethod
    def _transmision(cls, v):
        v = None if v is None else v.strip().upper()
        if v and v not in ("SCA", "ADC"):
            raise ValueError("La modalidad de transmisión es SCA o ADC.")
        return v


class PedidoPayload(BaseModel):
    """Los datos del pedido de certificado. `empresa` y `ambiente` van en la query, como en
    todas las rutas que escriben un par; acá sólo lo que va en el sujeto del `.csr`."""

    cuit: str
    razon_social: str
    alias: str
    #: 🔴 Reemplazar un pedido pendiente destruye su clave: si el `.csr` anterior ya está en
    #: ARCA, el `.crt` que devuelva queda sin pareja. Sin esta confirmación explícita es 409.
    reemplazar: bool = False


def _cbus_a_guardar(payload: ArcaPayload, existente: dict | None) -> tuple[str | None, list[dict] | None]:
    """`(fce_cbu, fce_cbus)` a guardar, con `None` en lo que no hay que tocar. 422 si no cierra.

    🔑 **Lista y predeterminado nunca quedan desalineados.**

    - Viene `fce_cbus` (pantalla nueva): no puede repetir CBU ni alias; vacía borra
      también el predeterminado; si no, `fce_cbu` tiene que estar en la lista, y si
      no vino se usa el primero.
    - No viene (pantalla vieja): manda `fce_cbu` como siempre. Un CBU nuevo se
      **agrega** a la lista (sin alias ni etiqueta) y queda de predeterminado; `""`
      es «borralo» y vacía todo, porque esa pantalla no sabe que hay más cuentas.
      Los alias y etiquetas de las que ya estaban no se tocan.
    """
    if payload.fce_cbus is None:
        if payload.fce_cbu is None:
            return None, None
        if not payload.fce_cbu:
            return "", []
        lista = db_arca_config.cbus_fce(existente)
        if payload.fce_cbu not in [f["cbu"] for f in lista]:
            lista.append({"cbu": payload.fce_cbu, "alias": "", "etiqueta": ""})
        return payload.fce_cbu, lista

    lista = [f.model_dump() for f in payload.fce_cbus]
    cbus = [f["cbu"] for f in lista]
    if len(set(cbus)) != len(cbus):
        raise HTTPException(422, "Hay un CBU repetido en la lista.")
    aliases = [f["alias"] for f in lista if f["alias"]]
    if len(set(aliases)) != len(aliases):
        raise HTTPException(422, "Hay un alias repetido en la lista.")
    if not lista:
        return "", []
    if not payload.fce_cbu:
        return cbus[0], lista
    if payload.fce_cbu not in cbus:
        raise HTTPException(422, "El CBU predeterminado tiene que ser uno de los de la lista.")
    return payload.fce_cbu, lista


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
    return db_arca_config.config_del_emisor()


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

    Envuelve a `arca_credenciales.paths_en_disco` sólo para aplicar el default
    de la pantalla: acá un ambiente vacío o raro cae en `homologacion`, que es
    el menos peligroso de los dos para una pantalla de configuración.
    """
    cfg = cfg or {}
    return arca_credenciales.paths_en_disco(cfg, _ambiente_de(cfg, ambiente))


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
    return _con_pedido(
        _estado_de_archivos(ambiente, cert_path, clave_path),
        arca_servicios.SERVICIO_FACTURACION, (cfg or {}).get("empresa", ""),
    )


def _estado_de_archivos(ambiente: str, cert_path: str, clave_path: str, *,
                        con_cuit: bool = False) -> dict:
    """El estado de un par a partir de sus dos rutas. Lo comparten la facturación
    y los demás servicios: **una sola cuenta**, para que las dos pantallas digan lo mismo.

    `con_cuit` agrega el CUIT que trae el sujeto del certificado
    (`cuit_certificado`). Es del dato nuevo (ADR-032) y por eso es opcional: las
    respuestas que ya existían no ganan una clave que sus consumidores no esperan.
    """
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
            if con_cuit:
                salida["cuit_certificado"] = datos.cuit
    return salida


def _con_pedido(par: dict, servicio: str, empresa: str) -> dict:
    """Agrega al estado de un par el pedido de certificado que espera su `.crt`, si hay uno.

    🔑 **La clave `pedido` aparece sólo cuando hay un pedido pendiente.** Las respuestas que ya
    existían no ganan una clave que sus consumidores no esperan, y ninguna lleva la clave privada:
    el pedido se describe con su alias, su CUIT, su razón social y su fecha (ADR-036).
    """
    pedido = arca_pedidos.leer(_certs_dir(), servicio, par["ambiente"], empresa)
    if pedido:
        par["pedido"] = pedido.como_dict()
    return par


def _certificado_valido(contenido: bytes):
    """Los datos del `.crt` subido, o 422 con la causa. Antes de tocar el disco."""
    try:
        return arca_certificados.leer_certificado(contenido)
    except arca_certificados.ArchivoInvalido as e:
        raise HTTPException(422, f"El certificado {e}") from None


def _clave_valida(contenido: bytes) -> None:
    try:
        arca_certificados.leer_clave(contenido)
    except arca_certificados.ArchivoInvalido as e:
        raise HTTPException(422, f"La clave privada {e}") from None


def _exigir_pareja_con_la_clave_cargada(contenido: bytes, clave_path: str, ambiente: str) -> None:
    """Si ya hay clave para ese ambiente, el certificado nuevo tiene que ser su pareja."""
    if _existe(clave_path):
        with open(clave_path, "rb") as f:
            if not arca_certificados.son_pareja(contenido, f.read()):
                raise HTTPException(
                    422,
                    "Este certificado no es pareja de la clave privada que ya "
                    f"está cargada para {ambiente}. Subí las dos mitades del mismo par.",
                )


def _exigir_pareja_con_el_certificado_cargado(contenido: bytes, cert_path: str, ambiente: str) -> None:
    """Si ya hay certificado para ese ambiente, la clave nueva tiene que ser su pareja."""
    if _existe(cert_path):
        with open(cert_path, "rb") as f:
            if not arca_certificados.son_pareja(f.read(), contenido):
                raise HTTPException(
                    422,
                    "Esta clave privada no es pareja del certificado que ya "
                    f"está cargado para {ambiente}. Subí las dos mitades del mismo par.",
                )


def _clave_pendiente_que_empareja(contenido: bytes, servicio: str, ambiente: str, empresa: str,
                                  clave_path: str) -> tuple[arca_pedidos.Pedido, bytes] | None:
    """La clave pendiente a instalar si este `.crt` es la respuesta a su pedido; si no, `None`.

    - Sin pedido pendiente: `None`, y el upload sigue como siempre.
    - Con pedido, y el `.crt` empareja con **su** clave: `(pedido, clave)`. Es la promoción.
    - Con pedido, y el `.crt` empareja con la clave **vigente** (un `.crt` de la clave de siempre
      que se sube mientras hay un pedido a medias): `None`, sigue como siempre y el pedido espera.
    - Con pedido, y no empareja con ninguna: 422. Dejar pasar un certificado que no es de ninguna
      de las dos claves es el defecto que `son_pareja` existe para evitar.
    """
    carpeta = _certs_dir()
    pedido = arca_pedidos.leer(carpeta, servicio, ambiente, empresa)
    if pedido is None:
        return None
    pendiente = arca_pedidos.clave(carpeta, servicio, ambiente, empresa)

    def _es_pareja(clave: bytes | None) -> bool:
        try:
            return clave is not None and arca_certificados.son_pareja(contenido, clave)
        except arca_certificados.ArchivoInvalido:
            return False

    if _es_pareja(pendiente):
        return pedido, pendiente
    vigente = None
    if _existe(clave_path):
        with open(clave_path, "rb") as f:
            vigente = f.read()
    if _es_pareja(vigente):
        return None
    raise HTTPException(
        422,
        f"Este certificado no corresponde al pedido pendiente para {ambiente} (alias "
        f"{pedido.alias}, del {pedido.como_dict()['creado']})"
        + (" ni a la clave que ya está cargada" if vigente is not None else "")
        + ". Tiene que ser el .crt que ARCA devolvió para ese .csr.",
    )


def _instalar_par(nombres: tuple[str, str], certificado: bytes, clave: bytes) -> tuple[str, str]:
    """Escribe el par en `CERTS_DIR` con los nombres del ambiente: `(ruta del .crt, ruta del .key)`.

    La clave con `escribir_clave_privada` (0600), como en toda subida. Es lo que hace la promoción
    de un pedido: la clave sale de su archivo pendiente y entra al de la clave vigente, sin pasar por
    ninguna respuesta.
    """
    os.makedirs(_certs_dir(), exist_ok=True)
    destino_clave = os.path.join(_certs_dir(), nombres[1])
    destino_cert = os.path.join(_certs_dir(), nombres[0])
    arca_certificados.escribir_clave_privada(destino_clave, clave)
    with open(destino_cert, "wb") as f:
        f.write(certificado)
    return destino_cert, destino_clave


def _nombres_de_servicio(servicio: str, ambiente: str, empresa: str) -> tuple[str, str]:
    """Con qué nombre se guarda en disco el par de un servicio que no es la facturación.

    🔑 Lleva el servicio, el ambiente **y una huella de la empresa**: dos servicios
    o dos ambientes nunca comparten archivo (el defecto que `ARCHIVOS_POR_AMBIENTE`
    corrigió para la facturación), y la huella evita que dos empresas del mismo
    servicio se pisen sin meter el nombre de la empresa, que puede traer cualquier
    carácter, en una ruta del disco.
    """
    huella = hashlib.sha1(empresa.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    base = f"{re.sub(r'[^a-z0-9]+', '-', servicio.lower()).strip('-')}-{ambiente}-{huella}"
    return f"{base}.crt", f"{base}.key"


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
    usuario_actual: Callable[..., Any] | None = None,
    al_cambiar: Callable[[str, dict, Any], None] | None = None,
    servicios: tuple[str, ...] = ("wsfe",),
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

    ## `al_cambiar`, y por qué es *best-effort*

    Se llama con una de las `ACCIONES`, un `detalle` y el usuario. **Lo
    que levante no tumba la request**, igual que el `al_emitir` de
    `facturas_router` y por el mismo motivo: para cuando corre, el archivo ya
    está escrito en `CERTS_DIR` y la fila ya está guardada, así que un error no
    lo desharía — dejaría al operador creyendo que la subida falló mientras el
    certificado está puesto. Se registra en el log de la app con `exception`.

    🔴 **Eso lo hace distinto de una auditoría transaccional**, que es lo que un
    producto con ORM escribe para sus propias tablas: ahí el asiento y el cambio
    entran o no entran juntos. Acá no se puede, porque el cambio es un archivo
    en disco. Quien conecte esto tiene que saber que el peor caso es un cambio
    aplicado sin su registro, no un registro sin cambio.

    🔑 **El `detalle` no lleva NUNCA el contenido del par.** Lleva de qué
    empresa y de qué ambiente es, y —para el certificado— lo que ya es público
    de él: sujeto, vencimiento y número de serie. Un log de auditoría con la
    clave privada adentro es peor que no tener log. Hay un test que lo fija.

    ## `servicios`: más de un servicio de ARCA en la misma pantalla (ADR-032)

    Por omisión sólo la facturación (`wsfe`), y el router es **idéntico al de
    siempre**: ninguna ruta nueva salvo `GET /servicios`, que lista un solo
    servicio. Un producto que además usa otro servicio de ARCA —LibraCargo, con el
    CTG y la Carta de Porte (`wscpe`)— lo declara una vez al montar:

        build_arca_router(servicios=("wsfe", "wscpe"))

    y obtiene, por cada servicio **que no es la facturación**:

    - `GET  {prefix}/servicios/{servicio}/estado`: el estado de los dos ambientes.
    - `POST {prefix}/servicios/{servicio}/certificado` y `/clave`: suben una mitad
      del par de **un ambiente**, con las mismas validaciones que la facturación.
    - `DELETE {prefix}/servicios/{servicio}/credenciales`: saca el par de un ambiente.
    - `POST {prefix}/servicios/{servicio}/probar`: se autentica contra WSAA **para
      ese servicio** y dice, en castellano, por qué ARCA lo rechaza.

    La facturación sigue en sus rutas de siempre y en `arca_config`: lo que cambia
    es que `GET /servicios` la lista junto a las demás, para que la pantalla pinte
    un bloque por servicio sin casos especiales.

    🔑 **Para estos servicios el `ambiente` es obligatorio** en subir, quitar y
    probar. La facturación puede caer al selector de la instancia porque lo tiene;
    un servicio sin selector que adivinara el ambiente subiría un certificado de
    prueba sobre el real, o al revés.

    🔑 Sin `empresa`, es `empresa_por_defecto` y **no** «la primera fila de
    facturación»: un servicio que se carga antes de tener fila de `arca_config`
    tiene que quedar en el mismo lugar cuando la fila aparezca.

    ## El pedido de certificado: la clave nace en el servidor (ADR-036)

    Para una empresa nueva, la clave y el `.csr` ya no se generan afuera para subir la clave
    como archivo. Cada par —la facturación en `{prefix}/pedido…` y cada servicio en
    `{prefix}/servicios/{servicio}/pedido…`— tiene:

    - `POST …/pedido?ambiente=` (cuerpo: `cuit`, `razon_social`, `alias`, `reemplazar`): genera la
      clave **pendiente** y devuelve el `.csr` (PEM). `ambiente` es obligatorio. Si ya hay un
      pedido pendiente, 409 salvo `reemplazar: true`.
    - `GET …/pedido?ambiente=`: si hay pedido pendiente, y de qué alias, CUIT y fecha.
    - `GET …/pedido.csr?ambiente=`: el `.csr` como descarga.
    - `DELETE …/pedido?ambiente=`: descarta el pedido y su clave.

    🔑 **La clave pendiente vive aparte de la vigente** y no la pisa: ver `arca_pedidos`. Cuando se
    sube el `.crt` por `…/certificado` y empareja con la clave pendiente, el par se instala y el
    pedido desaparece; si empareja con la vigente, sigue como siempre; si no empareja con ninguna,
    422. **La clave privada no sale en ninguna respuesta, ni va al `detalle` de `al_cambiar`.**
    """
    for _id in servicios:
        try:
            arca_servicios.servicio(_id)
        except arca_servicios.ServicioDesconocido:
            raise ValueError(
                f"Servicio de ARCA desconocido: {_id!r}. Los que conoce el motor son "
                + ", ".join(arca_servicios.CATALOGO) + ".") from None
    #: Los que tienen rutas propias: todos menos la facturación, que no cambia.
    extras = tuple(dict.fromkeys(i for i in servicios if i != arca_servicios.SERVICIO_FACTURACION))
    router = APIRouter(prefix=prefix, tags=["arca"])
    identidad = usuario_actual or _sin_identidad

    def _avisar(accion: str, detalle: dict, usuario: Any) -> None:
        if al_cambiar is None:
            return
        try:
            al_cambiar(accion, detalle, usuario)
        except Exception:
            logger.exception(
                "El hook de auditoría de ARCA falló para %r sobre %r. El cambio "
                "SÍ se aplicó; lo que quedó sin registrar es quién lo hizo.",
                accion, detalle.get("empresa", ""),
            )

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
            "fce_cbu":          cfg.get("fce_cbu", "") or "",
            "fce_cbus":         db_arca_config.cbus_fce(cfg),
            "fce_transmision":  cfg.get("fce_transmision", "") or "",
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
    def guardar(payload: ArcaPayload, usuario: Any = Depends(identidad)):
        empresa = payload.empresa.strip() or _empresa_de("", empresa_por_defecto)
        ambiente = payload.ambiente if payload.ambiente in AMBIENTES else "homologacion"
        existente = db_arca_config.obtener_arca_config(empresa)
        # Antes de escribir nada: un 422 de la lista no puede dejar la fila a medias.
        fce_cbu, fce_cbus = _cbus_a_guardar(payload, existente)
        if existente:
            db_arca_config.actualizar_arca_config(
                empresa, cuit=payload.cuit, punto_venta=payload.punto_venta,
                ambiente=ambiente, alias=payload.alias,
                fce_cbu=fce_cbu, fce_transmision=payload.fce_transmision, fce_cbus=fce_cbus,
            )
        else:
            db_arca_config.crear_arca_config(
                empresa=empresa, cuit=payload.cuit, punto_venta=payload.punto_venta,
                clave_path="", certificado_path="", ambiente=ambiente,
                alias=payload.alias,
            )
            if fce_cbu or fce_cbus or payload.fce_transmision:
                db_arca_config.actualizar_arca_config(
                    empresa, fce_cbu=fce_cbu, fce_transmision=payload.fce_transmision,
                    fce_cbus=fce_cbus)
        _avisar("configurar", {
            "empresa": empresa, "cuit": payload.cuit,
            "punto_venta": payload.punto_venta, "ambiente": ambiente,
            "alias": payload.alias,
        }, usuario)
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

    def _ambiente_exigido(ambiente: str) -> str:
        """El ambiente, **obligatorio**: sin selector que adivinar, o con uno que no se debe adivinar."""
        pedido = (ambiente or "").strip().lower()
        if pedido not in AMBIENTES:
            raise HTTPException(
                422,
                "Indicá el ambiente: " + " o ".join(AMBIENTES) + "."
                + (f" Llegó {ambiente!r}." if pedido else ""),
            )
        return pedido

    # 🔴 Subir el certificado, la clave y `probar` son `def` y no `async def`:
    # uvicorn corre con UN solo proceso, y las tres van a la base y al disco
    # —`probar` además firma el TRA con `openssl` por subproceso—. Como
    # corrutinas frenaban el loop entero mientras duraban. Como `def` corren
    # en el threadpool: el archivo subido se lee de su `SpooledTemporaryFile`
    # sin `await`, y la autenticación va con `asyncio.run` en un loop propio
    # del hilo.

    @router.post("/certificado")
    def subir_certificado(archivo: UploadFile = File(...), empresa: str = "",
                                ambiente: str = "",
                                usuario: Any = Depends(identidad)):
        """Sube el `.crt` **del ambiente indicado**. Se valida antes de escribirlo.

        Y si ya hay una clave cargada **para ese mismo ambiente**, se chequea que
        sean pareja: cambiar una de las dos mitades es la forma habitual de
        romper el par sin darse cuenta.

        🔴 Sin `ambiente`, el del selector. Cada ambiente escribe **su propio
        archivo**: hasta el 2026-09-01 los dos iban a `certificado.crt` y subir
        el de homologación pisaba el de producción.
        """
        contenido = archivo.file.read()
        datos = _certificado_valido(contenido)

        empresa = _empresa_de(empresa or "", empresa_por_defecto)
        cfg = _resolver(empresa)
        amb = _ambiente_del_pedido(cfg, ambiente)
        _, clave_path = _paths(cfg, amb)
        # 🔑 Si este `.crt` es la respuesta a un pedido pendiente, la clave pendiente pasa a
        # vigente junto con él (ADR-036). Si hay un pedido y no empareja con nada, es 422.
        promovido = _clave_pendiente_que_empareja(
            contenido, arca_servicios.SERVICIO_FACTURACION, amb, empresa, clave_path)
        detalle_extra = {}
        if promovido:
            pedido, clave_nueva = promovido
            destino_cert, destino_clave = _instalar_par(_nombres_de(amb), contenido, clave_nueva)
            _guardar_path(empresa, amb, certificado_path=destino_cert, clave_path=destino_clave)
            arca_pedidos.descartar(_certs_dir(), arca_servicios.SERVICIO_FACTURACION, amb, empresa)
            detalle_extra = {"desde_pedido": True, "alias": pedido.alias}
        else:
            _exigir_pareja_con_la_clave_cargada(contenido, clave_path, amb)

            os.makedirs(_certs_dir(), exist_ok=True)
            destino = os.path.join(_certs_dir(), _nombres_de(amb)[0])
            with open(destino, "wb") as f:
                f.write(contenido)
            _guardar_path(empresa, amb, certificado_path=destino)
        # 🔑 Los datos del certificado y no el certificado: son los que dicen
        # **cuál** se subió —el modo de fallar que este registro cubre es
        # "alguien lo cambió y nadie sabe por cuál"— y ninguno es secreto.
        _avisar("certificado", {
            "empresa": empresa, "ambiente": amb, "archivo": archivo.filename or "",
            "sujeto": datos.sujeto, "numero_de_serie": datos.numero_de_serie,
            "vence": datos.vence.strftime("%d-%m-%Y"), **detalle_extra,
        }, usuario)
        if promovido:
            # La clave también cambió: el registro de «quién cambió la clave» no se lo pierde.
            _avisar("clave", {"empresa": empresa, "ambiente": amb, "desde_pedido": True}, usuario)
        return {**obtener(empresa), "vence": datos.vence.strftime("%d-%m-%Y"),
                "dias_para_vencer": datos.dias_para_vencer}

    @router.post("/clave")
    def subir_clave(archivo: UploadFile = File(...), empresa: str = "",
                          ambiente: str = "", usuario: Any = Depends(identidad)):
        """Sube el `.key` del ambiente indicado. Mismas validaciones, del otro lado."""
        contenido = archivo.file.read()
        _clave_valida(contenido)

        empresa = _empresa_de(empresa or "", empresa_por_defecto)
        cfg = _resolver(empresa)
        amb = _ambiente_del_pedido(cfg, ambiente)
        cert_path, _ = _paths(cfg, amb)
        _exigir_pareja_con_el_certificado_cargado(contenido, cert_path, amb)

        os.makedirs(_certs_dir(), exist_ok=True)
        destino = os.path.join(_certs_dir(), _nombres_de(amb)[1])
        # 🔴 0600 y no `open(..., "wb")`: ver `escribir_clave_privada`.
        arca_certificados.escribir_clave_privada(destino, contenido)
        _guardar_path(empresa, amb, clave_path=destino)
        # De la clave no va NADA más que de cuál ambiente es. No hay un dato
        # público equivalente al sujeto del certificado, y el nombre del archivo
        # que subieron no dice nada que valga el riesgo de acostumbrarse a
        # copiar campos desde acá.
        _avisar("clave", {"empresa": empresa, "ambiente": amb}, usuario)
        return obtener(empresa)

    # ── El pedido de certificado (ADR-036) ──────────────────────────────────
    #
    # Las cuatro operaciones son iguales para la facturación y para cada servicio: sólo
    # cambia de dónde salen la empresa y el servicio. Las funciones de abajo son lo
    # compartido, para que las dos familias de rutas no diverjan.

    def _generar_pedido(servicio: str, empresa: str, ambiente: str,
                        datos: PedidoPayload, usuario: Any) -> arca_pedidos.Pedido:
        """Valida, genera la clave **pendiente** y la guarda. No toca la vigente."""
        try:
            cuit, razon_social, alias = arca_certificados.datos_del_pedido(
                datos.cuit, datos.razon_social, datos.alias)
        except arca_certificados.PedidoInvalido as e:
            raise HTTPException(422, str(e)) from None
        if not arca_wsfe.cuit_con_verificador_valido(cuit):
            raise HTTPException(
                422, "El CUIT no es válido: el dígito verificador no cierra. Revisá que esté bien escrito.")

        carpeta = _certs_dir()
        previo = arca_pedidos.leer(carpeta, servicio, ambiente, empresa)
        if previo and not datos.reemplazar:
            raise HTTPException(
                409,
                f"Ya hay un pedido pendiente para {ambiente} (alias {previo.alias}, del "
                f"{previo.como_dict()['creado']}). Si lo reemplazás se pierde su clave: el "
                "certificado que ARCA devuelva para ese pedido ya no va a servir. "
                "Confirmá el reemplazo o usá el pedido que ya está.")

        generado = arca_certificados.generar_pedido(cuit, razon_social, alias)
        pedido = arca_pedidos.guardar(carpeta, servicio, ambiente, empresa, generado)
        # 🔑 Sujeto, alias y CUIT: lo que dice **qué** se pidió. La clave no está en el
        # `PedidoDeCertificado` que se imprime, y acá tampoco.
        _avisar("pedido", {
            "empresa": empresa, "servicio": servicio, "ambiente": ambiente,
            "alias": pedido.alias, "cuit": pedido.cuit, "sujeto": pedido.sujeto,
            "reemplazo": previo is not None,
        }, usuario)
        return pedido

    def _descargar_csr(servicio: str, empresa: str, ambiente: str) -> Response:
        pedido = arca_pedidos.leer(_certs_dir(), servicio, ambiente, empresa)
        if pedido is None:
            raise HTTPException(404, f"No hay un pedido de certificado pendiente para {ambiente}.")
        return Response(
            pedido.csr, media_type="application/pkcs10",
            headers={"Content-Disposition": f'attachment; filename="{pedido.alias}.csr"'},
        )

    def _descartar_pedido(servicio: str, empresa: str, ambiente: str, usuario: Any) -> dict:
        previo = arca_pedidos.leer(_certs_dir(), servicio, ambiente, empresa)
        if not arca_pedidos.descartar(_certs_dir(), servicio, ambiente, empresa):
            raise HTTPException(404, f"No hay un pedido de certificado pendiente para {ambiente}.")
        _avisar("descartar_pedido", {
            "empresa": empresa, "servicio": servicio, "ambiente": ambiente,
            "alias": previo.alias if previo else "",
        }, usuario)
        return {"pendiente": False, "servicio": servicio, "ambiente": ambiente}

    def _estado_del_pedido(servicio: str, empresa: str, ambiente: str) -> dict:
        pedido = arca_pedidos.leer(_certs_dir(), servicio, ambiente, empresa)
        return pedido.como_dict() if pedido else {
            "pendiente": False, "servicio": servicio, "ambiente": ambiente}

    @router.post("/pedido")
    def generar_pedido_de_certificado(datos: PedidoPayload, empresa: str = "", ambiente: str = "",
                                      usuario: Any = Depends(identidad)):
        """Genera la clave **dentro del servidor** y devuelve el `.csr` para ARCA.

        Esta es la ruta de la facturación; las de los demás servicios, más abajo, hacen lo mismo.
        La clave queda pendiente hasta que se sube el `.crt` por `POST /certificado` (ver
        `arca_pedidos`): **no pisa la clave vigente**, así que pedir uno nuevo con una instancia
        facturando —una renovación— no la deja sin facturar.

        Una instancia sin fila de `arca_config` la gana acá: es el primer paso de un cliente nuevo,
        y la pantalla no tendría de dónde leer el pedido pendiente si la fila no estuviera.
        """
        nombre = _empresa_de(empresa or "", empresa_por_defecto)
        amb = _ambiente_exigido(ambiente)
        pedido = _generar_pedido(
            arca_servicios.SERVICIO_FACTURACION, nombre, amb, datos, usuario)
        existente = db_arca_config.obtener_arca_config(nombre)
        if existente is None:
            _guardar_path(nombre, amb)
            existente = db_arca_config.obtener_arca_config(nombre)
        if existente and not (existente.get("cuit") or "").strip():
            db_arca_config.actualizar_arca_config(nombre, cuit=pedido.cuit)
        return pedido.como_dict(con_csr=True)

    @router.get("/pedido")
    def pedido_de_certificado(empresa: str = "", ambiente: str = ""):
        """Si hay un pedido pendiente para el ambiente, y de qué alias, CUIT y fecha. Sin la clave."""
        return _estado_del_pedido(
            arca_servicios.SERVICIO_FACTURACION, _empresa_de(empresa or "", empresa_por_defecto),
            _ambiente_exigido(ambiente))

    @router.get("/pedido.csr")
    def descargar_pedido_de_certificado(empresa: str = "", ambiente: str = ""):
        """El `.csr` del pedido pendiente, como archivo. Es público: es lo que se le manda a ARCA."""
        return _descargar_csr(
            arca_servicios.SERVICIO_FACTURACION, _empresa_de(empresa or "", empresa_por_defecto),
            _ambiente_exigido(ambiente))

    @router.delete("/pedido")
    def descartar_pedido_de_certificado(empresa: str = "", ambiente: str = "",
                                        usuario: Any = Depends(identidad)):
        """Tira el pedido pendiente **y su clave**. El par vigente no se toca."""
        return _descartar_pedido(
            arca_servicios.SERVICIO_FACTURACION, _empresa_de(empresa or "", empresa_por_defecto),
            _ambiente_exigido(ambiente), usuario)

    @router.delete("/credenciales")
    def borrar_credenciales(empresa: str = "", ambiente: str = "",
                            usuario: Any = Depends(identidad)):
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
        _avisar("borrar", {"empresa": cfg["empresa"], "ambiente": amb}, usuario)
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
    def probar(empresa: str = ""):
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
            asyncio.run(arca_wsaa.autenticar(cert_path, clave_path, ambiente))
        except Exception as e:
            # El texto de ARCA va tal cual: es el que dice si el problema es el
            # certificado, la relación con el servicio o la hora del servidor.
            raise HTTPException(502, f"ARCA rechazó la autenticación: {e}") from None

        info = arca_wsaa.info_certificado(cert_path)
        return {"ok": True, "ambiente": ambiente, "cuit": cfg.get("cuit", ""),
                "certificado": info}

    # ── Los servicios que no son la facturación (ADR-032) ───────────────────

    def _estado_del_servicio(svc: arca_servicios.Servicio, empresa: str) -> dict:
        """El bloque de un servicio: una entrada de `GET /servicios`."""
        if svc.id == arca_servicios.SERVICIO_FACTURACION:
            cfg = _resolver(empresa)
            nombre = (cfg or {}).get("empresa", "") or empresa
            pares = {
                a: _con_pedido(
                    _estado_de_archivos(a, *_paths(cfg, a), con_cuit=True),
                    arca_servicios.SERVICIO_FACTURACION, nombre)
                for a in AMBIENTES
            }
        else:
            nombre = empresa or empresa_por_defecto
            pares = {
                a: _con_pedido(
                    _estado_de_archivos(
                        a, *arca_credenciales.paths_en_disco_de_servicio(nombre, svc.id, a),
                        con_cuit=True),
                    svc.id, nombre)
                for a in AMBIENTES
            }
        return {
            "servicio":    svc.id,
            "etiqueta":    svc.etiqueta,
            "ayuda":       svc.ayuda,
            "empresa":     nombre,
            # Hay con qué autenticarse en algún ambiente. NO dice que ande.
            "configurado": any(p["completo"] for p in pares.values()),
            # 🔑 Un motor con el pedido de certificado (ADR-036) lo dice acá: la pantalla del kit
            # muestra el botón sólo si esta clave está, y un motor viejo —que no la manda— no
            # recibe un botón que contestaría 404.
            "admite_pedido": True,
            "pares":       pares,
        }

    @router.get("/servicios")
    def listar_servicios(empresa: str = ""):
        """Los servicios de ARCA de este producto, cada uno con el estado de sus dos pares.

        Existe siempre y con la facturación sola devuelve un único bloque: es la
        pregunta con la que una pantalla decide si pinta uno o varios.
        """
        return [_estado_del_servicio(arca_servicios.servicio(i), empresa.strip()) for i in servicios]

    if not extras:
        return router

    def _servicio_o_404(servicio: str) -> arca_servicios.Servicio:
        if (servicio or "").strip().lower() not in extras:
            raise HTTPException(404, f"Este producto no configura el servicio {servicio!r} de ARCA.")
        return arca_servicios.servicio(servicio)

    @router.get("/servicios/{servicio}/estado")
    def estado_del_servicio(servicio: str, empresa: str = ""):
        """Qué hay cargado para el servicio, por ambiente, y hasta cuándo dura."""
        svc = _servicio_o_404(servicio)
        return _estado_del_servicio(svc, empresa.strip())

    @router.post("/servicios/{servicio}/certificado")
    def subir_certificado_de_servicio(servicio: str, archivo: UploadFile = File(...),
                                      empresa: str = "", ambiente: str = "",
                                      usuario: Any = Depends(identidad)):
        """Sube el `.crt` del servicio **en el ambiente indicado**. Se valida antes de escribirlo."""
        svc = _servicio_o_404(servicio)
        amb = _ambiente_exigido(ambiente)
        contenido = archivo.file.read()
        datos = _certificado_valido(contenido)

        nombre = empresa.strip() or empresa_por_defecto
        _, clave_path = arca_credenciales.paths_en_disco_de_servicio(nombre, svc.id, amb)
        # 🔑 Igual que en la facturación: el `.crt` de un pedido pendiente trae su clave (ADR-036).
        promovido = _clave_pendiente_que_empareja(contenido, svc.id, amb, nombre, clave_path)
        detalle_extra = {}
        if promovido:
            pedido, clave_nueva = promovido
            destino_cert, destino_clave = _instalar_par(
                _nombres_de_servicio(svc.id, amb, nombre), contenido, clave_nueva)
            db_servicio.guardar_paths_de_servicio(
                nombre, svc.id, amb, certificado_path=destino_cert, clave_path=destino_clave)
            arca_pedidos.descartar(_certs_dir(), svc.id, amb, nombre)
            detalle_extra = {"desde_pedido": True, "alias": pedido.alias}
        else:
            _exigir_pareja_con_la_clave_cargada(contenido, clave_path, amb)

            os.makedirs(_certs_dir(), exist_ok=True)
            destino = os.path.join(_certs_dir(), _nombres_de_servicio(svc.id, amb, nombre)[0])
            with open(destino, "wb") as f:
                f.write(contenido)
            db_servicio.guardar_paths_de_servicio(nombre, svc.id, amb, certificado_path=destino)
        _avisar("certificado", {
            "empresa": nombre, "servicio": svc.id, "ambiente": amb,
            "archivo": archivo.filename or "", "sujeto": datos.sujeto,
            "numero_de_serie": datos.numero_de_serie,
            "vence": datos.vence.strftime("%d-%m-%Y"), **detalle_extra,
        }, usuario)
        if promovido:
            _avisar("clave", {"empresa": nombre, "servicio": svc.id, "ambiente": amb,
                              "desde_pedido": True}, usuario)
        return _estado_del_servicio(svc, nombre)

    @router.post("/servicios/{servicio}/pedido")
    def generar_pedido_de_servicio(servicio: str, datos: PedidoPayload, empresa: str = "",
                                   ambiente: str = "", usuario: Any = Depends(identidad)):
        """Genera la clave del servicio **dentro del servidor** y devuelve el `.csr`. `ambiente` obligatorio."""
        svc = _servicio_o_404(servicio)
        amb = _ambiente_exigido(ambiente)
        pedido = _generar_pedido(svc.id, empresa.strip() or empresa_por_defecto, amb, datos, usuario)
        return pedido.como_dict(con_csr=True)

    @router.get("/servicios/{servicio}/pedido")
    def pedido_de_servicio(servicio: str, empresa: str = "", ambiente: str = ""):
        """Si hay un pedido pendiente para el servicio en ese ambiente. Sin la clave."""
        svc = _servicio_o_404(servicio)
        return _estado_del_pedido(
            svc.id, empresa.strip() or empresa_por_defecto, _ambiente_exigido(ambiente))

    @router.get("/servicios/{servicio}/pedido.csr")
    def descargar_pedido_de_servicio(servicio: str, empresa: str = "", ambiente: str = ""):
        """El `.csr` del pedido pendiente del servicio, como archivo."""
        svc = _servicio_o_404(servicio)
        return _descargar_csr(
            svc.id, empresa.strip() or empresa_por_defecto, _ambiente_exigido(ambiente))

    @router.delete("/servicios/{servicio}/pedido")
    def descartar_pedido_de_servicio(servicio: str, empresa: str = "", ambiente: str = "",
                                     usuario: Any = Depends(identidad)):
        """Tira el pedido pendiente del servicio **y su clave**. El par vigente no se toca."""
        svc = _servicio_o_404(servicio)
        return _descartar_pedido(
            svc.id, empresa.strip() or empresa_por_defecto, _ambiente_exigido(ambiente), usuario)

    @router.post("/servicios/{servicio}/clave")
    def subir_clave_de_servicio(servicio: str, archivo: UploadFile = File(...),
                                empresa: str = "", ambiente: str = "",
                                usuario: Any = Depends(identidad)):
        """Sube el `.key` del servicio en el ambiente indicado. Mismas validaciones, del otro lado."""
        svc = _servicio_o_404(servicio)
        amb = _ambiente_exigido(ambiente)
        contenido = archivo.file.read()
        _clave_valida(contenido)

        nombre = empresa.strip() or empresa_por_defecto
        cert_path, _ = arca_credenciales.paths_en_disco_de_servicio(nombre, svc.id, amb)
        _exigir_pareja_con_el_certificado_cargado(contenido, cert_path, amb)

        os.makedirs(_certs_dir(), exist_ok=True)
        destino = os.path.join(_certs_dir(), _nombres_de_servicio(svc.id, amb, nombre)[1])
        # 🔴 0600, igual que la facturación: ver `escribir_clave_privada`.
        arca_certificados.escribir_clave_privada(destino, contenido)
        db_servicio.guardar_paths_de_servicio(nombre, svc.id, amb, clave_path=destino)
        _avisar("clave", {"empresa": nombre, "servicio": svc.id, "ambiente": amb}, usuario)
        return _estado_del_servicio(svc, nombre)

    @router.delete("/servicios/{servicio}/credenciales")
    def borrar_credenciales_de_servicio(servicio: str, empresa: str = "", ambiente: str = "",
                                        usuario: Any = Depends(identidad)):
        """Saca el par del servicio **de un ambiente**; el otro y la facturación no se tocan.

        Se borran los dos archivos **y** la fila: dejar la ruta apuntando a un
        archivo que ya no está no revive nada acá (no hay rescate por nombre de
        facturación), pero una fila huérfana mostraría «cargado» sin estarlo.
        """
        svc = _servicio_o_404(servicio)
        amb = _ambiente_exigido(ambiente)
        nombre = empresa.strip() or empresa_por_defecto
        for path in arca_credenciales.paths_en_disco_de_servicio(nombre, svc.id, amb):
            if _existe(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        db_servicio.borrar_paths_de_servicio(nombre, svc.id, amb)
        _avisar("borrar", {"empresa": nombre, "servicio": svc.id, "ambiente": amb}, usuario)
        return _estado_del_servicio(svc, nombre)

    @router.post("/servicios/{servicio}/probar")
    def probar_servicio(servicio: str, empresa: str = "", ambiente: str = ""):
        """Se autentica de verdad contra WSAA **para este servicio**.

        Es el único chequeo que dice que el certificado está **habilitado para el
        servicio**: un par perfecto al que nadie le asoció el servicio en el
        Administrador de Relaciones pasa toda validación local y lo rechaza ARCA.

        El ticket se **reusa** si hay uno vigente (`arca_wsaa.autenticar` lo cachea
        por certificado, ambiente y servicio): «Probar» OK no es pedir otro, que
        ARCA rechazaría con `coe.alreadyAuthenticated`.

        Para los servicios con `dummy` (`wscpe`) se agrega si el servicio está
        arriba. Es **informativo**: que ARCA no conteste no desmiente que el
        certificado sirva.
        """
        svc = _servicio_o_404(servicio)
        amb = _ambiente_exigido(ambiente)
        nombre = empresa.strip() or empresa_por_defecto
        cert_path, clave_path = arca_credenciales.paths_en_disco_de_servicio(nombre, svc.id, amb)

        if not _existe(cert_path) or not _existe(clave_path):
            falta = ("el certificado y la clave privada" if not _existe(cert_path) and not _existe(clave_path)
                     else "el certificado" if not _existe(cert_path) else "la clave privada")
            raise HTTPException(
                400, f"Falta cargar {falta} de {svc.etiqueta} para {amb}.")
        errores = arca_certificados.revisar_par_de_archivos(cert_path, clave_path)
        if errores:
            raise HTTPException(400, " | ".join(errores))

        try:
            ticket = asyncio.run(
                arca_wsaa.autenticar(cert_path, clave_path, amb, servicio=svc.wsaa))
        except Exception as e:
            # La explicación va primero y el texto de ARCA tal cual al final: es el
            # que dice si el problema es el certificado, la relación o la hora.
            raise HTTPException(
                502, "ARCA rechazó la autenticación: "
                + arca_servicios.traducir_error_wsaa(str(e))) from None

        info = arca_wsaa.info_certificado(cert_path)
        salida = {
            "ok": True, "servicio": svc.id, "ambiente": amb, "empresa": nombre,
            "certificado": info,
            "cuit_certificado": arca_certificados.leer_certificado_de_archivo(cert_path).cuit,
            "ticket_vence": (ticket or {}).get("expiracion", ""),
            "mensaje": f"Autenticado con ARCA para {svc.etiqueta} ({amb}).",
        }
        try:
            salida["servicio_en_linea"] = arca_servicios.dummy(svc.id, amb)
        except Exception as e:  # informativo: no desmiente la autenticación
            salida["servicio_en_linea"] = None
            salida["aviso_en_linea"] = f"El servicio no contestó el chequeo de estado: {e}"
        return salida

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
