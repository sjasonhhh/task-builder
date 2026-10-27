#!/usr/bin/env bash
set -Eeuo pipefail

# task-builder 启动脚本：只使用项目自己的虚拟环境。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "run.sh: 找不到项目虚拟环境 Python: $PYTHON" >&2
    echo "run.sh: 请先运行 bash $ROOT/setup_env.sh 创建项目环境" >&2
    exit 1
fi

# VS Code 等非登录终端不会加载 /etc/profile.d/ssh-init.sh，缺少集群变量时 rjob 报
# "brainpp only work in kubebrain environment"；这里只从 PID 1 补 KUBEBRAIN_*/BRAIN_*。
if [ -z "${KUBEBRAIN_CLUSTER_ENTRY:-}" ] && [ -r /proc/1/environ ]; then
    while IFS= read -r -d '' entry; do
        case "$entry" in
            KUBEBRAIN_*=*|BRAIN_*=*) export "$entry" ;;
        esac
    done < /proc/1/environ
fi

cd "$ROOT"
exec "$PYTHON" -m task_builder.cli "$@"
