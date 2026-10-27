from __future__ import annotations

import argparse
import contextlib
import fcntl
import hmac
import http.client
import json
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, getproxies, proxy_bypass


ROOT = Path(__file__).resolve().parent
HEALTH_PATH = "/__relay__/health"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
AUTH_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


class RelayError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RelayError(f"无法读取配置文件: {path}") from error
    if not isinstance(payload, dict):
        raise RelayError(f"配置必须是 JSON 对象: {path}")
    return payload


def atomic_json(path: Path, payload: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise RelayError(f"无法读取入口配置: {path}") from error
    values = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RelayError(f"入口配置第 {number} 行缺少等号: {path}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or any(character.isspace() for character in key):
            raise RelayError(f"入口配置第 {number} 行变量名无效: {path}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            try:
                value = shlex.split(value, comments=False)[0]
            except (ValueError, IndexError) as error:
                raise RelayError(f"入口配置第 {number} 行引号无效: {path}") from error
        values[key] = value
    return values


def read_entry_values(path: Path, file_format: str) -> dict[str, str]:
    if file_format == "env":
        return read_env(path)
    if file_format != "json":
        raise RelayError("env_format 必须为 env 或 json")
    values = read_json(path).get("env")
    if not isinstance(values, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()):
        raise RelayError(f"JSON 配置的 env 必须为字符串变量对象: {path}")
    return values


def checked_url(value: str):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise RelayError("API 地址无效") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RelayError("API 地址必须为 HTTP/HTTPS，且不包含账号、密码、查询参数或片段")
    if port == 0:
        raise RelayError("API 端口不能为 0")
    return parsed


def detect_host() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("8.8.8.8", 53))
            address = probe.getsockname()[0]
            if not address.startswith("127."):
                return address
        except OSError:
            pass
    for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        if not address[4][0].startswith("127."):
            return address[4][0]
    raise RelayError("无法识别原开发机地址，请填写 advertised_host")


def host_identity() -> dict:
    return {"hostname": socket.gethostname(), "workspace_process": os.environ.get("KUBEBRAIN_PROCESS_NAME", ""), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def process_start(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def managed_process(record: dict) -> bool:
    host = host_identity()
    saved_host = record.get("host", {})
    # 只按主机名判断是否同一台开发机；KUBEBRAIN_PROCESS_NAME 取决于终端是否为登录 shell，不能作为机器身份。
    if saved_host.get("hostname") != host["hostname"]:
        raise RelayError("请在启动服务的原开发机管理反代；当前是另一台工作机")
    if saved_host.get("boot_id") != host["boot_id"]:
        return False
    try:
        pid = int(record["pid"])
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return process_start(pid) == record["process_start"] and str(Path(__file__).resolve()).encode() in command and b"serve" in command and record["service_id"].encode() in command
    except (OSError, KeyError, ValueError):
        return False


def guard_master() -> None:
    process = os.environ.get("KUBEBRAIN_PROCESS_NAME", "")
    if os.environ.get("KUBEBRAIN_RESOURCE_TYPE") == "worker" or "-worker-" in process or (os.environ.get("KUBEBRAIN_RESOURCE_TYPE") == "workspace" and process and not process.endswith("-master")):
        raise RelayError("请在开发机运行 relay.py start/stop；worker 只负责连接")


@dataclass(frozen=True)
class Entry:
    name: str
    env_file: Path
    upstream: str
    auth_type: str
    auth_value: str
    model: str | None
    port: int = 0


def load_config(path: Path) -> tuple[dict, list[Entry]]:
    config = read_json(path)
    registrations = config.get("entries")
    if not isinstance(registrations, list) or not registrations:
        raise RelayError("config.json 必须登记至少一个 entries 入口")
    names, files = set(), set()
    entries = []
    for item in registrations:
        if not isinstance(item, dict):
            raise RelayError("入口登记必须是对象")
        required = ("name", "env_file", "base_url_var", "auth_var", "auth_type")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
            raise RelayError("入口缺少必需字段: " + ", ".join(required))
        name = item["name"]
        env_file = Path(item["env_file"]).expanduser()
        if not env_file.is_absolute():
            env_file = path.parent / env_file
        env_file = env_file.resolve()
        if name in names or env_file in files:
            raise RelayError(f"入口名称或配置文件重复: {name}")
        names.add(name)
        files.add(env_file)
        if item["auth_type"] not in {"bearer", "api-key"}:
            raise RelayError(f"入口 {name} 的 auth_type 必须为 bearer 或 api-key")
        port = item.get("listen_port", 0)
        if type(port) is not int or not 0 <= port <= 65535:
            raise RelayError(f"入口 {name} 的 listen_port 必须为 0 到 65535")
        file_format = item.get("env_format", "json" if env_file.suffix.lower() == ".json" else "env")
        values = read_entry_values(env_file, file_format)
        variables = [item["base_url_var"], item["auth_var"]]
        if item.get("model_var"):
            variables.append(item["model_var"])
        if any(not isinstance(key, str) or not values.get(key) for key in variables):
            raise RelayError(f"入口 {name} 的原配置缺少登记的变量")
        upstream = values[item["base_url_var"]]
        checked_url(upstream)
        auth_value = values[item["auth_var"]]
        if "\r" in auth_value or "\n" in auth_value:
            raise RelayError(f"入口 {name} 的认证变量包含非法换行")
        entries.append(Entry(name, env_file, upstream, item["auth_type"], auth_value, values.get(item.get("model_var", "")), port))
    timeout = config.get("timeout_sec", 3900)
    if type(timeout) is not int or timeout < 1:
        raise RelayError("timeout_sec 必须为正整数")
    if not isinstance(config.get("bind_host", "0.0.0.0"), str) or not config.get("bind_host", "0.0.0.0"):
        raise RelayError("bind_host 必须为有效的监听地址")
    host = config.get("advertised_host")
    if host is not None and (not isinstance(host, str) or not host or any(character.isspace() for character in host) or "/" in host or "://" in host):
        raise RelayError("advertised_host 只能填写 IP 或主机名")
    return config, entries


class APIService:
    def __init__(self, entry: Entry, *, bind_host: str, advertised_host: str, service_id: str, timeout: int):
        self.entry = entry
        self.token = secrets.token_urlsafe(32)
        self.service_id = service_id
        self.advertised_host = advertised_host
        self.timeout = timeout
        self.requests = 0
        self.failures = 0
        self.lock = threading.Lock()
        self.upstream = checked_url(entry.upstream)
        self.proxy = None
        if not proxy_bypass(self.upstream.hostname):
            value = getproxies().get(self.upstream.scheme) or getproxies().get("all")
            if value:
                self.proxy = checked_url(value if "://" in value else "http://" + value)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            server_version = "DataAPIRelay"
            sys_version = ""

            def log_message(self, format, *args):
                return

            def setup(self):
                super().setup()
                self.connection.settimeout(owner.timeout)

            def respond(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def forward(self) -> None:
                authorization = self.headers.get("Authorization", "")
                supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else self.headers.get("x-api-key", "")
                if not hmac.compare_digest(supplied, owner.token):
                    self.respond(401, {"error": "反代临时令牌无效；服务重启后请重新启动客户端"})
                    return
                incoming = urlsplit(self.path)
                if incoming.scheme or incoming.netloc:
                    self.respond(400, {"error": "只能访问已登记入口的相对路径"})
                    return
                if incoming.path == HEALTH_PATH:
                    if self.command != "GET":
                        self.respond(405, {"error": "健康检查仅支持 GET"})
                        return
                    with owner.lock:
                        stats = {"requests": owner.requests, "failures": owner.failures}
                    self.respond(200, {"service_id": owner.service_id, "entry": owner.entry.name, **stats})
                    return
                if self.headers.get("Transfer-Encoding"):
                    self.respond(400, {"error": "请求必须携带 Content-Length，不支持分块请求正文"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.respond(400, {"error": "Content-Length 无效"})
                    return
                if not 0 <= length <= 128 * 1024 * 1024:
                    self.respond(413, {"error": "请求内容过大"})
                    return
                started = time.monotonic()
                status = 502
                error_name = ""
                connection = response = None
                with owner.lock:
                    owner.requests += 1
                try:
                    body = self.rfile.read(length) if length else None
                except OSError as error:
                    owner.record(400, started, type(error).__name__)
                    return
                if body is not None and len(body) != length:
                    self.respond(400, {"error": "请求正文不完整"})
                    return
                excluded = HOP_HEADERS | {"host", "authorization", "x-api-key", "content-length"}
                excluded |= {name.strip().lower() for name in self.headers.get("Connection", "").split(",")}
                headers = {key: value for key, value in self.headers.items() if key.lower() not in excluded}
                headers["Host"] = owner.upstream.netloc
                if owner.entry.auth_type == "bearer":
                    headers["Authorization"] = "Bearer " + owner.entry.auth_value
                else:
                    headers["x-api-key"] = owner.entry.auth_value
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                # 部分网关（如 dpinfer）并发时对 /v1/messages 偶发返回空 404，重试即可成功；按可重试处理。
                messages_call = self.command == "POST" and incoming.path.rstrip("/").endswith("/v1/messages")
                flaky_404 = False
                for attempt in range(6):
                    try:
                        connection, target = owner.upstream_connection(incoming.path or "/", incoming.query)
                        connection.request(self.command, target, body=body, headers=headers)
                        response = connection.getresponse()
                        status = response.status
                        flaky_404 = messages_call and status == 404 and response.getheader("Content-Length") == "0"
                        if (status not in {429, 500, 502, 503, 504} and not flaky_404) or attempt == 5:
                            break
                        owner.record(status, started, "retryable-response", attempt=attempt)
                    except (OSError, ValueError, http.client.HTTPException) as error:
                        error_name = type(error).__name__
                        details = str(error).replace(owner.entry.auth_value, "<redacted>")[:240]
                        owner.record(502, started, error_name, attempt=attempt, details=details, errno=getattr(error, "errno", None))
                        if isinstance(error, ValueError) or attempt == 5:
                            self.respond(502, {"error": f"入口 {owner.entry.name} 上游连接失败: {error_name}"})
                            if connection:
                                connection.close()
                            return
                    if response:
                        response.close()
                    if connection:
                        connection.close()
                    connection = response = None
                    time.sleep((1, 2, 4, 8, 15)[attempt])
                if flaky_404:
                    # 重试仍失败：改报 503，让 Claude Code 按自身退避继续重试，而不是当作“模型不存在”直接失败。
                    response.close()
                    connection.close()
                    owner.record(503, started, "upstream-empty-404")
                    self.respond(503, {"type": "error", "error": {"type": "overloaded_error", "message": f"入口 {owner.entry.name} 上游多次返回空 404"}})
                    return
                try:
                    error_name = ""
                    self.send_response(status, response.reason)
                    excluded = HOP_HEADERS | {"server", "date"}
                    excluded |= {name.strip().lower() for name in response.getheader("Connection", "").split(",")}
                    for key, value in response.getheaders():
                        if key.lower() not in excluded:
                            self.send_header(key, value)
                    self.end_headers()
                    self.close_connection = True
                    if self.command != "HEAD":
                        while True:
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (OSError, http.client.HTTPException) as error:
                    error_name = type(error).__name__
                finally:
                    response.close()
                    connection.close()
                    owner.record(status, started, error_name)

            do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = forward

        self.server = ThreadingHTTPServer((bind_host, entry.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def upstream_connection(self, path: str, query: str):
        target = self.upstream.path.rstrip("/") + path
        if query:
            target += "?" + query
        port = self.upstream.port or (443 if self.upstream.scheme == "https" else 80)
        if self.proxy:
            connection_type = http.client.HTTPSConnection if self.upstream.scheme == "https" or self.proxy.scheme == "https" else http.client.HTTPConnection
            connection = connection_type(self.proxy.hostname, self.proxy.port or (443 if self.proxy.scheme == "https" else 80), timeout=self.timeout)
            if self.upstream.scheme == "https":
                connection.set_tunnel(self.upstream.hostname, port)
            else:
                target = "http://" + self.upstream.netloc + target
        else:
            connection_type = http.client.HTTPSConnection if self.upstream.scheme == "https" else http.client.HTTPConnection
            connection = connection_type(self.upstream.hostname, port, timeout=self.timeout)
        return connection, target

    def record(self, status: int, started: float, error: str, **details) -> None:
        with self.lock:
            if status >= 400 or error:
                self.failures += 1
        print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "entry": self.entry.name, "status": status, "seconds": round(time.monotonic() - started, 3), "error": error or None, **details}, ensure_ascii=False), flush=True)

    def connection_info(self) -> dict:
        host = self.advertised_host
        if ":" in host:
            host = "[" + host + "]"
        return {"name": self.entry.name, "env_file": str(self.entry.env_file), "base_url": f"http://{host}:{self.server.server_port}", "model": self.entry.model, "token": self.token}

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()


def read_connection(path: Path) -> dict:
    if path.is_symlink():
        raise RelayError("connection.json 不能为符号链接")
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as error:
        raise RelayError(f"反代尚未开启，找不到 {path}；请在原开发机执行 relay_on") from error
    if mode != 0o600:
        raise RelayError("connection.json 权限必须为 0600")
    payload = read_json(path)
    if payload.get("version") != 1 or not isinstance(payload.get("service_id"), str) or not isinstance(payload.get("entries"), list) or not payload["entries"]:
        raise RelayError("connection.json 格式无效，请在原开发机重新开启服务")
    names, files = set(), set()
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str) or not entry[key] for key in ("name", "env_file", "base_url", "token")):
            raise RelayError("connection.json 入口信息无效")
        checked_url(entry["base_url"])
        path_key = str(Path(entry["env_file"]).expanduser().resolve())
        if entry["name"] in names or path_key in files:
            raise RelayError("connection.json 入口重复")
        names.add(entry["name"])
        files.add(path_key)
    return payload


def match_entry(payload: dict, env_file: Path, model: str) -> dict:
    path = env_file.expanduser().resolve()
    for entry in payload["entries"]:
        if Path(entry["env_file"]).expanduser().resolve() == path:
            if entry.get("model") != model:
                raise RelayError(f"入口 {entry['name']} 的模型与项目配置不一致，请重新开启反代")
            return entry
    raise RelayError(f"配置文件未登记反代入口: {path}")


def check_entry(entry: dict, service_id: str, *, timeout: int = 10) -> dict:
    request = Request(entry["base_url"].rstrip("/") + HEALTH_PATH, headers={"Authorization": "Bearer " + entry["token"]})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            payload = json.loads(response.read(65536))
    except (OSError, ValueError) as error:
        raise RelayError(f"无法连接反代入口 {entry['name']}；请在开发机运行 relay.py status 或 relay.py start") from error
    if payload.get("service_id") != service_id or payload.get("entry") != entry["name"]:
        raise RelayError(f"反代入口 {entry['name']} 的服务身份不一致，请重新读取连接文件")
    return payload


def current_model_entry(entry: dict, config_path: Path) -> dict:
    """Model selection is client-side; re-read it without restarting shared services.

    Keep the running service's address and temporary token. Changing upstream
    authentication or routing still requires a service restart.
    """
    registrations = read_json(config_path).get("entries", [])
    registration = next((item for item in registrations if item.get("name") == entry["name"]), None)
    if registration is None:
        raise RelayError(f"入口配置未登记: {entry['name']}")
    api_format = registration.get("api_format", "anthropic")
    if api_format not in {"anthropic", "openai"}:
        raise RelayError(f"入口 {entry['name']} 的 api_format 必须为 anthropic 或 openai")
    entry = {**entry, "api_format": api_format}
    model_var = registration.get("model_var")
    if not model_var:
        return entry
    env_path = Path(registration["env_file"]).expanduser()
    if not env_path.is_absolute():
        env_path = config_path.parent / env_path
    file_format = registration.get("env_format", "json" if env_path.suffix.lower() == ".json" else "env")
    values = read_entry_values(env_path, file_format)
    model = values.get(model_var)
    if not model:
        raise RelayError(f"入口 {entry['name']} 的配置缺少模型变量 {model_var}")
    return {**entry, "model": model}


def proxy_environment(entry: dict, original: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if original is None else original)
    for key in PROXY_KEYS + AUTH_KEYS:
        env.pop(key, None)
    env["ANTHROPIC_BASE_URL"] = entry["base_url"]
    env["ANTHROPIC_AUTH_TOKEN"] = entry["token"]
    env["ANTHROPIC_API_KEY"] = entry["token"]
    env["RELAY_BASE_URL"] = entry["base_url"]
    env["RELAY_TOKEN"] = entry["token"]
    if entry.get("model"):
        env["ANTHROPIC_MODEL"] = entry["model"]
    env["CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"] = "1"
    host = urlsplit(entry["base_url"]).hostname
    for key in ("NO_PROXY", "no_proxy"):
        entries = [value.strip() for value in env.get(key, "").split(",") if value.strip()]
        env[key] = ",".join(dict.fromkeys([*entries, host]))
    return env


def remove_owned(path: Path, service_id: str) -> None:
    if path.exists() and read_json(path).get("service_id") == service_id:
        path.unlink()


def serve(config_path: Path, service_id: str) -> None:
    guard_master()
    root = config_path.parent
    lock_file = os.open(root / ".relay.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(lock_file)
        raise RelayError("反代服务已开启，不能重复启动") from error
    services = []
    stop_event = threading.Event()
    try:
        config, entries = load_config(config_path)
        advertised_host = config.get("advertised_host") or detect_host()
        for entry in entries:
            services.append(APIService(entry, bind_host=config.get("bind_host", "0.0.0.0"), advertised_host=advertised_host, service_id=service_id, timeout=config.get("timeout_sec", 3900)))
        for item in services:
            item.start()
        record = {"version": 1, "service_id": service_id, "host": host_identity(), "pid": os.getpid(), "process_start": process_start(os.getpid())}
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop_event.set())
        atomic_json(root / "relay.pid", record)
        atomic_json(root / "connection.json", {**record, "entries": [item.connection_info() for item in services]})
        print("反代服务已开启: " + str(root / "connection.json"), flush=True)
        for item in services:
            info = item.connection_info()
            print(info["name"] + " " + info["base_url"], flush=True)
        stop_event.wait()
    finally:
        for item in services:
            item.close()
        remove_owned(root / "connection.json", service_id)
        remove_owned(root / "relay.pid", service_id)
        os.close(lock_file)


def start(config_path: Path) -> None:
    guard_master()
    load_config(config_path)
    root = config_path.parent
    pid_path = root / "relay.pid"
    if pid_path.exists():
        record = read_json(pid_path)
        if record.get("host", {}).get("hostname") == host_identity()["hostname"]:
            if managed_process(record):
                status(root / "connection.json")
                print("已有反代服务，未重复启动")
                return
        # 其他开发机的 PID 不能在本机查询，更不能发送信号。
        # serve 在整个服务生命周期持有共享锁；锁空闲才允许接替旧记录。
        # 此处仅预检查，serve 启动时仍会原子获取同一把锁，防止并发启动。
        lock_file = os.open(root / ".relay.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RelayError("反代服务仍在运行；请在原开发机停止服务后再在本机启动") from error
        finally:
            os.close(lock_file)
    service_id = secrets.token_hex(16)
    descriptor = os.open(root / "relay.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--config", str(config_path), "serve", "--service-id", service_id], stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RelayError("启动失败，请查看 " + str(root / "relay.log"))
        with contextlib.suppress(RelayError):
            payload = read_connection(root / "connection.json")
            if payload["service_id"] == service_id:
                for entry in payload["entries"]:
                    local = {**entry, "base_url": "http://127.0.0.1:" + str(urlsplit(entry["base_url"]).port)}
                    check_entry(local, service_id, timeout=2)
                print("反代服务已在后台开启: " + str(root / "connection.json"))
                return
        time.sleep(0.2)
    child.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=10)
    raise RelayError("启动检查超时，请查看 " + str(root / "relay.log"))


def stop(root: Path) -> None:
    guard_master()
    pid_path = root / "relay.pid"
    if not pid_path.exists():
        print("反代服务未运行")
        return
    record = read_json(pid_path)
    if managed_process(record):
        os.kill(record["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and pid_path.exists() and managed_process(record):
            time.sleep(0.2)
        if pid_path.exists() and managed_process(record):
            raise RelayError("反代尚未停止，请查看日志；没有强制关闭其他进程")
    remove_owned(root / "connection.json", record["service_id"])
    remove_owned(pid_path, record["service_id"])
    print("反代服务已关闭")


def status(path: Path, selected: str | None = None, *, config_path: Path | None = None) -> None:
    payload = read_connection(path)
    entries = [entry for entry in payload["entries"] if selected is None or entry["name"] == selected]
    if not entries:
        raise RelayError(f"入口未登记: {selected}")
    for entry in entries:
        if config_path is not None:
            entry = current_model_entry(entry, config_path)
        health = check_entry(entry, payload["service_id"])
        print(f"{entry['name']} {entry['base_url']} 模型={entry.get('model') or '-'} 请求={health['requests']} 失败={health['failures']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通用已登记 API 反代")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--connection", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start")
    commands.add_parser("stop")
    service_parser = commands.add_parser("serve")
    service_parser.add_argument("--service-id", default=None)
    for name in ("status", "check"):
        command_parser = commands.add_parser(name)
        command_parser.add_argument("--entry")
    exec_parser = commands.add_parser("exec")
    exec_parser.add_argument("--entry", required=True)
    exec_parser.add_argument("program", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    config_path = args.config.expanduser().resolve()
    connection_path = args.connection or config_path.parent / "connection.json"
    try:
        if args.command == "start":
            start(config_path)
        elif args.command == "stop":
            stop(config_path.parent)
        elif args.command == "serve":
            serve(config_path, args.service_id or secrets.token_hex(16))
        elif args.command in {"status", "check"}:
            status(connection_path, args.entry, config_path=config_path)
        else:
            payload = read_connection(connection_path)
            entry = next((entry for entry in payload["entries"] if entry["name"] == args.entry), None)
            if entry is None:
                raise RelayError(f"入口未登记: {args.entry}")
            entry = current_model_entry(entry, config_path)
            check_entry(entry, payload["service_id"])
            program = args.program[1:] if args.program[:1] == ["--"] else args.program
            if not program:
                raise RelayError("exec 需要 -- 后面的程序命令")
            env = proxy_environment(entry)
            if Path(program[0]).name == "claude" and entry.get("model"):
                flags = {argument.split("=", 1)[0] for argument in program[1:]}
                if "--bare" not in flags:
                    program[1:1] = ["--bare"]
                if "--restricted" not in flags:
                    program[1:1] = ["--restricted"]
                if "--tools" not in flags:
                    program[1:1] = ["--tools", "default"]
                model = entry["model"]
                native = model.startswith("claude-") or model in {"sonnet", "opus", "haiku"}
                env["ANTHROPIC_MODEL"] = model if native else "claude-opus-4-8"
                program[1:1] = ["--model", env["ANTHROPIC_MODEL"]]
                if not native:
                    program[1:1] = ["--settings", json.dumps({"modelOverrides": {"claude-opus-4-8": model}}, separators=(",", ":"))]
                if not any(argument.split("=", 1)[0] in {"--session-id", "--resume", "--continue", "-r", "-c"} for argument in program[1:]):
                    program[1:1] = ["--session-id", str(uuid.uuid4())]
            if entry.get("api_format") == "openai":
                from chat_compat import run_client
                return run_client(entry, program, env)
            os.execvpe(program[0], program, env)
    except (RelayError, OSError) as error:
        print("反代错误: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
