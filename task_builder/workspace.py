from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .models import PipelineConfig, SourceBundle
from .integrity import file_digest
from .artifact_history import copy_history
from .paths import ENV_IMAGE_CONFIG_NAME, SCIENV_IMAGE_ROOT

# 共享环境 image/<env>/ 必须包含的文件；这些文件会复制进运行目录 shared/<env>/，并全文写入 agent 提示词。
ENVIRONMENT_FILES = (
    "Dockerfile.experiment",
    "Dockerfile.evaluation",
    "requirements-experiment.txt",
    "requirements-evaluation.txt",
    "README.md",
    ENV_IMAGE_CONFIG_NAME,
)


@dataclass(frozen=True)
class SourceWorkspace:
    run_root: Path
    workspace_root: Path
    shared_dir: Path
    source_root: Path
    input_dir: Path
    plan_dir: Path
    draft_dir: Path
    reviews_dir: Path
    reviewer_dir: Path
    validation_dir: Path
    failures_dir: Path
    logs_dir: Path
    events: Path


def new_run_root(output_root: Path) -> Path:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    workspace_root = output_root / "runs" / run_id / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=False)
    return workspace_root.parent


def complete_environments(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return [path.name for path in sorted(root.iterdir()) if path.is_dir()]


def resolve_environment_dir(name: str, root: Path = SCIENV_IMAGE_ROOT) -> Path:
    """按 --sci-env 取共享环境目录 root/<name>/，读取已有材料。"""
    available = "、".join(complete_environments(root)) or "无"
    directory = root / name
    if not directory.is_dir():
        raise ValueError(f"共享环境目录不存在: {directory}；{root} 下可用的环境: {available}")
    return directory


def create_shared_workspace(run_root: Path, config: PipelineConfig) -> Path:
    workspace_root = run_root / "workspace"
    shared_dir = workspace_root / "shared"
    environment_source = config.environment_dir
    if environment_source is None:
        raise ValueError("配置缺少共享环境目录（--sci-env）")
    environment_target = shared_dir / environment_source.name
    shared_dir.mkdir(parents=True, exist_ok=True)
    environment_target.mkdir(parents=True, exist_ok=True)
    for name in ENVIRONMENT_FILES:
        # 镜像配置复制本次实际生效的那一份（可能由 --image-env 覆盖）。
        source_path = Path(config.image_env_path) if name == ENV_IMAGE_CONFIG_NAME else environment_source / name
        if not source_path.is_file():
            continue
        shutil.copy2(source_path, environment_target / name)
    # shared 中只包含当前生效的规则文件。
    shutil.copy2(config.rubric_path, shared_dir / ("rubric" + Path(config.rubric_path).suffix))
    advice = environment_source / "human_advise.md"
    if advice.is_file():
        shutil.copy2(advice, shared_dir / "human_advise.md")
    return shared_dir


def create_source_workspace(run_root: Path, shared_dir: Path, source: SourceBundle) -> SourceWorkspace:
    workspace_root = run_root / "workspace"
    source_root = workspace_root / "sources" / source.source_slug
    input_dir = source_root / "input"
    plan_dir = source_root / "plan"
    draft_dir = source_root / "draft"
    reviews_dir = source_root / "reviews"
    reviewer_dir = reviews_dir / "agent"
    validation_dir = source_root / "validation"
    failures_dir = source_root / "failures"
    logs_dir = source_root / "logs"
    for path in (input_dir, plan_dir, draft_dir, reviewer_dir, validation_dir, failures_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)
    for name in ("context", "data", "code"):
        source_dir = getattr(source, name)
        (input_dir / name).mkdir(exist_ok=True)
        if source_dir.is_dir():
            shutil.copytree(source_dir, input_dir / name, dirs_exist_ok=True)
    return SourceWorkspace(
        run_root=run_root,
        workspace_root=workspace_root,
        shared_dir=shared_dir,
        source_root=source_root,
        input_dir=input_dir,
        plan_dir=plan_dir,
        draft_dir=draft_dir,
        reviews_dir=reviews_dir,
        reviewer_dir=reviewer_dir,
        validation_dir=validation_dir,
        failures_dir=failures_dir,
        logs_dir=logs_dir,
        events=logs_dir / "events.jsonl",
    )


def directory_manifest(root: Path) -> dict[str, tuple[str, str]]:
    manifest: dict[str, tuple[str, str]] = {}
    if not root.is_dir():
        return manifest
    paths = (path for path in root.rglob("*") if ".git" not in path.relative_to(root).parts)
    for path in paths:
        relative = str(path.relative_to(root))
        if path.is_symlink():
            manifest[relative] = ("symlink", str(path.readlink()))
        elif path.is_file():
            manifest[relative] = ("file", file_digest(path))
        elif path.is_dir():
            manifest[relative] = ("directory", "")
    return manifest


def record_event(workspace: SourceWorkspace, event: str, **data: object) -> None:
    payload = {"time": time.time(), "event": event, **data}
    with workspace.events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def snapshot_draft(workspace: SourceWorkspace, cycle: int) -> Path:
    snapshot = workspace.reviews_dir / f"draft-before-repair-{cycle:03d}"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    # Historical evidence is not another runnable copy of all task data.
    copy_history(workspace.draft_dir, snapshot, compact_all=True)
    return snapshot


def write_failure(workspace: SourceWorkspace, cycle: int, stage: str, text: str) -> Path:
    safe_stage = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage)
    path = workspace.failures_dir / f"cycle-{cycle:03d}-{safe_stage}.txt"
    path.write_text(text, encoding="utf-8")
    record_event(workspace, "failure", cycle=cycle, stage=stage, path=str(path), text=text)
    return path
