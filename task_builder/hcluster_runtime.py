"""Harbor environment backed by an H-cluster RJob.

This module intentionally owns only the runtime bridge. It does not import or
call the benchmark repository's runner scripts.
"""
from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
import shlex
import subprocess
import time
import uuid
import shutil
import tarfile
import hashlib
import tempfile
import select
from pathlib import Path
from typing import Any

from .integrity import content_manifest, file_digest, manifest_differences
from .deadline import remaining_timeout
from .models import TaskTimeout

try:
    from harbor.environments.base import BaseEnvironment, ExecResult, HealthcheckError
    from harbor.environments.capabilities import EnvironmentCapabilities, EnvironmentResourceCapabilities
    from harbor.environments.definition import parse_dockerfile_workdir
except ImportError:  # pragma: no cover - lets local unit tests import the module without Harbor
    BaseEnvironment = object  # type: ignore[assignment,misc]

    class ExecResult:  # type: ignore[no-redef]
        def __init__(self, *, stdout: str | None = None, stderr: str | None = None, return_code: int = 0):
            self.stdout, self.stderr, self.return_code = stdout, stderr, return_code

    class HealthcheckError(RuntimeError):
        pass

    class EnvironmentCapabilities:  # type: ignore[no-redef]
        def __init__(self, **_: Any):
            pass

    class EnvironmentResourceCapabilities:  # type: ignore[no-redef]
        def __init__(self, **_: Any):
            pass

    def parse_dockerfile_workdir(_: Path) -> str | None:
        return None


_JOB_PATTERN = re.compile(r"rjob\s+(?P<name>\S+)\s+\(showname=(?P<showname>\S+)\)", re.IGNORECASE)
_REPLICA_PATTERN = re.compile(r"replica\s+(?P<name>\S+):\s+(?P<status>[A-Z]+)", re.IGNORECASE)
_CREATED_JOB_PATTERN = re.compile(r"created\s+rjob_name:\s*(?P<name>\S+)", re.IGNORECASE)

_RJOB_QUERY_TIMEOUT_SEC = 60.0
_RJOB_POLL_INTERVAL_SEC = 15.0
_RJOB_STARTUP_GRACE_SEC = 120.0
_RJOB_COMMAND_TIMEOUT_SEC = 180.0
_RJOB_SHUTDOWN_GRACE_SEC = 120.0
_RJOB_SHUTDOWN_TIMEOUT_SEC = 300.0
_UPLOAD_CHUNK_BYTES = 16 * 1024 * 1024

# 任务 task.toml 未声明资源时的默认申请量。
DEFAULT_CPUS = 4
DEFAULT_MEMORY_MB = 8192
DEFAULT_GPUS = 1
THREAD_ENV_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")


class _UploadSourceChanged(RuntimeError):
    pass


class _UploadCleanupUnconfirmed(RuntimeError):
    pass


def resolve_claude_source(claude_bin: str = "claude") -> Path:
    override = os.environ.get("TASK_BUILDER_CLAUDE_SOURCE")
    candidate = override or shutil.which(claude_bin)
    if not candidate:
        candidate = str(Path(claude_bin).expanduser()) if "/" in claude_bin else str(Path.home() / ".local/bin/claude")
    source = Path(candidate).expanduser().resolve()
    if not source.is_file() or not os.access(source, os.X_OK):
        raise FileNotFoundError(f"找不到可运行的 Claude Code: {source}")
    return source


class TaskBuilderRJobEnvironment(BaseEnvironment):
    """Run one Harbor agent or verifier container in one H-cluster RJob."""

    def __init__(self, *args: Any, **kwargs: Any):
        self._job_name: str | None = None
        self._rjob_name: str | None = None
        self._replica_name: str | None = None
        self._is_verifier = False
        self._last_job_text = ""
        self._last_query_timed_out = False
        self._submission_uncertain = False
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "task-builder-rjob"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(gpus=True, disable_internet=True, mounted=False, docker_compose=False)

    @classmethod
    def resource_capabilities(cls):
        return None

    def _validate_definition(self) -> None:
        if not self.environment_dir.is_dir():
            raise FileNotFoundError(f"环境目录不存在: {self.environment_dir}")
        if not self.task_env_config.docker_image:
            raise ValueError("H 集群运行必须在环境配置中提供 docker_image")

    @property
    def _default_cwd(self) -> str:
        if self.task_env_config.workdir:
            return self.task_env_config.workdir
        return parse_dockerfile_workdir(self.environment_dir / "Dockerfile") or "/workspace"

    @property
    def _namespace(self) -> str:
        return getattr(self, "_cleanup_namespace", None) or os.environ.get("TASK_BUILDER_RJOB_NAMESPACE", "")

    @property
    def _charged_group(self) -> str:
        return os.environ.get("TASK_BUILDER_CHARGED_GROUP", "")

    @property
    def _mount(self) -> str:
        return os.environ.get("TASK_BUILDER_RJOB_MOUNT", "")

    def _bounded(self, name: str, requested: int | None, default: int, limit_var: str) -> str:
        """按任务 task.toml 申请资源，未声明时用默认值，超过 image-env 上限时截断。"""
        value = default if requested is None else int(requested)
        limit = os.environ.get(limit_var)
        if limit is not None and value > int(limit):
            self._runtime_event("resource-capped", resource=name, requested=value, limit=int(limit))
            value = int(limit)
        return str(value)

    @property
    def _cpu(self) -> str:
        return self._bounded("cpu", self.task_env_config.cpus, DEFAULT_CPUS, "TASK_BUILDER_RJOB_MAX_CPU")

    @property
    def _memory(self) -> str:
        return self._bounded("memory_mb", self.task_env_config.memory_mb, DEFAULT_MEMORY_MB, "TASK_BUILDER_RJOB_MAX_MEMORY_MB")

    @property
    def _gpus(self) -> str:
        limit_var = "TASK_BUILDER_MAX_VERIFIER_GPUS" if self._is_verifier else "TASK_BUILDER_MAX_AGENT_GPUS"
        return self._bounded("gpus", self.task_env_config.gpus, DEFAULT_GPUS, limit_var)

    @staticmethod
    def _decode(value: bytes | None) -> str:
        return (value or b"").decode("utf-8", errors="replace")

    def _run(self, args: list[str], *, input_data: bytes | None = None, timeout: float = 180.0) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(args, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)

    def _submit(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        for attempt in range(2):
            try:
                result = self._run(args, timeout=self._duration_env("TASK_BUILDER_RJOB_SUBMIT_TIMEOUT_SEC", 300.0))
                self._runtime_event("submit", attempt=attempt, stdout=self._decode(result.stdout), stderr=self._decode(result.stderr), returncode=result.returncode)
                text = self._decode(result.stdout) + self._decode(result.stderr)
                if result.returncode != 0 and re.search(r"ReadTimeout|TimeoutError|Read timed out|ConnectionError|Max retries exceeded", text):
                    raise subprocess.TimeoutExpired(args, 300, output=result.stdout, stderr=result.stderr)
                return result
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or b""
                errors = exc.stderr or b""
                partial = (output.decode(errors="replace") if isinstance(output, bytes) else output) + (errors.decode(errors="replace") if isinstance(errors, bytes) else errors)
                created = _CREATED_JOB_PATTERN.search(partial)
                if created:
                    self._rjob_name = created.group("name")
                text = self._job_text()
                self._runtime_event("submit-timeout", attempt=attempt, output=partial, query=text)
                if created or _JOB_PATTERN.search(text):
                    return subprocess.CompletedProcess(args, 0, partial.encode(), b"")
                confirmed_absent = not self._last_query_timed_out and bool(re.search(r"(?:rjob|job)\s+[^\n]*\bnot found\b|no rjobs? found|未找到.*(?:rjob|任务)", text, re.IGNORECASE))
                if not confirmed_absent:
                    self._submission_uncertain = True
                    raise RuntimeError("RJob 提交超时，状态不明；已保留日志，禁止重复提交或自动停止/删除") from exc
                if attempt == 1:
                    raise RuntimeError("RJob 提交两次超时，查询确认未创建；已停止重试") from exc
        raise AssertionError("unreachable")

    def _runtime_event(self, event: str, **details: Any) -> None:
        payload = {"time": time.time(), "event": event, "showname": self._job_name, "rjob_name": self._rjob_name, **details}
        for name in ("TASK_BUILDER_PROXY_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            token = os.environ.get(name)
            if token:
                payload = json.loads(json.dumps(payload).replace(token, "<redacted>"))
        root = Path(getattr(self, "_cleanup_log_dir", None) or os.environ.get("TASK_BUILDER_RUNTIME_LOG_DIR", "harbor-runtime-logs"))
        root.mkdir(parents=True, exist_ok=True)
        with (root / f"{self._job_name or 'runtime'}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _duration_env(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return max(0.0, float(raw))
        except ValueError:
            return default

    @property
    def _rjob_query_timeout(self) -> float:
        return self._duration_env("TASK_BUILDER_RJOB_QUERY_TIMEOUT_SEC", _RJOB_QUERY_TIMEOUT_SEC)

    @property
    def _rjob_poll_interval(self) -> float:
        return self._duration_env("TASK_BUILDER_RJOB_POLL_INTERVAL_SEC", _RJOB_POLL_INTERVAL_SEC)

    @property
    def _rjob_startup_grace(self) -> float:
        return self._duration_env("TASK_BUILDER_RJOB_STARTUP_GRACE_SEC", _RJOB_STARTUP_GRACE_SEC)

    @property
    def _rjob_command_timeout(self) -> float:
        return self._duration_env("TASK_BUILDER_RJOB_COMMAND_TIMEOUT_SEC", _RJOB_COMMAND_TIMEOUT_SEC)

    @property
    def _rjob_shutdown_grace(self) -> float:
        return self._duration_env("TASK_BUILDER_RJOB_SHUTDOWN_GRACE_SEC", _RJOB_SHUTDOWN_GRACE_SEC)

    @property
    def _rjob_shutdown_timeout(self) -> float:
        return self._duration_env("TASK_BUILDER_RJOB_SHUTDOWN_TIMEOUT_SEC", _RJOB_SHUTDOWN_TIMEOUT_SEC)

    def _job_query_args(self) -> list[str]:
        if self._rjob_name:
            args = ["rjob", "get", self._rjob_name]
        else:
            args = ["rjob", "get", "--name", self._job_name or ""]
        args.extend(["--namespace", self._namespace])
        return args

    def _job_text(self) -> str:
        if not self._job_name:
            return ""
        timeout = self._rjob_query_timeout
        try:
            result = self._run(self._job_query_args(), timeout=timeout)
        except subprocess.TimeoutExpired:
            self._last_query_timed_out = True
            self._last_job_text = f"rjob get 查询暂时超时（已等待 {timeout:g} 秒），将在下一轮继续查询"
            self._runtime_event("query-timeout", timeout=timeout)
            return self._last_job_text
        text = self._decode(result.stdout) + self._decode(result.stderr)
        self._last_query_timed_out = False
        self._last_query_returncode = result.returncode
        self._last_job_text = text
        match = _JOB_PATTERN.search(text)
        if match:
            self._rjob_name = match.group("name")
        return text

    def _job_replica_prefix(self) -> str:
        return self._rjob_name or self._job_name or ""

    def _wait(self, deadline: float) -> None:
        deadline = max(deadline, time.monotonic() + self._rjob_startup_grace)
        while time.monotonic() < deadline:
            text = self._job_text()
            for match in _REPLICA_PATTERN.finditer(text):
                name, status = match.group("name"), match.group("status").upper()
                prefix = self._job_replica_prefix()
                if prefix and name.startswith(prefix + "-"):
                    self._replica_name = name
                    if status in {"RUNNING", "SUCCEEDED", "SUCCEED", "COMPLETED"}:
                        try:
                            if self._brainctl_exec("true", timeout=60).return_code == 0:
                                return
                        except subprocess.TimeoutExpired:
                            pass
                    if status in {"FAILED", "STOPPED", "DELETED"}:
                        raise RuntimeError(f"RJob 进入 {status}: {text}")
            time.sleep(self._rjob_poll_interval)
        raise TimeoutError(f"等待 RJob 超时: {self._last_job_text or '没有收到有效的 RJob 状态'}")

    def _submit_args(self) -> list[str]:
        image = self.task_env_config.docker_image
        if self._is_verifier:
            image = os.environ.get("TASK_BUILDER_EVALUATION_IMAGE", image)
        assert image
        args = [
            "rjob", "submit", f"--name={self._job_name}",
            f"--namespace={getattr(self, '_namespace', os.environ.get('TASK_BUILDER_RJOB_NAMESPACE', ''))}",
            f"--charged-group={self._charged_group}", "--private-machine=group",
            f"--cpu={self._cpu}", f"--memory={self._memory}", "-P", "1",
            f"--gpu={self._gpus}", f"--mount={self._mount}",
            f"--image={image}", "--image-pull-policy=IfNotPresent",
            "--priority=" + os.environ.get("TASK_BUILDER_PRIORITY", "9"),
            # lxcfs 让容器内 /proc/cpuinfo、/proc/meminfo 显示本 RJob 的配额，而不是整台节点。
            "--enable-lxcfs=true",
        ]
        threads = self._cpu
        args.extend(f"--set-env={key}={threads}" for key in THREAD_ENV_VARS)
        if not self._is_verifier:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
                if os.environ.get(key):
                    args.append(f"--set-env={key}={os.environ[key]}")
        return args

    def _brainctl_raw(self, command: str, *, cwd: str | None = None, env: dict[str, str] | None = None, input_data: bytes | None = None, timeout: float = 86400.0) -> subprocess.CompletedProcess[bytes]:
        if not self._replica_name:
            raise RuntimeError("RJob replica 尚未启动")
        parts: list[str] = []
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)}")
        if env:
            parts.extend(f"export {key}={shlex.quote(str(value))}" for key, value in env.items())
        parts.append(command)
        remote = " && ".join(parts)
        args = ["brainctl", "exec", "--namespace=" + self._namespace, "--pod-running-timeout=5m"]
        if input_data is not None:
            args.append("-i")
        args.extend(["replicas/" + self._replica_name, "--", "bash", "-lc", remote])
        return self._run(args, input_data=input_data, timeout=timeout)

    def _brainctl_exec(self, command: str, *, cwd: str | None = None, env: dict[str, str] | None = None, input_data: bytes | None = None, timeout: float = 86400.0) -> ExecResult:
        result = self._brainctl_raw(command, cwd=cwd, env=env, input_data=input_data, timeout=timeout)
        return ExecResult(stdout=self._decode(result.stdout), stderr=self._decode(result.stderr), return_code=result.returncode)

    async def start(self, force_build: bool) -> None:
        del force_build
        environment_name = str(self.environment_name).lower()
        session_id = str(self.session_id).lower()
        self._is_verifier = (
            self.environment_dir.name.lower() in {"tests", "verifier"}
            or (self.environment_dir / "test.sh").is_file()
            or "verifier" in environment_name
            or "verifier" in session_id
        )
        self._job_name = "hb-" + re.sub(r"[^a-z0-9-]", "-", environment_name)[:24] + ("-ver-" if self._is_verifier else "-agt-") + str(abs(hash((self.session_id, time.time_ns()))) % 100000000)
        result = self._submit(self._submit_args() + ["--", "bash", "-lc", "exec sleep 172800"])
        if result.returncode != 0:
            raise RuntimeError("提交 RJob 失败:\n" + self._decode(result.stdout) + self._decode(result.stderr))
        submission_text = self._decode(result.stdout) + self._decode(result.stderr)
        created_match = _CREATED_JOB_PATTERN.search(submission_text)
        if created_match:
            self._rjob_name = created_match.group("name")
        self._runtime_event("submitted")
        self._wait(time.monotonic() + 900)
        prepared = self._brainctl_exec("mkdir -p /workspace /tests /logs/agent /logs/verifier /logs/artifacts /state", timeout=180)
        if prepared.return_code != 0:
            raise RuntimeError(prepared.stderr or prepared.stdout or "准备远程目录失败")
        if not self._is_verifier:
            await self.upload_dir(self.environment_dir, self._default_cwd)
            data_dir = self.environment_dir / "data"
            remote_data = posixpath.join(self._default_cwd, "data")
            if data_dir.is_dir():
                link = self._brainctl_exec(
                    f"mkdir -p /app && rm -rf /app/inputs && ln -s {shlex.quote(remote_data)} /app/inputs",
                    timeout=60,
                )
                if link.return_code != 0:
                    raise RuntimeError(link.stderr or link.stdout or "创建 /app/inputs 输入路径失败")
            await self._install_claude_runtime()
        else:
            tests_dir = self.environment_dir if (self.environment_dir / "test.sh").is_file() else self.environment_dir.parent / "tests"
            if tests_dir.is_dir():
                await self.upload_dir(tests_dir, "/tests")
            data_dir = self.environment_dir.parent / "data"
            remote_data = "/workspace/data"
            if data_dir.is_dir():
                await self.upload_dir(data_dir, remote_data)
        if data_dir.is_dir():
            self._checked_exec(f"mkdir -p /app /app/results && rm -rf /app/inputs && ln -s {shlex.quote(remote_data)} /app/inputs")

    async def _install_claude_runtime(self) -> None:
        source = resolve_claude_source()
        await self.upload_file(source, "/usr/local/bin/claude")
        result = self._brainctl_exec("chmod 755 /usr/local/bin/claude", timeout=60)
        if result.return_code != 0:
            raise RuntimeError(result.stderr or result.stdout or "安装 Claude Code 失败")

    async def stop(self, delete: bool) -> None:
        if not self._job_name or self._submission_uncertain:
            return
        action = "delete" if delete else "stop"
        if self._rjob_name:
            command = ["rjob", action, self._rjob_name]
        else:
            command = ["rjob", action, "--name", self._job_name]
        job_name = self._rjob_name
        self._runtime_event("shutdown-request", action=action)
        try:
            self._run(command + ["--namespace", self._namespace], timeout=self._rjob_command_timeout)
        except subprocess.TimeoutExpired:
            pass
        self._replica_name = None
        await self._wait_for_shutdown()
        self._runtime_event("shutdown-complete", stopped_rjob_name=job_name)

    async def _wait_for_shutdown(self) -> None:
        started = time.monotonic()
        grace_deadline = started + self._rjob_shutdown_grace
        deadline = grace_deadline + self._rjob_shutdown_timeout
        saw_confirmed_job = False
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now < grace_deadline:
                await asyncio.sleep(min(self._rjob_poll_interval, grace_deadline - now))
                continue
            text = await asyncio.to_thread(self._job_text)
            if self._last_query_timed_out:
                await asyncio.sleep(self._rjob_poll_interval)
                continue
            if _JOB_PATTERN.search(text):
                saw_confirmed_job = True
                await asyncio.sleep(self._rjob_poll_interval)
                continue
            absent = not text.strip() and getattr(self, "_last_query_returncode", None) == 0
            absent = absent or bool(re.search(r"(?:rjob|job)\s+[^\n]*\bnot found\b|no rjobs? found|未找到.*(?:rjob|任务)", text, re.IGNORECASE))
            if not absent:
                await asyncio.sleep(self._rjob_poll_interval)
                continue
            self._rjob_name = None
            return
        status = "仍能查询到 RJob" if saw_confirmed_job else "查询一直没有给出可确认的删除结果"
        raise TimeoutError(f"等待 RJob 删除确认超时（已等待约 {self._rjob_shutdown_timeout:g} 秒；{status}）")

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None, timeout_sec: int | None = None, user: str | int | None = None) -> ExecResult:
        del user
        return await asyncio.to_thread(self._brainctl_exec, command, cwd=cwd or self._default_cwd, env=env, timeout=float(timeout_sec or 86400) + 120)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        await asyncio.to_thread(self._verified_upload, Path(source_path), target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await asyncio.to_thread(self._verified_upload, Path(source_dir), target_dir)

    def _checked_exec(self, command: str) -> None:
        result = self._brainctl_exec(command, timeout=180)
        if result.return_code != 0:
            raise RuntimeError(result.stderr or result.stdout or "远程文件操作失败")

    def _remote_manifest(self, path: str) -> dict:
        program = '''import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
if not root.exists(): raise FileNotFoundError(str(root))
result={}
paths=[root] if root.is_file() else sorted(root.rglob("*"))
for path in paths:
 name="." if path==root else path.relative_to(root).as_posix()
 if path.is_symlink(): result[name]={"kind":"symlink","target":str(path.readlink())}
 elif path.is_file():
  digest=hashlib.sha256()
  with path.open("rb") as handle:
   for chunk in iter(lambda:handle.read(1048576),b""): digest.update(chunk)
  result[name]={"kind":"file","size":path.stat().st_size,"sha256":digest.hexdigest()}
 elif path.is_dir(): result[name]={"kind":"directory"}
print(json.dumps(result))'''
        result = self._brainctl_exec(f"python3 -c {shlex.quote(program)} {shlex.quote(path)}", timeout=1800)
        if result.return_code != 0:
            raise RuntimeError(result.stderr or result.stdout or "远程指纹检查失败")
        manifest = json.loads(result.stdout or "")
        if not isinstance(manifest, dict):
            raise RuntimeError("远程文件清单不是 JSON 对象")
        return manifest

    def _verified_upload(self, source: Path, target: str) -> None:
        if not source.exists() or source.is_symlink():
            raise FileNotFoundError(f"上传源文件不存在或不是独立文件: {source}")
        expected = content_manifest(source)
        stage = target.rstrip("/") + ".upload-" + uuid.uuid4().hex
        failure = ""
        transaction = stage + ".transaction"
        quoted_target = shlex.quote(target)
        quoted_stage = shlex.quote(stage)
        quoted_transaction = shlex.quote(transaction)
        backup = shlex.quote(transaction + "/previous")
        rollback = f"if [ -d {quoted_transaction} ]; then if [ ! -e {quoted_transaction}/committed ]; then if [ -e {backup} ] || [ -L {backup} ]; then rm -rf {quoted_target} && mv {backup} {quoted_target} || exit 1; elif [ -e {quoted_transaction}/new-target ]; then rm -rf {quoted_target} || exit 1; fi; fi; rm -rf {quoted_transaction} || exit 1; fi; rm -rf {quoted_stage} && test ! -e {quoted_stage} && test ! -e {quoted_transaction}"
        for attempt in range(3):
            try:
                if content_manifest(source) != expected:
                    raise _UploadSourceChanged("上传过程中本地源文件发生变化")
                self._checked_exec(f"rm -rf {shlex.quote(stage)} && mkdir -p {shlex.quote(str(Path(stage).parent))}")
                result = self._brainctl_cp_to(source, stage)
                if result.returncode != 0:
                    raise RuntimeError(self._decode(result.stderr) or "上传失败")
                if content_manifest(source) != expected:
                    raise _UploadSourceChanged("上传过程中本地源文件发生变化")
                differences = manifest_differences(expected, self._remote_manifest(stage))
                if differences:
                    raise RuntimeError("缺失或内容不一致: " + ", ".join(differences))
                self._checked_exec(f"mkdir {quoted_transaction} && if [ -e {quoted_target} ] || [ -L {quoted_target} ]; then mv {quoted_target} {backup}; else touch {quoted_transaction}/new-target; fi && mv {quoted_stage} {quoted_target}")
                actual = self._remote_manifest(target)
                differences = manifest_differences(expected, actual)
                if differences:
                    raise RuntimeError("正式位置检查失败: " + ", ".join(differences))
                self._checked_exec(f"touch {quoted_transaction}/committed && rm -rf {quoted_transaction} && test ! -e {quoted_transaction}")
                self._runtime_event("upload-verified", source=str(source), target=target, attempt=attempt, manifest=expected)
                return
            except TaskTimeout:
                raise
            except _UploadCleanupUnconfirmed as exc:
                self._runtime_event("upload-failed", source=str(source), target=target, attempt=attempt, error=str(exc))
                raise
            except _UploadSourceChanged:
                raise
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                failure = str(exc)
                self._runtime_event("upload-failed", source=str(source), target=target, attempt=attempt, error=failure)
            finally:
                try:
                    self._checked_exec(rollback)
                except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    self._runtime_event("upload-cleanup-failed", directory=stage, error=str(exc))
                    self._runtime_event("upload-failed", source=str(source), target=target, attempt=attempt, error=f"上传临时文件清理或原文件恢复未确认: {stage}: {exc}")
                    raise _UploadCleanupUnconfirmed(f"上传临时文件清理或原文件恢复未确认: {stage}") from exc
        raise RuntimeError(f"上传完整性检查失败，已重传两次；任务禁止启动: {source} -> {target}: {failure}")

    def _brainctl_cp_to(self, source: Path, target: str) -> subprocess.CompletedProcess[bytes]:
        if not self._replica_name:
            raise RuntimeError("RJob replica 尚未启动")
        for path in source.rglob("*") if source.is_dir() else (source,):
            if path.is_symlink() and (path.readlink().is_absolute() or not path.resolve().is_relative_to(source.resolve())):
                raise RuntimeError(f"上传目录含越界链接: {path}")
            if not (path.is_symlink() or path.is_file() or path.is_dir()):
                raise RuntimeError(f"上传目录含不支持的文件类型: {path}")
        temporary_dir = str(Path(target).parent / (".task-builder-upload-" + uuid.uuid4().hex))
        created = False
        active_error = None
        try:
            created = True
            self._transfer_call("init", temporary_dir)
            with tempfile.TemporaryDirectory(prefix="task-builder-upload-", dir=os.environ.get("TASK_BUILDER_TMPDIR")) as local_dir:
                archive = Path(local_dir) / "archive.tar"
                with tarfile.open(archive, "w", dereference=False) as bundle:
                    bundle.add(source, arcname=source.name)
                size = archive.stat().st_size
                digest = file_digest(archive)
                slots = []
                with archive.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(_UPLOAD_CHUNK_BYTES), b""):
                        slots.append(self._send_upload_chunk(temporary_dir, len(slots), chunk))
                count = len(slots)
                expected = {"count": count, "target": target, "size": size, "sha256": digest}
                try:
                    self._transfer_call("assemble", temporary_dir, ",".join(str(slot) for slot in slots), size, digest, source.name, target, timeout=1800)
                except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                    self._runtime_event("upload-assemble-unconfirmed", directory=temporary_dir, error=str(error))
                complete = self._wait_upload_record("complete", temporary_dir, timeout=1800)
                if complete != expected:
                    raise RuntimeError("上传完成确认与本地文件包不一致")
                self._runtime_event("upload-archive-confirmed", source=str(source), target=target, bytes=size, chunks=count)
                return subprocess.CompletedProcess([], 0, b"", b"")
        except BaseException as error:
            active_error = error
            raise
        finally:
            if created:
                try:
                    self._transfer_call("cleanup", temporary_dir)
                    self._transfer_call("absent", temporary_dir)
                except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                    self._runtime_event("upload-cleanup-failed", directory=temporary_dir, error=str(error))
                    if isinstance(active_error, TaskTimeout):
                        raise active_error from error
                    raise _UploadCleanupUnconfirmed(f"上传临时目录删除未确认: {temporary_dir}") from error

    def _transfer_command(self, action: str, directory: str, *arguments, stdin: bool = False) -> list[str]:
        program = Path(__file__).with_name("transfer_receiver.py").read_text(encoding="utf-8")
        command = ["brainctl", "exec", "--namespace=" + self._namespace, "--pod-running-timeout=5m"]
        if stdin:
            command.append("-i")
        command.extend(["replicas/" + self._replica_name, "--", "python3", "-c", program, action, directory, *(str(value) for value in arguments)])
        return command

    def _transfer_call(self, action: str, directory: str, *arguments, timeout: float = 180) -> dict:
        command = self._transfer_command(action, directory, *arguments)
        result = self._run(command, timeout=remaining_timeout(timeout))
        if result.returncode != 0:
            raise RuntimeError(f"上传 {action} 阶段失败: {self._decode(result.stderr)}")
        record = json.loads(result.stdout or b"")
        if not isinstance(record, dict):
            raise RuntimeError(f"上传 {action} 阶段没有返回确认记录")
        return record

    def _wait_upload_record(self, action: str, directory: str, *arguments, timeout: float = 10) -> dict:
        deadline = time.monotonic() + remaining_timeout(timeout)
        while True:
            try:
                return self._transfer_call(action, directory, *arguments, timeout=max(0.1, deadline - time.monotonic()))
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(remaining_timeout(min(0.25, deadline - time.monotonic())))

    def _send_upload_chunk(self, directory: str, index: int, chunk: bytes) -> int:
        digest = hashlib.sha256(chunk).hexdigest()
        failure = ""
        for attempt in range(3):
            slot = index * 3 + attempt
            expected = {"index": slot, "size": len(chunk), "sha256": digest}
            try:
                command = self._transfer_command("receive", directory, slot, len(chunk), digest, stdin=True)
                acknowledgment, returncode = self._stream_upload_chunk(command, chunk, directory, slot)
                if acknowledgment != expected:
                    raise RuntimeError(f"第 {index} 块接收确认与发送内容不一致: {acknowledgment}")
                self._runtime_event("upload-chunk-confirmed", directory=directory, index=index, bytes=len(chunk), sha256=digest, attempt=attempt, returncode=returncode)
                return slot
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                failure = str(error)
                self._runtime_event("upload-chunk-failed", directory=directory, index=index, expected_bytes=len(chunk), attempt=attempt, error=failure)
                if attempt < 2:
                    time.sleep(remaining_timeout((1, 3)[attempt]))
        raise RuntimeError(f"第 {index} 块上传未确认，已尝试 3 次: {failure}")

    def _open_upload_process(self, command: list[str]) -> subprocess.Popen:
        return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _stream_upload_chunk(self, command: list[str], chunk: bytes, directory: str, slot: int) -> tuple[dict, int | None]:
        process = self._open_upload_process(command)
        try:
            deadline = time.monotonic() + remaining_timeout(180)
            assert process.stdin is not None
            descriptor = process.stdin.fileno()
            os.set_blocking(descriptor, False)
            payload = memoryview(chunk)
            offset = 0
            while offset < len(payload):
                timeout = remaining_timeout(deadline - time.monotonic())
                if timeout <= 0:
                    raise subprocess.TimeoutExpired("brainctl upload chunk", 180)
                ready = select.select([], [descriptor], [], timeout)[1]
                if not ready:
                    raise subprocess.TimeoutExpired("brainctl upload chunk", 180)
                try:
                    offset += os.write(descriptor, payload[offset:offset + 1024 * 1024])
                except BlockingIOError:
                    continue
            acknowledgment = self._wait_upload_record("status", directory, slot, timeout=min(30, max(0.1, deadline - time.monotonic())))
            process.stdin.close()
            process.stdin = None
            try:
                process.communicate(timeout=remaining_timeout(15))
            except subprocess.TimeoutExpired:
                self._runtime_event("upload-chunk-exit-unconfirmed", directory=directory, slot=slot)
            return acknowledgment, process.returncode
        finally:
            if process.poll() is None:
                process.kill()
            if process.stdin is not None:
                process.stdin.close()
                process.stdin = None
            process.communicate(timeout=5)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._verified_download, source_path, target)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        target = Path(target_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._verified_download, source_dir, target)

    def _brainctl_cp_from(self, source: str, target: Path) -> subprocess.CompletedProcess[bytes]:
        remote = Path(source.rstrip("/") or "/")
        command = ["brainctl", "exec", "--namespace=" + self._namespace, "--pod-running-timeout=5m", "replicas/" + self._replica_name, "--", "tar", "cf", "-", "-C", str(remote.parent), remote.name or "."]
        archive = target.with_name(target.name + ".tar")
        extraction = target.with_name(target.name + ".extract")
        try:
            with archive.open("wb") as output:
                result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, timeout=900, check=False)
            if result.returncode != 0:
                return subprocess.CompletedProcess(command, result.returncode, b"", result.stderr)
            extraction.mkdir()
            with tarfile.open(archive) as bundle:
                for member in bundle.getmembers():
                    parts = Path(member.name).parts
                    if remote.name and (not parts or parts[0] != remote.name):
                        raise ValueError("下载文件包含预期目录之外的路径")
                bundle.extractall(extraction, filter="data")
            extracted = extraction / remote.name if remote.name else extraction
            extracted.replace(target)
            return subprocess.CompletedProcess(command, 0, b"", result.stderr)
        finally:
            archive.unlink(missing_ok=True)
            if extraction.exists():
                shutil.rmtree(extraction)

    def _verified_download(self, source: str, target: Path) -> None:
        expected = self._remote_manifest(source)
        stage = target.with_name(target.name + ".download-" + uuid.uuid4().hex)
        failure = ""
        for attempt in range(3):
            try:
                if stage.is_dir():
                    shutil.rmtree(stage)
                else:
                    stage.unlink(missing_ok=True)
                result = self._brainctl_cp_from(source, stage)
                if result.returncode != 0:
                    raise RuntimeError(self._decode(result.stderr) or "下载失败")
                differences = manifest_differences(expected, content_manifest(stage))
                if differences:
                    raise RuntimeError("下载缺失或内容不一致: " + ", ".join(differences))
                if self._remote_manifest(source) != expected:
                    raise RuntimeError("下载过程中远程文件发生变化")
                if stage.is_dir() and target.exists():
                    shutil.copytree(stage, target, dirs_exist_ok=True)
                    shutil.rmtree(stage)
                else:
                    stage.replace(target)
                actual = content_manifest(target)
                differences = manifest_differences(expected, {name: actual[name] for name in expected if name in actual})
                if differences:
                    raise RuntimeError("下载正式位置检查失败: " + ", ".join(differences))
                self._runtime_event("download-verified", source=source, target=str(target), attempt=attempt)
                return
            except (RuntimeError, OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
                failure = str(exc)
                self._runtime_event("download-failed", source=source, target=str(target), attempt=attempt, error=failure)
        if stage.is_dir() and not stage.is_symlink():
            shutil.rmtree(stage)
        else:
            stage.unlink(missing_ok=True)
        raise RuntimeError(f"下载完整性检查失败，已重试两次: {failure}")

    async def healthcheck(self) -> None:
        result = await asyncio.to_thread(self._brainctl_exec, "true", timeout=30)
        if result.return_code != 0:
            raise HealthcheckError(result.stderr or result.stdout or "brainctl healthcheck failed")


async def cleanup_logged_rjobs(log_dir: Path, namespace: str) -> list[str]:
    failures = []
    for log in log_dir.glob("*.jsonl"):
        entries = []
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not entries or any(entry.get("event") == "shutdown-complete" for entry in entries):
            continue
        job_name = next((entry.get("rjob_name") for entry in reversed(entries) if entry.get("rjob_name")), None)
        showname = next((entry.get("showname") for entry in reversed(entries) if entry.get("showname")), None)
        if not job_name:
            for entry in entries:
                match = _CREATED_JOB_PATTERN.search(str(entry.get("stdout", "")) + str(entry.get("output", "")))
                if match:
                    job_name = match.group("name")
        if not job_name:
            if any(entry.get("event") in {"submit", "submit-timeout", "submitted"} for entry in entries):
                failures.append(f"{showname}: 提交状态不明，未自动删除；请查看 {log}")
            continue
        worker = object.__new__(TaskBuilderRJobEnvironment)
        worker._job_name = showname or job_name
        worker._rjob_name = job_name
        worker._replica_name = None
        worker._submission_uncertain = False
        worker._last_query_timed_out = False
        worker._last_job_text = ""
        worker._cleanup_namespace = namespace
        worker._cleanup_log_dir = str(log_dir)
        try:
            await worker.stop(delete=True)
        except TaskTimeout:
            raise
        except Exception as error:
            failures.append(f"{job_name}: {type(error).__name__}: {error}")
    return failures
