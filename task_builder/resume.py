from __future__ import annotations

import json
import shutil
from pathlib import Path

from .integrity import content_manifest
from .artifact_history import EXPORT_STATE, copy_history, latest_repair_candidate
from .models import PipelineConfig, SourceBundle, StageFailure
from .workspace import SourceWorkspace, record_event

# 环境定义仅用于记录，不作为恢复条件。
_FINGERPRINT_FILES = ("Dockerfile.experiment", "Dockerfile.evaluation", "requirements-experiment.txt", "requirements-evaluation.txt")


def environment_fingerprint(shared_dir: Path) -> dict[str, dict[str, object]]:
    """记录共享环境定义文件的内容。"""
    fingerprint: dict[str, dict[str, object]] = {}
    for env_dir in sorted(path for path in shared_dir.iterdir() if path.is_dir()) if shared_dir.is_dir() else ():
        for name in _FINGERPRINT_FILES:
            if (env_dir / name).is_file():
                fingerprint[name] = content_manifest(env_dir / name)["."]
    return fingerprint


def save_run_identity(run_root: Path, config: PipelineConfig) -> None:
    payload = {
        "shared": content_manifest(run_root / "workspace" / "shared"),
        "environment": environment_fingerprint(run_root / "workspace" / "shared"),
        "image_env": content_manifest(config.image_env_path),
    }
    (run_root / "identity.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_source(source: SourceBundle, workspace: SourceWorkspace, config: PipelineConfig, *, restore_draft: bool = True) -> tuple[bool, bool]:
    if config.resume_from is None:
        return False, False
    old_run = config.resume_from
    old = old_run / "workspace" / "sources" / source.source_slug
    if not old.is_dir():
        record_event(workspace, "resume-source-missing")
        return False, False
    evidence = workspace.source_root / "previous-evidence"
    for name in ("logs", "failures", "reviews"):
        if (old / name).is_dir():
            copy_history(old / name, evidence / name, logical=(name,))
    if (old / "previous-evidence").is_dir():
        copy_history(old / "previous-evidence", evidence / "previous-evidence", logical=("previous-evidence",))
    validation = old / "validation"
    if validation.is_dir():
        for path in validation.rglob("*"):
            if path.is_file() and "data" not in path.relative_to(validation).parts and path.suffix in {".json", ".log", ".txt", ".jsonl"}:
                target = evidence / "validation" / path.relative_to(validation)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    export_state = old / EXPORT_STATE
    partial = export_state.is_file() and json.loads(export_state.read_text()).get("checkpoint", False)
    current = old
    saved_candidate = latest_repair_candidate(old)
    if partial and (old / "draft").is_dir():
        copy_history(old / "draft", evidence / "checkpoint-draft", compact_all=True)
    if saved_candidate is not None:
        copy_history(saved_candidate, evidence / "interrupted-candidate", compact_all=True)
    candidate = None if partial or not restore_draft else saved_candidate
    required = ("task.toml", "instruction.md", "solution/solve.sh")
    if candidate is not None and all((candidate / "draft" / name).is_file() for name in required):
        try:
            candidate_plan = json.loads((candidate / "plan/plan.json").read_text())
        except (OSError, ValueError):
            candidate_plan = None
        if isinstance(candidate_plan, dict) and any((candidate / "draft/tests" / name).is_file() for name in ("Dockerfile", "test.sh")):
            current = candidate
            record_event(workspace, "interrupted-repair-reused", path=str(candidate), old_scores_trusted=False)
    plan_ok = False
    plan_path = current / "plan" / "plan.json"
    if plan_path.is_file():
        shutil.copytree(current / "plan", workspace.plan_dir, dirs_exist_ok=True)
        plan_ok = True
        record_event(workspace, "plan-reused")
    draft_ok = (restore_draft and not partial and plan_ok and all((current / "draft" / name).is_file() for name in required)
                and any((current / "draft/tests" / name).is_file() for name in ("Dockerfile", "test.sh")))
    if draft_ok:
        shutil.copytree(current / "draft", workspace.draft_dir, dirs_exist_ok=True)
        record_event(workspace, "draft-reused", old_scores_trusted=False, repair_count=0)
    return plan_ok, draft_ok
