from __future__ import annotations

import json
import os
import re
import shutil
import time

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib
from pathlib import Path

from .models import PipelineConfig, SourceBundle, StageFailure
from .source_privacy import validate_public_description
from .integrity import SOLVER_HIDDEN_DATA, content_manifest, leaked_withheld_files, manifest_differences
from .deadline import remaining_timeout


_HARBOR_ORG_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _set_table_value(text: str, table: str, key: str, value: str | int) -> str:
    header = f"[{table}]"
    assignment = f'{key} = {value}' if isinstance(value, int) else f'{key} = "{value}"'
    lines = text.splitlines()
    header_index = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if header_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((header, assignment))
        return "\n".join(lines) + "\n"
    end = next((index for index in range(header_index + 1, len(lines)) if re.match(r"^\s*\[", lines[index])), len(lines))
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(header_index + 1, end):
        if key_pattern.match(lines[index]):
            lines[index] = assignment
            return "\n".join(lines) + "\n"
    lines.insert(header_index + 1, assignment)
    return "\n".join(lines) + "\n"


def _remove_environment_mode(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    section = ""
    for line in lines:
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            section = header.group(1)
        if section == "verifier" and re.match(r"^\s*environment_mode\s*=", line):
            continue
        output.append(line)
    return "\n".join(output) + "\n"


def _task_files(draft: Path, target: Path, *, include_readme: bool = False) -> None:
    for relative in ("task.toml", "instruction.md", "solution", "tests"):
        source_path = draft / relative
        # 缺少 solution/ 时跳过；必需文件缺失由上游草稿检查暴露。
        if relative == "solution" and not source_path.exists():
            continue
        destination = target / relative
        if source_path.is_dir():
            shutil.copytree(source_path, destination)
        else:
            shutil.copy2(source_path, destination)
    if include_readme:
        shutil.copy2(draft / "README.md", target / "README.md")


def _copy_writer_data(draft: Path, source: SourceBundle, target: Path) -> None:
    """任务 data/ 完全由 Writer/Repair 在草稿 data/ 中准备，程序只做防泄漏检查后原样复制。"""
    draft_data = draft / "data"
    if not draft_data.is_dir() or not any(path.is_file() for path in draft_data.rglob("*")):
        raise StageFailure("草稿 data/ 为空：求解者数据由 Writer 按计划 data_split 复制、改名或转换后放入草稿 data/", stage="validation-data")
    hidden = sorted(path.name for path in draft_data.iterdir() if path.name in SOLVER_HIDDEN_DATA)
    if hidden:
        raise StageFailure("评测参考或来源说明不能放进任务 data/，请移到 tests/: " + ", ".join(hidden), stage="validation-data")
    links = sorted(path.relative_to(draft_data).as_posix() for path in draft_data.rglob("*") if path.is_symlink())
    if links:
        raise StageFailure("草稿 data/ 不能包含文件链接，请复制实际文件: " + ", ".join(links), stage="validation-data")
    plan_path = draft.parent / "plan" / "plan.json"
    if not plan_path.is_file():
        raise StageFailure("缺少 plan/plan.json，无法核对任务 data/", stage="validation-data")
    leaked = leaked_withheld_files(draft_data, source.data, json.loads(plan_path.read_text(encoding="utf-8")))
    if leaked:
        raise StageFailure("草稿 data/ 中以下文件与计划标为 author_result / evaluation_reference 的来源文件内容相同，不能交给求解者: " + ", ".join(leaked), stage="validation-data")
    expected = content_manifest(draft_data)
    shutil.copytree(draft_data, target / "data", dirs_exist_ok=True)
    differences = manifest_differences(expected, content_manifest(target / "data"))
    if differences:
        raise StageFailure("本地数据复制不完整: " + ", ".join(differences), stage="validation-data")


def _copy_task_data(draft: Path, source: SourceBundle, target: Path, config: PipelineConfig) -> None:
    _copy_writer_data(draft, source, target)


def validate_task_config(task_path: Path) -> None:
    """Validate a task.toml with the Harbor version used by this project."""
    try:
        document = tomllib.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StageFailure(f"Harbor task.toml 不是有效 TOML: {exc}", stage="reviewer") from exc

    if not isinstance(document.get("artifacts"), list):
        raise StageFailure(
            "Harbor task.toml 必须使用根级 artifacts = [...] 列表，不能写成 [artifacts] 表",
            stage="reviewer",
        )
    task = document.get("task")
    if not isinstance(task, dict):
        raise StageFailure("Harbor task.toml 缺少 [task] 表", stage="reviewer")
    task_name = task.get("name")
    if not isinstance(task_name, str) or not _HARBOR_ORG_NAME_PATTERN.fullmatch(task_name) or ".." in task_name:
        raise StageFailure(
            "Harbor task.toml 的 [task].name 必须是 org/name 格式，例如 example/task",
            stage="reviewer",
        )

    try:
        from harbor.models.task.config import TaskConfig
    except ImportError:
        return
    try:
        TaskConfig.model_validate_toml(task_path.read_text(encoding="utf-8"))
    except StageFailure:
        raise
    except Exception as exc:
        raise StageFailure(f"当前 Harbor 无法解析 task.toml: {exc}", stage="reviewer") from exc


ALLOWED_METADATA_KEYS = ("domain", "field", "subfield", "expert_time_estimate_hours")


def validate_task_metadata(task_path: Path) -> None:
    """新草稿的 [metadata] 只保留领域与工时字段。"""
    try:
        document = tomllib.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StageFailure(f"Harbor task.toml 不是有效 TOML: {exc}", stage="reviewer") from exc
    metadata = document.get("metadata", {})
    extra = sorted(set(metadata) - set(ALLOWED_METADATA_KEYS)) if isinstance(metadata, dict) else ["<非表格>"]
    if extra:
        raise StageFailure(
            "task.toml 的 [metadata] 只允许 " + "、".join(ALLOWED_METADATA_KEYS) + "，请删除: " + ", ".join(extra),
            stage="reviewer",
        )


def finalize_harbor_task(target: Path, config: PipelineConfig, *, agent_timeout_sec: int | None = None) -> Path:
    """两条运行路径（造任务验证与 run_task 答题）共用的 Harbor 任务收尾步骤。"""
    environment = target / "environment"
    environment.mkdir()
    shutil.copytree(target / "data", environment / "data")
    task_path = target / "task.toml"
    text = _remove_environment_mode(task_path.read_text(encoding="utf-8"))
    for table, image in (("environment", config.experiment_image), ("verifier.environment", config.evaluation_image)):
        text = _set_table_value(text, table, "docker_image", image)
        text = _set_table_value(text, table, "network_mode", "no-network")
        text = _set_table_value(text, table, "build_timeout_sec", 1800)
    if agent_timeout_sec is not None:
        text = _set_table_value(text, "agent", "timeout_sec", agent_timeout_sec)
    task_path.write_text(text, encoding="utf-8")
    validate_task_config(task_path)
    return target


def prepare_harbor_task(draft: Path, source: SourceBundle, target: Path, config: PipelineConfig) -> Path:
    if target.exists():
        for path in target.iterdir():
            if path.name == "harbor-jobs":
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    target.mkdir(parents=True, exist_ok=True)
    _task_files(draft, target)
    for attempt in range(3):
        try:
            _copy_task_data(draft, source, target, config)
            break
        except (OSError, StageFailure) as exc:
            if isinstance(exc, StageFailure) and exc.stage == "task-timeout":
                raise
            if isinstance(exc, StageFailure) and exc.stage == "validation-data" and "复制不完整" not in str(exc):
                raise
            if attempt == 2:
                raise
            if (target / "data").exists():
                shutil.rmtree(target / "data")
            time.sleep(remaining_timeout((5, 15)[attempt]))
    finalize_harbor_task(target, config)
    return target


def _strip_image_values(task_path: Path) -> None:
    text = task_path.read_text(encoding="utf-8")
    output: list[str] = []
    section = ""
    for line in text.splitlines():
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            section = header.group(1)
        if section in {"environment", "verifier.environment"} and re.match(r"^\s*docker_image\s*=", line):
            continue
        if section == "verifier" and re.match(r"^\s*environment_mode\s*=", line):
            continue
        output.append(line)
    cleaned: list[str] = []
    current_section = ""
    section_has_values = False
    section_lines: list[str] = []
    def flush() -> None:
        if current_section != "verifier.environment" or section_has_values:
            cleaned.extend(section_lines)
    for line in output:
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            flush()
            current_section = header.group(1)
            section_lines = [line]
            section_has_values = False
        else:
            section_lines.append(line)
            if line.strip() and not line.lstrip().startswith("#"):
                section_has_values = True
    flush()
    task_path.write_text("\n".join(cleaned).rstrip() + "\n", encoding="utf-8")


def publish_task(draft: Path, source: SourceBundle, output_root: Path, config: PipelineConfig) -> Path:
    privacy = validate_public_description(draft, source)
    if not privacy.passed:
        raise StageFailure(privacy.text, stage="source-privacy")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = draft.parent / "publish-temp"
    backup = draft.parent / "previous-published"
    for path in (temporary, backup):
        if path.exists():
            shutil.rmtree(path)
    temporary.mkdir(parents=True)
    _task_files(draft, temporary, include_readme=True)
    _copy_task_data(draft, source, temporary, config)
    _strip_image_values(temporary / "task.toml")
    validate_task_config(temporary / "task.toml")
    target = output_root / source.source_slug
    moved_old = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        os.replace(temporary, target)
    except OSError:
        if moved_old and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    return target
