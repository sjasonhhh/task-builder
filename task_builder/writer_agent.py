from __future__ import annotations

import shutil
import stat
import uuid
from pathlib import Path

from .agents import IndependentClaudeAgent, save_agent_log
import json

from .models import AgentError, PipelineConfig, SourceBundle, StageFailure
from .prompting import load_prompt, read_shared_material, source_manifest
from .planner_agent import validate_plan
from .workspace import SourceWorkspace, directory_manifest, directories_unchanged, record_event


# solution/solve.sh 是迁移自论文结果或代码的参考提交。
_REQUIRED_FILES = ("task.toml", "instruction.md", "README.md", "solution/solve.sh", "tests/test.sh")
_MISSING_FILE_ATTEMPTS = 2


def _missing_files(draft: Path, required: tuple[str, ...] = _REQUIRED_FILES) -> list[str]:
    return [relative for relative in required if not (draft / relative).is_file()]


def _run_until_files_written(agent: IndependentClaudeAgent, draft: Path, log_path: Path, *, required: tuple[str, ...] = _REQUIRED_FILES, **options) -> None:
    """模型有时把文件内容只写在思考里就结束会话；缺文件时在原会话中要求它直接写出。"""
    run = agent.run(**options)
    save_agent_log(log_path, run)
    for attempt in range(1, _MISSING_FILE_ATTEMPTS + 1):
        missing = _missing_files(draft, required)
        if not missing:
            return
        prompt = (
            f"任务草稿 {draft} 中以下必需文件仍不存在：" + "、".join(missing)
            + "。你上一轮没有调用工具保存。不要重新阅读材料或重新设计，立即用 Write 工具逐个写出这些文件的完整内容"
            + "（相对路径基于草稿目录），全部保存后用一句话说明。"
        )
        run = agent.run(**{**options, "role": f"{options['role']}-files-{attempt}", "prompt": prompt, "resume_session": run.session_id})
        save_agent_log(log_path.with_name(f"{log_path.stem}-files-{attempt}.log"), run)


def _check_draft(draft: Path, *, stage: str = "writer", required: tuple[str, ...] = _REQUIRED_FILES) -> None:
    missing = _missing_files(draft, required)
    if missing:
        raise StageFailure("任务草稿缺少文件: " + ", ".join(missing), stage=stage)
    for script in (draft / "solution" / "solve.sh", draft / "tests" / "test.sh"):
        if not script.is_file():
            continue
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _replace_draft(candidate: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.new-{uuid.uuid4().hex}"
    backup = target.parent / f".{target.name}.old-{uuid.uuid4().hex}"
    moved_old = False
    try:
        shutil.copytree(candidate, staging)
        if target.exists():
            target.replace(backup)
            moved_old = True
        try:
            staging.replace(target)
        except OSError:
            if moved_old and backup.exists() and not target.exists():
                backup.replace(target)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if backup.exists():
        shutil.rmtree(backup)


def run_writer_agent(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent) -> Path:
    (workspace.draft_dir / "solution").mkdir(parents=True, exist_ok=True)
    (workspace.draft_dir / "tests").mkdir(parents=True, exist_ok=True)
    # Writer 按计划准备数据，不自动加入未声明的来源文件。
    (workspace.draft_dir / "data").mkdir(parents=True, exist_ok=True)
    rubric, environment_dir, environment = read_shared_material(workspace.shared_dir)
    prompt = load_prompt(
        "writer.md",
        draft=workspace.draft_dir,
        context=workspace.input_dir / "context",
        data=workspace.input_dir / "data",
        code=workspace.input_dir / "code",
        plan_path=workspace.plan_dir / "plan.json",
        environment_dir=workspace.shared_dir / environment_dir,
        rubric=rubric,
        environment=environment,
        source_manifest=source_manifest(workspace.input_dir),
    )
    required = _REQUIRED_FILES
    # input/ 不再受保护（Writer 需用 Bash 从中复制数据）；shared/ 不在访问范围内，这里再核对一次兜底。
    protected = {
        workspace.plan_dir: directory_manifest(workspace.plan_dir),
        workspace.shared_dir: directory_manifest(workspace.shared_dir),
    }
    try:
        _run_until_files_written(
            agent,
            workspace.draft_dir,
            workspace.logs_dir / "writer-agent.log",
            required=required,
            role="writer",
            prompt=prompt,
            workdir=workspace.draft_dir,
            env_config=config.author_env,
            # shared/ 的内容已全部写进提示词，不交给 agent：不在访问范围内，Claude Code 会拒绝任何写入。
            allowed_dirs=(workspace.input_dir, workspace.plan_dir),
            read_only_dirs=(workspace.plan_dir,),
        )
    except AgentError as exc:
        if hasattr(exc, "run"):
            save_agent_log(workspace.logs_dir / "writer-agent.log", exc.run)
        raise StageFailure(str(exc), stage="writer") from exc
    if not directories_unchanged(protected):
        raise StageFailure("Writer Agent 修改了计划或共享资料", stage="writer")
    _check_draft(workspace.draft_dir, required=required)
    return workspace.draft_dir


def _failure_evidence(workspace: SourceWorkspace, current_failure: str) -> str:
    parts = ["当前失败摘要：\n" + current_failure]
    roots = (workspace.failures_dir, workspace.reviews_dir, workspace.validation_dir, workspace.logs_dir)
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name.endswith(".log") or path.suffix in {".txt", ".json"}:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if len(text) > 12000:
                    text = text[-12000:]
                parts.append(f"\n===== {path} =====\n{text}")
    return "\n".join(parts)[-60000:]


def _accept_repaired_plan(candidate_plan: Path, workspace: SourceWorkspace) -> None:
    """校验修复后的计划；通过后替换正式计划，未改动时保持原样。"""
    extra = sorted(path.name for path in candidate_plan.iterdir() if path.name != "plan.json")
    if extra:
        raise StageFailure("Repair Writer Agent 在计划目录中生成了 plan.json 之外的文件: " + ", ".join(extra), stage="repair-writer")
    new_path, old_path = candidate_plan / "plan.json", workspace.plan_dir / "plan.json"
    if new_path.read_bytes() == old_path.read_bytes():
        return
    try:
        plan = json.loads(new_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageFailure(f"修复后的 plan.json 不是有效 JSON: {exc}", stage="repair-writer") from exc
    validate_plan(plan, data_root=workspace.input_dir / "data")
    revision = len(list(workspace.failures_dir.glob("plan-before-repair-*.json")))
    shutil.copy2(old_path, workspace.failures_dir / f"plan-before-repair-{revision:03d}.json")
    old_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    record_event(workspace, "plan-repaired", revision=revision)


def run_repair_writer_agent(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent, failure: str) -> Path:
    rubric, environment_dir, environment = read_shared_material(workspace.shared_dir)
    # 修复只操作候选计划，校验通过后再替换正式计划。
    # input/ 不再受保护；shared/ 不在访问范围内，这里再核对一次兜底。
    protected = {
        workspace.shared_dir: directory_manifest(workspace.shared_dir),
        workspace.plan_dir: directory_manifest(workspace.plan_dir),
    }
    failure_path = workspace.failures_dir / "current-failure.txt"
    failure_path.write_text(failure, encoding="utf-8")
    evidence = _failure_evidence(workspace, failure)
    candidate_root = workspace.source_root / ".repair-candidates" / uuid.uuid4().hex
    candidate_draft = candidate_root / "draft"
    candidate_plan = candidate_root / "plan"
    candidate_draft.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(workspace.draft_dir, candidate_draft)
        shutil.copytree(workspace.plan_dir, candidate_plan)
    except BaseException:
        shutil.rmtree(candidate_root, ignore_errors=True)
        candidates_parent = candidate_root.parent
        if candidates_parent.exists() and not any(candidates_parent.iterdir()):
            candidates_parent.rmdir()
        raise
    prompt = load_prompt(
        "repair.md",
        draft=candidate_draft,
        context=workspace.input_dir / "context",
        data=workspace.input_dir / "data",
        code=workspace.input_dir / "code",
        plan_path=candidate_plan / "plan.json",
        environment_dir=workspace.shared_dir / environment_dir,
        rubric=rubric,
        environment=environment,
        source_manifest=source_manifest(workspace.input_dir),
        failure=evidence,
    )
    plan_dir = candidate_plan
    required = _REQUIRED_FILES
    try:
        try:
            _run_until_files_written(
                agent,
                candidate_draft,
                workspace.logs_dir / "repair-writer-agent.log",
                required=required,
                role="repair-writer",
                prompt=prompt,
                workdir=candidate_draft,
                env_config=config.author_env,
                allowed_dirs=(workspace.input_dir, plan_dir),
                read_only_dirs=(),
            )
        except AgentError as exc:
            if hasattr(exc, "run"):
                save_agent_log(workspace.logs_dir / "repair-writer-agent.log", exc.run)
            raise StageFailure(str(exc), stage="repair-writer") from exc
        if not directories_unchanged(protected):
            raise StageFailure("Repair Writer Agent 修改了正式计划或共享资料", stage="repair-writer")
        _check_draft(candidate_draft, stage="repair-writer", required=required)
        _accept_repaired_plan(candidate_plan, workspace)
        _replace_draft(candidate_draft, workspace.draft_dir)
    finally:
        shutil.rmtree(candidate_root, ignore_errors=True)
        candidates_parent = candidate_root.parent
        if candidates_parent.exists() and not any(candidates_parent.iterdir()):
            candidates_parent.rmdir()
    return workspace.draft_dir
