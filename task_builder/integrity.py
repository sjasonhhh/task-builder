from __future__ import annotations

import hashlib
import json
from pathlib import Path

# 输入 data/ 顶层的这些条目是评测参考或来源说明，只给 Planner/Writer 构建 tests/ 使用，
# 不进入求解者可见的任务 data/。
SOLVER_HIDDEN_DATA = frozenset({"evaluation", "published-results", "author-public", "SOURCE.md", "material-checks.json"})


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_manifest(root: Path) -> dict[str, dict[str, object]]:
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    result = {}
    for path in paths:
        name = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = {"kind": "symlink", "target": str(path.readlink())}
        elif path.is_file():
            result[name] = {"kind": "file", "size": path.stat().st_size, "sha256": file_digest(path)}
        elif path.is_dir():
            result[name] = {"kind": "directory"}
    return result


def manifest_differences(expected: dict, actual: dict) -> list[str]:
    return [name for name in sorted(expected.keys() | actual.keys()) if expected.get(name) != actual.get(name)]


_DATA_PREFIXES = ("input/data/", "data/")


def normalize_data_path(raw: object, data_root: Path | None = None) -> str:
    """把 data_split 中的路径统一成相对来源 data/ 的路径。

    Planner 常写成绝对路径、带 input/data/ 或 data/ 前缀；这里统一去掉，结果仍可含 * ? [ ] { } 通配符。
    """
    text = str(raw).strip().replace("\\", "/")
    if data_root is not None:
        for root in {data_root.as_posix().rstrip("/"), data_root.resolve().as_posix().rstrip("/")}:
            if text == root or text.startswith(root + "/"):
                text = text[len(root):]
                break
    if "/input/data/" in text:
        text = text.split("/input/data/", 1)[1]
    text = text.strip("/")
    changed = True
    while changed:
        changed = False
        if text.startswith("./"):
            text, changed = text[2:], True
        for prefix in _DATA_PREFIXES:
            if text.startswith(prefix):
                text, changed = text[len(prefix):], True
    return text.strip("/")


def _expand_braces(pattern: str) -> list[str]:
    start = pattern.find("{")
    end = pattern.find("}", start + 1)
    if start < 0 or end < 0:
        return [pattern]
    head, body, tail = pattern[:start], pattern[start + 1:end], pattern[end + 1:]
    return [expanded for option in body.split(",") for expanded in _expand_braces(head + option + tail)]


def resolve_data_path(pattern: str, data_root: Path) -> list[str]:
    """data_split 路径（可含通配符）在来源 data/ 中实际对应的相对路径，按字典序排列。"""
    matches: set[str] = set()
    for option in _expand_braces(pattern):
        if not option:
            continue
        if any(char in option for char in "*?["):
            matches.update(path.relative_to(data_root).as_posix() for path in data_root.glob(option))
        elif (data_root / option).exists():
            matches.add(option)
    return sorted(matches)


def _data_split_files(plan: dict) -> list:
    split = plan.get("data_split")
    files = split.get("files") if isinstance(split, dict) else None
    return files if isinstance(files, list) else []


def withheld_data_paths(plan: dict, data_root: Path) -> tuple[str, ...]:
    """计划中标为 author_result / evaluation_reference 的来源数据路径（展开通配符）。"""
    paths: list[str] = []
    for item in _data_split_files(plan):
        if not isinstance(item, dict) or item.get("role") == "solver_input":
            continue
        path = normalize_data_path(item.get("path", ""), data_root)
        if path:
            paths.extend(resolve_data_path(path, data_root))
    return tuple(dict.fromkeys(paths))


def unresolved_data_paths(plan: dict, data_root: Path) -> list[str]:
    """data_split 中在来源 data/ 里找不到对应文件或目录的路径（按计划原文返回，便于报错定位）。"""
    missing = []
    for item in _data_split_files(plan):
        if isinstance(item, dict):
            path = normalize_data_path(item.get("path", ""), data_root)
            if not path or not resolve_data_path(path, data_root):
                missing.append(str(item.get("path", "")))
    return missing


def leaked_withheld_files(task_data: Path, source_data: Path, plan: dict) -> list[str]:
    """task_data 中与计划里 author_result / evaluation_reference 文件字节相同的文件（相对 task_data）。"""
    withheld: set[str] = set()
    for path in withheld_data_paths(plan, source_data):
        target = source_data / path
        for item in [target] if target.is_file() else sorted(target.rglob("*")):
            if item.is_file() and not item.is_symlink():
                withheld.add(file_digest(item))
    if not withheld or not task_data.is_dir():
        return []
    return sorted(
        path.relative_to(task_data).as_posix()
        for path in task_data.rglob("*")
        if path.is_file() and not path.is_symlink() and file_digest(path) in withheld
    )

