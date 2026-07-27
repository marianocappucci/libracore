"""
Resumen automático de cuenta corriente por email.

Envío periódico y opcional del detalle de cuenta corriente a cada cliente,
con un toggle por cliente (`clients.cc_resumen_auto`) y una frecuencia propia
(`clients.cc_resumen_frecuencia`: semanal | quincenal | mensual). La llave
maestra del sistema y los parámetros comunes viven en `config.json`
(`cc_resumen_*`, ver `config_manager.DEFAULTS`).

El disparo real no vive acá: es un script por producto ejecutado a diario por
cron en el host (mismo patrón que `scripts/sync_mp_auto.py` de Contalibra),
que llama a `enviar_resumenes_pendientes()`. Este módulo decide a quién le
toca hoy, arma el PDF y manda el mail; es agnóstico de cómo se lo invoque.

La idempotencia se apoya en `clients.cc_resumen_ultimo_envio`: una segunda
corrida el mismo día no reenvía nada, y si el contenedor estuvo caído el día
del corte, el envío se recupera en la siguiente corrida (ver `_ancla`).
"""
import datetime
import logging

from libracore import config_manager, email_sender, pdf_generator
from libracore.db.clients import get_client, get_clients_cc_resumen_auto
from libracore.db.cuenta_corriente import (
    get_cc_movimientos_periodo,
    registrar_resumen_enviado,
)

logger = logging.getLogger(__name__)

FRECUENCIAS = ("semanal", "quincenal", "mensual")

_CUERPO_DEFAULT = (
    "Estimado/a {cliente},\n\n"
    "Le enviamos el resumen de su cuenta corriente al {hasta}.\n\n"
    "Saldo actual: $ {saldo}\n"
    "Período informado: {desde} a {hasta}\n\n"
    "El detalle de movimientos va adjunto en PDF.\n\n"
    "Ante cualquier consulta, quedamos a disposición.\n{empresa}"
)


def _ar(value) -> str:
    return pdf_generator._ar(value)


def _fecha(s: str) -> str:
    return pdf_generator._fmt_fecha(s)


def _parse_fecha(s: str):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _ancla(frecuencia: str, hoy: datetime.date, dia_mes: int, dia_semana: int):
    """Fecha del último corte que ya debería haberse enviado, o None si todavía
    no hubo ninguno en este ciclo.

    Trabajar contra un ancla (en vez de exigir que `hoy` sea exactamente el día
    del corte) es lo que hace que un día de cron perdido no saltee el envío:
    mientras el último envío sea anterior al ancla vigente, el resumen sale en
    la próxima corrida.
    """
    if frecuencia == "mensual":
        dia = max(1, min(28, int(dia_mes or 1)))
        ancla = hoy.replace(day=dia)
        if ancla > hoy:  # todavía no llegamos al corte de este mes
            mes_ant = (hoy.replace(day=1) - datetime.timedelta(days=1))
            ancla = mes_ant.replace(day=dia)
        return ancla

    dia = max(1, min(7, int(dia_semana or 1)))
    delta = (hoy.isoweekday() - dia) % 7
    return hoy - datetime.timedelta(days=delta)


def corresponde_enviar(cliente: dict, hoy: datetime.date, cfg: dict) -> bool:
    """¿Le toca hoy a este cliente? No mira el saldo (eso lo decide
    `enviar_resumenes_pendientes` según `cc_resumen_solo_con_saldo`)."""
    if not cliente.get("cc_resumen_auto"):
        return False

    frecuencia = (cliente.get("cc_resumen_frecuencia") or "mensual").strip()
    if frecuencia not in FRECUENCIAS:
        frecuencia = "mensual"

    ancla = _ancla(
        frecuencia, hoy,
        int(cfg.get("cc_resumen_dia_mes", 1) or 1),
        int(cfg.get("cc_resumen_dia_semana", 1) or 1),
    )
    ultimo = _parse_fecha(cliente.get("cc_resumen_ultimo_envio"))

    if ultimo is None:
        return hoy >= ancla
    if ultimo >= ancla:
        return False
    if frecuencia == "quincenal":
        # Con anclas semanales, exigir 13 días desde el último envío deja el
        # envío una semana por medio sin depender de qué semana del mes sea.
        return (hoy - ultimo).days >= 13
    return True


def calcular_periodo(cliente: dict, hoy: datetime.date, frecuencia: str = "") -> dict:
    """Rango a informar: desde el día siguiente al último envío (o el arranque
    del ciclo si nunca se envió) hasta hoy. El corte en `hoy` es a propósito:
    así el saldo final del resumen es el saldo real al momento del envío."""
    frecuencia = (frecuencia or cliente.get("cc_resumen_frecuencia") or "mensual").strip()
    ultimo = _parse_fecha(cliente.get("cc_resumen_ultimo_envio"))
    if ultimo:
        desde = ultimo + datetime.timedelta(days=1)
    elif frecuencia == "semanal":
        desde = hoy - datetime.timedelta(days=7)
    elif frecuencia == "quincenal":
        desde = hoy - datetime.timedelta(days=15)
    else:
        primero = hoy.replace(day=1)
        desde = (primero - datetime.timedelta(days=1)).replace(day=1)
    if desde > hoy:
        desde = hoy
    return get_cc_movimientos_periodo(cliente["id"], desde.isoformat(), hoy.isoformat())


def _render(plantilla: str, cliente: dict, periodo: dict, empresa: str) -> str:
    return plantilla.format(
        cliente=cliente.get("name", ""),
        empresa=empresa,
        saldo=_ar(periodo["saldo_final"]),
        desde=_fecha(periodo["desde"]),
        hasta=_fecha(periodo["hasta"]),
    )


def enviar_resumen(cliente_id: int, hoy: datetime.date | None = None,
                   cfg: dict | None = None, automatico: bool = True) -> dict:
    """Genera y envía el resumen de un cliente. Deja rastro en
    `cc_resumenes_enviados` tanto si sale bien como si falla.

    Devuelve `{"ok": bool, "motivo"|"detalle": str, ...}` en vez de propagar la
    excepción: el envío masivo no puede cortarse porque falle un cliente.
    """
    hoy = hoy or datetime.date.today()
    cfg = cfg if cfg is not None else config_manager.load()

    cliente = get_client(cliente_id)
    if not cliente:
        return {"ok": False, "motivo": "cliente_inexistente"}

    email = (cliente.get("email") or "").strip()
    if not email:
        return {"ok": False, "motivo": "sin_email", "cliente": cliente.get("name", "")}

    if not (cfg.get("email_smtp_host") and cfg.get("email_smtp_user")):
        return {"ok": False, "motivo": "smtp_no_configurado"}

    periodo = calcular_periodo(cliente, hoy)
    periodo["emitido"] = hoy.isoformat()
    empresa = cfg.get("empresa_nombre", "")

    asunto = _render(
        cfg.get("cc_resumen_asunto") or "Resumen de cuenta corriente - {empresa}",
        cliente, periodo, empresa)
    cuerpo = _render(cfg.get("cc_resumen_cuerpo") or _CUERPO_DEFAULT,
                     cliente, periodo, empresa)

    try:
        pdf_path = pdf_generator.generate_pdf_resumen_cc(cliente, periodo)
        email_sender.enviar_documento(
            to_email=email,
            to_name=cliente.get("name", ""),
            pdf_path=pdf_path,
            asunto=asunto,
            cuerpo=cuerpo,
            smtp_host=cfg["email_smtp_host"],
            smtp_port=int(cfg.get("email_smtp_port", 587) or 587),
            smtp_user=cfg["email_smtp_user"],
            smtp_password=cfg.get("email_smtp_password", ""),
            from_email=cfg.get("email_from") or cfg["email_smtp_user"],
            from_name=cfg.get("email_from_name", ""),
            filename=f"resumen-cuenta-{hoy.isoformat()}.pdf",
        )
    except Exception as e:  # noqa: BLE001 — se registra y se sigue con el resto
        logger.exception("Falló el resumen de cuenta corriente del cliente %s", cliente_id)
        registrar_resumen_enviado(
            cliente_id, hoy.isoformat(), periodo["desde"], periodo["hasta"],
            periodo["saldo_final"], email, estado="error",
            detalle=f"{type(e).__name__}: {e}", automatico=automatico,
        )
        return {"ok": False, "motivo": "error_envio", "detalle": str(e),
                "cliente": cliente.get("name", "")}

    registrar_resumen_enviado(
        cliente_id, hoy.isoformat(), periodo["desde"], periodo["hasta"],
        periodo["saldo_final"], email, estado="ok", automatico=automatico,
    )
    return {
        "ok": True, "cliente": cliente.get("name", ""), "email": email,
        "desde": periodo["desde"], "hasta": periodo["hasta"],
        "saldo": periodo["saldo_final"], "pdf": pdf_path,
    }


def enviar_resumenes_pendientes(hoy: datetime.date | None = None,
                                dry_run: bool = False,
                                forzar: bool = False) -> dict:
    """Recorre los clientes con el toggle activo y envía a los que les toca hoy.

    `dry_run` lista a quién se le enviaría sin mandar nada (ni tocar la base);
    `forzar` ignora la frecuencia y el último envío (para pruebas y para el
    "enviar ahora" masivo).
    """
    hoy = hoy or datetime.date.today()
    cfg = config_manager.load()

    resultado = {"fecha": hoy.isoformat(), "enviados": [], "omitidos": [], "errores": []}

    if str(cfg.get("cc_resumen_habilitado", "0")) != "1" and not forzar:
        resultado["omitidos"].append({"motivo": "deshabilitado_en_config"})
        return resultado

    solo_con_saldo = str(cfg.get("cc_resumen_solo_con_saldo", "1")) == "1"

    for cliente in get_clients_cc_resumen_auto():
        nombre = cliente.get("name", "")
        if not forzar and not corresponde_enviar(cliente, hoy, cfg):
            resultado["omitidos"].append({"cliente": nombre, "motivo": "no_le_toca_hoy"})
            continue

        periodo = calcular_periodo(cliente, hoy)
        if solo_con_saldo and periodo["saldo_final"] <= 0:
            resultado["omitidos"].append({"cliente": nombre, "motivo": "sin_saldo"})
            continue
        if not (cliente.get("email") or "").strip():
            resultado["omitidos"].append({"cliente": nombre, "motivo": "sin_email"})
            continue

        if dry_run:
            resultado["enviados"].append({
                "cliente": nombre, "email": cliente.get("email"),
                "desde": periodo["desde"], "hasta": periodo["hasta"],
                "saldo": periodo["saldo_final"], "dry_run": True,
            })
            continue

        r = enviar_resumen(cliente["id"], hoy=hoy, cfg=cfg, automatico=True)
        if r.get("ok"):
            resultado["enviados"].append(r)
        else:
            resultado["errores"].append({"cliente": nombre, **r})

    return resultado
