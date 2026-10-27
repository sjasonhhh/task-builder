from __future__ import annotations

import hmac
import http.client
import json
import os
import secrets
import socket
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from .models import EnvConfig


class ClaudeProxyError(RuntimeError):
    pass


def detect_proxy_host() -> str:
    route_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        route_probe.connect(("8.8.8.8", 53))
        address = route_probe.getsockname()[0]
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    finally:
        route_probe.close()

    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        addresses = []
    for address in addresses:
        candidate = address[4][0]
        if candidate and not candidate.startswith("127."):
            return candidate
    raise ClaudeProxyError("无法自动确定 RJob 可访问的本机地址，请通过 --proxy-host 指定")


def _dotenv_line(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'{key}="{escaped}"\n'


@dataclass(frozen=True)
class WorkerProxyCredentials:
    base_url: str
    token: str
    model_id: str


def worker_environment_values(
    env_config: EnvConfig,
    credentials: WorkerProxyCredentials,
) -> dict[str, str]:
    values = {
        "ANTHROPIC_MODEL": credentials.model_id,
        "ANTHROPIC_BASE_URL": credentials.base_url,
        "ANTHROPIC_AUTH_TOKEN": credentials.token,
        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
    }
    reasoning_effort = env_config.values.get("REASONING_EFFORT")
    if reasoning_effort:
        values["REASONING_EFFORT"] = reasoning_effort
    return values


def write_worker_env_file(
    env_config: EnvConfig,
    credentials: WorkerProxyCredentials,
    path: Path,
) -> dict[str, str]:
    values = worker_environment_values(env_config, credentials)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(_dotenv_line(key, value))
    return values


def claude_code_settings(model_id: str) -> dict[str, Any]:
    if model_id.startswith("claude-") or model_id in {"sonnet", "opus", "haiku"}:
        return {}
    return {"modelOverrides": {"claude-opus-4-8": model_id}}


def _resolve_upstream_proxy(scheme: str, host: str) -> tuple[str, str, int] | None:
    if proxy_bypass(host):
        return None
    proxies = getproxies()
    proxy_value = proxies.get(scheme) or proxies.get("all")
    if not proxy_value:
        return None
    if "://" not in proxy_value:
        proxy_value = "http://" + proxy_value
    parsed = urlsplit(proxy_value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ClaudeProxyError("本机上游代理必须是有效的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClaudeProxyError("本机上游代理地址不能包含账号、密码、查询参数或片段")
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname, parsed.port or default_port


class ClaudeAPIProxy:
    def __init__(
        self,
        env_config: EnvConfig,
        *,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        advertised_host: str | None = None,
        timeout_sec: int = 3600,
    ):
        upstream = urlsplit(env_config.values["ANTHROPIC_BASE_URL"])
        if upstream.scheme not in {"http", "https"} or not upstream.hostname:
            raise ClaudeProxyError("ANTHROPIC_BASE_URL 必须是有效的 HTTP 或 HTTPS 地址")
        if upstream.username or upstream.password or upstream.query or upstream.fragment:
            raise ClaudeProxyError("ANTHROPIC_BASE_URL 不应包含账号、密码、查询参数或片段")
        if bind_port < 0 or bind_port > 65535:
            raise ClaudeProxyError("代理端口必须在 0 到 65535 之间")

        auth_token = env_config.values.get("ANTHROPIC_AUTH_TOKEN")
        api_key = env_config.values.get("ANTHROPIC_API_KEY")
        if auth_token:
            self._upstream_auth = ("Authorization", f"Bearer {auth_token}")
        elif api_key:
            self._upstream_auth = ("x-api-key", api_key)
        else:
            raise ClaudeProxyError("模型配置缺少上游认证令牌")

        self._upstream_scheme = upstream.scheme
        self._upstream_host = upstream.hostname
        self._upstream_port = upstream.port
        self._upstream_path = upstream.path.rstrip("/")
        self._upstream_proxy = _resolve_upstream_proxy(upstream.scheme, upstream.hostname)
        upstream_port = upstream.port or (443 if upstream.scheme == "https" else 80)
        self._upstream_authority = upstream.hostname
        if (upstream.scheme == "http" and upstream_port != 80) or (upstream.scheme == "https" and upstream_port != 443):
            self._upstream_authority = f"{self._upstream_authority}:{upstream_port}"
        self._worker_token = secrets.token_urlsafe(32)
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._advertised_host = advertised_host or detect_proxy_host()
        if any(character.isspace() for character in self._advertised_host) or "/" in self._advertised_host or "://" in self._advertised_host:
            raise ClaudeProxyError("--proxy-host 只能填写主机名或 IP 地址")
        self._timeout_sec = max(1, timeout_sec)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stats_lock = threading.Lock()
        self._worker_requests = 0
        self._upstream_statuses: list[int] = []
        self._upstream_errors: list[str] = []
        self._client_addresses: set[str] = set()

    @property
    def worker_token(self) -> str:
        return self._worker_token

    @property
    def advertised_host(self) -> str:
        return self._advertised_host

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise ClaudeProxyError("代理尚未启动")
        host = self._advertised_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self._server.server_port}"

    @property
    def upstream_statuses(self) -> tuple[int, ...]:
        with self._stats_lock:
            return tuple(self._upstream_statuses)

    @property
    def client_addresses(self) -> tuple[str, ...]:
        with self._stats_lock:
            return tuple(sorted(self._client_addresses))

    @property
    def successful_upstream_requests(self) -> int:
        return sum(200 <= status < 300 for status in self.upstream_statuses)

    @property
    def worker_requests(self) -> int:
        with self._stats_lock:
            return self._worker_requests

    @property
    def upstream_errors(self) -> tuple[str, ...]:
        with self._stats_lock:
            return tuple(self._upstream_errors)

    @property
    def upstream_route(self) -> str:
        if self._upstream_proxy is None:
            return "直接连接上游"
        scheme, host, port = self._upstream_proxy
        return f"经本机 {scheme}://{host}:{port} 连接上游"

    def diagnostic(self) -> str:
        statuses = self.upstream_statuses
        errors = self.upstream_errors
        addresses = self.client_addresses
        rendered_statuses = ", ".join(str(status) for status in statuses) or "无"
        rendered_errors = "; ".join(errors) or "无"
        rendered_addresses = ", ".join(addresses) or "无"
        return (
            "模型反代诊断:\n"
            f"Worker 请求数: {self.worker_requests}\n"
            f"上游连接方式: {self.upstream_route}\n"
            f"上游 HTTP 状态码: {rendered_statuses}\n"
            f"上游连接错误: {rendered_errors}\n"
            f"发起请求的 Worker 地址: {rendered_addresses}\n"
            f"成功响应数: {self.successful_upstream_requests}"
        )

    def start(self) -> None:
        if self._server is not None:
            return
        owner = self

        class ProxyServer(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        class ProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            server_version = "TaskBuilderClaudeProxy"
            sys_version = ""

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                self._forward()

            def do_POST(self) -> None:
                self._forward()

            def do_PUT(self) -> None:
                self._forward()

            def do_PATCH(self) -> None:
                self._forward()

            def do_DELETE(self) -> None:
                self._forward()

            def do_HEAD(self) -> None:
                self._forward()

            def _error(self, status: int, message: str) -> None:
                body = json.dumps({"error": message}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _forward(self) -> None:
                supplied_token = self.headers.get("x-api-key", "")
                authorization = self.headers.get("Authorization", "")
                if authorization.lower().startswith("bearer "):
                    supplied_token = authorization[7:].strip()
                if not hmac.compare_digest(supplied_token, owner._worker_token):
                    self._error(401, "代理令牌无效")
                    return

                with owner._stats_lock:
                    owner._worker_requests += 1
                    owner._client_addresses.add(self.client_address[0])

                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                    if content_length < 0 or content_length > 128 * 1024 * 1024:
                        self._error(413, "请求内容过大")
                        return
                    body = self.rfile.read(content_length) if content_length else None
                    incoming = urlsplit(self.path)
                    if incoming.scheme or incoming.netloc:
                        self._error(400, "只允许转发相对路径")
                        return
                    target = owner._upstream_path + (incoming.path or "/")
                    if incoming.query:
                        target += "?" + incoming.query
                    upstream_port = owner._upstream_port or (443 if owner._upstream_scheme == "https" else 80)
                    if owner._upstream_proxy is None:
                        connection_type = http.client.HTTPSConnection if owner._upstream_scheme == "https" else http.client.HTTPConnection
                        connection_options: dict[str, Any] = {
                            "host": owner._upstream_host,
                            "port": upstream_port,
                            "timeout": owner._timeout_sec,
                        }
                        if owner._upstream_scheme == "https":
                            connection_options["context"] = ssl.create_default_context()
                        request_target = target
                    else:
                        proxy_scheme, proxy_host, proxy_port = owner._upstream_proxy
                        connection_type = (
                            http.client.HTTPSConnection
                            if proxy_scheme == "https" or owner._upstream_scheme == "https"
                            else http.client.HTTPConnection
                        )
                        connection_options = {
                            "host": proxy_host,
                            "port": proxy_port,
                            "timeout": owner._timeout_sec,
                        }
                        if proxy_scheme == "https":
                            connection_options["context"] = ssl.create_default_context()
                        if owner._upstream_scheme == "https":
                            request_target = target
                        else:
                            request_target = f"http://{owner._upstream_authority}{target}"
                    connection = connection_type(**connection_options)
                    if owner._upstream_proxy is not None and owner._upstream_scheme == "https":
                        connection.set_tunnel(owner._upstream_host, upstream_port)
                    headers = {
                        key: value
                        for key, value in self.headers.items()
                        if key.lower() not in {
                            "host",
                            "connection",
                            "proxy-connection",
                            "proxy-authorization",
                            "authorization",
                            "x-api-key",
                            "content-length",
                            "transfer-encoding",
                        }
                    }
                    headers[owner._upstream_auth[0]] = owner._upstream_auth[1]
                    headers["Host"] = owner._upstream_authority
                    if body is not None:
                        headers["Content-Length"] = str(len(body))
                    connection.request(self.command, request_target, body=body, headers=headers)
                    response = connection.getresponse()
                    with owner._stats_lock:
                        owner._upstream_statuses.append(response.status)
                        owner._client_addresses.add(self.client_address[0])
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    with owner._stats_lock:
                        owner._upstream_errors.append(error)
                    try:
                        connection.close()
                    except (UnboundLocalError, OSError):
                        pass
                    self._error(502, f"上游连接失败: {type(exc).__name__}")
                    return

                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "transfer-encoding", "trailer", "server", "date"}:
                        self.send_header(key, value)
                self.end_headers()
                self.close_connection = True
                try:
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        if self.command != "HEAD":
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    response.close()
                    connection.close()

        try:
            self._server = ProxyServer((self._bind_host, self._bind_port), ProxyHandler)
        except OSError as exc:
            raise ClaudeProxyError(f"无法监听反代地址 {self._bind_host}:{self._bind_port}: {exc}") from exc
        self._thread = threading.Thread(target=self._server.serve_forever, name="task-builder-claude-proxy", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> ClaudeAPIProxy:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()
