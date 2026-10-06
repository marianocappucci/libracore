"""Los comprobantes de la familia: facturas, notas de crédito y notas de débito.

> ⚠️ **No confundir con `comprobantes_router.py`, que está al lado.** Ese es la
> **bandeja de comprobantes pendientes** —lo que un producto deposita para que
> alguien lo facture después—. Éste es la emisión en sí. Los dos hablan de
> "comprobantes" y hacen cosas distintas; el nombre de este archivo dice
> "facturas" justamente para separarlos.

Hasta acá esto estaba escrito **dos veces**, en `app/web/api/facturas.py` de
Contalibra y en el de Restolibra, y las dos copias eran casi la misma. Se
diffearon antes de unificarlas —que es donde este trabajo se suele arruinar— y
las divergencias reales resultaron ser exactamente **cuatro**:

1. el docstring del módulo;
2. Contalibra cierra los ítems de la bandeja de MercadoPago que la factura vino
   a cubrir (`comprobantes_pendientes_ids`);
3. Restolibra vincula la venta del POS de origen (`venta_id`);
4. un mensaje: *"Configuración → Email"* contra *"Configuración → Integraciones"*.

Los doce endpoints, `_crear_nota`, el reintento de CAE, el cobro y el borrado
eran **idénticos**. Por eso lo que se parametriza acá es sólo eso: las
dependencias de rol, un hook post-emisión y el texto del SMTP.

> 🔴 **Ninguna de las dos copias tenía una defensa que la otra no.** Vale
> escribirlo porque el modo de fallar de una unificación es justamente ése —
> quedarse con la versión más pobre sin notarlo—, y acá se verificó línea por
> línea, no de memoria.

## Cómo lo monta un producto

```python
app.include_router(
    build_comprobantes_router(
        usuario_actual=get_current_user_json,
        solo_admin=require_role_json("admin"),
        al_emitir=cerrar_pendientes_de_la_bandeja,   # opcional
        donde_configurar_smtp="Configuración → Email",
    )
)
```

`al_emitir(factura_id, datos, usuario)` corre **después** del CAE y **no puede
tumbar la request**: para ese punto el comprobante ya existe y está autorizado
ante ARCA, así que un error ahí no lo desharía — dejaría al operador creyendo
que no se emitió. Lo que falle queda para resolver a mano, que es el peor caso
tolerable.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, field_validator

from libracore import (
    arca_credenciales,
    arca_facturacion,
    arca_wsaa,
    arca_wsfe,
    arca_wsfecred,
    config_manager,
    email_sender,
    notas_de_credito,
)
from libracore import pdf_generator as pdf_gen
from libracore import tipos_comprobante as tipos_cbte
from libracore.arca_facturacion import RECEPTORES_DE_FACTURA_A, get_next_numero_with_arca, solicitar_cae
from libracore.cobros import MedioNoEsDeCobro, registrar_cobro_factura
from libracore.db import arca_config as db_arca
from libracore.db import caja as db_caja
from libracore.db import clients as db_clients
from libracore.db import cuenta_corriente as db_cc
from libracore.db import facturas as db_facturas
from libracore.emisor_del_pdf import emisor_para
from libracore.facturas_borrador import armar_borrador
from libracore.validacion import rechazar_booleanos, sin_booleanos

logger = logging.getLogger(__name__)

#: Qué comprobantes puede emitir un emisor, según SU condición frente al IVA.
#:
#: Un monotributista emite **C** y nada más; un Responsable Inscripto elige
#: entre A y B según a quién le factura. Es la lista que la pantalla ofrece, y
#: por eso sale del emisor y no de una constante global.
TIPOS_POR_CONDICION = {
    "Responsable Inscripto": [
        {"value": 1, "label": "Factura A"},
        {"value": 6, "label": "Factura B"},
    ],
    "IVA Exento": [{"value": 6, "label": "Factura B"}],
    "Monotributista": [{"value": 11, "label": "Factura C"}],
}
#: El default es el más conservador: C no discrimina IVA, así que equivocarse
#: hacia acá no inventa un impuesto que nadie pagó.
TIPOS_DEFAULT = TIPOS_POR_CONDICION["Monotributista"]

#: De qué factura sale qué nota. La letra se conserva: una NC de una Factura C
#: es una Nota de Crédito C.
TIPO_NC = tipos_cbte.TIPO_NC
TIPO_ND = tipos_cbte.TIPO_ND

TIPO_LABEL = tipos_cbte.NOMBRE

#: Los tres que son factura —y no nota—. Se usa para decidir si un comprobante
#: puede tener notas colgando y si se le pueden imputar cobros.
TIPOS_FACTURA = tipos_cbte.FACTURAS

CONCEPTOS = [
    {"value": 1, "label": "Productos"},
    {"value": 2, "label": "Servicios"},
    {"value": 3, "label": "Productos y Servicios"},
]

CONDICIONES_VENTA = [
    "Contado", "Tarjeta de Débito", "Tarjeta de Crédito", "Cuenta Corriente",
    "Cheque", "Transferencia Bancaria", "Otros medios de pago electrónico", "Otra",
]

#: Los códigos de condición de IVA del receptor que exige ARCA. Las claves
#: repetidas —"Monotributista" y "Responsable Monotributo"— son los dos nombres
#: con los que el dato llegó a la base a lo largo de los años: sacar uno deja
#: comprobantes viejos sin mapear.
IVA_CODES = {
    "Responsable Inscripto": 1, "IVA Responsable Inscripto": 1,
    "Monotributista": 6, "Responsable Monotributo": 6,
    "IVA Exento": 4, "Consumidor Final": 5,
    "No Alcanzado": 3, "IVA No Responsable": 3,
}

PAGE_SIZE = 50


def _datos_de_fce(payload, cliente: dict) -> tuple[str, str]:
    """`(cbu, transmisión)` para una FCE, o `("", "")` si no lo es. 422 si falta algo.

    El CBU y la modalidad son del emisor y salen de su config de ARCA; el
    vencimiento de pago lo pone quien factura; y el receptor tiene que ser una
    empresa con CUIT (con consumidor final ARCA contesta 10015).
    """
    if payload.tipo in tipos_cbte.FCE_NOTA:
        raise HTTPException(
            422, "Las notas de una FCE se emiten desde la factura, no desde el alta.")
    if payload.tipo not in tipos_cbte.FCE_FACTURA:
        return "", ""
    cuit = (cliente["client_cuit"] or "").replace("-", "").replace(" ", "")
    if not (len(cuit) == 11 and cuit.isdigit()):
        raise HTTPException(422, "La FCE se le emite a una empresa: falta el CUIT del cliente.")
    if not (payload.fch_vto_pago or "").strip():
        raise HTTPException(422, "La FCE exige la fecha de vencimiento de pago.")
    cfg = _config_del_emisor(payload.emisor_id) or {}
    cbu = (cfg.get("fce_cbu") or "").strip()
    transmision = (cfg.get("fce_transmision") or "").strip().upper()
    if not cbu or transmision not in ("SCA", "ADC"):
        raise HTTPException(
            422, "Para emitir una FCE falta cargar el CBU y la modalidad de "
                 "transmisión (SCA o ADC) en la configuración de ARCA.")
    return cbu, transmision


#: Qué FCE puede emitir un emisor, según su condición: la letra sigue a la de
#: sus facturas comunes.
TIPOS_FCE_POR_CONDICION = {
    "Responsable Inscripto": [201, 206],
    "Monotributista": [211],
}


def _config_del_emisor(emisor_id: int | None) -> dict | None:
    """`arca_config.config_del_emisor`, con un emisor que no existe como 422 y no como 500."""
    try:
        return db_arca.config_del_emisor(emisor_id)
    except db_arca.EmisorDesconocido as e:
        raise HTTPException(422, str(e)) from None


def _tipos_fce_del_emisor(emisor_id: int | None = None) -> list[dict]:
    """Las opciones de FCE para el selector, o `[]` si el emisor no la habilitó."""
    cfg = _config_del_emisor(emisor_id) or {}
    if not (cfg.get("fce_cbu") and cfg.get("fce_transmision")):
        return []
    emisor = config_manager.load().get("empresa_iva_condition", "Monotributista")
    return [{"value": t, "label": TIPO_LABEL[t]} for t in TIPOS_FCE_POR_CONDICION.get(emisor, [])]


def exigir_tipo_valido_para_el_receptor(tipo: int, receptor_cond: str) -> None:
    """Falla con un 422 legible si ARCA va a rechazar esa letra para ese receptor.

    A inscriptos y monotributistas les corresponde **A**; a todos los demás,
    **B**. Sin esto la combinación inválida llega a ARCA (error 10243), que ahí
    rechaza, y el comprobante queda numerado y sin CAE. Si la condición es
    desconocida no se opina: ahí decide `arca_wsfe.condicion_iva_receptor_id`.
    """
    # La letra manda, sea común o FCE: una FCE B a un inscripto da 10243 igual.
    letra = tipos_cbte.LETRA.get(tipo)
    if letra not in ("A", "B") or receptor_cond not in IVA_CODES:
        return
    pide_a = receptor_cond in RECEPTORES_DE_FACTURA_A
    if letra == "B" and pide_a:
        raise HTTPException(
            422, f"A un cliente «{receptor_cond}» le corresponde Factura A, no B.")
    if letra == "A" and not pide_a:
        raise HTTPException(
            422, f"A un cliente «{receptor_cond}» le corresponde Factura B, no A.")


def calcular_totales(items: list[dict], tax_rate: float) -> dict:
    """Subtotal, IVA y total a partir de los ítems y la tasa.

    Vivía en `web/helpers/form_helper.py` de cada producto, con seis líneas
    idénticas.
    """
    subtotal = round(sum(i["subtotal"] for i in items), 2)
    iva_amount = round(subtotal * tax_rate, 2)
    return {
        "subtotal": subtotal,
        "iva_amount": iva_amount,
        "total": round(subtotal + iva_amount, 2),
    }


#: Los seis campos del SMTP, resueltos.
#:
#: 🔴 **Hay DOS configuraciones de SMTP en la familia, y esto es lo que las une.**
#:
#: - La de **libraauth** (`smtp_settings`, cifrada) manda el mail de
#:   recuperacion de contrasena. La tienen los ocho productos, y es la que
#:   configura la pantalla compartida de `libra-ui`.
#: - La de **`config.json`** (`email_smtp_*`) manda **los comprobantes**, que es
#:   lo que hace este router. La usan los tres productos que lo montan:
#:   Contalibra, Restolibra y LibraClub.
#:
#: Que sean dos no era un diseno: la de comprobantes nacio antes que la otra y
#: quedo leyendo `config.json`. El sintoma es que el cliente carga su contrasena
#: de aplicacion en la pantalla, la pantalla dice "Guardado", y los comprobantes
#: siguen sin salir --porque configuro el OTRO store.
#:
#: `smtp_config` es como el producto le pasa el resolver de libraauth. Se inyecta
#: y no se importa porque **LibraCore no depende de libraauth**: es el mismo
#: criterio que `registrar_cobro` y `al_emitir`.
def smtp_efectivo(resolver) -> dict:
    """El SMTP a usar: el del resolver del producto si lo hay, `config.json` si no.

    🔑 **Publica a proposito.** En cada producto esto se resuelve en tres
    lugares --el envio de comprobantes de acá, el de presupuestos, y el
    endpoint que prueba la conexion-- y los tres tienen que dar lo mismo. Que
    cada uno lo resuelva por su cuenta es exactamente como aparecieron los dos
    stores que este cambio viene a unificar.

    ⚠️ La caida a `config.json` es una **red de seguridad medida**, no un
    default de diseno. Se relevaron las 7 instancias de la flota que montan
    este router antes de escribirla:

    - En 6 de 7, `config.json` esta **vacio** y el SMTP sale del entorno. En
      esas, mandar un comprobante por mail **hoy falla con un 400**, aunque la
      instancia tiene un SMTP perfectamente usable en `LIBRAAUTH_SMTP_*`. Este
      cambio tambien las arregla.
    - La unica con datos en `config.json` es `contalibra` de produccion, y sus
      valores son **identicos** a los del entorno --la misma casilla--. Por eso
      no hace falta migrar nada: copiarlos a la base solo agregaria una tercera
      copia cifrada de las mismas credenciales.

    O sea que hoy esta rama **no se ejecuta en ninguna instancia**. Se deja
    igual porque es la direccion segura: si a `contalibra` le sacaran las
    variables de entorno, sus comprobantes seguirian saliendo por donde salen
    hoy en vez de cortarse **sin ningun sintoma** --nadie se entera hasta que
    un cliente reclama una factura que no le llego--.
    """
    if resolver is not None:
        cfg = resolver()
        if cfg.configurado:
            return {
                "host": cfg.host, "port": int(cfg.port or 587), "user": cfg.user,
                "password": cfg.password,
                "from_email": cfg.from_email or cfg.user,
                "from_name": cfg.from_name,
            }
    cfg = config_manager.load()
    return {
        "host": cfg.get("email_smtp_host", ""),
        "port": int(cfg.get("email_smtp_port", 587) or 587),
        "user": cfg.get("email_smtp_user", ""),
        "password": cfg.get("email_smtp_password", ""),
        "from_email": cfg.get("email_from") or cfg.get("email_smtp_user", ""),
        "from_name": cfg.get("email_from_name", ""),
    }


def smtp_configurado(resolver=None) -> bool:
    smtp = smtp_efectivo(resolver)
    return bool(smtp["host"] and smtp["user"])


def enviar_comprobante_por_mail(
    *, to_email: str, to_name: str, pdf_path: str, factura_label: str, total: float,
    resolver=None, empresa_nombre: str | None = None,
) -> None:
    """Manda el PDF con la config SMTP resuelta. Ver `smtp_efectivo`.

    `empresa_nombre` es quien firma el asunto y el cuerpo: el emisor del comprobante
    (`emisor_del_pdf.emisor_para`). Sin él, el de la configuración de la instancia, como siempre.
    """
    smtp = smtp_efectivo(resolver)
    email_sender.enviar_comprobante(
        to_email=to_email, to_name=to_name, pdf_path=pdf_path,
        empresa_nombre=(
            empresa_nombre if empresa_nombre is not None
            else config_manager.load().get("empresa_nombre", "")),
        factura_label=factura_label, total=total,
        smtp_host=smtp["host"],
        smtp_port=smtp["port"],
        smtp_user=smtp["user"],
        smtp_password=smtp["password"],
        from_email=smtp["from_email"],
        from_name=smtp["from_name"],
        asunto="", cuerpo="",
    )


class ItemPayload(BaseModel):
    description: str
    qty: float
    unit_price: float

    _no_son_booleanos = sin_booleanos("qty", "unit_price")


class FacturaPayload(BaseModel):
    """Lo que manda el formulario de alta.

    ⚠️ `extra="allow"` **a propósito**: cada producto agrega su propio campo
    —`comprobantes_pendientes_ids` en Contalibra, `venta_id` en Restolibra— y lo
    lee desde su hook `al_emitir`. Sin esto, pydantic los descartaría **en
    silencio** y el hook recibiría un diccionario sin el dato que vino a usar.
    El costo es que un campo mal tipeado tampoco se rechaza; el beneficio es que
    el motor no tiene que conocer los campos de sus consumidores.
    """

    model_config = ConfigDict(extra="allow")

    tipo: int
    punto_venta: int = 1
    concepto: int = 1
    condicion_venta: str = ""
    fecha: str
    observations: str = ""
    fch_serv_desde: str = ""
    fch_serv_hasta: str = ""
    fch_vto_pago: str = ""
    tax_rate: float = 0.21
    client_id: int | None = None
    client_name: str = ""
    client_cuit: str = ""
    client_address: str = ""
    client_iva: str = ""
    items: list[ItemPayload]
    #: Con qué configuración de ARCA se emite (`arca_config.id`). Sólo la manda un
    #: producto con varias razones sociales; sin ella, el emisor único de la instancia.
    emisor_id: int | None = None

    _no_son_booleanos = sin_booleanos(
        "tipo", "punto_venta", "concepto", "tax_rate", "client_id", "emisor_id")


class AnularIn(BaseModel):
    """El cuerpo, **opcional**, de `POST /{factura_id}/anular`: por qué se anula."""

    model_config = ConfigDict(extra="forbid")

    motivo: str = ""


#: Cómo traduce este router cada motivo de `ComprobanteNoAnulable` a su respuesta HTTP.
_STATUS_DE_ANULACION = {
    db_facturas.ComprobanteNoAnulable.NO_EXISTE: 404,
    db_facturas.ComprobanteNoAnulable.CON_CAE: 409,
    db_facturas.ComprobanteNoAnulable.YA_ANULADO: 409,
    db_facturas.ComprobanteNoAnulable.CON_COBROS: 409,
}


class CobroPayload(BaseModel):
    fecha: str = ""
    caja_id: int | None = None
    #: `[{medio_id, monto, referencia}]`
    pagos: list[dict]

    _no_son_booleanos = sin_booleanos("caja_id")

    @field_validator("pagos", mode="before")
    @classmethod
    def _pagos_sin_booleanos(cls, pagos):
        """Cada pago es un `dict` sin tipar a propósito (la pantalla manda filas con `""` donde no hay monto, y las ignora el motor), así que `sin_booleanos` no llega: `{"monto": true}` era un
        cobro de 1 peso (`float(True)`). Se rechaza acá, sin cambiar el contrato: mismas claves, mismos opcionales, mismas filas vacías."""
        rechazar_booleanos(pagos, ("monto",), "pagos[].")
        rechazar_booleanos(pagos, ("medio_id",), "pagos[].", esperado="un texto")
        return pagos


class EmailPayload(BaseModel):
    email: str


def _tipos_emisor() -> list[dict]:
    cfg = config_manager.load()
    return TIPOS_POR_CONDICION.get(
        cfg.get("empresa_iva_condition", "Monotributista"), TIPOS_DEFAULT
    )


def _arca_punto_venta(emisor_id: int | None = None) -> int:
    cfg = _config_del_emisor(emisor_id)
    return cfg.get("punto_venta", 1) if cfg else 1


def _resolve_cliente(payload: FacturaPayload) -> dict:
    """El cliente de la ficha si vino por id; si no, lo que se tipeó a mano.

    El alta a mano existe porque un mostrador le factura a alguien que no está
    en la ficha todo el tiempo.
    """
    if payload.client_id:
        c = db_clients.get_client(payload.client_id)
        if c:
            return {
                "client_name": c["name"],
                "client_cuit": c.get("cuit_dni", ""),
                "client_address": c.get("address", ""),
                "client_iva": c.get("iva_condition", ""),
            }
    return {
        "client_name": payload.client_name.strip(),
        "client_cuit": payload.client_cuit.strip(),
        "client_address": payload.client_address.strip(),
        "client_iva": payload.client_iva,
    }


def _exigir_vigente(factura: dict) -> None:
    """409 si el comprobante está anulado (ADR-022): no se autoriza, no se cobra y no admite notas.

    Pedirle CAE a un anulado sería autorizar ante ARCA un comprobante que el
    operador ya dio de baja; cobrarlo o hacerle una nota, operar sobre algo que
    no existe.
    """
    if factura.get("anulada_en"):
        raise HTTPException(409, "El comprobante está anulado.")


def _numero(factura: dict) -> str:
    return f"{str(factura['punto_venta']).zfill(4)}-{str(factura['numero']).zfill(8)}"


def _pdf_del_comprobante(factura: dict, resolvedor: Callable[[dict], dict | None] | None) -> str:
    """La ruta del PDF **guardado** del comprobante; si el archivo no está, se arma de nuevo desde su fila.

    El guardado es el que se generó al emitir o autorizar, con el emisor de ese momento (ADR-031): no se
    regenera aunque después cambie el logo o el domicilio, porque es lo que se le mandó al cliente. Se
    regenera sólo si se perdió (un redeploy que borra el disco del contenedor), y entonces sale con el
    emisor de hoy. **No se vuelve a guardar en `pdf_path`**: es lo que hacía el endpoint de mail.
    """
    pdf_path = factura.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        pdf_path = pdf_gen.generate_pdf_factura(factura, resolvedor=resolvedor)
    return pdf_path


def _registrar_enviar_email(
    router: APIRouter, *, exigir: Callable[[int], dict],
    emisor_del_pdf: Callable[[dict], dict | None] | None, smtp_config: Callable[[], Any] | None,
    donde_configurar_smtp: str,
) -> None:
    """Registra `POST /{factura_id}/enviar-email` en `router`. **Una sola implementación**, la usan los dos factories.

    `exigir(factura_id)` devuelve el comprobante o levanta el 404: es lo único que cambia entre el router de
    comprobantes y el de sólo-PDF (que además filtra qué comprobantes se ven).
    """
    @router.post("/{factura_id}/enviar-email")
    def enviar_email(factura_id: int, payload: EmailPayload):
        factura = exigir(factura_id)
        if not smtp_configurado(smtp_config):
            raise HTTPException(
                400, f"Configurá el servidor SMTP en {donde_configurar_smtp}."
            )
        if not payload.email.strip():
            raise HTTPException(422, "Ingresá una dirección de email.")

        pdf_path = _pdf_del_comprobante(factura, emisor_del_pdf)

        etiqueta = (
            f"{pdf_gen._TIPO_LABELS.get(factura['tipo'], 'Comprobante')} "
            f"{_numero(factura)}"
        )
        try:
            enviar_comprobante_por_mail(
                to_email=payload.email.strip(), to_name=factura["cliente_razon"],
                pdf_path=pdf_path, factura_label=etiqueta, total=factura["total"],
                resolver=smtp_config,
                # Firma el emisor del comprobante, no el de la instancia.
                empresa_nombre=emisor_para(factura, resolvedor=emisor_del_pdf).get("nombre") or "",
            )
        except Exception as e:
            raise HTTPException(502, f"Error al enviar: {e}") from e
        return {"ok": True}


def _detalle(factura: dict) -> dict:
    """El comprobante con todo lo que le cuelga: sus notas, su original y sus cobros."""
    es_factura = factura["tipo"] in TIPOS_FACTURA
    ncs = nds = []
    if es_factura:
        # Del mismo emisor y ambiente: `(tipo, pv, número)` solo no identifica al comprobante.
        mismo = {"emisor_id": factura.get("emisor_id"), "ambiente": factura.get("ambiente")}
        ncs = db_facturas.get_nc_de_factura(
            factura["tipo"], factura["punto_venta"], factura["numero"], **mismo
        )
        nds = db_facturas.get_nd_de_factura(
            factura["tipo"], factura["punto_venta"], factura["numero"], **mismo
        )

    factura_original = None
    if factura.get("cbte_asoc_tipo") and factura.get("cbte_asoc_nro"):
        factura_original = db_facturas.get_factura_por_tipo_pv_nro(
            factura["cbte_asoc_tipo"], factura["cbte_asoc_pv"], factura["cbte_asoc_nro"],
            emisor_id=factura.get("emisor_id"), ambiente=factura.get("ambiente"),
        )

    cobros = db_caja.get_cobros_factura(factura["id"]) if es_factura else []
    total_cobrado = sum(c["monto"] for c in cobros)
    # `max(0, ...)`: un cobro de más no puede mostrarse como pendiente negativo.
    pendiente = (
        max(0.0, round(factura["total"] - total_cobrado, 2))
        if es_factura and not factura.get("anulada_en") else 0.0
    )

    cliente = db_clients.get_client_by_cuit(factura.get("cliente_cuit", ""))

    return {
        "factura": factura,
        "tipo_label": pdf_gen._TIPO_LABELS.get(factura["tipo"], "Documento"),
        "concepto_label": pdf_gen._CONCEPTO_LABELS.get(
            factura.get("concepto", 1), "Productos"
        ),
        "iva_label": pdf_gen._IVA_LABELS.get(factura.get("cliente_iva_cond") or 0, ""),
        "notas_credito": ncs,
        "notas_debito": nds,
        "factura_original": factura_original,
        "cobros": cobros,
        "total_cobrado": total_cobrado,
        "pendiente": pendiente,
        "cliente_email": cliente.get("email", "") if cliente else "",
    }


#: Cómo traduce este router cada motivo del núcleo (`notas_de_credito`) a su respuesta HTTP. Los otros
#: productos que emiten notas traducen los mismos códigos como les convenga.
_STATUS_DE_NOTA = {
    notas_de_credito.NotaNoPermitida.TIPO: 400,
    notas_de_credito.NotaNoPermitida.YA_TIENE_NOTA: 409,
    notas_de_credito.NotaNoPermitida.NOTA_SIN_CAE: 409,
    notas_de_credito.NotaNoPermitida.EN_CURSO: 409,
    notas_de_credito.NotaNoPermitida.RECEPTOR: 422,
    notas_de_credito.NotaNoPermitida.IMPORTE: 422,
    notas_de_credito.NotaNoPermitida.SUPERA_SALDO: 409,
}


def _registrar_nota(nota: dict, ambiente: str, usuario_id: int, emisor_id: int | None) -> int:
    """Guarda la nota en la tabla `facturas` de este router (todavía sin CAE) y devuelve su id.

    La nota es del emisor de su original: lo emite la misma razón social, con su par de ARCA.
    """
    return db_facturas.create_factura(
        emisor_id=emisor_id,
        # 🔑 El ambiente con el que se emitió, que es lo que separa un comprobante real de uno de prueba en el
        # libro IVA. Sin ARCA configurado no hay CAE y el número es el de la propia instancia: ese comprobante
        # **es** el real del cliente, así que va como `produccion`. No es un default silencioso: es la respuesta
        # a «¿contra qué se emitió?» cuando no se emitió contra nada.
        ambiente=ambiente, usuario_id=usuario_id, **nota,
    )


async def _crear_nota(
    orig: dict, nuevo_tipo: int, obs_prefijo: str, usuario_id: int,
    emisor_del_pdf: Callable[[dict], dict | None] | None = None,
) -> int:
    """Emite una nota **de débito** que referencia al comprobante original.

    La nota de **crédito** ya no pasa por acá: es una sola para toda la familia y vive en
    `libracore.notas_de_credito` (`emitir_nota_de_credito`). Esta función arma la nota con el mismo
    `armar_nota` (importes copiados, fecha de hoy, comprobante asociado), pero la nota de débito no tiene
    guardas propias: una factura admite varias.
    """
    nota = notas_de_credito.armar_nota(orig, nuevo_tipo, prefijo=obs_prefijo)
    emisor_id = orig.get("emisor_id")
    numero, ta, arca = await get_next_numero_with_arca(orig["punto_venta"], nuevo_tipo, emisor_id)
    nota["numero"] = numero
    nota_id = _registrar_nota(nota, arca_facturacion.ambiente_de(arca), usuario_id, emisor_id)
    nota_db = db_facturas.get_factura(nota_id)
    nota_db = await solicitar_cae(nota_id, nota_db, ta, arca)
    db_facturas.update_factura_pdf_path(nota_id, pdf_gen.generate_pdf_factura(nota_db, resolvedor=emisor_del_pdf))
    return nota_id


async def _emitir_nota_de_credito(
    orig: dict, usuario_id: int, importe: Decimal | None = None,
    emisor_del_pdf: Callable[[dict], dict | None] | None = None,
) -> int:
    """La nota de crédito de este router: el núcleo del motor con las costuras de la tabla `facturas`."""
    emisor_id = orig.get("emisor_id")

    async def numerar(tipo_nota: int, punto_venta: int):
        numero, ta, arca = await get_next_numero_with_arca(punto_venta, tipo_nota, emisor_id)
        return numero, (ta, arca)

    def registrar(nota: dict, contexto) -> int:
        _ta, arca = contexto
        return _registrar_nota(nota, arca_facturacion.ambiente_de(arca), usuario_id, emisor_id)

    async def pedir_cae(nota_id: int, _nota: dict, contexto) -> int:
        ta, arca = contexto
        nota_db = await solicitar_cae(nota_id, db_facturas.get_factura(nota_id), ta, arca)
        db_facturas.update_factura_pdf_path(nota_id, pdf_gen.generate_pdf_factura(nota_db, resolvedor=emisor_del_pdf))
        return nota_id

    emitida = await notas_de_credito.emitir_nota_de_credito(
        orig,
        clave=("facturas", orig["id"]),
        cargar_previas=lambda: db_facturas.get_nc_de_factura(
            orig["tipo"], orig["punto_venta"], orig["numero"],
            emisor_id=emisor_id, ambiente=orig.get("ambiente")),
        numerar=numerar, registrar=registrar, pedir_cae=pedir_cae, importe=importe,
    )
    return emitida.registro


class NotaCreditoIn(BaseModel):
    """El cuerpo, **opcional**, de `POST /{factura_id}/nota-credito`.

    Sin cuerpo (o sin `importe`) la nota es **total**, como siempre. Con `importe` es **parcial**: el monto a acreditar,
    con IVA incluido, de hasta dos decimales. La fecha, la letra y el comprobante asociado no se eligen.
    """

    model_config = ConfigDict(extra="forbid")

    importe: Decimal | None = None
    _no_son_booleanos = sin_booleanos("importe")


def _registrar_nota_de_credito(router: APIRouter, *, usuario_actual: Callable[..., Any],
                               solo_admin: Callable[..., Any],
                               emisor_del_pdf: Callable[[dict], dict | None] | None = None) -> None:
    """Registra `POST /{factura_id}/nota-credito` en `router`. **Una sola implementación**, la usan los dos factories.

    Existe para que un producto que no tiene pantallas de facturas (VentaLibra) pueda ofrecer la nota de crédito
    sin montar los otros once endpoints —alta manual, borrador, cobro, email, borrado—, que no usa. La lógica
    de fondo está en `notas_de_credito` (ADR-014); esto es el borde HTTP y el abono a la cuenta corriente.
    """
    @router.post("/{factura_id}/nota-credito", dependencies=[Depends(solo_admin)])
    def nota_credito(factura_id: int, payload: NotaCreditoIn | None = None, usuario: dict = Depends(usuario_actual)):
        orig = db_facturas.get_factura(factura_id)
        if not orig:
            raise HTTPException(404, "Factura no encontrada")
        _exigir_vigente(orig)
        importe = payload.importe if payload else None
        try:
            nota_id = asyncio.run(_emitir_nota_de_credito(orig, usuario["id"], importe, emisor_del_pdf))
        except notas_de_credito.NotaNoPermitida as e:
            raise HTTPException(_STATUS_DE_NOTA[e.codigo], str(e)) from None

        # Si el original era a crédito, la deuda del cliente se cancela: quedó
        # anulada, y dejarla en la cuenta corriente sería cobrarle algo que ya
        # no debe.
        if orig.get("condicion_venta") == "Cuenta Corriente":
            cliente = db_clients.get_client_by_cuit(orig.get("cliente_cuit", ""))
            if cliente:
                nota_db = db_facturas.get_factura(nota_id)
                db_cc.create_cc_pago(
                    # El importe de ESTA nota (en la total es el de la factura; en una parcial, el acreditado).
                    cliente_id=cliente["id"], monto=nota_db["total"],
                    fecha=datetime.date.today().isoformat(),
                    concepto=(
                        f"NC {_numero(orig)} (anula "
                        f"{TIPO_LABEL.get(orig['tipo'], 'comprobante')} "
                        f"{_numero(orig)})"
                    ),
                    # La marca que dice que la nota ya abonó: `anular_venta` no acredita otra vez.
                    referencia=notas_de_credito.referencia_cc_de_nota(factura_id, nota_id),
                    medio_pago="Cuenta Corriente", caja_id=None,
                    usuario_id=usuario["id"],
                )
        return db_facturas.get_factura(nota_id)


def build_nota_de_credito_router(
    *,
    usuario_actual: Callable[..., Any],
    solo_admin: Callable[..., Any],
    prefix: str = "/api/facturas",
    emisor_del_pdf: Callable[[dict], dict | None] | None = None,
) -> APIRouter:
    """Sólo `POST {prefix}/{factura_id}/nota-credito`: la nota de crédito total de una factura, autorizada por ARCA.

    Para los productos que facturan desde otra pantalla (la venta) y no necesitan el router completo de
    comprobantes. Mismas guardas, mismos códigos HTTP y mismo abono a la cuenta corriente que
    `build_comprobantes_router`: es el mismo código. `solo_admin` gatea la ruta. `emisor_del_pdf`: ver
    `build_comprobantes_router`.
    """
    router = APIRouter(prefix=prefix, tags=["facturas"])
    _registrar_nota_de_credito(
        router, usuario_actual=usuario_actual, solo_admin=solo_admin, emisor_del_pdf=emisor_del_pdf)
    return router


def build_comprobantes_pdf_router(
    *,
    usuario_actual: Callable[..., Any],
    prefix: str = "/api/facturas",
    emisor_del_pdf: Callable[[dict], dict | None] | None = None,
    puede_ver: Callable[[dict], bool] | None = None,
    smtp_config: Callable[[], Any] | None = None,
    donde_configurar_smtp: str = "Configuración → Email",
) -> APIRouter:
    """**Sólo** el PDF del comprobante y su envío por mail, para un producto que emite por su cuenta (ADR-031).

    - `GET {prefix}/{factura_id}/pdf`: el PDF (`application/pdf`, en línea).
    - `POST {prefix}/{factura_id}/enviar-email`, `{"email": ...}`: el mismo PDF por correo.

    Es lo que necesita LibraCargo, cuyo comprobante vive en `facturas` del motor pero cuya emisión, anulación y
    notas de crédito son suyas: `build_comprobantes_router` trae además el alta, el borrador, el cobro, el
    borrado y la anulación, que ese producto no debe exponer. **Mismo código que el router grande**
    (`_pdf_del_comprobante`, `_registrar_enviar_email`): lo que se manda por mail es el archivo que se ve.

    - `usuario_actual` gatea todas las rutas (una sesión, no un rol).
    - `emisor_del_pdf`: `(comprobante: dict) -> dict | None`, el resolvedor del emisor de esta factory. Ver
      `build_comprobantes_router` y `libracore.emisor_del_pdf`.
    - `puede_ver`: `(comprobante: dict) -> bool`. Un comprobante que no pasa **no existe** para este router:
      404 en las dos rutas, el mismo de un id inexistente, sin delatar que está. Un producto lo usa para no
      mostrar, por ejemplo, los de otro ambiente o los de una razón social que no le corresponde al usuario.
    - `smtp_config` y `donde_configurar_smtp`: como en `build_comprobantes_router`.
    """
    router = APIRouter(
        prefix=prefix, tags=["facturas"], dependencies=[Depends(usuario_actual)])

    def _exigir(factura_id: int) -> dict:
        factura = db_facturas.get_factura(factura_id)
        if not factura or (puede_ver is not None and not puede_ver(factura)):
            raise HTTPException(404, "Factura no encontrada")
        return factura

    @router.get("/{factura_id}/pdf")
    def pdf(factura_id: int):
        factura = _exigir(factura_id)
        try:
            with open(_pdf_del_comprobante(factura, emisor_del_pdf), "rb") as archivo:
                contenido = archivo.read()
        except Exception as e:
            raise HTTPException(500, f"Error generando el PDF: {e}") from e
        nombre = f"comprobante-{_numero(factura)}.pdf"
        return Response(
            contenido, media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{nombre}"'},
        )

    _registrar_enviar_email(
        router, exigir=_exigir, emisor_del_pdf=emisor_del_pdf, smtp_config=smtp_config,
        donde_configurar_smtp=donde_configurar_smtp,
    )
    return router


def build_comprobantes_router(
    *,
    usuario_actual: Callable[..., Any],
    solo_admin: Callable[..., Any],
    prefix: str = "/api/facturas",
    al_emitir: Callable[[int, dict, dict], None] | None = None,
    registrar_cobro: Callable[..., None] | None = None,
    donde_configurar_smtp: str = "Configuración → Email",
    smtp_config: Callable[[], Any] | None = None,
    emisor_del_pdf: Callable[[dict], dict | None] | None = None,
) -> APIRouter:
    """Los doce endpoints de comprobantes, con lo del producto inyectado.

    `usuario_actual` es la dependencia que devuelve el usuario de la sesión
    —hace falta para el `usuario_id` que queda en cada comprobante, que es la
    trazabilidad de quién le facturó qué a quién—. `solo_admin` gatea las tres
    rutas que no son de mostrador: borrar, nota de crédito y nota de débito.

    `registrar_cobro` **reemplaza** —no envuelve— la escritura del cobro. Se
    llama con `(factura, pagos, fecha=..., caja_id=..., usuario=...)` y lo que
    levante sale tal cual: un producto puede devolver un 409 si no hay caja
    abierta. Por omisión se usa `libracore.cobros.registrar_cobro_factura`, que
    es lo que hacen Contalibra y Restolibra.

    `emisor_del_pdf` es `(comprobante: dict) -> dict | None`: los datos del emisor para el PDF y para quien
    firma el mail (`direccion`, `iva_condition`, `iibb`, `inicio_actividades`, `logo_bytes`...). Pisa lo que
    sale de la configuración de la instancia y del `emisor_id` del comprobante (ADR-031, ver
    `libracore.emisor_del_pdf`) y **reemplaza**, para este router, al resolvedor registrado con
    `emisor_del_pdf.registrar_resolvedor`. Sin él, se usa ese, y sin ése, nada cambia.

    🔑 **Existe por LibraClub, y el motivo no es cosmético.** Ese producto lleva
    la caja **por turno** (`turnos_caja`, con su arqueo al cerrar) y el default
    escribe el movimiento **sin `turno_id`**: la plata entraba y ningún cierre la
    contaba — que es exactamente lo que una caja por turno viene a evitar. La
    alternativa era que el producto tapara la ruta del factory con una propia,
    y dos rutas con el mismo path resueltas por orden de registro es peor que
    un parámetro.
    """
    router = APIRouter(prefix=prefix, tags=["facturas"])
    admin = [Depends(solo_admin)]

    def _exigir(factura_id: int) -> dict:
        factura = db_facturas.get_factura(factura_id)
        if not factura:
            raise HTTPException(404, "Factura no encontrada")
        return factura

    # ── Catálogo y listado ────────────────────────────────────────────────

    @router.get("/tipos")
    def tipos(usuario: dict = Depends(usuario_actual), emisor_id: int | None = None):
        """Qué puede emitir este emisor, y con qué opciones. Lo lee el formulario.

        🔑 **El `punto_venta` que devuelve es el del POS donde está parado quien
        pregunta**, no el de la empresa: sale de la caja de su turno abierto. Un
        cliente con varios mostradores necesita numeración fiscal separada por
        mostrador, porque ARCA numera por (tipo, punto de venta).

        Si esa caja no tiene uno propio —o no hay turno abierto— cae al de la
        empresa, que es como funcionó siempre y es el caso de todas las
        instancias existentes.

        Con `emisor_id` (un producto con varias razones sociales) el punto de
        venta es el de ESE emisor: la caja no sabe de razones sociales.
        """
        tipos_emisor = _tipos_emisor()
        return {
            # La FCE aparece como una opción más del mismo selector, y sólo si el
            # emisor ya cargó su CBU: sin él ARCA la rechazaría.
            "tipos": tipos_emisor + _tipos_fce_del_emisor(emisor_id),
            "conceptos": CONCEPTOS,
            "condiciones_venta": CONDICIONES_VENTA,
            "punto_venta": (
                _arca_punto_venta(emisor_id) if emisor_id is not None
                else db_caja.resolver_punto_venta((usuario or {}).get("id"))
                or _arca_punto_venta()
            ),
            "es_monotributista": (
                len(tipos_emisor) == 1 and tipos_emisor[0]["value"] == 11
            ),
        }

    @router.get("/fce/corresponde")
    def fce_corresponde(
        cuit: str = Query(..., description="CUIT del receptor"),
        total: Decimal = Query(..., gt=0, description="total de la factura, con IVA"),
        fecha: datetime.date | None = Query(None, description="fecha de emisión; hoy si no viene"),
        emisor_id: int | None = Query(None, description="arca_config.id; sin él, el emisor único"),
    ):
        """¿A esta factura le corresponde ser FCE? Lo pregunta el formulario **antes de emitir** (ADR-019).

        ARCA no lo frena al emitir, y una factura emitida no se cambia: el aviso tiene que llegar antes. Es un aviso,
        no un bloqueo, y nunca falla por ARCA: si el registro de FCE no contesta, `disponible` viene en `false` con el
        motivo, y se emite como siempre. `fce_habilitada` dice si este emisor ya puede emitir FCE (cargó su CBU y su
        modalidad); si corresponde y no la tiene, hay que decirle qué cargar.
        """
        digitos = "".join(c for c in cuit if c.isdigit())
        if len(digitos) != 11:
            raise HTTPException(422, "El CUIT del receptor tiene que tener 11 dígitos.")
        arca = _config_del_emisor(emisor_id)
        resultado = asyncio.run(arca_wsfecred.corresponde_fce(
            arca, digitos, total, fecha or datetime.date.today()))
        return resultado | {"fce_habilitada": bool(_tipos_fce_del_emisor(emisor_id))}

    @router.get("")
    def listar(
        q: str = "", vista: str = "facturas", desde: str = "", hasta: str = "",
        page: int = 1,
    ):
        page = max(1, page)
        resultado = db_facturas.get_facturas_filtradas(
            desde, hasta, q, vista, PAGE_SIZE, (page - 1) * PAGE_SIZE
        )
        total = resultado["total"]
        return {
            "items": resultado["items"],
            "total": total,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "page": page,
        }

    # 🔴 Las rutas que emiten —`crear`, `autorizar`, las dos notas— y el
    # borrador son `def` y no `async def`, a propósito: uvicorn corre con UN
    # solo proceso. La numeración y el CAE son async sólo en los bordes de
    # red: entre medio va la base y la firma del TRA con `openssl` por
    # subproceso, y después el PDF, que es CPU. Como corrutinas, emitir un
    # comprobante frenaba a la instancia entera. Como `def` corren en el
    # threadpool, y cada corrutina del motor va con `asyncio.run` en un loop
    # propio de ese hilo.
    #
    # ── Emisión ───────────────────────────────────────────────────────────

    @router.post("/borrador-pdf")
    def borrador_pdf(payload: FacturaPayload):
        """El PDF de lo que se está por emitir, **sin guardar ni llamar a ARCA**.

        Es lo que deja mirar el comprobante antes de quemarle un número a la
        numeración fiscal, que no se puede devolver.
        """
        import tempfile

        cliente = _resolve_cliente(payload)
        _config_del_emisor(payload.emisor_id)   # un emisor que no existe es un 422, no un 500 al dibujar
        items = [
            {
                "description": i.description.strip(), "qty": i.qty,
                "unit_price": i.unit_price,
                "subtotal": round(i.qty * i.unit_price, 2),
            }
            for i in payload.items
            if i.description.strip()
        ]
        if not items:
            items = [{
                "description": "Ejemplo de servicio", "qty": 1,
                "unit_price": 1000.0, "subtotal": 1000.0,
            }]

        # Una C no discrimina IVA: el neto ES el total.
        tax_rate = 0.0 if payload.tipo in tipos_cbte.C else payload.tax_rate
        totales = calcular_totales(items, tax_rate)

        borrador = {
            "id": 0, "tipo": payload.tipo, "punto_venta": payload.punto_venta,
            "numero": 0, "fecha": payload.fecha,
            "cliente_cuit": cliente["client_cuit"],
            "cliente_razon": cliente["client_name"] or "BORRADOR",
            "cliente_iva_cond": IVA_CODES.get(cliente["client_iva"], 0),
            "cliente_domicilio": cliente["client_address"], "items": items,
            "subtotal": totales["subtotal"], "iva_amount": totales["iva_amount"],
            "total": totales["total"], "concepto": payload.concepto,
            "observaciones": payload.observations.strip(),
            "condicion_venta": payload.condicion_venta,
            "fch_serv_desde": payload.fch_serv_desde,
            "fch_serv_hasta": payload.fch_serv_hasta,
            "fch_vto_pago": payload.fch_vto_pago, "cae": "", "cae_vto": "",
            # El borrador lleva el emisor que se eligió: lo que se mira es lo que va a salir.
            "emisor_id": payload.emisor_id,
        }

        # Carpeta temporal que se borra sola: un borrador no tiene por qué
        # sobrevivir a la request que lo pidió.
        with tempfile.TemporaryDirectory() as carpeta:
            try:
                ruta = pdf_gen.generate_pdf_factura(borrador, output_dir=carpeta, resolvedor=emisor_del_pdf)
                with open(ruta, "rb") as archivo:
                    contenido = archivo.read()
            except Exception as e:
                raise HTTPException(500, f"Error generando borrador: {e}") from e

        return Response(
            contenido,
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="borrador.pdf"'},
        )

    @router.post("")
    def crear(payload: FacturaPayload, usuario: dict = Depends(usuario_actual)):
        cliente = _resolve_cliente(payload)
        if not cliente["client_name"]:
            raise HTTPException(422, "El nombre/razón social del cliente es requerido.")
        exigir_tipo_valido_para_el_receptor(payload.tipo, cliente["client_iva"])
        # Un emisor que no existe es un 422 antes de cualquier otra cosa: después
        # se pediría un número con un par que no hay.
        _config_del_emisor(payload.emisor_id)

        items = [
            {
                "description": i.description.strip(), "qty": i.qty,
                "unit_price": i.unit_price,
                "subtotal": round(i.qty * i.unit_price, 2),
            }
            for i in payload.items
            if i.description.strip()
        ]
        if not items:
            raise HTTPException(422, "Debe agregar al menos un ítem válido.")

        tax_rate = 0.0 if payload.tipo in tipos_cbte.C else payload.tax_rate
        totales = calcular_totales(items, tax_rate)

        # 🔑 Todo lo que una FCE exige se valida ANTES de pedir el número: el
        # número es fiscal y no se devuelve, y un comprobante numerado que ARCA
        # va a rechazar es un hueco en la correlatividad.
        fce_cbu, fce_transmision = _datos_de_fce(payload, cliente)

        numero, ta, arca = asyncio.run(get_next_numero_with_arca(
            payload.punto_venta, payload.tipo, payload.emisor_id
        ))
        factura_id = db_facturas.create_factura(
            emisor_id=payload.emisor_id,
            fce_cbu=fce_cbu, fce_transmision=fce_transmision,
            # 🔑 El ambiente con el que se emitió, que es lo que separa un
            # comprobante real de uno de prueba en el libro IVA.
            #
            # Sin ARCA configurado no hay CAE y el número es el de la propia
            # instancia: ese comprobante **es** el real del cliente, así que va
            # como `produccion`. No es un default silencioso — es la respuesta a
            # "¿contra qué se emitió?" cuando no se emitió contra nada.
            ambiente=arca_facturacion.ambiente_de(arca),
            tipo=payload.tipo, punto_venta=payload.punto_venta, numero=numero,
            fecha=payload.fecha, cliente_cuit=cliente["client_cuit"],
            cliente_razon=cliente["client_name"],
            cliente_iva_cond=IVA_CODES.get(cliente["client_iva"], 0), items=items,
            subtotal=totales["subtotal"], iva_amount=totales["iva_amount"],
            total=totales["total"], concepto=payload.concepto,
            observaciones=payload.observations.strip(),
            cliente_domicilio=cliente["client_address"],
            fch_serv_desde=payload.fch_serv_desde,
            fch_serv_hasta=payload.fch_serv_hasta,
            fch_vto_pago=payload.fch_vto_pago,
            condicion_venta=payload.condicion_venta, usuario_id=usuario["id"],
        )
        factura = db_facturas.get_factura(factura_id)
        factura = asyncio.run(solicitar_cae(factura_id, factura, ta, arca))

        pdf_path = pdf_gen.generate_pdf_factura(factura, resolvedor=emisor_del_pdf)
        db_facturas.update_factura_pdf_path(factura_id, pdf_path)

        # A crédito, el comprobante entra como débito a la cuenta del cliente:
        # la plata no entró, y la deuda tiene que quedar registrada en algún lado.
        if payload.condicion_venta == "Cuenta Corriente":
            pv_str = str(payload.punto_venta).zfill(4)
            num_str = str(numero).zfill(8)
            # ⚠️ **El `concepto` dice "Factura" dos veces** —queda
            # `"Factura Factura C 0001-00000001 — Juan"`— porque la etiqueta ya
            # trae la palabra. Está **preservado a propósito**: es el texto que
            # hoy tienen los movimientos de cuenta corriente de Contalibra y
            # Restolibra, y la extracción no puede cambiar lo que se escribe en
            # la base. Arreglarlo es una decisión aparte, y hay que decidir
            # también qué pasa con las filas viejas: si se corrige sólo de acá
            # en adelante, el extracto de un cliente muestra los dos formatos.
            tipo_label = TIPO_LABEL.get(payload.tipo, "Factura")
            db_caja.create_caja_movimiento(
                fecha=payload.fecha, tipo="ingreso",
                concepto=(
                    f"Factura {tipo_label} {pv_str}-{num_str} — "
                    f"{cliente['client_name']}"
                ),
                monto=totales["total"], referencia="", factura_id=factura_id,
                medio_pago="Cuenta Corriente", usuario_id=usuario["id"],
            )

        # 🔑 El hook va al final y **no puede tumbar la request**: acá el
        # comprobante ya existe y ARCA ya lo autorizó, así que un error no lo
        # desharía — dejaría al operador creyendo que no se emitió.
        if al_emitir is not None:
            try:
                al_emitir(factura_id, payload.model_dump(), usuario)
            except Exception:
                logger.exception(
                    "El hook post-emisión falló para la factura %s. El "
                    "comprobante está emitido y autorizado; lo que quedó sin "
                    "hacer se resuelve a mano.",
                    factura_id,
                )

        # 🔴 **El comprobante PELADO, no `_detalle(...)`.** Es lo que devolvían
        # las dos copias, y la pantalla de alta hace
        # `navigate(`/facturas/${factura.id}`)` con esto: envuelto en el detalle,
        # `factura.id` queda `undefined` y el usuario aterriza en
        # `/facturas/undefined` justo después de emitir.
        #
        # Se descubrió al migrar Contalibra (2026-08-27) comparando la forma de
        # la respuesta antes y después. **Ninguna de las dos suites lo cubría**,
        # así que el cambio habría llegado a producción; el test de más abajo lo
        # fija para que no vuelva a pasar.
        return db_facturas.get_factura(factura_id)

    # ── Un comprobante ────────────────────────────────────────────────────

    @router.get("/{factura_id}")
    def detalle(factura_id: int):
        return _detalle(_exigir(factura_id))

    @router.post("/{factura_id}/duplicar")
    def duplicar(factura_id: int):
        """Un borrador para emitir una copia, con las fechas recalculadas a hoy.

        No emite nada: la pantalla prefillea el formulario de alta con esto.
        """
        return armar_borrador(_exigir(factura_id))

    @router.post("/{factura_id}/autorizar")
    def autorizar(factura_id: int):
        """Reintenta el CAE de un comprobante que quedó sin autorizar.

        🔑 **Lo que se reintenta es el CAE, no la emisión.** El comprobante ya
        existe y tiene número; volver a emitirlo sería un segundo comprobante
        por el mismo hecho. Si ya tiene CAE, esto no hace nada y devuelve el
        detalle.
        """
        factura = _exigir(factura_id)
        if factura.get("cae"):
            return _detalle(factura)
        _exigir_vigente(factura)

        # El par del emisor con el que se numeró: otro CUIT no puede autorizarlo.
        arca = _config_del_emisor(factura.get("emisor_id"))
        # 🔑 Una llamada: elegir el par del ambiente y resolver dónde está en
        # disco son dos decisiones encadenadas, y separarlas hace que la segunda
        # deshaga a la primera. Ver `arca_credenciales`.
        cert_path, clave_path = arca_credenciales.paths_en_disco(arca)
        if not arca or not cert_path or not clave_path:
            raise HTTPException(
                400,
                "ARCA no está configurado. Cargá los certificados en Configuración.",
            )
        try:
            ta = asyncio.run(arca_wsaa.autenticar(cert_path, clave_path, arca["ambiente"]))
            cae_data = asyncio.run(arca_wsfe.solicitar_cae(
                factura, arca["cuit"], ta["token"], ta["sign"], arca["ambiente"]
            ))
            db_facturas.update_factura_cae(
                factura_id, cae_data["cae"], cae_data["cae_vto"]
            )
            factura = db_facturas.get_factura(factura_id)
            pdf_path = pdf_gen.generate_pdf_factura(factura, resolvedor=emisor_del_pdf)
            db_facturas.update_factura_pdf_path(factura_id, pdf_path)
            return _detalle(factura)
        except HTTPException:
            raise
        except Exception as e:
            # El motivo queda en el comprobante, no sólo en la respuesta de esta request.
            db_facturas.update_factura_cae_error(factura_id, str(e))
            # 502 y no 500: el que falló es ARCA, no esta aplicación.
            raise HTTPException(502, str(e)) from e

    @router.post("/{factura_id}/cobrar")
    def cobrar(
        factura_id: int, payload: CobroPayload,
        usuario: dict = Depends(usuario_actual),
    ):
        factura = _exigir(factura_id)
        _exigir_vigente(factura)
        # La lógica vive en `libracore.cobros`: el movimiento por pago, la
        # acreditación en cuenta corriente si el comprobante era a crédito, y el
        # rechazo de "cuenta corriente" como medio de cobro. Estaba duplicada
        # byte a byte, que es como los dos productos terminaron con el mismo bug.
        #
        # Un producto con caja por turno reemplaza esta escritura entera —ver
        # `registrar_cobro` en el docstring del factory—, porque el default no
        # sabe de turnos y dejaría la plata fuera del arqueo.
        try:
            if registrar_cobro is not None:
                registrar_cobro(
                    factura, payload.pagos, fecha=payload.fecha or None,
                    caja_id=payload.caja_id, usuario=usuario,
                )
            else:
                registrar_cobro_factura(
                    factura, payload.pagos, fecha=payload.fecha or None,
                    caja_id=payload.caja_id, usuario_id=usuario["id"],
                )
        except MedioNoEsDeCobro as exc:
            raise HTTPException(400, str(exc)) from exc
        return _detalle(db_facturas.get_factura(factura_id))

    _registrar_enviar_email(
        router, exigir=_exigir, emisor_del_pdf=emisor_del_pdf, smtp_config=smtp_config,
        donde_configurar_smtp=donde_configurar_smtp,
    )

    @router.delete("/{factura_id}", dependencies=admin)
    def eliminar(factura_id: int):
        """Borra un comprobante que **todavía no tiene CAE**.

        🔴 Con CAE emitido no se borra y no es una preferencia: ese número ya
        existe ante ARCA, y hacerlo desaparecer de la base deja un salto en la
        numeración que no se puede explicar. Lo que corresponde es una nota de
        crédito.
        """
        factura = _exigir(factura_id)
        if factura.get("cae") and factura["cae"] != "PENDIENTE":
            raise HTTPException(
                400,
                "No se puede eliminar una factura con CAE ya emitido por ARCA — "
                "use nota de crédito/débito.",
            )
        db_facturas.delete_factura(factura_id)
        return {"ok": True}

    @router.post("/{factura_id}/anular", dependencies=admin)
    def anular(factura_id: int, payload: AnularIn | None = None, usuario: dict = Depends(usuario_actual)):
        """Anula un comprobante **sin CAE** y lo deja en la base, con quién, cuándo y por qué (ADR-022).

        Es la alternativa al `DELETE` para quien no quiere que un número
        desaparezca. El comprobante sale de los libros, los totales y la cuenta
        corriente, y se sigue viendo en el listado con su marca.
        """
        try:
            factura = db_facturas.anular_factura(
                factura_id, usuario_id=usuario["id"], motivo=payload.motivo if payload else "")
        except db_facturas.ComprobanteNoAnulable as e:
            raise HTTPException(_STATUS_DE_ANULACION[e.codigo], str(e)) from None
        return _detalle(factura)

    # ── Notas ─────────────────────────────────────────────────────────────

    _registrar_nota_de_credito(
        router, usuario_actual=usuario_actual, solo_admin=solo_admin, emisor_del_pdf=emisor_del_pdf)

    @router.post("/{factura_id}/nota-debito", dependencies=admin)
    def nota_debito(factura_id: int, usuario: dict = Depends(usuario_actual)):
        orig = _exigir(factura_id)
        _exigir_vigente(orig)
        nd_tipo = TIPO_ND.get(orig["tipo"])
        if not nd_tipo:
            raise HTTPException(400, "Tipo de comprobante no admite nota de débito")
        nota_id = asyncio.run(_crear_nota(orig, nd_tipo, "Referencia", usuario["id"], emisor_del_pdf))
        return db_facturas.get_factura(nota_id)

    return router
