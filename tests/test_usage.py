from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from task_builder.agents import IndependentClaudeAgent, parse_print_json, save_agent_log
from task_builder.codex_agent import IndependentCodexAgent
from task_builder.deadline import run_process, task_deadline
from task_builder.models import AgentError, EnvConfig, TaskTimeout
from task_builder.usage import (TOKEN_KEYS, load_pricing, main, read_entries, report, source_summary,
                                summarize, usage_files, write_source_usage)
from task_builder.usage_capture import CodexRolloutWatcher, UsageCapture, normalize_usage

MODEL = "deepseek-v4.1-flash"
PRICING = {"models": {MODEL: {"currency": "CNY", "input": 1, "output": 4, "cache_read": 0.1, "cache_creation": 1}}}


def workspace(tmp_path):
    root = tmp_path / "workspace" / "sources" / "example"
    (root / "logs").mkdir(parents=True)
    (root / "plan").mkdir()
    return root


def capture(root, monkeypatch, **kwargs):
    monkeypatch.setattr("task_builder.usage_capture.load_pricing", lambda: PRICING)
    return UsageCapture(root / "plan", role="planner", model=MODEL, agent="claude", session_id="s", **kwargs)


def result(raw, **extra):
    return {"type": "result", "result": "saved", "usage": raw, "num_turns": 1, "duration_api_ms": 500, **extra}


def test_protocols_are_exclusive_and_reasoning_is_not_added_again():
    raw = {"input_tokens": 100, "cached_input_tokens": 30, "cache_write_input_tokens": 10,
           "output_tokens": 7, "reasoning_output_tokens": 3}
    normalized = normalize_usage(raw, "codex")
    assert [normalized[k] for k in TOKEN_KEYS] == [60, 7, 30, 10]
    raw = {"input_tokens": 60, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10, "output_tokens": 7}
    assert normalize_usage(raw, "anthropic") == normalized
    assert not normalize_usage({"input_tokens": 10, "cached_input_tokens": 20, "output_tokens": 1}, "codex")["usage_valid"]
    assert not normalize_usage({"input_tokens": 10}, "anthropic")["usage_valid"]


def test_claude_stream_snapshots_and_final_total_are_not_added_twice(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    c = capture(root, monkeypatch)
    events = [
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "r1", "model": MODEL,
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 30, "output_tokens": 1}}}},
        {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 7}}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {"type": "assistant", "message": {"id": "r1", "model": MODEL,
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 30, "output_tokens": 7}}},
        result({"input_tokens": 100, "cache_read_input_tokens": 30, "output_tokens": 7}),
    ]
    for e in events:
        c.claude_line(json.dumps(e))
    c.finish(status="completed", returncode=0)
    s = source_summary(root, PRICING)
    assert s["api_requests"] == 1
    assert s["input_tokens"] == 100 and s["output_tokens"] == 7
    assert s["estimated_cost"] == 0.0001 and s["currency"] == "CNY"
    assert s["cost_status"] == "estimated" and s["estimated_cost_usd"] is None
    assert parse_print_json("\n".join(map(json.dumps, events)))[0] == "saved"


def test_codex_native_requests_ignore_cumulative_snapshots_and_old_bytes(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    c = capture(root, monkeypatch)
    home = root / "logs" / "codex"
    log = home / "sessions" / "2026" / "run.jsonl"
    log.parent.mkdir(parents=True)
    def record(rid, count=100):
        return {"type": "token_usage_record", "payload": {"response_id": rid, "usage": {
            "input_tokens": count, "cached_input_tokens": 30, "output_tokens": 7, "reasoning_output_tokens": 4}}}
    log.write_text(json.dumps(record("old", 1000)) + "\n")
    watcher = CodexRolloutWatcher(home, c)
    rows = [record("r1"), {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 99999}}}},
            record("r1"), record("r2")]
    with log.open("a") as f:
        f.write("\n".join(map(json.dumps, rows)) + "\n")
    watcher.poll(force=True)
    watcher.poll(force=True)
    c.finish(status="completed", returncode=0)
    s = source_summary(root, PRICING)
    assert (s["input_tokens"], s["cache_read_input_tokens"], s["output_tokens"], s["api_requests"]) == (140, 60, 14, 2)


def test_price_snapshots_unknown_models_decimal_and_currency(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    c = capture(root, monkeypatch)
    c.request("r1", {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}, "anthropic", complete=True)
    c.finish(status="completed")
    changed = {"models": {MODEL: {"currency": "CNY", "input": 2, "output": 8, "cache_read": 0.2}}}
    assert source_summary(root, changed)["estimated_cost"] == 5.1
    assert source_summary(root, changed, recalculate=True)["estimated_cost"] == 10.2
    unknown = summarize([{"model": "gpt-6.1-sol", "role": "planner", "input_tokens": 100}], PRICING)
    assert unknown["estimated_cost"] is None and unknown["stages"]["planner"]["estimated_cost"] is None
    assert summarize([], PRICING)["estimated_cost"] is None
    assert summarize([{"model": MODEL, "input_tokens": 100}], {"default": {"input": 1, "output": 4}})["estimated_cost"] is None
    usd = {"models": {"usd-model": {"currency": "USD", "input": 1, "output": 4}, **PRICING["models"]}}
    multi = summarize([{"model": MODEL, "input_tokens": 1_000_000}, {"model": "usd-model", "input_tokens": 1_000_000}], usd)
    assert multi["estimated_cost"] is None and multi["known_cost_by_currency"] == {"CNY": 1.0, "USD": 1.0}
    # Round after summation, not once per request.
    small = summarize([{"model": MODEL, "input_tokens": 1} for _ in range(60)], PRICING)
    assert small["estimated_cost"] == 0.0001


def test_copied_evidence_scope_real_retries_and_cumulative_duration(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    c = capture(root, monkeypatch, agent_call_id="call1")
    c.claude_line(json.dumps(result({"input_tokens": 1_000_000, "output_tokens": 0}, duration_api_ms=1000)))
    c.finish(status="completed")
    history = root / "previous-evidence" / "logs"
    history.mkdir(parents=True)
    shutil.move(c.path, history / c.path.name)
    nested = root / "previous-evidence" / "previous-evidence" / "logs"
    nested.mkdir(parents=True)
    shutil.copy(history / c.path.name, nested / c.path.name)
    # Unrelated copies in inputs/checkpoints must not enter accounting.
    ignored = root / "input" / "logs"
    ignored.mkdir(parents=True)
    (ignored / "usage.jsonl").write_text(json.dumps({"model": MODEL, "input_tokens": 999999999}) + "\n")
    c2 = capture(root, monkeypatch, resumed=True, agent_call_id="call2")
    c2.claude_line(json.dumps(result({"input_tokens": 2_000_000, "output_tokens": 0}, duration_api_ms=1300)))
    c2.finish(status="completed")
    s = source_summary(root, PRICING)
    assert s["estimated_cost"] == 3 and s["calls"] == 2
    assert s["current_run"]["estimated_cost"] == 2 and s["inherited_history"]["estimated_cost"] == 1
    assert s["duration_api_ms"] == 1300 and s["current_run"]["duration_api_ms"] == 300
    c3 = capture(root, monkeypatch, agent_call_id="call2")
    c3.request("new-response", {"input_tokens": 3_000_000, "output_tokens": 0}, "anthropic", complete=True)
    c3.finish(status="completed")
    s = source_summary(root, PRICING)
    assert s["estimated_cost"] == 6 and s["calls"] == 2 and s["process_attempts"] == 3


@pytest.mark.parametrize("timeout_type", [subprocess.TimeoutExpired, TaskTimeout])
@pytest.mark.parametrize("agent_type", ["claude", "codex"])
def test_adapters_persist_partial_usage_before_timeout_without_save_agent_log(tmp_path, monkeypatch, timeout_type, agent_type):
    root = workspace(tmp_path)
    monkeypatch.setattr("task_builder.usage_capture.load_pricing", lambda: PRICING)
    config = EnvConfig(tmp_path / "a.env", MODEL, {"OPENAI_BASE_URL": "https://example.test", "OPENAI_API_KEY": "test-secret"}, agent_type)
    def execute(command, **kw):
        if agent_type == "claude":
            kw["on_stdout"](json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {
                "id": "r1", "usage": {"input_tokens": 1_000_000, "output_tokens": 1}}}}))
        else:
            native = Path(kw["env"]["CODEX_HOME"]) / "sessions" / "new.jsonl"
            native.parent.mkdir(parents=True, exist_ok=True)
            native.write_text(json.dumps({"type": "token_usage_record", "payload": {"response_id": "r1", "usage": {
                "input_tokens": 1_000_000, "cached_input_tokens": 30, "output_tokens": 7}}}) + "\n")
            kw["on_poll"]()
        if timeout_type is TaskTimeout:
            raise TaskTimeout("deadline")
        raise subprocess.TimeoutExpired(command, 1)
    module = "task_builder.agents" if agent_type == "claude" else "task_builder.codex_agent"
    monkeypatch.setattr(module + ".run_process", execute)
    agent = IndependentClaudeAgent() if agent_type == "claude" else IndependentCodexAgent()
    with pytest.raises(TaskTimeout if timeout_type is TaskTimeout else AgentError):
        agent.run(role="planner", prompt="task", workdir=root / "plan", env_config=config)
    s = source_summary(root, PRICING)
    assert s["total_tokens"] > 999_999 and s["cost_status"] == "partial"
    assert s["estimated_cost"] is None and s["known_cost_by_currency"]["CNY"] >= 1
    assert not (root / "logs" / "usage.jsonl").exists()
    assert "test-secret" not in (root / "logs" / "usage-events.jsonl").read_text()


@pytest.mark.parametrize("global_deadline", [False, True])
def test_real_process_delivers_usage_before_kill_and_drains_stderr(tmp_path, global_deadline):
    lines = []
    command = [sys.executable, "-u", "-c", "import sys,time; print('first', flush=True); sys.stderr.write('e'*200000); sys.stderr.flush(); time.sleep(10)"]
    kwargs = dict(input="prompt", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  timeout=0.5 if not global_deadline else 10, on_stdout=lines.append)
    with pytest.raises(TaskTimeout if global_deadline else subprocess.TimeoutExpired) as caught:
        if global_deadline:
            with task_deadline(0.5):
                run_process(command, **kwargs)
        else:
            run_process(command, **kwargs)
    assert lines == ["first\n"]
    assert caught.value.stdout == "first\n" and len(caught.value.stderr) == 200000


def test_compatibility_summary_not_double_counted_and_recalculation_preserves_raw(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    c = capture(root, monkeypatch)
    c.claude_line(json.dumps(result({"input_tokens": 1_000_000, "output_tokens": 0})))
    summary = c.finish(status="completed")
    from task_builder.agents import AgentRun
    save_agent_log(root / "logs" / "planner.log", AgentRun("planner", "s", "saved", "", 0, (), summary))
    assert source_summary(root, PRICING)["input_tokens"] == 1_000_000
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in usage_files(root)}
    target = write_source_usage(root, tmp_path / "out", "example", PRICING)
    rows = report(tmp_path / "out", recalculate=True, pricing=PRICING)
    assert rows[0]["estimated_cost"] == 1
    assert hashes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in usage_files(root)}
    assert json.loads(target.read_text())["schema_version"] == 2
    assert main(["--run", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["source"] == "example"


def test_truncated_ledger_is_partial_and_missing_rates_are_not_zero(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    c = capture(root, monkeypatch)
    c.request("r", {"input_tokens": 100, "output_tokens": 2}, "anthropic", complete=True)
    with c.path.open("a") as f:
        f.write('{"kind":')
    summary = source_summary(root, PRICING)
    assert summary["cost_status"] == "partial" and summary["estimated_cost"] is None
    missing = summarize([{"model": MODEL, "input_tokens": 100, "cache_creation_input_tokens": 20}],
                        {"models": {MODEL: {"currency": "CNY", "input": 1, "output": 4}}})
    assert missing["estimated_cost"] is None


def test_unknown_snapshot_cannot_silently_be_repriced(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    monkeypatch.setattr("task_builder.usage_capture.load_pricing", lambda: {})
    c = UsageCapture(root / "plan", role="planner", model=MODEL, agent="claude", session_id="s")
    c.request("r", {"input_tokens": 1_000_000, "output_tokens": 0}, "anthropic", complete=True)
    c.finish(status="completed")
    assert source_summary(root, PRICING)["estimated_cost"] is None
    assert source_summary(root, PRICING, recalculate=True)["estimated_cost"] == 1


def test_historical_cellbender_regression():
    root = Path(__file__).resolve().parents[1] / "output/test_env/runs/20261101-022925-fd47f8b7/workspace/sources/cellbender-remove-background"
    if not root.is_dir():
        pytest.skip("historical local evidence unavailable")
    s = source_summary(root, load_pricing(), recalculate=True)
    assert s["estimated_cost"] == 321.9806 and s["currency"] == "CNY"
    assert s["current_run"]["estimated_cost"] == 48.8184 and s["inherited_history"]["estimated_cost"] == 273.1622
    assert s["input_tokens"] == 285680294 and s["cache_read_input_tokens"] == 201907274
    assert s["duration_api_ms"] == 39219975
    assert s["cost_status"] == "legacy_unverified"


def test_codex_resume_uses_cumulative_difference_even_without_request_records(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    monkeypatch.setattr("task_builder.usage_capture.load_pricing", lambda: PRICING)
    home = root / "logs" / "codex"
    native = home / "sessions" / "rollout-session-123.jsonl"
    native.parent.mkdir(parents=True)
    native.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": 1000, "cached_input_tokens": 300, "output_tokens": 100}}}}) + "\n")
    c = UsageCapture(root / "plan", role="planner", model=MODEL, agent="codex", session_id="session-123", resumed=True)
    CodexRolloutWatcher(home, c)
    c.codex_line(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1100, "cached_input_tokens": 330, "output_tokens": 107}}))
    c.codex_line(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 360, "output_tokens": 114}}))
    c.finish(status="completed")
    s = source_summary(root, PRICING)
    assert [s[k] for k in TOKEN_KEYS] == [140, 14, 60, 0]
    assert s["incomplete_invocations"] == 0 and s["num_turns"] == 2
    assert s["api_requests"] is None


def test_codex_resume_missing_baseline_is_unknown_not_full_session_charge(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    monkeypatch.setattr("task_builder.usage_capture.load_pricing", lambda: PRICING)
    c = UsageCapture(root / "plan", role="planner", model=MODEL, agent="codex", session_id="missing", resumed=True)
    c.codex_line(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000000, "cached_input_tokens": 300, "output_tokens": 100}}))
    c.finish(status="completed")
    s = source_summary(root, PRICING)
    assert s["cost_status"] == "partial" and s["estimated_cost"] is None
    assert s["known_cost_by_currency"] == {} and s["total_tokens"] == 0


def test_partial_request_and_aggregate_mismatch_remain_visible(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    c = capture(root, monkeypatch)
    c.request("r1", {"input_tokens": 100, "output_tokens": 10}, "anthropic", complete=True)
    c.claude_line(json.dumps(result({"input_tokens": 200, "output_tokens": 20})))
    c.finish(status="completed")
    s = source_summary(root, PRICING)
    assert s["cost_status"] == "partial" and s["estimated_cost"] is None
    assert s["input_tokens"] == 100


def test_codex_parser_does_not_add_session_snapshots():
    from task_builder.codex_agent import parse_codex_events
    events = [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 7}},
        {"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 60, "output_tokens": 14}},
    ]
    _, _, usage, _ = parse_codex_events("\n".join(map(json.dumps, events)))
    assert usage["input_tokens"] == 140 and usage["output_tokens"] == 14
    assert usage["num_turns"] == 2 and usage["usage_scope"] == "session_cumulative"
