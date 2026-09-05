#!/bin/bash
# 本机开发的统一入口：bash zhijun.sh start|status|stop
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/backend/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then PYTHON=python3; fi
if [ "$#" -eq 0 ]; then set -- start; fi
exec "$PYTHON" "$SCRIPT_DIR/scripts/dev_runtime.py" "$@"
