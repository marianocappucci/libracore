#!/usr/bin/env bash
# Aplica las migraciones de Alembic de LibraCore contra la base de un
# consumidor (Contalibra, Restolibra, VentaLibra, Gestiolibra, MedLibra,
# LibraDesk), clonando el repo en el tag exacto que ese consumidor pinea.
#
# Espejo de scripts/run_migrations.sh de LibraGenda.
#
# NOTA (2026-08-25): las migraciones AHORA SI viajan en el wheel --se mudaron a
# libracore/migrations/-- asi que desde el lado de un consumidor instalado con
# pip el camino corto es:
#
#     libracore-migrar upgrade --prefijo <producto>
#
# Este script sigue siendo util para el caso en que hace falta aplicar un TAG
# EXACTO distinto del instalado, porque clona el repo en esa referencia. Para el
# deploy normal de una instancia, usar el console script.
#
# Uso:
#   LIBRACORE_REF=v1.19.0 DATABASE_URL=postgresql://user:pass@host/db \
#     ./scripts/run_migrations.sh
#
#   LIBRACORE_REF=v1.19.0 DATABASE_URL=/root/contalibra/clientes/demo/data/contalibra.db \
#     ./scripts/run_migrations.sh
#
# Variables de entorno:
#   LIBRACORE_REF   tag exacto de LibraCore a aplicar (obligatorio, ej. v1.19.0)
#   DATABASE_URL    URL PostgreSQL **o ruta del archivo SQLite** de la instancia
#   LIBRACORE_REPO  URL del repo a clonar (default: origin de GitHub)
#
# Sobre correrlo contra una instancia viva: la baseline llama a
# init_core_schema(), que es idempotente, asi que aplicarla sobre una base que
# ya tiene el schema hace lo mismo que un arranque de la app y ademas registra
# la version. Aun asi, **backup antes**: es una operacion de schema.

set -euo pipefail

: "${LIBRACORE_REF:?LIBRACORE_REF es obligatorio (ej. v1.19.0)}"
: "${DATABASE_URL:?DATABASE_URL es obligatorio (URL PostgreSQL o ruta del .db)}"
LIBRACORE_REPO="${LIBRACORE_REPO:-https://github.com/marianocappucci/libracore.git}"

workdir="$(mktemp -d)"
cleanup() { rm -rf "$workdir"; }
trap cleanup EXIT

echo "Clonando LibraCore @ ${LIBRACORE_REF}..." >&2
git clone --quiet --depth 1 --branch "$LIBRACORE_REF" "$LIBRACORE_REPO" "$workdir"

cd "$workdir"
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir --quiet --upgrade pip
# El extra "migrations" trae alembic y SQLAlchemy, que NO son dependencias de
# runtime de LibraCore: solo las necesita esta cadena.
.venv/bin/pip install --no-cache-dir --quiet -e ".[migrations]"

DATABASE_URL="$DATABASE_URL" .venv/bin/alembic -c alembic.ini upgrade head

echo "Migraciones de LibraCore ${LIBRACORE_REF} aplicadas contra la base indicada." >&2
