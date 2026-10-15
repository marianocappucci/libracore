"""El motor le entrega `data/` a la app que corre sin root, y le saca privilegios al contenedor.

Relevamiento de seguridad de la VPS del 2026-10-10, punto 8: seis de ocho productos corrían como
`uid 0` dentro del contenedor. LibraCargo y LibraClub ya usan `uid 10001`; el `USER` del
Dockerfile lo pone cada producto, y esto es lo que pone el motor (`provisioning/sin_root.py`).

🔴 **Lo que se mide acá es lo que se rompe en silencio.** Con la imagen en 10001 y `data/` de root,
el contenedor arranca, `/salud` da 200 y la app no puede escribir un PDF, el `arca_ta`, `errores/`
ni un backup, ni leer una clave de ARCA (modo 600). Se midió el 2026-10-15 con una imagen de prueba
y un `data/` de root: `touch`, `mkdir`, `echo >` y `cat` de un 600 dan `Permission denied`; con
`chown -R 10001:10001` andan los cuatro. Por eso se fija:

1. `entregar_datos`: a quién, hasta dónde llega y de qué se niega a correr;
2. que el deploy lo haga **antes del `up -d`**, después de respaldar y migrar, y **no mueva** una
   instancia a la que no pudo entregarle la carpeta;
3. que el alta lo haga **después** de lo último que el host escribe en `data/` y antes del
   primer arranque;
4. que `data/backups`, si la crea el host, nazca con el dueño de `data/`;
5. `no-new-privileges` en la plantilla del compose y en los composes existentes (vía `pinear_image`),
   **sólo en la app** y no en el sidecar de PostgreSQL.

Lo que NO se puede medir con tests: que `chown` a 10001 funcione (hay que ser root) y que el
proceso arranque como 10001. `lchown` se registra con un doble; lo real se midió con Docker.
"""
import json
import os
import subprocess

import pytest
import yaml

from libracore import provisioning
from libracore.provisioning import panel_admin as pa
from libracore.provisioning import sin_root

from .test_nuevo_cliente import CUIT, cfg, fake_docker, fake_plans  # noqa: F401
from .test_respaldo_antes_de_actualizar import _armar, _contexto_falso  # noqa: F401

# Tomada al importar, antes de que el `conftest` la reemplace por el doble.
_ENTREGAR_REAL = pa._entregar_datos_a_la_app


@pytest.fixture(autouse=True)
def _reset_config():
    provisioning._cfg = None
    yield
    provisioning._cfg = None


@pytest.fixture
def lchown(monkeypatch):
    """Registra `[(ruta, uid, gid)]` en vez de cambiar dueños (sólo root puede)."""
    llamadas: list[tuple[str, int, int]] = []
    monkeypatch.setattr(sin_root.os, "lchown", lambda ruta, uid, gid: llamadas.append((str(ruta), uid, gid)))
    return llamadas


# ── entregar_datos ───────────────────────────────────────────────────────────


def _arbol_de_datos(tmp_path):
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    (data / "arca_certs").mkdir()
    (data / "backups" / "viejo.zip").write_text("zip")
    clave = data / "arca_certs" / "clave.key"
    clave.write_text("secreto")
    clave.chmod(0o600)
    (data / "config.json").write_text("{}")
    return data


def test_entrega_la_carpeta_y_todo_lo_que_tiene_adentro(tmp_path, lchown):
    data = _arbol_de_datos(tmp_path)

    cambios = sin_root.entregar_datos(data)

    esperado = {str(data), str(data / "backups"), str(data / "arca_certs"),
                str(data / "backups" / "viejo.zip"), str(data / "arca_certs" / "clave.key"),
                str(data / "config.json")}
    assert {ruta for ruta, _, _ in lchown} == esperado
    assert cambios == len(esperado)
    assert {(uid, gid) for _, uid, gid in lchown} == {(10001, 10001)}


def test_cubre_el_archivo_en_modo_600(tmp_path, lchown):
    """Es el que la app no podría ni LEER si quedara de root: las claves de ARCA."""
    data = _arbol_de_datos(tmp_path)
    sin_root.entregar_datos(data)
    assert str(data / "arca_certs" / "clave.key") in {ruta for ruta, _, _ in lchown}


def test_no_sigue_symlinks(tmp_path, lchown):
    """Un symlink dentro de `data/` hacia afuera (los hay: `dev-data` apunta a otros árboles)
    no puede arrastrar el `chown` a lo que hay del otro lado."""
    data = _arbol_de_datos(tmp_path)
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    (afuera / "ajeno.txt").write_text("no es de la instancia")
    (data / "enlace").symlink_to(afuera)

    sin_root.entregar_datos(data)

    tocadas = {ruta for ruta, _, _ in lchown}
    assert str(data / "enlace") in tocadas       # el enlace en sí, con `lchown`
    assert str(afuera) not in tocadas
    assert str(afuera / "ajeno.txt") not in tocadas


def test_es_idempotente_y_no_toca_lo_que_ya_es_del_uid(tmp_path, lchown):
    data = _arbol_de_datos(tmp_path)
    assert sin_root.entregar_datos(data, uid=os.getuid(), gid=os.getgid()) == 0
    assert lchown == []


@pytest.mark.parametrize("que", ["inexistente", "archivo", "symlink", "raiz"])
def test_se_niega_a_correr_sobre_algo_que_no_es_una_carpeta_de_datos(tmp_path, lchown, que):
    """Es un `chown -R`: una ruta mal armada no puede terminar en el dueño de otra cosa."""
    if que == "inexistente":
        ruta = tmp_path / "no-existe"
    elif que == "archivo":
        ruta = tmp_path / "archivo"
        ruta.write_text("x")
    elif que == "symlink":
        ruta = tmp_path / "enlace"
        ruta.symlink_to(_arbol_de_datos(tmp_path))
    else:
        ruta = "/"
    with pytest.raises(ValueError):
        sin_root.entregar_datos(ruta)
    assert lchown == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root sí puede cambiar el dueño")
def test_sin_ser_root_propaga_el_permission_error(tmp_path):
    """El que llama decide: el deploy no mueve la instancia y el alta avisa."""
    with pytest.raises(PermissionError):
        sin_root.entregar_datos(_arbol_de_datos(tmp_path))


# ── imagen_sin_root ──────────────────────────────────────────────────────────


def _docker_dice(monkeypatch, salida, codigo=0):
    monkeypatch.setattr(sin_root.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(
        args, codigo, stdout=salida, stderr=""))


@pytest.mark.parametrize("usuario", ["", "\n", "root", "0", "0:0", "root:root"])
def test_una_imagen_que_corre_como_root_no_necesita_la_entrega(monkeypatch, usuario):
    _docker_dice(monkeypatch, usuario)
    assert sin_root.imagen_sin_root("testprod:v1") is False


@pytest.mark.parametrize("usuario", ["libracargo", "10001", "10001:10001", "app"])
def test_una_imagen_con_usuario_propio_si_la_necesita(monkeypatch, usuario):
    _docker_dice(monkeypatch, usuario + "\n")
    assert sin_root.imagen_sin_root("testprod:v1") is True


def test_si_docker_no_sabe_se_entrega_igual(monkeypatch):
    """Ante la duda se entrega: es inocuo para root, y lo contrario deja a una imagen sin root
    sin poder escribir."""
    _docker_dice(monkeypatch, "", codigo=1)
    assert sin_root.imagen_sin_root("testprod:v1") is True


# ── asegurar_directorio ──────────────────────────────────────────────────────


def test_cada_carpeta_nueva_nace_con_el_dueno_de_su_madre(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    cambios: list[tuple[str, int, int]] = []
    monkeypatch.setattr(sin_root, "_cambiar", lambda ruta, uid, gid: cambios.append((ruta, uid, gid)))

    sin_root.asegurar_directorio(data / "backups" / "sub")

    st = data.stat()
    assert (data / "backups" / "sub").is_dir()
    # Sólo las NUEVAS, y cada una con lo que tiene su madre (que acá es lo mismo que `data/`).
    assert cambios == [(str(data / "backups"), st.st_uid, st.st_gid),
                       (str(data / "backups" / "sub"), st.st_uid, st.st_gid)]


def test_si_ya_existe_no_cambia_nada(tmp_path, monkeypatch):
    (tmp_path / "data" / "backups").mkdir(parents=True)
    cambios: list = []
    monkeypatch.setattr(sin_root, "_cambiar", lambda *a: cambios.append(a))
    sin_root.asegurar_directorio(tmp_path / "data" / "backups")
    assert cambios == []


def test_si_no_puede_cambiar_el_dueno_deja_la_carpeta_creada(tmp_path, monkeypatch):
    def _no_root(*a):
        raise PermissionError

    monkeypatch.setattr(sin_root, "_cambiar", _no_root)
    sin_root.asegurar_directorio(tmp_path / "data" / "backups")
    assert (tmp_path / "data" / "backups").is_dir()


def test_el_backup_del_cron_crea_backups_con_el_dueno_de_data(tmp_path, monkeypatch):
    """🔴 Es lo que pasó con la demo de LibraCargo el 2026-08-22: `backup-all` corre como root en
    el host, creó `data/backups` de root, y la app —que ya no era root— no podía escribir su
    backup (`Permission denied`)."""
    import libracore.respaldo as respaldo

    destino = tmp_path / "demo" / "data" / "backups"
    (tmp_path / "demo" / "data").mkdir(parents=True)
    visto = {}

    def _crear(instancia, destino_dir, **k):
        visto["existia_al_crear_el_zip"] = destino_dir.is_dir()
        return destino_dir / "x.zip"

    monkeypatch.setattr(respaldo, "crear_backup", _crear)
    monkeypatch.setattr(respaldo, "verificar_backup", lambda *a, **k: {"tamano_mb": 0, "bases": {}})
    monkeypatch.setattr(pa, "_instancia_del_cliente", lambda *a, **k: object())
    pedidas = []
    monkeypatch.setattr(pa.sin_root, "asegurar_directorio", lambda ruta: pedidas.append(ruta))

    pa._backup_zip({"dir": tmp_path / "demo"}, provisioning.ProductConfig(
        product_name="T", image_name="t:latest", container_prefix="t", db_filename="t.db",
        repo_root=tmp_path), [], lambda *a: None)

    assert pedidas == [destino]


# ── con_security_opt ─────────────────────────────────────────────────────────

APP = (
    "services:\n"
    "  testprod-demo:\n"
    "    image: testprod:v1\n"
    "    container_name: testprod-demo\n"
    "    restart: unless-stopped\n"
    "    environment:\n"
    "      - DATA_DIR=/app/data\n"
    "\n"
    "  testprod-demo-postgres:\n"
    "    image: postgres:16\n"
    "    container_name: testprod-demo-postgres\n"
)


def _servicios(texto):
    return yaml.safe_load(texto)["services"]


def test_agrega_security_opt_solo_al_servicio_de_la_app():
    nuevo = sin_root.con_security_opt(APP, 2)
    servicios = _servicios(nuevo)
    assert servicios["testprod-demo"]["security_opt"] == ["no-new-privileges:true"]
    assert "security_opt" not in servicios["testprod-demo-postgres"]
    # y el resto del servicio sigue donde estaba
    assert servicios["testprod-demo"]["image"] == "testprod:v1"
    assert servicios["testprod-demo"]["environment"] == ["DATA_DIR=/app/data"]


def test_es_idempotente():
    una = sin_root.con_security_opt(APP, 2)
    dos = sin_root.con_security_opt(una, 2)
    assert dos == una and una.count("security_opt:") == 1


def test_suma_a_las_opciones_que_ya_habia_sin_pisarlas():
    con_otra = APP.replace("    restart: unless-stopped\n",
                           "    restart: unless-stopped\n    security_opt:\n      - seccomp=perfil.json\n")
    nuevo = sin_root.con_security_opt(con_otra, 2)
    assert _servicios(nuevo)["testprod-demo"]["security_opt"] == ["seccomp=perfil.json", "no-new-privileges:true"]


def test_si_ya_la_tiene_no_cambia_nada():
    ya = APP.replace("    restart: unless-stopped\n",
                     "    restart: unless-stopped\n    security_opt:\n      - no-new-privileges:true\n")
    assert sin_root.con_security_opt(ya, 2) == ya


def test_un_formato_que_no_conoce_no_se_toca():
    en_linea = APP.replace("    restart: unless-stopped\n",
                           "    restart: unless-stopped\n    security_opt: [seccomp=perfil.json]\n")
    assert sin_root.con_security_opt(en_linea, 2) is None


# ── pinear_image: el compose de una instancia que ya existe ──────────────────


@pytest.fixture
def instancia(tmp_path, fake_docker):  # noqa: F811
    repo_root = tmp_path / "repo"
    (repo_root / "clientes").mkdir(parents=True)
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest",
        container_prefix="testprod", db_filename="testprod.db",
        repo_root=repo_root, base_port=9000,
    )
    cdir = provisioning.get_config().clientes_dir / "demo"
    (cdir / "data").mkdir(parents=True)
    (cdir / "cliente.json").write_text(json.dumps({
        "nombre": "Demo", "slug": "demo", "port": 9000, "container": "testprod-demo", "plan": "basico"}))
    (cdir / "docker-compose.yml").write_text(APP)
    return cdir


def test_el_primer_pin_le_pone_no_new_privileges_a_un_compose_anterior(instancia):
    assert "security_opt" not in (instancia / "docker-compose.yml").read_text()

    pa.pinear_image("demo", "testprod:v2")

    servicios = _servicios((instancia / "docker-compose.yml").read_text())
    assert servicios["testprod-demo"]["security_opt"] == ["no-new-privileges:true"]
    assert "security_opt" not in servicios["testprod-demo-postgres"]
    assert servicios["testprod-demo"]["image"] == "testprod:v2"
    assert servicios["testprod-demo-postgres"]["image"] == "postgres:16"


def test_pinear_de_nuevo_o_hacer_rollback_no_duplica_la_opcion(instancia):
    pa.pinear_image("demo", "testprod:v2")
    pa.pinear_image("demo", "testprod:v3")
    pa.pinear_image("demo", "testprod:v2")
    assert (instancia / "docker-compose.yml").read_text().count("security_opt:") == 1


# ── la plantilla del alta ────────────────────────────────────────────────────


def nc_crear(cfg, **k):  # noqa: F811
    from libracore.provisioning import nuevo_cliente as nc
    return nc.crear_cliente(empresa_cuit=CUIT, nombre="Cliente Uno", slug="cliente-uno",
                            admin_password="secreto123", setup_npm=False, **k)


def test_el_compose_de_un_alta_nuevo_trae_no_new_privileges_solo_en_la_app(cfg):  # noqa: F811
    provisioning.configure(
        product_name="TESTPROD", image_name="testprod:latest", container_prefix="testprod",
        db_filename="testprod.db", repo_root=cfg.repo_root, base_port=9000, postgres=True)
    nc_crear(provisioning.get_config())

    texto = (provisioning.get_config().clientes_dir / "cliente-uno" / "docker-compose.yml").read_text()
    servicios = _servicios(texto)
    assert servicios["testprod-cliente-uno"]["security_opt"] == ["no-new-privileges:true"]
    assert [n for n, s in servicios.items() if "security_opt" in s] == ["testprod-cliente-uno"]


def test_el_alta_entrega_data_despues_de_lo_que_escribe_el_host_y_antes_de_arrancar(cfg, fake_docker, monkeypatch):  # noqa: F811
    """Si fuera antes de `config.json`, ese archivo nacería de root; si fuera después del `up`, el
    primer arranque ya habría corrido contra una carpeta de root."""
    from libracore.provisioning import nuevo_cliente as nc

    visto = {}

    def _entregar(data_dir):
        visto["data_dir"] = data_dir
        visto["config_ya_escrito"] = (data_dir / "config.json").exists()
        visto["subcarpetas"] = sorted(p.name for p in data_dir.iterdir() if p.is_dir())
        fake_docker.append(["ENTREGA"])
        return 7

    monkeypatch.setattr(nc.sin_root, "entregar_datos", _entregar)
    nc_crear(cfg)

    assert visto["data_dir"] == cfg.clientes_dir / "cliente-uno" / "data"
    assert visto["config_ya_escrito"] is True
    assert "logos" in visto["subcarpetas"] and "arca_certs" in visto["subcarpetas"]
    ups = [i for i, a in enumerate(fake_docker) if a[:3] == ["docker", "compose", "up"]]
    assert ups and fake_docker.index(["ENTREGA"]) < ups[0]


def test_el_alta_sigue_si_el_proceso_no_es_root_y_avisa_el_comando(cfg, monkeypatch):  # noqa: F811
    from libracore.provisioning import nuevo_cliente as nc

    def _no_root(data_dir):
        raise PermissionError

    monkeypatch.setattr(nc.sin_root, "entregar_datos", _no_root)
    mensajes: list[str] = []
    info = nc.crear_cliente(empresa_cuit=CUIT, nombre="Cliente Uno", slug="cliente-uno",
                            admin_password="secreto123", setup_npm=False, log=mensajes.append)

    assert info["container"] == "testprod-cliente-uno"
    avisos = [m for m in mensajes if "AVISO" in m and "chown -R 10001:10001" in m]
    assert avisos, mensajes


# ── el deploy: cmd_actualizar ────────────────────────────────────────────────


def _con_entrega(tmp_path, monkeypatch, *, entrega_ok=True, **k):
    eventos, estado = _armar(tmp_path, monkeypatch, **k)

    def _entregar(c, ref):
        eventos.append(("entrega", c["slug"], ref))
        return entrega_ok

    monkeypatch.setattr(pa, "_entregar_datos_a_la_app", _entregar)
    return eventos


def test_el_deploy_entrega_data_antes_de_migrar_y_otra_vez_pegado_al_up(tmp_path, monkeypatch):
    """🔑 Dos veces, por dos motivos. **Antes de migrar**: con una imagen sin root, el contenedor
    efímero de cada migración corre como 10001 (una instancia con SQLite escribiría en `data/`).
    **Pegada al `up -d`**: lo último que el host le hace a `data/`, para recoger lo que el contenedor
    viejo —todavía root— haya escrito mientras migraba. Las dos, después del respaldo (que escribe un
    ZIP de root) y del pin."""
    eventos = _con_entrega(tmp_path, monkeypatch, migraciones=(("alembic", "upgrade", "head"),))

    assert pa.cmd_actualizar(["demo"]) is True

    assert [e[0] for e in eventos] == ["respaldo", "pinear", "entrega", "run", "entrega", "up"]
    assert eventos[4][1] == "demo" and eventos[4][2].startswith("testprod:")


def test_sin_migraciones_entrega_una_sola_vez_pegado_al_up(tmp_path, monkeypatch):
    eventos = _con_entrega(tmp_path, monkeypatch)

    assert pa.cmd_actualizar(["demo"]) is True

    assert [e[0] for e in eventos] == ["respaldo", "pinear", "entrega", "up"]


def test_si_no_puede_entregar_no_mueve_la_instancia_y_repinea(tmp_path, monkeypatch):
    """Una imagen sin root sobre una carpeta de root arranca sana y no puede escribir: peor que
    no desplegar. La instancia queda como estaba."""
    eventos = _con_entrega(tmp_path, monkeypatch, entrega_ok=False,
                           migraciones=(("alembic", "upgrade", "head"),))

    assert pa.cmd_actualizar(["demo"]) is False

    tipos = [e[0] for e in eventos]
    assert "up" not in tipos
    assert "run" not in tipos  # ni siquiera migra: falla en la primera entrega
    pines = [e[1] for e in eventos if e[0] == "pinear"]
    assert pines[-1] == "testprod:v1"  # volvió al anterior


def test_si_la_segunda_entrega_falla_tampoco_mueve_la_instancia(tmp_path, monkeypatch):
    eventos = _con_entrega(tmp_path, monkeypatch, migraciones=(("alembic", "upgrade", "head"),))
    resultados = iter([True, False])
    monkeypatch.setattr(pa, "_entregar_datos_a_la_app", lambda c, ref: next(resultados))

    assert pa.cmd_actualizar(["demo"]) is False

    tipos = [e[0] for e in eventos]
    assert "run" in tipos and "up" not in tipos
    assert [e[1] for e in eventos if e[0] == "pinear"][-1] == "testprod:v1"


def test_el_control_de_que_el_rojo_viene_de_la_entrega(tmp_path, monkeypatch):
    """Con la entrega en verde, el mismo deploy sale bien: lo que lo frena es ella."""
    eventos = _con_entrega(tmp_path, monkeypatch, entrega_ok=True)
    assert pa.cmd_actualizar(["demo"]) is True
    assert "up" in [e[0] for e in eventos]


# ── _entregar_datos_a_la_app ─────────────────────────────────────────────────


def _cliente(tmp_path):
    (tmp_path / "demo" / "data").mkdir(parents=True)
    return {"slug": "demo", "dir": tmp_path / "demo", "container": "testprod-demo"}


def test_con_una_imagen_root_no_toca_nada(tmp_path, monkeypatch):
    """Mientras el producto no migre su imagen, el deploy sigue siendo el de siempre."""
    monkeypatch.setattr(pa.sin_root, "imagen_sin_root", lambda ref: False)
    monkeypatch.setattr(pa.sin_root, "entregar_datos", lambda *a: pytest.fail("no debía entregar"))
    assert _ENTREGAR_REAL(_cliente(tmp_path), "testprod:v1") is True


def test_con_una_imagen_sin_root_entrega_la_carpeta_de_la_instancia(tmp_path, monkeypatch):
    c = _cliente(tmp_path)
    monkeypatch.setattr(pa.sin_root, "imagen_sin_root", lambda ref: True)
    pedidas = []
    monkeypatch.setattr(pa.sin_root, "entregar_datos", lambda d: pedidas.append(d) or 3)
    assert _ENTREGAR_REAL(c, "testprod:v2") is True
    assert pedidas == [tmp_path / "demo" / "data"]


def test_si_el_chown_falla_devuelve_false_y_lo_dice(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pa.sin_root, "imagen_sin_root", lambda ref: True)

    def _no_root(d):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(pa.sin_root, "entregar_datos", _no_root)
    assert _ENTREGAR_REAL(_cliente(tmp_path), "testprod:v2") is False
    assert "10001" in capsys.readouterr().out


def test_una_instancia_sin_carpeta_de_datos_no_es_un_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pa.sin_root, "imagen_sin_root", lambda ref: pytest.fail("no debía preguntar"))
    assert _ENTREGAR_REAL({"slug": "x", "dir": tmp_path / "x"}, "testprod:v2") is True
