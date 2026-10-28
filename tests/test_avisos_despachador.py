"""El despachador de avisos (ADR-090), contra SQLite y PostgreSQL de verdad.

Lo que se fija:
- **no se manda dos veces lo mismo**: la misma `(canal, clave)` no sale otra vez, ni con otro texto, y dos procesos a la vez
  mandan una sola vez (la fila se reserva antes de salir a la red);
- **el reintento**: un `fallido` vuelve a intentarse hasta `MAX_INTENTOS`; un `rechazado` no;
- 🔴 **`registrado` sólo es terminal para un proveedor simulado**: lo anotado en modo prueba se manda cuando llega uno real;
- un proceso que murió a mitad de un envío no deja el aviso trabado, pero tampoco se pisa con uno que está mandando;
- un nodo no manda; un canal no disponible no escribe nada;
- un proveedor que explota no tumba el barrido, y lo que dice no termina en el registro ni en los logs.

Los proveedores son dobles: nada real sale.
"""
import logging
import os
import threading
import time
from datetime import timedelta

import pytest

from libracore import fechas
from libracore.avisos import (
    EN_NODO,
    MAX_INTENTOS,
    NO_DISPONIBLE,
    Envio,
    Mensaje,
    Resultado,
    Resumen,
    cerradas,
    cerrado,
    despachar,
    omitir,
    reintentar_fallidos,
)
from libracore.avisos import despachador as modulo
from libracore.db import avisos_envios as db
from libracore.db import core
from libracore.db.schema import init_core_schema


@pytest.fixture(params=["sqlite", "postgres"])
def base(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("LIBRACORE_POSTGRES_URL")
        if not url:
            pytest.skip("LIBRACORE_POSTGRES_URL no configurada")
        import psycopg

        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")
        core.configure(db_path=url)
    else:
        core.configure(db_path=str(tmp_path / "avisos.db"))
    with core.get_connection() as conn:
        init_core_schema(conn)
        conn.commit()
    yield request.param
    core._db_path = None
    core._database_url = None


@pytest.fixture(autouse=True)
def _fuera_de_un_nodo(monkeypatch):
    monkeypatch.delenv("LIBRAEDGE_NODE_ID", raising=False)


class Falso:
    """Un proveedor de juguete: contesta lo que se le dice, en orden, y anota lo que le llega."""

    def __init__(self, *resultados, canal="whatsapp", simulado=False, nombre="falso", disponible=True, demora=0.0):
        self.canal, self.simulado, self.nombre = canal, simulado, nombre
        self._resultados = list(resultados)
        self._disponible = disponible
        self._demora = demora
        self.enviados: list[Mensaje] = []

    def disponible(self):
        return self._disponible

    def enviar(self, mensaje):
        self.enviados.append(mensaje)
        if self._demora:
            time.sleep(self._demora)
        r = self._resultados.pop(0) if self._resultados else Resultado.enviado("id-1")
        if isinstance(r, BaseException):
            raise r
        return r


MSG = Mensaje("5491155551234", cuerpo="*Turno confirmado*\nMañana 19:00")
CLAVE = "reserva:41:confirmacion"


def _fila(canal="whatsapp", clave=CLAVE):
    return db.obtener(canal, clave)


def _tras_la_espera():
    """Un `ahora` pasada la espera entre intentos de `reintentar_fallidos`."""
    return fechas.ahora() + modulo.ESPERA_ENTRE_INTENTOS + timedelta(seconds=30)


# ── Una sola vez ──────────────────────────────────────────────────────────────────────────────────────────


def test_manda_y_deja_el_rastro(base):
    p = Falso(Resultado.enviado("BAE5"))
    e = despachar(p, Mensaje("5491155551234", "asunto", "texto", "turno_confirmado", {"hora": "19:00"}), clave=CLAVE,
                  tipo="confirmacion")

    assert (e.estado, e.intentos, e.repetido, e.salio, e.id_externo) == ("enviado", 1, False, True, "BAE5")
    f = _fila()
    assert f["estado"] == "enviado" and f["intentos"] == 1 and f["tipo"] == "confirmacion" and f["proveedor"] == "falso"
    assert f["destino"] == "5491155551234" and f["cuerpo"] == "texto" and f["asunto"] == "asunto"
    assert f["plantilla"] == "turno_confirmado" and f["variables"] == {"hora": "19:00"}
    assert f["enviado_at"] and f["id_externo"] == "BAE5"


def test_la_misma_clave_no_sale_dos_veces_ni_con_otro_texto(base):
    p = Falso()
    despachar(p, MSG, clave=CLAVE)
    e = despachar(p, Mensaje("5491155551234", cuerpo="otro texto"), clave=CLAVE)

    assert len(p.enviados) == 1
    assert e.estado == "enviado" and e.repetido and e.salio
    assert len(db.listar()) == 1


def test_el_mismo_hecho_por_dos_canales_son_dos_avisos(base):
    despachar(Falso(canal="whatsapp"), MSG, clave=CLAVE)
    despachar(Falso(canal="email"), Mensaje("jugador@ejemplo.example", "Turno", "texto"), clave=CLAVE)
    assert {f["canal"] for f in db.listar()} == {"whatsapp", "email"}


@pytest.mark.parametrize("clave", ["", "   ", "x" * 201])
def test_la_clave_es_obligatoria_y_acotada(base, clave):
    with pytest.raises(ValueError):
        despachar(Falso(), MSG, clave=clave)


def test_dos_procesos_a_la_vez_mandan_una_sola_vez(base):
    """El caso que el motor de LibraClub tenía abierto: dos corridas superpuestas del cron. La fila se reserva antes de mandar."""
    p = Falso(demora=0.3)
    barrera = threading.Barrier(6)
    envios: list[Envio] = []

    def correr():
        barrera.wait()
        envios.append(despachar(p, MSG, clave=CLAVE))

    hilos = [threading.Thread(target=correr) for _ in range(6)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(30)

    assert len(p.enviados) == 1, "seis procesos, un solo mensaje"
    assert sum(not e.repetido for e in envios) == 1 and len(envios) == 6
    assert _fila()["estado"] == "enviado" and _fila()["intentos"] == 1


# ── Reintento ─────────────────────────────────────────────────────────────────────────────────────────────


def test_un_fallo_se_reintenta_en_la_llamada_siguiente(base):
    p = Falso(Resultado.fallido("Evolution respondió 503."), Resultado.enviado("ok"))

    e1 = despachar(p, MSG, clave=CLAVE)
    assert (e1.estado, e1.intentos, e1.salio) == ("fallido", 1, False) and "503" in e1.detalle
    assert _fila()["estado"] == "fallido"

    e2 = despachar(p, MSG, clave=CLAVE)
    assert (e2.estado, e2.intentos, e2.repetido) == ("enviado", 2, False)
    assert len(p.enviados) == 2
    assert _fila()["detalle"] == "", "el motivo del fallo viejo no queda pegado a un envío logrado"


def test_se_deja_de_insistir_a_los_max_intentos(base):
    p = Falso(*[Resultado.fallido("caído")] * 10)
    estados = [despachar(p, MSG, clave=CLAVE).estado for _ in range(MAX_INTENTOS + 2)]

    assert len(p.enviados) == MAX_INTENTOS
    assert estados == ["fallido"] * (MAX_INTENTOS + 2)
    assert _fila()["intentos"] == MAX_INTENTOS
    assert cerrado(p, CLAVE), "agotado: ya no hay que despacharlo"


def test_un_rechazado_no_se_reintenta(base):
    p = Falso(Resultado.rechazado("El número no tiene WhatsApp."))
    despachar(p, MSG, clave=CLAVE)
    e = despachar(p, MSG, clave=CLAVE)

    assert len(p.enviados) == 1 and e.estado == "rechazado" and e.repetido
    assert cerrado(p, CLAVE)


def test_el_tope_de_intentos_se_puede_cambiar(base):
    p = Falso(*[Resultado.fallido("caído")] * 10)
    for _ in range(5):
        despachar(p, MSG, clave=CLAVE, max_intentos=1)
    assert len(p.enviados) == 1


# ── 🔴 `registrado` ───────────────────────────────────────────────────────────────────────────────────────


def test_registrado_cierra_el_tema_para_un_proveedor_simulado(base):
    prueba = Falso(Resultado.registrado("modo prueba"), simulado=True, nombre="registro")
    despachar(prueba, MSG, clave=CLAVE)
    e = despachar(prueba, MSG, clave=CLAVE)

    assert len(prueba.enviados) == 1 and e.estado == "registrado" and e.repetido
    assert cerrado(prueba, CLAVE)


def test_registrado_NO_cierra_el_tema_para_un_proveedor_real(base):
    """Lo que se anotó en modo prueba se manda cuando llega Evolution: ni se pierde ni se da por hecho."""
    prueba = Falso(Resultado.registrado("modo prueba"), simulado=True, nombre="registro")
    real = Falso(Resultado.enviado("BAE5"), nombre="evolution")
    despachar(prueba, MSG, clave=CLAVE)
    assert not cerrado(real, CLAVE)

    e = despachar(real, MSG, clave=CLAVE)

    assert len(real.enviados) == 1 and not e.repetido
    f = _fila()
    assert f["estado"] == "enviado" and f["proveedor"] == "evolution" and f["id_externo"] == "BAE5"
    assert f["intentos"] == 1, "el intento en modo prueba no gasta uno de los de verdad"
    # y ahora sí: ni el real ni el simulado lo vuelven a mandar
    assert cerrado(real, CLAVE) and cerrado(prueba, CLAVE)
    despachar(real, MSG, clave=CLAVE)
    assert len(real.enviados) == 1


def test_lo_enviado_de_verdad_no_se_registra_en_modo_prueba(base):
    real = Falso(nombre="evolution")
    prueba = Falso(simulado=True, nombre="registro")
    despachar(real, MSG, clave=CLAVE)
    e = despachar(prueba, MSG, clave=CLAVE)
    assert prueba.enviados == [] and e.estado == "enviado" and e.repetido


# ── Proveedor no disponible, nodo, omitidos ───────────────────────────────────────────────────────────────


def test_un_canal_no_disponible_no_manda_ni_escribe(base):
    """Sin esto, una instancia sin SMTP acumularía un fallo por aviso y a los tres intentos los daría por perdidos."""
    p = Falso(disponible=False)
    e = despachar(p, MSG, clave=CLAVE)
    assert e.estado == NO_DISPONIBLE and not e.salio
    assert p.enviados == [] and db.listar() == []


def test_un_nodo_no_manda_ni_escribe(base):
    p = Falso()
    e = despachar(p, MSG, clave=CLAVE, nodo=lambda: "nodo-1")
    assert e.estado == EN_NODO and p.enviados == [] and db.listar() == []


def test_el_nodo_se_reconoce_por_el_entorno(base, monkeypatch):
    monkeypatch.setenv("LIBRAEDGE_NODE_ID", "nodo-del-local")
    p = Falso()
    assert despachar(p, MSG, clave=CLAVE).estado == EN_NODO and p.enviados == []


def test_sin_destino_queda_omitido_con_su_motivo(base):
    p = Falso()
    e = despachar(p, Mensaje("  ", cuerpo="hola"), clave=CLAVE)
    assert e.estado == "omitido" and p.enviados == []
    assert _fila()["estado"] == "omitido" and "destino" in _fila()["detalle"]
    assert despachar(p, MSG, clave=CLAVE).repetido, "un omitido cierra el tema: no se reabre solo"


def test_omitir_anota_y_no_pisa_lo_que_ya_salio(base):
    omitir("whatsapp", "reserva:7:recordatorio", motivo="El cliente pidió no recibir avisos.", tipo="recordatorio")
    assert _fila(clave="reserva:7:recordatorio")["estado"] == "omitido"

    despachar(Falso(), MSG, clave=CLAVE)
    e = omitir("whatsapp", CLAVE, motivo="tarde")
    assert e.estado == "enviado", "un aviso que ya salió no se convierte en omitido"


# ── Un proceso que murió a mitad del envío ────────────────────────────────────────────────────────────────


def _dejar_enviando(intentos=1):
    ident = db.reservar("whatsapp", CLAVE, tipo="", destino=MSG.destino, asunto="", cuerpo=MSG.cuerpo, plantilla="", variables={},
                        proveedor="falso")
    if intentos > 1:
        with core.get_connection() as c:
            c.execute("UPDATE avisos_envios SET intentos=? WHERE id=?", (intentos, ident))
    return ident


def test_un_envio_en_curso_no_se_pisa(base):
    _dejar_enviando()
    p = Falso()
    e = despachar(p, MSG, clave=CLAVE)
    assert p.enviados == [] and e.estado == "enviando" and e.repetido


def test_un_envio_abandonado_se_rescata_pasado_el_vencimiento(base):
    _dejar_enviando()
    p = Falso(Resultado.enviado("ok"))
    e = despachar(p, MSG, clave=CLAVE, ahora=fechas.ahora() + modulo.VENCE_EL_ENVIO + timedelta(minutes=1))
    assert len(p.enviados) == 1 and e.estado == "enviado" and e.intentos == 2


def test_un_envio_abandonado_con_los_intentos_agotados_no_se_rescata(base):
    _dejar_enviando(intentos=MAX_INTENTOS)
    p = Falso()
    despachar(p, MSG, clave=CLAVE, ahora=fechas.ahora() + timedelta(hours=1))
    assert p.enviados == []


def test_un_envio_abandonado_sin_intentos_queda_fallido_y_no_enviando_para_siempre(base):
    _dejar_enviando(intentos=MAX_INTENTOS)
    p = Falso()
    e = despachar(p, MSG, clave=CLAVE, ahora=fechas.ahora() + timedelta(hours=1))
    assert p.enviados == [] and e.estado == "fallido" and e.repetido
    assert _fila()["estado"] == "fallido" and "no quedan intentos" in _fila()["detalle"]
    assert cerrado(p, CLAVE), "terminal: el barrido que deduce candidatos lo descarta"


def test_un_envio_sin_intentos_pero_todavia_en_curso_no_se_cierra(base):
    _dejar_enviando(intentos=MAX_INTENTOS)
    despachar(Falso(), MSG, clave=CLAVE)
    reintentar_fallidos(Falso())
    assert _fila()["estado"] == "enviando", "recién empezado: el proceso puede estar mandándolo"


def test_reintentar_fallidos_cierra_los_abandonados_sin_intentos(base):
    _dejar_enviando(intentos=MAX_INTENTOS)
    p = Falso()
    assert reintentar_fallidos(p, ahora=fechas.ahora() + modulo.VENCE_EL_ENVIO + timedelta(minutes=1)) == Resumen()
    assert p.enviados == [] and _fila()["estado"] == "fallido"
    assert reintentar_fallidos(p, ahora=fechas.ahora() + timedelta(hours=1)) == Resumen(), "y ahí queda"


def test_tomar_es_una_compare_and_swap(base):
    """Dos que leyeron el mismo `fallido` y quieren reintentarlo: gana uno."""
    despachar(Falso(Resultado.fallido("x")), MSG, clave=CLAVE)
    leido = _fila()
    campos = dict(tipo="", destino=MSG.destino, asunto="", cuerpo=MSG.cuerpo, plantilla="", variables={}, proveedor="falso")
    assert db.tomar(leido, **campos) is True
    assert db.tomar(leido, **campos) is False, "el segundo ya encuentra otro estado y otros intentos"


# ── Un proveedor que explota ──────────────────────────────────────────────────────────────────────────────


def test_un_proveedor_que_levanta_no_tumba_el_barrido_ni_deja_su_texto(base, caplog):
    secreto = "apikey-ficticia-que-no-debe-salir"
    p = Falso(RuntimeError(f"fallo con {secreto}"))
    with caplog.at_level(logging.DEBUG):
        e = despachar(p, MSG, clave=CLAVE)

    assert e.estado == "fallido" and "RuntimeError" in e.detalle
    assert secreto not in e.detalle and secreto not in _fila()["detalle"] and secreto not in caplog.text
    assert despachar(p, MSG, clave=CLAVE).estado == "enviado", "y se reintenta"


def test_un_estado_desconocido_del_proveedor_es_un_fallo(base):
    e = despachar(Falso(Resultado("rarisimo")), MSG, clave=CLAVE)
    assert e.estado == "fallido" and "desconocido" in e.detalle


def test_el_cuerpo_del_mensaje_no_va_a_los_logs(base, caplog):
    with caplog.at_level(logging.DEBUG):
        despachar(Falso(), Mensaje("5491155551234", cuerpo="contenido-privado-del-aviso"), clave=CLAVE)
        despachar(Falso(Resultado.fallido("caído")), Mensaje("5491155551234", cuerpo="contenido-privado-del-aviso"), clave="otra")
    assert "contenido-privado" not in caplog.text
    assert "5491155551234" not in caplog.text, "ni el teléfono del cliente"


# ── Reintento propio del motor ────────────────────────────────────────────────────────────────────────────


def test_reintentar_fallidos_manda_lo_que_quedo_guardado(base):
    despachar(Falso(Resultado.fallido("caído")), Mensaje("5491155551234", "as", "cuerpo guardado", "plantilla_x", {"a": "1"}),
              clave=CLAVE, tipo="recordatorio")
    p = Falso(Resultado.enviado("ok"))

    resumen = reintentar_fallidos(p, ahora=_tras_la_espera())

    assert resumen.enviados == 1 and not resumen.fallidos
    (m,) = p.enviados
    assert (m.destino, m.asunto, m.cuerpo, m.plantilla, dict(m.variables)) == ("5491155551234", "as", "cuerpo guardado", "plantilla_x",
                                                                              {"a": "1"})
    assert _fila()["estado"] == "enviado" and _fila()["intentos"] == 2


def test_reintentar_fallidos_respeta_el_tope_la_antiguedad_y_el_canal(base):
    despachar(Falso(Resultado.fallido("x")), MSG, clave="viejo")
    despachar(Falso(Resultado.fallido("x"), canal="email"), Mensaje("a@b.example", "s", "c"), clave="de-mail")
    despachar(Falso(*[Resultado.fallido("x")] * 3), MSG, clave="agotado")
    for _ in range(MAX_INTENTOS - 1):
        despachar(Falso(Resultado.fallido("x")), MSG, clave="agotado")

    p = Falso()
    futuro = fechas.ahora() + timedelta(hours=25)
    assert reintentar_fallidos(p, ahora=futuro) == Resumen(), "pasadas 24 horas un recordatorio ya no sirve"
    assert reintentar_fallidos(p, ahora=_tras_la_espera()).enviados == 1, "el de whatsapp; el agotado y el de mail no"
    assert [m.cuerpo for m in p.enviados] == [MSG.cuerpo]


def test_reintentar_fallidos_espera_entre_intentos(base):
    despachar(Falso(Resultado.fallido("Evolution caído")), MSG, clave=CLAVE)
    p = Falso(Resultado.enviado("ok"))

    assert reintentar_fallidos(p) == Resumen(), "recién falló: no se reintenta en el mismo segundo"
    assert reintentar_fallidos(p, ahora=fechas.ahora() + timedelta(minutes=4)) == Resumen()
    assert p.enviados == [] and _fila()["intentos"] == 1

    assert reintentar_fallidos(p, ahora=_tras_la_espera()).enviados == 1
    assert _fila()["estado"] == "enviado" and _fila()["intentos"] == 2


def test_despachar_y_reintentar_en_el_mismo_cron_no_gastan_dos_intentos(base):
    """El caso del cron: un fallo de Evolution en `despachar`, y `reintentar_fallidos` justo después."""
    despachar(Falso(Resultado.fallido("Evolution caído")), MSG, clave=CLAVE)
    p = Falso(Resultado.fallido("Evolution caído"))
    reintentar_fallidos(p)
    assert p.enviados == [] and _fila()["intentos"] == 1, "queda el intento del barrido siguiente, no se gastó"


def test_la_espera_entre_intentos_se_puede_cambiar(base):
    despachar(Falso(Resultado.fallido("x")), MSG, clave=CLAVE)
    assert reintentar_fallidos(Falso(), espera=timedelta(0), ahora=fechas.ahora() + timedelta(seconds=1)).enviados == 1


def test_reintentar_fallidos_no_reenvia_lo_registrado_en_modo_prueba(base):
    despachar(Falso(Resultado.registrado("prueba"), simulado=True), MSG, clave=CLAVE)
    p = Falso()
    assert reintentar_fallidos(p) == Resumen() and p.enviados == []


def test_reintentar_fallidos_rescata_los_abandonados(base):
    _dejar_enviando()
    p = Falso()
    assert reintentar_fallidos(p).enviados == 0, "recién empezado: lo tiene otro"
    assert reintentar_fallidos(p, ahora=fechas.ahora() + modulo.VENCE_EL_ENVIO + timedelta(minutes=1)).enviados == 1


def test_reintentar_fallidos_en_un_nodo_o_sin_proveedor_no_hace_nada(base):
    despachar(Falso(Resultado.fallido("x")), MSG, clave=CLAVE)
    assert reintentar_fallidos(Falso(), nodo=lambda: "n1") == Resumen()
    assert reintentar_fallidos(Falso(disponible=False)) == Resumen()


# ── Una demo no manda mails ───────────────────────────────────────────────────────────────────────────────


def test_en_una_demo_el_smtp_registra_y_cierra_el_tema(base, monkeypatch):
    from libracore.avisos import ProveedorSmtp

    monkeypatch.setenv("DEMO_MODE", "1")
    salidas = []
    cfg = type("C", (), {"configurado": True, "host": "h", "port": 587, "user": "u", "password": "p",
                         "from_email": "u@b.example", "from_name": ""})()
    p = ProveedorSmtp(lambda: cfg, enviar_texto=lambda **kw: salidas.append(kw))
    mail = Mensaje("jugador@ejemplo.example", "Recordatorio", "Mañana 19:00")

    e = despachar(p, mail, clave=CLAVE, tipo="recordatorio")
    assert e.estado == "registrado" and salidas == []
    assert despachar(p, mail, clave=CLAVE).repetido, "registrado por un simulado cierra el tema: no se acumula ni se reintenta"
    assert salidas == []


# ── El resumen y el listado ───────────────────────────────────────────────────────────────────────────────


def test_el_resumen_cuenta_cada_cosa():
    r = Resumen()
    for e in (Envio("enviado"), Envio("enviado", repetido=True), Envio("registrado"), Envio("fallido"), Envio("rechazado"),
              Envio("omitido"), Envio(NO_DISPONIBLE), Envio(EN_NODO)):
        r = r.sumar(e)
    assert r == Resumen(enviados=1, registrados=1, fallidos=1, rechazados=1, omitidos=1, repetidos=1, sin_proveedor=2)
    assert str(r).startswith("enviados: 1 · fallidos: 1 · omitidos: 1") and "registrados: 1" in str(r)


def test_el_listado_filtra_y_va_del_mas_nuevo_al_mas_viejo(base):
    despachar(Falso(), MSG, clave="a", tipo="confirmacion")
    despachar(Falso(Resultado.fallido("x")), MSG, clave="b", tipo="recordatorio")
    despachar(Falso(canal="email"), Mensaje("a@b.example", "s", "c"), clave="c", tipo="recordatorio")

    assert [f["clave"] for f in db.listar()] == ["c", "b", "a"]
    assert [f["clave"] for f in db.listar(canal="whatsapp")] == ["b", "a"]
    assert [f["clave"] for f in db.listar(estado="fallido")] == ["b"]
    assert [f["clave"] for f in db.listar(tipo="recordatorio", canal="email")] == ["c"]
    assert len(db.listar(limite=1)) == 1


def test_cerrar_solo_vale_desde_enviando_y_rechaza_estados_raros(base):
    ident = _dejar_enviando()
    with pytest.raises(ValueError):
        db.cerrar(ident, "enviando")
    with pytest.raises(ValueError):
        db.cerrar(ident, "inventado")
    assert db.cerrar(ident, "enviado") is True
    assert db.cerrar(ident, "fallido") is False, "ya no está enviando"


def test_cerradas_pregunta_por_muchas_claves_a_la_vez(base):
    real = Falso(nombre="evolution")
    prueba = Falso(Resultado.registrado("p"), simulado=True, nombre="registro")
    despachar(real, MSG, clave="salio")
    despachar(prueba, MSG, clave="en-prueba")
    despachar(Falso(Resultado.fallido("x")), MSG, clave="fallo-reciente")
    despachar(Falso(Resultado.rechazado("x")), MSG, clave="rechazado")
    omitir("whatsapp", "omitido", motivo="sin teléfono")

    claves = ["salio", "en-prueba", "fallo-reciente", "rechazado", "omitido", "nunca"]
    assert cerradas(real, claves) == {"salio", "rechazado", "omitido"}
    assert cerradas(prueba, claves) == {"salio", "en-prueba", "rechazado", "omitido"}
    assert cerradas(real, []) == set()
    # y más de 500: se parte en tandas
    assert cerradas(real, [f"k{i}" for i in range(1200)] + ["salio"]) == {"salio"}


def test_omitir_en_un_nodo_no_escribe(base):
    assert omitir("whatsapp", CLAVE, motivo="x", nodo=lambda: "n1").estado == EN_NODO
    assert db.listar() == []


def test_las_variables_se_guardan_como_texto_aunque_lleguen_otros_tipos(base):
    from decimal import Decimal

    despachar(Falso(), Mensaje("5491155551234", cuerpo="x", variables={"monto": Decimal("1500.50"), "n": 3}), clave=CLAVE)
    assert _fila()["variables"] == {"monto": "1500.50", "n": "3"}
