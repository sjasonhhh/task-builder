#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"

fail() { echo "setup_env.sh: $*" >&2; exit 1; }

ready() {
  [ -x "$1/bin/python" ] && [ -x "$1/bin/harbor" ] || return 1
  "$1/bin/python" - "$ROOT/requirements.txt" <<'PY'
import importlib.metadata
import importlib.util
from pathlib import Path
import sys
try:
    assert sys.version_info >= (3, 12)
    for line in Path(sys.argv[1]).read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            name, version = line.split('==')
            assert importlib.metadata.version(name) == version
    package = Path(importlib.util.find_spec('harbor').origin).parent
    assert '_recover_unhandled_trial_result' in (package / 'job.py').read_text()
except (AssertionError, ImportError, AttributeError, ValueError, OSError):
    sys.exit(1)
PY
}

if ready "$VENV"; then
  echo "运行环境就绪: $VENV"
  exit 0
fi

# 外部环境只检查和复用，不在其中安装包或修改补丁。
if [ -L "$VENV" ]; then
  fail ".venv 指向的外部环境不可用或缺少固定依赖/补丁；请选择已准备好的 worker 镜像，或在新项目目录中建立独立环境"
fi
if [ ! -e "$VENV" ] && ready /opt/orch/venv; then
  ln -s /opt/orch/venv "$VENV"
  echo "使用镜像预装环境: $VENV"
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-python3.12}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  [ "$PYTHON_BIN" = python3.12 ] || fail "找不到 $PYTHON_BIN"
  PYTHON_BIN=python3
fi
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || fail "Harbor 0.22.0 需要 Python >= 3.12；可通过 PYTHON_BIN 指定解释器"
command -v patch >/dev/null 2>&1 || fail "请先安装 patch 命令"
if [ ! -d "$VENV" ]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -r "$ROOT/requirements.txt"
SITE_PACKAGES="$("$VENV/bin/python" -c 'import pathlib, harbor; print(pathlib.Path(harbor.__file__).resolve().parent.parent)')"
case "$SITE_PACKAGES/" in
  "$VENV/"*) ;;
  *) fail "Harbor 位于项目环境之外，拒绝修改: $SITE_PACKAGES" ;;
esac
if ! grep -q '_recover_unhandled_trial_result' "$SITE_PACKAGES/harbor/job.py"; then
  (cd "$SITE_PACKAGES" && patch -p1 --batch < "$ROOT/patches/harbor-0.22.0-job-isolation.patch")
fi
ready "$VENV" || fail "环境未通过版本与补丁检查"
echo "运行环境就绪: $VENV"
