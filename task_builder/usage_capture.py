"""Persist metering facts during execution, independently of final agent logs."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .usage import TOKEN_KEYS, load_pricing, price_snapshot


def source_root(workdir: Path) -> Path:
    workdir = workdir.resolve()
    return next((p for p in (workdir, *workdir.parents) if p.parent.name == "sources" and (p / "logs").is_dir()), workdir.parent)


def normalize_usage(raw: dict, protocol: str) -> dict:
    """Keep billed categories exclusive; reasoning output is already in output_tokens."""
    if protocol == "codex":
        cached = raw.get("cached_input_tokens", 0)
        created = raw.get("cache_write_input_tokens", 0)
    else:
        cached = raw.get("cache_read_input_tokens", 0)
        created = raw.get("cache_creation_input_tokens", 0)
    counts = (raw.get("input_tokens"), raw.get("output_tokens"), cached, created)
    valid = all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts)
    values = [n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0 for n in counts]
    if protocol == "codex":
        values[0] -= values[2] + values[3]
        valid = valid and values[0] >= 0
        values[0] = max(0, values[0])
    return {**dict(zip(TOKEN_KEYS, values)), "usage_valid": valid}


class UsageCapture:
    def __init__(self, workdir: Path, *, role: str, model: str, agent: str, session_id: str,
                 base_url: str = "", resumed: bool = False, agent_call_id: str | None = None):
        self.path = source_root(workdir) / "logs" / "usage-events.jsonl"
        self.pricing = load_pricing()
        self.started = time.monotonic()
        self.requests: dict[str, dict] = {}
        self.fallback = None
        self.result = None
        self.closed = False
        self.observed = False
        self.codex_turns = 0
        self.codex_baseline_raw = None if resumed else {"input_tokens": 0, "output_tokens": 0}
        self.current_message = None
        self.issues = []
        self.metadata = {
            "schema_version": 2, "invocation_id": str(uuid.uuid4()),
            "agent_call_id": agent_call_id or str(uuid.uuid4()), "role": role, "agent": agent,
            "session_id": session_id, "model": model, "resumed": resumed,
            "provider_id": hashlib.sha256(base_url.encode()).hexdigest()[:20] if base_url else "unknown",
            "pricing_snapshot": price_snapshot(self.pricing, model),
        }
        self.append("start", **self.metadata)

    def append(self, kind: str, **values):
        row = {"schema_version": 2, "invocation_id": self.metadata["invocation_id"],
               "kind": kind, "time": time.time(), **values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def request(self, identifier: str, raw: dict, protocol: str, *, complete: bool, reported_model: str | None = None):
        previous = self.requests.get(identifier, {})
        allowed = set(TOKEN_KEYS) | {"cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens", "total_tokens"}
        merged = {**previous.get("raw_usage", {}), **{k: v for k, v in raw.items() if k in allowed}}
        model = reported_model or previous.get("model") or self.metadata["model"]
        # This is the CLI alias explicitly selected by our DeepSeek adapter.
        if model == "claude-opus-4-8" and not self.metadata["model"].startswith("claude-"):
            model = self.metadata["model"]
        row = {"request_id": identifier, "protocol": protocol, "model": model,
               "reported_model": reported_model or previous.get("reported_model"), "raw_usage": merged,
               "pricing_snapshot": price_snapshot(self.pricing, model),
               "complete": complete or previous.get("complete", False), **normalize_usage(merged, protocol)}
        if row != previous:
            self.requests[identifier] = row
            self.append("request", **row)

    def claude_line(self, line: str):
        self.observed = True
        try:
            payload = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("session_id"):
            self.metadata["session_id"] = payload["session_id"]
        kind = payload.get("type")
        if kind == "result":
            self.result = payload
            raw = payload.get("usage")
            if isinstance(raw, dict) and raw:
                self.fallback = {**normalize_usage(raw, "anthropic"), "raw_usage": raw, "protocol": "anthropic"}
        elif kind == "assistant":
            message = payload.get("message") or {}
            if message.get("id") and isinstance(message.get("usage"), dict):
                self.request(message["id"], message["usage"], "anthropic", complete=True, reported_model=message.get("model"))
        elif kind == "stream_event":
            event = payload.get("event") or {}
            if event.get("type") == "message_start":
                message = event.get("message") or {}
                self.current_message = message.get("id")
                if self.current_message:
                    self.request(self.current_message, message.get("usage") or {}, "anthropic", complete=False, reported_model=message.get("model"))
            elif event.get("type") == "message_delta" and self.current_message:
                self.request(self.current_message, event.get("usage") or {}, "anthropic", complete=False)
            elif event.get("type") == "message_stop" and self.current_message:
                self.request(self.current_message, {}, "anthropic", complete=True)
                self.current_message = None

    def codex_line(self, line: str):
        self.observed = True
        try:
            payload = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "thread.started":
            self.metadata["session_id"] = payload.get("thread_id") or self.metadata["session_id"]
        # turn.completed is only a fallback/reconciliation total, never another request.
        if payload.get("type") == "turn.completed" and isinstance(payload.get("usage"), dict):
            current = normalize_usage(payload["usage"], "codex")
            baseline = normalize_usage(self.codex_baseline_raw or {}, "codex")
            delta = {k: current[k] - baseline[k] for k in TOKEN_KEYS}
            valid = current["usage_valid"] and baseline["usage_valid"] and all(n >= 0 for n in delta.values())
            self.fallback = {**{k: n if valid else 0 for k, n in delta.items()}, "usage_valid": valid, "raw_usage": payload["usage"],
                             "raw_baseline": self.codex_baseline_raw, "protocol": "codex",
                             "scope": "session_cumulative_difference"}
            self.codex_turns += 1

    def replay(self, text: str):
        if self.observed:
            return
        handler = self.codex_line if self.metadata["agent"] == "codex" else self.claude_line
        for line in text.splitlines():
            handler(line)

    def finish(self, *, status: str, returncode: int = -1) -> dict:
        if self.closed:
            return self.summary
        totals = {k: sum(r[k] for r in self.requests.values()) for k in TOKEN_KEYS}
        if not self.requests and self.fallback:
            totals = {k: self.fallback[k] for k in TOKEN_KEYS}
        valid = all(r["usage_valid"] for r in self.requests.values()) if self.requests else bool(self.fallback and self.fallback["usage_valid"])
        complete = status == "completed" and valid and (all(r["complete"] for r in self.requests.values()) if self.requests else bool(self.fallback))
        if self.requests and self.fallback and self.fallback["usage_valid"] and any(totals[k] != self.fallback[k] for k in TOKEN_KEYS):
            complete = False
            self.issues.append("request_totals_differ_from_cli_aggregate")
        result = self.result or {}
        end = {**self.metadata, **totals, "status": status, "returncode": returncode,
               "usage_valid": valid, "usage_complete": complete, "issues": self.issues,
               "num_turns": result.get("num_turns", self.codex_turns),
               "duration_ms": round((time.monotonic() - self.started) * 1000),
               "duration_api_reported_ms": result.get("duration_api_ms"),
               "fallback_usage": self.fallback, "api_requests": len(self.requests) if self.requests else None}
        self.append("end", **end)
        self.summary = {**end, "captured_invocations": [self.metadata["invocation_id"]]}
        self.closed = True
        return self.summary


class CodexRolloutWatcher:
    """Read only appended native usage records, never replay existing session history."""
    def __init__(self, home: Path, capture: UsageCapture):
        self.root = home / "sessions"
        self.capture = capture
        self.offsets = {p: p.stat().st_size for p in self.root.rglob("*.jsonl")}
        self.last_scan = 0.0
        if capture.metadata["resumed"]:
            session = capture.metadata["session_id"]
            for path in self.offsets:
                if session and session in path.name:
                    with path.open("rb") as handle:
                        for line in handle:
                            if b'"token_count"' not in line:
                                continue
                            try:
                                payload = json.loads(line).get("payload") or {}
                            except (ValueError, AttributeError):
                                continue
                            raw = (payload.get("info") or {}).get("total_token_usage")
                            if isinstance(raw, dict) and normalize_usage(raw, "codex")["usage_valid"]:
                                capture.codex_baseline_raw = raw

    def poll(self, force: bool = False):
        if not force and time.monotonic() - self.last_scan < 0.5:
            return
        self.last_scan = time.monotonic()
        for path in self.root.rglob("*.jsonl"):
            offset = self.offsets.get(path, 0)
            with path.open("rb") as handle:
                if path.stat().st_size < offset:
                    offset = 0
                handle.seek(offset)
                while line := handle.readline():
                    if not line.endswith(b"\n"):
                        break
                    offset = handle.tell()
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or row.get("type") != "token_usage_record":
                        continue  # token_count snapshots must not be added again.
                    payload = row.get("payload") or {}
                    raw = payload.get("usage")
                    if not isinstance(raw, dict):
                        continue
                    identifier = payload.get("response_id")
                    if not identifier:
                        self.capture.issues.append("request_missing_response_id")
                        identifier = f"{path.name}:{offset}"
                    self.capture.request(identifier, raw, "codex", complete=True, reported_model=payload.get("model"))
            self.offsets[path] = offset
