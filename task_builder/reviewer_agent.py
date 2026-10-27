from __future__ import annotations

import json
from pathlib import Path

from .agents import IndependentClaudeAgent, run_agent_expecting_json, save_agent_log
from .models import AgentError, PipelineConfig, SourceBundle, StageFailure
from .staging import validate_task_config, validate_task_metadata
from .prompting import draft_manifest, load_prompt, read_shared_material, source_manifest, task_data_manifest
from .workspace import SourceWorkspace, directory_manifest, directories_unchanged


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


def _validate_contract(result: dict) -> None:
    if not isinstance(result.get("pass"), bool):
        raise StageFailure("Reviewer JSON 缺少布尔值 pass", stage="reviewer")
    issues = result.get("blocking_issues")
    warnings = result.get("warnings")
    if not isinstance(issues, list) or not isinstance(warnings, list) or not isinstance(result.get("summary"), str):
        raise StageFailure("Reviewer JSON 不符合结构化审查格式", stage="reviewer")
    for issue in issues:
        if not isinstance(issue, dict) or not all(isinstance(issue.get(key), expected) for key, expected in (("criterion", str), ("problem", str), ("evidence", list), ("required_fix", str))):
            raise StageFailure("Reviewer 的 blocking_issues 必须是包含 criterion/problem/evidence/required_fix 的对象数组", stage="reviewer")
        if not all(isinstance(item, str) for item in issue["evidence"]):
            raise StageFailure("Reviewer 的 blocking_issues.evidence 必须是字符串数组", stage="reviewer")
    if result["pass"] and issues:
        raise StageFailure("Reviewer JSON 标记通过但仍有 blocking_issues", stage="reviewer")
    if not result["pass"] and not issues:
        raise StageFailure("Reviewer JSON 未通过但没有 blocking_issues", stage="reviewer")


def run_reviewer_agent(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent, *, cycle: int) -> dict:
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
        environment=environment,
        environment_dir=workspace.shared_dir / environment_dir,
        source_manifest=source_manifest(workspace.input_dir),
        draft_manifest=draft_manifest(workspace.draft_dir),
        task_data_manifest=task_data_manifest(workspace.input_dir / "data", workspace.draft_dir, workspace.plan_dir / "plan.json"),
    )
    protected = {
        workspace.input_dir: directory_manifest(workspace.input_dir),
        workspace.plan_dir: directory_manifest(workspace.plan_dir),
        workspace.draft_dir: directory_manifest(workspace.draft_dir),
        workspace.shared_dir: directory_manifest(workspace.shared_dir),
        view: directory_manifest(view),
    }
    run_options = {
        "workdir": view,
        "env_config": config.author_env,
        "allowed_dirs": (workspace.input_dir, workspace.plan_dir, workspace.draft_dir, workspace.shared_dir),
        "read_only_dirs": (workspace.input_dir, workspace.plan_dir, workspace.draft_dir, workspace.shared_dir),
    }
    try:
        _, result = run_agent_expecting_json(
            agent,
            role=f"reviewer-cycle-{cycle}",
            prompt=prompt,
            log_path=workspace.logs_dir / f"reviewer-cycle-{cycle:03d}.log",
            **run_options,
        )
    except AgentError as exc:
        if hasattr(exc, "run"):
            save_agent_log(workspace.logs_dir / f"reviewer-cycle-{cycle:03d}.log", exc.run)
        raise StageFailure(str(exc), stage="reviewer") from exc
    if not directories_unchanged(protected):
        raise StageFailure("Reviewer Agent 修改了只读资料或在审查目录中生成了文件", stage="reviewer")
    _validate_contract(result)
    path = workspace.reviews_dir / f"review-cycle-{cycle:03d}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result["pass"]:
        issues = result["blocking_issues"]
        details = "；".join(
            f"{item['criterion']}: {item['problem']}；证据={item['evidence']}；修复={item['required_fix']}"
            for item in issues
        )
        raise StageFailure("Reviewer Agent 未通过（共 " + str(len(issues)) + " 项，详见 reviews/）: " + details, stage="reviewer")
    return result
