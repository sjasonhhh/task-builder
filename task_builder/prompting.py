from __future__ import annotations

import stat
import os
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
    prompt = template.format(**{key: str(value) for key, value in defaults.items()})
    if os.environ.get("TASK_BUILDER_CPU_ONLY") == "1":
        prompt += ("\n\n本轮通过 CPU 沙盒运行：任务 task.toml 的 [environment].gpus 必须为 0。"
                   "选择可在 CPU 上完成的科研任务；不得要求 CUDA 或 GPU。"
                   "如果论文任务必须使用 GPU，请明确报告不支持，不得假装已运行或验证。")
    shared_dir = values.get("shared_dir")
    advice = Path(shared_dir) / "human_advise.md" if shared_dir else None
    if advice is not None and advice.is_file():
        text = advice.read_text(encoding="utf-8").strip()
        if text:
            prompt += "\n\n领域专家建议（补充领域要求，不替代或放宽 rubric 的硬性规则）：\n" + text
    feedback_path = values.get("human_feedback_path")
    if feedback_path and Path(feedback_path).is_file() and name in {"repair.md", "reviewer.md"}:
        prompt += "\n\n本轮人工审核原始记录：\n" + Path(feedback_path).read_text(encoding="utf-8")
        if name == "repair.md":
            prompt += "\n逐条处理修改意见，同步更新 README 现有相关章节，不新增审核章节。最后用简短文字逐条说明已修改内容、依据及仍未解决的问题；不要声称人工已通过。"
        else:
            prompt += "\n逐条核对人工修改意见是否解决；未解决的有效要求列入 blocking_issues。意见与 rubric 冲突时说明冲突，不得放宽硬性规则。审核提示不是必须执行的修改意见。"
    return prompt


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
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except ValueError:
            plan = {}
        leaked = leaked_withheld_files(draft_data, source_data, plan)
        lines.append("程序核对：" + ("以下文件与计划标为 author_result / evaluation_reference 的来源文件内容相同：" + ", ".join(leaked) if leaked else "没有文件与计划标为 author_result / evaluation_reference 的来源文件内容相同"))
    return "\n".join(lines)


def _shared_environment_dir(shared_dir: Path) -> Path:
    candidates = sorted(path for path in shared_dir.iterdir() if path.is_dir())
    return max(candidates, key=lambda path: sum((path / name).is_file() for name in ENVIRONMENT_FILES)) if candidates else shared_dir


def read_shared_material(shared_dir: Path) -> tuple[str, str, str]:
    rubrics = sorted(path for path in shared_dir.glob("rubric.*") if path.is_file())
    rubric = "\n\n".join(path.read_text(encoding="utf-8") for path in rubrics)
    environment_dir = _shared_environment_dir(shared_dir)
    environment_parts = []
    for name in ENVIRONMENT_FILES:
        path = environment_dir / name
        if path.is_file():
            environment_parts.append(f"===== {name} =====\n{path.read_text(encoding='utf-8')}")
    return rubric, environment_dir.name, "\n\n".join(environment_parts)
