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
from .agent_config import AUTH_KEYS, model_connection, codex_settings
from .agent_runtime import resolve_codex_source


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


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
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
            passed = True
            reason = f"单任务已完成评分，verifier reward={reward:g}"
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
        passed = True
        reason = f"单任务已完成评分，reward={reward:g}"
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
    evals = stats.get("evals") if isinstance(stats, dict) else None
    if not isinstance(evals, dict):
        return None
    rewards: list[float] = []
    means: list[float] = []
    exceptions = stats.get("exception_stats")
    errors = _numeric(stats.get("n_errored_trials")) or 0
    for evaluation in evals.values():
        if not isinstance(evaluation, dict):
            continue
        exceptions = exceptions or evaluation.get("exception_stats")
        errors = errors or (_numeric(evaluation.get("n_errors")) or 0)
        metrics = evaluation.get("metrics", [])
        for metric in metrics if isinstance(metrics, list) else []:
            mean = _numeric(metric.get("mean")) if isinstance(metric, dict) else None
            if mean is not None:
                means.append(mean)
        reward_stats = evaluation.get("reward_stats")
        reward_map = reward_stats.get("reward") if isinstance(reward_stats, dict) else None
        if isinstance(reward_map, dict):
            for value, trials in reward_map.items():
                reward = _numeric(value)
                if reward is not None:
                    rewards.extend([reward] * (len(trials) if isinstance(trials, list) else 1))
    progress, output_rewards = _summary_output_evidence(output)
    scores = tuple(rewards or means or output_rewards)
    if not scores and progress:
        scores = (progress[2],)
    if exceptions or errors > 0:
        return ResultAssessment(False, "harbor-summary", f"汇总记录运行异常：{exceptions or errors}", scores)
    if not scores:
        return ResultAssessment(False, "harbor-summary", "汇总未取得可读取的成绩")
    return ResultAssessment(True, "harbor-summary", "汇总已完成评分", scores,
                            stats.get("n_completed_trials"), payload.get("n_total_trials"))


def _assess_result(payload: Any, output: str = "") -> ResultAssessment:
    if not isinstance(payload, dict):
        return ResultAssessment(False, "unknown", "结果文件顶层不是 JSON 对象")
    if "verifier_result" in payload or "exception_info" in payload or any(key in payload for key in ("passed", "success", "reward")):
        return _trial_assessment(payload)
    summary = _summary_assessment(payload, output)
    if summary is not None:
        return summary
    return ResultAssessment(False, "unknown", "结果文件中没有取得可读取的成绩或执行结论")


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
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            unknown.append(path)
            continue
        if not isinstance(payload, dict):
            unknown.append(path)
            continue
        is_trial = "verifier_result" in payload or "exception_info" in payload or any(key in payload for key in ("passed", "success", "reward"))
        if is_trial:
            trial_results.append(path)
        elif isinstance(payload.get("stats"), dict):
            summaries.append(path)
        else:
            unknown.append(path)

    if trial_results:
        latest = max(trial_results, key=lambda path: (path.stat().st_mtime_ns, str(path)))
        return latest, f"从本次新运行目录 {run_dir} 选择最新单任务结果"
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
    verdict = "已评分" if passed else "失败"
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
    agent_env = load_agent_env(env_file, role="模型代理") if env_file is not None else None
    jobs_dir = task_dir / "harbor-jobs" / job_name
    jobs_dir.mkdir(parents=True, exist_ok=True)
    previous_run_names = {path.name for path in jobs_dir.iterdir() if path.is_dir()}
    command = [
        str(PROJECT_HARBOR),
        "run",
        "--path", str(task_dir),
        "--agent", agent,
        "--env", ("task_builder.sandbox_runtime:TaskBuilderSandboxEnvironment" if getattr(config, "sandbox", False)
                  else "task_builder.hcluster_runtime:TaskBuilderRJobEnvironment"),
        "--jobs-dir", str(jobs_dir),
        "--n-attempts", "1",
        "--n-concurrent", "1",
        "--yes",
        "--delete",
    ]
    selected_model = model
    # 造任务验证（非 managed）通过 claude-opus-4-8 别名加 modelOverrides 使用非 Claude 模型；
    # run_task（managed）经反代直接透传模型名，两条路径在此有意不同。
    if agent == "claude-code" and selected_model and not config.extra.get("managed_harbor") and not (selected_model.startswith("claude-") or selected_model in {"sonnet", "opus", "haiku"}):
        selected_model = "claude-opus-4-8"
    if selected_model:
        command.extend(["--model", selected_model])
    env = os.environ.copy()
    for key in AUTH_KEYS:
        env.pop(key, None)
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
    env["TASK_BUILDER_AGENT"] = agent
    if agent == "claude-code":
        env["TASK_BUILDER_CLAUDE_SOURCE"] = config.extra.get("claude_source") or str(resolve_claude_source(config.claude_bin))
    elif agent == "codex":
        env["TASK_BUILDER_CODEX_SOURCE"] = config.extra.get("codex_source") or str(resolve_codex_source(config.codex_bin))
    if config.extra.get("managed_harbor"):
        command.extend(["--agent-setup-timeout-multiplier", "5"])
    temporary_token: str | None = None
    direct_model = bool(getattr(config, "sandbox", False) and not config.model_relay)
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
            if config.model_relay:
                from .model_relay import relay_module, selected_relay_entry

                entry = selected_relay_entry(config, agent_env)
                temporary_token = entry["token"]
                worker_base_url = entry["base_url"]
                advertised_host = urlsplit(worker_base_url).hostname
                scope = nullcontext()
                env = relay_module().proxy_environment(entry, env)
                for key in AUTH_KEYS:
                    env.pop(key, None)
            elif direct_model:
                worker_base_url, temporary_token = model_connection(agent_env.values, agent_env.agent)
                advertised_host = urlsplit(worker_base_url).hostname
                scope = nullcontext()
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
                base_key = "OPENAI_BASE_URL" if agent == "codex" else "ANTHROPIC_BASE_URL"
                token_key = "OPENAI_API_KEY" if agent == "codex" else "ANTHROPIC_AUTH_TOKEN"
                proxy_env_names = {base_key: "TASK_BUILDER_PROXY_BASE_URL", token_key: "TASK_BUILDER_PROXY_TOKEN"}
                if agent == "claude-code" and direct_model and not agent_env.values.get("ANTHROPIC_AUTH_TOKEN"):
                    worker_values["ANTHROPIC_API_KEY"] = worker_values.pop("ANTHROPIC_AUTH_TOKEN")
                    proxy_env_names["ANTHROPIC_API_KEY"] = "TASK_BUILDER_PROXY_TOKEN"
                env["TASK_BUILDER_PROXY_BASE_URL"] = worker_values[base_key]
                env["TASK_BUILDER_PROXY_TOKEN"] = temporary_token
                for key, value in worker_values.items():
                    if key in proxy_env_names:
                        command.extend(["--ae", f"{key}=${{{proxy_env_names[key]}}}"])
                    elif key == "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT":
                        command.extend(["--ae", f"{key}={value}"])
                    elif key == "REASONING_EFFORT":
                        command.extend(["--ae", f"{key}={value}"])
                if agent == "codex":
                    settings = codex_settings(worker_values[base_key], agent_env.values.get("REASONING_EFFORT"))
                    command.extend(["--ak", "config=" + json.dumps(settings, separators=(",", ":"))])
                    if agent_env.values.get("REASONING_EFFORT"):
                        command.extend(["--ak", "reasoning_effort=" + agent_env.values["REASONING_EFFORT"]])
                elif selected_model and not config.extra.get("managed_harbor"):
                    settings = claude_code_settings(model or selected_model)
                    if settings:
                        command.extend(["--ak", "config=" + json.dumps(settings, separators=(",", ":"))])
                for key in ("NO_PROXY", "no_proxy"):
                    env[key] = _append_no_proxy(env.get(key, ""), advertised_host)
                if config.model_relay or direct_model:
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
        if getattr(config, "sandbox", False):
            import asyncio
            from .sandbox_runtime import cleanup_logged_sandboxes

            cleanup_failures = asyncio.run(cleanup_logged_sandboxes(jobs_dir / "runtime-logs"))
            if cleanup_failures:
                raise StageFailure("沙盒清理未完成：" + "; ".join(cleanup_failures), stage="infrastructure")

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
    # RJob 与 Sandbox 共用：答案产物收集是尽力执行，空提交没有产物是正常情况。
    # 评分日志下载、输入上传和实际环境故障仍需重试，不能当作题目问题修复。
    entries = []
    for path in log_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("time"), (int, float)) and entry["time"] * 1_000_000_000 >= started_ns:
                entries.append(entry)
    failures = {}
    for entry in sorted(entries, key=lambda item: item["time"]):
        event = entry.get("event")
        source = str(entry.get("source", ""))
        target = str(entry.get("target", ""))
        if event in {"infrastructure-failed", "renew-failed"}:
            return "运行环境故障：" + str(entry.get("error") or entry.get("operation", event))
        if source == "/logs/agent" or source.startswith("/logs/agent/"):
            continue
        if event == "download-failed" and source.rstrip("/") != "/logs/verifier" and not source.startswith("/logs/verifier/") and "artifacts" in Path(target).parts:
            continue
        key = (source, target)
        if event in {"upload-failed", "download-failed"}:
            failures[key] = str(entry.get("error") or f"{event}: {source} -> {target}")
        elif event in {"upload-complete", "download-complete", "upload-verified", "download-verified"}:
            failures.pop(key, None)
    return "; ".join(failures.values())


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
