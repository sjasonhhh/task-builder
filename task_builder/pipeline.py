from __future__ import annotations

import sys
import json
import traceback
import asyncio
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import shutil
from pathlib import Path

from .agents import IndependentClaudeAgent
from .discovery import discover_sources, read_run_names, validate_source
from .harbor_runtime import validate_with_harbor
from .models import AgentError, PipelineConfig, SourceBundle, StageFailure, TaskTimeout, ValidationIssue, ValidationReport
from .deadline import remaining_timeout, task_deadline
from .planner_agent import run_planner_agent
from .reviewer_agent import run_reviewer_agent
from .source_privacy import validate_public_description
from .staging import publish_task
from .resume import restore_source, save_run_identity
from .usage import write_source_usage
from .writer_agent import run_repair_writer_agent, run_writer_agent
from .workspace import SourceWorkspace, create_shared_workspace, create_source_workspace, new_run_root, record_event, snapshot_draft, write_failure

_REQUIRED_PUBLISHED = (
    "task.toml",
    "instruction.md",
    "data",
    "README.md",
    "solution/solve.sh",
    "tests/test.sh",
)
_FORBIDDEN_PUBLISHED = ("environment", "context", "code")


def is_complete_published_task(path: Path) -> bool:
    if not path.is_dir():
        return False
    for relative in _REQUIRED_PUBLISHED:
        candidate = path / relative
        if relative == "data":
            if not candidate.is_dir():
                return False
        elif not candidate.is_file():
            return False
    return not any((path / name).exists() for name in _FORBIDDEN_PUBLISHED)


def published_source_slugs(output_root: Path) -> set[str]:
    final_root = output_root / "final"
    if not final_root.is_dir():
        return set()
    return {path.name for path in final_root.iterdir() if path.is_dir() and is_complete_published_task(path)}


def migrate_staged_published_tasks(output_root: Path, sources: list[SourceBundle]) -> None:
    runs_root = output_root / "runs"
    if not runs_root.is_dir():
        return
    source_by_slug = {source.source_slug: source for source in sources}
    final_root = output_root / "final"
    for run_dir in sorted(runs_root.iterdir(), key=lambda path: path.name):
        published_dir = run_dir / "published"
        if not published_dir.is_dir():
            continue
        for candidate in sorted(published_dir.iterdir(), key=lambda path: path.name):
            source = source_by_slug.get(candidate.name)
            if source is None or not is_complete_published_task(candidate):
                continue
            if not validate_public_description(candidate, source).passed:
                continue
            final_root.mkdir(parents=True, exist_ok=True)
            target = final_root / candidate.name
            if target.exists():
                if is_complete_published_task(target):
                    shutil.rmtree(candidate)
                    continue
                shutil.rmtree(target)
            candidate.rename(target)


def _failure_text(failure: StageFailure | None, reports: list[ValidationReport]) -> str:
    lines: list[str] = []
    failure_message = str(failure) if failure else ""
    if failure:
        stage = failure.stage or "stage"
        # StageFailure 可能已直接封装某份报告全文（如 source-privacy），避免阶段前缀重复。
        if failure_message.startswith(f"{stage}: "):
            lines.append(failure_message)
        else:
            lines.append(f"{stage}: {failure_message}")
    # 与失败消息相同的报告不再重复追加，其余失败报告保留完整证据。
    lines.extend(report.text for report in reports if not report.passed and report.text != failure_message)
    return "\n\n".join(lines) or "未知失败，请重新检查当前草稿和原始科研材料。"


def _cleanup_validation_jobs(workspace: SourceWorkspace, config: PipelineConfig) -> None:
    from .hcluster_runtime import cleanup_logged_rjobs

    failures = []
    for log_dir in workspace.validation_dir.rglob("runtime-logs"):
        failures.extend(asyncio.run(cleanup_logged_rjobs(log_dir, config.namespace)))
    if failures:
        raise StageFailure("集群资源未确认关闭，不再启动新验证：" + "; ".join(failures), stage="infrastructure")


def _validation_step(operation, workspace: SourceWorkspace, config: PipelineConfig, stage: str):
    for attempt in range(3):
        try:
            remaining_timeout(1)
            return operation()
        except TaskTimeout:
            raise
        except StageFailure as exc:
            if exc.stage not in {"infrastructure", "proxy"}:
                raise
            _cleanup_validation_jobs(workspace, config)
            record_event(workspace, "validation-retry", stage=stage, attempt=attempt, error=str(exc))
            if attempt == 2:
                raise
            time.sleep(remaining_timeout((5, 15)[attempt]))


def _run_validation_cycle(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent, cycle: int) -> tuple[bool, StageFailure | None, list[ValidationReport]]:
    reports: list[ValidationReport] = []
    failure: StageFailure | None = None
    privacy = validate_public_description(workspace.draft_dir, source)
    reports.append(privacy)
    record_event(workspace, "source-privacy-validation", cycle=cycle, passed=privacy.passed)
    if not privacy.passed:
        failure = StageFailure(privacy.text, stage="source-privacy")
    else:
        try:
            run_reviewer_agent(source, config, workspace, agent, cycle=cycle)
            record_event(workspace, "reviewer-passed", cycle=cycle)
        except TaskTimeout:
            raise
        except StageFailure as exc:
            failure = exc
            record_event(workspace, "reviewer-failed", cycle=cycle, error=str(exc))
    if failure is None:
        try:
            harbor = _validation_step(lambda: validate_with_harbor(workspace.draft_dir, source, workspace, config), workspace, config, "harbor")
        except TaskTimeout:
            raise
        except StageFailure as exc:
            failure = exc
            harbor = ValidationReport("harbor", False, ())
        reports.append(harbor)
        record_event(workspace, "harbor-validation", cycle=cycle, passed=harbor.passed)
    return failure is None and bool(reports) and all(report.passed for report in reports), failure, reports


def _process_source(source: SourceBundle, config: PipelineConfig, run_root: Path, staging_output: Path) -> Path:
    seconds = getattr(config, "task_timeout_sec", None)
    try:
        if seconds is None:
            return _process_source_work(source, config, run_root, staging_output)
        with task_deadline(seconds):
            return _process_source_work(source, config, run_root, staging_output)
    except BaseException as error:
        source_root = run_root / "workspace" / "sources" / source.source_slug
        if isinstance(error, TaskTimeout):
            from .model_relay import redact_model_secrets

            failures_dir = source_root / "failures"
            failures_dir.mkdir(parents=True, exist_ok=True)
            payload = {"stage": "task-timeout", "limit_sec": seconds, "error": str(error)}
            for field in ("stdout", "stderr"):
                value = getattr(error, field, None)
                if value:
                    payload[field] = value.decode(errors="replace") if isinstance(value, bytes) else value
            (failures_dir / "task-timeout.json").write_text(redact_model_secrets(json.dumps(payload, ensure_ascii=False, indent=2), config), encoding="utf-8")
        if source_root.is_dir() and hasattr(config, "namespace"):
            from types import SimpleNamespace

            try:
                _cleanup_validation_jobs(SimpleNamespace(validation_dir=source_root / "validation"), config)
            except Exception as cleanup_error:
                print(f"{source.source_slug}: 清理未完成：{cleanup_error}", file=sys.stderr, flush=True)
        raise


def _process_source_work(source: SourceBundle, config: PipelineConfig, run_root: Path, staging_output: Path) -> Path:
    shared_dir = run_root / "workspace" / "shared"
    workspace = create_source_workspace(run_root, shared_dir, source)
    record_event(workspace, "source-started", source=str(source.source_dir), slug=source.source_slug)
    agent = IndependentClaudeAgent(claude_bin=config.claude_bin, timeout_sec=config.agent_timeout_sec)
    plan_reused, draft_reused = restore_source(source, workspace, config)
    if config.local_only:
        from .local_validation import validate_local_source

        report = validate_local_source(source, workspace, plan_reused=plan_reused, draft_reused=draft_reused)
        (workspace.validation_dir / "local-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return workspace.source_root
    if not plan_reused:
        run_planner_agent(source, config, workspace, agent)
    record_event(workspace, "planner-passed")
    if not draft_reused:
        run_writer_agent(source, config, workspace, agent)
    record_event(workspace, "writer-passed", cycle=0)

    revision_limit = config.max_revisions
    record_event(workspace, "revision-limit", revision_limit=revision_limit)
    cycle = 0
    while True:
        try:
            remaining_timeout(1)
            passed, failure, reports = _run_validation_cycle(source, config, workspace, agent, cycle)
        except TaskTimeout:
            raise
        except Exception as exc:
            passed = False
            failure = exc if isinstance(exc, StageFailure) else StageFailure(f"验证程序错误：{type(exc).__name__}: {exc}", stage="validation")
            reports = [ValidationReport("validation", False, (ValidationIssue("validation", str(failure), traceback.format_exc()),))]
        if isinstance(failure, TaskTimeout):
            raise failure
        if passed:
            published = publish_task(workspace.draft_dir, source, staging_output, config)
            record_event(workspace, "published", cycle=cycle, path=str(published))
            try:
                # staging_output = <output>/runs/<run-id>/published
                usage_path = write_source_usage(workspace.source_root, run_root.parent.parent, source.source_slug)
                record_event(workspace, "usage-recorded", path=str(usage_path))
            except (OSError, ValueError) as exc:
                # 用量统计不影响发布。
                record_event(workspace, "usage-record-failed", error=f"{type(exc).__name__}: {exc}")
            return published
        text = _failure_text(failure, reports)
        write_failure(workspace, cycle, failure.stage if failure else "validation", text)
        if failure and failure.stage in {"infrastructure", "proxy", "resume"}:
            raise failure
        if any(report.evidence.get("infrastructure_failure") for report in reports):
            raise StageFailure(text, stage="infrastructure")
        # 修复结果被程序拒绝（计划不合格、改动越界、缺文件）时草稿与计划保持原样，
        # 带着拒绝原因再修一次；每次修复尝试都计入修复次数。
        while True:
            if cycle >= revision_limit:
                raise StageFailure(f"来源 {source.source_slug} 超过最大修复次数 {revision_limit}（共 {revision_limit + 1} 次检查）:\n{text}", stage=failure.stage if failure else "validation")
            snapshot = snapshot_draft(workspace, cycle)
            record_event(workspace, "repair-started", cycle=cycle, snapshot=str(snapshot))
            try:
                run_repair_writer_agent(source, config, workspace, agent, text)
            except StageFailure as exc:
                if exc.stage != "repair-writer" or isinstance(exc.__cause__, AgentError):
                    raise
                cycle += 1
                text = f"{text}\n\n上一次修复未被接受（草稿与计划未改动），请据此重新修复：\n{exc}"
                write_failure(workspace, cycle, "repair-rejected", text)
                record_event(workspace, "repair-rejected", cycle=cycle, error=str(exc))
                continue
            record_event(workspace, "repair-writer-passed", cycle=cycle)
            cycle += 1
            break


def _format_source_failure(source: SourceBundle, error: Exception) -> str:
    if isinstance(error, StageFailure):
        return f"{source.source_slug}: {error}"
    return f"{source.source_slug}: {type(error).__name__}: {error}"


def _save_source_exception(source: SourceBundle, run_root: Path, error: Exception) -> None:
    root = run_root / "workspace" / "sources" / source.source_slug / "failures"
    root.mkdir(parents=True, exist_ok=True)
    payload = {"source": source.source_slug, "stage": getattr(error, "stage", None) or "unexpected", "exception": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
    (root / "source-exception.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _announce_source(source: SourceBundle, message: str, details: str = "") -> None:
    suffix = f"；{details}" if details else ""
    print(f"{message}：{source.source_slug}{suffix}", file=sys.stderr, flush=True)


def _run_sources_sequential(
    sources: list[SourceBundle],
    config: PipelineConfig,
    run_root: Path,
    staging_output: Path,
) -> tuple[list[Path], list[str]]:
    outputs: list[Path] = []
    failures: list[str] = []
    for source in sources:
        _announce_source(source, "开始处理")
        try:
            outputs.append(_process_source(source, config, run_root, staging_output))
        except Exception as exc:
            _save_source_exception(source, run_root, exc)
            failure = _format_source_failure(source, exc)
            failures.append(failure)
            _announce_source(source, "处理失败", str(exc))
        else:
            _announce_source(source, "处理完成")
    return outputs, failures


def _run_sources_parallel(
    sources: list[SourceBundle],
    config: PipelineConfig,
    run_root: Path,
    staging_output: Path,
) -> tuple[list[Path], list[str]]:
    worker_count = min(config.concurrency, len(sources))
    ordered_outputs: list[Path | None] = [None] * len(sources)
    failures_by_index: dict[int, str] = {}
    pending = {}
    next_index = 0

    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        def submit_next() -> None:
            nonlocal next_index
            source_index = next_index
            next_index += 1
            source = sources[source_index]
            _announce_source(source, "开始处理")
            try:
                future = executor.submit(_process_source, source, config, run_root, staging_output)
            except Exception as exc:
                _save_source_exception(source, run_root, exc)
                failure = _format_source_failure(source, exc)
                failures_by_index[source_index] = failure
                _announce_source(source, "处理失败")
                print(f"{failure}", file=sys.stderr, flush=True)
                return
            pending[future] = source_index

        while next_index < len(sources) and len(pending) < worker_count:
            submit_next()

        while pending:
            completed, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in completed:
                source_index = pending.pop(future)
                source = sources[source_index]
                try:
                    ordered_outputs[source_index] = future.result()
                except Exception as exc:
                    _save_source_exception(source, run_root, exc)
                    failure = _format_source_failure(source, exc)
                    failures_by_index[source_index] = failure
                    _announce_source(source, "处理失败", str(exc))
                else:
                    _announce_source(source, "处理完成")
                while next_index < len(sources) and len(pending) < worker_count:
                    submit_next()

    outputs = [output for output in ordered_outputs if output is not None]
    failures = [failures_by_index[index] for index in sorted(failures_by_index)]
    return outputs, failures


def _run_sources(
    sources: list[SourceBundle],
    config: PipelineConfig,
    run_root: Path,
    staging_output: Path,
) -> tuple[list[Path], list[str]]:
    if config.concurrency == 1:
        return _run_sources_sequential(sources, config, run_root, staging_output)
    return _run_sources_parallel(sources, config, run_root, staging_output)


def run_pipeline(config: PipelineConfig) -> list[Path]:
    if config.run_txt:
        candidates = discover_sources(config.input_root, validate=False)
        source_by_name = {source.source_dir.name: source for source in candidates}
        all_sources = []
        for name in read_run_names(config.run_txt):
            if name not in source_by_name:
                print(f"任务名单中的名字不存在，已跳过: {name}", file=sys.stderr, flush=True)
            else:
                all_sources.append(source_by_name[name])
    else:
        all_sources = discover_sources(config.input_root)
    if config.resume_from is None and not config.local_only:
        migrate_staged_published_tasks(config.output_root, all_sources)
    complete = set() if not config.run_txt and (config.resume_from or config.local_only) else published_source_slugs(config.output_root)
    sources = [source for source in all_sources if source.source_slug not in complete]
    if not sources:
        if config.run_txt:
            print("没有待运行任务", flush=True)
        return []
    if config.model_relay and not config.local_only:
        from .model_relay import prepare_model_relay

        prepare_model_relay(config)
    if config.run_txt:
        for source in sources:
            validate_source(source)
    run_root = new_run_root(config.output_root)
    staging_output = run_root / "published"
    staging_output.mkdir(parents=True, exist_ok=True)
    create_shared_workspace(run_root, config)
    save_run_identity(run_root, config)
    outputs, failures = _run_sources(sources, config, run_root, staging_output)
    if config.local_only:
        (run_root / "local-summary.json").write_text(json.dumps({"sources": [str(path) for path in outputs], "failures": failures, "remote_validated": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        if failures:
            raise StageFailure("本地恢复失败:\n" + "\n".join(failures), stage="resume")
        return outputs
    if outputs:
        final_root = config.output_root / "final"
        final_root.mkdir(parents=True, exist_ok=True)
        for staged in outputs:
            target = final_root / staged.name
            backup = run_root / f"previous-{staged.name}"
            moved_old = False
            try:
                if target.exists():
                    target.rename(backup)
                    moved_old = True
                staged.rename(target)
            except OSError:
                if moved_old and backup.exists() and not target.exists():
                    backup.rename(target)
                raise
            if backup.exists():
                shutil.rmtree(backup)
    if failures:
        raise StageFailure("部分来源未发布:\n" + "\n".join(failures), stage="pipeline")
    return [config.output_root / "final" / source.source_slug for source in sources if source.source_slug in {path.name for path in outputs}]
