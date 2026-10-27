from __future__ import annotations

import json
import shutil
from pathlib import Path

from .integrity import content_manifest, manifest_differences
from .models import PipelineConfig, SourceBundle, StageFailure
from .planner_agent import migrate_plan_meta
from .workspace import SourceWorkspace, directory_manifest, record_event

# 续跑只比对决定运行环境的文件；README.md 只是说明，image-env.toml 另由 image_env 指纹比对。
_FINGERPRINT_FILES = ("Dockerfile.experiment", "Dockerfile.evaluation", "requirements-experiment.txt", "requirements-evaluation.txt")


def environment_fingerprint(shared_dir: Path) -> dict[str, dict[str, object]]:
    """续跑只比对共享环境中决定运行环境的文件内容（按文件名），不看环境目录名、规则文件或说明文件。"""
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


def restore_source(source: SourceBundle, workspace: SourceWorkspace, config: PipelineConfig) -> tuple[bool, bool]:
    if config.resume_from is None:
        return False, False
    old_run = config.resume_from
    old = old_run / "workspace" / "sources" / source.source_slug
    if not old.is_dir():
        record_event(workspace, "resume-source-missing")
        return False, False
    old_shared = old_run / "workspace" / "shared"
    current = environment_fingerprint(workspace.shared_dir)
    differences = manifest_differences(environment_fingerprint(old_shared), current) if old_shared.is_dir() else ["shared/"]
    if differences or set(current) != set(_FINGERPRINT_FILES):
        raise StageFailure("共享环境文件与旧运行不一致，拒绝续跑: " + ", ".join(differences or ["当前环境文件不完整"]), stage="resume")
    differences = manifest_differences(directory_manifest(old / "input"), directory_manifest(workspace.input_dir))
    if differences:
        raise StageFailure("原始输入不匹配，拒绝复用旧计划和草稿: " + ", ".join(differences), stage="resume")
    identity = old_run / "identity.json"
    if identity.is_file():
        saved = json.loads(identity.read_text(encoding="utf-8"))
        if saved.get("image_env") != content_manifest(config.image_env_path):
            raise StageFailure("镜像/集群配置与旧运行不一致，拒绝复用", stage="resume")
    else:
        old_tasks = list((old / "draft").glob("task.toml")) + list((old / "validation").glob("*/task.toml"))
        for task in old_tasks:
            import tomli

            environment = tomli.loads(task.read_text(encoding="utf-8")).get("environment", {})
            image = environment.get("docker_image")
            if image and image != config.experiment_image:
                raise StageFailure("旧草稿使用的实验镜像与当前配置不同", stage="resume")
        record_event(workspace, "legacy-identity-checked", note="旧运行无配置指纹；已核对完整输入、共享环境和可用镜像记录")
    evidence = workspace.source_root / "previous-evidence"
    for name in ("logs", "failures", "reviews"):
        if (old / name).is_dir():
            shutil.copytree(old / name, evidence / name, dirs_exist_ok=True)
    validation = old / "validation"
    if validation.is_dir():
        for path in validation.rglob("*"):
            if path.is_file() and "data" not in path.relative_to(validation).parts and path.suffix in {".json", ".log", ".txt", ".jsonl"}:
                target = evidence / "validation" / path.relative_to(validation)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    plan_ok = False
    plan_path = old / "plan" / "plan.json"
    if plan_path.is_file():
        shutil.copytree(old / "plan", workspace.plan_dir, dirs_exist_ok=True)
        try:
            original = json.loads(plan_path.read_text(encoding="utf-8"))
            migrated = migrate_plan_meta(original, data_root=workspace.input_dir / "data")
            (workspace.plan_dir / "plan.json").write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
            plan_ok = True
            record_event(workspace, "plan-reused", meta_migrated="meta" in original)
        except (StageFailure, ValueError) as exc:
            record_event(workspace, "old-plan-invalid", error=str(exc))
            shutil.copy2(plan_path, workspace.failures_dir / "old-invalid-plan.json")
            (workspace.plan_dir / "plan.json").unlink()
    required = ("task.toml", "instruction.md", "solution/solve.sh", "tests/test.sh")
    draft_ok = plan_ok and all((old / "draft" / name).is_file() for name in required)
    if draft_ok:
        shutil.copytree(old / "draft", workspace.draft_dir, dirs_exist_ok=True)
        record_event(workspace, "draft-reused", old_scores_trusted=False, repair_count=0)
    return plan_ok, draft_ok
