from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import PipelineConfig, SourceBundle, StageFailure, TaskTimeout, ValidationIssue, ValidationReport
from .deadline import has_deadline, remaining_timeout, run_process
from .paths import PROJECT_HARBOR
from .staging import prepare_harbor_task
from .claude_proxy import (
    ClaudeAPIProxy,
    ClaudeProxyError,
    WorkerProxyCredentials,
    claude_code_settings,
    worker_environment_values,
)
from .config import load_agent_env
from .hcluster_runtime import resolve_claude_source


@dataclass(frozen=True)
class ResultAssessment:
    passed: bool
    result_type: str
    reason: str
    rewards: tuple[float, ...] = ()
    completed_trials: int | None = None
    total_trials: int | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class HarborRunResult:
    name: str
    passed: bool | None
    returncode: int
    command: tuple[str, ...]
    result_path: Path | None
    output: str
    diagnostic: str = ""
    assessment: ResultAssessment | None = None
    model_requests_ok: bool | None = None


def _result_files(root: Path) -> list[Path]:
    return sorted(root.rglob("result.json")) if root.exists() else []


def _task_name(task_dir: Path) -> str | None:
    try:
        lines = (task_dir / "task.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_task_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_task_section = stripped == "[task]"
        elif in_task_section:
            match = re.match(r"name\s*=\s*['\"]([^'\"]+)['\"]", stripped)
            if match:
                return match.group(1)
    return None


def _payload_belongs_to_task(payload: dict[str, Any], task_dir: Path) -> tuple[bool, str]:
    expected_name = _task_name(task_dir)
    actual_name = payload.get("task_name")
    if expected_name and isinstance(actual_name, str) and actual_name != expected_name:
        return False, f"任务名称不符：期望 {expected_name}，实际 {actual_name}"

    expected_path = task_dir.resolve()
    owner_paths: list[str] = []
    task_id = payload.get("task_id")
    if isinstance(task_id, dict) and isinstance(task_id.get("path"), str):
        owner_paths.append(task_id["path"])
    config = payload.get("config")
    if isinstance(config, dict):
        config_task = config.get("task")
        if isinstance(config_task, dict) and isinstance(config_task.get("path"), str):
            owner_paths.append(config_task["path"])
    for owner_path in owner_paths:
        if Path(owner_path).resolve() != expected_path:
            return False, f"任务目录不符：期望 {expected_path}，实际 {owner_path}"
    if owner_paths:
        return True, f"任务名称和目录匹配：{expected_name or expected_path}"
    return True, f"结果在本次任务专属运行目录内；结果未提供任务目录字段（任务名：{expected_name or '未知'}）"


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _trial_assessment(payload: dict[str, Any]) -> ResultAssessment:
    exception = payload.get("exception_info")
    timed_out = isinstance(exception, dict) and exception.get("exception_type") == "AgentTimeoutError"
    if payload.get("exception_info"):
        exception = payload["exception_info"]
        if not isinstance(exception, dict) or exception.get("exception_type") not in {"DownloadAgentDirError", "AgentTimeoutError"}:
            details = str(exception) if not isinstance(exception, dict) else str(exception.get("exception_type", "")) + "；" + str(exception.get("exception_message", ""))
            return ResultAssessment(False, "single-trial", "单任务结果包含异常信息：" + details[:4000])
    verifier = payload.get("verifier_result")
    if isinstance(verifier, dict):
        rewards = verifier.get("rewards")
        reward = _numeric(rewards.get("reward")) if isinstance(rewards, dict) else None
        if reward is None:
            reward = _numeric(verifier.get("reward"))
        if reward is not None:
            if timed_out and not 0.0 <= reward <= 1.0:
                return ResultAssessment(False, "single-trial", "执行超时且评分器分数不在 0–1 范围内")
            passed = reward == 1.0
            reason = f"单任务 verifier reward={reward:g}" + ("，达到通过标准" if passed else "，未达到 1.0")
            if timed_out:
                reason = "执行超时，采用最后保存提交的评分；" + reason
            return ResultAssessment(passed, "single-trial", reason, (reward,), 1, 1, timed_out=timed_out)
        if timed_out:
            return ResultAssessment(False, "single-trial", "执行超时且评分器没有有效数值成绩")
        if isinstance(verifier.get("passed"), bool):
            passed = verifier["passed"]
            return ResultAssessment(passed, "single-trial", f"单任务 verifier passed={passed}", (), 1, 1)
        return ResultAssessment(False, "single-trial", "单任务结果缺少可识别的 verifier reward/passed 字段")
    if timed_out:
        return ResultAssessment(False, "single-trial", "执行超时且评分器没有有效数值成绩")
    for key in ("passed", "success"):
        if isinstance(payload.get(key), bool):
            passed = payload[key]
            return ResultAssessment(passed, "single-trial", f"单任务字段 {key}={passed}", (), 1, 1)
    reward = _numeric(payload.get("reward"))
    if reward is not None:
        passed = reward == 1.0
        reason = f"单任务 reward={reward:g}" + ("，达到通过标准" if passed else "，未达到 1.0")
        return ResultAssessment(passed, "single-trial", reason, (reward,), 1, 1)
    return ResultAssessment(False, "unknown", "结果格式无法识别：缺少单任务 verifier 结果或 Harbor 汇总 stats.evals")


def _summary_output_evidence(output: str) -> tuple[tuple[int, int, float] | None, tuple[float, ...]]:
    progress_matches = re.findall(r"(?m)(\d+)\s*/\s*(\d+)\s+Mean:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", output)
    progress = None
    if progress_matches:
        completed, total, mean = progress_matches[-1]
        progress = int(completed), int(total), float(mean)
    reward_matches = re.findall(r"(?i)\bReward\s*[:=]\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", output)
    return progress, tuple(float(value) for value in reward_matches)


def _summary_assessment(payload: dict[str, Any], output: str = "") -> ResultAssessment | None:
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None
    evals = stats.get("evals")
    if not isinstance(evals, dict) or not evals:
        return None

    summary_exception_stats = stats.get("exception_stats")
    if isinstance(summary_exception_stats, dict) and summary_exception_stats:
        exception_count = sum(value for value in summary_exception_stats.values() if isinstance(value, int) and not isinstance(value, bool))
        count_text = f"异常 {exception_count} 个" if exception_count else "存在异常"
        return ResultAssessment(False, "harbor-summary", f"汇总明确记录{count_text}：{summary_exception_stats}")

    trial_counts: list[int] = []
    eval_errors: list[int] = []
    means: list[float] = []
    rewards: list[float] = []
    for eval_name, evaluation in evals.items():
        if not isinstance(evaluation, dict):
            return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 的汇总格式错误")
        count = evaluation.get("n_trials")
        error_count = evaluation.get("n_errors", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 缺少有效 n_trials")
        if not isinstance(error_count, int) or isinstance(error_count, bool) or error_count < 0:
            return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 的 n_errors 无效")
        trial_counts.append(count)
        eval_errors.append(error_count)

        exception_stats = evaluation.get("exception_stats")
        if error_count or (isinstance(exception_stats, dict) and exception_stats):
            details = f"；异常详情：{exception_stats}" if exception_stats else ""
            return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 明确记录异常 {error_count} 个{details}", (), None, None)

        metrics = evaluation.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 缺少 metrics/mean")
        for metric in metrics:
            mean = _numeric(metric.get("mean")) if isinstance(metric, dict) else None
            if mean is None:
                return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 存在无效 mean")
            means.append(mean)

        reward_stats = evaluation.get("reward_stats")
        if reward_stats is not None:
            reward_map = reward_stats.get("reward") if isinstance(reward_stats, dict) else None
            if not isinstance(reward_map, dict):
                return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 的 reward_stats 格式无法识别")
            eval_rewards: list[float] = []
            for reward_text, trial_ids in reward_map.items():
                try:
                    reward = float(reward_text)
                except (TypeError, ValueError):
                    return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 的奖励值无效：{reward_text}")
                if not math.isfinite(reward) or not isinstance(trial_ids, list):
                    return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 的 reward_stats 条目无效")
                eval_rewards.extend([reward] * len(trial_ids))
            if len(eval_rewards) != count:
                return ResultAssessment(False, "harbor-summary", f"评测 {eval_name} 的奖励条数 {len(eval_rewards)} 与 n_trials={count} 不一致", tuple(eval_rewards))
            rewards.extend(eval_rewards)

    total = payload.get("n_total_trials")
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        total = sum(trial_counts)
    completed = stats.get("n_completed_trials")
    errored = stats.get("n_errored_trials", sum(eval_errors))
    running = stats.get("n_running_trials", 0)
    pending = stats.get("n_pending_trials", 0)
    cancelled = stats.get("n_cancelled_trials", 0)
    counts = (completed, errored, running, pending, cancelled)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        return ResultAssessment(False, "harbor-summary", "汇总缺少有效的已完成/异常/运行中/等待中任务数量", tuple(rewards), None, total)
    if sum(trial_counts) != total:
        return ResultAssessment(False, "harbor-summary", f"各评测任务数量合计 {sum(trial_counts)} 与总数 {total} 不一致", tuple(rewards), completed, total)
    if completed + errored + running + pending + cancelled != total:
        return ResultAssessment(False, "harbor-summary", f"任务数量不完整：完成 {completed}、异常 {errored}、运行中 {running}、等待中 {pending}、取消 {cancelled}，总数 {total}", tuple(rewards), completed, total)
    if completed != total or errored or running or pending or cancelled or any(eval_errors):
        return ResultAssessment(False, "harbor-summary", f"任务未全部成功完成：完成 {completed}/{total}，异常 {max(errored, sum(eval_errors))}，运行中 {running}，等待中 {pending}，取消 {cancelled}", tuple(rewards), completed, total)

    if not means or any(mean != 1.0 for mean in means):
        return ResultAssessment(False, "harbor-summary", f"汇总平均奖励未全部达到 1.0：{means}", tuple(rewards or means), completed, total)
    if rewards and (len(rewards) != total or any(reward != 1.0 for reward in rewards)):
        return ResultAssessment(False, "harbor-summary", f"汇总奖励未全部达到 1.0：{rewards}", tuple(rewards), completed, total)

    progress, output_rewards = _summary_output_evidence(output)
    if progress and (progress[0] != total or progress[1] != total or progress[2] != 1.0):
        return ResultAssessment(False, "harbor-summary", f"Harbor 输出摘要 {progress[0]}/{progress[1]} mean={progress[2]:g} 与通过条件不符", tuple(rewards or means), completed, total)
    if output_rewards and any(reward != 1.0 for reward in output_rewards):
        return ResultAssessment(False, "harbor-summary", f"Harbor 输出中存在失败奖励：{output_rewards}", tuple(rewards or output_rewards), completed, total)

    reward_description = f"，reward_stats={rewards}" if rewards else ""
    progress_description = f"，控制台进度={progress[0]}/{progress[1]} mean={progress[2]:g}" if progress else ""
    return ResultAssessment(True, "harbor-summary", f"全部 {completed}/{total} 个任务完成，所有 mean=1.0{reward_description}{progress_description}", tuple(rewards or means), completed, total)


def _assess_result(payload: Any, output: str = "") -> ResultAssessment:
    if not isinstance(payload, dict):
        return ResultAssessment(False, "unknown", "结果文件顶层不是 JSON 对象")
    if "verifier_result" in payload or "exception_info" in payload or any(key in payload for key in ("passed", "success", "reward")):
        return _trial_assessment(payload)
    summary = _summary_assessment(payload, output)
    if summary is not None:
        return summary
    return ResultAssessment(False, "unknown", "结果格式无法识别或缺少关键字段（需要单任务 verifier 结果或完整的 stats.evals 汇总）")


def _trial_passed(payload: Any) -> bool:
    return _assess_result(payload).passed


def _current_run_dirs(jobs_dir: Path, previous_names: set[str], started_ns: int) -> list[Path]:
    if not jobs_dir.is_dir():
        return []
    minimum_mtime_ns = started_ns - 1_000_000_000
    candidates = []
    for path in jobs_dir.iterdir():
        if not path.is_dir() or path.name in previous_names or path.name == "runtime-logs":
            continue
        try:
            if path.stat().st_mtime_ns >= minimum_mtime_ns:
                candidates.append(path)
        except OSError:
            continue
    return sorted(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _select_current_result(task_dir: Path, jobs_dir: Path, previous_names: set[str], started_ns: int) -> tuple[Path | None, str]:
    run_dirs = _current_run_dirs(jobs_dir, previous_names, started_ns)
    if not run_dirs:
        return None, "本次 Harbor 运行没有创建新的运行目录；为避免误用旧结果，拒绝读取历史结果文件"
    run_dir = run_dirs[-1]
    minimum_mtime_ns = started_ns - 1_000_000_000
    candidates = []
    for path in _result_files(run_dir):
        try:
            if path.stat().st_mtime_ns >= minimum_mtime_ns:
                candidates.append(path)
        except OSError:
            continue
    if not candidates:
        return None, f"本次运行目录 {run_dir} 中没有新生成的 result.json"

    trial_results: list[Path] = []
    summaries: list[Path] = []
    unknown: list[Path] = []
    mismatches: list[str] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            unknown.append(path)
            mismatches.append(f"{path}: JSON 读取失败（{exc}）")
            continue
        if not isinstance(payload, dict):
            unknown.append(path)
            continue
        is_trial = "verifier_result" in payload or "exception_info" in payload or any(key in payload for key in ("passed", "success", "reward"))
        if is_trial:
            belongs, reason = _payload_belongs_to_task(payload, task_dir)
            if belongs:
                trial_results.append(path)
            else:
                mismatches.append(f"{path}: {reason}")
        elif isinstance(payload.get("stats"), dict):
            summaries.append(path)
        else:
            unknown.append(path)

    task_mismatches = [item for item in mismatches if "任务名称不符" in item or "任务目录不符" in item]
    if task_mismatches:
        return None, "本次结果不属于当前任务：" + "；".join(task_mismatches)
    if len(trial_results) > 1:
        paths = ", ".join(str(path) for path in trial_results)
        return None, f"本次 Harbor 运行发现多个当前任务的单任务结果，无法安全选择：{paths}"
    if trial_results:
        return trial_results[0], f"从本次新运行目录 {run_dir} 选择当前任务的单任务结果"
    if summaries:
        summaries.sort(key=lambda path: (path.stat().st_mtime_ns, str(path)))
        return summaries[-1], f"本次运行没有单任务结果，使用新运行目录 {run_dir} 的最新汇总结果"
    if unknown:
        unknown.sort(key=lambda path: (path.stat().st_mtime_ns, str(path)))
        return unknown[-1], f"本次运行仅发现无法识别或损坏的结果文件：{unknown[-1]}"
    return None, f"本次运行目录 {run_dir} 中没有属于当前任务的有效结果"


def _format_diagnostic(result_path: Path | None, selection_reason: str, assessment: ResultAssessment, returncode: int) -> str:
    passed = assessment.passed and (returncode == 0 or assessment.timed_out)
    reason = assessment.reason
    if returncode != 0:
        reason = f"Harbor 进程退出码为 {returncode}；{reason}"
    rewards = ", ".join(f"{reward:g}" for reward in assessment.rewards) if assessment.rewards else "未解析"
    completed = f"{assessment.completed_trials}/{assessment.total_trials}" if assessment.completed_trials is not None and assessment.total_trials is not None else "未解析"
    verdict = "通过" if passed else "已评分（未满分）" if assessment.timed_out else "失败"
    return "\n".join((
        "Harbor 结果诊断:",
        f"选中结果文件: {result_path if result_path else '无'}",
        f"结果类型: {assessment.result_type}",
        f"奖励或均值: {rewards}",
        f"完成任务数: {completed}",
        f"结果选择依据: {selection_reason}",
        f"最终判定: {verdict}；原因：{reason}",
    ))


def _scrub_temporary_token(root: Path, token: str) -> None:
    needle = token.encode("utf-8")
    replacement = b"<temporary-proxy-token>"
    text_suffixes = {".json", ".jsonl", ".log", ".txt", ".yaml", ".yml", ".toml", ".env"}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in text_suffixes:
            continue
        try:
            content = path.read_bytes()
            if needle in content:
                path.write_bytes(content.replace(needle, replacement))
        except OSError:
            continue


def _append_no_proxy(value: str, host: str) -> str:
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if host not in entries:
        entries.append(host)
    return ",".join(entries)


def _run_managed_harbor(command: list[str], task_dir: Path, env: dict[str, str], config: PipelineConfig, output_path: Path) -> subprocess.CompletedProcess[str]:
    interrupted = False
    timed_out = False
    with output_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=task_dir, env=env, text=True, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + config.harbor_timeout_sec
        try:
            while process.poll() is None:
                interrupted = config.extra["cancel_event"].is_set()
                timed_out = time.monotonic() >= deadline
                if interrupted or timed_out:
                    break
                time.sleep(1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=660)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    text = output_path.read_text(encoding="utf-8", errors="replace")
    if interrupted:
        raise StageFailure("运行已中断；已等待运行程序清理集群资源", stage="infrastructure")
    if timed_out:
        raise subprocess.TimeoutExpired(command, config.harbor_timeout_sec, output=text)
    return subprocess.CompletedProcess(command, process.returncode, text)


def run_harbor_agent(task_dir: Path, config: PipelineConfig, *, agent: str, env_file: Path | None = None, model: str | None = None, job_name: str) -> HarborRunResult:
    jobs_dir = task_dir / "harbor-jobs" / job_name
    jobs_dir.mkdir(parents=True, exist_ok=True)
    previous_run_names = {path.name for path in jobs_dir.iterdir() if path.is_dir()}
    command = [
        str(PROJECT_HARBOR),
        "run",
        "--path", str(task_dir),
        "--agent", agent,
        "--env", "task_builder.hcluster_runtime:TaskBuilderRJobEnvironment",
        "--jobs-dir", str(jobs_dir),
        "--n-attempts", "1",
        "--n-concurrent", "1",
        "--yes",
        "--delete",
    ]
    selected_model = model
    # 造任务验证（非 managed）通过 claude-opus-4-8 别名加 modelOverrides 使用非 Claude 模型；
    # run_task（managed）经反代直接透传模型名，两条路径在此有意不同。
    if selected_model and not config.extra.get("managed_harbor") and not (selected_model.startswith("claude-") or selected_model in {"sonnet", "opus", "haiku"}):
        selected_model = "claude-opus-4-8"
    if selected_model:
        command.extend(["--model", selected_model])
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    env["TASK_BUILDER_RJOB_NAMESPACE"] = config.namespace
    env["TASK_BUILDER_CHARGED_GROUP"] = config.charged_group
    env["TASK_BUILDER_RJOB_MOUNT"] = config.mount
    env["TASK_BUILDER_RJOB_MAX_CPU"] = str(config.cpu)
    env["TASK_BUILDER_RJOB_MAX_MEMORY_MB"] = str(config.memory_mb)
    env["TASK_BUILDER_MAX_AGENT_GPUS"] = str(config.agent_gpus)
    env["TASK_BUILDER_MAX_VERIFIER_GPUS"] = str(config.verifier_gpus)
    env["TASK_BUILDER_PRIORITY"] = str(config.priority)
    env["TASK_BUILDER_EXPERIMENT_IMAGE"] = config.experiment_image
    env["TASK_BUILDER_EVALUATION_IMAGE"] = config.evaluation_image
    env["TASK_BUILDER_RUNTIME_LOG_DIR"] = str(jobs_dir / "runtime-logs")
    # 两条运行路径都显式指定上传到 RJob 的 Claude Code，可执行文件由 --claude-bin 决定。
    env["TASK_BUILDER_CLAUDE_SOURCE"] = config.extra.get("claude_source") or str(resolve_claude_source(config.claude_bin))
    if config.extra.get("managed_harbor"):
        command.extend(["--agent-setup-timeout-multiplier", "5"])
    temporary_token: str | None = None
    proxy: ClaudeAPIProxy | None = None
    started_ns = time.time_ns()
    output_path = jobs_dir / "harbor-output.log"
    command_path = jobs_dir / "harbor-command.json"

    def save_output(output: str) -> str:
        if temporary_token:
            output = output.replace(temporary_token, "<temporary-proxy-token>")
        output_path.write_text(output, encoding="utf-8")
        safe = [str(argument).replace(temporary_token, "<temporary-proxy-token>") if temporary_token else str(argument) for argument in command]
        command_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        return output

    def execute() -> subprocess.CompletedProcess[str]:
        if config.extra.get("managed_harbor"):
            return _run_managed_harbor(command, task_dir, env, config, output_path)
        runner = run_process if has_deadline() else subprocess.run
        return runner(command, cwd=task_dir, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=remaining_timeout(config.harbor_timeout_sec), check=False)

    try:
        if env_file is None:
            started_ns = time.time_ns()
            completed = execute()
        else:
            agent_env = load_agent_env(env_file, role="模型代理")
            if config.model_relay:
                from .model_relay import relay_module, selected_relay_entry

                entry = selected_relay_entry(config, agent_env)
                temporary_token = entry["token"]
                worker_base_url = entry["base_url"]
                advertised_host = urlsplit(worker_base_url).hostname
                scope = nullcontext()
                env = relay_module().proxy_environment(entry, env)
                for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                    env.pop(key, None)
            else:
                proxy = ClaudeAPIProxy(
                    agent_env,
                    bind_host=config.proxy_bind,
                    bind_port=config.proxy_port,
                    advertised_host=config.proxy_host,
                    timeout_sec=config.agent_timeout_sec + 300,
                )
                scope = proxy
            with scope:
                if proxy is not None:
                    temporary_token = proxy.worker_token
                    worker_base_url = proxy.base_url
                    advertised_host = proxy.advertised_host
                worker_values = worker_environment_values(
                    agent_env,
                    WorkerProxyCredentials(worker_base_url, temporary_token, selected_model or "claude-opus-4-8"),
                )
                proxy_env_names = {
                    "ANTHROPIC_BASE_URL": "TASK_BUILDER_PROXY_BASE_URL",
                    "ANTHROPIC_AUTH_TOKEN": "TASK_BUILDER_PROXY_TOKEN",
                }
                env["TASK_BUILDER_PROXY_BASE_URL"] = worker_values["ANTHROPIC_BASE_URL"]
                env["TASK_BUILDER_PROXY_TOKEN"] = worker_values["ANTHROPIC_AUTH_TOKEN"]
                for key, value in worker_values.items():
                    if key in proxy_env_names:
                        command.extend(["--ae", f"{key}=${{{proxy_env_names[key]}}}"])
                    elif key == "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT":
                        command.extend(["--ae", f"{key}={value}"])
                    elif key == "REASONING_EFFORT":
                        command.extend(["--ae", f"{key}={value}"])
                if selected_model and not config.extra.get("managed_harbor"):
                    settings = claude_code_settings(model or selected_model)
                    if settings:
                        command.extend(["--ak", "config=" + json.dumps(settings, separators=(",", ":"))])
                for key in ("NO_PROXY", "no_proxy"):
                    env[key] = _append_no_proxy(env.get(key, ""), advertised_host)
                if config.model_relay:
                    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                        command.extend(["--ae", f"{key}="])
                    for key in ("NO_PROXY", "no_proxy"):
                        command.extend(["--ae", f"{key}={advertised_host}"])
                started_ns = time.time_ns()
                completed = execute()
    except TaskTimeout as exc:
        partial = getattr(exc, "stdout", "") or ""
        exc.stdout = save_output(partial.decode(errors="replace") if isinstance(partial, bytes) else partial)
        if temporary_token and getattr(exc, "stderr", None):
            errors = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            exc.stderr = errors.replace(temporary_token, "<temporary-proxy-token>")
        raise
    except FileNotFoundError as exc:
        save_output(str(exc))
        raise StageFailure(f"找不到 Harbor: {PROJECT_HARBOR}", stage="harbor") from exc
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        save_output(partial.decode("utf-8", errors="replace") if isinstance(partial, bytes) else partial)
        raise StageFailure(f"Harbor 运行超时: {job_name}；输出保存在 {output_path}", stage="infrastructure") from exc
    except ClaudeProxyError as exc:
        save_output(str(exc))
        raise StageFailure(f"启动本机模型反代失败: {exc}", stage="proxy") from exc
    except StageFailure as exc:
        partial = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
        save_output(partial + "\n" + str(exc))
        raise
    finally:
        if proxy is not None:
            (jobs_dir / "model-proxy-diagnostic.txt").write_text(proxy.diagnostic(), encoding="utf-8")
        if temporary_token:
            _scrub_temporary_token(jobs_dir, temporary_token)

    output = save_output(completed.stdout or "")
    safe_command = tuple(
        "ANTHROPIC_AUTH_TOKEN=<temporary-proxy-token>" if argument.startswith("ANTHROPIC_AUTH_TOKEN=") else argument
        for argument in command
    )
    result_path, selection_reason = _select_current_result(task_dir, jobs_dir, previous_run_names, started_ns)
    if result_path is None:
        assessment = ResultAssessment(False, "missing", selection_reason)
    else:
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            assessment = _assess_result(payload, output)
        except (OSError, json.JSONDecodeError) as exc:
            assessment = ResultAssessment(False, "invalid-json", f"无法读取结果文件：{exc}")
    passed = assessment.passed and (completed.returncode == 0 or assessment.timed_out)
    diagnostic = _format_diagnostic(result_path, selection_reason, assessment, completed.returncode)
    diagnostic_path = jobs_dir / "harbor-diagnostic.txt"
    diagnostic_path.write_text(diagnostic, encoding="utf-8")
    transport_failure = _critical_runtime_failure(jobs_dir / "runtime-logs", started_ns)
    if transport_failure:
        raise StageFailure(f"关键集群传输失败：{transport_failure}；日志: {output_path}\n{diagnostic}", stage="infrastructure")
    runtime_types = ("EnvironmentStartError", "EnvironmentSetupTimeoutError", "HealthcheckError", "DownloadVerifierDirError", "AddTestsDirError")
    model_types = ("UnknownApiError", "AuthenticationError", "RateLimitError", "APIConnectionError", "APITimeoutError")
    exception_types = _result_exception_types(payload) if result_path and assessment.result_type != "invalid-json" else set()
    if exception_types.intersection(model_types):
        raise StageFailure(f"模型调用未完成；日志: {output_path}\n{diagnostic}", stage="proxy")
    if exception_types.intersection(runtime_types):
        raise StageFailure(f"集群运行未完成；日志: {output_path}\n{diagnostic}", stage="infrastructure")
    if proxy is not None:
        proxy_diagnostic = proxy.diagnostic()
        if proxy.successful_upstream_requests == 0:
            raise StageFailure("Worker 没有收到任何成功的模型 API 响应\n" + proxy_diagnostic, stage="proxy")
        else:
            proxy_diagnostic += "\n最终判定: 已确认 RJob Worker 收到真实模型 API 成功响应"
        diagnostic = f"{diagnostic}\n{proxy_diagnostic}"
    if result_path is None or (completed.returncode != 0 and not assessment.timed_out) or assessment.result_type in {"missing", "invalid-json", "unknown"} or "异常" in assessment.reason or (not assessment.rewards and assessment.completed_trials is None):
        raise StageFailure(f"验证未完成有效评分；请检查并修复任务脚本；日志: {output_path}\n{diagnostic}", stage="validation")
    return HarborRunResult(job_name, passed, completed.returncode, safe_command, result_path, output, diagnostic, assessment, proxy.successful_upstream_requests > 0 if proxy is not None else None)


def _result_exception_types(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    exception = payload.get("exception_info")
    names = {str(exception.get("exception_type"))} if isinstance(exception, dict) else set()
    stats = payload.get("stats")
    if isinstance(stats, dict):
        evaluations = stats.get("evals", {})
        for item in [stats, *(evaluations.values() if isinstance(evaluations, dict) else ())]:
            if isinstance(item, dict) and isinstance(item.get("exception_stats"), dict):
                names.update(str(name) for name, count in item["exception_stats"].items() if count)
    return names


def _critical_runtime_failure(log_dir: Path, started_ns: int) -> str:
    for path in log_dir.glob("*.jsonl"):
        failures = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("time"), (int, float)) or entry["time"] * 1_000_000_000 < started_ns:
                continue
            source = str(entry.get("source", ""))
            if source == "/logs/agent" or source.startswith("/logs/agent/"):
                continue
            key = (entry.get("source"), entry.get("target"))
            if entry.get("event") in {"upload-failed", "download-failed"}:
                failures[key] = str(entry.get("error", "传输失败"))
            elif entry.get("event") in {"upload-verified", "download-verified"}:
                failures.pop(key, None)
        if failures:
            return "; ".join(failures.values())
    return ""


def _single_reward(result: HarborRunResult) -> float | None:
    rewards = result.assessment.rewards if result.assessment else ()
    return rewards[0] if len(rewards) == 1 else None


def validate_with_harbor(draft: Path, source: SourceBundle, workspace, config: PipelineConfig) -> ValidationReport:
    task_dir = prepare_harbor_task(draft, source, workspace.validation_dir / "harbor-task", config)
    issues: list[ValidationIssue] = []
    evidence: dict[str, object] = {}
    for agent, job_name, accepts, requirement in (
        ("nop", "starter", lambda reward: reward == 0.0, "空提交总分必须为 0"),
        ("oracle", "oracle", lambda reward: 0.0 < reward <= 1.0, "参考提交总分必须大于 0 且不超过 1"),
    ):
        result = run_harbor_agent(task_dir, config, agent=agent, job_name=job_name)
        (workspace.logs_dir / f"harbor-{job_name}.log").write_text(f"{result.output.rstrip()}\n\n{result.diagnostic}\n", encoding="utf-8")
        reward = _single_reward(result)
        evidence[job_name] = {"result": result.result_path and str(result.result_path), "reward": reward, "diagnostic": result.diagnostic}
        if result.returncode != 0 or result.result_path is None or reward is None or not accepts(reward):
            shown = "未解析" if reward is None else f"{reward:g}"
            issues.append(ValidationIssue("harbor", f"{requirement}，实际为 {shown}", result.diagnostic or result.output[-4000:]))
    report = ValidationReport("harbor", not issues, tuple(issues), evidence)
    _write_harbor_report(workspace, report)
    return report


def _write_harbor_report(workspace, report: ValidationReport) -> None:
    payload = {"passed": report.passed, "issues": [issue.render() for issue in report.issues], "evidence": report.evidence}
    (workspace.validation_dir / "harbor-report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
