"""Conservar `demo_codigos` a través del reset nocturno (ADR-039).

Lo que fijan, en orden de lo que duele si falla:

1. 🔴 Que **los secretos no pasen por el host**: `$POSTGRES_USER` y
   `$POSTGRES_DB` van sin expandir dentro del `sh -c` del sidecar.
2. Que un `devolver` fallido **no borre** el volcado: es lo único que queda de
   los códigos que se le entregaron a alguien.
3. Que el volcado nazca 0600 y que sin tabla no se escriba nada.
4. Que la CLI imprima las mismas líneas que el bloque bash que reemplaza, para
   que `/var/log/demo_reset.log` se siga leyendo igual.

No hay Docker acá: el ejecutor es un doble que registra las órdenes.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

from libracore.provisioning import demo_codigos as dc

DUMP = "SET statement_timeout = 0;\nCOPY public.demo_codigos (codigo) FROM stdin;\nabc\n\\.\n"


class Falso:
    """Ejecutor falso: contesta según un guion y guarda cada llamada."""

    def __init__(self, existe=True, filas=3, dump=DUMP, falla_en=None):
        self.llamadas: list[tuple[list[str], str | None]] = []
        self.existe, self.filas, self.dump, self.falla_en = existe, filas, dump, falla_en

    def __call__(self, args, *, entrada=None, timeout=60):
        self.llamadas.append((list(args), entrada))
        script = args[-1]
        for marca in (self.falla_en or []):
            if marca in script:
                return subprocess.CompletedProcess(args, 1, "", "boom\nfalla simulada")
        if "information_schema" in script:
            return subprocess.CompletedProcess(args, 0, "1\n" if self.existe else "", "")
        if "pg_dump" in script:
            return subprocess.CompletedProcess(args, 0, self.dump, "")
        if "COUNT(*)" in script:
            return subprocess.CompletedProcess(args, 0, f"{self.filas}\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")  # psql de restauracion


def scripts(f):
    return [a[-1] for a, _ in f.llamadas]


# --- guardar ---------------------------------------------------------------

def test_guardar_arma_las_ordenes_con_las_variables_sin_expandir(tmp_path):
    f = Falso()
    assert dc.guardar("demo_pg", tmp_path / "v.sql", correr=f) == 3
    for args, _ in f.llamadas:
        assert args[:3] == ["docker", "exec", "demo_pg"] and args[3:5] == ["sh", "-c"]
    existe, dump, cuenta = scripts(f)
    assert existe == ('psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1 FROM '
                      "information_schema.tables WHERE table_name = 'demo_codigos'\"")
    assert dump == 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --data-only --table=demo_codigos'
    assert cuenta == 'psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT COUNT(*) FROM demo_codigos"'


def test_la_contrasena_y_el_usuario_nunca_pasan_por_el_host(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "usuario-del-host")
    monkeypatch.setenv("POSTGRES_PASSWORD", "clave-del-host")
    f = Falso()
    dc.guardar("demo_pg", tmp_path / "v.sql", correr=f)
    for args, _ in f.llamadas:
        assert "usuario-del-host" not in " ".join(args)
        assert "clave-del-host" not in " ".join(args)
        assert "PASSWORD" not in " ".join(args)


def test_guardar_escribe_el_volcado_con_permisos_600(tmp_path):
    destino = tmp_path / "v.sql"
    old = os.umask(0)  # el peor caso: ningun bit enmascarado
    try:
        assert dc.guardar("demo_pg", destino, correr=Falso()) == 3
    finally:
        os.umask(old)
    assert destino.read_text() == DUMP
    assert stat.S_IMODE(destino.stat().st_mode) == 0o600


def test_guardar_reemplaza_un_archivo_previo_abierto_de_permisos(tmp_path):
    destino = tmp_path / "v.sql"
    destino.write_text("viejo")
    destino.chmod(0o644)
    dc.guardar("demo_pg", destino, correr=Falso())
    assert destino.read_text() == DUMP
    assert stat.S_IMODE(destino.stat().st_mode) == 0o600


def test_guardar_sin_tabla_no_escribe_nada_y_devuelve_none(tmp_path):
    destino = tmp_path / "v.sql"
    f = Falso(existe=False)
    assert dc.guardar("demo_pg", destino, correr=f) is None
    assert not destino.exists()
    assert len(f.llamadas) == 1  # ni pg_dump ni conteo


def test_guardar_con_la_tabla_vacia_devuelve_cero(tmp_path):
    assert dc.guardar("demo_pg", tmp_path / "v.sql", correr=Falso(filas=0)) == 0


@pytest.mark.parametrize("falla", [["information_schema"], ["pg_dump"], ["COUNT"]])
def test_guardar_si_docker_falla_levanta_y_no_deja_volcado(tmp_path, falla):
    destino = tmp_path / "v.sql"
    with pytest.raises(dc.DockerFalla):
        dc.guardar("demo_pg", destino, correr=Falso(falla_en=falla))
    assert not destino.exists()


def test_guardar_si_docker_no_esta_instalado_es_dockerfalla(tmp_path):
    def sin_docker(args, *, entrada=None, timeout=60):
        raise FileNotFoundError("docker")

    with pytest.raises(dc.DockerFalla):
        dc.guardar("demo_pg", tmp_path / "v.sql", correr=sin_docker)


def test_base_explicita_reemplaza_a_postgres_db(tmp_path):
    f = Falso()
    dc.guardar("demo_pg", tmp_path / "v.sql", "libraauth_demo", correr=f)
    for s in scripts(f):
        assert "-d libraauth_demo" in s
        assert "$POSTGRES_DB" not in s
        assert '-U "$POSTGRES_USER"' in s


@pytest.mark.parametrize("base", ["x; rm -rf /", "a b", "x'y", 'x"y', "$(id)", "-d", "", "1base", "a-b"])
def test_base_invalida_se_rechaza_antes_de_correr_nada(tmp_path, base):
    f = Falso()
    with pytest.raises(ValueError):
        dc.guardar("demo_pg", tmp_path / "v.sql", base, correr=f)
    with pytest.raises(ValueError):
        dc.devolver("demo_pg", tmp_path / "v.sql", base, correr=f)
    assert f.llamadas == []


@pytest.mark.parametrize("sidecar", ["-ti", "a b", "x;y", "", "$(id)"])
def test_sidecar_invalido_se_rechaza(tmp_path, sidecar):
    f = Falso()
    with pytest.raises(ValueError):
        dc.guardar(sidecar, tmp_path / "v.sql", correr=f)
    assert f.llamadas == []


# --- devolver --------------------------------------------------------------

def test_devolver_pasa_el_volcado_por_stdin_y_borra_el_archivo(tmp_path):
    archivo = tmp_path / "v.sql"
    archivo.write_text(DUMP)
    f = Falso(filas=2)
    assert dc.devolver("demo_pg", archivo, correr=f) == 2
    (args, entrada), (cuenta, _) = f.llamadas
    assert args == ["docker", "exec", "-i", "demo_pg", "sh", "-c",
                    'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"']
    assert entrada == DUMP
    assert "COUNT(*)" in cuenta[-1]
    assert not archivo.exists()


def test_devolver_con_base_explicita(tmp_path):
    archivo = tmp_path / "v.sql"
    archivo.write_text(DUMP)
    f = Falso()
    dc.devolver("demo_pg", archivo, "otra", correr=f)
    assert all("-d otra" in s for s in scripts(f))


def test_devolver_sin_archivo_no_hace_nada(tmp_path):
    f = Falso()
    assert dc.devolver("demo_pg", tmp_path / "no-existe.sql", correr=f) is None
    assert f.llamadas == []


def test_devolver_con_archivo_vacio_no_hace_nada_y_lo_deja(tmp_path):
    archivo = tmp_path / "v.sql"
    archivo.write_text("")
    f = Falso()
    assert dc.devolver("demo_pg", archivo, correr=f) is None
    assert f.llamadas == []
    assert archivo.exists()


def test_devolver_con_error_de_psql_no_borra_el_archivo(tmp_path):
    archivo = tmp_path / "v.sql"
    archivo.write_text(DUMP)
    with pytest.raises(dc.NoSePudieronDevolver, match="falla simulada"):
        dc.devolver("demo_pg", archivo, correr=Falso(falla_en=["ON_ERROR_STOP"]))
    assert archivo.read_text() == DUMP


# --- CLI -------------------------------------------------------------------

def _cli(capsys, argv, f):
    codigo = dc.main(argv, correr=f)
    salida = capsys.readouterr()
    return codigo, salida.out, salida.err


def test_cli_guardar_imprime_la_linea_de_siempre(tmp_path, capsys):
    codigo, out, _ = _cli(capsys, ["guardar", "--sidecar", "demo_pg", "--archivo", str(tmp_path / "v.sql")],
                          Falso(filas=4))
    assert (codigo, out) == (0, "codigos de acceso a preservar: 4\n")


def test_cli_guardar_sin_tabla_sale_en_cero(tmp_path, capsys):
    codigo, out, _ = _cli(capsys, ["guardar", "--sidecar", "demo_pg", "--archivo", str(tmp_path / "v.sql")],
                          Falso(existe=False))
    assert (codigo, out) == (0, "todavia no existe demo_codigos: nada que preservar\n")


def test_cli_guardar_con_docker_caido_sale_distinto_de_cero(tmp_path, capsys):
    codigo, out, err = _cli(capsys, ["guardar", "--sidecar", "demo_pg", "--archivo", str(tmp_path / "v.sql")],
                            Falso(falla_en=["information_schema"]))
    assert codigo == dc.EXIT_DOCKER != 0
    assert out == "" and "[ERROR]" in err


def test_cli_devolver_imprime_la_linea_de_siempre_y_borra(tmp_path, capsys):
    archivo = tmp_path / "v.sql"
    archivo.write_text(DUMP)
    codigo, out, _ = _cli(capsys, ["devolver", "--sidecar", "demo_pg", "--archivo", str(archivo)], Falso(filas=4))
    assert (codigo, out) == (0, "codigos de acceso devueltos: 4\n")
    assert not archivo.exists()


def test_cli_devolver_sin_archivo_sale_en_cero_y_callada(tmp_path, capsys):
    codigo, out, _ = _cli(capsys, ["devolver", "--sidecar", "demo_pg", "--archivo", str(tmp_path / "no.sql")],
                          Falso())
    assert (codigo, out) == (0, "")


def test_cli_devolver_fallido_imprime_el_ojo_y_sale_distinto_de_cero(tmp_path, capsys):
    archivo = tmp_path / "v.sql"
    archivo.write_text(DUMP)
    codigo, out, _ = _cli(capsys, ["devolver", "--sidecar", "demo_pg", "--archivo", str(archivo)],
                          Falso(falla_en=["ON_ERROR_STOP"]))
    assert codigo == dc.EXIT_NO_DEVUELTOS != 0
    assert out == "OJO: no se pudieron devolver los codigos de acceso. Hay que emitir uno nuevo.\n"
    assert archivo.exists()


def test_cli_rechaza_una_base_invalida(tmp_path, capsys):
    f = Falso()
    with pytest.raises(SystemExit) as e:
        dc.main(["guardar", "--sidecar", "demo_pg", "--archivo", str(tmp_path / "v.sql"), "--base", "x; ls"], correr=f)
    assert e.value.code == 2
    assert "nombre de base invalido" in capsys.readouterr().err
    assert f.llamadas == []


def test_cli_pasa_la_base_al_comando(tmp_path, capsys):
    f = Falso()
    _cli(capsys, ["guardar", "--sidecar", "demo_pg", "--archivo", str(tmp_path / "v.sql"), "--base", "otra"], f)
    assert all("-d otra" in s for s in scripts(f))


def test_el_entry_point_esta_declarado():
    import tomllib

    raiz = Path(__file__).resolve().parents[2]
    scripts_ = tomllib.loads((raiz / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts_["libracore-demo-codigos"] == "libracore.provisioning.demo_codigos:main"
