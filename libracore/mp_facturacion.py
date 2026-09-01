"""Qué hacer cuando un pago de MercadoPago tiene que convertirse en factura.

Es el paso que comparten **los cuatro caminos** por los que un pago de MP puede
terminar en un comprobante fiscal:

1. El **webhook** automático, cuando el cliente resuelto tiene `auto_facturar`.
2. El botón *Facturar* sobre un pago pendiente de la bandeja.
3. El botón *Facturar* sobre una transferencia bancaria entrante.
4. El **cron nocturno**, que emite la mayoría de las facturas y corre sin nadie
   mirando.

> 🔴 **Los cuatro pasan por acá, y esa es la razón de que este módulo exista.**
> La lista decía "3" hasta el 2026-08-04 en la documentación de la familia, el
> cron quedó fuera del cambio que introdujo los alias, y siguió resolviendo el
> cliente a mano durante tres semanas: dos facturas emitidas contra ARCA al CUIT
> equivocado (RIPEHO 2026-07-10, VISCO 2026-08-03).

Migrado desde `app/mp_facturacion.py` de Contalibra al normalizar la
facturación electrónica de la suite. La copia de Restolibra **no** era
equivalente: resolvía el cliente con `get_client_by_email()` en vez de
`resolver_cliente_pago()`, o sea sin mirar los alias. Tenía la función
importada en su shim y no la llamaba en ningún lado. Esa copia se va; queda
ésta.

## Por qué el alias no es un lujo para casos raros

El match directo **no es un empate: elige el cliente más nuevo.**
`get_client_by_email` ordena `activo DESC, id DESC`, así que ante dos clientes
con el mismo email gana el de id más alto — y el de id más alto suele ser
justamente el placeholder que crea el fallback de acá abajo cuando un pago no
matchea: razón social = el email, sin CUIT, "Consumidor Final". El sistema
fabrica el duplicado que después envenena su propio match, y el alias es lo
único que lo desempata bien.
"""

import calendar
import datetime
import logging

from libracore import (
    arca_facturacion,
    config_manager,
    email_sender,
    pdf_generator as pdf_gen,
)
from libracore.registro_de_clientes import RegistroDeClientes, el_registro
from libracore.db import arca_config as db_arca_config
from libracore.db import caja as db_caja
from libracore.db import facturas as db_facturas
from libracore.db import mp as db_mp

logger = logging.getLogger(__name__)

#: Qué tipo de comprobante emite el negocio según su propia condición frente al
#: IVA. Un monotributista emite C (11); un responsable inscripto o exento, B (6).
TIPO_POR_CONDICION = {
    "Monotributista":        11,
    "IVA Exento":            6,
    "Responsable Inscripto": 6,
}
TIPO_LABEL = {1: "Factura A", 6: "Factura B", 11: "Factura C"}

#: Condición del **receptor** en el código que exige ARCA.
IVA_CODES = {
    "Responsable Inscripto": 1,
    "IVA Responsable Inscripto": 1,
    "Monotributista": 6,
    "Responsable Monotributo": 6,
    "IVA Exento": 4,
    "Consumidor Final": 5,
    "No Alcanzado": 3,
    "IVA No Responsable": 3,
}

#: Cómo se llama en el comprobante el medio con el que MercadoPago cobró.
CONDICION_POR_PAYMENT_TYPE = {
    "bank_transfer":    "Transferencia Bancaria",
    "credit_card":      "Tarjeta de Crédito",
    "debit_card":       "Tarjeta de Débito",
    "account_money":    "Otros medios de pago electrónico",
    "digital_wallet":   "Otros medios de pago electrónico",
    "digital_currency": "Otros medios de pago electrónico",
    "prepaid_card":     "Tarjeta de Crédito",
}


def _fechas_de_servicio_del_mes():
    """Del 1 al último día del mes en curso, más la fecha de hoy como
    vencimiento de pago. WSFE las exige cuando el concepto es Servicios."""
    hoy = datetime.date.today()
    ultimo = calendar.monthrange(hoy.year, hoy.month)[1]
    return (
        hoy.replace(day=1).isoformat(),
        hoy.replace(day=ultimo).isoformat(),
        hoy.isoformat(),
    )


def _importes(monto: float, tipo: int, cfg: dict) -> tuple[float, float, float]:
    """Neto, IVA y total. El monto que llega de MP es **el total cobrado**.

    En Factura C el IVA va siempre en cero y todo el importe es neto — no es
    una simplificación, es lo que ARCA exige para los tipos 11/12/13.
    """
    try:
        iva_rate = float(cfg.get("mp_iva_rate", "0") or "0")
    except ValueError:
        iva_rate = 0.0

    if tipo == 11 or iva_rate == 0:
        return round(monto, 2), 0.0, round(monto, 2)
    subtotal = round(monto / (1 + iva_rate), 2)
    return subtotal, round(monto - subtotal, 2), round(monto, 2)


def resolver_cliente(
    payer_email: str,
    payer_name: str,
    payer_cuit: str = "",
    registro: RegistroDeClientes | None = None,
) -> dict:
    """A quién facturarle este pago. **Punto único** para los cuatro caminos.

    El `registro` decide: primero el alias explícito, después el match directo.
    Y recién si no hay ninguno se crea un cliente nuevo. Ningún llamador
    resuelve el cliente por su cuenta — es la regla que se rompió una vez y
    costó dos comprobantes.

    Sin `registro` usa el de LibraCore, que es lo que hacían Contalibra y
    Restolibra: para ellos no cambia nada.
    """
    registro = el_registro(registro)
    client = registro.resolver(payer_email, payer_cuit)
    if client:
        return client
    return registro.crear(
        nombre=payer_name or payer_email or "Sin nombre",
        email=payer_email,
        iva_condition="Consumidor Final",
    )


async def generar_factura_mp(
    monto: float,
    payer_email: str,
    payer_name: str,
    referencia: str,
    cfg: dict,
    concepto_override: str = "",
    cliente_override: dict | None = None,
    payment_type: str = "",
    payer_cuit: str = "",
    registro: RegistroDeClientes | None = None,
) -> tuple[int, str, str, bool]:
    """Crea la factura con CAE, el PDF y el movimiento de caja; manda el mail si
    hay SMTP configurado.

    Devuelve `(factura_id, "0005-00000012", "Factura C", email_enviado)`.

    Con `cliente_override` se usa ese cliente y no se resuelve nada — es el caso
    del botón *Facturar* sobre un pago que el operador ya vinculó a mano.
    """
    client = cliente_override or resolver_cliente(
        payer_email, payer_name, payer_cuit, registro
    )

    iva_cond = cfg.get("empresa_iva_condition", "Monotributista")
    tipo = TIPO_POR_CONDICION.get(iva_cond, 11)
    subtotal, iva_amount, total = _importes(monto, tipo, cfg)

    descripcion = (
        concepto_override
        or cfg.get("mp_concepto_descripcion", "")
        or "Cobro con Mercadopago"
    )
    items = [{"description": descripcion, "qty": 1,
              "unit_price": subtotal, "subtotal": subtotal}]

    fecha_hoy = datetime.date.today().isoformat()
    fch_desde, fch_hasta, fch_vto = _fechas_de_servicio_del_mes()

    # El punto de venta sale de la config de ARCA y hace falta ANTES de pedir el
    # número, así que se lee acá aunque `arca_facturacion` vuelva a leerla.
    configs = db_arca_config.obtener_todas_arca_configs()
    punto_venta = configs[0]["punto_venta"] if configs else 1

    # 🔑 La numeración y el CAE los pone `arca_facturacion`, que es el mismo
    # camino que usa la facturación manual de los seis productos. Antes esto
    # repetía el diálogo con WSAA/WSFE por su cuenta, con lo cual el fallback a
    # numeración local existía en dos versiones que podían divergir.
    numero, ta, arca = await arca_facturacion.get_next_numero_with_arca(punto_venta, tipo)

    factura_id = db_facturas.create_factura(
                # 🔑 El ambiente con el que se emitió, que es lo que separa un
        # comprobante real de uno de prueba en el libro IVA.
        #
        # Sin ARCA configurado no hay CAE y el número es el de la propia
        # instancia: ese comprobante **es** el real del cliente, así que va
        # como `produccion`. No es un default silencioso — es la respuesta a
        # "¿contra qué se emitió?" cuando no se emitió contra nada.
        ambiente=arca_facturacion.ambiente_de(arca),
        tipo=tipo, punto_venta=punto_venta, numero=numero,
        fecha=fecha_hoy,
        cliente_cuit=client.get("cuit_dni", ""),
        cliente_razon=client["name"],
        cliente_iva_cond=IVA_CODES.get(client.get("iva_condition", "Consumidor Final"), 5),
        items=items,
        subtotal=subtotal,
        iva_amount=iva_amount,
        total=total,
        concepto=2,
        observaciones=f"Pago MercadoPago {referencia}",
        cliente_domicilio=client.get("address", ""),
        fch_serv_desde=fch_desde,
        fch_serv_hasta=fch_hasta,
        fch_vto_pago=fch_vto,
        condicion_venta=CONDICION_POR_PAYMENT_TYPE.get(
            payment_type, "Otros medios de pago electrónico"
        ),
    )
    factura = db_facturas.get_factura(factura_id)
    factura = await arca_facturacion.solicitar_cae(factura_id, factura, ta, arca)

    pdf_path = None
    try:
        pdf_path = pdf_gen.generate_pdf_factura(factura)
        db_facturas.update_factura_pdf_path(factura_id, pdf_path)
        factura = db_facturas.get_factura(factura_id)
    except Exception as e:
        # El PDF no bloquea: la factura ya tiene CAE y existe en ARCA. Se
        # regenera desde la pantalla.
        logger.error("Error PDF factura MP %s: %s", factura_id, e)

    pv_str = str(punto_venta).zfill(4)
    # ⚠️ `factura["numero"]`, no la variable local `numero`: `create_factura()`
    # puede haber reintentado con un número distinto si el original chocó contra
    # `idx_facturas_numero_unico`. Con la variable local, el movimiento de caja
    # y el mail nombran un comprobante que no es el que se emitió.
    num_str = str(factura["numero"]).zfill(8)
    tipo_lb = TIPO_LABEL.get(tipo, "Factura")
    etiqueta = f"{tipo_lb} {pv_str}-{num_str}"

    db_caja.create_caja_movimiento(
        fecha=fecha_hoy,
        tipo="ingreso",
        concepto=f"Cobro {etiqueta} — {client['name']} (MP)",
        monto=total,
        referencia=referencia,
        factura_id=factura_id,
    )

    enviado = _mandar_por_mail(cfg, client, payer_email, pdf_path, etiqueta, total)
    return factura_id, f"{pv_str}-{num_str}", tipo_lb, enviado


def _mandar_por_mail(cfg, client, payer_email, pdf_path, etiqueta, total) -> bool:
    """El comprobante al cliente. Devuelve si se mandó.

    El email del **cliente registrado** manda sobre el del pagador: son
    distintos justamente cuando alguien paga desde otra cuenta, que es el caso
    para el que existen los alias.
    """
    smtp_host = cfg.get("email_smtp_host", "")
    smtp_user = cfg.get("email_smtp_user", "")
    smtp_pass = cfg.get("email_smtp_password", "")
    from_email = cfg.get("email_from", "")
    to_email = client.get("email") or payer_email

    if not (smtp_host and smtp_user and smtp_pass and from_email and to_email and pdf_path):
        return False

    try:
        email_sender.enviar_comprobante(
            to_email=to_email,
            to_name=client["name"],
            pdf_path=pdf_path,
            empresa_nombre=cfg.get("empresa_nombre", ""),
            factura_label=etiqueta,
            total=total,
            smtp_host=smtp_host,
            smtp_port=int(cfg.get("email_smtp_port", "587") or "587"),
            smtp_user=smtp_user,
            smtp_password=smtp_pass,
            from_email=from_email,
            from_name=cfg.get("email_from_name", ""),
        )
        logger.info("Email enviado a %s para %s", to_email, etiqueta)
        return True
    except Exception as e:
        # Que no salga el mail no invalida la factura: ya tiene CAE.
        logger.error("Error email a %s: %s", to_email, e)
        return False


def cargar_config() -> dict:
    """Atajo para los llamadores que sólo necesitan la config de la instancia."""
    return config_manager.load()
