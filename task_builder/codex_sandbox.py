"""Keep Codex filesystem restrictions working in unprivileged containers."""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
import shlex
import tarfile
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from .sandbox_runtime import SandboxError
from .sandbox_transfer import upload_tree

COMPATIBLE_VERSION = "0.114.0"


@contextmanager
def compatible_runtime():
    """Fetch an official, checksum-verified runtime; retain no local cache."""
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        raise SandboxError("当前机器架构没有可用的 Codex 沙盒兼容程序")
    version = f"{COMPATIBLE_VERSION}-linux-{arch}"
    with urllib.request.urlopen(f"https://registry.npmjs.org/@openai%2Fcodex/{version}", timeout=30) as response:
        metadata = json.loads(response.read(1024 * 1024))
    dist = metadata["dist"]
    expected_url = f"https://registry.npmjs.org/@openai/codex/-/codex-{version}.tgz"
    if dist["tarball"] != expected_url or not dist["integrity"].startswith("sha512-"):
        raise SandboxError("Codex 兼容程序的官方安装包信息不匹配")
    with tempfile.TemporaryDirectory(prefix="task-builder-codex-") as temporary:
        root = Path(temporary)
        archive = root / "runtime.tgz"
        size = 0
        with urllib.request.urlopen(expected_url, timeout=120) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 128 * 1024 * 1024:
                    raise SandboxError("Codex 安装包超过预期大小")
                output.write(chunk)
        with archive.open("rb") as handle:
            digest = base64.b64encode(hashlib.file_digest(handle, "sha512").digest()).decode()
        if "sha512-" + digest != dist["integrity"]:
            raise SandboxError("Codex 兼容程序校验失败")
        triple = "x86_64" if arch == "x64" else "aarch64"
        prefix = f"package/vendor/{triple}-unknown-linux-musl"
        files = []
        with tarfile.open(archive) as package:
            for member_name, destination in ((f"{prefix}/codex/codex", "bin/codex"),
                                              (f"{prefix}/path/rg", "bin/rg")):
                member = package.getmember(member_name)
                if not member.isfile() or member.size > 200 * 1024 * 1024:
                    raise SandboxError("Codex 安装包中的程序无效")
                local = root / Path(destination).name
                with package.extractfile(member) as source, local.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                local.chmod(0o755)
                files.append((local, destination))
        yield files


def sandbox_probe(session, private: Path):
    home = shlex.quote(str(private / "codex-probe"))
    session.command(f"mkdir -p {home}")
    prefix = f"CODEX_HOME={home} codex sandbox"
    try:
        help_result = session.client.command(session.id, prefix + " --help")
        linux = " linux" if re.search(r"^\s+linux\s", help_result.stdout, re.MULTILINE) else ""
        return session.client.command(session.id, prefix + linux + " -- /bin/true")
    finally:
        session.command(f"rm -rf {home}")


def ensure_codex_sandbox(session, private: Path):
    result = sandbox_probe(session, private)
    if result.return_code == 0:
        return
    # Only a known nested-container limitation selects the older Landlock
    # implementation. Both versions still enforce Codex's filesystem policy.
    details = (result.stderr or "") + (result.stdout or "")
    if "bwrap:" not in details and "bubblewrap" not in details:
        raise SandboxError("Codex 的文件保护自检失败，未启动造题")
    print(f"沙盒不支持当前 Codex 的系统隔离操作，使用兼容程序 {COMPATIBLE_VERSION}；模型参数保持不变", flush=True)
    with compatible_runtime() as files:
        upload_tree(session.client, session.id, files, "/usr/local", session.cancelled)
    result = sandbox_probe(session, private)
    if result.return_code:
        raise SandboxError("兼容版 Codex 的文件保护自检失败，未启动造题")
    session.save(codex_runtime_version=COMPATIBLE_VERSION)
