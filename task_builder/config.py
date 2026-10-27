from __future__ import annotations

import os
import shlex
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from .models import ConfigError, EnvConfig, ImageEnvConfig, PipelineConfig
from .paths import ENV_IMAGE_CONFIG_NAME, PROJECT_RUBRIC, SCIENV_IMAGE_ROOT


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        try:
            return shlex.split(value, comments=False, posix=True)[0]
        except (IndexError, ValueError):
            return value[1:-1]
    return value


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigError(f"环境配置文件不存在: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"环境配置第 {line_number} 行缺少 '=': {path}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or any(char.isspace() for char in key):
            raise ConfigError(f"环境配置第 {line_number} 行变量名无效: {path}")
        values[key] = _unquote(raw_value)
    return values


def load_agent_env(path: Path, *, role: str) -> EnvConfig:
    values = read_env_file(path)
    missing: list[str] = []
    if not values.get("MODEL_ID"):
        missing.append("MODEL_ID")
    if not values.get("ANTHROPIC_BASE_URL"):
        missing.append("ANTHROPIC_BASE_URL")
    if not values.get("ANTHROPIC_AUTH_TOKEN") and not values.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY")
    if missing:
        raise ConfigError(f"{role} 配置缺少: {', '.join(missing)}")
    if any("<" in value or ">" in value for value in values.values()):
        raise ConfigError(f"{role} 尚未填写实际配置: {path}")
    agent = values.get("AGENT", "claude-code").strip().lower().replace("_", "-")
    if agent in {"claude", "claude-cli"}:
        agent = "claude-code"
    if agent != "claude-code":
        raise ConfigError(f"{role} 必须使用 Claude Code，当前 AGENT={agent!r}")
    return EnvConfig(path=path.resolve(), model_id=values["MODEL_ID"], values=values)


def _toml_table(document: dict, name: str, path: Path) -> dict:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"image-env 缺少 [{name}] 配置: {path}")
    return value


def _toml_string(table: dict, key: str, section: str, path: Path) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip() or "<" in value or ">" in value:
        raise ConfigError(f"image-env [{section}] 的 {key} 必须是非空字符串: {path}")
    return value


def _toml_int(table: dict, key: str, section: str, path: Path) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"image-env [{section}] 的 {key} 必须是整数: {path}")
    return value


def load_image_env(path: str | Path) -> ImageEnvConfig:
    image_env_path = Path(path).expanduser().resolve()
    if not image_env_path.is_file():
        raise ConfigError(f"image-env 文件不存在: {image_env_path}")
    try:
        document = tomllib.loads(image_env_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"image-env 不是有效 TOML: {image_env_path}: {exc}") from exc
    images = _toml_table(document, "images", image_env_path)
    hcluster = _toml_table(document, "hcluster", image_env_path)
    timeouts = _toml_table(document, "timeouts", image_env_path)
    return ImageEnvConfig(
        experiment_image=_toml_string(images, "experiment", "images", image_env_path),
        evaluation_image=_toml_string(images, "evaluation", "images", image_env_path),
        charged_group=_toml_string(hcluster, "charged_group", "hcluster", image_env_path),
        mount=_toml_string(hcluster, "mount", "hcluster", image_env_path),
        namespace=_toml_string(hcluster, "namespace", "hcluster", image_env_path),
        cpu=_toml_int(hcluster, "max_cpu", "hcluster", image_env_path),
        memory_mb=_toml_int(hcluster, "max_memory_mb", "hcluster", image_env_path),
        agent_gpus=_toml_int(hcluster, "max_agent_gpus", "hcluster", image_env_path),
        verifier_gpus=_toml_int(hcluster, "max_verifier_gpus", "hcluster", image_env_path),
        priority=_toml_int(hcluster, "priority", "hcluster", image_env_path),
        agent_timeout_sec=_toml_int(timeouts, "agent_timeout_sec", "timeouts", image_env_path),
        harbor_timeout_sec=_toml_int(timeouts, "harbor_timeout_sec", "timeouts", image_env_path),
    )


def _existing_file(path_value: str | Path, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"{label}不存在: {path}")
    return path


def resolve_environment(sci_env: str, image_env: str | Path | None = None, environment_root: Path = SCIENV_IMAGE_ROOT) -> tuple[Path, Path]:
    """返回 (共享环境目录, 镜像配置文件)。镜像配置优先用显式 --image-env，否则用环境目录下的 image-env.toml。"""
    from .workspace import resolve_environment_dir

    try:
        environment_dir = resolve_environment_dir(sci_env, Path(environment_root or SCIENV_IMAGE_ROOT))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if image_env is not None:
        return environment_dir, _existing_file(image_env, "镜像配置")
    return environment_dir, (environment_dir / ENV_IMAGE_CONFIG_NAME).resolve()


def _env_root_argument(path_value: str | Path, sci_env: str, flag: str) -> Path:
    """路径参数传根目录，环境层由程序追加；误传到环境这一层时报错。"""
    path = Path(path_value).expanduser().resolve()
    if path.name == sci_env:
        raise ConfigError(f"{flag} 请传根目录（如 {path.parent}），环境层 {sci_env}/ 由 --sci-env 自动追加")
    return path


def build_config(
    *,
    input_root: str | Path,
    output_root: str | Path,
    author_env: str | Path,
    sci_env: str,
    environment_root: str | Path = SCIENV_IMAGE_ROOT,
    image_env: str | Path | None = None,
    rubric_path: str | Path | None = None,
    experiment_image: str | None = None,
    evaluation_image: str | None = None,
    charged_group: str | None = None,
    mount: str | None = None,
    namespace: str | None = None,
    cpu: int | None = None,
    memory_mb: int | None = None,
    agent_gpus: int | None = None,
    verifier_gpus: int | None = None,
    priority: int | None = None,
    max_revisions: int = 3,
    concurrency: int = 1,
    proxy_bind: str = "0.0.0.0",
    proxy_port: int = 0,
    proxy_host: str | None = None,
    claude_bin: str = "claude",
    agent_timeout_sec: int | None = None,
    harbor_timeout_sec: int | None = None,
    task_timeout_sec: int = 19800,
    resume_from: str | Path | None = None,
    local_only: bool = False,
    model_relay: str | Path | None = None,
    run_txt: str | Path | None = None,
) -> PipelineConfig:
    environment_dir, image_env_path = resolve_environment(sci_env, image_env, Path(environment_root or SCIENV_IMAGE_ROOT))
    image_values = load_image_env(image_env_path)
    experiment_image = experiment_image if experiment_image is not None else image_values.experiment_image
    evaluation_image = evaluation_image if evaluation_image is not None else image_values.evaluation_image
    charged_group = charged_group if charged_group is not None else image_values.charged_group
    mount = mount if mount is not None else image_values.mount
    namespace = namespace if namespace is not None else image_values.namespace
    cpu = cpu if cpu is not None else image_values.cpu
    memory_mb = memory_mb if memory_mb is not None else image_values.memory_mb
    agent_gpus = agent_gpus if agent_gpus is not None else image_values.agent_gpus
    verifier_gpus = verifier_gpus if verifier_gpus is not None else image_values.verifier_gpus
    priority = priority if priority is not None else image_values.priority
    agent_timeout_sec = agent_timeout_sec if agent_timeout_sec is not None else image_values.agent_timeout_sec
    harbor_timeout_sec = harbor_timeout_sec if harbor_timeout_sec is not None else image_values.harbor_timeout_sec
    if not experiment_image or not evaluation_image:
        raise ConfigError("实验镜像和评测镜像不能为空")
    for name, value in (("cpu", cpu), ("memory_mb", memory_mb), ("priority", priority)):
        if value <= 0:
            raise ConfigError(f"{name} 必须大于 0")
    for name, value in (("agent_gpus", agent_gpus), ("verifier_gpus", verifier_gpus)):
        if value < 0:
            raise ConfigError(f"{name} 不能小于 0")
    for name, value in (("agent_timeout_sec", agent_timeout_sec), ("harbor_timeout_sec", harbor_timeout_sec), ("task_timeout_sec", task_timeout_sec)):
        if value <= 0:
            raise ConfigError(f"{name} 必须大于 0")
    if max_revisions < 0:
        raise ConfigError("--max-revisions 不能小于 0")
    if concurrency <= 0:
        raise ConfigError("--concurrency 必须大于 0")
    if proxy_port < 0 or proxy_port > 65535:
        raise ConfigError("--proxy-port 必须在 0 到 65535 之间")
    if not proxy_bind.strip():
        raise ConfigError("--proxy-bind 不能为空")
    if proxy_host is not None and (not proxy_host.strip() or any(char.isspace() for char in proxy_host) or "/" in proxy_host or "://" in proxy_host):
        raise ConfigError("--proxy-host 只能填写主机名或 IP 地址")
    if rubric_path is None:
        rubric_path = PROJECT_RUBRIC
    resolved_output = _env_root_argument(output_root, sci_env, "--output")
    return PipelineConfig(
        # 输入与输出都按环境再分一层：inputs/<env>/<来源>、<output>/<env>/{final,runs,usage}。
        input_root=_env_root_argument(input_root, sci_env, "--input") / sci_env,
        output_root=resolved_output / sci_env,
        sci_env=sci_env,
        environment_dir=environment_dir,
        author_env=load_agent_env(Path(author_env).expanduser().resolve(), role="author-env"),
        experiment_image=experiment_image,
        evaluation_image=evaluation_image,
        rubric_path=_existing_file(rubric_path, "任务 rubric"),
        image_env_path=image_env_path,
        charged_group=charged_group,
        mount=mount,
        namespace=namespace,
        cpu=cpu,
        memory_mb=memory_mb,
        agent_gpus=agent_gpus,
        verifier_gpus=verifier_gpus,
        priority=priority,
        max_revisions=max_revisions,
        concurrency=concurrency,
        proxy_bind=proxy_bind.strip(),
        proxy_port=proxy_port,
        proxy_host=proxy_host,
        claude_bin=claude_bin,
        agent_timeout_sec=agent_timeout_sec,
        harbor_timeout_sec=harbor_timeout_sec,
        task_timeout_sec=task_timeout_sec,
        resume_from=Path(resume_from).expanduser().resolve() if resume_from else None,
        local_only=local_only,
        model_relay=Path(model_relay).expanduser().absolute() if model_relay else None,
        run_txt=_existing_file(run_txt, "任务名单") if run_txt else None,
    )


def environment_for_subprocess(config: EnvConfig, *, model_id: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if config.values.get("TASK_BUILDER_MODEL_RELAY_ACTIVE") == "1":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
    env.update(config.values)
    env["ANTHROPIC_MODEL"] = model_id or config.model_id
    env["CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"] = "1"
    if not env.get("ANTHROPIC_API_KEY") and env.get("ANTHROPIC_AUTH_TOKEN"):
        env["ANTHROPIC_API_KEY"] = env["ANTHROPIC_AUTH_TOKEN"]
    return env
