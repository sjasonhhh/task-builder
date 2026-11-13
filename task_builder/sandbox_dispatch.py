"""Host dispatcher: one remotely executed authoring pipeline per paper."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields
from pathlib import Path

from .cli import _positive_int
from .config import build_config, tomllib
from .models import ConfigError, PipelineConfig, PipelineError, StageFailure
from .paths import PROJECT_ROOT, PROJECT_PRICING
from .sandbox_runtime import SandboxClient, SandboxError, sandbox_settings, author_lifetime_minutes
from .sandbox_session import AuthoringSandbox
from .sandbox_transfer import checked, download_archive, upload_tree
from .sandbox_worker import ROOT, PRIVATE

DEFAULT_TASK_BUILDER_IMAGE = (
    "registry.h.pjlab.org.cn/ailab-datatools-data_tools_gpu_pool/shaoyuting"
    "@sha256:9871a689fce9c97bb114ac06c065c45f1d8954a62d60a45343d2ebb51e7b52b0"
)

POLL_SECONDS = 10
CHECKPOINT_SECONDS = 300
PYTHON = str(ROOT / ".venv/bin/python")
_HELPER_LOCK = threading.Lock()
_HELPER_WHEEL = None


def _helper_wheel():
    """Fetch the small pure-Python optional Harbor dependency into memory only."""
    global _HELPER_WHEEL
    with _HELPER_LOCK:
        if _HELPER_WHEEL is None:
            with urllib.request.urlopen("https://pypi.org/pypi/dockerfile-parse/2.0.1/json", timeout=30) as response:
                package = json.loads(response.read(1024 * 1024))
            wheels = [entry for entry in package["urls"] if entry["packagetype"] == "bdist_wheel"
                      and entry["filename"].endswith("-none-any.whl")]
            if len(wheels) != 1:
                raise SandboxError("找不到 dockerfile-parse 2.0.1 的通用安装包")
            wheel = wheels[0]
            address = urllib.parse.urlsplit(wheel["url"])
            if address.scheme != "https" or address.hostname != "files.pythonhosted.org":
                raise SandboxError("依赖下载地址不是 PyPI 官方文件服务")
            with urllib.request.urlopen(wheel["url"], timeout=60) as response:
                content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024 or hashlib.sha256(content).hexdigest() != wheel["digests"]["sha256"]:
                raise SandboxError("Dockerfile 解析依赖校验失败")
            _HELPER_WHEEL = content
        return _HELPER_WHEEL


def _prepare_helper(session):
    if session.client.command(session.id, f"{PYTHON} -c 'import dockerfile_parse' >/dev/null 2>&1").return_code == 0:
        return
    # Some orchestration venvs have neither pip nor DockerfileParser. Upload a
    # checksum-verified pure-Python wheel; install nothing on the local machine.
    wheel = _helper_wheel()
    session.client.upload(session.id, wheel, str(PRIVATE / "helper.whl"))
    script = '''from pathlib import PurePosixPath
from zipfile import ZipFile
import sys
with ZipFile(sys.argv[1]) as archive:
    for name in archive.namelist():
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts: raise ValueError('invalid wheel path')
    archive.extractall(sys.argv[2])
'''
    session.command(f"{PYTHON} -c {shlex.quote(script)} {PRIVATE}/helper.whl {PRIVATE}/python && "
                    f"PYTHONPATH={PRIVATE}/python {PYTHON} -c 'import dockerfile_parse' && rm {PRIVATE}/helper.whl")


def build_parser():
    parser = argparse.ArgumentParser(description="每篇论文使用独立沙盒造任务", allow_abbrev=False)
    for name, help_text in (
        ("sci-env", "领域名；读取对应 image-env.toml 的实验、评测镜像、CPU 和内存"),
        ("input", "论文材料根目录"), ("output", "结果根目录"),
        ("author-env", "直连模型配置文件，如 dpinfer.env 或 free-router.env"),
    ):
        parser.add_argument("--" + name, required=True, help=help_text)
    parser.add_argument("--task-builder-image", help="只更换造题沙盒镜像；所有领域默认使用固定造题镜像，与实验、评测镜像无关")
    parser.add_argument("--codex-bin", default="codex", help="待传送到造题沙盒的本机 Codex 程序")
    parser.add_argument("--image-env")
    parser.add_argument("--environment-root")
    parser.add_argument("--experiment-image")
    parser.add_argument("--evaluation-image")
    parser.add_argument("--run-txt", "--run_txt", dest="run_txt")
    parser.add_argument("--resume-from")
    parser.add_argument("--human-review", action="store_true")
    parser.add_argument("--max-revisions", type=int, default=3)
    parser.add_argument("--concurrency", type=_positive_int, default=1, help="同时运行的造任务主沙盒数量")
    parser.add_argument("--agent-timeout-sec", type=_positive_int)
    parser.add_argument("--harbor-timeout-sec", type=_positive_int)
    parser.add_argument("--task-timeout-sec", type=_positive_int, default=19800)
    parser.add_argument("--dry-run", action="store_true", help="只显示镜像和资源配置，不创建沙盒")
    return parser


def dispatch_config(values):
    values = dict(values)
    image = values.pop("task_builder_image", None)
    if image is not None:
        image = image.strip()
        if not image:
            raise ConfigError("--task-builder-image 不能为空")
    values.pop("dry_run", None)
    values["input_root"] = values.pop("input")
    values["output_root"] = values.pop("output")
    config = build_config(**values, sandbox=True)
    table = tomllib.loads(config.image_env_path.read_text()).get("hcluster", {})
    for key in ("max_cpu", "max_memory_mb"):
        value = table.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"造任务沙盒要求 image-env.toml 明确填写 [hcluster].{key}")
    config.extra.update(sandbox_dispatch=True, sandbox_author_image=image if image is not None else DEFAULT_TASK_BUILDER_IMAGE)
    return config


def _entries_and_job(source, config, run_root, sandbox_id):
    remote_output = ROOT / "output" / config.sci_env
    remote_run = remote_output / "runs" / run_root.name
    remote_env = ROOT / "image" / config.sci_env
    values = {}
    for field in fields(PipelineConfig):
        value = getattr(config, field.name)
        if field.name == "author_env":
            continue
        values[field.name] = str(value) if isinstance(value, Path) else value
    values.update(input_root=str(ROOT / "inputs" / config.sci_env), output_root=str(remote_output),
                  environment_dir=str(remote_env), image_env_path=str(remote_env / "image-env.toml"),
                  rubric_path=str(ROOT / "config/rubric.md"), run_txt=None, resume_from=None,
                  concurrency=1, model_relay=None, claude_bin="claude", codex_bin="codex", extra={})
    # Only this paper's human feedback travels to its sandbox.
    for key in ("human_review_requests", "publication_expected"):
        if source.source_slug in config.extra.get(key, {}):
            values["extra"][key] = {source.source_slug: config.extra[key][source.source_slug]}
    entries = []
    for path in sorted((PROJECT_ROOT / "task_builder").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".md"} and "__pycache__" not in path.parts:
            entries.append((path, str(path.relative_to(PROJECT_ROOT))))
    for name in ("requirements.txt", "setup_env.sh", "run.sh"):
        entries.append((PROJECT_ROOT / name, name))
    entries.append((config.rubric_path, "config/rubric.md"))
    if PROJECT_PRICING.is_file():
        entries.append((PROJECT_PRICING, "config/pricing.toml"))
    from .workspace import ENVIRONMENT_FILES
    for name in (*ENVIRONMENT_FILES, "human_advise.md"):
        path = config.image_env_path if name == "image-env.toml" else config.environment_dir / name
        if path.is_file():
            entries.append((path, f"image/{config.sci_env}/{name}"))
    for name in ("context", "data", "code"):
        path = getattr(source, name)
        if path.is_dir():
            entries.append((path, f"inputs/{config.sci_env}/{source.source_dir.name}/{name}"))
    previous = config.resume_from
    if source.source_slug in config.extra.get("human_review_requests", {}):
        from .human_review import _find_run
        target = config.output_root / "final" / source.source_slug
        previous = _find_run(source, config, target)
        entries.append((target, f"output/{config.sci_env}/final/{source.source_slug}"))
    if previous:
        values["resume_from"] = str(remote_output / "runs" / previous.name)
        old = previous / "workspace/sources" / source.source_slug
        if old.is_dir():
            for path in sorted(old.iterdir()):
                if path.name in {"plan", "draft", "logs", "reviews", "failures", "previous-evidence",
                                 "recovery", ".repair-candidates", ".artifact-export.json", ".history-files.json"}:
                    entries.append((path, f"output/{config.sci_env}/runs/{previous.name}/workspace/sources/{source.source_slug}/{path.name}"))
            for path in sorted((old / "validation").rglob("*")):
                if path.is_file() and "data" not in path.relative_to(old / "validation").parts and path.suffix in {".json", ".log", ".txt", ".jsonl"}:
                    entries.append((path, f"output/{config.sci_env}/runs/{previous.name}/workspace/sources/{source.source_slug}/{path.relative_to(old)}"))
    job = {"config": values, "source_name": source.source_dir.name, "slug": source.source_slug,
           "run_root": str(remote_run), "sandbox_id": sandbox_id}
    return entries, job


def _env_bytes(values):
    return ("\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n").encode()


def _prepare(session, source, config, run_root):
    entries, job = _entries_and_job(source, config, run_root, session.id)
    session.save(phase="uploading", remote_run=job["run_root"])
    transfer = upload_tree(session.client, session.id, entries, str(ROOT), session.cancelled)
    session.save(last_upload=transfer)
    session.check()
    session.command(f"mkdir -p {PRIVATE} && chmod 700 {PRIVATE}")
    settings = sandbox_settings()
    model_values = {key: value for key, value in config.author_env.values.items()
                    if key not in {"AD_USERNAME", "AD_PASSWORD", "OPEN_SANDBOX_GATEWAY_TOKEN"}
                    and key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
                    and not key.startswith("TASK_BUILDER_")}
    for name, data in (("job.json", json.dumps(job).encode()), ("sandbox.env", _env_bytes(settings)),
                       ("model.env", _env_bytes(model_values))):
        session.client.upload(session.id, data, str(PRIVATE / name))
    session.command(f"chmod 600 {PRIVATE}/*")
    agent = config.author_env.agent
    if agent == "codex":
        from .agent_runtime import runtime_files
        files = runtime_files(agent, config.codex_bin)
        upload_tree(session.client, session.id, [(source_path, str(Path(destination).relative_to('/usr/local'))) for source_path, destination in files],
                    "/usr/local", session.cancelled)
        for source_path, destination in files:
            session.command(f"chmod 755 {shlex.quote(destination)}")
        from .codex_sandbox import ensure_codex_sandbox
        ensure_codex_sandbox(session, PRIVATE)
    program = "codex" if agent == "codex" else "claude"
    # Reuse the prepared image. Never install or build scientific software locally.
    # Linking first makes setup_env fail rather than silently install dependencies.
    try:
        session.command(f"test -x /opt/orch/venv/bin/python && test -x /opt/orch/venv/bin/harbor && "
                        f"{program} --version >/dev/null && command -v setsid >/dev/null && "
                        f"ln -s /opt/orch/venv {ROOT}/.venv && cd {ROOT} && bash setup_env.sh")
    except SandboxError:
        raise SandboxError(f"造任务镜像缺少可用的 {program} 或 /opt/orch/venv（Python 3.12、Harbor 及项目补丁）；请用 --task-builder-image 指定已准备好的镜像") from None
    session.save(phase="checking-dependencies")
    try:
        _prepare_helper(session)
    except Exception:
        raise SandboxError("远端 Dockerfile 解析依赖准备失败；开发机需要访问 PyPI 以传送小型依赖，或使用已包含 dockerfile-parse 的造任务镜像") from None
    remote_run = shlex.quote(job["run_root"])
    session.command(f"mkdir -p {remote_run}")
    wrapper = (f"#!/bin/bash\ncd {ROOT} || exit 1\n"
               f"export PYTHONPATH={ROOT}:{PRIVATE}/python\n"
               f"echo $$ >{PRIVATE}/pid\n"
               f"{PYTHON} -m task_builder.sandbox_worker >{remote_run}/worker.log 2>&1\n"
               f"rc=$?\nprintf '%s' \"$rc\" >{PRIVATE}/exit.tmp\nmv {PRIVATE}/exit.tmp {PRIVATE}/exit\n")
    session.client.upload(session.id, wrapper.encode(), str(PRIVATE / "launch.sh"))
    return job


def _launch(session):
    # A timed-out HTTP response must not start a second copy of the pipeline.
    session.command(f"if mkdir {PRIVATE}/claimed 2>/dev/null; then "
                    f"setsid bash {PRIVATE}/launch.sh </dev/null >/dev/null 2>&1 & fi", retry=True)
    session.save(phase="working")


def _worker_status(session):
    # Reading a marker is safe to retry after a transient gateway error. Do not
    # stop a healthy authoring process merely because one poll received 502.
    return session.command(
        f"if test -f {PRIVATE}/exit; then cat {PRIVATE}/exit; else echo running; fi",
        retry=True,
    ).strip()


def _stop(session):
    # Claude and Harbor start their own process groups. Stop descendants and
    # tagged orphan processes too, before exporting files or cleaning children.
    script = '''import os,signal,sys,time
from pathlib import Path
pidfile=Path(sys.argv[1])
roots={int(pidfile.read_text())} if pidfile.is_file() else set()
tag=('TASK_BUILDER_SANDBOX_PARENT='+sys.argv[2]).encode()
parents={}
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit(): continue
    pid=int(entry.name)
    if pid in (1,os.getpid(),os.getppid()): continue
    try:
        fields=(entry/'stat').read_text().rsplit(')',1)[1].split()
        parents[pid]=int(fields[1])
        if tag in (entry/'environ').read_bytes().split(b'\\0'): roots.add(pid)
    except (OSError,ValueError): pass
while True:
    expanded=roots|{pid for pid,parent in parents.items() if parent in roots}
    if expanded==roots: break
    roots=expanded
for sig in (signal.SIGTERM,signal.SIGKILL):
    for pid in sorted(roots,reverse=True):
        if pid in (1,os.getpid(),os.getppid()): continue
        try: os.kill(pid,sig)
        except ProcessLookupError: pass
    if sig==signal.SIGTERM: time.sleep(2)
'''
    session.command(f"{PYTHON} -c {shlex.quote(script)} {PRIVATE}/pid {shlex.quote(session.id)}")


def _export(session, run_root, slug, *, checkpoint=False, config=None):
    remote = "/tmp/tb-export-" + uuid.uuid4().hex
    flag = " --checkpoint" if checkpoint else ""
    # Export may exceed one gateway request; run it once, then poll a small marker.
    command = (f"mkdir {remote} && cd {ROOT} && "
               f"({PYTHON} -m task_builder.sandbox_worker --export {remote}{flag} "
               f">{remote}/export.log 2>&1; echo $? >{remote}/done) </dev/null >/dev/null 2>&1 &")
    session.command(command)
    end = time.monotonic() + (180 if checkpoint else 1800)
    try:
        while time.monotonic() < end:
            status = session.command(f"if test -f {remote}/done; then cat {remote}/done; else echo pending; fi").strip()
            if status != "pending":
                if status != "0":
                    # Keep the real failure before finally removes the temporary
                    # export directory; otherwise every failure looks identical.
                    detail = ""
                    try:
                        log = session.client.download(session.id, remote + "/export.log").decode("utf-8", errors="replace")
                        if config is not None:
                            from .model_relay import redact_model_secrets
                            log = redact_model_secrets(log, config)
                        for name in ("_username", "_password", "_token"):
                            secret = getattr(session.client, name, "")
                            if secret:
                                log = log.replace(secret, "<redacted>")
                        if config is not None:
                            records = run_root / "sandbox-workers" / slug
                            records.mkdir(parents=True, exist_ok=True)
                            (records / "export-error.log").write_text(log, encoding="utf-8")
                            detail = log.strip().splitlines()[-1] if log.strip() else ""
                    except (OSError, SandboxError):
                        pass
                    raise SandboxError("结果打包失败，主沙盒保留等待恢复" + (f"：{detail}" if detail else ""))
                break
            time.sleep(2)
        else:
            raise SandboxError("结果打包超时，主沙盒保留等待恢复")
        def allowed(path):
            return (path.parts[:3] == ("workspace", "sources", slug)
                    or (not checkpoint and path.parts[:2] == ("published", slug))
                    or str(path) in {"worker-result.json", "worker.log"})
        destination = run_root / "checkpoints" / slug if checkpoint else run_root
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".sandbox-result-", dir=run_root) as temp:
            extracted = Path(temp) / "contents"
            transfer = download_archive(session.client, session.id, remote, extracted, allowed=allowed)
            for name in ("workspace/sources/" + slug, "published/" + slug):
                path = extracted / name
                if path.is_dir():
                    target = destination / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        shutil.rmtree(target)
                    path.rename(target)
            records = destination / "sandbox-workers" / slug
            records.mkdir(parents=True, exist_ok=True)
            for name in ("worker-result.json", "worker.log"):
                path = extracted / name
                if path.is_file():
                    path.replace(records / name)
        session.save(last_checkpoint=time.time() if checkpoint else session.state.get("last_checkpoint"),
                     artifacts_received=not checkpoint, last_download=transfer)
    finally:
        try:
            session.command(f"rm -rf {remote}")
        except SandboxError:
            pass


def _dispatch_one(source, config, run_root, cancelled):
    session = AuthoringSandbox(config, run_root, source.source_slug, cancelled)
    launched, received = False, False
    error = None
    transfer_error = None
    transfer_stage = "stop-worker"
    try:
        session.start()
        _prepare(session, source, config, run_root)
        session.check()
        launched = True
        _launch(session)
        end = time.monotonic() + config.task_timeout_sec + 120
        checkpoint_at = time.monotonic() + CHECKPOINT_SECONDS
        while True:
            session.check()
            status = _worker_status(session)
            if status != "running":
                break
            if time.monotonic() >= end:
                raise SandboxError("造任务达到本篇论文的时间上限")
            if time.monotonic() >= checkpoint_at:
                try:
                    _export(session, run_root, source.source_slug, checkpoint=True, config=config)
                except Exception as exc:
                    session.save(checkpoint_failed=True, checkpoint_error=str(exc))
                checkpoint_at = time.monotonic() + CHECKPOINT_SECONDS
                print(f"{source.source_slug}：造任务沙盒仍在运行，状态记录在 {session.path}", flush=True)
            cancelled.wait(POLL_SECONDS)
    except BaseException as exc:
        error = exc
        session.save(dispatch_error={"type": type(exc).__name__, "message": str(exc)})
    finally:
        if launched:
            try:
                _stop(session)
                transfer_stage = "export-artifacts"
                # Retry final transfer while the only remote copy still exists.
                for attempt in range(3):
                    try:
                        _export(session, run_root, source.source_slug, config=config)
                        received = True
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(5)
            except Exception as exc:
                transfer_error = exc
                session.save(transfer_error={"stage": transfer_stage, "type": type(exc).__name__, "message": str(exc)})
        try:
            session.close(retain=launched and not received)
        except Exception as exc:
            session.save(cleanup_error=str(exc))
            error = error or exc
    if transfer_error:
        if session.state.get("phase") == "missing":
            recovery = "平台已无可用主沙盒，只能复用本地备份"
        elif session.state.get("retained"):
            recovery = "已确认主沙盒仍在，保留等待恢复"
        else:
            recovery = "主沙盒是否保留尚未确认，请查看状态记录"
        reason = f"；最初错误：{error}" if error else ""
        raise SandboxError(f"结果回传未完成：{transfer_error}{reason}；{recovery}；恢复时使用 --resume-from {run_root}") from transfer_error
    if error:
        raise error
    result_path = run_root / "sandbox-workers" / source.source_slug / "worker-result.json"
    if not result_path.is_file():
        raise SandboxError("远程进程退出但没有生成有效结果；日志已回传")
    result = json.loads(result_path.read_text())
    if result.get("slug") != source.source_slug or not result.get("ok"):
        raise StageFailure(result.get("error", "造任务失败，详见远程日志"), stage=result.get("stage"))
    candidate = run_root / "published" / source.source_slug
    from .pipeline import is_complete_published_task
    if not is_complete_published_task(candidate):
        raise SandboxError("远程报告成功，但回传任务不完整，未发布")
    from .usage import write_source_usage
    write_source_usage(run_root / "workspace/sources" / source.source_slug, config.output_root, source.source_slug)
    return candidate


def run_sources_in_sandboxes(sources, config, run_root, staging_output):
    del staging_output
    from .human_review import review_manifest, publication_lock
    from .pipeline import _save_source_exception
    (run_root / "sandbox-dispatch.json").write_text(json.dumps({"sources": [s.source_slug for s in sources]}))
    expected = config.extra.setdefault("publication_expected", {})
    for source in sources:
        if source.source_slug not in expected:
            with publication_lock(config.output_root, source.source_slug):
                target = config.output_root / "final" / source.source_slug
                expected[source.source_slug] = review_manifest(target) if target.exists() else None
    cancelled = threading.Event()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, lambda *_: cancelled.set())
    outputs, failures = {}, {}
    try:
        with ThreadPoolExecutor(max_workers=min(config.concurrency, len(sources))) as executor:
            futures = {executor.submit(_dispatch_one, source, config, run_root, cancelled): source for source in sources}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    outputs[source.source_slug] = future.result()
                    print(f"{source.source_slug}：结果已回传，沙盒已清理", flush=True)
                except Exception as exc:
                    _save_source_exception(source, run_root, exc)
                    failures[source.source_slug] = f"{source.source_slug}: {exc}"
                    print(failures[source.source_slug], file=sys.stderr, flush=True)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    return ([outputs[s.source_slug] for s in sources if s.source_slug in outputs],
            [failures[s.source_slug] for s in sources if s.source_slug in failures])


def recover_retained(config):
    """Explicit resume recovers remote artifacts before selecting/publishing tasks."""
    previous = config.resume_from
    if previous is None:
        return
    selected = None
    if config.run_txt:
        from .discovery import discover_sources, read_run_names
        names = set(read_run_names(config.run_txt))
        selected = {source.source_slug for source in discover_sources(config.input_root, validate=False)
                    if source.source_dir.name in names}
    for path in sorted((previous / "sandbox-workers").glob("*.json")):
        state = json.loads(path.read_text())
        if selected is not None and state.get("source") not in selected:
            continue
        if state.get("phase") == "deleted":
            continue
        client = SandboxClient()
        if state.get("base_url") != client.base_url or state.get("resource") != client.resource:
            raise ConfigError("恢复记录与当前沙盒平台或资源池不一致")
        if not state.get("sandbox_id"):
            if state.get("phase") not in {"creating", "create-uncertain"}:
                continue
            found = client.find_created(state["name"], time.monotonic() + 120)
            if len(found) != 1:
                raise SandboxError("旧主沙盒创建状态仍未确认，未重复申请；记录: " + str(path))
            state["sandbox_id"] = found[0]["id"]
        session = AuthoringSandbox.__new__(AuthoringSandbox)
        session.client, session.id, session.name = client, state["sandbox_id"], state["name"]
        session.path, session.state, session.lock = path, state, threading.Lock()
        session.cancelled, session.stop_renewing = threading.Event(), threading.Event()
        session.renew_thread, session.renew_error = None, None
        slug = state["source"]
        try:
            detail = client.detail(session.id)
        except SandboxError as exc:
            if exc.status != 404:
                raise
            detail = {"state": "Deleted"}
        platform_state = (detail.get("status") or {}).get("state") or detail.get("state")
        if platform_state in {"Deleted", "Terminated", "Failed", "Error"}:
            checkpoint = previous / "checkpoints" / slug / "workspace/sources" / slug
            target = previous / "workspace/sources" / slug
            if not (target / "plan/plan.json").is_file() and checkpoint.is_dir():
                # Checkpoints omit large data. Reuse their plan and evidence, but
                # make the writer rebuild a complete draft before validation.
                for item in checkpoint.iterdir():
                    if item.name in {"draft", ".repair-candidates"}:
                        dest = target / "previous-evidence" / ("checkpoint-" + item.name.lstrip("."))
                    else:
                        dest = target / item.name
                    if item.is_dir():
                        shutil.copytree(item, dest, dirs_exist_ok=True)
            session.close()
            print(f"{slug}：旧主沙盒已结束，将复用本地可用材料并重新验证", flush=True)
            continue
        if not state.get("remote_run"):
            session.close()
            continue
        session.renew_once()
        try:
            _stop(session)
            _export(session, previous, slug, config=config)
        except Exception as exc:
            session.save(transfer_error={"type": type(exc).__name__, "message": str(exc)})
            session.close(retain=True)
            raise SandboxError(f"旧沙盒结果尚未恢复：{exc}；未开始新的造任务；记录: {path}") from exc
        session.close()


def main(argv=None):
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg == "--image" or arg.startswith("--image=") for arg in argv):
        parser.error("--image 已移除；请使用 --task-builder-image 指定造题镜像")
    args = parser.parse_args(argv)
    try:
        config = dispatch_config(vars(args))
        if args.dry_run:
            print(json.dumps({"author_image": config.extra["sandbox_author_image"],
                              "experiment_image": config.experiment_image, "evaluation_image": config.evaluation_image,
                              "cpu": config.cpu, "memory_mb": config.memory_mb,
                              "concurrency": config.concurrency, "models": "direct", "backend": "sandbox",
                              "task_timeout_sec": config.task_timeout_sec,
                              "author_lifecycle_minutes": author_lifetime_minutes(config.task_timeout_sec)},
                             ensure_ascii=False, indent=2))
            return 0
        recover_retained(config)
        from .pipeline import run_pipeline
        for output in run_pipeline(config):
            print(output)
        return 0
    except (ConfigError, PipelineError, SandboxError, OSError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
