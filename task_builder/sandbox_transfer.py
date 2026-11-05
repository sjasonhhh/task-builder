"""Bounded-memory transfers; input archives are never written on the host."""
from __future__ import annotations

import hashlib
import json
import shlex
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .sandbox_runtime import CHUNK_BYTES, SandboxError


def checked(client, sandbox_id, command, **kwargs):
    result = client.command(sandbox_id, command, **kwargs)
    if result.return_code != 0:
        # Command output may contain credentials; callers get a safe error.
        raise SandboxError(f"沙盒文件或环境操作失败（退出码 {result.return_code}）")
    return result.stdout or ""


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SandboxError("传输文件路径越界")
    return path


class UploadStream:
    def __init__(self, client, sandbox_id, directory, cancelled):
        self.client, self.sandbox_id, self.directory = client, sandbox_id, directory
        self.cancelled = cancelled
        self.buffer = bytearray()
        self.digest = hashlib.sha256()
        self.parts = 0

    def write(self, data):
        self.digest.update(data)
        self.buffer.extend(data)
        while len(self.buffer) >= CHUNK_BYTES:
            self.flush_part(CHUNK_BYTES)
        return len(data)

    def flush_part(self, size):
        if self.cancelled.is_set():
            raise SandboxError("上传已中断")
        self.client.upload(self.sandbox_id, bytes(self.buffer[:size]),
                           f"{self.directory}/part-{self.parts:08d}")
        del self.buffer[:size]
        self.parts += 1

    def finish(self):
        if self.buffer:
            self.flush_part(len(self.buffer))


def upload_tree(client, sandbox_id, entries, destination: str, cancelled):
    """entries: (local path, relative remote name). Dereference only internal links."""
    temporary = "/tmp/tb-upload-" + uuid.uuid4().hex
    checked(client, sandbox_id, f"mkdir -m 700 {temporary}")
    stream = UploadStream(client, sandbox_id, temporary, cancelled)
    try:
        with tarfile.open(fileobj=stream, mode="w|", dereference=True) as archive:
            for local, name in entries:
                local = Path(local)
                safe_name(name)
                boundary = local.resolve() if local.is_dir() else local.resolve().parent

                def select(info):
                    if cancelled.is_set():
                        raise SandboxError("上传已中断")
                    relative = PurePosixPath(info.name).relative_to(PurePosixPath(name))
                    if any(p in {".git", ".venv", "__pycache__", ".pytest_cache"} for p in relative.parts):
                        return None
                    actual = local.joinpath(*relative.parts)
                    if not actual.resolve().is_relative_to(boundary):
                        raise SandboxError(f"材料中有指向目录外的链接: {actual}")
                    if not (info.isfile() or info.isdir()):
                        raise SandboxError(f"材料包含不支持的文件: {actual}")
                    return info

                archive.add(local, arcname=name, filter=select)
        stream.finish()
        digest = stream.digest.hexdigest()
        dest = shlex.quote(destination)
        checked(client, sandbox_id,
                f"cat {temporary}/part-* >{temporary}/archive.tar && "
                f"echo '{digest}  {temporary}/archive.tar' | sha256sum -c - >/dev/null && "
                f"mkdir -p {dest} && tar -xf {temporary}/archive.tar -C {dest}")
    finally:
        try:
            checked(client, sandbox_id, f"rm -rf {temporary}")
        except SandboxError:
            pass


def download_archive(client, sandbox_id, remote: str, destination: Path, *, allowed):
    """Download checked chunks into temporary disk space; reject archive escapes."""
    manifest = json.loads(client.download(sandbox_id, remote + "/manifest.json"))
    parts = manifest["parts"]
    if not isinstance(parts, int) or not 0 < parts <= 1_000_000:
        raise SandboxError("结果分片清单无效")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sandbox-transfer-", dir=destination.parent) as temp:
        archive_path = Path(temp) / "archive.tar"
        digest = hashlib.sha256()
        with archive_path.open("wb") as output:
            for index in range(parts):
                data = client.download(sandbox_id, f"{remote}/part-{index:08d}")
                digest.update(data)
                output.write(data)
        if digest.hexdigest() != manifest["sha256"]:
            raise SandboxError("结果校验失败，尚未替换本地文件")
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                path = safe_name(member.name)
                if not allowed(path) or not (member.isdir() or member.isfile()):
                    raise SandboxError("结果包中包含不允许的路径或文件类型")
            archive.extractall(destination, filter="data")


def pack_artifacts(run_root: Path, slug: str, destination: Path, *, checkpoint=False):
    """Executed remotely. A checkpoint omits large data; final export is complete."""
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / "archive.tar"
    source = run_root / "workspace" / "sources" / slug
    entries = [(source, f"workspace/sources/{slug}")]
    if not checkpoint:
        entries.append((run_root / "published" / slug, f"published/{slug}"))
    for name in ("worker-result.json", "worker.log"):
        entries.append((run_root / name, name))
    with tarfile.open(archive_path, "w", dereference=True) as archive:
        def select(info):
            parts = PurePosixPath(info.name).parts
            if "__pycache__" in parts:
                return None
            if parts[:3] == ("workspace", "sources", slug) and len(parts) > 3 and parts[3] == "input":
                return None
            if checkpoint and ("data" in parts or info.size > 4 * 1024 * 1024):
                return None
            actual = run_root.joinpath(*parts)
            if not actual.resolve().is_relative_to(run_root.resolve()) or not (info.isfile() or info.isdir()):
                raise SandboxError("结果中包含越界链接或特殊文件，未丢弃原件")
            return info
        for local, name in entries:
            if local.exists():
                archive.add(local, arcname=name, filter=select)
    digest, parts = hashlib.sha256(), 0
    with archive_path.open("rb") as handle:
        while data := handle.read(CHUNK_BYTES):
            digest.update(data)
            (destination / f"part-{parts:08d}").write_bytes(data)
            parts += 1
    archive_path.unlink()
    (destination / "manifest.json").write_text(json.dumps({"parts": parts, "sha256": digest.hexdigest()}))
