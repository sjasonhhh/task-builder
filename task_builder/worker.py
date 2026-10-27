"""Launch the authoring worker using the same cluster config as task execution."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shlex
import subprocess
import sys

from .config import load_image_env
from .models import ConfigError
from .paths import PROJECT_ROOT, SCIENV_IMAGE_ROOT


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动任务生成 worker", allow_abbrev=False)
    parser.add_argument("--sci-env", required=True)
    parser.add_argument("--environment-root", type=Path, default=SCIENV_IMAGE_ROOT)
    parser.add_argument("--image-env", type=Path)
    parser.add_argument("--image", required=True, help="含科研软件、Claude Code 和 rjob 的镜像")
    parser.add_argument("--cpu", type=_positive_int, default=16)
    parser.add_argument("--memory-mb", type=_positive_int, default=65536)
    parser.add_argument("--gpus", type=_positive_int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="显示启动命令，不申请集群资源")
    return parser


def launch_command(args: argparse.Namespace, project_root: Path = PROJECT_ROOT) -> tuple[list[str], str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.sci_env):
        raise ConfigError("--sci-env 必须是环境目录名，不能包含路径")
    if not args.image.strip() or any(c in args.image for c in "<>\r\n"):
        raise ConfigError("请填写实际可访问的 --image 镜像地址")
    image_env = args.image_env or args.environment_root / args.sci_env / "image-env.toml"
    config = load_image_env(image_env)
    source, separator, target = config.mount.rpartition(":")
    if not separator or not source.startswith("gpfs://") or not target.startswith("/"):
        raise ConfigError("mount 应为 gpfs://gpfs1/<存储名称>:<开发机共享目录绝对路径>")
    root = project_root.resolve()
    if not root.is_relative_to(Path(target).resolve()):
        raise ConfigError(f"mount 的目标目录 {target} 未包含项目 {root}；请保持开发机和 worker 的项目路径一致")
    command = [
        "rlaunch", f"--namespace={config.namespace}", f"--charged-group={config.charged_group}",
        "--private-machine=group", f"--image={args.image}", f"--mount={config.mount}",
        f"--cpu={args.cpu}", f"--memory={args.memory_mb}", f"--requests.memory={args.memory_mb}",
        f"--gpu={args.gpus}", f"--workdir={root}", "--enable-sshd=true", "--max-wait-duration=15m",
        "--i-know-i-am-wasting-resource=true", "--comment=task-builder", "--detach",
        "--", "bash", "-c", 'export PATH="/kubebrain:$PATH"; exec sleep infinity',
    ]
    return command, config.namespace


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        command, namespace = launch_command(args)
        if args.dry_run:
            print(shlex.join(command))
            return 0
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1000)
        print(result.stdout, end="", flush=True)
        if result.returncode:
            return result.returncode
        workers = re.findall(r"\bws-[A-Za-z0-9-]+-worker-[A-Za-z0-9-]+\b", result.stdout)
        name = workers[-1] if workers else "<启动结果中的worker名称>"
        print("进入 worker：\n" + shlex.join(["brainctl", "exec", f"--namespace={namespace}", "-it", f"process/{name}", "--", "bash"]))
        return 0
    except subprocess.TimeoutExpired:
        print("启动等待超时，请在集群中检查本次 task-builder worker 是否已启动。", file=sys.stderr)
    except (ConfigError, OSError, ValueError) as error:
        print(f"启动失败: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
