#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || { echo "请先运行 bash $ROOT/setup_env.sh" >&2; exit 1; }
cd "$ROOT"
exec "$PYTHON" -m task_builder.sandbox_dispatch "$@"
