"""Toda función del motor que escribe `clients` le avisa a `libracore.auditoria` (ADR-043).

Es el test que sostiene la premisa de la auditoría: **un log incompleto se ve igual que uno
completo**. Sembrar `registrar(...)` a mano funciona el primer día y se pudre cuando alguien
agrega una escritura nueva y se olvida; acá esa escritura no pasa el CI.

Recorre el AST de todo `libracore/` (menos las migraciones, que son de schema y de un momento):
una función cuyo SQL —un literal o un f-string— tiene `INSERT INTO clients`, `UPDATE clients` o
`DELETE FROM clients` tiene que llamar a `registrar` (o a `_registrar_cambio`, el ayudante de
`db/clients.py` que lo llama). Si el motor llegara a escribir `clients` desde otro módulo, entra
en la regla sin tocar este archivo.

El escáner se prueba a sí mismo contra fuentes de juguete: sin eso, un escáner que no encuentra
nada pasaría el test para siempre.
"""
import ast
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "libracore"

# `\\s+` y no un espacio: el SQL de varias líneas parte las sentencias.
ESCRITURA = re.compile(r"(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+clients\b", re.IGNORECASE)

#: Las llamadas que cuentan como «emite el cambio».
EMISORES = {"registrar", "_registrar_cambio"}


def _textos(funcion: ast.AST):
    """Los literales de texto de una función, f-strings incluidos (sus partes constantes)."""
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            yield nodo.value


def _emite(funcion: ast.AST) -> bool:
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Call):
            f = nodo.func
            nombre = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if nombre in EMISORES:
                return True
    return False


def funciones_que_escriben_clients_sin_emitir(fuente: str) -> list[str]:
    """Los nombres de las funciones de `fuente` que escriben `clients` y no emiten el cambio."""
    faltan = []
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            escribe = any(ESCRITURA.search(t) for t in _textos(nodo))
            if escribe and not _emite(nodo):
                faltan.append(nodo.name)
    return faltan


def _modulos_del_motor():
    for ruta in sorted(RAIZ.rglob("*.py")):
        if "migrations" in ruta.parts or "__pycache__" in ruta.parts:
            continue
        yield ruta


def test_toda_funcion_del_motor_que_escribe_clients_emite_el_cambio():
    sin_emitir = {}
    for ruta in _modulos_del_motor():
        faltan = funciones_que_escriben_clients_sin_emitir(ruta.read_text(encoding="utf-8"))
        if faltan:
            sin_emitir[str(ruta.relative_to(RAIZ.parent))] = faltan
    assert sin_emitir == {}, (
        "Estas funciones escriben `clients` y no llaman a `libracore.auditoria.registrar`: "
        f"{sin_emitir}. Una escritura sin auditar deja un log que parece completo y no lo es."
    )


def test_el_escaner_encuentra_las_escrituras_de_clients_del_motor():
    """Contraprueba: el módulo donde viven las escrituras SÍ tiene funciones que el escáner ve
    como escritoras. Sin esto, una regex rota daría «cero funciones sin emitir» para siempre."""
    fuente = (RAIZ / "db" / "clients.py").read_text(encoding="utf-8")
    escritoras = [
        n.name for n in ast.walk(ast.parse(fuente))
        if isinstance(n, ast.FunctionDef) and any(ESCRITURA.search(t) for t in _textos(n))
    ]
    assert set(escritoras) >= {
        "create_client", "crear_cliente", "update_client", "actualizar_cliente", "desactivar_cliente",
        "activar_cliente", "toggle_auto_facturar", "resolver_cliente_externo", "delete_client",
    }


@pytest.mark.parametrize("sql", [
    'conn.execute("INSERT INTO clients (name) VALUES (?)", (n,))',
    'conn.execute("INSERT OR IGNORE INTO clients (name) VALUES (?)", (n,))',
    'conn.execute(f"UPDATE clients SET {sets} WHERE id=?", v)',
    'conn.execute("""\n  DELETE FROM\n   clients WHERE id=?""", (i,))',
    'conn.execute("update clients set name = ?", (n,))',
])
def test_el_escaner_marca_una_escritura_sin_emitir(sql):
    fuente = f"def escribe(conn, n):\n    {sql.replace(chr(10), chr(10) + '    ')}\n"
    assert funciones_que_escriben_clients_sin_emitir(fuente) == ["escribe"]


@pytest.mark.parametrize("emisor", ["registrar(conn, 'clients', 'crear', 1)", "auditoria.registrar(conn, 'clients', 'crear', 1)",
                                    "_registrar_cambio(conn, 'crear', 1)"])
def test_el_escaner_acepta_la_que_emite(emisor):
    fuente = f'def escribe(conn):\n    conn.execute("DELETE FROM clients WHERE id=1")\n    {emisor}\n'
    assert funciones_que_escriben_clients_sin_emitir(fuente) == []


def test_el_escaner_ignora_lo_que_no_es_clients():
    fuente = (
        "def espejo(conn):\n"
        '    conn.execute("INSERT INTO parties (id) VALUES (1)")\n'
        '    conn.execute("UPDATE client_notes SET x = 1")\n'
        '    conn.execute("SELECT * FROM clients")\n'
    )
    assert funciones_que_escriben_clients_sin_emitir(fuente) == []
