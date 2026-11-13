"""Resolve portable agent executables for isolated workers."""
from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


def resolve_codex_source(codex_bin: str = "codex") -> Path:
    candidate = os.environ.get("TASK_BUILDER_CODEX_SOURCE") or shutil.which(codex_bin) or codex_bin
    source = Path(candidate).expanduser().resolve()
    if source.is_file():
        with source.open("rb") as handle:
            header = handle.read(4)
        if header == b"\x7fELF" and os.access(source, os.X_OK):
            return source
        # npm's codex executable is JavaScript, not a portable executable.
        arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
        triple = {"x64": "x86_64", "arm64": "aarch64"}.get(arch)
        if triple:
            package = source.parent.parent
            for vendor in (package / f"node_modules/@openai/codex-linux-{arch}/vendor",
                           package.parent / f"codex-linux-{arch}/vendor", package / "vendor"):
                for folder in ("bin", "codex"):
                    native = vendor / f"{triple}-unknown-linux-musl" / folder / "codex"
                    if native.is_file() and os.access(native, os.X_OK):
                        return native.resolve()
    raise FileNotFoundError(f"找不到可传送到 Linux worker 的 Codex 可执行程序: {source}")


def runtime_files(agent: str, executable: str | None = None) -> list[tuple[Path, str]]:
    if agent in {"nop", "oracle"}:
        return []
    if agent == "claude-code":
        from .hcluster_runtime import resolve_claude_source
        return [(resolve_claude_source(executable or "claude"), "/usr/local/bin/claude")]
    if agent != "codex":
        raise ValueError(f"不支持的运行程序: {agent}")
    source = resolve_codex_source(executable or "codex")
    files = [(source, "/usr/local/bin/codex")]
    host = source.with_name("codex-code-mode-host")
    if host.is_file():
        files.append((host, "/usr/local/bin/codex-code-mode-host"))
    bwrap = source.parent.parent / "codex-resources/bwrap"
    if bwrap.is_file():
        files.append((bwrap, "/usr/local/codex-resources/bwrap"))
    for rg in (source.with_name("rg"), source.parent.parent / "codex-path/rg", source.parent.parent / "path/rg"):
        if rg.is_file():
            files.append((rg, "/usr/local/bin/rg"))
            break
    return files
