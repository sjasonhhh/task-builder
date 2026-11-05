from __future__ import annotations

import json
from pathlib import Path

from .agents import IndependentClaudeAgent, extract_json, save_agent_log
from .models import AgentError, PipelineConfig, SourceBundle, StageFailure, TaskTimeout
from .prompting import load_prompt, read_shared_material, source_manifest
from .workspace import SourceWorkspace, record_event


def _format_fix_prompt(plan_path: Path, format_error: str) -> str:
    return (
        f"{plan_path} 尚未保存：{format_error}。不要重新阅读材料，不要改动科学内容；"
        "直接保存完整计划，不要修改已有科学内容。"
        "保留所有说明字段。"
    )


def _planner_attempt(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent, *, attempt: int, format_error: str, resume_session: str | None = None) -> Path:
    plan_path = workspace.plan_dir / "plan.json"
    rubric, environment_dir, environment = read_shared_material(workspace.shared_dir)
    resume_prompt = (
        f"继续原会话完成任务方案 {plan_path}。读取规则已经更新，文本数据可以用 Read 的 offset、limit 分段预览。"
        "保留已确认的科学内容与证据，只补充尚缺的必要信息，不要重新遍历已经读过的论文和源码。"
        "先确认计划文件是否已存在；不存在则用 Write 创建，存在则先 Read 再完善。"
        "仍须遵守原会话的完整任务规则，只允许写入这个计划文件。完成后保存完整计划并简短说明。"
    )
    prompt = (_format_fix_prompt(plan_path, format_error) if format_error else resume_prompt) if resume_session else load_prompt(
        "planner.md",
        plan=plan_path,
        context=workspace.input_dir / "context",
        data=workspace.input_dir / "data",
        code=workspace.input_dir / "code",
        rubric=rubric,
        shared_dir=workspace.shared_dir,
        human_feedback_path=workspace.reviews_dir / "human" / "request.md",
        environment=environment,
        environment_dir=workspace.shared_dir / environment_dir,
        source_manifest=source_manifest(workspace.input_dir),
    )
    if format_error and not resume_session:
        prompt += f"\n上次计划未保存成功：{format_error}。保留科学内容和说明字段，保存可读取的 JSON。"
    try:
        run = agent.run(
            role=f"planner-format-{attempt}" if attempt else "planner",
            prompt=prompt,
            workdir=workspace.plan_dir,
            env_config=config.author_env,
            allowed_dirs=(workspace.input_dir, workspace.shared_dir),
            read_only_dirs=(workspace.input_dir, workspace.shared_dir),
            resume_session=resume_session,
        )
        save_agent_log(workspace.logs_dir / f"planner-attempt-{attempt:03d}.log", run)
    except AgentError as exc:
        if hasattr(exc, "run"):
            save_agent_log(workspace.logs_dir / f"planner-attempt-{attempt:03d}.log", exc.run)
        raise StageFailure(str(exc), stage="planner") from exc
    try:
        return _check_plan_output(workspace, run.stdout)
    except StageFailure as exc:
        exc.session_id = run.session_id
        raise


def _check_plan_output(workspace: SourceWorkspace, stdout: str) -> Path:
    plan_path = workspace.plan_dir / "plan.json"
    if not plan_path.exists():
        try:
            plan_from_output = extract_json(stdout)
        except AgentError:
            plan_from_output = None
        if isinstance(plan_from_output, dict):
            plan_path.write_text(json.dumps(plan_from_output, ensure_ascii=False, indent=2), encoding="utf-8")
    if not plan_path.is_file():
        raise StageFailure("Planner Agent 未生成 plan/plan.json", stage="planner")
    return plan_path


def run_planner_agent(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent) -> Path:
    # 修复次数为 0 也必须执行一次初始 Planner；max_revisions=N 时最多 N+1 次尝试。
    attempt_limit = max(1, config.max_revisions + 1)
    format_error = ""
    resume_session = config.extra.get("planner_resume_session")
    if resume_session:
        record_event(workspace, "planner-session-resumed", session_id=resume_session, reason="read-policy-update")
    for attempt in range(attempt_limit):
        try:
            return _planner_attempt(source, config, workspace, agent, attempt=attempt, format_error=format_error, resume_session=resume_session)
        except TaskTimeout:
            raise
        except StageFailure as exc:
            path = workspace.plan_dir / "plan.json"
            if path.is_file():
                (workspace.failures_dir / f"planner-attempt-{attempt:03d}.json").write_bytes(path.read_bytes())
            (workspace.failures_dir / f"planner-attempt-{attempt:03d}.txt").write_text(str(exc), encoding="utf-8")
            last_attempt = attempt == attempt_limit - 1
            record_event(
                workspace,
                "planner-failed",
                attempt=attempt,
                attempt_limit=attempt_limit,
                last_attempt=last_attempt,
                error=str(exc),
            )
            if exc.__cause__ is not None and isinstance(exc.__cause__, AgentError):
                raise
            if last_attempt:
                raise
            format_error = str(exc)
            # 文件尚未保存时继续原会话，不重复阅读来源材料。
            resume_session = getattr(exc, "session_id", None)
    raise AssertionError("unreachable")
