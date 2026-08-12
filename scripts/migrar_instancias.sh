#!/usr/bin/env bash
# Aplica las migraciones de LibraCore a TODAS las instancias de un producto,
# desde el host del VPS.
#
# `run_migrations.sh` migra UNA base y asume que el que lo llama sabe la URL.
# En el VPS ninguna de esas dos cosas se cumple sola:
#
#  1. Cada producto corre N instancias (`clientes/<slug>/`), no una.
#  2. La URL de cada instancia vive en el entorno de SU contenedor, con
#     nombres que además difieren entre productos (ver
#     `libracore/db/url_de_instancia.py`).
#  3. 🔴 **El host no puede resolver esa URL.** El destino es
#     `postgresql://...@<producto>-postgres:5432/...`, y ese nombre es un
#     alias de la red de Docker del sidecar: desde afuera de esa red no
#     existe. Correr `run_migrations.sh` derecho en el host falla con
#     "could not translate host name".
#
# Por eso la migración va en un contenedor efímero adosado a la MISMA red que
# la instancia. Las redes `<producto>-dev-datos` no son `internal`, así que
# desde ahí se llega al sidecar y también a GitHub/PyPI.
#
# Uso:
#   LIBRACORE_REF=v1.20.0 ./scripts/migrar_instancias.sh contalibra-dev
#   LIBRACORE_REF=v1.20.0 ./scripts/migrar_instancias.sh --si contalibra-dev
#
# Sin `--si` hace **dry-run**: dice qué instancias encontró y contra qué base
# iría, sin tocar nada. Es a propósito: la lista sale de inspeccionar
# contenedores, y una instancia de cliente metida ahí por error se migra igual
# que una de dev.
#
# Variables:
#   LIBRACORE_REF   tag o rama de LibraCore a aplicar (obligatorio)
#   LIBRACORE_REPO  repo a clonar (default: el de GitHub)
#   IMAGEN_PYTHON   imagen para el contenedor efímero (default: python:3.12)
#
# Las URLs se imprimen SIEMPRE enmascaradas: la de PostgreSQL lleva la
# contraseña del sidecar adentro.

set -euo pipefail

: "${LIBRACORE_REF:?LIBRACORE_REF es obligatorio (ej. v1.20.0)}"
LIBRACORE_REPO="${LIBRACORE_REPO:-https://github.com/marianocappucci/libracore.git}"
IMAGEN_PYTHON="${IMAGEN_PYTHON:-python:3.12}"

EJECUTAR=0
if [ "${1:-}" = "--si" ]; then
  EJECUTAR=1
  shift
fi

if [ "$#" -eq 0 ]; then
  echo "Uso: [--si] <contenedor> [<contenedor>...]" >&2
  exit 2
fi

enmascarar() { printf '%s' "$1" | sed -E 's#//[^@/]*@#//***:***@#'; }

url_de() {
  # 🔴 **La base de LIBRACORE, que no siempre es la del producto.**
  #
  # Gestiolibra y MedLibra corren DOS bases: la del dominio, que es de
  # LibraGenda, y una propia de LibraCore (`<producto>_core`). La versión
  # anterior de esta función tomaba la primera variable en orden alfabético, y
  # `DATABASE_URL` viene antes que `GESTIOLIBRA_LIBRACORE_DB_PATH`: elegía la
  # base equivocada.
  #
  # Aplicar la cadena de LibraCore ahí **no habría fallado, habría "andado"**:
  # los `CREATE TABLE IF NOT EXISTS` se saltean la tabla `clients` que ya
  # existe, y después los ALTER defensivos le agregan las columnas de LibraCore
  # **a la tabla de LibraGenda** — la que tiene los clientes reales y los
  # turnos colgando. Lo agarró el dry-run el 2026-08-12, antes de aplicar.
  #
  # El orden es el mismo que usa `libracore.db.url_de_instancia` con
  # `core=True`: primero las variables específicas del motor y, sólo si no hay
  # ninguna, la del producto — que es el caso de Contalibra, Restolibra y
  # VentaLibra, donde el motor comparte la base del dominio.
  docker exec "$1" sh -c '
    for k in $(env | sed -n "s/^\([A-Z_]*LIBRACORE_\(DATABASE_URL\|DB_PATH\)\)=.*/\1/p" | sort -u); do
      printenv "$k"; exit 0
    done
    for k in $(env | sed -n "s/^\([A-Z_]*\(DATABASE_URL\|DB_PATH\)\)=.*/\1/p" | sort -u); do
      printenv "$k"; exit 0
    done
  ' 2>/dev/null | head -1
}

red_de() {
  # La primera red que NO sea la compartida del stack: la del sidecar de datos.
  docker inspect "$1" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}
{{end}}' | grep -v '^stack_' | grep -v '^$' | head -1
}

echo "LibraCore ref: ${LIBRACORE_REF}"
[ "$EJECUTAR" -eq 1 ] || echo "MODO DRY-RUN — nada se va a modificar (pasá --si para aplicar)"
echo

fallas=0
for contenedor in "$@"; do
  destino="$(url_de "$contenedor" || true)"
  red="$(red_de "$contenedor" || true)"

  if [ -z "$destino" ] || [ -z "$red" ]; then
    echo "✗ ${contenedor}: no pude resolver $([ -z "$destino" ] && echo 'la URL de la base' || echo 'la red de datos')" >&2
    fallas=$((fallas + 1))
    continue
  fi

  echo "→ ${contenedor}"
  echo "    base: $(enmascarar "$destino")"
  echo "    red:  ${red}"

  if [ "$EJECUTAR" -eq 0 ]; then
    continue
  fi

  if docker run --rm --network "$red" \
      -e "LIBRACORE_REF=${LIBRACORE_REF}" \
      -e "LIBRACORE_REPO=${LIBRACORE_REPO}" \
      -e "DATABASE_URL=${destino}" \
      "$IMAGEN_PYTHON" \
      sh -c 'set -e
        git clone --quiet --depth 1 --branch "$LIBRACORE_REF" "$LIBRACORE_REPO" /src
        cd /src
        pip install --no-cache-dir --quiet -e ".[migrations]"
        alembic -c alembic.ini upgrade head'; then
    echo "    ✓ migrada"
  else
    echo "    ✗ FALLÓ" >&2
    fallas=$((fallas + 1))
  fi
  echo
done

if [ "$fallas" -gt 0 ]; then
  echo "Terminó con ${fallas} instancia(s) con problemas." >&2
  exit 1
fi

[ "$EJECUTAR" -eq 1 ] && echo "Todas las instancias quedaron en head." || true
