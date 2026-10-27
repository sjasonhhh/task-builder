from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sys
import tarfile
from pathlib import Path


def fingerprint(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def atomic_record(path: Path, record: dict) -> None:
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(record, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def receive(root: Path, index: int, size: int, digest: str) -> dict:
    part = root / f"{index:08d}.part"
    acknowledgment = root / f"{index:08d}.json"
    failed = root / f"{index:08d}.failed.json"
    temporary = part.with_suffix(".partial")
    acknowledgment.unlink(missing_ok=True)
    failed.unlink(missing_ok=True)
    actual = hashlib.sha256()
    received = 0
    try:
        with temporary.open("wb") as output:
            while received < size:
                chunk = sys.stdin.buffer.read(min(1024 * 1024, size - received))
                if not chunk:
                    raise RuntimeError(f"chunk {index} truncated: expected {size}, received {received}")
                output.write(chunk)
                actual.update(chunk)
                received += len(chunk)
            if actual.hexdigest() != digest:
                raise RuntimeError(f"chunk {index} digest mismatch")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(part)
        record = {"index": index, "size": received, "sha256": digest}
        atomic_record(acknowledgment, record)
        return record
    except Exception as error:
        if root.is_dir():
            atomic_record(failed, {"index": index, "failed": True, "expected_size": size, "received_size": received, "error": str(error)})
        raise
    finally:
        temporary.unlink(missing_ok=True)


def status(root: Path, index: int) -> dict:
    failed = root / f"{index:08d}.failed.json"
    if failed.is_file():
        return json.loads(failed.read_text())
    record = json.loads((root / f"{index:08d}.json").read_text())
    actual = fingerprint(root / f"{index:08d}.part")
    if record != {"index": index, **actual}:
        raise RuntimeError(f"chunk {index} acknowledgment does not match disk")
    return record


def assemble(root: Path, slots: list[int], size: int, digest: str, name: str, target: Path) -> dict:
    archive = root / "archive.tar"
    extraction = root / "unpacked"
    with archive.open("wb") as output:
        for index in slots:
            status(root, index)
            with (root / f"{index:08d}.part").open("rb") as handle:
                shutil.copyfileobj(handle, output, 1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    actual = fingerprint(archive)
    if actual != {"size": size, "sha256": digest}:
        raise RuntimeError("assembled archive does not match sender")
    extraction.mkdir()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != name:
                raise RuntimeError("archive contains an unexpected path")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise RuntimeError("archive contains an unsupported file type")
            if member.issym():
                link = Path(member.linkname)
                resolved = (extraction / path.parent / link).resolve()
                if link.is_absolute() or not resolved.is_relative_to(extraction / name):
                    raise RuntimeError("archive symlink escapes uploaded tree")
        bundle.extractall(extraction)
    (extraction / name).replace(target)
    record = {"count": len(slots), "target": str(target), **actual}
    atomic_record(root / "complete.json", record)
    return record


def main() -> None:
    action, directory, *arguments = sys.argv[1:]
    root = Path(directory)
    if not root.name.startswith(".task-builder-upload-"):
        raise ValueError("transfer directory must be private")
    def expired(signum, frame):
        raise TimeoutError(f"upload receiver {action} timed out")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(170 if action == "receive" else 1700)
    if action == "init":
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        record = {"ready": True}
    elif action == "receive":
        index, size, digest = arguments
        record = receive(root, int(index), int(size), digest)
    elif action == "status":
        record = status(root, int(arguments[0]))
    elif action == "assemble":
        slots, size, digest, name, target = arguments
        record = assemble(root, [int(slot) for slot in slots.split(",")], int(size), digest, name, Path(target))
    elif action == "complete":
        record = json.loads((root / "complete.json").read_text())
        if not Path(record["target"]).exists():
            raise RuntimeError("completed upload target is missing")
    elif action == "cleanup":
        if root.exists():
            shutil.rmtree(root, ignore_errors=False)
        record = {"removed": True}
    elif action == "absent":
        if root.exists():
            raise RuntimeError("private transfer directory still exists")
        record = {"removed": True}
    else:
        raise ValueError("unknown transfer action")
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
