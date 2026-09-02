"""Un pin con extras sigue siendo un pin.

🔴 **Cuatro de los ocho productos del VPS quedaban sin pin detectado**, y por
eso `check_venv_sync` no avisaba nada para ellos: Contalibra, LibraCargo,
Restolibra y VentaLibra declaran `libracore[migrations]` porque su deploy corre
`libracore-migrar`, y el regex esperaba `@` justo después de `libracore`.

Medido el 2026-09-02 sobre las ocho instalaciones reales: los cuatro que
declaran el extra son exactamente los cuatro con `_pin_declarado() is None`. El
aviso estuvo mudo durante el deploy de ese día, con el venv **cinco versiones**
atrás.

Es la tercera vez que este módulo se calla por leer la forma equivocada de una
dependencia. Las dos anteriores están narradas en el docstring de
`_pin_declarado`, y las dos terminaron igual: la guarda no fallaba, no decía
nada, y nada se lee igual que todo bien.
"""
import pytest

from libracore import provisioning

#: Recorte fiel del `pyproject.toml` real de LibraCargo, con el extra y con el
#: comentario que nombra al motor justo arriba --- que es lo que hace que no
#: alcance con buscar la palabra "libracore" en el archivo.
CON_EXTRAS = '''\
[project]
name = "libracargo"
dependencies = [
    # El extra `[migrations]` es el que trae alembic, que LibraCore no declara
    # como dependencia propia. Sin el, `libracore-migrar` muere en el deploy.
    "libracore[migrations] @ git+https://github.com/marianocappucci/libracore.git@v1.74.0",
    "libraauth @ git+https://github.com/marianocappucci/libraauth.git@v0.35.0",
    "fastapi",
]
'''


def test_el_pin_se_lee_aunque_declare_extras(tmp_path):
    (tmp_path / "pyproject.toml").write_text(CON_EXTRAS, encoding="utf-8")
    assert provisioning._pin_declarado(tmp_path) == ("1.74.0", "pyproject.toml")


def test_check_venv_sync_avisa_con_un_pin_con_extras(tmp_path, monkeypatch):
    """El caso exacto que estuvo mudo en el VPS."""
    import libracore
    monkeypatch.setattr(libracore, "__version__", "1.69.0")
    (tmp_path / "pyproject.toml").write_text(CON_EXTRAS, encoding="utf-8")

    aviso = provisioning.check_venv_sync(tmp_path)
    assert aviso is not None, "con extras la guarda vuelve a callarse"
    assert "1.69.0" in aviso and "1.74.0" in aviso


def test_check_venv_sync_no_avisa_con_extras_al_dia(tmp_path, monkeypatch):
    """El control. Sin esto, una guarda que avisara SIEMPRE pasaria el de arriba."""
    import libracore
    monkeypatch.setattr(libracore, "__version__", "1.74.0")
    (tmp_path / "pyproject.toml").write_text(CON_EXTRAS, encoding="utf-8")
    assert provisioning.check_venv_sync(tmp_path) is None


@pytest.mark.parametrize("paquete", ["libracore", "libraauth"])
def test_depende_de_ve_la_dependencia_con_extras(tmp_path, paquete):
    """El otro lector del módulo tenía la misma ceguera.

    Hoy no le pega a nadie —sólo `libracore` usa extras en el parque, y a
    `_depende_de` se lo llama con `libracommerce`, `libragenda` y `libraauth`—
    pero se arregla igual: dejar uno de los dos leyendo la forma vieja es cómo
    la corrección anterior **no se propagó** hasta acá, según su propio
    docstring.
    """
    (tmp_path / "pyproject.toml").write_text(
        f'dependencies = ["{paquete}[algo] @ git+https://x/y.git@v1.0.0"]\n',
        encoding="utf-8")
    assert provisioning._depende_de(tmp_path, paquete) is True


def test_un_paquete_VECINO_no_se_lee_como_el_pin_de_libracore(tmp_path):
    """🔑 Lo encontró una mutación que sobrevivió a la primera batería.

    Aflojar el patrón hasta `libracore\\S*` —"cualquier cosa después del
    nombre"— pasaba todos los tests de arriba, y ademas leería
    `libracore-utils` como si fuera `libracore`. Un pin equivocado es peor que
    ninguno: la guarda **avisaría**, pero comparando contra la versión de otro
    paquete.

    Lo que se admite es un corchete de extras, no un sufijo cualquiera.
    """
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = [\n'
        '    "libracore-utils @ git+https://github.com/x/libracore-utils.git@v9.9.9",\n'
        ']\n', encoding="utf-8")
    assert provisioning._pin_declarado(tmp_path) is None
    assert provisioning._depende_de(tmp_path, "libracore") is False


def test_un_paquete_que_solo_se_NOMBRA_no_cuenta_aunque_lleve_corchetes(tmp_path):
    """El extra no puede aflojar lo que ya se exigía: hace falta el `@ git+`."""
    (tmp_path / "pyproject.toml").write_text(
        "# libracore[migrations] provee la facturacion\n"
        'dependencies = ["fastapi"]\n', encoding="utf-8")
    assert provisioning._pin_declarado(tmp_path) is None
    assert provisioning._depende_de(tmp_path, "libracore") is False
