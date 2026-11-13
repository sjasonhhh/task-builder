from __future__ import annotations

import json
import subprocess
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from task_builder.agents import IndependentClaudeAgent, _deny_read
from task_builder.models import EnvConfig


@pytest.mark.parametrize("extra_logs", [0, 60])
def test_repair_prompt_preserves_latest_failure_with_history_limits(tmp_path, monkeypatch, extra_logs):
    from task_builder import writer_agent

    workspace = SimpleNamespace(source_root=tmp_path)
    for field in ("shared", "input", "plan", "draft", "reviews", "validation", "logs", "failures"):
        directory = tmp_path / field
        directory.mkdir()
        setattr(workspace, field + "_dir", directory)
    (workspace.plan_dir / "plan.json").write_text("{}")
    workspace.events = workspace.logs_dir / "events.jsonl"
    history = {
        workspace.failures_dir / "previous.txt": "FAILURE_BEGIN\n" + "旧失败\n" * 45_000 + "FAILURE_END",
        workspace.reviews_dir / "review.json": json.dumps({"advice": "旧审核\n" * 45_000}, ensure_ascii=False),
        workspace.validation_dir / "report.txt": "VALIDATION_BEGIN\n" + "旧检查\n" * 45_000 + "VALIDATION_END",
        workspace.logs_dir / "previous.log": "LOG_BEGIN\n" + "旧日志\n" * 45_000 + "LOG_END",
    }
    assert sum(map(len, history.values())) > 600_000
    for index in range(extra_logs):
        history[workspace.logs_dir / f"z-history-{index:03d}.log"] = f"BEGIN_{index}\n" + "x" * 13_000 + f"\nEND_{index}"
    for path, content in history.items():
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(writer_agent, "read_shared_material", lambda _: ("rubric", "test_env", "environment"))
    monkeypatch.setattr(writer_agent, "source_manifest", lambda _: "source files")
    captured = {}

    def run(**options):
        captured.update(options)
        raise RuntimeError("prompt captured without a model call")

    failure = "reviewer: Harbor task.toml 缺少 [task] 表"
    with pytest.raises(RuntimeError, match="prompt captured"):
        writer_agent.run_repair_writer_agent(
            None, SimpleNamespace(author_env=None), workspace, SimpleNamespace(run=run), failure,
        )
    prompt = captured["prompt"]
    evidence = prompt.split("本轮失败及历史参考：\n", 1)[1].split("\n\n只允许修改", 1)[0]
    assert evidence.startswith("当前失败摘要：\n" + failure)
    assert prompt.index(failure) < prompt.index("来源文件清单")
    assert len(evidence) <= 600_000
    if extra_logs:
        assert len(evidence) == 600_000
        path = workspace.logs_dir / f"z-history-{extra_logs - 1:03d}.log"
        assert f"\n===== {path} =====\n{history[path][-12_000:]}" in evidence
    else:
        for path, content in history.items():
            assert f"\n===== {path} =====\n{content[-12_000:]}" in evidence


def test_current_failure_is_never_truncated_even_above_history_budget(tmp_path):
    from task_builder.writer_agent import _failure_evidence

    workspace = SimpleNamespace(**{name + "_dir": tmp_path / name for name in ("failures", "reviews", "validation", "logs")})
    failure = "最新失败开始\n" + "错误" * 300_000 + "\n最新失败结束"
    assert _failure_evidence(workspace, failure) == "当前失败摘要：\n" + failure


@pytest.mark.parametrize("suffix", [".txt", ".csv", ".tsv", ".jsonl", ".smi", ".smiles", ".sdf", ".fasta", ".fastq", ".pdb", ".log", ""])
def test_large_text_is_not_denied_by_total_size(tmp_path, suffix):
    path = tmp_path / ("data" + suffix)
    path.write_text("id,value\n" * 10000)
    assert path.stat().st_size > 16000
    assert not _deny_read(path)


@pytest.mark.parametrize("suffix", [".h5", ".h5ad", ".npz", ".npy", ".gz", ".zip", ".pdf", ".RDS"])
def test_binary_protection_is_preserved(tmp_path, suffix):
    path = tmp_path / ("data" + suffix)
    path.write_bytes(b"binary data")
    assert _deny_read(path)


@pytest.mark.parametrize("role", ["planner", "planner-format-1", "writer", "writer-files-1", "reviewer-0", "repair-writer", "repair-writer-files-1"])
def test_all_roles_and_resume_receive_text_preview_policy(tmp_path, monkeypatch, role):
    work = tmp_path / "work"
    source = tmp_path / "input"
    source.mkdir()
    csv = source / "data.csv"
    csv.write_text("id,value\n" * 10000)
    binary = source / "data.h5"
    binary.write_bytes(b"binary data")
    image = source / "figure.png"
    image.write_bytes(b"image fixture" * 10000)
    env = EnvConfig(tmp_path / "author.env", "model", {"MODEL_ID": "model"})
    seen = {}
    def execute(self, actual_role, session, command, prompt, *args):
        seen.update(role=actual_role, command=command, prompt=prompt)
        return "done"
    monkeypatch.setattr(IndependentClaudeAgent, "_execute", execute)
    agent = IndependentClaudeAgent()
    assert agent.run(role=role, prompt="task", workdir=work, env_config=env,
                     allowed_dirs=(source,), read_only_dirs=(source,), resume_session="saved-session") == "done"
    command = seen["command"]
    settings = json.loads(command[command.index("--settings") + 1])
    deny = settings.get("permissions", {}).get("deny", [])
    assert not any(str(csv).lstrip("/") in rule for rule in deny)
    assert not any(str(image).lstrip("/") in rule for rule in deny)
    assert "limit=50" in seen["prompt"] and "200 行" in seen["prompt"]
    assert "Glob" in seen["prompt"] and "allow_large" in seen["prompt"]
    assert "--resume" in command and "saved-session" in command
    assert "--restricted" in command and "none" in command
    writes_data = role.startswith(("writer", "repair-writer"))
    if not writes_data:
        assert any(str(binary).lstrip("/") in rule for rule in deny)
        assert any(rule.startswith("Edit(") for rule in deny)
        assert "Bash" not in command
    if role.startswith("reviewer"):
        assert "Write" not in command and "Edit" not in command
    if writes_data:
        assert "Bash(cp *)" in settings["permissions"]["allow"]
        assert not deny  # Keep source-file copying working in Claude's Bash checks.


@pytest.mark.parametrize("configured,expected", [(None, "8000"), ("4000", "4000"), ("90000", "8000"), ("0", "8000"), ("-1", "8000"), ("invalid", "8000")])
def test_native_read_budget_applies_to_every_model_call(tmp_path, monkeypatch, configured, expected):
    values = {"MODEL_ID": "model"}
    monkeypatch.delenv("CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS", raising=False)
    if configured is not None:
        values["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] = configured
    env = EnvConfig(tmp_path / "author.env", "model", values)
    observed = []
    def execute(command, **kwargs):
        observed.append(kwargs["env"]["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"])
        return subprocess.CompletedProcess(command, 0, "done", "")
    monkeypatch.setattr("task_builder.agents.run_process", execute)
    for role in ("planner", "writer", "reviewer", "repair-writer"):
        IndependentClaudeAgent().run(role=role, prompt="task", workdir=tmp_path / role, env_config=env)
    assert observed == [expected] * 4


def test_policy_resume_keeps_session_and_allows_missing_data_preview(tmp_path, monkeypatch):
    from task_builder import planner_agent
    workspace = SimpleNamespace(plan_dir=tmp_path / "plan", shared_dir=tmp_path / "shared",
                                input_dir=tmp_path / "input", reviews_dir=tmp_path / "reviews",
                                logs_dir=tmp_path / "logs")
    workspace.plan_dir.mkdir()
    monkeypatch.setattr(planner_agent, "read_shared_material", lambda _: ("rubric", "chemistry", "environment"))
    monkeypatch.setattr(planner_agent, "save_agent_log", lambda *a: None)
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        (workspace.plan_dir / "plan.json").write_text('{"problem":{"question":"saved scientific question"}}')
        return SimpleNamespace(stdout="saved", session_id="existing-session")
    result = planner_agent._planner_attempt(None, SimpleNamespace(author_env=None), workspace,
                SimpleNamespace(run=run), attempt=0, format_error="", resume_session="existing-session")
    assert result.is_file()
    assert calls[0]["resume_session"] == "existing-session"
    assert "分段预览" in calls[0]["prompt"]
    assert "不要重新遍历" in calls[0]["prompt"]


@pytest.mark.skipif(shutil.which("claude") is None, reason="requires installed Claude Code; uses a local fake API, no model calls")
@pytest.mark.parametrize("role", ["planner", "writer", "reviewer", "repair-writer"])
def test_real_claude_can_preview_large_text_and_retry_oversized_read(tmp_path, monkeypatch, role):
    """Exercise real Read permissions with scripted, credential-free replies."""
    source = tmp_path / "input"
    source.mkdir()
    csv = source / "large.csv"
    csv.write_text("id,value\n" + "".join(f"{i},marker_{i}_" + "x," * 40 + "\n" for i in range(5000)))
    calls = [
        ("Read", {"file_path": str(csv), "offset": 1, "limit": 3}),
        ("Read", {"file_path": str(csv)}),
        ("Read", {"file_path": str(csv), "offset": 1002, "limit": 2}),
    ]
    observed = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = json.dumps({"data": [], "has_more": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "count_tokens" in self.path:
                data = b'{"input_tokens":100}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            for message in body.get("messages", []):
                blocks = message.get("content", [])
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        observed[block["tool_use_id"]] = block
            index = len(observed)
            if index < len(calls):
                name, arguments = calls[index]
                content = {"type": "tool_use", "id": f"toolu_read_{index}", "name": name, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
                stop = "tool_use"
            else:
                content, delta, stop = {"type": "text", "text": ""}, {"type": "text_delta", "text": "READ_POLICY_OK"}, "end_turn"
            events = [
                ("message_start", {"type": "message_start", "message": {"id": f"msg_{index}", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": content}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 1}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            data = "".join(f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("TASK_BUILDER_DIRECT_MODELS", "1")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION"):
        monkeypatch.delenv(key, raising=False)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = EnvConfig(tmp_path / "model.env", "claude-sonnet-4-6", {
            "MODEL_ID": "claude-sonnet-4-6", "ANTHROPIC_API_KEY": "test-not-a-secret",
            "ANTHROPIC_AUTH_TOKEN": "test-not-a-secret", "API_TIMEOUT_MS": "10000",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}", "CLAUDE_CODE_MAX_RETRIES": "0",
        })
        try:
            result = IndependentClaudeAgent(timeout_sec=45).run(role=role, prompt="Exercise bounded reads of the fixture.",
                workdir=tmp_path / "work", env_config=env, allowed_dirs=(source,), read_only_dirs=(source,))
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert result.returncode == 0
    from task_builder.usage import source_summary
    usage = source_summary(tmp_path, {})
    assert usage["input_tokens"] == 400 and usage["output_tokens"] == 4
    assert usage["api_requests"] == 4 and usage["incomplete_invocations"] == 0
    assert len(observed) == len(calls)
    def content(index):
        return str(observed[f"toolu_read_{index}"].get("content", ""))
    assert "id,value" in content(0) and not observed["toolu_read_0"].get("is_error")
    assert observed["toolu_read_1"].get("is_error"), "Whole-file reads must stay bounded"
    assert "denied by your permission settings" not in content(1)
    assert "marker_1000_" in content(2) and not observed["toolu_read_2"].get("is_error")
