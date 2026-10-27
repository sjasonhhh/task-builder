from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import PROJECT_RUBRIC


@dataclass(frozen=True)
class EnvConfig:
    path: Path
    model_id: str
    values: dict[str, str]
    agent: str = "claude-code"


@dataclass(frozen=True)
class ImageEnvConfig:
    experiment_image: str
    evaluation_image: str
    charged_group: str
    mount: str
    namespace: str
    # 以下四项是单个 RJob 的资源上限；实际申请量取自任务 task.toml 的 [environment]。
    cpu: int
    memory_mb: int
    agent_gpus: int
    verifier_gpus: int
    priority: int
    agent_timeout_sec: int
    harbor_timeout_sec: int


@dataclass(frozen=True)
class SourceBundle:
    source_dir: Path
    source_slug: str
    context: Path
    data: Path
    code: Path


@dataclass
class PipelineConfig:
    input_root: Path
    output_root: Path
    author_env: EnvConfig
    experiment_image: str
    evaluation_image: str
    image_env_path: Path
    sci_env: str = ""
    environment_dir: Path | None = None
    rubric_path: Path = PROJECT_RUBRIC
    charged_group: str = ""
    mount: str = ""
    namespace: str = ""
    # 单个 RJob 的资源上限，见 hcluster_runtime 中的资源申请逻辑。
    cpu: int = 4
    memory_mb: int = 8192
    agent_gpus: int = 0
    verifier_gpus: int = 0
    priority: int = 9
    max_revisions: int = 3
    concurrency: int = 1
    proxy_bind: str = "0.0.0.0"
    proxy_port: int = 0
    proxy_host: str | None = None
    claude_bin: str = "claude"
    agent_timeout_sec: int = 3600
    harbor_timeout_sec: int = 3600
    task_timeout_sec: int = 19800
    resume_from: Path | None = None
    local_only: bool = False
    model_relay: Path | None = None
    run_txt: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationIssue:
    scope: str
    message: str
    details: str = ""

    def render(self) -> str:
        suffix = f"：{self.details}" if self.details else ""
        return f"[{self.scope}] {self.message}{suffix}"


@dataclass(frozen=True)
class ValidationReport:
    stage: str
    passed: bool
    issues: tuple[ValidationIssue, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        if self.passed:
            return f"{self.stage}: PASS"
        return "\n".join([f"{self.stage}: FAIL", *(issue.render() for issue in self.issues)])


class PipelineError(RuntimeError):
    def __init__(self, message: str, *, stage: str | None = None):
        super().__init__(message)
        self.stage = stage


class ConfigError(ValueError):
    pass


class AgentError(PipelineError):
    pass


class StageFailure(PipelineError):
    pass


class TaskTimeout(StageFailure):
    def __init__(self, message: str):
        super().__init__(message, stage="task-timeout")
