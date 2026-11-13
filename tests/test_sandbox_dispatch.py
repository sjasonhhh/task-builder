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
        "--environment-root", str(config.environment_dir.parent), "--task-builder-image", "main-only",
    ])
    actual = dispatch.dispatch_config(vars(args))
    assert (actual.cpu, actual.memory_mb) == (16, 65536)
    assert actual.extra["sandbox_author_image"] == "main-only"
    assert actual.experiment_image == config.experiment_image
    assert actual.evaluation_image == config.evaluation_image
    args.task_builder_image = None
    assert dispatch.dispatch_config(vars(args)).extra["sandbox_author_image"] == dispatch.DEFAULT_TASK_BUILDER_IMAGE
    config.image_env_path.write_text(config.image_env_path.read_text().replace("max_cpu = 16\n", ""))
    with pytest.raises(ValueError, match="max_cpu"):
        dispatch.dispatch_config(vars(args))


@pytest.mark.parametrize("sci_env", ["chemistry", "single-cell", "test_env"])
@pytest.mark.parametrize("override", [False, True])
def test_default_author_image_is_independent_of_domain_and_runtime_images(config, monkeypatch, sci_env, override):
    monkeypatch.setattr("task_builder.sandbox_runtime.sandbox_settings", lambda: {})
    environment = config.environment_dir.parent / sci_env
    environment.mkdir(exist_ok=True)
    experiment, evaluation = f"{sci_env}-experiment", f"{sci_env}-evaluation"
    text = config.image_env_path.read_text().replace(config.experiment_image, experiment).replace(config.evaluation_image, evaluation)
    (environment / "image-env.toml").write_text(text)
    argv = ["--sci-env", sci_env, "--input", str(config.input_root.parent),
            "--output", str(config.output_root.parent), "--author-env", str(config.author_env.path),
            "--environment-root", str(config.environment_dir.parent)]
    if override:
        experiment, evaluation = "override-experiment", "override-evaluation"
        argv += ["--experiment-image", experiment, "--evaluation-image", evaluation]
    actual = dispatch.dispatch_config(vars(dispatch.build_parser().parse_args(argv)))
    assert actual.extra["sandbox_author_image"] == dispatch.DEFAULT_TASK_BUILDER_IMAGE
    assert (actual.experiment_image, actual.evaluation_image) == (experiment, evaluation)


@pytest.mark.parametrize("image", [None, "other-author-image"])
def test_dry_run_reports_three_images_without_starting_tasks(config, monkeypatch, capsys, image):
    monkeypatch.setattr("task_builder.sandbox_runtime.sandbox_settings", lambda: {})
    def unexpected(*args, **kwargs):
        pytest.fail("dry-run must not create sandboxes or run tasks")
    monkeypatch.setattr(dispatch, "recover_retained", unexpected)
    monkeypatch.setattr(AuthoringSandbox, "start", unexpected)
    monkeypatch.setattr("task_builder.pipeline.run_pipeline", unexpected)
    argv = ["--sci-env", "chemistry", "--input", str(config.input_root.parent),
            "--output", str(config.output_root.parent), "--author-env", str(config.author_env.path),
            "--environment-root", str(config.environment_dir.parent), "--dry-run"]
    if image is not None:
        argv += ["--task-builder-image", image]
    assert dispatch.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["author_image"] == (image or dispatch.DEFAULT_TASK_BUILDER_IMAGE)
    assert result["experiment_image"] == config.experiment_image
    assert result["evaluation_image"] == config.evaluation_image
    assert not config.output_root.exists()


@pytest.mark.parametrize("old_args", [["--image", "old-image"], ["--image=old-image"]])
def test_removed_image_flag_names_replacement(old_args, capsys):
    with pytest.raises(SystemExit) as exc:
        dispatch.main(old_args)
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "--image 已移除" in error and "--task-builder-image" in error


def test_help_shows_only_new_author_image_flag():
    parser = dispatch.build_parser()
    assert "--task-builder-image" in parser.format_help()
    assert "--image" not in parser._option_string_actions


@pytest.mark.parametrize("image_args", [["--task-builder-image", "author"], ["--task-builder-image=author"]])
def test_local_cli_explains_remote_authoring_entry(image_args, capsys):
    from task_builder import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["--sandbox", *image_args])
    assert exc.value.code == 2
    assert "bash run_sandbox.sh --task-builder-image" in capsys.readouterr().err
    assert "造题程序在当前机器运行" in cli.build_parser().format_help()


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
        def renew(self, sid, expires, **kwargs):
            calls.append(("POST", f"/{sid}/renew", {"expiresAt": expires.isoformat()}))
            return expires.isoformat()
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


def test_seven_hour_task_requests_full_lifetime_and_checks_platform(config, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from task_builder import sandbox_session
    config.task_timeout_sec = 25200
    class Client:
        base_url, resource = "http://platform", "pool"
        def json(self, method, path, payload, **kwargs):
            assert payload['lifecycleMinutes'] == 1440
            self.expires = datetime.now(timezone.utc) + timedelta(minutes=payload['lifecycleMinutes'])
            return {'id':'parent'}
        def detail(self, sid, **kwargs):
            return {'status':{'state':'Running'}, 'expiresAt':self.expires.isoformat()}
    monkeypatch.setattr(sandbox_session, 'SandboxClient', Client)
    monkeypatch.setattr(AuthoringSandbox, 'command', lambda *a, **k: '')
    monkeypatch.setattr(AuthoringSandbox, '_renew', lambda self: None)
    session = AuthoringSandbox(config, tmp_path/'run', 'paper', threading.Event())
    session.start()
    assert session.state['task_timeout_sec'] == 25200
    assert session.state['expires_at'] == session.client.expires.isoformat()


@pytest.mark.parametrize('actual', [None, '2020-01-01T00:00:00Z', 'invalid'])
def test_renew_success_response_cannot_hide_unextended_expiry(monkeypatch, actual):
    from datetime import datetime, timedelta, timezone
    from task_builder.sandbox_runtime import SandboxClient
    client = SandboxClient({'BASE_URL':'http://platform','TASK_BUILDER_SANDBOX_RESOURCE':'pool',
                            'AD_USERNAME':'user','AD_PASSWORD':'pass'})
    requested = datetime.now(timezone.utc)+timedelta(hours=4)
    monkeypatch.setattr(client, 'json', lambda *a, **k: {'expiresAt':requested.isoformat()})
    monkeypatch.setattr(client, 'detail', lambda *a, **k: {'expiresAt':actual})
    with pytest.raises(SandboxError, match='实际到期时间'):
        client.renew('parent', requested)


def test_renew_does_not_shorten_seven_hour_allocation(config, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    target = datetime.now(timezone.utc)+timedelta(hours=48)
    class Client:
        base_url, resource = 'http://platform','pool'
        def renew(self, sid, expires, **kwargs):
            pytest.fail('充分的已确认时长不应再次发送续期请求')
    monkeypatch.setattr('task_builder.sandbox_session.SandboxClient', Client)
    session = AuthoringSandbox(config, tmp_path/'run', 'paper', threading.Event())
    session.id = 'parent'
    session.state['expires_at'] = target.isoformat()
    session.renew_once()
    assert session.state['expires_at'] == target.isoformat()
    assert session.state['renewal_skipped'] == 'existing_lifetime_sufficient'
    assert session.state['lifetime_checked_at']
    assert 'last_renewed_at' not in session.state


def test_missing_sandbox_is_not_reported_retained(config, tmp_path, monkeypatch):
    class Client:
        base_url, resource = 'http://platform','pool'
        def detail(self, sid):
            raise SandboxError('not found', 404)
        def children(self, sid):
            return []
    monkeypatch.setattr('task_builder.sandbox_session.SandboxClient', Client)
    session = AuthoringSandbox(config, tmp_path/'run', 'paper', threading.Event())
    session.id = 'gone'
    session.close(retain=True)
    assert session.state['phase'] == 'missing'
    assert session.state['retained'] is False


def test_final_transfer_error_preserves_original_cause(config, tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, 'AuthoringSandbox', FakeSession)
    monkeypatch.setattr(dispatch, '_prepare', lambda *a: None)
    monkeypatch.setattr(dispatch, '_launch', lambda *a: (_ for _ in ()).throw(SandboxError('original failure')))
    monkeypatch.setattr(dispatch, '_stop', lambda *a: (_ for _ in ()).throw(SandboxError('sandbox disappeared', 404)))
    with pytest.raises(SandboxError, match='sandbox disappeared.*original failure'):
        dispatch._dispatch_one(bundle(config), config, tmp_path/'run', threading.Event())
    state = FakeSession.instances[-1].state
    assert state['dispatch_error']['message'] == 'original failure'
    assert state['transfer_error']['stage'] == 'stop-worker'


@pytest.mark.parametrize('disappears_after_check', [False, True])
def test_cleanup_accepts_confirmed_absence_even_when_delete_returns_500(monkeypatch, disappears_after_check):
    from task_builder.sandbox_runtime import SandboxClient
    client = SandboxClient({'BASE_URL':'http://platform','TASK_BUILDER_SANDBOX_RESOURCE':'pool',
                            'AD_USERNAME':'user','AD_PASSWORD':'pass'})
    calls = []
    def detail(*a, **k):
        calls.append('detail')
        if disappears_after_check and len(calls)==1:
            return {'state':'Running'}
        raise SandboxError('not found',404)
    def delete(*a, **k):
        assert disappears_after_check
        assert k['retry'] is False
        calls.append('delete')
        raise SandboxError('Failed to delete sandbox',500)
    monkeypatch.setattr(client,'detail',detail)
    monkeypatch.setattr(client,'json',delete)
    client.delete('gone')
    assert calls == (['detail','delete','detail'] if disappears_after_check else ['detail'])


def test_recovery_only_touches_requested_sources(config, tmp_path, monkeypatch):
    previous=tmp_path/'previous'
    config.resume_from=previous
    config.run_txt=tmp_path/'run.txt'
    config.run_txt.write_text('paper\n')
    states=previous/'sandbox-workers'
    states.mkdir(parents=True)
    for slug in ['paper','unrelated']:
        (states/(slug+'.json')).write_text(json.dumps({'source':slug,'name':'tb-'+slug,
            'sandbox_id':slug,'phase':'working','base_url':'http://platform','resource':'pool'}))
    touched=[]
    class Client:
        base_url,resource='http://platform','pool'
        def detail(self,sid):
            touched.append(sid)
            raise SandboxError('gone',404)
        def children(self,sid): return []
        def delete(self,sid): pass
    monkeypatch.setattr(dispatch,'SandboxClient',Client)
    dispatch.recover_retained(config)
    assert touched == ['paper']
    assert json.loads((states/'unrelated.json').read_text())['phase']=='working'


def test_resume_keeps_checkpoint_draft_evidence(config, tmp_path):
    from task_builder.resume import restore_source
    from task_builder.workspace import create_source_workspace
    config.resume_from=tmp_path/'previous'
    old=config.resume_from/'workspace/sources/paper'
    for name in ['plan/plan.json','previous-evidence/checkpoint-draft/solution/solve.sh','reviews/review.json']:
        p=old/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text('saved evidence')
    workspace=create_source_workspace(tmp_path/'new',tmp_path/'shared',bundle(config))
    plan,draft=restore_source(bundle(config),workspace,config)
    assert plan and not draft
    assert (workspace.source_root/'previous-evidence/previous-evidence/checkpoint-draft/solution/solve.sh').read_text()=='saved evidence'


def test_rebuilding_writer_can_read_prior_draft_and_review(config, tmp_path, monkeypatch):
    from task_builder import writer_agent
    from task_builder.workspace import create_source_workspace
    workspace=create_source_workspace(tmp_path/'new',tmp_path/'shared',bundle(config))
    evidence=workspace.source_root/'previous-evidence'
    evidence.mkdir()
    (evidence/'review.txt').write_text('fix the selected channels check')
    monkeypatch.setattr(writer_agent,'read_shared_material',lambda p: ('rubric','chemistry','environment'))
    seen={}
    def run(agent,draft,log_path,**options):
        seen.update(options)
        for name in writer_agent._REQUIRED_FILES:
            p=draft/name
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('rebuilt')
    monkeypatch.setattr(writer_agent,'_run_until_files_written',run)
    writer_agent.run_writer_agent(bundle(config),config,workspace,object())
    assert str(evidence) in seen['prompt']
    assert evidence in seen['allowed_dirs']
    assert evidence in seen['read_only_dirs']
    assert '缺少 data/' in seen['prompt']


@pytest.mark.parametrize('temporary_name', ['tmp', '.tmp'])
@pytest.mark.parametrize('checkpoint', [False, True])
def test_codex_native_temporary_links_are_not_results(tmp_path, temporary_name, checkpoint):
    run = tmp_path / 'run'
    source = run / 'workspace/sources/paper'
    external = tmp_path / 'codex-binary'
    external.write_bytes(b'binary')
    homes = [source / 'logs/codex/session', source / 'previous-evidence/previous-evidence/logs/codex/old-session']
    for home in homes:
        temporary = home / temporary_name / 'arg0/launcher'
        temporary.mkdir(parents=True)
        (temporary / 'apply_patch').symlink_to(external)
        (temporary / 'dangling-helper').symlink_to(tmp_path / 'missing-binary')
        sessions = home / 'sessions'
        sessions.mkdir()
        (sessions / 'session.jsonl').write_text('{"type":"token_usage_record"}\n')
    (source / 'logs/usage-events.jsonl').write_text('{"kind":"start"}\n')
    draft_tmp = source / 'draft/tmp'
    draft_tmp.mkdir(parents=True)
    (draft_tmp / 'required.txt').write_text('task artifact')
    destination = tmp_path / 'packed'
    pack_artifacts(run, 'paper', destination, checkpoint=checkpoint)
    restored = tmp_path / 'restored'
    download_archive(LocalFiles(), 'id', str(destination), restored, allowed=lambda _: True)
    for home in homes:
        relative = home.relative_to(run)
        assert not (restored / relative / temporary_name).exists()
        assert (restored / relative / 'sessions/session.jsonl').is_file()
        assert (home / temporary_name / 'arg0/launcher/apply_patch').is_symlink()
    assert (restored / 'workspace/sources/paper/logs/usage-events.jsonl').is_file()
    assert (restored / 'workspace/sources/paper/draft/tmp/required.txt').is_file()


def test_external_links_in_task_outputs_still_fail_with_path(tmp_path):
    run = tmp_path / 'run'
    draft = run / 'workspace/sources/paper/draft/tmp'
    draft.mkdir(parents=True)
    outside = tmp_path / 'private'
    outside.write_text('private')
    (draft / 'answer').symlink_to(outside)
    with pytest.raises(SandboxError, match='draft/tmp/answer'):
        pack_artifacts(run, 'paper', tmp_path / 'packed')
    assert outside.read_text() == 'private'


def test_export_failure_keeps_redacted_diagnostic_before_temp_cleanup(config, tmp_path):
    commands = []
    class Client:
        _password = 'platform-private'
        _username = 'account-private'
        _token = 'session-private'
        def download(self, sandbox, path):
            return b'Traceback: test-token platform-private account-private session-private\nSandboxError: blocked artifact\n'
    class Session:
        id = 'sandbox'
        client = Client()
        def command(self, command):
            commands.append(command)
            return '1\n' if 'then cat' in command else ''
    with pytest.raises(SandboxError, match='blocked artifact'):
        dispatch._export(Session(), tmp_path / 'run', 'paper', config=config)
    log = (tmp_path / 'run/sandbox-workers/paper/export-error.log').read_text()
    assert '<redacted>' in log
    for secret in ['test-token', 'platform-private', 'account-private', 'session-private']:
        assert secret not in log
    assert commands[-1].startswith('rm -rf /tmp/tb-export-')


@pytest.mark.parametrize('remaining_hours,should_stop', [(23,False),(0.5,True)])
def test_failed_renewal_preserves_existing_allocation(config, tmp_path, monkeypatch, remaining_hours, should_stop):
    from datetime import datetime,timedelta,timezone
    class Client:
        base_url,resource='http://platform','pool'
        def renew(self,*args,**kwargs): raise SandboxError('renew endpoint HTTP 500',500)
    monkeypatch.setattr('task_builder.sandbox_session.SandboxClient',Client)
    session=AuthoringSandbox(config,tmp_path/'run','paper',threading.Event())
    session.id='parent'
    expiry=(datetime.now(timezone.utc)+timedelta(hours=remaining_hours)).isoformat()
    session.state['expires_at']=expiry
    class StopAfterAttempt:
        calls=0
        def wait(self,seconds):
            self.calls+=1
            return self.calls>1
    session.stop_renewing=StopAfterAttempt()
    session._renew()
    assert bool(session.renew_error)==should_stop
    assert session.state['expires_at']==expiry
    if not should_stop:
        session.check()
        assert session.state['renew_warning']=='renew endpoint HTTP 500'
