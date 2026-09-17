"""Los tres secretos de `config.json` dejan de estar en texto plano (2026-09-17).

**Casi todos estos tests miran el archivo `config.json` crudo**, no lo que
devuelve `load()`. Es deliberado: `load()` devuelve el secreto en claro por
diseño —para eso existe la indirección, para que los ~12 consumidores no
cambien— así que un assert sobre `load()` da verde exactamente igual con la
implementación vieja, la que escribía el token en el JSON. Lo único que
distingue una de otra es **qué quedó en el disco**.

El almacén de acá es un doble en memoria, y eso también es a propósito:
LibraCore no depende de libraauth y este módulo no tiene que saber quién le
guarda los secretos. Que `libraauth.secretos.SecretosRepository` cumpla el
contrato lo mide el último test, que se saltea si el paquete no está instalado.
"""
import importlib
import json

import pytest

TOKEN = "APP_USR-1234567890123456-091712-abcdef0123456789abcdef0123456789-3392230021"
FIRMA = "firma-del-webhook-de-mercadopago"
SMTP_PASS = "la-contrasena-de-la-casilla"


class AlmacenFalso:
    """Lo mínimo que `config_manager` le pide a un almacén: `get` y `set`.

    Cuenta los `set` porque uno de los hechos a fijar es que guardar la razón
    social **no** reescribe las tres credenciales.
    """

    def __init__(self, inicial=None, rompe_con=None):
        self.datos = dict(inicial or {})
        self.sets = []
        #: Si está, `set` de esa clave lanza — simula una instancia sin
        #: `SECRET_KEY`, donde `cifrar()` levanta `ClaveDeCifradoAusente`.
        self.rompe_con = rompe_con

    def get(self, clave):
        return self.datos.get(clave, "")

    def set(self, clave, valor):
        if self.rompe_con and clave == self.rompe_con:
            raise RuntimeError("no hay con que cifrar")
        self.sets.append(clave)
        if valor:
            self.datos[clave] = valor
        else:
            self.datos.pop(clave, None)


@pytest.fixture
def cm(tmp_path, monkeypatch):
    """Un `config_manager` recién recargado apuntando a un DATA_DIR propio.

    El `reload` también deja `_almacen` en `None`, así que cada test arranca
    desde el estado de una instancia que todavía no enchufó nada.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as modulo
    importlib.reload(modulo)
    yield modulo
    modulo.usar_almacen_de_secretos(None)


def _json_crudo(cm):
    with open(cm.CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── El control positivo: sin almacén, el secreto SÍ queda en el archivo ─────

def test_sin_almacen_el_token_queda_en_claro_en_el_archivo(cm):
    """🔑 El control positivo de todo este archivo.

    Es el comportamiento **viejo**, y se deja fijado a propósito: sin él, el
    test de al lado —"con almacén el token no está en el archivo"— pasaría
    igual si `save()` no escribiera nada, o si el barrido estuviera mirando el
    archivo equivocado.
    """
    cm.save({"mp_access_token": TOKEN})
    assert _json_crudo(cm)["mp_access_token"] == TOKEN
    assert cm.load()["mp_access_token"] == TOKEN


def test_sin_almacen_no_cambia_nada_mas(cm):
    """Una instancia sin almacén enchufado se comporta como antes. Es lo que
    permite que `panel_admin` y el provisioning sigan leyendo la config de una
    instancia ajena sin una base al lado."""
    cm.save({"empresa_nombre": "Compulibra SRL"})
    cfg = cm.load()
    assert cfg["empresa_nombre"] == "Compulibra SRL"
    assert cfg["servicio_estado"] == "activo"
    assert cm.almacen_de_secretos() is None


# ── Con almacén: el archivo deja de tener el secreto ────────────────────────

def test_con_almacen_el_archivo_no_tiene_el_token(cm):
    almacen = AlmacenFalso()
    cm.usar_almacen_de_secretos(almacen)

    cm.save({"mp_access_token": TOKEN, "empresa_nombre": "Compulibra SRL"})

    crudo = _json_crudo(cm)
    assert crudo["mp_access_token"] == ""
    # Control positivo del mismo barrido, sobre el mismo archivo: lo que no es
    # secreto sí quedó escrito.
    assert crudo["empresa_nombre"] == "Compulibra SRL"
    # Y el secreto está en el almacén, que es quien lo cifra.
    assert almacen.datos["mp_access_token"] == TOKEN


def test_los_tres_secretos_salen_del_archivo(cm):
    almacen = AlmacenFalso()
    cm.usar_almacen_de_secretos(almacen)

    cm.save({
        "mp_access_token": TOKEN,
        "mp_webhook_secret": FIRMA,
        "email_smtp_password": SMTP_PASS,
        "mp_user_id": "3392230021",
    })

    crudo = _json_crudo(cm)
    for clave in cm.CLAVES_SECRETAS:
        assert crudo[clave] == "", f"{clave} quedó en el archivo"
    # `mp_user_id` NO es secreto: identifica la cuenta, no autoriza nada. Si
    # entrara al almacén, rotar el SECRET_KEY lo volvería ilegible sin ninguna
    # ganancia.
    assert crudo["mp_user_id"] == "3392230021"
    assert "mp_user_id" not in almacen.datos


def test_load_devuelve_el_secreto_del_almacen(cm):
    cm.usar_almacen_de_secretos(AlmacenFalso({"mp_access_token": TOKEN}))
    # Para los ~12 consumidores no cambia nada: misma clave, mismo valor.
    assert cm.load()["mp_access_token"] == TOKEN


def test_el_almacen_le_gana_al_json(cm):
    """Si los dos tienen algo, manda el almacén: es la fuente de verdad desde
    el momento en que tiene el secreto."""
    cm.save({"mp_access_token": "token-viejo-del-json"})
    cm.usar_almacen_de_secretos(AlmacenFalso({"mp_access_token": TOKEN}))
    assert cm.load()["mp_access_token"] == TOKEN


def test_el_json_sigue_sirviendo_mientras_el_almacen_este_vacio(cm):
    """🔑 La ventana entre que se despliega el código nuevo y que corre la
    migración. Durante esos segundos la instancia tiene que seguir cobrando."""
    cm.save({"mp_access_token": TOKEN})          # escrito por la versión vieja
    cm.usar_almacen_de_secretos(AlmacenFalso())  # almacén todavía vacío
    assert cm.load()["mp_access_token"] == TOKEN


def test_guardar_la_razon_social_no_recifra_las_credenciales(cm):
    """`config_router` hace load-modificar-save para guardar los datos de
    empresa. Sin el cotejo, cada edición reescribiría las tres credenciales y
    les movería el `actualizado_at` — que después se lee para saber cuándo se
    tocó una credencial de verdad."""
    almacen = AlmacenFalso({"mp_access_token": TOKEN})
    cm.usar_almacen_de_secretos(almacen)

    cfg = cm.load()
    cfg["empresa_nombre"] = "Compulibra SRL"
    cm.save(cfg)

    assert almacen.sets == []
    assert almacen.datos["mp_access_token"] == TOKEN


def test_cambiar_el_token_si_escribe_el_almacen(cm):
    almacen = AlmacenFalso({"mp_access_token": TOKEN})
    cm.usar_almacen_de_secretos(almacen)
    cm.save({**cm.load(), "mp_access_token": "token-nuevo"})
    assert almacen.sets == ["mp_access_token"]
    assert almacen.datos["mp_access_token"] == "token-nuevo"


def test_vaciar_el_token_lo_borra_del_almacen(cm):
    """Es lo que hace `DELETE /credenciales` de `mp_config_router`: sin esto,
    desconectar la cuenta desde la pantalla dejaría la credencial guardada."""
    almacen = AlmacenFalso({"mp_access_token": TOKEN})
    cm.usar_almacen_de_secretos(almacen)
    cm.save({**cm.load(), "mp_access_token": ""})
    assert "mp_access_token" not in almacen.datos
    assert cm.load()["mp_access_token"] == ""


def test_si_el_almacen_falla_el_json_no_se_escribe(cm):
    """🔴 El orden no es simétrico: primero el almacén, después el JSON. Si
    fuera al revés, un fallo al cifrar dejaría la credencial borrada de los dos
    lados."""
    cm.save({"mp_access_token": TOKEN, "empresa_nombre": "Antes"})
    cm.usar_almacen_de_secretos(AlmacenFalso(rompe_con="mp_access_token"))

    with pytest.raises(RuntimeError):
        cm.save({"mp_access_token": "token-nuevo", "empresa_nombre": "Despues"})

    # El archivo quedó como estaba: ni el nombre nuevo, ni el token borrado.
    crudo = _json_crudo(cm)
    assert crudo["empresa_nombre"] == "Antes"
    assert crudo["mp_access_token"] == TOKEN


# ── La migración ────────────────────────────────────────────────────────────

def test_migrar_saca_el_secreto_del_archivo(cm):
    cm.save({"mp_access_token": TOKEN, "email_smtp_password": SMTP_PASS})
    assert _json_crudo(cm)["mp_access_token"] == TOKEN   # el punto de partida

    almacen = AlmacenFalso()
    cm.usar_almacen_de_secretos(almacen)
    informe = cm.migrar_secretos_al_almacen()

    assert sorted(informe["migradas"]) == ["email_smtp_password", "mp_access_token"]
    assert informe["fallaron"] == {}
    crudo = _json_crudo(cm)
    assert crudo["mp_access_token"] == ""
    assert crudo["email_smtp_password"] == ""
    assert almacen.datos["mp_access_token"] == TOKEN
    assert cm.load()["mp_access_token"] == TOKEN


def test_migrar_es_idempotente(cm):
    """Corre en cada arranque: la segunda vez no tiene que hacer nada."""
    cm.save({"mp_access_token": TOKEN})
    almacen = AlmacenFalso()
    cm.usar_almacen_de_secretos(almacen)
    cm.migrar_secretos_al_almacen()

    informe = cm.migrar_secretos_al_almacen()
    assert informe == {"migradas": [], "ya_estaban": [], "fallaron": {}}
    assert almacen.sets == ["mp_access_token"]   # un solo set en total


def test_migrar_no_pisa_lo_que_el_almacen_ya_tiene(cm):
    """Un `config.json` con una copia vieja del token —restaurada de un backup,
    por ejemplo— no puede pisar la credencial vigente del almacén. Se limpia el
    JSON y el almacén no se toca."""
    cm.save({"mp_access_token": "token-viejo-del-backup"})
    almacen = AlmacenFalso({"mp_access_token": TOKEN})
    cm.usar_almacen_de_secretos(almacen)

    informe = cm.migrar_secretos_al_almacen()

    assert informe["ya_estaban"] == ["mp_access_token"]
    assert informe["migradas"] == []
    assert almacen.sets == []
    assert almacen.datos["mp_access_token"] == TOKEN
    assert _json_crudo(cm)["mp_access_token"] == ""


def test_si_cifrar_falla_el_secreto_sobrevive_en_el_json(cm):
    """🔑 Una instancia sin clave de cifrado tiene que seguir cobrando con la
    credencial que tiene, no quedarse sin ninguna."""
    cm.save({"mp_access_token": TOKEN, "mp_webhook_secret": FIRMA})
    cm.usar_almacen_de_secretos(AlmacenFalso(rompe_con="mp_access_token"))

    informe = cm.migrar_secretos_al_almacen()

    assert informe["fallaron"] == {"mp_access_token": "RuntimeError"}
    assert informe["migradas"] == ["mp_webhook_secret"]
    crudo = _json_crudo(cm)
    # El que falló sigue ahí y sirve…
    assert crudo["mp_access_token"] == TOKEN
    assert cm.load()["mp_access_token"] == TOKEN
    # …y el que sí se pudo migrar salió del archivo igual.
    assert crudo["mp_webhook_secret"] == ""


def test_migrar_no_toca_las_claves_propias_del_producto(cm):
    """🔴 La migración reescribe el JSON **crudo**, sin pasar por `save()`.

    `save()` mergea contra `DEFAULTS`, así que toda clave propia de un producto
    que no esté ahí —los cargos de cubierto de Restolibra, el
    `mp_auto_facturar_reservas` de LibraClub— volvería a su valor por defecto,
    o directamente desaparecería. Ese merge ya borró el token de MercadoPago
    una vez (ver el docstring de `mp_config_router`).
    """
    cm.save({"mp_access_token": TOKEN}, extra_defaults={"cubierto_precio": "1500"})
    assert _json_crudo(cm)["cubierto_precio"] == "1500"

    cm.usar_almacen_de_secretos(AlmacenFalso())
    cm.migrar_secretos_al_almacen()

    crudo = _json_crudo(cm)
    assert crudo["cubierto_precio"] == "1500"
    assert crudo["mp_access_token"] == ""


def test_migrar_sin_config_json_no_hace_nada(cm):
    cm.usar_almacen_de_secretos(AlmacenFalso())
    assert cm.migrar_secretos_al_almacen() == {
        "migradas": [], "ya_estaban": [], "fallaron": {},
    }


def test_migrar_sin_almacen_enchufado_falla(cm):
    with pytest.raises(RuntimeError, match="almacen de secretos"):
        cm.migrar_secretos_al_almacen()


def test_el_informe_no_trae_ningun_valor(cm):
    """El informe se loguea. Uno que imprima el secreto lo muda del JSON a los
    logs, que es una superficie peor: los logs se copian y se mandan."""
    cm.save({"mp_access_token": TOKEN, "email_smtp_password": SMTP_PASS})
    cm.usar_almacen_de_secretos(AlmacenFalso())
    plano = repr(cm.migrar_secretos_al_almacen())
    assert TOKEN not in plano
    assert SMTP_PASS not in plano


# ── El almacén de verdad cumple el contrato ────────────────────────────────

def test_el_repositorio_de_libraauth_sirve_como_almacen(cm, tmp_path, monkeypatch):
    """LibraCore no depende de libraauth, así que este test se saltea si el
    paquete no está. Donde sí corre —los ocho productos, que tienen los dos— es
    el único lugar donde se mide que el contrato `get`/`set` cierra de punta a
    punta, con cifrado real."""
    libraauth_secretos = pytest.importorskip("libraauth.secretos")
    from libraauth.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setenv("SECRET_KEY", "s" * 64)
    db = tmp_path / "auth.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    almacen = libraauth_secretos.SecretosRepository(sessionmaker(bind=engine))

    cm.save({"mp_access_token": TOKEN})
    cm.usar_almacen_de_secretos(almacen)
    cm.migrar_secretos_al_almacen()

    # Salió del JSON, se lee igual, y en la base tampoco está en claro.
    assert _json_crudo(cm)["mp_access_token"] == ""
    assert cm.load()["mp_access_token"] == TOKEN
    crudo_db = db.read_bytes()
    assert TOKEN.encode() not in crudo_db
    assert b"mp_access_token" in crudo_db      # control positivo del barrido
