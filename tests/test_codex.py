from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from task_builder.agent_config import codex_base_url, codex_config_arguments, codex_settings
from task_builder.agent_runtime import resolve_codex_source, runtime_files
from task_builder.agents import create_agent, save_agent_log
from task_builder.codex_agent import IndependentCodexAgent, adapt_prompt, parse_codex_events
from task_builder.config import environment_for_subprocess, load_agent_env
from task_builder.models import AgentError, ConfigError, EnvConfig
from task_builder.model_relay import relay_module


def events(*, failed=False):
    data = [
        {"type": "thread.started", "thread_id": "codex-session"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"pass":true}'}},
        {"type": "turn.failed", "error": {"message": "bad-token"}} if failed else
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 7}},
    ]
    return "\n".join(map(json.dumps, data))


@pytest.mark.parametrize("style", ["legacy", "openai"])
def test_codex_config_and_clean_child_environment(tmp_path, monkeypatch, style):
    base, token = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN") if style == "legacy" else ("OPENAI_BASE_URL", "OPENAI_API_KEY")
    path = tmp_path / "a.env"
    original = f'AGENT="codex"\nMODEL_ID="gpt-6.1-sol"\n{base}="https://example.test"\n{token}="my-token"\nREASONING_EFFORT="high"\n'
    path.write_text(original)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-token")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "other-token")
    monkeypatch.setenv("AD_PASSWORD", "private-password")
    config = load_agent_env(path, role="test")
    assert config.agent == "codex"
    child = environment_for_subprocess(config)
    assert child["OPENAI_API_KEY"] == "my-token"
    assert child["OPENAI_BASE_URL"] == "https://example.test"
    assert child["REASONING_EFFORT"] == "high"
    assert not {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "AD_PASSWORD"} & child.keys()
    assert path.read_text() == original


def test_invalid_agent_fails_before_credentials(tmp_path):
    path = tmp_path / "bad.env"
    path.write_text("AGENT=wrong\n")
    with pytest.raises(ConfigError, match="AGENT"):
        load_agent_env(path, role="test")


@pytest.mark.parametrize("role", ["writer", "repair-writer", "repair-writer-files-1"])
def test_calibration_allowed_only_for_remote_writing_roles(role):
    remote = adapt_prompt("保存任务", role=role, remote_authoring=True)
    assert "允许在草稿临时目录执行" in remote
    assert "不能代替 rubric 要求的原文依据" in remote
    assert "不得运行科研计算" not in remote
    local = adapt_prompt("保存任务", role=role, remote_authoring=False)
    assert "不得运行科研计算" in local
    assert "允许在草稿临时目录执行" not in local


@pytest.mark.parametrize("role", ["planner", "reviewer", "reviewer-cycle-2", ""])
def test_remote_calibration_does_not_change_planning_or_review_scope(role):
    prompt = adapt_prompt("审查材料", role=role, remote_authoring=True)
    assert "不得运行科研计算" in prompt
    assert "允许在草稿临时目录执行" not in prompt


def test_events_usage_and_terminal_failure():
    text, session, usage, error = parse_codex_events(events())
    assert json.loads(text) == {"pass": True}
    assert session == "codex-session" and not error
    assert usage["input_tokens"] == 70
    assert usage["cache_read_input_tokens"] == 30
    assert usage["output_tokens"] == 7
    _, _, written, _ = parse_codex_events(events().replace('"cached_input_tokens": 30', '"cached_input_tokens": 30, "cache_write_input_tokens": 10'))
    assert written["input_tokens"] == 60 and written["cache_creation_input_tokens"] == 10
    assert parse_codex_events(events(failed=True))[3] == "bad-token"
    assert parse_codex_events(events() + '\n{"type":"turn.failed","error":{"message":"terminal"}}')[3] == "terminal"
    assert parse_codex_events('{"type":"turn.started"}\npartial')[3]


@pytest.mark.parametrize("role", ["planner", "writer", "reviewer-cycle-0", "repair-writer"])
@pytest.mark.parametrize("remote", [False, True])
def test_codex_command_resume_permissions_and_logs(tmp_path, monkeypatch, role, remote):
    if remote:
        monkeypatch.setenv("TASK_BUILDER_SANDBOX_PARENT", "test-author-sandbox")
    else:
        monkeypatch.delenv("TASK_BUILDER_SANDBOX_PARENT", raising=False)
    calls = []
    source = tmp_path / "source"
    source.mkdir()
    config = EnvConfig(tmp_path / "a.env", "gpt-6.1-sol", {
        "ANTHROPIC_BASE_URL": "https://example.test", "ANTHROPIC_AUTH_TOKEN": "bad-token", "REASONING_EFFORT": "high",
    }, agent="codex")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, events(), "bad-token")

    monkeypatch.setattr("task_builder.codex_agent.run_process", run)
    agent = IndependentCodexAgent(codex_bin="custom-codex")
    options = dict(role=role, prompt="先 Read 再 Write", workdir=tmp_path / "work", env_config=config,
                   allowed_dirs=(source,), read_only_dirs=(source,))
    result = agent.run(**options)
    agent.run(**options, resume_session=result.session_id)
    command, kwargs = calls[0]
    assert command[0] == "custom-codex"
    assert command[command.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in command
    assert ('sandbox_mode="read-only"' if role.startswith("reviewer") else 'sandbox_mode="workspace-write"') in command
    assert "--add-dir" not in command and "--settings" not in command
    assert "Read" not in kwargs["input"] and "Write" not in kwargs["input"]
    assert ("允许在草稿临时目录执行" in kwargs["input"]) == (remote and role in {"writer", "repair-writer"})
    assert kwargs["env"]["OPENAI_API_KEY"] == "bad-token"
    assert calls[1][0][1:4] == ["exec", "resume", "codex-session"]
    assert calls[1][1]["env"]["CODEX_HOME"] == kwargs["env"]["CODEX_HOME"]
    state_home = Path(kwargs["env"]["CODEX_SQLITE_HOME"])
    assert state_home.is_relative_to(Path("/tmp")) and state_home.is_dir()
    assert calls[1][1]["env"]["CODEX_SQLITE_HOME"] == str(state_home)
    assert result.stderr == "<redacted>"
    log = tmp_path / "logs" / "agent.log"
    save_agent_log(log, result)
    assert log.with_suffix(".events.jsonl").is_file()
    assert "bad-token" not in log.read_text()
    assert json.loads((log.parent / "usage.jsonl").read_text())["model"] == "gpt-6.1-sol"


def test_codex_failure_with_zero_exit_and_timeout(tmp_path, monkeypatch):
    config = EnvConfig(tmp_path / "a.env", "model", {"OPENAI_BASE_URL": "https://example.test", "OPENAI_API_KEY": "bad-token"}, "codex")
    def failed(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, events(failed=True), "")
    monkeypatch.setattr("task_builder.codex_agent.run_process", failed)
    with pytest.raises(AgentError) as error:
        IndependentCodexAgent().run(role="writer", prompt="save", workdir=tmp_path / "work", env_config=config)
    assert error.value.run.returncode == 1
    assert "bad-token" not in str(error.value)
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1, output=events().encode(), stderr=b"bad-token")
    monkeypatch.setattr("task_builder.codex_agent.run_process", timeout)
    with pytest.raises(AgentError) as error:
        IndependentCodexAgent().run(role="writer", prompt="save", workdir=tmp_path / "work", env_config=config)
    assert error.value.run.session_id == "codex-session"
    assert error.value.run.stderr == "<redacted>"


def test_native_program_and_auxiliary_files(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    wrapper = root / "bin/codex.js"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env node")
    vendor = root / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl"
    binary = vendor / "bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o755)
    rg = vendor / "codex-path/rg"
    rg.parent.mkdir()
    rg.write_bytes(b"\x7fELF")
    host = binary.with_name("codex-code-mode-host")
    host.write_bytes(b"\x7fELF")
    bwrap = vendor / "codex-resources/bwrap"
    bwrap.parent.mkdir()
    bwrap.write_bytes(b"\x7fELF")
    monkeypatch.delenv("TASK_BUILDER_CODEX_SOURCE", raising=False)
    monkeypatch.setattr("task_builder.agent_runtime.platform.machine", lambda: "x86_64")
    assert resolve_codex_source(str(wrapper)) == binary
    assert runtime_files("codex", str(wrapper)) == [(binary, "/usr/local/bin/codex"),
        (host, "/usr/local/bin/codex-code-mode-host"), (bwrap, "/usr/local/codex-resources/bwrap"), (rg, "/usr/local/bin/rg")]
    assert runtime_files("nop") == runtime_files("oracle") == []


@pytest.mark.parametrize("upstream_path", ["", "/v1", "/api/v1", "/custom"])
def test_relay_codex_credentials_and_v1_join(tmp_path, upstream_path):
    relay = relay_module()
    entry = {"agent": "codex", "base_url": "http://relay.test:1000", "token": "temporary", "model": "gpt-6.1-sol", "reasoning_effort": "high"}
    env = relay.proxy_environment(entry, {"ANTHROPIC_AUTH_TOKEN": "original", "OPENAI_API_KEY": "original"})
    assert env["OPENAI_BASE_URL"] == "http://relay.test:1000/v1"
    assert env["OPENAI_API_KEY"] == "temporary"
    assert env["REASONING_EFFORT"] == "high"
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    from urllib.parse import urlsplit
    owner = SimpleNamespace(entry=SimpleNamespace(agent="codex"), upstream=urlsplit("https://model.test" + upstream_path), timeout=30, proxy=None)
    # Inspect the URL joining without a network request.
    connection, target = relay.APIService.upstream_connection(owner, "/v1/responses", "")
    connection.close()
    assert target == (upstream_path or "/v1") + "/responses"


def test_settings_are_valid_toml_and_no_key_in_command():
    import tomllib
    args = codex_config_arguments(codex_settings("https://example.test", "high"))
    for value in args[1::2]:
        tomllib.loads(value)
    assert codex_base_url("https://example.test/v1/") == "https://example.test/v1"
    assert codex_base_url("https://example.test") == "https://example.test/v1"
    assert 'model_supports_reasoning_summaries=true' in args


@pytest.mark.parametrize("valid_digest", [True, False])
def test_compatible_runtime_verifies_and_removes_download(tmp_path, monkeypatch, valid_digest):
    import base64
    import hashlib
    import io
    import tarfile
    from task_builder import codex_sandbox as runtime
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("codex/codex", "path/rg"):
            entry = tarfile.TarInfo("package/vendor/x86_64-unknown-linux-musl/" + name)
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b"\x7fELF"))
    content = buffer.getvalue()
    digest = base64.b64encode(hashlib.sha512(content if valid_digest else b"incorrect").digest()).decode()
    metadata = {"dist": {"tarball": "https://registry.npmjs.org/@openai/codex/-/codex-0.114.0-linux-x64.tgz",
                         "integrity": "sha512-" + digest}}
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda url, **kw: io.BytesIO(content if url.endswith('.tgz') else json.dumps(metadata).encode()))
    if not valid_digest:
        with pytest.raises(runtime.SandboxError, match="校验失败"):
            with runtime.compatible_runtime():
                pytest.fail("corrupt runtime accepted")
        return
    with runtime.compatible_runtime() as files:
        assert [destination for _, destination in files] == ["bin/codex", "bin/rg"]
        assert all(path.read_bytes() == b"\x7fELF" for path, _ in files)
    assert all(not path.exists() for path, _ in files)


def test_container_fallback_is_limited_to_bwrap_errors(monkeypatch, tmp_path):
    from task_builder import codex_sandbox as runtime
    result = SimpleNamespace(return_code=1, stdout="", stderr="unrelated failure")
    monkeypatch.setattr(runtime, "sandbox_probe", lambda *args: result)
    with pytest.raises(runtime.SandboxError, match="未启动造题"):
        runtime.ensure_codex_sandbox(object(), tmp_path)


def test_relay_forwards_responses_without_buffering():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.request import Request, build_opener, ProxyHandler
    relay = relay_module()
    release = threading.Event()
    observed = []
    first_event = b'event: response.created\ndata: {"type":"response.created"}\n\n'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            observed.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(first_event)
            self.wfile.flush()
            release.wait(5)
            self.wfile.write(b'data: done\n\n')
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    entry = relay.Entry("codex", Path("model.env"), f"http://127.0.0.1:{upstream.server_port}/v1", "bearer", "upstream-secret", "gpt-6.1-sol", agent="codex")
    service = relay.APIService(entry, bind_host="127.0.0.1", advertised_host="127.0.0.1", service_id="test-service", timeout=5, proxy_config={"upstream_proxy": None})
    service.start()
    payload = {"model": "gpt-6.1-sol", "reasoning": {"effort": "high"}, "stream": True}
    try:
        info = service.connection_info()
        request = Request(info["base_url"] + "/v1/responses", data=json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + info["token"], "Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            assert response.read(len(first_event)) == first_event
            release.set()
            assert response.read() == b'data: done\n\n'
        assert observed == [("/v1/responses", "Bearer upstream-secret", payload)]
    finally:
        release.set()
        service.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=3)


def test_harbor_uses_codex_model_connection_and_effort(tmp_path, monkeypatch):
    from task_builder import harbor_runtime
    from task_builder.models import PipelineConfig, StageFailure
    env_file = tmp_path / "a.env"
    env_file.write_text('AGENT=codex\nMODEL_ID=gpt-6.1-sol\nANTHROPIC_BASE_URL=https://example.test\nANTHROPIC_AUTH_TOKEN=private-key\nREASONING_EFFORT=high\n')
    env = load_agent_env(env_file, role="test")
    config = PipelineConfig(input_root=tmp_path, output_root=tmp_path / "out", author_env=env,
                            experiment_image="experiment", evaluation_image="evaluation",
                            image_env_path=tmp_path / "image.toml", sandbox=True, extra={"codex_source": "/native/codex"})
    captured = {}
    def execute(command, **kwargs):
        captured.update(command=command, env=kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "done")
    monkeypatch.setattr(harbor_runtime.subprocess, "run", execute)
    with pytest.raises(StageFailure, match="评分"):
        harbor_runtime.run_harbor_agent(tmp_path / "task", config, agent="codex", env_file=env_file, model=env.model_id, job_name="agent")
    cmd = captured["command"]
    assert cmd[cmd.index("--agent") + 1] == "codex"
    assert cmd[cmd.index("--model") + 1] == "gpt-6.1-sol"
    assert "reasoning_effort=high" in cmd
    assert "OPENAI_API_KEY=${TASK_BUILDER_PROXY_TOKEN}" in cmd
    assert "ANTHROPIC_AUTH_TOKEN=${TASK_BUILDER_PROXY_TOKEN}" not in cmd
    assert captured["env"]["TASK_BUILDER_AGENT"] == "codex"
    assert captured["env"]["TASK_BUILDER_PROXY_BASE_URL"] == "https://example.test/v1"
    assert "private-key" not in " ".join(cmd)


@pytest.mark.parametrize("role", ["reviewer", "writer", "repair-writer"])
def test_real_codex_protocol_and_resume(tmp_path, role):
    """Real CLI, scripted local API, no external model or credentials."""
    import shutil
    import os
    import shlex
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    executable = os.environ.get("TASK_BUILDER_TEST_CODEX_BIN", "codex")
    if shutil.which(executable) is None:
        pytest.skip("requires installed Codex")
    requests = []
    source = tmp_path / "source"
    source.mkdir()
    outside = source / "source.txt"
    outside.write_text("original")
    plan = tmp_path / "plan"
    plan.mkdir()
    plan_file = plan / "plan.json"
    plan_file.write_text("original-plan")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            response = {"id": f"resp_test_{len(requests)}", "object": "response", "status": "completed",
                        "output": [{"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                                    "content": [{"type": "output_text", "text": "protocol-ok", "annotations": []}]}],
                        "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 30}, "output_tokens": 7, "total_tokens": 107}}
            if len(requests) == 1:
                def tool_names(tools, prefix=""):
                    for tool in tools:
                        if tool.get("type") == "namespace":
                            yield from tool_names(tool.get("tools", []), prefix + tool["name"] + ".")
                        elif tool.get("name"):
                            yield prefix + tool["name"]
                names = list(tool_names(body.get("tools", [])))
                script = ("printf saved > probe.txt; printf updated-plan > " + shlex.quote(str(plan_file))
                          + "; printf changed > " + shlex.quote(str(outside)))
                name = next((name for name in names if name.split(".")[-1] in {"exec_command", "shell_command", "shell"}), "shell")
                if name.split(".")[-1] == "exec_command":
                    arguments = {"cmd": script, "login": False, "max_output_tokens": 1000}
                elif name.split(".")[-1] == "shell_command":
                    arguments = {"command": script, "workdir": str(tmp_path / "work"), "timeout_ms": 10000}
                else:
                    arguments = {"command": ["/bin/bash", "-c", script], "workdir": str(tmp_path / "work"), "timeout_ms": 10000}
                response["output"] = [{"type": "function_call", "id": "fc_test", "call_id": "call_test", "name": name, "arguments": json.dumps(arguments)}]
                if not names:
                    # Recent Codex models expose tools through the code-mode host.
                    code = "text(await tools.exec_command(" + json.dumps({"cmd": script, "login": False, "max_output_tokens": 1000}) + "));"
                    response["output"] = [{"type": "custom_tool_call", "id": "fc_test", "call_id": "call_test", "name": "exec", "input": code}]
            output = response["output"][0]
            events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                      {"type": "response.output_item.added", "output_index": 0, "item": output},
                      {"type": "response.output_item.done", "output_index": 0, "item": output},
                      {"type": "response.completed", "response": response}]
            raw = "".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = EnvConfig(tmp_path / "model.env", "gpt-6.1-sol", {
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1", "OPENAI_API_KEY": "test-no-secret", "REASONING_EFFORT": "high",
    }, "codex")
    try:
        agent = IndependentCodexAgent(codex_bin=executable, timeout_sec=45)
        options = dict(role=role, workdir=tmp_path / "work", env_config=env,
                       allowed_dirs=(source, plan), read_only_dirs=(source,))
        first = agent.run(prompt="Reply protocol-ok.", **options)
        assert first.stdout == "protocol-ok"
        assert outside.read_text() == "original"
        assert (tmp_path / "work/probe.txt").exists() == (role != "reviewer"), first.events
        assert plan_file.read_text() == ("updated-plan" if role == "repair-writer" else "original-plan")
        second = agent.run(prompt="Repeat.", resume_session=first.session_id, **options)
        assert second.session_id == first.session_id
        assert second.stdout == "protocol-ok"
        fresh = IndependentCodexAgent(codex_bin=executable, timeout_sec=45)
        third = fresh.run(prompt="Repeat again.", resume_session=first.session_id, **options)
        assert third.session_id == first.session_id and third.stdout == "protocol-ok"
        assert not list((tmp_path / "logs/codex").glob("*/.tmp/plugins"))
        assert all(path == "/v1/responses" and auth == "Bearer test-no-secret" for path, auth, _ in requests)
        assert all(body["model"] == "gpt-6.1-sol" and body["reasoning"]["effort"] == "high" for _, _, body in requests)
        from task_builder.usage import source_summary
        usage = source_summary(tmp_path, {})
        assert usage["input_tokens"] == 70 * len(requests)
        assert usage["cache_read_input_tokens"] == 30 * len(requests)
        assert usage["output_tokens"] == 7 * len(requests)
        assert usage["calls"] == 3
        assert usage["incomplete_invocations"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
