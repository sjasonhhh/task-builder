from __future__ import annotations

import stat
from pathlib import Path

import json

from .integrity import content_manifest, leaked_withheld_files
from .workspace import ENVIRONMENT_FILES

_PROMPT_ROOT = Path(__file__).with_name("prompts")


def load_prompt(name: str, **values: object) -> str:
    template = (_PROMPT_ROOT / name).read_text(encoding="utf-8")
    defaults = {
        "plan": "<plan.json>",
        "plan_path": "<plan.json>",
        "rubric": "<完整任务 rubric 内容未提供>",
        "environment": "<完整共享环境文件未提供>",
        "environment_dir": "<shared-environment>",
        "failure": "无",
        "source_manifest": "<来源文件清单未提供>",
        "draft_manifest": "<任务草稿文件清单未提供>",
        "task_data_manifest": "<任务 data/ 最终文件清单未提供>",
    }
    defaults.update(values)
    return template.format(**{key: str(value) for key, value in defaults.items()})


def source_manifest(input_dir: Path) -> str:
    lines: list[str] = []
    for root_name in ("context", "data", "code"):
        root = input_dir / root_name
        if not root.is_dir():
            lines.append(f"{root_name}/ <目录不存在>")
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative = path.relative_to(input_dir).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            lines.append(f"{relative} ({size} bytes)")
    return "\n".join(lines) or "<context/、data/ 和 code/ 为空>"


def draft_manifest(draft_dir: Path) -> str:
    if not draft_dir.is_dir():
        return "<任务草稿目录不存在>"
    lines: list[str] = []
    for path in sorted(draft_dir.rglob("*"), key=lambda item: item.relative_to(draft_dir).as_posix()):
        relative = path.relative_to(draft_dir).as_posix()
        try:
            metadata = path.lstat()
        except OSError as exc:
            lines.append(f"{relative} [无法读取: {exc}]")
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            lines.append(f"{relative} [符号链接 -> {path.readlink()} mode={mode:04o}]")
        elif path.is_dir():
            lines.append(f"{relative}/ [目录 mode={mode:04o}]")
        elif path.is_file():
            lines.append(f"{relative} [文件 size={metadata.st_size} bytes mode={mode:04o}]")
        else:
            lines.append(f"{relative} [其他文件类型 mode={mode:04o}]")
    return "\n".join(lines) or "<任务草稿为空>"


def task_data_manifest(source_data: Path, draft_dir: Path, plan_path: Path) -> str:
    """求解者实际可见的任务 data/（即草稿 data/），附程序的防泄漏核对结果。"""
    draft_data = draft_dir / "data"
    entries = content_manifest(draft_data) if draft_data.is_dir() else {}
    lines = [
        f"data/{name}/" if entry.get("kind") == "directory" else f"data/{name} ({entry.get('size', '?')} bytes)"
        for name, entry in sorted(entries.items())
        if name != "."
    ]
    if not lines:
        return "<任务 data/ 为空>"
    if plan_path.is_file():
        leaked = leaked_withheld_files(draft_data, source_data, json.loads(plan_path.read_text(encoding="utf-8")))
        lines.append("程序核对：" + ("以下文件与计划标为 author_result / evaluation_reference 的来源文件内容相同：" + ", ".join(leaked) if leaked else "没有文件与计划标为 author_result / evaluation_reference 的来源文件内容相同"))
    return "\n".join(lines)


def _shared_environment_dir(shared_dir: Path) -> Path:
    candidates = [
        path
        for path in sorted(shared_dir.iterdir(), key=lambda item: item.name)
        if path.is_dir() and all((path / name).is_file() for name in ENVIRONMENT_FILES)
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            "shared 中必须恰好有一个完整共享环境目录，当前为: "
            + (", ".join(str(path) for path in candidates) or "无")
        )
    return candidates[0]


def read_shared_material(shared_dir: Path) -> tuple[str, str, str]:
    rubrics = sorted(path for path in shared_dir.glob("rubric.*") if path.is_file())
    if len(rubrics) != 1:
        raise RuntimeError("shared 中必须恰好有一份规则文件，当前为: " + (", ".join(path.name for path in rubrics) or "无"))
    rubric = rubrics[0].read_text(encoding="utf-8")
    environment_dir = _shared_environment_dir(shared_dir)
    environment_parts = []
    for name in ENVIRONMENT_FILES:
        path = environment_dir / name
        environment_parts.append(f"===== {name} =====\n{path.read_text(encoding='utf-8')}")
    return rubric, environment_dir.name, "\n\n".join(environment_parts)
