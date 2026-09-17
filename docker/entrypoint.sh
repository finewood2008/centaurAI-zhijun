#!/usr/bin/env sh
set -eu

data_root="${CENTAURAI_DATABASE_DATA_ROOT:-/var/lib/mindos}"

if ! mkdir -p "$data_root"; then
  echo "MindOS data root cannot be created: $data_root" >&2
  exit 1
fi

probe_file="$(mktemp "$data_root/.mindos-write-test.XXXXXX")" || {
  echo "MindOS data root is not writable by the container user: $data_root" >&2
  exit 1
}
rm -f "$probe_file"

export CENTAURAI_DATABASE_DATA_ROOT="$data_root"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"
exec "$@"
