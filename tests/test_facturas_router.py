"""La emisión de comprobantes, montada como la montaría un producto.

Lo que se prueba acá es lo que la extracción podría haber roto sin que nadie lo
note: que el tipo de comprobante salga del emisor, que una C no invente IVA, que
una nota copie el original en vez de recalcularlo, y que el hook del producto no
pueda tumbar una emisión ya autorizada.

`ENV=development` en todos los tests: con eso el motor numera localmente y
devuelve un CAE simulado, así que la emisión se ejercita **entera** sin salir a
ARCA. El camino sin CAE —una instancia sin certificado— tiene su propio test,
que apaga esa variable.
"""

import json
import pathlib

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from libracore import config_manager
from libracore import facturas_router as fr
from libracore import pdf_generator as pdf_gen
from libracore.db import core
from libracore.db.schema import init_core_schema

USUARIO = {"id": 1, "username": "admin", "nombre": "Administrador"}


def _montar(tmp_path, monkeypatch, *, al_emitir=None, registrar_cobro=None, dev=True,
            smtp_config=None):
    """Una app con el router montado, como lo haría un producto."""
    core.configure(db_path=str(tmp_path / "comprobantes.db"))
    conn = core.get_connection()
    init_core_schema(conn)
    # 🔑 `facturas.usuario_id` es una FK **real** y la base la hace cumplir, así
    # que emitir con un id inventado revienta en el INSERT. El usuario que queda
    # en cada comprobante es la trazabilidad de quién le facturó qué a quién, y
    # por eso la FK existe: acá se siembra el que la app tendría sembrado.
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) "
        "VALUES (?, ?, ?, ?, ?)",
        (USUARIO["id"], USUARIO["username"], USUARIO["nombre"], "x", "admin"),
    )
    conn.commit()
    conn.close()

    if dev:
        monkeypatch.setenv("ENV", "development")
    else:
        monkeypatch.delenv("ENV", raising=False)

    # 🔴 `FACTURAS_PDF_DIR` se monkeypatchea directo y no vía `DATA_DIR`: la
    # constante se resuelve **al importar** el módulo, así que setear la
    # variable de entorno cuando `pdf_generator` ya está importado no cambia
    # nada y los PDF de los tests terminarían escritos adentro del repo.
    monkeypatch.setattr(pdf_gen, "FACTURAS_PDF_DIR", str(tmp_path / "pdf"))
    # La config de empresa vive en un JSON del cwd y **persiste entre
    # corridas**: sin aislarla, un test que cambia la condición de IVA le
    # cambia el tipo de comprobante a la corrida siguiente.
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(tmp_path / "config.json"))

    def gate_admin(x_rol: str = Header(default="")):
        if x_rol != "admin":
            raise HTTPException(403, "solo administradores")

    app = FastAPI()
    app.include_router(
        fr.build_comprobantes_router(
            usuario_actual=lambda: USUARIO,
            solo_admin=gate_admin,
            al_emitir=al_emitir,
            registrar_cobro=registrar_cobro,
            donde_configurar_smtp="Configuración → Integraciones",
            smtp_config=smtp_config,
        )
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _montar(tmp_path, monkeypatch)


ADMIN = {"x-rol": "admin"}

#: El prefijo real del router. Las rutas se escriben completas a propósito:
#: con un `base_url` que ya lo incluyera, un test seguiría pasando aunque el
#: factory montara el router en otro lado.
API = "/api/facturas"


def _factura(**extra):
    cuerpo = {
        "tipo": 11, "punto_venta": 1, "fecha": "2026-08-27",
        "condicion_venta": "Contado", "client_name": "Juan Perez",
        "client_cuit": "20304050607", "client_iva": "Consumidor Final",
        "items": [{"description": "Alquiler de cancha", "qty": 1, "unit_price": 14000.0}],
    }
    cuerpo.update(extra)
    return cuerpo


def _emitir(client, **extra) -> dict:
    r = client.post(API, json=_factura(**extra))
    assert r.status_code == 200, r.text
    return r.json()


# ── Qué puede emitir el emisor ────────────────────────────────────────────


def test_los_tipos_salen_del_emisor_y_no_de_una_lista_fija(client):
    """🔑 Un monotributista emite C y nada más; un Responsable Inscripto elige.

    Se prueba en las **dos direcciones**: si sólo se mirara el caso
    monotributista, una implementación que devolviera siempre C pasaría igual.
    """
    config_manager.save({"empresa_iva_condition": "Monotributista"})
    datos = client.get(f"{API}/tipos").json()
    assert [t["value"] for t in datos["tipos"]] == [11]
    assert datos["es_monotributista"] is True

    config_manager.save({"empresa_iva_condition": "Responsable Inscripto"})
    datos = client.get(f"{API}/tipos").json()
    assert [t["value"] for t in datos["tipos"]] == [1, 6]
    assert datos["es_monotributista"] is False


# ── Emitir ────────────────────────────────────────────────────────────────


def test_emitir_deja_la_factura_con_sus_items_y_su_cae(client):
    factura = _emitir(client)
    assert factura["numero"] == 1
    assert factura["total"] == 14000.0
    assert factura["cae"], "en modo dev el motor devuelve un CAE simulado"

    # Se relee por el listado y no se mira la respuesta del POST: lo que
    # importa es que quedó guardada, no que el endpoint devolvió algo.
    items = client.get(API).json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == factura["id"]
    assert json.loads(json.dumps(items[0]["items"]))[0]["description"] == "Alquiler de cancha"


def test_la_C_no_discrimina_iva_y_la_B_si(client):
    """🔑 El monotributista no cobra IVA: el neto ES el total.

    El control es la B: sin él, una implementación que dejara el IVA en cero
    siempre pasaría el primer assert.
    """
    c = _emitir(client)
    assert c["iva_amount"] == 0.0
    assert c["subtotal"] == c["total"] == 14000.0

    b = _emitir(client, tipo=6, tax_rate=0.21)
    assert b["iva_amount"] == 2940.0
    assert b["total"] == 16940.0
    assert round(b["subtotal"] + b["iva_amount"], 2) == b["total"]


def test_una_factura_sin_items_no_se_emite(client):
    r = client.post(API, json=_factura(items=[]))
    assert r.status_code == 422, r.text


def test_una_factura_sin_cliente_no_se_emite(client):
    r = client.post(API, json=_factura(client_name="  "))
    assert r.status_code == 422, r.text


def test_sin_ARCA_la_factura_existe_sin_CAE(tmp_path, monkeypatch):
    """Una instancia sin certificado emite igual y queda pendiente de CAE.

    No es un error: la factura tiene número y lo que falta es que ARCA la
    autorice. Es el estado en el que arranca cualquier complejo recién dado de
    alta, así que tiene que funcionar.
    """
    client = _montar(tmp_path, monkeypatch, dev=False)
    factura = _emitir(client)
    assert factura["numero"] == 1
    assert not factura["cae"]


# ── El listado ────────────────────────────────────────────────────────────


def test_el_listado_filtra_por_texto(client):
    _emitir(client)
    _emitir(client, client_name="Marcela Gutierrez")
    assert client.get(API).json()["total"] == 2, "control: sin filtro están las dos"
    assert client.get(API, params={"q": "Marcela"}).json()["total"] == 1
    assert client.get(API, params={"q": "nadie"}).json()["total"] == 0


# ── Notas de crédito y de débito ──────────────────────────────────────────


def test_la_nota_de_credito_copia_el_original_y_lo_referencia(client):
    """🔴 Una NC que anula tiene que decir exactamente lo que anula.

    Si recalculara los importes —con la tasa de hoy, o con un precio que
    cambió— anularía un número distinto del que se facturó, y ante ARCA
    quedarían dos comprobantes que no cierran.

    🔑 **Va sobre una Factura B, y ese es el punto.** La primera versión de este
    test usaba una C, donde el IVA ya es cero y el total iguala al subtotal: ahí
    copiar y recalcular dan **exactamente lo mismo**, así que el test pasaba sin
    distinguir nada. Lo agarró una mutación que puso el IVA de la nota en cero y
    el total en el subtotal, y los 28 tests siguieron verdes. Con una B los tres
    importes son distintos entre sí y la diferencia se ve.
    """
    original = _emitir(client, tipo=6, tax_rate=0.21)
    assert original["iva_amount"] > 0, "el original tiene que discriminar IVA"
    assert original["total"] != original["subtotal"], "y su total no puede ser el neto"

    r = client.post(f"{API}/{original['id']}/nota-credito", headers=ADMIN)
    assert r.status_code == 200, r.text
    nota = r.json()

    assert nota["tipo"] == 8, "una B da una Nota de Crédito B"
    assert nota["total"] == original["total"]
    assert nota["subtotal"] == original["subtotal"]
    assert nota["iva_amount"] == original["iva_amount"]
    assert nota["items"] == original["items"]
    # Lo que la ata al original ante ARCA. Sin esto es un comprobante suelto.
    assert nota["cbte_asoc_tipo"] == original["tipo"]
    assert nota["cbte_asoc_pv"] == original["punto_venta"]
    assert nota["cbte_asoc_nro"] == original["numero"]
    assert "0001-00000001" in nota["observaciones"]


def test_la_nota_de_debito_conserva_la_letra(client):
    original = _emitir(client)
    nota = client.post(f"{API}/{original['id']}/nota-debito", headers=ADMIN).json()
    assert nota["tipo"] == 12, "una C da una Nota de Débito C"


def test_una_nota_no_admite_otra_nota(client):
    """Encadenar notas no existe: una NC se corrige con otra factura, no con la
    NC de la NC."""
    original = _emitir(client)
    nota = client.post(f"{API}/{original['id']}/nota-credito", headers=ADMIN).json()
    r = client.post(f"{API}/{nota['id']}/nota-credito", headers=ADMIN)
    assert r.status_code == 400, r.text


def test_las_notas_son_de_admin(client):
    """Emitir una nota mueve plata ya facturada; no es del mostrador.

    El control es la misma llamada con el rol puesto: sin él, un 403 se
    cumpliría también si la ruta no existiera.
    """
    original = _emitir(client)
    assert client.post(f"{API}/{original['id']}/nota-credito").status_code == 403
    assert client.post(f"{API}/{original['id']}/nota-debito").status_code == 403
    assert client.post(f"{API}/{original['id']}/nota-credito", headers=ADMIN).status_code == 200


def test_el_detalle_muestra_las_notas_colgando_de_su_factura(client):
    original = _emitir(client)
    client.post(f"{API}/{original['id']}/nota-credito", headers=ADMIN)
    client.post(f"{API}/{original['id']}/nota-debito", headers=ADMIN)

    detalle = client.get(f"{API}/{original['id']}").json()
    assert len(detalle["notas_credito"]) == 1
    assert len(detalle["notas_debito"]) == 1
    assert detalle["pendiente"] == original["total"], "nadie la cobró todavía"


# ── El hook del producto ──────────────────────────────────────────────────


def test_el_hook_recibe_los_campos_propios_del_producto(tmp_path, monkeypatch):
    """🔑 El campo que agrega el producto tiene que llegarle al hook.

    Sin `extra="allow"` en el payload, pydantic lo descartaría **en silencio** y
    el hook recibiría un diccionario sin el dato que vino a usar. El síntoma
    sería una venta que nunca queda marcada como facturada, sin ningún error.
    """
    visto = {}

    def al_emitir(factura_id, datos, usuario):
        visto["factura_id"] = factura_id
        visto["venta_id"] = datos.get("venta_id")
        visto["usuario"] = usuario["id"]

    client = _montar(tmp_path, monkeypatch, al_emitir=al_emitir)
    factura = _emitir(client, venta_id=99)

    assert visto["factura_id"] == factura["id"]
    assert visto["venta_id"] == 99
    assert visto["usuario"] == USUARIO["id"]


def test_un_hook_que_explota_NO_deshace_la_emision(tmp_path, monkeypatch):
    """🔴 Para cuando el hook corre, ARCA ya autorizó el comprobante.

    Dejar que su error tumbe la request dejaría al operador creyendo que no se
    emitió, y volvería a emitir: dos comprobantes por el mismo hecho.
    """
    def al_emitir(factura_id, datos, usuario):
        raise RuntimeError("la bandeja no contesta")

    client = _montar(tmp_path, monkeypatch, al_emitir=al_emitir)
    factura = _emitir(client)

    assert factura["cae"], "la factura quedó emitida y autorizada"
    assert client.get(API).json()["total"] == 1


# ── Cuenta corriente ──────────────────────────────────────────────────────


def test_a_credito_se_escribe_el_movimiento_de_cuenta_corriente(client):
    """A crédito la plata no entró: la deuda tiene que quedar registrada.

    ⚠️ El `concepto` dice "Factura" dos veces y **así tiene que quedar**: es el
    texto que hoy tienen los movimientos de Contalibra y Restolibra, y esta
    extracción no puede cambiar lo que se escribe en la base. Si algún día se
    arregla, este test es el que avisa que cambió.
    """
    _emitir(client, condicion_venta="Cuenta Corriente")

    conn = core.get_connection()
    filas = conn.execute(
        "SELECT concepto, monto, medio_pago FROM caja_movimientos"
    ).fetchall()
    conn.close()

    assert len(filas) == 1
    concepto, monto, medio = filas[0][0], filas[0][1], filas[0][2]
    assert concepto == "Factura Factura C 0001-00000001 — Juan Perez"
    assert monto == 14000.0
    assert medio == "Cuenta Corriente"


def test_al_contado_NO_se_escribe_movimiento_de_cuenta_corriente(client):
    """El control del de arriba: sin esto, un movimiento escrito siempre
    pasaría igual."""
    _emitir(client, condicion_venta="Contado")

    conn = core.get_connection()
    cuantos = conn.execute("SELECT count(*) FROM caja_movimientos").fetchone()[0]
    conn.close()
    assert cuantos == 0


# ── Borrar y reintentar ───────────────────────────────────────────────────


def test_con_CAE_no_se_borra(client):
    """🔴 Ese número ya existe ante ARCA: hacerlo desaparecer de la base deja un
    salto en la numeración que no se puede explicar."""
    factura = _emitir(client)
    assert factura["cae"]
    r = client.delete(f"{API}/{factura['id']}", headers=ADMIN)
    assert r.status_code == 400, r.text
    assert "nota de crédito" in r.json()["detail"]


def test_sin_CAE_se_borra(tmp_path, monkeypatch):
    """El control del de arriba: si no se pudiera borrar nunca, aquel 400 no
    probaría que lo que frena es el CAE."""
    client = _montar(tmp_path, monkeypatch, dev=False)
    factura = _emitir(client)
    assert not factura["cae"]
    assert client.delete(f"{API}/{factura['id']}", headers=ADMIN).status_code == 200
    assert client.get(API).json()["total"] == 0


def test_borrar_es_de_admin(client):
    factura = _emitir(client)
    assert client.delete(f"{API}/{factura['id']}").status_code == 403


def test_autorizar_una_factura_que_ya_tiene_CAE_no_emite_otra_vez(client):
    """Lo que se reintenta es el CAE, no la emisión."""
    factura = _emitir(client)
    r = client.post(f"{API}/{factura['id']}/autorizar")
    assert r.status_code == 200, r.text
    assert r.json()["factura"]["cae"] == factura["cae"]
    assert client.get(API).json()["total"] == 1, "no apareció un segundo comprobante"


def test_autorizar_sin_ARCA_configurado_dice_que_falta(tmp_path, monkeypatch):
    client = _montar(tmp_path, monkeypatch, dev=False)
    factura = _emitir(client)
    r = client.post(f"{API}/{factura['id']}/autorizar")
    assert r.status_code == 400, r.text
    assert "ARCA no está configurado" in r.json()["detail"]


# ── Mail ──────────────────────────────────────────────────────────────────


def test_sin_SMTP_el_mensaje_dice_DONDE_configurarlo(client):
    """🔑 Y con el texto que pasó el producto, no uno del motor.

    Cada producto tiene la pantalla en otro lado —Contalibra en "Email",
    Restolibra en "Integraciones"—, y mandar a la solapa equivocada es peor que
    no decir nada.
    """
    factura = _emitir(client)
    r = client.post(f"{API}/{factura['id']}/enviar-email", json={"email": "a@b.com"})
    assert r.status_code == 400, r.text
    assert "Configuración → Integraciones" in r.json()["detail"]


# ── Un comprobante que no existe ──────────────────────────────────────────


@pytest.mark.parametrize(
    "metodo,ruta",
    [
        ("get", "/99999"),
        ("post", "/99999/duplicar"),
        ("post", "/99999/autorizar"),
        ("delete", "/99999"),
        ("post", "/99999/nota-credito"),
        ("post", "/99999/nota-debito"),
    ],
)
def test_un_comprobante_inexistente_da_404(client, metodo, ruta):
    r = getattr(client, metodo)(f"{API}{ruta}", headers=ADMIN)
    assert r.status_code == 404, r.text


# ── El borrador ───────────────────────────────────────────────────────────


def _textos_del_pdf(datos: bytes) -> list[str]:
    """Los textos dibujados en el PDF, sólo con la biblioteca estándar.

    No es un extractor general: los PDF de fpdf2 traen los streams con
    FlateDecode y el texto en operadores `(...) Tj`. Se decodifica en **cp1252**
    y no en latin-1 porque es lo que el motor declara —`WinAnsiEncoding`—: con
    latin-1 el guión largo y las comillas curvas salen vacíos y parecen un
    defecto del PDF que no existe.
    """
    import re
    import zlib

    textos = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", datos, re.S):
        try:
            contenido = zlib.decompress(m.group(1))
        except zlib.error:
            continue
        for t in re.finditer(rb"\((?:[^()\\]|\\.)*\)\s*Tj", contenido):
            crudo = t.group(0)
            crudo = crudo[1:crudo.rindex(b")")]
            crudo = crudo.replace(b"\\(", b"(").replace(b"\\)", b")")
            textos.append(crudo.decode("cp1252").strip())
    return textos


def test_el_borrador_muestra_los_importes_de_verdad(client):
    """🔴 El borrador es lo que alguien mira **antes** de quemar un número.

    Un número fiscal no se puede devolver, así que si el borrador miente el
    error se descubre con el comprobante ya emitido. Se leen los textos del PDF
    y no sólo el `200`: una mutación que puso la tasa en cero en este endpoint
    pasó los 28 tests sin que nadie la viera, justamente porque acá sólo se
    miraba que devolviera algo.
    """
    r = client.post(
        f"{API}/borrador-pdf", json=_factura(tipo=6, tax_rate=0.21)
    )
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")

    textos = _textos_del_pdf(r.content)
    assert "IVA 21%" in textos, textos
    # 14.000 + 21% = 16.940. Si el endpoint ignorara la tasa, acá diría 14.000.
    assert "$ 2.940,00" in textos, textos
    assert "$ 16.940,00" in textos, textos


def test_el_borrador_de_una_C_no_inventa_IVA(client):
    """El control del de arriba: con una C el IVA va en cero, y el neto ES el
    total. Sin este caso, un endpoint que sumara 21% siempre pasaría aquél."""
    r = client.post(f"{API}/borrador-pdf", json=_factura(tipo=11))
    assert r.status_code == 200, r.text

    textos = _textos_del_pdf(r.content)
    assert "IVA 0%" in textos, textos
    assert "$ 14.000,00" in textos, textos
    assert "$ 16.940,00" not in textos


def test_el_borrador_NO_guarda_ni_numera(client):
    """No toca la base: es una previsualización, no una emisión."""
    client.post(f"{API}/borrador-pdf", json=_factura())
    assert client.get(API).json()["total"] == 0, "no se emitió nada"


# ── Cobrar ────────────────────────────────────────────────────────────────


def test_cobrar_imputa_el_pago_contra_la_factura(client):
    """El cobro deja el comprobante saldado y el movimiento en la caja.

    Se mira el `pendiente` del detalle y no sólo el `200`: un endpoint que
    escribiera el movimiento sin imputarlo a la factura devolvería 200 igual, y
    el comprobante quedaría figurando como impago para siempre.
    """
    factura = _emitir(client)
    assert client.get(f"{API}/{factura['id']}").json()["pendiente"] == 14000.0

    r = client.post(
        f"{API}/{factura['id']}/cobrar",
        json={"pagos": [{"medio_id": "efectivo", "monto": 14000.0, "referencia": ""}]},
    )
    assert r.status_code == 200, r.text

    detalle = r.json()
    assert detalle["total_cobrado"] == 14000.0
    assert detalle["pendiente"] == 0.0
    assert len(detalle["cobros"]) == 1


def test_un_cobro_parcial_deja_el_resto_pendiente(client):
    """El control del de arriba: si `pendiente` fuera siempre cero, aquel test
    pasaría igual."""
    factura = _emitir(client)
    client.post(
        f"{API}/{factura['id']}/cobrar",
        json={"pagos": [{"medio_id": "efectivo", "monto": 4000.0}]},
    )
    detalle = client.get(f"{API}/{factura['id']}").json()
    assert detalle["total_cobrado"] == 4000.0
    assert detalle["pendiente"] == 10000.0


def test_la_cuenta_corriente_no_es_un_medio_de_COBRO(client):
    """🔴 Cobrar con "cuenta corriente" es no cobrar: es mover la deuda de lugar.

    Aceptarlo daría por saldado un comprobante que nadie pagó. El motor lo
    rechaza y acá se verifica que el 400 llegue a la pantalla en vez de un 500.
    """
    factura = _emitir(client)
    r = client.post(
        f"{API}/{factura['id']}/cobrar",
        json={"pagos": [{"medio_id": "cuenta_corriente", "monto": 14000.0}]},
    )
    assert r.status_code == 400, r.text

    # Y no dejó nada escrito: el rechazo es antes de la primera escritura.
    assert client.get(f"{API}/{factura['id']}").json()["pendiente"] == 14000.0


def test_autorizar_pide_el_CAE_a_ARCA_y_lo_guarda(tmp_path, monkeypatch):
    """El camino exitoso del reintento, con ARCA simulado.

    Lo que se prueba es el cableado del endpoint —que autentique, pida el CAE,
    lo guarde y regenere el PDF—, no el cliente de ARCA, que tiene sus propios
    tests. Sin esto el único camino cubierto era el del error.
    """
    client = _montar(tmp_path, monkeypatch, dev=False)
    factura = _emitir(client)
    assert not factura["cae"], "arranca sin CAE"

    from libracore.db import arca_config as db_arca

    cert = tmp_path / "cert.pem"
    clave = tmp_path / "clave.key"
    cert.write_text("x")
    clave.write_text("x")
    db_arca.crear_arca_config(
        "empresa", "30712345679", 1, str(clave), str(cert), "homologacion"
    )

    async def autenticar_falso(*a, **kw):
        return {"token": "t", "sign": "s"}

    async def cae_falso(*a, **kw):
        return {"cae": "75123456789012", "cae_vto": "20260906"}

    monkeypatch.setattr(fr.arca_wsaa, "autenticar", autenticar_falso)
    monkeypatch.setattr(fr.arca_wsfe, "solicitar_cae", cae_falso)

    r = client.post(f"{API}/{factura['id']}/autorizar")
    assert r.status_code == 200, r.text

    # Se relee del servidor: lo que importa es que el CAE quedó GUARDADO, no
    # que el endpoint devolvió algo.
    guardada = client.get(f"{API}/{factura['id']}").json()["factura"]
    assert guardada["cae"] == "75123456789012"
    assert guardada["cae_vto"] == "20260906"


def test_mandar_el_comprobante_adjunta_el_PDF_y_lo_nombra(client, monkeypatch):
    """Con SMTP cargado, el envío arma el adjunto y la etiqueta del comprobante.

    Se intercepta el `enviar_comprobante` del motor —que tiene sus propios
    tests— y se mira **con qué lo llamaron**: el PDF que existe en disco, y la
    etiqueta con el tipo y el número, que es lo que el cliente lee en el asunto.
    """
    from libracore import config_manager as cm

    cm.save({
        "email_smtp_host": "smtp.example.com", "email_smtp_user": "yo@example.com",
        "email_smtp_password": "x", "empresa_nombre": "Complejo Centro",
    })

    llamado = {}

    def sender_falso(**kw):
        llamado.update(kw)

    monkeypatch.setattr(fr.email_sender, "enviar_comprobante", sender_falso)

    factura = _emitir(client)
    r = client.post(
        f"{API}/{factura['id']}/enviar-email", json={"email": "cliente@example.com"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    assert llamado["to_email"] == "cliente@example.com"
    assert llamado["to_name"] == "Juan Perez"
    assert llamado["factura_label"] == "FACTURA C 0001-00000001"
    assert llamado["total"] == 14000.0
    # El adjunto tiene que ser un archivo que exista: mandar la ruta de un PDF
    # que no está deja al cliente con un mail sin comprobante.
    assert pathlib.Path(llamado["pdf_path"]).is_file()


def test_sin_direccion_no_se_manda_nada(client, monkeypatch):
    """El control: un 422 antes de tocar el SMTP."""
    from libracore import config_manager as cm

    cm.save({"email_smtp_host": "smtp.example.com", "email_smtp_user": "yo@example.com"})

    def sender_que_no_deberia_correr(**kw):
        raise AssertionError("no tendría que haberse llamado")

    monkeypatch.setattr(fr.email_sender, "enviar_comprobante", sender_que_no_deberia_correr)

    factura = _emitir(client)
    r = client.post(f"{API}/{factura['id']}/enviar-email", json={"email": "   "})
    assert r.status_code == 422, r.text


def test_el_alta_devuelve_el_comprobante_PELADO(client):
    """🔴 La pantalla de alta hace `navigate(`/facturas/${factura.id}`)` con esto.

    Envuelto en el detalle —`{"factura": {...}, "cobros": [...]}`— `factura.id`
    queda `undefined` y el usuario aterriza en `/facturas/undefined` justo
    después de emitir. Es lo que devolvían las dos copias de Contalibra y
    Restolibra, y la primera versión de este factory lo cambió sin querer: no lo
    agarró ninguna de las dos suites, ni el volcado A/B de la migración, porque
    el arnés estaba escrito tolerante a las dos formas.

    El detalle completo lo da `GET /{id}`, que es otro endpoint.
    """
    r = client.post(API, json=_factura())
    cuerpo = r.json()

    assert "id" in cuerpo, "la pantalla necesita el id acá arriba"
    assert "factura" not in cuerpo, "no viene envuelto en el detalle"
    assert cuerpo["numero"] == 1
    assert cuerpo["total"] == 14000.0

    # Control: el detalle SÍ viene envuelto, y ese es el que tiene las notas.
    detalle = client.get(f"{API}/{cuerpo['id']}").json()
    assert "factura" in detalle
    assert "notas_credito" in detalle


# ── El cobro, cuando el producto lleva su propia caja ─────────────────────


def test_el_producto_puede_reemplazar_la_escritura_del_cobro(tmp_path, monkeypatch):
    """🔑 Existe por LibraClub, y el motivo no es de estilo.

    Ese producto lleva la caja **por turno**, con su arqueo al cerrar, y el
    default escribe el movimiento **sin `turno_id`**: la plata entraba y ningún
    cierre la contaba — que es exactamente lo que una caja por turno evita.

    Se verifica lo que recibe el reemplazo **y** que el default no corrió: si
    corrieran los dos, el ingreso quedaría contado dos veces.
    """
    recibido = {}

    def registrar(factura, pagos, *, fecha, caja_id, usuario):
        recibido.update(
            factura_id=factura["id"], pagos=pagos, fecha=fecha,
            caja_id=caja_id, usuario=usuario,
        )

    client = _montar(tmp_path, monkeypatch, registrar_cobro=registrar)
    factura = _emitir(client)

    r = client.post(
        f"{API}/{factura['id']}/cobrar",
        json={"pagos": [{"medio_id": "efectivo", "monto": 14000.0}], "caja_id": 7},
    )
    assert r.status_code == 200, r.text

    assert recibido["factura_id"] == factura["id"]
    assert recibido["pagos"] == [{"medio_id": "efectivo", "monto": 14000.0}]
    assert recibido["caja_id"] == 7
    assert recibido["usuario"] == USUARIO

    # 🔴 El default NO escribió: el comprobante sigue sin cobrar. Sin esta
    # aserción, un factory que llamara a los dos pasaría igual.
    assert r.json()["pendiente"] == 14000.0
    assert r.json()["cobros"] == []


def test_lo_que_levanta_el_reemplazo_sale_tal_cual(tmp_path, monkeypatch):
    """El producto decide su propio error: LibraClub devuelve 409 si no hay caja
    abierta, porque un cobro sin turno queda fuera de todo arqueo."""
    def registrar(factura, pagos, *, fecha, caja_id, usuario):
        raise HTTPException(409, "No hay una caja abierta.")

    client = _montar(tmp_path, monkeypatch, registrar_cobro=registrar)
    factura = _emitir(client)

    r = client.post(
        f"{API}/{factura['id']}/cobrar",
        json={"pagos": [{"medio_id": "efectivo", "monto": 14000.0}]},
    )
    assert r.status_code == 409, r.text
    assert "caja abierta" in r.text


def test_sin_reemplazo_el_cobro_sigue_siendo_el_de_siempre(tmp_path, monkeypatch):
    """El control: los dos productos que ya usan el factory no cambian.

    Sin esto, un factory que ignorara el default y no escribiera nunca pasaría
    los dos tests de arriba.
    """
    client = _montar(tmp_path, monkeypatch)
    factura = _emitir(client)

    r = client.post(
        f"{API}/{factura['id']}/cobrar",
        json={"pagos": [{"medio_id": "efectivo", "monto": 14000.0}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pendiente"] == 0.0
    assert len(r.json()["cobros"]) == 1


# ── De dónde sale el SMTP con el que se manda ─────────────────────────────
#
# 🔴 En la familia hay DOS configuraciones de SMTP: la de libraauth —cifrada,
# la que configura la pantalla compartida— y la de `config.json`, que es la que
# este router leía. El producto le pasa la primera vía `smtp_config`; lo que se
# prueba acá es que **le haga caso**, y que una instancia sin migrar siga
# mandando.


class _SmtpFalso:
    """Duck-type de `libraauth.email_sender.SmtpConfig`, que LibraCore no importa."""

    def __init__(self, **kw):
        self.host = kw.get("host", "")
        self.port = kw.get("port", 587)
        self.user = kw.get("user", "")
        self.password = kw.get("password", "")
        self.from_email = kw.get("from_email", "")
        self.from_name = kw.get("from_name", "")

    @property
    def configurado(self) -> bool:
        return bool(self.host and self.user)


def test_manda_con_el_SMTP_del_producto_y_no_con_el_de_config_json(tmp_path, monkeypatch):
    """🔑 Los dos stores están cargados, **y con datos distintos**.

    Ese es el punto: con `config.json` vacío, un router que ignorara el resolver
    pasaría este test igual —las dos ramas darían el mismo mail sin mandar—. La
    única forma de ver cuál de los dos ganó es que digan cosas diferentes.
    """
    from libracore import config_manager as cm

    resolver = lambda: _SmtpFalso(
        host="smtp.libraauth", port=465, user="cifrado@example.com",
        password="el-bueno", from_email="facturas@example.com", from_name="Ventas",
    )
    client = _montar(tmp_path, monkeypatch, smtp_config=resolver)
    cm.save({
        "email_smtp_host": "smtp.viejo", "email_smtp_user": "viejo@example.com",
        "email_smtp_password": "el-viejo", "empresa_nombre": "Complejo Centro",
    })

    llamado = {}
    monkeypatch.setattr(fr.email_sender, "enviar_comprobante", lambda **kw: llamado.update(kw))

    factura = _emitir(client)
    r = client.post(f"{API}/{factura['id']}/enviar-email", json={"email": "c@example.com"})
    assert r.status_code == 200, r.text

    assert llamado["smtp_host"] == "smtp.libraauth"
    assert llamado["smtp_port"] == 465
    assert llamado["smtp_user"] == "cifrado@example.com"
    assert llamado["smtp_password"] == "el-bueno"
    assert llamado["from_email"] == "facturas@example.com"
    assert llamado["from_name"] == "Ventas"
    # El nombre de la empresa NO viaja en el SMTP: sigue saliendo de la config
    # del producto, y el cliente lo lee en el cuerpo del mail.
    assert llamado["empresa_nombre"] == "Complejo Centro"


def test_una_instancia_sin_migrar_sigue_mandando_por_config_json(tmp_path, monkeypatch):
    """⚠️ La red de seguridad, y por qué existe.

    El resolver contesta —el producto lo inyectó— pero no tiene nada: ni base
    ni entorno. Se relevó la flota antes de escribir esta rama y **hoy no se
    ejecuta en ninguna instancia**; se deja porque es la dirección segura. Sin
    ella, una instancia con datos sólo en `config.json` dejaría de mandar
    comprobantes **sin ningún síntoma**: el endpoint contesta 400 y el que
    factura no mira la respuesta del mail.
    """
    from libracore import config_manager as cm

    client = _montar(tmp_path, monkeypatch, smtp_config=lambda: _SmtpFalso())
    cm.save({
        "email_smtp_host": "smtp.viejo", "email_smtp_user": "viejo@example.com",
        "email_smtp_password": "el-viejo",
    })

    llamado = {}
    monkeypatch.setattr(fr.email_sender, "enviar_comprobante", lambda **kw: llamado.update(kw))

    factura = _emitir(client)
    r = client.post(f"{API}/{factura['id']}/enviar-email", json={"email": "c@example.com"})
    assert r.status_code == 200, r.text
    assert llamado["smtp_host"] == "smtp.viejo"
    # Sin `email_from`, el remitente es el propio usuario del SMTP.
    assert llamado["from_email"] == "viejo@example.com"


def test_sin_SMTP_en_ninguno_de_los_dos_el_400_dice_donde_configurarlo(tmp_path, monkeypatch):
    """El control negativo: que el 400 siga saliendo, y con el texto del producto."""
    client = _montar(tmp_path, monkeypatch, smtp_config=lambda: _SmtpFalso())

    def sender_que_no_deberia_correr(**kw):
        raise AssertionError("no tendría que haberse llamado")

    monkeypatch.setattr(fr.email_sender, "enviar_comprobante", sender_que_no_deberia_correr)

    factura = _emitir(client)
    r = client.post(f"{API}/{factura['id']}/enviar-email", json={"email": "c@example.com"})
    assert r.status_code == 400, r.text
    assert "Configuración → Integraciones" in r.json()["detail"]
