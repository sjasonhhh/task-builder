from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .cli import _positive_int
from .config import load_agent_env, load_image_env, resolve_environment
from .discovery import read_run_names
from .harbor_runtime import run_harbor_agent
from .hcluster_runtime import cleanup_logged_rjobs, resolve_claude_source
from .integrity import content_manifest, manifest_differences
from .models import ConfigError, PipelineConfig, PipelineError, StageFailure
from . import paths
from .paths import PROJECT_HARBOR
from .staging import finalize_harbor_task, validate_task_config


TASK_PARTS = ("task.toml", "instruction.md", "data", "solution", "tests")
# 答题模型看不到参考提交，评分只使用 tests；也支持不含参考提交的外部任务。
OPTIONAL_PARTS = ("solution",)


@dataclass(frozen=True)
class SelectedTask:
    name: str
    path: Path
    manifest: dict
    identity: str


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在 H 集群运行 final 中的成品任务，并独立评分", allow_abbrev=False)
    parser.add_argument("--sci-env", required=True, help="共享环境名：镜像配置默认取 image/<sci-env>/image-env.toml，--input 须为某个 <sci-env>/final")
    parser.add_argument("--run_txt", required=True, help="任务名单，每行一个 final 下的目录名")
    parser.add_argument("--input", help="成品任务目录，默认 output/<sci-env>/final")
    parser.add_argument("--output", required=True, help="运行记录、生成文件与评分结果的保存目录")
    parser.add_argument("--env", required=True, help="答题模型配置文件")
    parser.add_argument("--model-relay", help="已有模型反代连接文件；不传则直连 API")
    parser.add_argument("--environment-root", help="共享环境根目录，默认项目内 image/")
    parser.add_argument("--image-env", help="共享镜像与集群配置文件，默认 image/<sci-env>/image-env.toml")
    parser.add_argument("--concurrency", type=_positive_int, default=1, help="同时处理的任务数，默认 1")
    parser.add_argument("--force", action="store_true", help="重新运行已评分的任务，保留旧结果")
    parser.add_argument("--agent-timeout-sec", type=_positive_int, default=14400, help="答题时限，默认 14400 秒（4 小时），须小于 Harbor 总时限")
    parser.add_argument("--harbor-timeout-sec", type=_positive_int, help="包含启动、答题、评分与关闭的总时限")
    parser.add_argument("--experiment-image", help="覆盖实验镜像")
    parser.add_argument("--evaluation-image", help="覆盖评测镜像")
    parser.add_argument("--charged-group", help="覆盖配额组")
    parser.add_argument("--namespace", help="覆盖集群命名空间")
    parser.add_argument("--mount", help="覆盖共享存储挂载设置")
    parser.add_argument("--cpu", type=_positive_int, help="覆盖单个 RJob 的 CPU 上限")
    parser.add_argument("--memory-mb", type=_positive_int, help="覆盖单个 RJob 的内存上限（MiB）")
    parser.add_argument("--agent-gpus", type=_nonnegative_int, help="覆盖答题 RJob 的 GPU 上限")
    parser.add_argument("--verifier-gpus", type=_nonnegative_int, help="覆盖评分 RJob 的 GPU 上限")
    parser.add_argument("--priority", type=_positive_int, help="覆盖集群优先级")
    parser.add_argument("--proxy-bind", default="0.0.0.0", help="本机模型转发服务监听地址")
    parser.add_argument("--proxy-port", type=_nonnegative_int, default=0, help="转发端口，默认自动选择")
    parser.add_argument("--proxy-host", help="集群可访问的本机地址")
    parser.add_argument("--claude-bin", default="claude", help="本机 Claude Code 程序名或路径")
    return parser


def runtime_config(args: argparse.Namespace) -> PipelineConfig:
    environment_dir, image_env_path = resolve_environment(args.sci_env, args.image_env, Path(args.environment_root) if args.environment_root else paths.SCIENV_IMAGE_ROOT)
    input_root = Path(args.input).expanduser().resolve() if args.input else paths.PROJECT_OUTPUT / args.sci_env / "final"
    if input_root.parent.name != args.sci_env:
        raise ConfigError(f"--input 必须是 --sci-env {args.sci_env} 对应的成品目录（…/{args.sci_env}/final），当前为 {input_root}")
    images = load_image_env(image_env_path)
    agent = load_agent_env(Path(args.env).expanduser().resolve(), role="答题模型")
    values = vars(images).copy()
    for name in values:
        override = getattr(args, name, None)
        if override is not None:
            values[name] = override
    if not 1 <= values["priority"] <= 9:
        raise ConfigError("priority 必须在 1 到 9 之间")
    if args.proxy_port > 65535 or (args.proxy_port and args.concurrency > 1):
        raise ConfigError("转发端口必须不超过 65535；并行运行时请用 --proxy-port 0")
    return PipelineConfig(
        input_root=input_root,
        output_root=Path(args.output).expanduser().resolve(),
        author_env=agent,
        image_env_path=image_env_path,
        sci_env=args.sci_env,
        environment_dir=environment_dir,
        concurrency=args.concurrency,
        proxy_bind=args.proxy_bind,
        proxy_port=args.proxy_port,
        proxy_host=args.proxy_host,
        claude_bin=args.claude_bin,
        model_relay=Path(args.model_relay).expanduser().absolute() if args.model_relay else None,
        extra={"managed_harbor": True, "cancel_event": threading.Event()},
        **values,
    )


def _task_manifest(path: Path) -> dict:
    manifest = {}
    for name in TASK_PARTS:
        part = path / name
        if name in OPTIONAL_PARTS and not part.exists() and not part.is_symlink():
            continue
        if not part.exists():
            raise StageFailure(f"任务 {path.name} 缺少 {name}", stage="input")
        manifest[name] = content_manifest(part)
        if part.is_symlink() or any(entry["kind"] == "symlink" for entry in manifest[name].values()):
            raise StageFailure(f"任务 {path.name} 的 {name} 含文件链接，不能保证任务独立", stage="input")
    return manifest


def _identity(manifest: dict, config: PipelineConfig) -> str:
    settings = {
        name: getattr(config, name) for name in (
            "experiment_image", "evaluation_image", "agent_timeout_sec", "harbor_timeout_sec",
            "cpu", "memory_mb", "agent_gpus", "verifier_gpus", "charged_group", "namespace", "mount",
        )
    }
    settings["model"] = config.author_env.model_id
    settings["model_settings"] = {
        name: value for name, value in config.author_env.values.items()
        if not any(word in name.upper() for word in ("TOKEN", "KEY", "SECRET", "PASSWORD"))
    }
    payload = {"version": 2, "task": manifest, "settings": settings}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def select_tasks(names_path: Path, config: PipelineConfig) -> list[SelectedTask]:
    if not config.input_root.is_dir():
        raise ConfigError(f"成品任务目录不存在: {config.input_root}")
    directories = {path.name: path for path in config.input_root.iterdir() if path.is_dir() and not path.name.startswith(".")}
    tasks = []
    for name in read_run_names(names_path):
        path = directories.get(name)
        if path is None:
            print(f"任务名单中的名字不存在，已跳过: {name}", file=sys.stderr, flush=True)
            continue
        if path.is_symlink():
            raise ConfigError(f"任务目录不能是文件链接: {name}")
        manifest = _task_manifest(path)
        validate_task_config(path / "task.toml")
        if not (path / "instruction.md").is_file() or not (path / "data").is_dir():
            raise ConfigError(f"任务 {name} 的 instruction.md 或 data/ 类型不正确")
        for relative in ("solution/solve.sh", "tests/test.sh"):
            if relative.startswith("solution/") and "solution" not in manifest:
                continue
            if not (path / relative).is_file():
                raise ConfigError(f"任务 {name} 缺少 {relative}")
        tasks.append(SelectedTask(name, path, manifest, _identity(manifest, config)))
    return tasks


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def completed_identities(output: Path) -> set[tuple[str, str]]:
    complete = set()
    for report in output.glob("*/tasks/*/execution.json"):
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
            if payload.get("runner") == "run_task" and payload.get("status") == "scored":
                complete.add((payload["task"], payload["identity"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return complete


def prepare_task(task: SelectedTask, target: Path, config: PipelineConfig) -> Path:
    target.mkdir(parents=True)
    for name in TASK_PARTS:
        source = task.path / name
        if name not in task.manifest:
            continue
        if source.is_dir():
            shutil.copytree(source, target / name)
        else:
            shutil.copy2(source, target / name)
    if _task_manifest(target) != task.manifest:
        raise StageFailure(f"任务 {task.name} 复制不完整，或源文件在复制时发生变化", stage="input")
    finalize_harbor_task(target, config, agent_timeout_sec=config.agent_timeout_sec)
    if manifest_differences(task.manifest["data"], content_manifest(target / "environment" / "data")):
        raise StageFailure(f"任务 {task.name} 的运行数据复制不完整", stage="input")
    return target


def _safe_error(error: BaseException, config: PipelineConfig) -> str:
    from .model_relay import redact_model_secrets

    return redact_model_secrets(f"{type(error).__name__}: {error}", config)


def execute_task(task: SelectedTask, run_root: Path, config: PipelineConfig) -> dict:
    task_root = run_root / "tasks" / task.name
    task_root.mkdir(parents=True)
    report = {"runner": "run_task", "task": task.name, "identity": task.identity, "model": config.author_env.model_id, "status": "running"}
    report_path = task_root / "execution.json"
    _write_json(report_path, report)
    print(f"[{task.name}] 开始运行", flush=True)
    try:
        if config.extra["cancel_event"].is_set():
            raise InterruptedError("运行已中断")
        prepared = prepare_task(task, task_root / "task", config)
        for attempt in range(3):
            try:
                result = run_harbor_agent(prepared, config, agent="claude-code", env_file=config.author_env.path, model=config.author_env.model_id, job_name="agent")
                break
            except StageFailure as error:
                if error.stage not in {"infrastructure", "proxy"} or attempt == 2:
                    raise
                if config.extra["cancel_event"].is_set():
                    raise InterruptedError("运行已中断") from error
                cleanup = asyncio.run(cleanup_logged_rjobs(prepared / "harbor-jobs/agent/runtime-logs", config.namespace))
                if cleanup:
                    raise StageFailure("旧集群资源未确认关闭，不重复提交: " + "; ".join(cleanup), stage="infrastructure") from error
                print(f"[{task.name}] 连接未完成，第 {attempt + 1} 次重试", flush=True)
                if config.extra["cancel_event"].wait((5, 15)[attempt]):
                    raise InterruptedError("运行已中断") from error
        if not result.assessment or result.assessment.result_type not in {"single-trial", "harbor-summary"}:
            raise StageFailure("没有取得可识别的独立评分结果", stage="infrastructure")
        if (result.returncode != 0 and not result.assessment.timed_out) or result.passed is None or result.model_requests_ok is False or (result.assessment.passed and not result.passed):
            raise StageFailure("模型调用或评分过程没有正常完成", stage="infrastructure")
        rewards = list(result.assessment.rewards)
        # score 为连续总分（0–1 小数）；passed 仅表示是否满分。
        report.update(status="scored", passed=result.passed, score=rewards[0] if len(rewards) == 1 else None, rewards=rewards, result_path=str(result.result_path), diagnostic=result.diagnostic)
        if result.assessment.timed_out:
            report.update(timed_out=True, score_source="verifier_after_agent_timeout")
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="interrupted" if isinstance(error, (KeyboardInterrupt, InterruptedError)) else "failed", error=_safe_error(error, config))
    finally:
        try:
            logs = task_root / "task" / "harbor-jobs" / "agent" / "runtime-logs"
            cleanup = asyncio.run(cleanup_logged_rjobs(logs, config.namespace))
            report["cleanup"] = cleanup
            if cleanup:
                report.update(status="failed", error="部分集群资源未确认关闭，请查看 cleanup 记录")
        except Exception as error:
            report.update(status="failed", error="集群清理失败: " + _safe_error(error, config))
        _write_json(report_path, report)
    if report["status"] == "scored":
        note = "执行超时，已采用最后保存提交的成绩；" if report.get("timed_out") else ""
        print(f"[{task.name}] {note}评分完成，得分: {report['rewards']}", flush=True)
    else:
        print(f"[{task.name}] 未完成: {report.get('error', report['status'])}", file=sys.stderr, flush=True)
    return report


def run_tasks(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    input_root = config.input_root.resolve()
    if config.output_root == input_root or input_root in config.output_root.parents:
        raise ConfigError("输出目录不能放在 final 内，避免修改成品任务")
    selected = select_tasks(Path(args.run_txt).expanduser().resolve(), config)
    completed = set() if args.force else completed_identities(config.output_root)
    pending = [task for task in selected if (task.name, task.identity) not in completed]
    skipped = [task.name for task in selected if (task.name, task.identity) in completed]
    for name in skipped:
        print(f"[{name}] 已完成评分，跳过；--force 可重新运行", flush=True)
    if not pending:
        print("没有待运行任务", flush=True)
        return 0
    if config.model_relay:
        from .model_relay import prepare_model_relay

        prepare_model_relay(config)
    if not PROJECT_HARBOR.is_file() or not shutil.which("rjob") or not shutil.which("brainctl"):
        raise ConfigError("缺少项目 Harbor、rjob 或 brainctl，请先检查本机环境")
    config.extra["claude_source"] = str(resolve_claude_source(config.claude_bin))
    config.output_root.mkdir(parents=True, exist_ok=True)
    run_root = config.output_root / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run_root.mkdir()
    print(f"本次结果目录: {run_root}", flush=True)
    reports = {}
    summary_path = run_root / "summary.json"

    def save_summary() -> None:
        _write_json(summary_path, {"runner": "run_task", "model": config.author_env.model_id, "skipped": skipped, "tasks": [reports[task.name] for task in pending if task.name in reports], "pending": [task.name for task in pending if task.name not in reports]})

    save_summary()
    interrupted = False
    with ThreadPoolExecutor(max_workers=min(config.concurrency, len(pending))) as executor:
        futures = {executor.submit(execute_task, task, run_root, config): task for task in pending}
        try:
            for future in as_completed(futures):
                reports[futures[future].name] = future.result()
                save_summary()
        except KeyboardInterrupt:
            interrupted = True
            config.extra["cancel_event"].set()
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print("收到中断，正在保存记录并关闭本次集群资源，请稍候", file=sys.stderr, flush=True)
            for future in futures:
                future.cancel()
    for future, task in futures.items():
        if not future.cancelled():
            reports[task.name] = future.result()
    save_summary()
    return 1 if interrupted or any(report["status"] != "scored" for report in reports.values()) else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        return run_tasks(args)
    except (ConfigError, PipelineError, OSError, ValueError) as error:
        print(f"失败: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("运行已中断", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    raise SystemExit(main())
