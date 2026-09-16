"""Dobles compartidos de los tests del deploy.

`cmd_actualizar` lee las migraciones **del árbol que construye** (2026-09-16):
`build_image_tagged` le pasa ese árbol por `al_materializar`. Un doble del build
que no lo llame deja al deploy sin migraciones leídas, y el deploy falla cerrado.
Este doble hace lo que hace el real: llama al callback con el árbol —en los
tests, el `repo_root` del producto— antes de "construir".
"""
from libracore import provisioning


def escribir_panel(repo_root, migraciones=()):
    """Un `scripts/panel_admin.py` mínimo que declara esas migraciones."""
    scripts = repo_root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "panel_admin.py").write_text(
        "from libracore.provisioning import configure\n"
        f"configure(product_name='TESTPROD', migraciones={tuple(migraciones)!r})\n",
        encoding="utf-8",
    )


def build_falso(ok=True, *, antes=None):
    """`ok` puede ser un bool o un callable (para decidirlo al momento del
    build). `antes` se llama primero, para registrar el orden. Si el árbol no
    tiene script del panel, se escribe uno con las migraciones del checkout:
    así los tests que no miran esto siguen midiendo lo suyo."""
    def _build(version, *a, al_materializar=None, **k):
        if antes is not None:
            antes()
        if al_materializar is not None:
            cfg = provisioning.get_config()
            if not (cfg.repo_root / "scripts" / "panel_admin.py").exists():
                escribir_panel(cfg.repo_root, cfg.migraciones)
            al_materializar(cfg.repo_root, "abc1234")
        return ok() if callable(ok) else ok
    return _build
