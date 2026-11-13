from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from task_builder.artifact_history import (EXPORT_STATE, HISTORY_MANIFEST, HISTORY_TEXT_LIMIT,
                                           latest_repair_candidate)
from task_builder.models import AgentError, StageFailure
from task_builder.resume import restore_source
from task_builder.sandbox_runtime import SandboxError
from task_builder.sandbox_transfer import download_archive, pack_artifacts, upload_tree
from task_builder.workspace import snapshot_draft


class Files:
    def command(self, sandbox_id, command, **kwargs):
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return SimpleNamespace(return_code=result.returncode, stdout=result.stdout)

    def upload(self, sandbox_id, data, path, **kwargs):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def download(self, sandbox_id, path, **kwargs):
        return Path(path).read_bytes()


def write(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def workspace(root):
    work = SimpleNamespace(source_root=root)
    for field in ("shared", "input", "plan", "draft", "reviews", "validation", "logs", "failures"):
        path = root / field
        path.mkdir(parents=True, exist_ok=True)
        setattr(work, field + "_dir", path)
    work.events = work.logs_dir / "events.jsonl"
    return work


def complete(root, label):
    write(root, "plan/plan.json", json.dumps({"label": label}))
    for name in ("task.toml", "instruction.md", "README.md", "solution/solve.sh", "tests/Dockerfile"):
        write(root, "draft/" + name, label)
    write(root, "draft/data/input.bin", b"\0original observations")


def test_history_snapshot_is_layout_independent_and_does_not_touch_current_data(tmp_path):
    work = workspace(tmp_path / "source")
    large = b"x" * (HISTORY_TEXT_LIMIT + 1)
    write(work.draft_dir, "solution/unusual/location/reference.json", large)
    write(work.draft_dir, "tests/arbitrary-name", b"\0binary result")
    write(work.draft_dir, "tests/nested/check.R", "print('check')\n")
    snap = snapshot_draft(work, 0)
    assert (snap / "tests/nested/check.R").read_text() == "print('check')\n"
    assert not (snap / "solution/unusual/location/reference.json").exists()
    assert not (snap / "tests/arbitrary-name").exists()
    records = json.loads((snap / HISTORY_MANIFEST).read_text())
    assert records["solution/unusual/location/reference.json"] == {
        "kind": "file", "size": len(large), "sha256": hashlib.sha256(large).hexdigest()}
    assert (work.draft_dir / "solution/unusual/location/reference.json").read_bytes() == large


def test_export_deduplicates_data_and_restores_independent_copies(tmp_path):
    run = tmp_path / "run"
    source = run / "workspace/sources/paper"
    blob = bytes(range(256)) * 17000
    for name in ("draft/data/input", "draft/tests/custom/place/ref", "draft/solution/ref"):
        write(source, name, blob)
    write(run, "published/paper/environment/data/input", blob)
    for name in ("reviews/draft-before-repair-000/tests/ref",
                 "previous-evidence/reviews/draft-before-repair-002/solution/ref"):
        write(source, name, blob)
    write(source, "reviews/draft-before-repair-000/tests/grader.py", "pass\n")
    write(source, "draft/data/same-as-script", "#!/bin/sh\nexit 0\n").chmod(0o644)
    write(source, "draft/solution/solve.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
    os.utime(source / "draft/solution/solve.sh", (1234567890, 1234567890))
    full = tmp_path / "full"
    pack_artifacts(run, "paper", full)
    manifest = json.loads((full / "manifest.json").read_text())
    assert manifest["format_version"] == 2
    assert manifest["deduplicated_bytes"] >= 3 * len(blob)
    assert manifest["history_omitted_bytes"] == 2 * len(blob)
    assert sum(p.stat().st_size for p in full.glob("part-*")) < len(blob) + 100000
    restored = tmp_path / "restored"
    transfer = download_archive(Files(), "id", str(full), restored, allowed=lambda _: True)
    result = restored / "workspace/sources/paper"
    a, b = result / "draft/data/input", result / "draft/tests/custom/place/ref"
    assert a.read_bytes() == b.read_bytes() == blob
    assert a.stat().st_ino != b.stat().st_ino
    a.write_text("changed")
    assert b.read_bytes() == blob
    assert (result / "draft/solution/solve.sh").stat().st_mode & 0o111
    assert (result / "draft/solution/solve.sh").stat().st_mtime == 1234567890
    assert not (result / "reviews/draft-before-repair-000/tests/ref").exists()
    assert (source / "reviews/draft-before-repair-000/tests/ref").read_bytes() == blob
    assert transfer["transferred_bytes"] < transfer["logical_bytes"]


def test_resume_upload_compacts_legacy_history_without_rewriting_old_run(tmp_path):
    source = tmp_path / "old/workspace/sources/paper"
    blob = b"\0" * (HISTORY_TEXT_LIMIT + 1)
    original = write(source, "previous-evidence/reviews/draft-before-repair-000/tests/ref.bin", blob)
    write(source, "previous-evidence/logs/usage-events.jsonl", '{"kind":"end"}\n')
    write(source, "draft/tests/custom-ref.bin", blob)
    target = tmp_path / "remote"
    result = upload_tree(Files(), "id", [(source, "output/test/runs/old/workspace/sources/paper")],
                         str(target), threading.Event())
    uploaded = target / "output/test/runs/old/workspace/sources/paper"
    assert not (uploaded / "previous-evidence/reviews/draft-before-repair-000/tests/ref.bin").exists()
    assert (uploaded / "previous-evidence/logs/usage-events.jsonl").is_file()
    assert (uploaded / "draft/tests/custom-ref.bin").read_bytes() == blob
    assert (uploaded / HISTORY_MANIFEST).is_file()
    assert original.read_bytes() == blob
    assert result["history_omitted_bytes"] == len(blob)


def test_failed_repair_keeps_only_latest_candidate_and_resume_rechecks_it(tmp_path, monkeypatch):
    from task_builder import writer_agent
    work = workspace(tmp_path / "old/workspace/sources/paper")
    complete(work.source_root, "accepted")
    monkeypatch.setattr(writer_agent, "read_shared_material", lambda _: ("rubric", "test", "environment"))
    monkeypatch.setattr(writer_agent, "source_manifest", lambda _: "sources")
    attempts = iter(["first interrupted edit", "latest interrupted edit"])

    def run(**options):
        label = next(attempts)
        (options["workdir"] / "solution/solve.sh").write_text(label)
        raise AgentError("upstream 503", stage="repair-writer")

    for _ in range(2):
        with pytest.raises(StageFailure, match="upstream 503"):
            writer_agent.run_repair_writer_agent(None, SimpleNamespace(author_env=None), work,
                                                SimpleNamespace(run=run), "current failure")
    candidate = latest_repair_candidate(work.source_root)
    assert candidate is not None
    assert (work.draft_dir / "solution/solve.sh").read_text() == "accepted"
    assert (candidate / "draft/solution/solve.sh").read_text() == "latest interrupted edit"
    assert len(list((work.source_root / "recovery").glob("interrupted-repair-*"))) == 1
    new = workspace(tmp_path / "new/workspace/sources/paper")
    plan, draft = restore_source(SimpleNamespace(source_slug="paper"), new,
                                 SimpleNamespace(resume_from=tmp_path / "old"))
    assert plan and draft
    assert (new.draft_dir / "solution/solve.sh").read_text() == "latest interrupted edit"
    events = [json.loads(line) for line in new.events.read_text().splitlines()]
    assert any(e["event"] == "interrupted-repair-reused" and e["old_scores_trusted"] is False for e in events)


def test_checkpoint_never_reuses_partial_candidate_as_complete_task(tmp_path):
    run = tmp_path / "run"
    source = run / "workspace/sources/paper"
    complete(source, "accepted")
    complete(source / ".repair-candidates/active", "unreviewed")
    packed, restored = tmp_path / "packed", tmp_path / "restored"
    pack_artifacts(run, "paper", packed, checkpoint=True)
    download_archive(Files(), "id", str(packed), restored, allowed=lambda _: True)
    assert json.loads((restored / "workspace/sources/paper" / EXPORT_STATE).read_text())["checkpoint"]
    new = workspace(tmp_path / "new")
    plan, draft = restore_source(SimpleNamespace(source_slug="paper"), new,
                                 SimpleNamespace(resume_from=restored))
    assert plan and not draft
    assert not (new.draft_dir / "task.toml").exists()


@pytest.mark.parametrize("link", ["../outside", "/outside", "future", "directory", "chain"])
def test_v2_dedup_links_cannot_escape_or_point_to_non_files(tmp_path, link):
    remote = tmp_path / "remote"
    remote.mkdir()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        file = tarfile.TarInfo("original")
        file.size = 2
        archive.addfile(file, io.BytesIO(b"ok"))
        directory = tarfile.TarInfo("directory")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        chain = tarfile.TarInfo("chain")
        chain.type, chain.linkname = tarfile.LNKTYPE, "original"
        archive.addfile(chain)
        duplicate = tarfile.TarInfo("duplicate")
        duplicate.type, duplicate.linkname = tarfile.LNKTYPE, link
        archive.addfile(duplicate)
    payload = buffer.getvalue()
    (remote / "part-00000000").write_bytes(payload)
    (remote / "manifest.json").write_text(json.dumps({"format_version": 2, "parts": 1,
                                                     "sha256": hashlib.sha256(payload).hexdigest()}))
    target = tmp_path / "target"
    with pytest.raises(SandboxError):
        download_archive(Files(), "id", str(remote), target, allowed=lambda _: True)
    assert not (target / "original").exists()
