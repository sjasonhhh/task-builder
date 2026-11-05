from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from task_builder import sandbox_dispatch as dispatch
from task_builder.config import environment_for_subprocess
from task_builder.models import EnvConfig, PipelineConfig, SourceBundle, StageFailure
from task_builder.sandbox_runtime import SandboxError
from task_builder.sandbox_session import AuthoringSandbox
from task_builder.sandbox_transfer import download_archive, pack_artifacts, upload_tree


@pytest.fixture
def config(tmp_path):
    env = tmp_path / "image/chemistry"
    env.mkdir(parents=True)
    image_env = env / "image-env.toml"
    image_env.write_text('''[images]
experiment = "experiment@sha256:abc"
evaluation = "evaluation@sha256:def"
[hcluster]
max_cpu = 16
max_memory_mb = 65536
[timeouts]
agent_timeout_sec = 60
harbor_timeout_sec = 120
''')
    author = tmp_path / "dpinfer.env"
    author.write_text("MODEL_ID=model\nANTHROPIC_BASE_URL=https://models.example.test\nANTHROPIC_AUTH_TOKEN=test-token\n")
    for name in ("context", "data", "code"):
        path = tmp_path / "inputs/chemistry/paper" / name
        path.mkdir(parents=True)
        (path / "file.txt").write_text(name)
    rubric = tmp_path / "rubric.md"
    rubric.write_text("rules")
    return PipelineConfig(input_root=tmp_path / "inputs/chemistry", output_root=tmp_path / "out/chemistry",
                          author_env=EnvConfig(author, "model", {"MODEL_ID": "model", "ANTHROPIC_AUTH_TOKEN": "test-token"}),
                          experiment_image="experiment@sha256:abc", evaluation_image="evaluation@sha256:def",
                          image_env_path=image_env, environment_dir=env, rubric_path=rubric,
                          sci_env="chemistry", cpu=16, memory_mb=65536, sandbox=True,
                          extra={"sandbox_dispatch": True, "sandbox_author_image": "author-image"})


def bundle(config, name="paper"):
    root = config.input_root / name
    return SourceBundle(root, name, *(root / child for child in ("context", "data", "code")))


def test_cli_routes_images_and_resources(config, monkeypatch):
    monkeypatch.setattr("task_builder.sandbox_runtime.sandbox_settings", lambda: {})
    args = dispatch.build_parser().parse_args([
        "--sci-env", "chemistry", "--input", str(config.input_root.parent),
        "--output", str(config.output_root.parent), "--author-env", str(config.author_env.path),
        "--environment-root", str(config.environment_dir.parent), "--image", "main-only",
    ])
    actual = dispatch.dispatch_config(vars(args))
    assert (actual.cpu, actual.memory_mb) == (16, 65536)
    assert actual.extra["sandbox_author_image"] == "main-only"
    assert actual.experiment_image == config.experiment_image
    assert actual.evaluation_image == config.evaluation_image
    args.image = None
    assert dispatch.dispatch_config(vars(args)).extra["sandbox_author_image"] == config.experiment_image
    config.image_env_path.write_text(config.image_env_path.read_text().replace("max_cpu = 16\n", ""))
    with pytest.raises(ValueError, match="max_cpu"):
        dispatch.dispatch_config(vars(args))


@pytest.mark.parametrize("flag", ["--sandbox-cpu", "--sandbox-memory-mb", "--cpu", "--memory-mb", "--model-relay", "--proxy-host"])
def test_no_duplicate_resource_or_relay_flags(flag):
    parser = dispatch.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--sci-env", "chemistry", "--input", "inputs", "--output", "output", "--author-env", "model.env", flag, "1"])


@pytest.mark.parametrize("filename", ["dpinfer.env", "free-router.env"])
def test_model_direct_and_no_ad_credentials(filename, monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_BUILDER_DIRECT_MODELS", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://local-relay")
    monkeypatch.setenv("AD_PASSWORD", "ad-secret")
    monkeypatch.setenv("TASK_BUILDER_SANDBOX_CONFIG", "/private/sandbox.env")
    values = {"ANTHROPIC_BASE_URL": "https://direct.example.test", "ANTHROPIC_AUTH_TOKEN": "token", "HTTP_PROXY": "http://proxy"}
    result = environment_for_subprocess(EnvConfig(tmp_path / filename, "model", values))
    assert result["ANTHROPIC_BASE_URL"] == values["ANTHROPIC_BASE_URL"]
    assert not {"HTTP_PROXY", "HTTPS_PROXY", "AD_PASSWORD", "TASK_BUILDER_SANDBOX_CONFIG"} & result.keys()


def test_bundle_isolated_and_resume_paths(config, tmp_path):
    source = bundle(config)
    other = config.input_root / "other/context"
    other.mkdir(parents=True)
    (other / "secret.txt").write_text("other paper")
    previous = tmp_path / "previous-run"
    old = previous / "workspace/sources/paper/plan"
    old.mkdir(parents=True)
    (old / "plan.json").write_text("{}")
    config.resume_from = previous
    config.extra["human_review_requests"] = {"other": {"raw": "private feedback"}}
    entries, job = dispatch._entries_and_job(source, config, tmp_path / "run", "sandbox-id")
    names = [name for _, name in entries]
    assert "inputs/chemistry/paper/context" in names
    assert not any("other" in name or name.endswith(".env") or ".venv" in name for name in names)
    assert "test-token" not in json.dumps(job)
    assert job["config"]["extra"] == {}
    assert job["config"]["resume_from"].endswith("runs/previous-run")
    assert job["config"]["experiment_image"] == config.experiment_image


class LocalFiles:
    def command(self, sandbox_id, command, **kwargs):
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return SimpleNamespace(return_code=result.returncode, stdout=result.stdout)

    def upload(self, sandbox_id, data, path, **kwargs):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def download(self, sandbox_id, path, **kwargs):
        return Path(path).read_bytes()


def test_transfer_round_trip_and_checkpoint_omits_input(tmp_path):
    client = LocalFiles()
    source = tmp_path / "original"
    source.mkdir()
    (source / "tiny.txt").write_text("中文\n")
    target = tmp_path / "uploaded"
    upload_tree(client, "id", [(source, "context")], str(target), threading.Event())
    assert (target / "context/tiny.txt").read_bytes() == (source / "tiny.txt").read_bytes()
    run = tmp_path / "run"
    for relative in ("workspace/sources/paper/input/secret", "workspace/sources/paper/plan/plan.json",
                     "workspace/sources/paper/draft/data/data.txt", "published/paper/instruction.md"):
        path = run / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    packed = tmp_path / "packed"
    pack_artifacts(run, "paper", packed, checkpoint=True)
    result = tmp_path / "result"
    download_archive(client, "id", str(packed), result, allowed=lambda _: True)
    assert (result / "workspace/sources/paper/plan/plan.json").is_file()
    assert not (result / "workspace/sources/paper/input").exists()
    assert not (result / "workspace/sources/paper/draft/data").exists()
    assert not (result / "published").exists()
    full = tmp_path / "full"
    pack_artifacts(run, "paper", full)
    download_archive(client, "id", str(full), result, allowed=lambda _: True)
    assert (result / "workspace/sources/paper/draft/data/data.txt").is_file()
    assert (result / "published/paper/instruction.md").is_file()


@pytest.mark.parametrize("mode", ["traversal", "symlink", "corrupt"])
def test_reject_invalid_download(tmp_path, mode):
    remote = tmp_path / "remote"
    remote.mkdir()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("../escape" if mode == "traversal" else "ok")
        if mode == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
        archive.addfile(member)
    raw = buffer.getvalue()
    (remote / "part-00000000").write_bytes(raw)
    (remote / "manifest.json").write_text(json.dumps({"parts": 1, "sha256": "bad" if mode == "corrupt" else hashlib.sha256(raw).hexdigest()}))
    with pytest.raises(SandboxError):
        download_archive(LocalFiles(), "id", str(remote), tmp_path / "result", allowed=lambda _: True)
    assert not (tmp_path / "escape").exists()


def test_reject_input_link_outside_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "private"
    outside.write_text("secret")
    (source / "link").symlink_to(outside)
    with pytest.raises(SandboxError, match="链接"):
        upload_tree(LocalFiles(), "id", [(source, "data")], str(tmp_path / "destination"), threading.Event())


def test_publication_conflict_preserves_human_changes(config, tmp_path):
    from task_builder.human_review import publish_staged, review_manifest
    target = config.output_root / "final/paper"
    target.mkdir(parents=True)
    review = target / "human_review.md"
    review.write_text("original")
    config.extra["publication_expected"] = {"paper": review_manifest(target)}
    candidate = tmp_path / "run/published/paper"
    candidate.mkdir(parents=True)
    (candidate / "instruction.md").write_text("new version")
    review.write_text("user's new feedback")
    with pytest.raises(StageFailure, match="发生变化"):
        publish_staged(candidate, config, tmp_path / "run")
    assert review.read_text() == "user's new feedback"
    assert candidate.is_dir()


class FakeSession:
    instances = []

    def __init__(self, config, run_root, slug, cancelled):
        self.id, self.path = "id", run_root / "state.json"
        self.state, self.cancelled = {}, cancelled
        self.retained = None
        self.instances.append(self)

    def start(self):
        self.check()

    def check(self):
        if self.cancelled.is_set():
            raise SandboxError("cancelled")

    def save(self, **kwargs):
        self.state.update(kwargs)

    def command(self, *args, **kwargs):
        return "0"

    def close(self, *, retain=False):
        self.retained = retain


@pytest.mark.parametrize("download_fails", [False, True])
def test_failure_exports_before_delete_or_retains(config, tmp_path, monkeypatch, download_fails):
    events = []
    monkeypatch.setattr(dispatch, "AuthoringSandbox", FakeSession)
    monkeypatch.setattr(dispatch, "_prepare", lambda *a: None)
    monkeypatch.setattr(dispatch, "_launch", lambda *a: (_ for _ in ()).throw(SandboxError("launch uncertain")))
    monkeypatch.setattr(dispatch, "_stop", lambda *a: events.append("stop"))
    monkeypatch.setattr(dispatch.time, "sleep", lambda _: None)
    def export(*args, **kwargs):
        events.append("export")
        if download_fails:
            raise SandboxError("network")
    monkeypatch.setattr(dispatch, "_export", export)
    with pytest.raises(SandboxError):
        dispatch._dispatch_one(bundle(config), config, tmp_path / "run", threading.Event())
    assert events[0] == "stop" and events[1] == "export"
    assert FakeSession.instances[-1].retained is download_fails


def test_cancelled_pending_source_never_launches(config, tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "AuthoringSandbox", FakeSession)
    monkeypatch.setattr(dispatch, "_prepare", lambda *a: pytest.fail("must not upload cancelled source"))
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(SandboxError, match="cancelled"):
        dispatch._dispatch_one(bundle(config), config, tmp_path / "run", cancelled)
    assert FakeSession.instances[-1].retained is False


def test_independent_sources_and_bounded_concurrency(config, tmp_path, monkeypatch):
    from task_builder.pipeline import _run_sources
    config.concurrency = 2
    barrier = threading.Barrier(2)
    seen = []
    def execute(source, cfg, root, cancel):
        seen.append(source.source_slug)
        barrier.wait(timeout=5)
        return root / "published" / source.source_slug
    monkeypatch.setattr(dispatch, "_dispatch_one", execute)
    run = tmp_path / "run"
    run.mkdir()
    outputs, failures = _run_sources([bundle(config, "one"), bundle(config, "two")], config, run, run / "published")
    assert seen == ["one", "two"]
    assert [p.name for p in outputs] == ["one", "two"] and failures == []


def test_renew_and_cleanup_only_owned_children(config, tmp_path, monkeypatch):
    calls = []
    class Client:
        base_url, resource = "http://platform", "resource"
        def json(self, method, path, body, **kwargs):
            calls.append((method, path, body))
        def api_path(self, path):
            return path
        def children(self, parent):
            assert parent == "parent"
            return [{"id": "owned-child"}]
        def delete(self, sid):
            calls.append(("delete", sid))
    monkeypatch.setattr("task_builder.sandbox_session.SandboxClient", Client)
    session = AuthoringSandbox(config, tmp_path / "run", "paper", threading.Event())
    session.id = "parent"
    session.renew_once()
    session.close()
    assert "/parent/renew" in calls[0][1]
    assert calls[1:] == [("delete", "owned-child"), ("delete", "parent")]


def test_legacy_route_still_used_without_dispatch_flag(config, tmp_path, monkeypatch):
    from task_builder import pipeline
    config.extra.clear()
    monkeypatch.setattr(pipeline, "_run_sources_sequential", lambda *args: (["legacy"], []))
    assert pipeline._run_sources([bundle(config)], config, tmp_path, tmp_path) == (["legacy"], [])


def test_platform_name_fallback_scopes_children_and_uncertain_create(monkeypatch):
    from task_builder.sandbox_runtime import SandboxClient
    client = SandboxClient({"BASE_URL": "http://platform", "TASK_BUILDER_SANDBOX_RESOURCE": "pool",
                            "AD_USERNAME": "user", "AD_PASSWORD": "pass"})
    items = [{"id": "ours"}, {"id": "other"}, {"id": "main"}]
    names = {"ours": "tb-child-parent-abc", "other": "tb-child-different-parent-xyz", "main": "tb-author-test"}
    monkeypatch.setattr(client, "json", lambda *a, **k: {"items": items})
    monkeypatch.setattr(client, "runtime_detail", lambda sid, **k: {"metadata": {"name": names[sid]}})
    assert client.children("parent") == [{"id": "ours"}]
    assert client.find_created("tb-author-test", 9999999999) == [{"id": "main"}]


def test_staged_sandbox_candidate_cannot_bypass_version_check(config, tmp_path):
    from task_builder.pipeline import migrate_staged_published_tasks
    run = config.output_root / "runs/old"
    candidate = run / "published/paper"
    for name in ("instruction.md", "README.md", "task.toml", "solution/solve.sh", "tests/test.sh"):
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("candidate")
    (run / "sandbox-dispatch.json").write_text("{}")
    migrate_staged_published_tasks(config.output_root, [bundle(config)])
    assert candidate.exists()
    assert not (config.output_root / "final/paper").exists()


def test_expired_checkpoint_restores_plan_but_rebuilds_partial_draft(config, tmp_path, monkeypatch):
    previous = tmp_path / "previous"
    config.resume_from = previous
    statefile = previous / "sandbox-workers/paper.json"
    statefile.parent.mkdir(parents=True)
    statefile.write_text(json.dumps({"sandbox_id": "gone", "name": "tb-author-old", "source": "paper",
                                    "base_url": "http://platform", "resource": "pool", "phase": "working"}))
    checkpoint = previous / "checkpoints/paper/workspace/sources/paper"
    for name in ("plan/plan.json", "draft/task.toml", "draft/instruction.md", "draft/solution/solve.sh", "draft/tests/test.sh"):
        path = checkpoint / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("checkpoint")
    class Client:
        base_url, resource = "http://platform", "pool"
        def detail(self, sid):
            raise SandboxError("gone", 404)
        def children(self, sid):
            return []
        def delete(self, sid):
            pass
    monkeypatch.setattr(dispatch, "SandboxClient", Client)
    dispatch.recover_retained(config)
    target = previous / "workspace/sources/paper"
    assert (target / "plan/plan.json").is_file()
    assert not (target / "draft").exists()
    assert (target / "previous-evidence/checkpoint-draft/task.toml").is_file()
    assert json.loads(statefile.read_text())["phase"] == "deleted"


def test_human_repair_uploads_only_selected_published_version(config, tmp_path):
    from task_builder.human_review import review_manifest
    config.human_review = True
    target = config.output_root / "final/paper"
    target.mkdir(parents=True)
    (target / "human_review.md").write_text("需要修改")
    (target / "instruction.md").write_text("old instructions")
    previous = config.output_root / "runs/old-run"
    plan = previous / "workspace/sources/paper/plan/plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text("{}")
    request = {"raw": "需要修改", "baseline": review_manifest(target), "opinion": "change scoring"}
    config.extra["human_review_requests"] = {"paper": request, "other": {"raw": "unrelated"}}
    entries, job = dispatch._entries_and_job(bundle(config), config, tmp_path / "new-run", "sid")
    assert (target, "output/chemistry/final/paper") in entries
    assert job["config"]["extra"]["human_review_requests"] == {"paper": request}
    assert job["config"]["resume_from"].endswith("/runs/old-run")


@pytest.mark.parametrize("valid_digest", [True, False])
def test_helper_download_checks_digest_and_keeps_only_memory(monkeypatch, valid_digest):
    content = b"small pure-python wheel fixture"
    manifest = {"urls": [{"packagetype": "bdist_wheel", "filename": "dockerfile_parse-2.0.1-py2.py3-none-any.whl",
                           "url": "https://files.pythonhosted.org/packages/helper.whl",
                           "digests": {"sha256": hashlib.sha256(content).hexdigest() if valid_digest else "bad"}}]}
    calls = []
    def open_url(address, **kwargs):
        calls.append(address)
        return io.BytesIO(json.dumps(manifest).encode() if address.endswith("/json") else content)
    monkeypatch.setattr(dispatch, "_HELPER_WHEEL", None)
    monkeypatch.setattr(dispatch.urllib.request, "urlopen", open_url)
    if valid_digest:
        assert dispatch._helper_wheel() == content
        assert dispatch._helper_wheel() == content
        assert len(calls) == 2
    else:
        with pytest.raises(SandboxError, match="校验失败"):
            dispatch._helper_wheel()


def test_preinstalled_helper_needs_no_network(monkeypatch):
    session = SimpleNamespace(id="sandbox", client=SimpleNamespace(command=lambda *a: SimpleNamespace(return_code=0)))
    monkeypatch.setattr(dispatch, "_helper_wheel", lambda: pytest.fail("unnecessary download"))
    dispatch._prepare_helper(session)


def test_status_poll_survives_one_502_without_relaunch(monkeypatch):
    import urllib.error
    from task_builder.sandbox_runtime import SandboxClient
    from task_builder.sandbox_transfer import checked
    client = SandboxClient({"BASE_URL": "http://platform", "TASK_BUILDER_SANDBOX_RESOURCE": "pool",
                            "AD_USERNAME": "user", "AD_PASSWORD": "pass"}, progress=lambda _: None)
    client._token = "test-token"
    monkeypatch.setattr(client, "endpoint", lambda *a, **kw: ("http://platform/execd", {}))
    monkeypatch.setattr("task_builder.sandbox_runtime.time.sleep", lambda _: None)
    requests = []
    def open_request(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 502, "temporary", {}, io.BytesIO(b'{}'))
        return io.BytesIO(b'data: {"type":"stdout","text":"running"}\ndata: {"type":"execution_complete","exit_code":0}\n')
    monkeypatch.setattr(client._opener, "open", open_request)
    session = SimpleNamespace(command=lambda command, **kw: checked(client, "id", command, **kw))
    assert dispatch._worker_status(session) == "running"
    assert len(requests) == 2
    assert requests[0].data == requests[1].data
    assert all(request.full_url.endswith("/command") for request in requests)
    assert b"launch.sh" not in requests[0].data
