"""Model connection settings shared by local agents, Harbor and the relay."""
from __future__ import annotations

from urllib.parse import urlsplit
import json


AUTH_KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def agent_name(values: dict[str, str]) -> str:
    name = values.get("AGENT", "claude-code").strip().lower().replace("_", "-")
    name = {"claude": "claude-code", "claude-cli": "claude-code"}.get(name, name)
    if name not in {"claude-code", "codex"}:
        raise ValueError(f"不支持 AGENT={name!r}；请使用 claude-code 或 codex")
    return name


def model_connection(values: dict[str, str], agent: str) -> tuple[str, str]:
    if agent == "codex":
        base = values.get("OPENAI_BASE_URL") or values.get("ANTHROPIC_BASE_URL", "")
        token = next((values[key] for key in AUTH_KEYS if values.get(key)), "")
    else:
        base = values.get("ANTHROPIC_BASE_URL", "")
        token = values.get("ANTHROPIC_AUTH_TOKEN") or values.get("ANTHROPIC_API_KEY", "")
    return base, token


def codex_base_url(base: str) -> str:
    base = base.rstrip("/")
    # A bare gateway address conventionally exposes OpenAI under /v1.
    return base + "/v1" if not urlsplit(base).path else base


def codex_settings(base_url: str, effort: str | None = None) -> dict:
    settings = {
        "model_provider": "task_builder",
        "model_providers": {"task_builder": {
            "name": "task-builder", "base_url": codex_base_url(base_url),
            "env_key": "OPENAI_API_KEY", "wire_api": "responses",
            "requires_openai_auth": False, "supports_websockets": False,
            "request_max_retries": 3, "stream_max_retries": 3,
            "stream_idle_timeout_ms": 600000,
        }},
        "web_search": "disabled",
        "features": {"daemon_auto_start": False, "remote_plugin": False, "apps": False},
    }
    if effort:
        settings["model_reasoning_effort"] = effort
        # Portable CLIs can predate the selected model's built-in metadata.
        settings["model_supports_reasoning_summaries"] = True
    return settings


def redact(text: str | bytes | None, values: dict[str, str]) -> str:
    text = text.decode(errors="replace") if isinstance(text, bytes) else text or ""
    secrets = {v for k, v in values.items() if v and any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    return text


def codex_config_arguments(settings: dict, prefix: str = "") -> list[str]:
    arguments = []
    for key, value in settings.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            arguments.extend(codex_config_arguments(value, name))
        else:
            # JSON strings, booleans, numbers and scalar arrays are valid TOML values.
            arguments.extend(("-c", name + "=" + json.dumps(value, ensure_ascii=False)))
    return arguments
