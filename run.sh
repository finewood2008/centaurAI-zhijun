#!/usr/bin/env bash
set -euo pipefail
umask 027

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python)}"
DATA_ROOT="${CENTAURAI_DATABASE_DATA_ROOT:-/var/lib/centauros/centaurai-database}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "CentaurAI Database runtime is incomplete: missing executable $PYTHON_BIN" >&2
  exit 1
fi

if ! mkdir -p "$DATA_ROOT"; then
  echo "CentaurAI Database data root cannot be created: $DATA_ROOT" >&2
  exit 1
fi
DATA_ROOT="$(cd "$DATA_ROOT" && pwd -P)"

write_probe="$(mktemp "$DATA_ROOT/.centaurai-database-write-test.XXXXXX")" || {
  echo "CentaurAI Database data root is not writable: $DATA_ROOT" >&2
  exit 1
}
rm -f "$write_probe"

export CENTAURAI_DATABASE_DATA_ROOT="$DATA_ROOT"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
cd "$PROJECT_ROOT/backend"
exec "$PYTHON_BIN" server.py
