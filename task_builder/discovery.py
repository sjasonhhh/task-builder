from __future__ import annotations

import re
from pathlib import Path

from .models import SourceBundle, StageFailure

def slugify(value: str) -> str:
    value = re.sub(r"[^\w-]+", "-", value.strip().lower(), flags=re.UNICODE).strip("-")
    return value or "source"


def read_run_names(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise StageFailure(f"无法读取 UTF-8 任务名单: {path}", stage="input") from error
    names = list(dict.fromkeys(line.strip() for line in lines if line.strip() and not line.strip().startswith("#")))
    return list(dict.fromkeys(name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] for name in names))


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
        sources.append(source)
    return sources
