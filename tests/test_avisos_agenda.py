"""El adaptador de `libragenda.NotificationPort` (ADR-090): lo que reemplaza al `LoggingNotificationPort` de GestioLibra y MedLibra.

La notificación es la de `libragenda` (`ReminderNotification`): sólo ids, sin texto ni canal. El adaptador la resuelve con el
contacto y el texto que pone el producto. Lo que se fija: sale por cada canal con destino, no sale dos veces aunque `dispatch` repita,
un fallo no levanta (cortaría la tanda de `ReminderDispatcher`) y queda reintentable, y un canal sin destino queda omitido.
"""
import dataclasses
from datetime import datetime, timedelta

import pytest

from libracore import fechas
from libracore.avisos import ESPERA_ENTRE_INTENTOS, Mensaje, Resultado, reintentar_fallidos
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


def test_sale_por_cada_canal_con_destino_y_normaliza_el_telefono():
    wa, mail = Falso("whatsapp"), Falso("email")
    envios = _puerto(wa, mail, CONTACTO).send(Notificacion())

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
    assert len(wa.enviados) == 1 and len(mail.enviados) == 1
    # otra política de la misma cita es otro recordatorio
    puerto.send(Notificacion(policy_id="2h"))
    assert len(wa.enviados) == 2


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
    assert [e.estado for e in _puerto(wa, Falso("email"), CONTACTO, nodo=lambda: "n1").send(Notificacion())] == ["en_nodo", "en_nodo"]
    assert wa.enviados == [] and db.listar() == []


def test_la_clave_de_un_recordatorio():
    assert clave_de_recordatorio("cita-7", "24h") == "agenda:cita-7:24h"
