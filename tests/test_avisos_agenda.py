"""El adaptador de `libragenda.NotificationPort` (ADR-090): lo que reemplaza al `LoggingNotificationPort` de GestioLibra y MedLibra.

La notificación es la de `libragenda` (`ReminderNotification`): sólo ids, sin texto ni canal. El adaptador la resuelve con el
contacto y el texto que pone el producto. Lo que se fija: **un solo canal** (WhatsApp si se puede, mail si no, y mail si WhatsApp rechaza el número; `politica="todos"`
para los dos), no sale dos veces aunque `dispatch` repita ni aunque la configuración cambie entre dos corridas, un fallo no levanta
(cortaría la tanda de `ReminderDispatcher`) y queda reintentable, y un canal sin destino queda omitido.
"""
import dataclasses
from datetime import datetime, timedelta

import pytest

from libracore import fechas
from libracore.avisos import (
    ESPERA_ENTRE_INTENTOS,
    MAX_INTENTOS,
    Mensaje,
    ProveedorRegistro,
    Resultado,
    reintentar_fallidos,
)
from libracore.avisos.agenda import NotificacionPorAvisos, clave_de_recordatorio
from libracore.db import avisos_envios as db
from libracore.db import core
from libracore.db.schema import init_core_schema


@dataclasses.dataclass(frozen=True)
class Notificacion:
    """La forma de `libragenda.ReminderNotification` (el motor no la importa)."""

    appointment_id: str = "cita-7"
    policy_id: str = "24h"
    resource_id: str = "consultorio-1"
    service_id: str = "consulta"
    client_id: str = "paciente-3"
    starts_at: datetime = datetime(2026, 11, 3, 10, 30)


class Falso:
    def __init__(self, canal, *resultados, disponible=True):
        self.canal, self.simulado, self.nombre, self._disp = canal, False, f"falso-{canal}", disponible
        self._resultados, self.enviados = list(resultados), []

    def disponible(self):
        return self._disp

    def enviar(self, mensaje):
        self.enviados.append(mensaje)
        return self._resultados.pop(0) if self._resultados else Resultado.enviado("ok")


@pytest.fixture(autouse=True)
def _base(tmp_path, monkeypatch):
    monkeypatch.delenv("LIBRAEDGE_NODE_ID", raising=False)
    core.configure(db_path=str(tmp_path / "agenda.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield
    core._db_path = None
    core._database_url = None


def _redactar(n, canal):
    cuerpo = f"Recordatorio: tu turno es el {n.starts_at:%d/%m a las %H:%M}."
    return Mensaje("", asunto="Recordatorio de turno", cuerpo=cuerpo, plantilla="recordatorio_turno",
                   variables={"fecha": f"{n.starts_at:%d/%m}"})


def _puerto(whatsapp, email, contacto, **kw):
    return NotificacionPorAvisos({"whatsapp": whatsapp, "email": email}, contacto=lambda cid: contacto,
                                 redactar=_redactar, **kw)


CONTACTO = {"email": "paciente@ejemplo.example", "telefono": "11 5555-1234"}


def test_con_politica_todos_sale_por_cada_canal_con_destino_y_normaliza_el_telefono():
    wa, mail = Falso("whatsapp"), Falso("email")
    envios = _puerto(wa, mail, CONTACTO, politica="todos").send(Notificacion())

    assert [e.estado for e in envios] == ["enviado", "enviado"]
    assert wa.enviados[0].destino == "5491155551234" and mail.enviados[0].destino == "paciente@ejemplo.example"
    assert wa.enviados[0].plantilla == "recordatorio_turno" and dict(wa.enviados[0].variables) == {"fecha": "03/11"}
    claves = {(f["canal"], f["clave"], f["tipo"]) for f in db.listar()}
    assert claves == {("whatsapp", "agenda:cita-7:24h", "recordatorio"), ("email", "agenda:cita-7:24h", "recordatorio")}


def test_si_dispatch_repite_la_notificacion_no_sale_dos_veces():
    wa, mail = Falso("whatsapp"), Falso("email")
    puerto = _puerto(wa, mail, CONTACTO)
    puerto.send(Notificacion())
    puerto.send(Notificacion())
    assert len(wa.enviados) == 1 and mail.enviados == []
    todos = _puerto(wa, mail, CONTACTO, politica="todos", )
    todos.send(Notificacion(appointment_id="cita-8"))
    todos.send(Notificacion(appointment_id="cita-8"))
    assert len(wa.enviados) == 2 and len(mail.enviados) == 1
    # otra política de la misma cita es otro recordatorio
    puerto.send(Notificacion(policy_id="2h"))
    assert len(wa.enviados) == 3


def test_un_fallo_no_levanta_y_lo_reintenta_el_motor():
    wa = Falso("whatsapp", Resultado.fallido("Evolution respondió 503."))
    puerto = _puerto(wa, Falso("email", disponible=False), CONTACTO)

    (envio,) = puerto.send(Notificacion())  # no levanta: cortaría la tanda de ReminderDispatcher
    assert envio.estado == "fallido"

    assert reintentar_fallidos(wa).enviados == 0, "en el mismo cron no gasta otro intento: espera"
    resumen = reintentar_fallidos(wa, ahora=fechas.ahora() + ESPERA_ENTRE_INTENTOS + timedelta(seconds=30))
    assert resumen.enviados == 1 and len(wa.enviados) == 2
    assert db.listar()[0]["estado"] == "enviado"


def test_un_canal_sin_destino_queda_omitido_y_el_otro_sale():
    wa, mail = Falso("whatsapp"), Falso("email")
    envios = _puerto(wa, mail, {"email": "paciente@ejemplo.example", "telefono": "5555"}).send(Notificacion())

    assert [e.estado for e in envios] == ["omitido", "enviado"]
    assert wa.enviados == []
    omitido = db.obtener("whatsapp", "agenda:cita-7:24h")
    assert omitido["estado"] == "omitido" and "teléfono" in omitido["detalle"]


def test_un_canal_apagado_no_escribe_nada():
    puerto = _puerto(Falso("whatsapp", disponible=False), Falso("email", disponible=False), CONTACTO)
    assert puerto.send(Notificacion()) == []
    assert db.listar() == []


def test_los_proveedores_se_resuelven_en_cada_envio():
    """La configuración cambia con el proceso andando (se prende WhatsApp por la pantalla): no se congela al construir el puerto."""
    actual = {"whatsapp": Falso("whatsapp", disponible=False)}
    puerto = NotificacionPorAvisos(lambda: actual, contacto=lambda cid: CONTACTO, redactar=_redactar)
    assert puerto.send(Notificacion()) == []
    actual["whatsapp"] = Falso("whatsapp")
    assert [e.estado for e in puerto.send(Notificacion())] == ["enviado"]


def test_redactar_puede_decidir_que_por_ese_canal_no_se_avisa():
    wa, mail = Falso("whatsapp"), Falso("email")
    puerto = NotificacionPorAvisos({"whatsapp": wa, "email": mail}, contacto=lambda cid: CONTACTO,
                                   redactar=lambda n, canal: _redactar(n, canal) if canal == "email" else None)
    puerto.send(Notificacion())
    assert wa.enviados == [] and len(mail.enviados) == 1


def test_en_un_nodo_no_sale_ni_se_anota():
    wa = Falso("whatsapp")
    # un solo canal: el nodo contesta una vez; con `todos`, una por canal
    assert [e.estado for e in _puerto(wa, Falso("email"), CONTACTO, nodo=lambda: "n1").send(Notificacion())] == ["en_nodo"]
    assert [e.estado for e in _puerto(wa, Falso("email"), CONTACTO, nodo=lambda: "n1", politica="todos").send(Notificacion())] == ["en_nodo", "en_nodo"]
    assert wa.enviados == [] and db.listar() == []


def test_la_clave_de_un_recordatorio():
    assert clave_de_recordatorio("cita-7", "24h") == "agenda:cita-7:24h"


# ---------------------------------------------------------------- un solo canal (v1.203.0)

def _claves(canal=None):
    return {(f["canal"], f["estado"]) for f in db.listar() if canal in (None, f["canal"])}


def test_con_telefono_y_whatsapp_disponible_sale_solo_por_whatsapp():
    wa, mail = Falso("whatsapp"), Falso("email")
    envios = _puerto(wa, mail, CONTACTO).send(Notificacion())

    assert [e.estado for e in envios] == ["enviado"]
    assert len(wa.enviados) == 1 and mail.enviados == []
    assert _claves() == {("whatsapp", "enviado")}, "ni una fila de mail: no se avisó ni se omitió por ahí"


def test_sin_telefono_valido_sale_solo_por_mail():
    wa, mail = Falso("whatsapp"), Falso("email")
    envios = _puerto(wa, mail, {"email": "paciente@ejemplo.example", "telefono": "5555"}).send(Notificacion())

    assert [e.estado for e in envios] == ["omitido", "enviado"]
    assert wa.enviados == [] and len(mail.enviados) == 1
    assert _claves() == {("whatsapp", "omitido"), ("email", "enviado")}


def test_con_whatsapp_apagado_sale_por_mail_y_no_escribe_nada_de_whatsapp():
    wa, mail = Falso("whatsapp", disponible=False), Falso("email")
    envios = _puerto(wa, mail, CONTACTO).send(Notificacion())

    assert [e.estado for e in envios] == ["enviado"]
    assert wa.enviados == [] and len(mail.enviados) == 1
    assert _claves() == {("email", "enviado")}


def test_en_modo_registro_se_anota_en_whatsapp_y_no_sale_ningun_mail():
    mail = Falso("email")
    envios = _puerto(ProveedorRegistro("whatsapp"), mail, CONTACTO).send(Notificacion())

    assert [e.estado for e in envios] == ["registrado"]
    assert mail.enviados == [] and _claves() == {("whatsapp", "registrado")}


def test_si_whatsapp_rechaza_el_numero_cae_al_mail_con_la_misma_clave():
    wa = Falso("whatsapp", Resultado.rechazado("El número no tiene WhatsApp."))
    mail = Falso("email")
    puerto = _puerto(wa, mail, CONTACTO)

    envios = puerto.send(Notificacion())

    assert [e.estado for e in envios] == ["rechazado", "enviado"]
    assert len(wa.enviados) == 1 and len(mail.enviados) == 1
    assert {(f["canal"], f["clave"]) for f in db.listar()} == {("whatsapp", "agenda:cita-7:24h"), ("email", "agenda:cita-7:24h")}

    # Si dispatch repite, no vuelve a probar WhatsApp ni a mandar el mail otra vez.
    puerto.send(Notificacion())
    assert len(wa.enviados) == 1 and len(mail.enviados) == 1


def test_si_whatsapp_rechaza_y_no_hay_mail_queda_rechazado_sin_levantar():
    wa = Falso("whatsapp", Resultado.rechazado("El número no tiene WhatsApp."))
    envios = _puerto(wa, Falso("email", disponible=False), CONTACTO).send(Notificacion())
    assert [e.estado for e in envios] == ["rechazado"]


def test_un_fallo_de_whatsapp_con_intentos_por_delante_no_cae_al_mail():
    wa, mail = Falso("whatsapp", Resultado.fallido("Evolution respondió 503.")), Falso("email")
    envios = _puerto(wa, mail, CONTACTO).send(Notificacion())
    assert [e.estado for e in envios] == ["fallido"] and mail.enviados == []


def test_un_fallo_de_whatsapp_con_los_intentos_agotados_cae_al_mail():
    wa = Falso("whatsapp", *[Resultado.fallido("Evolution respondió 503.")] * MAX_INTENTOS)
    mail = Falso("email")
    puerto = _puerto(wa, mail, CONTACTO)

    for _ in range(MAX_INTENTOS - 1):
        assert [e.estado for e in puerto.send(Notificacion())] == ["fallido"]
    assert mail.enviados == []
    envios = puerto.send(Notificacion())  # el tercer intento falla y no quedan más

    assert [e.estado for e in envios] == ["fallido", "enviado"] and len(mail.enviados) == 1


def test_si_la_redaccion_no_tiene_texto_para_whatsapp_se_prueba_el_mail():
    mail = Falso("email")
    puerto = NotificacionPorAvisos({"whatsapp": Falso("whatsapp"), "email": mail}, contacto=lambda cid: CONTACTO,
                                   redactar=lambda n, canal: None if canal == "whatsapp" else _redactar(n, canal))
    assert [e.estado for e in puerto.send(Notificacion())] == ["enviado"] and len(mail.enviados) == 1


def test_lo_que_salio_por_mail_no_sale_por_whatsapp_si_la_configuracion_cambia():
    wa, mail = Falso("whatsapp", disponible=False), Falso("email")
    puerto = _puerto(wa, mail, CONTACTO)
    assert [e.estado for e in puerto.send(Notificacion())] == ["enviado"]

    wa._disp = True  # entre las dos corridas se prendió WhatsApp
    envios = puerto.send(Notificacion())

    assert wa.enviados == [] and len(mail.enviados) == 1
    assert [(e.estado, e.repetido) for e in envios] == [("enviado", True)]
    assert _claves() == {("email", "enviado")}


def test_lo_que_salio_por_whatsapp_no_sale_por_mail_si_se_apaga_whatsapp():
    wa, mail = Falso("whatsapp"), Falso("email")
    puerto = _puerto(wa, mail, CONTACTO)
    puerto.send(Notificacion())

    wa._disp = False  # entre las dos corridas se apagó
    envios = puerto.send(Notificacion())

    assert mail.enviados == [] and len(wa.enviados) == 1
    assert [(e.estado, e.repetido) for e in envios] == [("enviado", True)]


def test_un_aviso_que_solo_se_registro_si_sale_por_mail_cuando_se_apaga_whatsapp():
    mail = Falso("email")
    _puerto(ProveedorRegistro("whatsapp"), mail, CONTACTO).send(Notificacion())  # modo prueba: no se entregó nada
    envios = _puerto(Falso("whatsapp", disponible=False), mail, CONTACTO).send(Notificacion())
    assert [e.estado for e in envios] == ["enviado"] and len(mail.enviados) == 1


def test_con_politica_todos_no_mira_lo_que_salio_por_el_otro_canal():
    wa, mail = Falso("whatsapp", disponible=False), Falso("email")
    todos = _puerto(wa, mail, CONTACTO, politica="todos")
    todos.send(Notificacion())
    wa._disp = True
    todos.send(Notificacion())
    assert len(wa.enviados) == 1 and len(mail.enviados) == 1


def test_una_politica_desconocida_no_se_acepta():
    with pytest.raises(ValueError, match="política"):
        _puerto(Falso("whatsapp"), Falso("email"), CONTACTO, politica="mail_primero")


# ---------------------------------------------------------------- canal_para

def test_canal_para_elige_whatsapp_con_telefono_y_si_no_el_mail():
    from libracore.avisos import canal_para

    wa, mail = Falso("whatsapp"), Falso("email")
    proveedores = {"whatsapp": wa, "email": mail}
    via = canal_para(CONTACTO, proveedores)
    assert (via.canal, via.destino, via.proveedor) == ("whatsapp", "5491155551234", wa)
    assert canal_para({"email": "a@b.example", "telefono": "5555"}, proveedores).canal == "email"
    wa._disp = False
    assert canal_para(CONTACTO, proveedores).canal == "email"
    assert canal_para({"telefono": "11 5555-1234"}, proveedores) is None
    assert canal_para(None, proveedores) is None
    mail._disp = False
    assert canal_para(CONTACTO, proveedores) is None
