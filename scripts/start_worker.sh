#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec "${PYTHON_BIN:-python3}" - "$@" <<'PY'
import re
import shlex
import subprocess
import sys

from task_builder.config import load_image_env
from task_builder.models import ConfigError
from task_builder.worker import build_parser


def main():
    args = build_parser().parse_args()
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.sci_env):
            raise ConfigError("--sci-env 必须是环境目录名，不能包含路径")
        if not args.image.strip() or any(c in args.image for c in "<>\r\n"):
            raise ConfigError("请填写实际可访问的 --image 镜像地址")
        image_env = args.image_env or args.environment_root / args.sci_env / "image-env.toml"
        config = load_image_env(image_env)
        source, separator, target = config.mount.rpartition(":")
        if not separator or not source.startswith("gpfs://") or not target.startswith("/"):
            raise ConfigError("mount 应为 gpfs://gpfs1/<存储名称>:<worker 上的绝对路径>")

        # rlaunch 默认沿用本机当前目录；显式使用 /，避免要求 worker 存在本机项目路径。
        command = [
            "rlaunch", f"--namespace={config.namespace}", f"--charged-group={config.charged_group}",
            "--private-machine=group", f"--image={args.image}", f"--mount={config.mount}",
            f"--cpu={args.cpu}", f"--memory={args.memory_mb}", f"--requests.memory={args.memory_mb}",
            f"--gpu={args.gpus}", "--workdir=/", "--enable-sshd=true", "--max-wait-duration=15m",
            "--i-know-i-am-wasting-resource=true", "--comment=task-builder", "--detach",
            "--", "bash", "-c", 'export PATH="/kubebrain:$PATH"; exec sleep infinity',
        ]
        if args.dry_run:
            print(shlex.join(command))
            return 0
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1000)
        print(result.stdout, end="", flush=True)
        if result.returncode:
            return result.returncode
        workers = re.findall(r"\bws-[A-Za-z0-9-]+-worker-[A-Za-z0-9-]+\b", result.stdout)
        name = workers[-1] if workers else "<启动结果中的worker名称>"
        print("进入 worker：\n" + shlex.join([
            "brainctl", "exec", f"--namespace={config.namespace}", "-it", f"process/{name}", "--", "bash",
        ]))
        return 0
    except subprocess.TimeoutExpired:
        print("启动等待超时，请在集群中检查本次 task-builder worker 是否已启动。", file=sys.stderr)
    except (ConfigError, OSError, ValueError) as error:
        print(f"启动失败: {error}", file=sys.stderr)
    return 1


sys.exit(main())
PY
