from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace

from .models import EnvConfig, PipelineConfig, StageFailure

from .paths import PROJECT_RELAY

RELAY_SCRIPT = PROJECT_RELAY


def relay_module():
    name = "task_builder_generic_relay"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, RELAY_SCRIPT)
        if spec is None or spec.loader is None:
            raise StageFailure(f"找不到通用反代脚本: {RELAY_SCRIPT}", stage="proxy")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except (OSError, ImportError):
            sys.modules.pop(name, None)
            raise StageFailure(f"无法加载通用反代脚本: {RELAY_SCRIPT}", stage="proxy") from None
    return sys.modules[name]


def entry_environment(config: EnvConfig, entry: dict) -> EnvConfig:
    module = relay_module()
    values = module.proxy_environment(entry, config.values)
    values["TASK_BUILDER_MODEL_RELAY_ACTIVE"] = "1"
    return replace(config, values=values)


def prepare_model_relay(config: PipelineConfig) -> None:
    module = relay_module()
    try:
        connection = module.read_connection(config.model_relay)
        entries = {}
        selected_envs = (config.author_env,)
        for env_config in selected_envs:
            if str(env_config.path.resolve()) in entries:
                continue
            entry = module.match_entry(connection, env_config.path, env_config.model_id)
            module.check_entry(entry, connection["service_id"])
            entries[str(env_config.path.resolve())] = entry
    except module.RelayError as error:
        raise StageFailure(str(error), stage="proxy") from error
    config.extra["model_relay_entries"] = entries
    config.extra["model_relay_service_id"] = connection["service_id"]
    secrets = config.extra.setdefault("model_secret_values", [])
    for env_config in (config.author_env,):
        secrets.extend(value for name, value in env_config.values.items() if value and any(word in name.upper() for word in ("TOKEN", "KEY", "SECRET", "PASSWORD")))
    secrets.extend(entry["token"] for entry in entries.values())
    config.author_env = entry_environment(config.author_env, entries[str(config.author_env.path.resolve())])


def redact_model_secrets(text: str, config: PipelineConfig) -> str:
    secrets = list(config.extra.get("model_secret_values", []))
    for env_config in (config.author_env,):
        if env_config is not None:
            secrets.extend(value for name, value in env_config.values.items() if value and any(word in name.upper() for word in ("TOKEN", "KEY", "SECRET", "PASSWORD")))
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


def selected_relay_entry(config: PipelineConfig, env_config: EnvConfig) -> dict:
    if "model_relay_entries" not in config.extra:
        prepare_model_relay(config)
    entry = config.extra["model_relay_entries"].get(str(env_config.path.resolve()))
    if entry is None:
        raise StageFailure(f"配置文件未登记反代入口: {env_config.path}", stage="proxy")
    module = relay_module()
    try:
        module.check_entry(entry, config.extra["model_relay_service_id"])
    except module.RelayError as error:
        raise StageFailure(str(error), stage="proxy") from error
    return entry
