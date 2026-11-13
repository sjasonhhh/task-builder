from __future__ import annotations

import json
import re
from pathlib import Path

from .agents import IndependentClaudeAgent, extract_json, save_agent_log
from .models import AgentError, PipelineConfig, SourceBundle, StageFailure
from .staging import validate_task_config, validate_task_metadata
from .prompting import draft_manifest, load_prompt, read_shared_material, source_manifest, task_data_manifest
from .workspace import SourceWorkspace


def _review_view(workspace: SourceWorkspace, cycle: int) -> Path:
    view = workspace.reviewer_dir / f"cycle-{cycle:03d}"
    if view.exists():
        for path in view.iterdir():
            if path.is_dir() and not path.is_symlink():
                import shutil

                shutil.rmtree(path)
            else:
                path.unlink()
    view.mkdir(parents=True, exist_ok=True)
    return view


def _review_response(text: str) -> dict:
    """读取审核结论，保留意见，不以 JSON 字段的形状拒绝审核。"""
    try:
        result = extract_json(text)
    except AgentError:
        result = {"summary": text}
    decision = result.get("pass")
    if not isinstance(decision, bool):
        value = str(decision).strip().lower()
        if value in {"true", "通过", "pass", "passed"}:
            decision = True
        elif value in {"false", "不通过", "未通过", "fail", "failed", "需要修改"}:
            decision = False
        else:
            # 自然语言意见也可使用；只接受明确的结论，不把“没有问题字段”当通过。
            summary = str(result.get("summary", text)).replace("**", "").strip()
            match = re.search(r"(?:^|\n)\s*(?:(?:审核结论|最终结论|结论|状态)\s*[:：]\s*)?(通过|不通过|未通过|需要修改|PASS|FAIL)\s*[。.!！]?\s*$", summary, re.I)
            decision = bool(match and match.group(1).lower() in {"通过", "pass"})
    result["pass"] = decision
    return result


def run_reviewer_agent(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent, *, cycle: int) -> dict:
    from .writer_agent import _check_draft
    _check_draft(workspace.draft_dir, stage="reviewer")
    validate_task_config(workspace.draft_dir / "task.toml")
    validate_task_metadata(workspace.draft_dir / "task.toml")
    view = _review_view(workspace, cycle)
    rubric, environment_dir, environment = read_shared_material(workspace.shared_dir)
    prompt = load_prompt(
        "reviewer.md",
        draft=workspace.draft_dir,
        context=workspace.input_dir / "context",
        data=workspace.input_dir / "data",
        code=workspace.input_dir / "code",
        plan_path=workspace.plan_dir / "plan.json",
        rubric=rubric,
        shared_dir=workspace.shared_dir,
        human_feedback_path=workspace.reviews_dir / "human" / "request.md",
        environment=environment,
        environment_dir=workspace.shared_dir / environment_dir,
        source_manifest=source_manifest(workspace.input_dir),
        draft_manifest=draft_manifest(workspace.draft_dir),
        task_data_manifest=task_data_manifest(workspace.input_dir / "data", workspace.draft_dir, workspace.plan_dir / "plan.json"),
    )
    run_options = {
        "workdir": view,
        "env_config": config.author_env,
        "allowed_dirs": (workspace.input_dir, workspace.plan_dir, workspace.draft_dir, workspace.shared_dir),
        "read_only_dirs": (workspace.input_dir, workspace.plan_dir, workspace.draft_dir, workspace.shared_dir),
    }
    try:
        run = agent.run(
            role=f"reviewer-cycle-{cycle}",
            prompt=prompt,
            **run_options,
        )
        save_agent_log(workspace.logs_dir / f"reviewer-cycle-{cycle:03d}.log", run)
        result = _review_response(run.stdout)
    except AgentError as exc:
        if hasattr(exc, "run"):
            save_agent_log(workspace.logs_dir / f"reviewer-cycle-{cycle:03d}.log", exc.run)
        raise StageFailure(str(exc), stage="reviewer") from exc
    path = workspace.reviews_dir / f"review-cycle-{cycle:03d}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result["pass"]:
        details = json.dumps(result, ensure_ascii=False)
        raise StageFailure("Reviewer Agent 未通过，审核意见（详见 reviews/）: " + details, stage="reviewer")
    return result
