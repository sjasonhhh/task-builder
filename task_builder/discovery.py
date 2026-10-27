from __future__ import annotations

import re
from pathlib import Path

from .models import SourceBundle, StageFailure

CONTEXT_REQUIRED = ("text.md", "links.json")


def slugify(value: str) -> str:
    value = re.sub(r"[^\w-]+", "-", value.strip().lower(), flags=re.UNICODE).strip("-")
    return value or "source"


def _readable(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def validate_source(source: SourceBundle) -> None:
    errors: list[str] = []
    for name in CONTEXT_REQUIRED:
        path = source.context / name
        if not path.is_file() or not _readable(path):
            errors.append(f"context/{name} 不存在或不可读取")
    if not source.data.is_dir() or not any(source.data.iterdir()):
        errors.append("data/ 不存在或为空")
    if not source.code.is_dir() or not any(source.code.iterdir()):
        errors.append("code/ 不存在或为空")
    if errors:
        raise StageFailure(f"来源 {source.source_dir.name} 检查失败: {'；'.join(errors)}", stage="input")


def read_run_names(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise StageFailure(f"无法读取 UTF-8 任务名单: {path}", stage="input") from error
    names = list(dict.fromkeys(line.strip() for line in lines if line.strip() and not line.strip().startswith("#")))
    nested = [name for name in names if "/" in name or "\\" in name]
    if nested:
        raise StageFailure("任务名单每行只写来源名，环境由 --sci-env 指定，不要写路径: " + ", ".join(nested), stage="input")
    return names


def _looks_like_source(path: Path) -> bool:
    return path.is_dir() and (path / "context").is_dir()


def discover_sources(input_root: Path, *, validate: bool = True) -> list[SourceBundle]:
    """input_root 是环境这一层 <input>/<env>/，其下每个子目录是一个来源。"""
    if not input_root.is_dir():
        parent = input_root.parent
        if parent.is_dir() and any(_looks_like_source(item) for item in parent.iterdir()):
            raise StageFailure(f"inputs 已改为 <input>/<env>/<来源>/ 结构：{parent} 下直接放着来源目录，请移到 {input_root}/", stage="input")
        raise StageFailure(f"输入目录不存在: {input_root}（--input 传根目录，程序会追加 --sci-env 指定的环境名）", stage="input")
    entries = sorted((item for item in input_root.iterdir() if not item.name.startswith(".")), key=lambda item: item.name)
    if validate and any(item.is_file() for item in entries):
        raise StageFailure("inputs/ 必须使用 <来源名>/context/、data/、code/ 结构，根目录不能直接放文件", stage="input")
    source_dirs = [item for item in entries if item.is_dir()]
    if validate and not source_dirs:
        raise StageFailure(f"inputs/ 为空，请放入至少一个来源目录: {input_root}/<来源名>/", stage="input")
    used: set[str] = set()
    sources: list[SourceBundle] = []
    for source_dir in source_dirs:
        context, data, code = source_dir / "context", source_dir / "data", source_dir / "code"
        base = slugify(source_dir.name)
        slug, index = base, 2
        while slug in used:
            slug, index = f"{base}-{index}", index + 1
        used.add(slug)
        source = SourceBundle(source_dir.resolve(), slug, context.resolve(), data.resolve(), code.resolve())
        if validate:
            validate_source(source)
        sources.append(source)
    return sources
