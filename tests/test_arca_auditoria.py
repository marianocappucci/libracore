"""Quién cambió el par de ARCA, que es la pantalla donde se sube una clave privada.

Este router no escribía **ningún** registro. LibraCargo sí lo hacía con su
router propio, y al normalizar contra éste lo perdió: ahí se vio el hueco.

Lo que se prueba acá:

1. Que el hook se llame en los **cuatro** cambios, con la acción y el usuario.
2. Que **no** se llame en las lecturas ni en `probar`, que no cambia nada.
3. 🔑 Que el par **nunca** viaje adentro del `detalle`. Un log de auditoría con
   la clave privada adentro es peor que no tener log.
4. Que un hook que revienta **no tumbe la request** ni deshaga el cambio.
5. Que un producto que no lo pasa siga funcionando igual — son seis los que ya
   montan este router sin él.
"""

import importlib

import pytest
from conftest import make_valid_cert_key
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore.db import core
from libracore.db.schema import init_core_schema

ADMIN = {"x-rol": "admin"}
USUARIO = {"id": 7, "username": "marta"}


@pytest.fixture
def registro():
    """Lo que el producto recibiría. Una lista, para poder asertar el orden."""
    return []


@pytest.fixture
def app(tmp_path, monkeypatch, registro):
    """El router montado como lo montaría un producto **con** auditoría."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.arca_router as ar
    importlib.reload(ar)

    core.configure(db_path=str(tmp_path / "arca_auditoria_test.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()

    def gate(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    def quien_es() -> dict:
        return USUARIO

    aplicacion = FastAPI()
    aplicacion.include_router(
        ar.build_arca_router(
            usuario_actual=quien_es,
            al_cambiar=lambda accion, detalle, usuario: registro.append(
                (accion, detalle, usuario)),
        ),
        dependencies=[Depends(gate)],
    )
    aplicacion.state.certs_dir = cm.CERTS_DIR
    aplicacion.state.acciones = ar.ACCIONES
    yield aplicacion
    conn.close()
    core._db_path = None


@pytest.fixture
def client(app):
    return TestClient(app)


def _par(tmp_path):
    cert_path, key_path = make_valid_cert_key(tmp_path)
    return open(cert_path, "rb").read(), open(key_path, "rb").read()


def _subir(client, tramo, contenido, ambiente="homologacion"):
    return client.post(
        f"/config/arca/{tramo}", params={"ambiente": ambiente},
        files={"archivo": (f"c.{tramo}", contenido, "application/octet-stream")},
        headers=ADMIN,
    )


def _configurar(client, **extra):
    cuerpo = {"empresa": "negocio", "cuit": "20-11111111-2", "punto_venta": 3,
              "ambiente": "homologacion", "alias": ""}
    cuerpo.update(extra)
    return client.put("/config/arca", json=cuerpo, headers=ADMIN)


# ── 1. Los cuatro cambios ──────────────────────────────────────────────────

def test_los_cuatro_cambios_quedan_registrados_con_su_usuario(client, tmp_path,
                                                              registro, app):
    cert, clave = _par(tmp_path)
    assert _configurar(client).status_code == 200
    assert _subir(client, "certificado", cert).status_code == 200
    assert _subir(client, "clave", clave).status_code == 200
    assert client.delete("/config/arca/credenciales",
                         params={"ambiente": "homologacion"},
                         headers=ADMIN).status_code == 200

    acciones = [a for a, _, _ in registro]
    assert acciones == ["configurar", "certificado", "clave", "borrar"], registro
    # Las cuatro son las que el módulo declara: si alguien agrega una quinta sin
    # ponerla en `ACCIONES`, un consumidor que mapee por esa tupla la ignoraría.
    assert set(acciones) == set(app.state.acciones)
    assert all(u == USUARIO for _, _, u in registro), "perdió quién lo hizo"
    assert all(d["empresa"] == "negocio" for _, d, _ in registro)


def test_el_registro_del_certificado_dice_CUAL_se_subio(client, tmp_path, registro):
    """El modo de fallar que este registro cubre no es "alguien lo cambió": es
    **por cuál**. Sin el sujeto y el número de serie, el asiento no distingue
    una renovación legítima de un certificado ajeno."""
    cert, _ = _par(tmp_path)
    _subir(client, "certificado", cert, ambiente="produccion")

    accion, detalle, _ = registro[-1]
    assert accion == "certificado"
    assert detalle["ambiente"] == "produccion"
    assert detalle["sujeto"], detalle
    assert detalle["numero_de_serie"], detalle
    # `dd-mm-aaaa`, el formato de la familia.
    assert detalle["vence"][2] == "-" and detalle["vence"][5] == "-", detalle


def test_el_borrado_dice_de_QUE_ambiente_fue(client, tmp_path, registro):
    """🔑 Lo encontró una mutación que sobrevivió a la primera batería.

    Borrar es **por ambiente**: sacar el par de homologación después de
    acompañar al cliente es rutina, y sacar el de producción deja al cliente sin
    facturar. Un asiento que diga sólo "borró credenciales" no distingue las dos
    cosas, que es justamente para lo que se mira un log de auditoría.

    Se hace con **los dos pares cargados** a propósito: con uno solo, "borró el
    de homologación" se cumpliría igual por ser el único que había.
    """
    cert, clave = _par(tmp_path)
    for ambiente in ("homologacion", "produccion"):
        _subir(client, "certificado", cert, ambiente=ambiente)
        _subir(client, "clave", clave, ambiente=ambiente)
    registro.clear()

    assert client.delete("/config/arca/credenciales",
                         params={"ambiente": "homologacion"},
                         headers=ADMIN).status_code == 200

    accion, detalle, _ = registro[-1]
    assert accion == "borrar"
    assert detalle["ambiente"] == "homologacion", detalle
    # El control: el otro sigue cargado, o sea que hubo dos que distinguir.
    assert client.get("/config/arca/estado", headers=ADMIN).json()[
        "pares"]["produccion"]["completo"] is True


def test_los_cuatro_registros_dicen_sobre_que_ambiente(client, tmp_path, registro):
    """La versión general de lo de arriba: ninguna de las cuatro acciones puede
    quedar sin decir a qué par se refiere."""
    cert, clave = _par(tmp_path)
    _configurar(client, ambiente="produccion")
    _subir(client, "certificado", cert, ambiente="produccion")
    _subir(client, "clave", clave, ambiente="produccion")
    client.delete("/config/arca/credenciales", params={"ambiente": "produccion"},
                  headers=ADMIN)

    assert len(registro) == 4, registro
    for accion, detalle, _ in registro:
        assert detalle.get("ambiente") == "produccion", (accion, detalle)


# ── 2. Lo que NO se registra ───────────────────────────────────────────────

def test_leer_no_registra_nada(client, tmp_path, registro):
    _subir(client, "certificado", _par(tmp_path)[0])
    registro.clear()

    client.get("/config/arca", headers=ADMIN)
    client.get("/config/arca/estado", headers=ADMIN)
    assert registro == [], "una auditoría que registra lecturas esconde los cambios"


def test_probar_no_registra_porque_no_cambia_nada(client, tmp_path, registro,
                                                  monkeypatch):
    """`probar` autentica contra ARCA y no toca ni el disco ni la fila."""
    import libracore.arca_router as ar

    cert, clave = _par(tmp_path)
    _subir(client, "certificado", cert)
    _subir(client, "clave", clave)
    registro.clear()

    async def autenticar(cert_path, clave_path, ambiente, servicio="wsfe"):
        return {"token": "T", "sign": "S"}

    monkeypatch.setattr(ar.arca_wsaa, "autenticar", autenticar)
    r = client.post("/config/arca/probar", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert registro == []


def test_un_rechazo_no_registra_un_cambio_que_no_paso(client, registro):
    """🔴 El control que hace que el registro signifique algo.

    Un hook llamado antes de validar diría que se subió un certificado que el
    router rechazó y nunca escribió. Ahí el log deja de ser evidencia.
    """
    r = _subir(client, "certificado", b"esto no es un PEM")
    assert r.status_code == 422
    assert registro == []


# ── 3. El par no viaja en el detalle ───────────────────────────────────────

def test_el_par_NUNCA_viaja_en_lo_que_recibe_el_hook(client, tmp_path, registro):
    """🔑 Un log de auditoría con la clave privada adentro es peor que no tenerlo.

    Se busca en el `repr` del detalle entero y no campo por campo: el modo de
    fallar es un campo nuevo que a nadie se le ocurrió mirar.
    """
    cert, clave = _par(tmp_path)
    _configurar(client)
    _subir(client, "certificado", cert)
    _subir(client, "clave", clave)
    client.delete("/config/arca/credenciales", params={"ambiente": "homologacion"},
                  headers=ADMIN)

    assert len(registro) == 4, registro
    for accion, detalle, _ in registro:
        texto = repr(detalle)
        assert "PRIVATE KEY" not in texto, accion
        assert "BEGIN CERTIFICATE" not in texto, accion
        assert clave.decode().strip() not in texto, accion
        assert cert.decode().strip() not in texto, accion


# ── 4. El hook no puede tumbar la request ──────────────────────────────────

def test_si_el_hook_revienta_el_cambio_igual_queda(tmp_path, monkeypatch, caplog):
    """Para cuando corre, el archivo ya está escrito y la fila guardada.

    Fallar acá dejaría al operador creyendo que la subida no salió, con el
    certificado puesto. El peor caso tolerable es un cambio sin registro — y
    tiene que quedar en el log, gritando.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.arca_router as ar
    importlib.reload(ar)

    core.configure(db_path=str(tmp_path / "hook_roto.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()

    def explota(accion, detalle, usuario):
        raise RuntimeError("la tabla de auditoría no existe")

    aplicacion = FastAPI()
    aplicacion.include_router(ar.build_arca_router(al_cambiar=explota))
    client = TestClient(aplicacion)

    cert, _ = _par(tmp_path)
    with caplog.at_level("ERROR"):
        r = client.post("/config/arca/certificado",
                        params={"ambiente": "homologacion"},
                        files={"archivo": ("c.crt", cert, "application/octet-stream")})

    assert r.status_code == 200, r.text
    assert r.json()["pares"]["homologacion"]["tiene_certificado"] is True, (
        "el hook roto deshizo el cambio")
    assert "auditoría" in caplog.text, "el fallo del hook no quedó en el log"

    conn.close()
    core._db_path = None


# ── 5. Los seis que ya lo montan sin nada de esto ──────────────────────────

def test_sin_hook_y_sin_usuario_anda_igual(tmp_path, monkeypatch):
    """El default tiene que ser exactamente lo de antes.

    Es lo que separa "agregar un hook" de "cambiarle la firma a un router que
    montan seis productos". Ninguno pasa estos dos parámetros hoy.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import libracore.config_manager as cm
    importlib.reload(cm)
    import libracore.arca_router as ar
    importlib.reload(ar)

    core.configure(db_path=str(tmp_path / "sin_hook.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    conn.commit()

    aplicacion = FastAPI()
    aplicacion.include_router(ar.build_arca_router())
    client = TestClient(aplicacion)

    cert, clave = _par(tmp_path)
    assert client.get("/config/arca").json() is None
    for tramo, contenido in (("certificado", cert), ("clave", clave)):
        r = client.post(f"/config/arca/{tramo}",
                        params={"ambiente": "homologacion"},
                        files={"archivo": ("c", contenido, "application/octet-stream")})
        assert r.status_code == 200, r.text
    assert client.get("/config/arca/estado").json()["configurado"] is True

    conn.close()
    core._db_path = None
