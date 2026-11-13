"""Bounded-memory transfers; input archives are never written on the host."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import shlex
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .sandbox_runtime import CHUNK_BYTES, SandboxError
from .artifact_history import (EXPORT_STATE, HISTORY_MANIFEST, disposable, historical_draft,
                               latest_repair_candidate, omitted_file, small_text)
from .integrity import file_digest


def _source_path(name):
    parts = PurePosixPath(name).parts
    for index in range(len(parts) - 2):
        if parts[index:index + 2] == ("workspace", "sources"):
            return PurePosixPath(*parts[:index + 3]), parts[index + 3:]
    return None, ()


def _json_member(archive, name, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    info = tarfile.TarInfo(str(name))
    info.size, info.mode = len(data), 0o600
    archive.addfile(info, io.BytesIO(data))


class ArtifactFilter:
    """A single history policy for old-run uploads and current-run exports."""
    def __init__(self, *, deduplicate=False):
        self.deduplicate = deduplicate
        self.omitted = {}
        self.candidates = {}
        self.content = {}
        self.fingerprints = {}
        self.stats = {"logical_bytes": 0, "payload_bytes": 0, "history_omitted_bytes": 0,
                      "deduplicated_bytes": 0, "categories": {}}

    def select(self, info, actual):
        prefix, relative = _source_path(info.name)
        if prefix is not None:
            if disposable(relative):
                return None
            source = actual
            for _ in relative:
                source = source.parent
            if relative and (relative[0] == ".repair-candidates" or
                             (relative[0] == "recovery" and len(relative) > 1
                              and relative[1].startswith("interrupted-repair-"))):
                if source not in self.candidates:
                    self.candidates[source] = latest_repair_candidate(source)
                chosen = self.candidates[source]
                if chosen is None or (actual != chosen and not actual.is_relative_to(chosen)
                                      and not chosen.is_relative_to(actual)):
                    return None
            if relative == (HISTORY_MANIFEST,) and info.isfile():
                self.omitted.setdefault(prefix, {}).update(json.loads(actual.read_text()))
                return None
            if info.isfile() and historical_draft(relative) and not small_text(actual):
                self.omitted.setdefault(prefix, {})[str(PurePosixPath(*relative))] = omitted_file(actual)
                self.stats["history_omitted_bytes"] += info.size
                return None
        if info.isfile():
            size = info.size
            self.stats["logical_bytes"] += size
            category = relative[0] if relative else PurePosixPath(info.name).parts[0]
            counts = self.stats["categories"]
            counts[category] = counts.get(category, 0) + size
            if self.deduplicate and size:
                before = actual.stat()
                digest = file_digest(actual)
                self.fingerprints[actual] = (before.st_ino, before.st_size, before.st_mtime_ns)
                key = (size, digest)
                if key in self.content:
                    info.type, info.linkname, info.size = tarfile.LNKTYPE, self.content[key], 0
                    self.stats["deduplicated_bytes"] += size
                    return info
                self.content[key] = info.name
            self.stats["payload_bytes"] += size
        return info

    def finish(self, archive):
        for prefix, records in self.omitted.items():
            _json_member(archive, prefix / HISTORY_MANIFEST, records)
        for path, expected in self.fingerprints.items():
            stat = path.stat()
            if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != expected:
                raise SandboxError(f"结果文件在打包期间发生变化，请重新回传: {path.name}")


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
        self.bytes = 0

    def write(self, data):
        self.bytes += len(data)
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
    policy = ArtifactFilter()
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
                    _, source_relative = _source_path(info.name)
                    if disposable(source_relative):
                        return None
                    actual = local.joinpath(*relative.parts)
                    if not actual.resolve().is_relative_to(boundary):
                        raise SandboxError(f"材料中有指向目录外的链接: {actual}")
                    if not (info.isfile() or info.isdir()):
                        raise SandboxError(f"材料包含不支持的文件: {actual}")
                    return policy.select(info, actual)

                archive.add(local, arcname=name, filter=select)
            policy.finish(archive)
        stream.finish()
        digest = stream.digest.hexdigest()
        dest = shlex.quote(destination)
        checked(client, sandbox_id,
                f"cat {temporary}/part-* >{temporary}/archive.tar && "
                f"echo '{digest}  {temporary}/archive.tar' | sha256sum -c - >/dev/null && "
                f"mkdir -p {dest} && tar -xf {temporary}/archive.tar -C {dest}")
        return {**policy.stats, "transferred_bytes": stream.bytes, "parts": stream.parts}
    finally:
        try:
            checked(client, sandbox_id, f"rm -rf {temporary}")
        except SandboxError:
            pass


def download_archive(client, sandbox_id, remote: str, destination: Path, *, allowed):
    """Download checked chunks into temporary disk space; reject archive escapes."""
    manifest = json.loads(client.download(sandbox_id, remote + "/manifest.json"))
    parts = manifest["parts"]
    version = manifest.get("format_version", 1)
    if version not in (1, 2) or isinstance(parts, bool) or not isinstance(parts, int) or not 0 < parts <= 1_000_000:
        raise SandboxError("结果分片清单无效")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sandbox-transfer-", dir=destination.parent) as temp:
        archive_path = Path(temp) / "archive.tar"
        digest = hashlib.sha256()
        received = 0
        with archive_path.open("wb") as output:
            for index in range(parts):
                data = client.download(sandbox_id, f"{remote}/part-{index:08d}")
                digest.update(data)
                output.write(data)
                received += len(data)
        if digest.hexdigest() != manifest["sha256"]:
            raise SandboxError("结果校验失败，尚未替换本地文件")
        with tarfile.open(archive_path) as archive:
            seen = {}
            required_directories = set()
            members = archive.getmembers()
            for member in members:
                path = safe_name(member.name)
                if (not allowed(path) or path in seen
                        or not (member.isdir() or member.isfile() or (version == 2 and member.islnk()))):
                    raise SandboxError("结果包中包含不允许的路径或文件类型")
                if any(parent in seen and not seen[parent].isdir() for parent in path.parents):
                    raise SandboxError("结果路径的父级不是目录")
                if path in required_directories and not member.isdir():
                    raise SandboxError("结果文件与目录路径冲突")
                target = destination.joinpath(*path.parts)
                if (target.is_symlink() or not target.resolve().is_relative_to(destination.resolve())
                        or any(destination.joinpath(*path.parts[:index]).is_symlink()
                               for index in range(1, len(path.parts)))):
                    raise SandboxError("结果目标包含不允许的文件链接")
                if member.islnk():
                    link = safe_name(member.linkname)
                    if not allowed(link) or link not in seen or not seen[link].isfile():
                        raise SandboxError("去重文件引用必须指向包内已出现的普通文件")
                seen[path] = member
                required_directories.update(path.parents)
                tarfile.data_filter(member, str(destination))
            directories = []
            for member in members:
                target = destination.joinpath(*safe_name(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories.append(member)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # Deduplication is a wire format only; editable copies must not share inodes.
                if target.exists():
                    target.unlink()
                if member.islnk():
                    original = destination.joinpath(*safe_name(member.linkname).parts)
                    shutil.copyfile(original, target)
                    filtered = tarfile.data_filter(member, str(destination))
                    target.chmod(filtered.mode if filtered.mode is not None else 0o600)
                    if filtered.mtime is not None:
                        os.utime(target, (filtered.mtime, filtered.mtime))
                else:
                    archive.extract(member, destination, filter="data")
            for member in reversed(directories):
                archive.extract(member, destination, filter="data")
        return {**manifest, "transferred_bytes": received}


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
    policy = ArtifactFilter(deduplicate=not checkpoint)
    with tarfile.open(archive_path, "w", dereference=True) as archive:
        def select(info):
            parts = PurePosixPath(info.name).parts
            if "__pycache__" in parts:
                return None
            # Codex leaves launcher links (including links to /usr/local/bin/codex)
            # in its native temporary directory. They are recreated on startup;
            # sessions and usage evidence beside this directory must be retained.
            if parts[:3] == ("workspace", "sources", slug):
                relative = parts[3:]
                while relative and relative[0] == "previous-evidence":
                    relative = relative[1:]
                if (relative[:2] == ("logs", "codex") and len(relative) >= 4
                        and relative[3] in {"tmp", ".tmp"}):
                    return None
            if parts[:3] == ("workspace", "sources", slug) and len(parts) > 3 and parts[3] == "input":
                return None
            if parts == ("workspace", "sources", slug, EXPORT_STATE):
                return None
            if checkpoint and ("data" in parts or info.size > 4 * 1024 * 1024):
                return None
            actual = run_root.joinpath(*parts)
            if not actual.resolve().is_relative_to(run_root.resolve()) or not (info.isfile() or info.isdir()):
                raise SandboxError(f"结果中包含越界链接或特殊文件，未丢弃原件: {info.name}")
            return policy.select(info, actual)
        for local, name in entries:
            if local.exists():
                archive.add(local, arcname=name, filter=select)
        policy.finish(archive)
        _json_member(archive, f"workspace/sources/{slug}/{EXPORT_STATE}",
                     {"format_version": 2, "checkpoint": checkpoint})
    digest, parts = hashlib.sha256(), 0
    with archive_path.open("rb") as handle:
        while data := handle.read(CHUNK_BYTES):
            digest.update(data)
            (destination / f"part-{parts:08d}").write_bytes(data)
            parts += 1
    archive_path.unlink()
    (destination / "manifest.json").write_text(json.dumps({"format_version": 2, "parts": parts,
        "sha256": digest.hexdigest(), "checkpoint": checkpoint, **policy.stats}, ensure_ascii=False))
