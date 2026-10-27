from __future__ import annotations

import argparse
import sys

from .config import build_config
from .models import ConfigError, PipelineError
from .pipeline import run_pipeline


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="论文到科研任务生成", allow_abbrev=False)
    parser.add_argument("--sci-env", required=True, help="共享环境名：使用 image/<sci-env>/ 的环境文件与 image-env.toml，读取 <input>/<sci-env>/，输出到 <output>/<sci-env>/")
    parser.add_argument("--input", required=True, help="输入根目录；实际读取 <input>/<sci-env>/<来源>/")
    parser.add_argument("--output", required=True, help="输出根目录；实际写入 <output>/<sci-env>/")
    parser.add_argument("--resume-from", help="复用旧运行目录的计划和草稿；不改旧目录")
    parser.add_argument("--local-only", action="store_true", help="仅恢复与本地检查，不调用模型、不连接集群、不发布")
    parser.add_argument("--author-env", required=True, help="作者、审查和造任务模型配置")
    parser.add_argument("--model-relay", help="通用反代连接文件；启动前检查原开发机服务")
    parser.add_argument("--run-txt", "--run_txt", dest="run_txt", help="每行一个来源名（<input>/<sci-env>/ 下的目录名），只运行名单中尚未发布的任务")
    parser.add_argument("--environment-root", help="共享环境根目录，默认项目内 image/")
    parser.add_argument("--image-env", help="镜像和 H 集群运行参数配置；默认 image/<sci-env>/image-env.toml")
    parser.add_argument("--experiment-image", help="覆盖 image-env 中的实验镜像")
    parser.add_argument("--evaluation-image", help="覆盖 image-env 中的评测镜像")
    parser.add_argument("--charged-group", help="覆盖 image-env 中的计费资源组")
    parser.add_argument("--mount", help="覆盖 image-env 中的 GPFS 挂载参数")
    parser.add_argument("--namespace", help="覆盖 image-env 中的 H 集群命名空间")
    parser.add_argument("--cpu", type=int, help="覆盖 image-env 中单个 RJob 的 CPU 上限")
    parser.add_argument("--memory-mb", type=int, help="覆盖 image-env 中单个 RJob 的内存上限（MiB）")
    parser.add_argument("--agent-gpus", type=int, help="覆盖 image-env 中 Agent RJob 的 GPU 上限")
    parser.add_argument("--verifier-gpus", type=int, help="覆盖 image-env 中 verifier RJob 的 GPU 上限")
    parser.add_argument("--priority", type=int, help="覆盖 image-env 中的优先级")
    parser.add_argument("--max-revisions", type=int, default=3)
    parser.add_argument("--concurrency", type=_positive_int, default=1, help="同时处理的输入来源数量")
    parser.add_argument("--proxy-bind", default="0.0.0.0", help="本机反代监听地址")
    parser.add_argument("--proxy-port", type=int, default=0, help="本机反代端口，0 表示自动选择")
    parser.add_argument("--proxy-host", help="RJob Worker 可访问的本机地址；默认自动探测")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--agent-timeout-sec", type=_positive_int, help="覆盖 image-env 中的 Agent 超时时间（秒）")
    parser.add_argument("--harbor-timeout-sec", type=_positive_int, help="覆盖 image-env 中的 Harbor 超时时间（秒）")
    parser.add_argument("--task-timeout-sec", type=_positive_int, default=19800, help="每道造任务总时限，默认 19800 秒（5.5 小时）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    values = vars(args)
    values["input_root"] = values.pop("input")
    values["output_root"] = values.pop("output")
    try:
        config = build_config(**values)
        outputs = run_pipeline(config)
    except (ConfigError, PipelineError, OSError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
