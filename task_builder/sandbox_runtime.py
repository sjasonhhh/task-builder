"""Harbor environment using the heterogeneous platform's TongSuan Sandbox API.

AD credentials stay in the controller process; no credentials are serialized in
runtime events or sent to the task container. No RJob CLI is used here.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import read_env_file
from .hcluster_runtime import (
    BaseEnvironment, EnvironmentCapabilities, ExecResult, HealthcheckError,
    THREAD_ENV_VARS, parse_dockerfile_workdir, resolve_claude_source,
)
from .integrity import content_manifest, file_digest
from .models import ConfigError
from .paths import PROJECT_CONFIG

LIFETIME_MINUTES = 24 * 60
RENEW_INTERVAL = 3600
STARTUP_TIMEOUT = 3600
RETRY_SECONDS = 900
CHUNK_BYTES = 16 * 1024 * 1024
CONFIG_KEYS = ("BASE_URL", "TASK_BUILDER_SANDBOX_RESOURCE", "AD_USERNAME", "AD_PASSWORD")


def author_lifetime_minutes(task_timeout_sec: int) -> int:
    # Cover the entire task at creation, plus preparation and result transfer.
    # Renewal is an additional safeguard, never the only way to finish a task.
    return max(LIFETIME_MINUTES, math.ceil(task_timeout_sec / 60) + 120)


def verified_expiry(detail: dict, minimum: datetime) -> str:
    value = detail.get("expiresAt")
    try:
        actual = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if actual.tzinfo is None or actual < minimum - timedelta(seconds=5):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise SandboxError("平台实际到期时间未覆盖申请时长；未确认续期成功") from None
    return actual.isoformat()


class SandboxError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def sandbox_settings() -> dict[str, str]:
    path = Path(os.environ.get("TASK_BUILDER_SANDBOX_CONFIG", str(PROJECT_CONFIG / "sandbox.env")))
    values = read_env_file(path) if path.is_file() else {}
    selected = {key: os.environ.get(key, values.get(key, "")) for key in CONFIG_KEYS}
    missing = [key for key, value in selected.items() if not value]
    if missing:
        raise ConfigError("config/sandbox.env 缺少: " + ", ".join(missing))
    url = urllib.parse.urlsplit(selected["BASE_URL"])
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ConfigError("sandbox BASE_URL 必须是有效的平台 HTTP(S) 地址")
    selected["BASE_URL"] = selected["BASE_URL"].rstrip("/")
    return selected


class SandboxClient:
    def __init__(self, settings: dict[str, str] | None = None, progress=None):
        settings = sandbox_settings() if settings is None else settings
        self.base_url = settings["BASE_URL"].rstrip("/")
        self.resource = settings["TASK_BUILDER_SANDBOX_RESOURCE"]
        self._username = settings["AD_USERNAME"]
        self._password = settings["AD_PASSWORD"]
        self._token = ""
        self._login_lock = threading.Lock()
        # The internal gateway must not be sent through model/Internet proxies.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.progress = progress or (lambda message: print("沙盒：" + message, flush=True))

    def login(self, previous_token: str | None = None) -> None:
        with self._login_lock:
            if self._token and (previous_token is None or previous_token != self._token):
                return
            body = json.dumps({"username": self._username, "password": self._password}).encode()
            raw = self.request("POST", "/api/v1/auth/ldap/login", data=body,
                               headers={"Content-Type": "application/json"}, authenticated=False)
            result = json.loads(raw)
            user = result.get("user") or {}
            if user.get("authSource") != "ldap" or user.get("status") != "active" or not result.get("token"):
                raise SandboxError("AD 账号未授权或平台未返回登录凭证")
            self._token = result["token"]

    def request(self, method: str, path: str, *, data: bytes | None = None,
                headers: dict | None = None, timeout: float = 90,
                retry: bool = True, authenticated: bool = True,
                deadline: float | None = None) -> bytes:
        if authenticated:
            self.login()
        url = path if path.startswith(("http://", "https://")) else self.base_url + path
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.base_url).netloc:
            raise SandboxError("平台返回了不同主机的访问地址，未向其发送凭证")
        end = min(deadline or float("inf"), time.monotonic() + timeout + RETRY_SECONDS)
        attempt = 0
        refreshed = False
        operation = method + " " + urllib.parse.urlsplit(url).path
        last_reason = ""
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise SandboxError("平台请求等待超时：" + operation + ("；" + last_reason if last_reason else ""))
            token = self._token
            request_headers = dict(headers or {})
            if authenticated:
                request_headers.update({"Authorization": "Bearer " + token,
                                        "OPEN-SANDBOX-API-KEY": token,
                                        "X-Sandbox-Resource": self.resource})
            request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
            retry_after = None
            try:
                with self._opener.open(request, timeout=min(timeout, remaining)) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After")
                detail = ""
                try:
                    body = json.loads(exc.read())
                    if isinstance(body, dict):
                        detail = body.get("message") or body.get("error") or body.get("detail") or ""
                        if isinstance(detail, dict):
                            detail = detail.get("message") or detail.get("code") or ""
                        detail = detail if isinstance(detail, str) else ""
                        for secret in (self._password, self._username, token, *(headers or {}).values()):
                            if secret:
                                detail = detail.replace(str(secret), "<redacted>")
                        detail = detail[:500]
                except (OSError, ValueError):
                    pass
                exc.close()
                last_reason = f"HTTP {status}" + (f"：{detail}" if detail else "")
                # A successful create can be followed by an endpoint rejecting
                # the cached login with 403. Confirm once with a fresh login;
                # a repeated refusal still fails as an infrastructure error.
                if status in {401, 403} and authenticated and not refreshed:
                    self.login(previous_token=token)
                    refreshed = True
                    continue
                if status not in {408, 429, 500, 502, 503, 504} or not retry:
                    hint = {401: "登录凭证或账号密码无效", 403: "账号或资源池权限不足"}.get(status, "平台请求失败")
                    raise SandboxError(f"{hint}（HTTP {status}）" + (f"：{detail}" if detail else ""), status) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_reason = type(exc).__name__
                if not retry:
                    raise SandboxError("平台请求连接中断或超时，结果尚未确认") from None
            delay = min(15 * 2 ** min(attempt, 3), 120)
            if retry_after and retry_after.isdigit():
                delay = max(delay, int(retry_after))
            delay = min(delay, max(0, end - time.monotonic()))
            self.progress(f"{operation}：{last_reason}；{delay:g} 秒后重试（第 {attempt + 1} 次）")
            time.sleep(delay)
            attempt += 1

    def json(self, method: str, path: str, body=None, **kwargs):
        data = None if body is None else json.dumps(body).encode()
        raw = self.request(method, path, data=data, headers={"Content-Type": "application/json"}, **kwargs)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise SandboxError("平台返回的内容不是有效 JSON") from None

    def api_path(self, path: str, **params) -> str:
        return path + "?" + urllib.parse.urlencode({"resource": self.resource, **params})

    def detail(self, sandbox_id: str, **kwargs) -> dict:
        return self.json("GET", self.api_path("/api/v1/sandboxes/" + sandbox_id), **kwargs)

    def runtime_detail(self, sandbox_id: str, **kwargs) -> dict:
        return self.json("GET", self.api_path("/v1/sandboxes/" + sandbox_id), **kwargs)

    def renew(self, sandbox_id: str, expires: datetime, *, deadline=None) -> str:
        self.json("POST", self.api_path(f"/api/v1/sandboxes/{sandbox_id}/renew"),
                  {"expiresAt": expires.isoformat()}, deadline=deadline)
        return verified_expiry(self.detail(sandbox_id, deadline=deadline), expires)

    def _instance_name(self, item: dict, deadline=None) -> str:
        name = (item.get("metadata") or {}).get("name") or item.get("name")
        if (not name or not name.startswith("tb-")) and item.get("id"):
            try:
                detail = self.runtime_detail(item["id"], deadline=deadline)
                name = (detail.get("metadata") or {}).get("name")
            except SandboxError as exc:
                if exc.status != 404:
                    raise
        return name or ""

    def find_created(self, name: str, deadline: float) -> list[dict]:
        found = []
        page = 1
        while True:
            result = self.json("GET", self.api_path("/api/v1/personal-sandboxes", page=page, pageSize=100), deadline=deadline)
            for item in result.get("items", []):
                if self._instance_name(item, deadline) == name or (item.get("metadata") or {}).get("task_builder_run") == name:
                    found.append(item)
            pagination = result.get("pagination") or {}
            if page * 100 >= pagination.get("totalItems", len(result.get("items", []))):
                return found
            page += 1

    def children(self, parent: str, *, deadline: float | None = None) -> list[dict]:
        """Only return sandboxes explicitly owned by this authoring sandbox."""
        found, page = [], 1
        while True:
            result = self.json("GET", self.api_path("/api/v1/personal-sandboxes", page=page, pageSize=100), deadline=deadline)
            items = result.get("items", [])
            # The platform's create adapter currently retains only metadata.name.
            # Put the exact parent ID in that name as well as custom metadata.
            for item in items:
                if ((item.get("metadata") or {}).get("task_builder_parent") == parent
                        or self._instance_name(item, deadline).startswith("tb-child-" + parent + "-")):
                    found.append(item)
            if page * 100 >= (result.get("pagination") or {}).get("totalItems", len(items)):
                return found
            page += 1

    def endpoint(self, sandbox_id: str, **kwargs) -> tuple[str, dict]:
        value = self.json("GET", self.api_path(f"/v1/sandboxes/{sandbox_id}/endpoints/44772", use_server_proxy="true"), **kwargs)
        url = str(value["endpoint"]).rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        return url, dict(value.get("headers") or {})

    def command(self, sandbox_id: str, command: str, *, deadline: float | None = None, retry: bool = False) -> ExecResult:
        url, headers = self.endpoint(sandbox_id, deadline=deadline)
        headers["Content-Type"] = "application/json"
        raw = self.request("POST", url + "/command", data=json.dumps({"command": command, "user": "root"}).encode(),
                           headers=headers, retry=retry, timeout=180, deadline=deadline)
        return parse_command_events(raw)

    def upload(self, sandbox_id: str, content: bytes, remote: str, *, deadline=None) -> None:
        url, headers = self.endpoint(sandbox_id, deadline=deadline)
        boundary = "tb-" + uuid.uuid4().hex
        body = b"\r\n".join([
            ("--" + boundary).encode(), b'Content-Disposition: form-data; name="metadata"; filename="metadata.json"',
            b"Content-Type: application/json", b"", json.dumps({"path": remote}).encode(),
            ("--" + boundary).encode(), b'Content-Disposition: form-data; name="file"; filename="part"',
            b"Content-Type: application/octet-stream", b"", content, ("--" + boundary + "--").encode(), b"",
        ])
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        self.request("POST", url + "/files/upload", data=body, headers=headers, timeout=600, deadline=deadline)

    def download(self, sandbox_id: str, remote: str, *, deadline=None) -> bytes:
        url, headers = self.endpoint(sandbox_id, deadline=deadline)
        return self.request("GET", url + "/files/download?" + urllib.parse.urlencode({"path": remote}),
                            headers=headers, timeout=600, deadline=deadline)

    def _confirmed_absent(self, sandbox_id: str, error: SandboxError, *, deadline: float) -> bool:
        if error.status == 404:
            return True
        if error.status != 403:
            return False
        # Deleted IDs can return 403 instead of 404. Check the same account and
        # resource's complete list; a permission error alone proves nothing.
        seen, page = set(), 1
        while True:
            result = self.json("GET", self.api_path("/api/v1/personal-sandboxes", page=page, pageSize=100),
                               deadline=deadline)
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise SandboxError("沙盒列表格式异常，无法确认删除")
            items = result["items"]
            pagination = result.get("pagination", {})
            if not isinstance(pagination, dict):
                raise SandboxError("沙盒列表分页信息异常，无法确认删除")
            total = pagination.get("totalItems")
            if total is not None and (type(total) is not int or total < 0):
                raise SandboxError("沙盒列表总数异常，无法确认删除")
            if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                   for item in items):
                raise SandboxError("沙盒列表缺少实例 ID，无法确认删除")
            ids = {item["id"] for item in items}
            if sandbox_id in ids:
                return False
            if len(ids) != len(items) or seen.intersection(ids):
                raise SandboxError("沙盒列表分页重复，无法确认删除")
            seen.update(ids)
            if total is not None:
                if len(seen) > total or (not items and len(seen) < total):
                    raise SandboxError("沙盒列表不完整，无法确认删除")
                if len(seen) == total:
                    return True
            elif not items:
                return True
            page += 1

    def delete(self, sandbox_id: str) -> None:
        end = time.monotonic() + 900
        path = self.api_path("/api/v1/sandboxes/" + sandbox_id)
        # This platform can return HTTP 500 when DELETE targets an expired ID.
        # A confirmed absence is already a successful cleanup.
        try:
            self.detail(sandbox_id, deadline=end)
        except SandboxError as exc:
            if self._confirmed_absent(sandbox_id, exc, deadline=end):
                return
            raise
        try:
            self.json("DELETE", path, deadline=end, retry=False)
        except SandboxError as exc:
            if exc.status != 404:
                try:
                    self.detail(sandbox_id, deadline=end)
                except SandboxError as check_error:
                    if self._confirmed_absent(sandbox_id, check_error, deadline=end):
                        return
                raise exc
        while time.monotonic() < end:
            try:
                item = self.detail(sandbox_id, deadline=end)
            except SandboxError as exc:
                if self._confirmed_absent(sandbox_id, exc, deadline=end):
                    return
                raise
            state = (item.get("status") or {}).get("state") or item.get("state")
            if state in {"Deleted", "Terminated"}:
                return
            time.sleep(10)
        raise SandboxError("等待沙盒删除确认超时")


def parse_command_events(raw: bytes) -> ExecResult:
    """Decode execd SSE/NDJSON. Missing completion must never mean success."""
    stdout, stderr = [], []
    code = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            line = line[5:].strip()
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in {"stdout", "stderr"}:
            # execd emits one line per event and removes the line terminator.
            # Concatenation would join a checksum and the following filename.
            text = str(event.get("text", ""))
            (stdout if kind == "stdout" else stderr).append(text if text.endswith("\n") else text + "\n")
        elif kind == "execution_complete":
            code = event.get("exit_code", 0) if code is None else code
        elif kind == "error":
            error = event.get("error") or {}
            if error.get("ename") == "CommandExecError" and re.fullmatch(r"\d+", str(error.get("evalue", ""))):
                code = int(error["evalue"])
            else:
                raise SandboxError("沙盒命令服务返回执行错误")
    if code is None:
        raise SandboxError("命令服务未返回明确的完成状态")
    return ExecResult(stdout="".join(stdout), stderr="".join(stderr), return_code=int(code))


class TaskBuilderSandboxEnvironment(BaseEnvironment):
    def __init__(self, *args, **kwargs):
        self._sandbox_id: str | None = None
        self._name = "tb-" + uuid.uuid4().hex
        parent = os.environ.get("TASK_BUILDER_SANDBOX_PARENT")
        if parent:
            if not re.fullmatch(r"[a-z0-9-]{1,40}", parent):
                raise ValueError("主沙盒编号格式无效")
            self._name = "tb-child-" + parent + "-" + uuid.uuid4().hex[:10]
        self._client = SandboxClient(progress=self._progress)
        self._renew_stop = threading.Event()
        self._renew_error: Exception | None = None
        self._renew_thread: threading.Thread | None = None
        self._start_cancelled = threading.Event()
        self._log_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "task-builder-sandbox"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(disable_internet=True, network_allowlist=True,
            network_allowlist_hostnames=True, network_allowlist_wildcard_hostnames=True,
            network_allowlist_ipv4_addresses=True, network_allowlist_ipv6_addresses=True,
            network_allowlist_ipv4_cidrs=True, network_allowlist_ipv6_cidrs=True,
            dynamic_network_policy=True, mounted=False)

    @classmethod
    def resource_capabilities(cls):
        return None

    def _validate_definition(self):
        if not self.environment_dir.is_dir() or not self.task_env_config.docker_image:
            raise ValueError("沙盒运行需要已有环境目录和 docker_image")

    @property
    def _is_verifier(self):
        return (self.environment_dir.name.lower() in {"tests", "verifier"}
                or (self.environment_dir / "test.sh").is_file()
                or "verifier" in str(self.session_id).lower())

    @property
    def _cwd(self):
        if self.task_env_config.workdir:
            return self.task_env_config.workdir
        if getattr(self, "_package_cwd", None):
            return self._package_cwd
        return parse_dockerfile_workdir(self.environment_dir / "Dockerfile") or "/workspace"

    def _progress(self, message):
        print(f"沙盒 {self._name}：{message}", flush=True)

    def _event(self, event: str, **details):
        path = Path(os.environ.get("TASK_BUILDER_RUNTIME_LOG_DIR", "harbor-runtime-logs"))
        path.mkdir(parents=True, exist_ok=True)
        record = {"time": time.time(), "backend": "sandbox", "event": event,
                  "name": self._name, "sandbox_id": self._sandbox_id,
                  "base_url": self._client.base_url, "resource": self._client.resource, **details}
        with self._log_lock, (path / (self._name + ".jsonl")).open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _network_payload(self, policy=None):
        policy = policy or self.network_policy
        mode = policy.network_mode.value
        targets = list(policy.allowed_hosts) if mode == "allowlist" else []
        # The existing model proxy is the control channel, not general Internet access.
        if not self._is_verifier and os.environ.get("TASK_BUILDER_PROXY_BASE_URL"):
            host = urllib.parse.urlsplit(os.environ["TASK_BUILDER_PROXY_BASE_URL"]).hostname
            if host:
                targets.append(host)
        return {"defaultAction": "allow" if mode == "public" else "deny",
                "egress": [{"action": "allow", "target": host} for host in sorted(set(targets))]}

    async def _apply_network_policy(self, network_policy):
        if self._sandbox_id:
            await asyncio.to_thread(self._client.json, "POST",
                f"/v1/sandboxes/{self._sandbox_id}/network-policy", self._network_payload(network_policy))

    def _create(self):
        end = time.monotonic() + STARTUP_TIMEOUT
        cpu = min(self.task_env_config.cpus or 4, int(os.environ.get("TASK_BUILDER_RJOB_MAX_CPU", "4")))
        memory = min(self.task_env_config.memory_mb or 8192, int(os.environ.get("TASK_BUILDER_RJOB_MAX_MEMORY_MB", "8192")))
        image = self.task_env_config.docker_image
        if self._is_verifier:
            image = os.environ.get("TASK_BUILDER_EVALUATION_IMAGE", image)
        payload = {"name": self._name, "resource": self._client.resource,
            "metadata": {"task_builder_run": self._name}, "image": {"uri": image},
            "entrypoint": ["sleep", "inf"], "resources": {"cpu": str(cpu), "memory": f"{memory}Mi"},
            "ports": [{"containerPort": 44772, "name": "execd", "protocol": "tcp", "purpose": "system service"}],
            "lifecycleMinutes": LIFETIME_MINUTES, "networkPolicy": self._network_payload()}
        for suffix in ("BATCH", "SOURCE", "PARENT"):
            if os.environ.get("TASK_BUILDER_SANDBOX_" + suffix):
                payload["metadata"]["task_builder_" + suffix.lower()] = os.environ["TASK_BUILDER_SANDBOX_" + suffix]
        self._event("create-requested", image=image, cpu=cpu, memory_mb=memory, lifecycle_minutes=LIFETIME_MINUTES)
        try:
            result = self._client.json("POST", "/api/v1/sandboxes", payload, timeout=1800, retry=False, deadline=end)
        except SandboxError as exc:
            if exc.status is not None and 400 <= exc.status < 500 and exc.status not in {408, 429}:
                self._event("create-rejected", status=exc.status)
                raise
            self._event("create-uncertain")
            while time.monotonic() < end:
                found = self._client.find_created(self._name, end)
                if len(found) == 1:
                    result = found[0]
                    break
                if len(found) > 1:
                    raise SandboxError("本次创建匹配到多个实例，未重复提交；需检查运行日志")
                self._progress("创建结果尚未确认，继续查询本次实例")
                time.sleep(15)
            else:
                raise SandboxError("创建结果未确认，未重复提交；可按运行日志中的名称检查实例")
        self._sandbox_id = result.get("id") or (result.get("sandbox") or {}).get("id")
        if not self._sandbox_id:
            raise SandboxError("创建响应缺少实例编号")
        self._event("created")
        if self._start_cancelled.is_set():
            self._client.delete(self._sandbox_id)
            self._event("shutdown-complete")
            self._sandbox_id = None
            raise SandboxError("沙盒启动已中断")
        self._renew_thread = threading.Thread(target=self._renew_loop, daemon=True)
        self._renew_thread.start()
        started = time.monotonic()
        last_notice = 0.0
        while time.monotonic() < end:
            if self._start_cancelled.is_set():
                raise SandboxError("沙盒启动已中断")
            self._check_renewal()
            detail = self._client.detail(self._sandbox_id, deadline=end)
            state = (detail.get("status") or {}).get("state") or detail.get("state")
            if time.monotonic() - last_notice >= 60:
                self._progress(f"状态 {state}，已等待 {int(time.monotonic()-started)} 秒")
                last_notice = time.monotonic()
            if state in {"Failed", "Error", "Terminated", "Deleted"}:
                raise SandboxError(f"沙盒进入终止状态 {state}")
            if state == "Running":
                try:
                    result = self._client.command(self._sandbox_id, "true", deadline=end)
                    if result.return_code == 0:
                        self._event("running")
                        return
                except SandboxError as exc:
                    if exc.status in {401, 403}:
                        raise
            time.sleep(15)
        raise SandboxError("沙盒启动超过 60 分钟")

    def _renew_loop(self):
        while not self._renew_stop.wait(RENEW_INTERVAL):
            end = time.monotonic() + 1800
            while not self._renew_stop.is_set():
                try:
                    expires = datetime.now(timezone.utc) + timedelta(minutes=LIFETIME_MINUTES)
                    confirmed = self._client.renew(self._sandbox_id, expires, deadline=end)
                    self._event("renewed", expires_at=confirmed)
                    break
                except Exception as exc:
                    if time.monotonic() >= end or getattr(exc, "status", None) in {400, 401, 403, 404}:
                        self._renew_error = SandboxError("沙盒续期失败")
                        self._event("renew-failed")
                        return
                    self._progress("续期暂未成功，稍后重试")
                    self._renew_stop.wait(60)

    def _check_renewal(self):
        if self._renew_error:
            raise self._renew_error

    def _checked(self, command: str, **kwargs) -> ExecResult:
        self._check_renewal()
        result = self._client.command(self._sandbox_id, command, **kwargs)
        if result.return_code != 0:
            raise SandboxError(f"沙盒文件或环境准备命令失败（退出码 {result.return_code}）")
        return result

    async def start(self, force_build: bool):
        del force_build
        creator = asyncio.create_task(asyncio.to_thread(self._create))
        try:
            await asyncio.shield(creator)
            await asyncio.to_thread(self._checked, "mkdir -p /workspace /tests /logs/agent /logs/verifier /logs/artifacts /state /app/results")
            from .task_package import install_context, is_standard_package
            if is_standard_package(self.environment_dir.parent):
                await install_context(self, verifier=self._is_verifier)
                if not self._is_verifier:
                    from .agent_runtime import runtime_files
                    agent = os.environ.get("TASK_BUILDER_AGENT", "claude-code")
                    for source, remote in runtime_files(agent):
                        await asyncio.to_thread(self._checked, f"mkdir -p {shlex.quote(str(Path(remote).parent))}")
                        await self.upload_file(source, remote)
                        await asyncio.to_thread(self._checked, f"chmod 755 {shlex.quote(remote)}")
                return
            if self._is_verifier:
                tests = self.environment_dir if (self.environment_dir / "test.sh").is_file() else self.environment_dir.parent / "tests"
                if tests.is_dir():
                    await self.upload_dir(tests, "/tests")
                data = self.environment_dir.parent / "data"
                remote_data = "/workspace/data"
                if data.is_dir():
                    await self.upload_dir(data, remote_data)
            else:
                await self.upload_dir(self.environment_dir, self._cwd)
                data = self.environment_dir / "data"
                remote_data = posixpath.join(self._cwd, "data")
                from .agent_runtime import runtime_files
                agent = os.environ.get("TASK_BUILDER_AGENT", "claude-code")
                for source, remote in runtime_files(agent):
                    await asyncio.to_thread(self._checked, f"mkdir -p {shlex.quote(str(Path(remote).parent))}")
                    await self.upload_file(source, remote)
                    await asyncio.to_thread(self._checked, f"chmod 755 {shlex.quote(remote)}")
                if agent not in {"nop", "oracle"}:
                    await asyncio.to_thread(self._checked, f"{'codex' if agent == 'codex' else 'claude'} --version")
            if data.is_dir():
                await asyncio.to_thread(self._checked, "mkdir -p /app && rm -rf /app/inputs && ln -s " + shlex.quote(remote_data) + " /app/inputs")
        except BaseException:
            self._start_cancelled.set()
            self._event("infrastructure-failed", operation="start")
            await self.stop(delete=True)
            raise

    async def stop(self, delete: bool):
        del delete
        self._renew_stop.set()
        self._start_cancelled.set()
        if self._sandbox_id:
            await asyncio.to_thread(self._client.delete, self._sandbox_id)
            self._event("shutdown-complete")
            self._sandbox_id = None

    def _exec(self, command, cwd, env, timeout_sec, user, cancelled):
        self._check_renewal()
        remote = "/tmp/tb-exec-" + uuid.uuid4().hex
        end = time.monotonic() + timeout_sec
        self._checked("mkdir -m 700 " + remote, deadline=end)
        variables = self._merge_env(env) or {}
        cpu = min(self.task_env_config.cpus or 4, int(os.environ.get("TASK_BUILDER_RJOB_MAX_CPU", "4")))
        variables = {**{key: str(cpu) for key in THREAD_ENV_VARS}, **variables}
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in variables):
            raise SandboxError("命令环境变量名称无效")
        script = "#!/bin/bash\n" + "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in variables.items())
        script += "\ncd " + shlex.quote(cwd or self._cwd) + " || exit $?\n" + command + "\n"
        self._client.upload(self._sandbox_id, script.encode(), remote + "/command.sh", deadline=end)
        runner = "bash " + remote + "/command.sh"
        if user not in {None, "root", 0, "0"}:
            runner = "runuser -u " + shlex.quote(str(user)) + " -- " + runner
        wrapper = "#!/bin/bash\n" + runner + f" >{remote}/stdout 2>{remote}/stderr\nrc=$?\nprintf '%s' \"$rc\" >{remote}/exit.tmp\nmv {remote}/exit.tmp {remote}/exit\n"
        self._client.upload(self._sandbox_id, wrapper.encode(), remote + "/wrapper.sh", deadline=end)
        # mkdir is the launch claim: a response timeout cannot start a second copy.
        launch = f"if mkdir {remote}/claimed 2>/dev/null; then setsid bash {remote}/wrapper.sh </dev/null >/dev/null 2>&1 & echo $! >{remote}/pid; fi"
        try:
            try:
                self._checked(launch, deadline=end)
            except SandboxError:
                self._checked(launch, deadline=end)
            while time.monotonic() < end and not cancelled.is_set():
                self._check_renewal()
                # 只读状态查询可重试，短暂平台故障不应重启整次答题。
                probe = self._checked(f"if test -f {remote}/exit; then cat {remote}/exit; else echo pending; fi", deadline=end, retry=True)
                value = (probe.stdout or "").strip()
                if re.fullmatch(r"\d+", value):
                    stdout = self._client.download(self._sandbox_id, remote + "/stdout", deadline=end).decode(errors="replace")
                    stderr = self._client.download(self._sandbox_id, remote + "/stderr", deadline=end).decode(errors="replace")
                    return ExecResult(stdout=stdout, stderr=stderr, return_code=int(value))
                cancelled.wait(min(2, max(0, end - time.monotonic())))
            raise TimeoutError("沙盒命令执行超时或已中断")
        finally:
            # Cleanup has its own small grace period after the command deadline.
            try:
                self._client.command(self._sandbox_id,
                    f"if test ! -f {remote}/exit && test -f {remote}/pid; then kill -TERM -- -$(cat {remote}/pid) 2>/dev/null || true; sleep 1; kill -KILL -- -$(cat {remote}/pid) 2>/dev/null || true; fi; rm -rf {remote}",
                    deadline=time.monotonic() + 180)
            except SandboxError as exc:
                self._event("command-cleanup-failed", error=str(exc))

    async def exec(self, command: str, cwd=None, env=None, timeout_sec=None, user=None):
        cancelled = threading.Event()
        env = {**getattr(self, "_package_env", {}), **(env or {})} or None
        worker = asyncio.create_task(asyncio.to_thread(self._exec, command, cwd, env, float(timeout_sec or 86400), self._resolve_user(user), cancelled))
        try:
            return await asyncio.shield(worker)
        except SandboxError as exc:
            self._event("infrastructure-failed", operation="exec", error=str(exc))
            raise
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=200)
            except (Exception, asyncio.CancelledError):
                pass
            raise

    def _remote_manifest(self, path: str) -> dict:
        program = ("import hashlib,json,sys\nfrom pathlib import Path\n"
                   + inspect.getsource(file_digest) + "\n" + inspect.getsource(content_manifest)
                   + "\nprint(json.dumps(content_manifest(Path(sys.argv[1]))))")
        result = self._checked("python3 -c " + shlex.quote(program) + " " + shlex.quote(path))
        try:
            value = json.loads(result.stdout or "")
        except ValueError:
            raise SandboxError("远程文件校验结果无效") from None
        if not isinstance(value, dict):
            raise SandboxError("远程文件校验结果不是对象")
        return value

    def _upload_path(self, source: Path, target: str, directory: bool):
        stage = "/tmp/tb-upload-" + uuid.uuid4().hex
        try:
            self._checked("mkdir -m 700 " + stage)
            with tempfile.TemporaryFile() as stream:
                with tarfile.open(fileobj=stream, mode="w") as bundle:
                    bundle.add(source, arcname="payload", recursive=True)
                stream.seek(0)
                count = 0
                for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                    self._client.upload(self._sandbox_id, chunk, f"{stage}/part-{count:08d}")
                    count += 1
                    if count % 4 == 0:
                        self._progress(f"上传 {source.name}：已传输 {count * CHUNK_BYTES // (1024 * 1024)} MiB")
                parent = posixpath.dirname(target.rstrip("/")) or "/"
                assemble = f"cat {stage}/part-* >{stage}/bundle.tar && tar -xf {stage}/bundle.tar -C {stage} && mkdir -p {shlex.quote(parent)}"
                if directory:
                    assemble += " && mkdir -p " + shlex.quote(target) + f" && cp -a {stage}/payload/. " + shlex.quote(target + "/")
                else:
                    assemble += f" && cp -a {stage}/payload " + shlex.quote(target)
                self._checked(assemble)
            self._event("upload-complete", source=str(source), target=target)
        except Exception:
            self._event("upload-failed", source=str(source), target=target)
            raise
        finally:
            self._client.command(self._sandbox_id, "rm -rf " + stage)

    async def upload_file(self, source_path, target_path):
        await asyncio.to_thread(self._upload_path, Path(source_path), str(target_path), False)

    async def upload_dir(self, source_dir, target_dir):
        await asyncio.to_thread(self._upload_path, Path(source_dir), str(target_dir), True)

    def _download_path(self, source: str, target: Path, directory: bool):
        stage = "/tmp/tb-download-" + uuid.uuid4().hex
        try:
            remote = source.rstrip("/") or "/"
            parent = posixpath.dirname(remote) or "/"
            name = posixpath.basename(remote) or "."
            self._checked(f"mkdir -m 700 {stage} && tar -cf {stage}/bundle.tar -C {shlex.quote(parent)} -- {shlex.quote(name)} && split -b {CHUNK_BYTES} -d -a 8 {stage}/bundle.tar {stage}/part-")
            parts = self._checked(f"ls {stage}/part-*").stdout.splitlines()
            with tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "bundle.tar"
                with archive.open("wb") as handle:
                    for part in parts:
                        if not re.fullmatch(re.escape(stage) + r"/part-\d{8}", part):
                            raise SandboxError("下载分块列表无效")
                        chunk = self._client.download(self._sandbox_id, part)
                        handle.write(chunk)
                extract = Path(temporary) / "extract"
                extract.mkdir()
                with tarfile.open(archive) as bundle:
                    bundle.extractall(extract, filter="data")
                actual = extract if name == "." else extract / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if directory:
                    shutil.copytree(actual, target, dirs_exist_ok=True, symlinks=True)
                else:
                    shutil.copy2(actual, target)
            self._event("download-complete", source=source, target=str(target))
        except Exception:
            self._event("download-failed", source=source, target=str(target))
            raise
        finally:
            self._client.command(self._sandbox_id, "rm -rf " + stage)

    async def download_file(self, source_path, target_path):
        await asyncio.to_thread(self._download_path, str(source_path), Path(target_path), False)

    async def download_dir(self, source_dir, target_dir):
        await asyncio.to_thread(self._download_path, str(source_dir), Path(target_dir), True)

    async def healthcheck(self):
        result = await asyncio.to_thread(self._client.command, self._sandbox_id, "true")
        if result.return_code:
            raise HealthcheckError("沙盒健康检查失败")


async def cleanup_logged_sandboxes(log_dir: Path) -> list[str]:
    failures = []
    client = None
    for log in log_dir.glob("tb-*.jsonl"):
        entries = []
        for line in log.read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        if not entries or entries[0].get("backend") != "sandbox" or any(x.get("event") in {"shutdown-complete", "create-rejected"} for x in entries):
            continue
        record = entries[-1]
        try:
            client = client or SandboxClient()
            if record.get("base_url") != client.base_url or record.get("resource") != client.resource:
                raise SandboxError("历史实例所属平台或资源池与当前配置不同，未使用当前账号操作")
            sid = next((x.get("sandbox_id") for x in reversed(entries) if x.get("sandbox_id")), None)
            if not sid:
                found = await asyncio.to_thread(client.find_created, record["name"], time.monotonic() + 900)
                if not found:
                    failures.append(record["name"] + ": 创建结果仍不明确")
                    continue
                ids = [x["id"] for x in found]
            else:
                ids = [sid]
            for sid in ids:
                await asyncio.to_thread(client.delete, sid)
            with log.open("a") as handle:
                handle.write(json.dumps({**record, "event": "shutdown-complete", "time": time.time()}) + "\n")
        except Exception as exc:
            failures.append(record.get("name", log.name) + ": " + str(exc))
    return failures


def preflight_sandbox() -> None:
    client = SandboxClient()
    resources = client.json("GET", "/api/v1/sandbox-resources")
    if client.resource not in {item.get("id") for item in resources.get("items", [])}:
        raise ConfigError("配置的沙盒资源池不在当前账号可用列表中")
