"""Compact historical evidence without trimming the current task's files."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path, PurePosixPath

from .integrity import file_digest

HISTORY_TEXT_LIMIT = 4 * 1024 * 1024
HISTORY_MANIFEST = ".history-files.json"
EXPORT_STATE = ".artifact-export.json"


def disposable(parts: tuple[str, ...]) -> bool:
    if "__pycache__" in parts:
        return True
    while parts and parts[0] == "previous-evidence":
        parts = parts[1:]
    return (parts[:2] == ("logs", "codex") and len(parts) >= 4
            and parts[3] in {"tmp", ".tmp"})


def historical_draft(parts: tuple[str, ...]) -> bool:
    if any(p.startswith("draft-before-repair-") for p in parts):
        return True
    return "previous-evidence" in parts and any(
        p in {"draft", ".repair-candidates", "accepted-before-interruption"}
        or p.startswith(("checkpoint-draft", "interrupted-repair-", "draft-interrupted-repair-"))
        for p in parts)


def small_text(path: Path) -> bool:
    if path.stat().st_size > HISTORY_TEXT_LIMIT:
        return False
    data = path.read_bytes()
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def omitted_file(path: Path) -> dict:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    return {"kind": "file", "size": path.stat().st_size, "sha256": file_digest(path)}


def copy_history(source: Path, target: Path, *, logical: tuple[str, ...] = (),
                 compact_all: bool = False) -> None:
    """Copy reports/logs intact; historical drafts contain small text and manifests."""
    target.mkdir(parents=True, exist_ok=True)
    omitted = {}
    for directory, directories, files in os.walk(source, followlinks=False):
        root = Path(directory)
        relative_root = root.relative_to(source)
        for name in list(directories):
            path = root / name
            relative = path.relative_to(source)
            if disposable(logical + relative.parts):
                directories.remove(name)
            elif path.is_symlink():
                omitted[relative.as_posix()] = omitted_file(path)
                directories.remove(name)
        (target / relative_root).mkdir(parents=True, exist_ok=True)
        for name in files:
            path = root / name
            relative = path.relative_to(source)
            if disposable(logical + relative.parts):
                continue
            compact = compact_all or historical_draft(logical + relative.parts)
            if path.is_symlink() or (compact and not small_text(path)):
                omitted[relative.as_posix()] = omitted_file(path)
                continue
            shutil.copy2(path, target / relative)
    if omitted:
        manifest = target / HISTORY_MANIFEST
        previous = json.loads(manifest.read_text()) if manifest.is_file() else {}
        previous.update(omitted)
        manifest.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n")


def latest_repair_candidate(source: Path) -> Path | None:
    """An interrupted candidate is evidence, never a previously accepted score."""
    active = [p for p in (source / ".repair-candidates").glob("*")
              if p.is_dir() and not p.is_symlink() and (p / "draft").is_dir() and (p / "plan").is_dir()]
    if active:
        return max(active, key=lambda p: (p.stat().st_mtime_ns, p.name))
    pointer = source / "recovery/current-repair.json"
    if pointer.is_file():
        try:
            relative = PurePosixPath(json.loads(pointer.read_text())["path"])
            candidate = source.joinpath(*relative.parts)
            if (not relative.is_absolute() and ".." not in relative.parts
                    and candidate.resolve().is_relative_to(source.resolve())
                    and (candidate / "draft").is_dir() and (candidate / "plan").is_dir()):
                return candidate
        except (OSError, ValueError, KeyError, TypeError):
            pass
    # A force-killed worker may not have reached its exception handler.
    choices = list((source / ".repair-candidates").glob("*"))
    choices += list((source / "recovery").glob("interrupted-repair-*"))
    choices = [p for p in choices if p.is_dir() and not p.is_symlink()
               and (p / "draft").is_dir() and (p / "plan").is_dir()]
    return max(choices, key=lambda p: (p.stat().st_mtime_ns, p.name)) if choices else None
