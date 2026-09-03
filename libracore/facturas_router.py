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

import datetime
import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from libracore import arca_credenciales, arca_facturacion, arca_wsaa, arca_wsfe, config_manager, email_sender
from libracore import pdf_generator as pdf_gen
from libracore.arca_facturacion import get_next_numero_with_arca, solicitar_cae
from libracore.cobros import MedioNoEsDeCobro, registrar_cobro_factura
from libracore.db import arca_config as db_arca
from libracore.db import caja as db_caja
from libracore.db import clients as db_clients
from libracore.db import cuenta_corriente as db_cc
from libracore.db import facturas as db_facturas
from libracore.facturas_borrador import armar_borrador

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
TIPO_NC = {1: 3, 6: 8, 11: 13}
TIPO_ND = {1: 2, 6: 7, 11: 12}

TIPO_LABEL = {
    1: "Factura A", 6: "Factura B", 11: "Factura C",
    3: "Nota de Crédito A", 8: "Nota de Crédito B", 13: "Nota de Crédito C",
    2: "Nota de Débito A", 7: "Nota de Débito B", 12: "Nota de Débito C",
}

#: Los tres que son factura —y no nota—. Se usa para decidir si un comprobante
#: puede tener notas colgando y si se le pueden imputar cobros.
TIPOS_FACTURA = (1, 6, 11)

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
    resolver=None,
) -> None:
    """Manda el PDF con la config SMTP resuelta. Ver `smtp_efectivo`."""
    smtp = smtp_efectivo(resolver)
    email_sender.enviar_comprobante(
        to_email=to_email, to_name=to_name, pdf_path=pdf_path,
        empresa_nombre=config_manager.load().get("empresa_nombre", ""),
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


class CobroPayload(BaseModel):
    fecha: str = ""
    caja_id: int | None = None
    #: `[{medio_id, monto, referencia}]`
    pagos: list[dict]


class EmailPayload(BaseModel):
    email: str


def _tipos_emisor() -> list[dict]:
    cfg = config_manager.load()
    return TIPOS_POR_CONDICION.get(
        cfg.get("empresa_iva_condition", "Monotributista"), TIPOS_DEFAULT
    )


def _arca_punto_venta() -> int:
    configs = db_arca.obtener_todas_arca_configs()
    return configs[0].get("punto_venta", 1) if configs else 1


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


def _numero(factura: dict) -> str:
    return f"{str(factura['punto_venta']).zfill(4)}-{str(factura['numero']).zfill(8)}"


def _detalle(factura: dict) -> dict:
    """El comprobante con todo lo que le cuelga: sus notas, su original y sus cobros."""
    es_factura = factura["tipo"] in TIPOS_FACTURA
    ncs = nds = []
    if es_factura:
        ncs = db_facturas.get_nc_de_factura(
            factura["tipo"], factura["punto_venta"], factura["numero"]
        )
        nds = db_facturas.get_nd_de_factura(
            factura["tipo"], factura["punto_venta"], factura["numero"]
        )

    factura_original = None
    if factura.get("cbte_asoc_tipo") and factura.get("cbte_asoc_nro"):
        factura_original = db_facturas.get_factura_por_tipo_pv_nro(
            factura["cbte_asoc_tipo"], factura["cbte_asoc_pv"], factura["cbte_asoc_nro"],
        )

    cobros = db_caja.get_cobros_factura(factura["id"]) if es_factura else []
    total_cobrado = sum(c["monto"] for c in cobros)
    # `max(0, ...)`: un cobro de más no puede mostrarse como pendiente negativo.
    pendiente = (
        max(0.0, round(factura["total"] - total_cobrado, 2)) if es_factura else 0.0
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


async def _crear_nota(
    orig: dict, nuevo_tipo: int, obs_prefijo: str, usuario_id: int,
) -> int:
    """Emite una nota que referencia al comprobante original.

    🔑 **La nota copia los ítems y los importes del original tal cual.** Una NC
    que anula tiene que decir exactamente lo mismo que anula: si los recalculara
    —con la tasa de hoy, o con un precio que cambió— anularía un importe distinto
    del que se facturó, y ante ARCA quedarían dos comprobantes que no cierran.

    `cbte_asoc_*` es lo que ata la nota a su factura ante ARCA; sin eso es un
    comprobante suelto.
    """
    fecha_hoy = datetime.date.today().isoformat()
    punto_venta = orig["punto_venta"]
    numero, ta, arca = await get_next_numero_with_arca(punto_venta, nuevo_tipo)

    nota_id = db_facturas.create_factura(
        # 🔑 El ambiente con el que se emitió, que es lo que separa un
        # comprobante real de uno de prueba en el libro IVA.
        #
        # Sin ARCA configurado no hay CAE y el número es el de la propia
        # instancia: ese comprobante **es** el real del cliente, así que va
        # como `produccion`. No es un default silencioso — es la respuesta a
        # "¿contra qué se emitió?" cuando no se emitió contra nada.
        ambiente=arca_facturacion.ambiente_de(arca),
        tipo=nuevo_tipo, punto_venta=punto_venta, numero=numero, fecha=fecha_hoy,
        cliente_cuit=orig["cliente_cuit"], cliente_razon=orig["cliente_razon"],
        cliente_iva_cond=orig.get("cliente_iva_cond") or 0, items=orig["items"],
        subtotal=orig["subtotal"], iva_amount=orig["iva_amount"], total=orig["total"],
        concepto=orig.get("concepto", 1),
        observaciones=(
            f"{obs_prefijo} {TIPO_LABEL.get(orig['tipo'], 'comprobante')} {_numero(orig)}"
        ),
        cliente_domicilio=orig.get("cliente_domicilio", ""),
        fch_serv_desde=orig.get("fch_serv_desde", ""),
        fch_serv_hasta=orig.get("fch_serv_hasta", ""),
        fch_vto_pago=fecha_hoy,
        cbte_asoc_tipo=orig["tipo"], cbte_asoc_pv=orig["punto_venta"],
        cbte_asoc_nro=orig["numero"], usuario_id=usuario_id,
    )
    nota = db_facturas.get_factura(nota_id)
    nota = await solicitar_cae(nota_id, nota, ta, arca)
    pdf_path = pdf_gen.generate_pdf_factura(nota)
    db_facturas.update_factura_pdf_path(nota_id, pdf_path)
    return nota_id


def build_comprobantes_router(
    *,
    usuario_actual: Callable[..., Any],
    solo_admin: Callable[..., Any],
    prefix: str = "/api/facturas",
    al_emitir: Callable[[int, dict, dict], None] | None = None,
    registrar_cobro: Callable[..., None] | None = None,
    donde_configurar_smtp: str = "Configuración → Email",
    smtp_config: Callable[[], Any] | None = None,
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
    def tipos(usuario: dict = Depends(usuario_actual)):
        """Qué puede emitir este emisor, y con qué opciones. Lo lee el formulario.

        🔑 **El `punto_venta` que devuelve es el del POS donde está parado quien
        pregunta**, no el de la empresa: sale de la caja de su turno abierto. Un
        cliente con varios mostradores necesita numeración fiscal separada por
        mostrador, porque ARCA numera por (tipo, punto de venta).

        Si esa caja no tiene uno propio —o no hay turno abierto— cae al de la
        empresa, que es como funcionó siempre y es el caso de todas las
        instancias existentes.
        """
        tipos_emisor = _tipos_emisor()
        return {
            "tipos": tipos_emisor,
            "conceptos": CONCEPTOS,
            "condiciones_venta": CONDICIONES_VENTA,
            "punto_venta": (
                db_caja.resolver_punto_venta((usuario or {}).get("id"))
                or _arca_punto_venta()
            ),
            "es_monotributista": (
                len(tipos_emisor) == 1 and tipos_emisor[0]["value"] == 11
            ),
        }

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

    # ── Emisión ───────────────────────────────────────────────────────────

    @router.post("/borrador-pdf")
    async def borrador_pdf(payload: FacturaPayload):
        """El PDF de lo que se está por emitir, **sin guardar ni llamar a ARCA**.

        Es lo que deja mirar el comprobante antes de quemarle un número a la
        numeración fiscal, que no se puede devolver.
        """
        import tempfile

        cliente = _resolve_cliente(payload)
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
        tax_rate = 0.0 if payload.tipo == 11 else payload.tax_rate
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
        }

        # Carpeta temporal que se borra sola: un borrador no tiene por qué
        # sobrevivir a la request que lo pidió.
        with tempfile.TemporaryDirectory() as carpeta:
            try:
                ruta = pdf_gen.generate_pdf_factura(borrador, output_dir=carpeta)
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
    async def crear(payload: FacturaPayload, usuario: dict = Depends(usuario_actual)):
        cliente = _resolve_cliente(payload)
        if not cliente["client_name"]:
            raise HTTPException(422, "El nombre/razón social del cliente es requerido.")

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

        tax_rate = 0.0 if payload.tipo == 11 else payload.tax_rate
        totales = calcular_totales(items, tax_rate)

        numero, ta, arca = await get_next_numero_with_arca(
            payload.punto_venta, payload.tipo
        )
        factura_id = db_facturas.create_factura(
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
        factura = await solicitar_cae(factura_id, factura, ta, arca)

        pdf_path = pdf_gen.generate_pdf_factura(factura)
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
    async def autorizar(factura_id: int):
        """Reintenta el CAE de un comprobante que quedó sin autorizar.

        🔑 **Lo que se reintenta es el CAE, no la emisión.** El comprobante ya
        existe y tiene número; volver a emitirlo sería un segundo comprobante
        por el mismo hecho. Si ya tiene CAE, esto no hace nada y devuelve el
        detalle.
        """
        factura = _exigir(factura_id)
        if factura.get("cae"):
            return _detalle(factura)

        configs = db_arca.obtener_todas_arca_configs()
        arca = configs[0] if configs else None
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
            ta = await arca_wsaa.autenticar(cert_path, clave_path, arca["ambiente"])
            cae_data = await arca_wsfe.solicitar_cae(
                factura, arca["cuit"], ta["token"], ta["sign"], arca["ambiente"]
            )
            db_facturas.update_factura_cae(
                factura_id, cae_data["cae"], cae_data["cae_vto"]
            )
            factura = db_facturas.get_factura(factura_id)
            pdf_path = pdf_gen.generate_pdf_factura(factura)
            db_facturas.update_factura_pdf_path(factura_id, pdf_path)
            return _detalle(factura)
        except HTTPException:
            raise
        except Exception as e:
            # 502 y no 500: el que falló es ARCA, no esta aplicación.
            raise HTTPException(502, str(e)) from e

    @router.post("/{factura_id}/cobrar")
    def cobrar(
        factura_id: int, payload: CobroPayload,
        usuario: dict = Depends(usuario_actual),
    ):
        factura = _exigir(factura_id)
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

    @router.post("/{factura_id}/enviar-email")
    def enviar_email(factura_id: int, payload: EmailPayload):
        factura = _exigir(factura_id)
        if not smtp_configurado(smtp_config):
            raise HTTPException(
                400, f"Configurá el servidor SMTP en {donde_configurar_smtp}."
            )
        if not payload.email.strip():
            raise HTTPException(422, "Ingresá una dirección de email.")

        # Se regenera si el archivo no está: el PDF guardado puede haberse
        # perdido en un redeploy, y el comprobante se reconstruye entero desde
        # su fila.
        pdf_path = factura.get("pdf_path")
        if not pdf_path or not os.path.exists(pdf_path):
            pdf_path = pdf_gen.generate_pdf_factura(factura)

        etiqueta = (
            f"{pdf_gen._TIPO_LABELS.get(factura['tipo'], 'Comprobante')} "
            f"{_numero(factura)}"
        )
        try:
            enviar_comprobante_por_mail(
                to_email=payload.email.strip(), to_name=factura["cliente_razon"],
                pdf_path=pdf_path, factura_label=etiqueta, total=factura["total"],
                resolver=smtp_config,
            )
        except Exception as e:
            raise HTTPException(502, f"Error al enviar: {e}") from e
        return {"ok": True}

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

    # ── Notas ─────────────────────────────────────────────────────────────

    @router.post("/{factura_id}/nota-credito", dependencies=admin)
    async def nota_credito(factura_id: int, usuario: dict = Depends(usuario_actual)):
        orig = _exigir(factura_id)
        nc_tipo = TIPO_NC.get(orig["tipo"])
        if not nc_tipo:
            raise HTTPException(400, "Tipo de comprobante no admite nota de crédito")
        nota_id = await _crear_nota(orig, nc_tipo, "Anula", usuario["id"])

        # Si el original era a crédito, la deuda del cliente se cancela: quedó
        # anulada, y dejarla en la cuenta corriente sería cobrarle algo que ya
        # no debe.
        if orig.get("condicion_venta") == "Cuenta Corriente":
            cliente = db_clients.get_client_by_cuit(orig.get("cliente_cuit", ""))
            if cliente:
                db_cc.create_cc_pago(
                    cliente_id=cliente["id"], monto=orig["total"],
                    fecha=datetime.date.today().isoformat(),
                    concepto=(
                        f"NC {_numero(orig)} (anula "
                        f"{TIPO_LABEL.get(orig['tipo'], 'comprobante')} "
                        f"{_numero(orig)})"
                    ),
                    referencia="", medio_pago="Cuenta Corriente", caja_id=None,
                    usuario_id=usuario["id"],
                )
        return db_facturas.get_factura(nota_id)

    @router.post("/{factura_id}/nota-debito", dependencies=admin)
    async def nota_debito(factura_id: int, usuario: dict = Depends(usuario_actual)):
        orig = _exigir(factura_id)
        nd_tipo = TIPO_ND.get(orig["tipo"])
        if not nd_tipo:
            raise HTTPException(400, "Tipo de comprobante no admite nota de débito")
        nota_id = await _crear_nota(orig, nd_tipo, "Referencia", usuario["id"])
        return db_facturas.get_factura(nota_id)

    return router
