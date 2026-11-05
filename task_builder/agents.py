from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import environment_for_subprocess
from .models import AgentError, EnvConfig, TaskTimeout
from .deadline import has_deadline, remaining_timeout, run_process


@dataclass(frozen=True)
class AgentRun:
    role: str
    session_id: str
    stdout: str
    stderr: str
    returncode: int
    command: tuple[str, ...]
    usage: dict | None = None


# 单次模型请求超时与重试次数（Claude Code 环境变量）：上游卡住时尽快放弃并重试，不叠加成数小时等待。
_API_TIMEOUT_MS = "600000"
_API_MAX_RETRIES = "3"
_USAGE_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def parse_print_json(text: str) -> tuple[str, dict | None]:
    """解析 `claude --print --output-format json` 的输出，返回 (result 文本, 用量)；不是该格式时原样返回。"""
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return text, None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return text, None
    if not isinstance(payload, dict) or payload.get("type") != "result":
        return text, None
    raw = payload.get("usage") or {}
    usage = {key: int(raw.get(key) or 0) for key in _USAGE_TOKEN_KEYS}
    usage.update(
        num_turns=int(payload.get("num_turns") or 0),
        duration_ms=int(payload.get("duration_ms") or 0),
        duration_api_ms=int(payload.get("duration_api_ms") or 0),
        is_error=bool(payload.get("is_error")),
    )
    return str(payload.get("result") or ""), usage


def record_usage(path: Path, run: AgentRun) -> None:
    """把一次 agent 调用的 token 用量追加到 usage.jsonl；没有用量（旧格式或进程被杀）时跳过。"""
    if not run.usage:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"time": time.time(), "role": run.role, "session_id": run.session_id, "returncode": run.returncode, **run.usage}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


_INLINE_SETTINGS_LIMIT = 100_000
_BINARY_SUFFIXES = {".pdf", ".h5", ".h5ad", ".gz", ".zip", ".7z", ".npz", ".npy", ".pkl", ".rds", ".bam"}
_READ_MAX_OUTPUT_TOKENS = 8000
_READ_GUIDANCE = """
统一文件读取规则：
- TXT、CSV、TSV、JSONL、SMILES、SDF、FASTA 等文本数据可以用 Read 分段读取，不因整个文件较大而禁止读取。
- 查看数据时先指定 offset=1、limit=50；需要更多内容再调整 offset，每次 limit 不超过 200 行。源码和论文长文也按段读取。
- 不使用 allow_large 绕过单次读取量限制；若 Read 提示内容过长，减小 limit，不要重复请求全文。Grep 查内容时设置 head_limit，先查看少量匹配。
- 先用提供的文件清单或 Glob 确认真实路径；文件不存在时不要反复尝试同一路径。作者代码引用的预测结果可能需要先运行代码才会生成，不能当作已有输入。
- plan.json 等本阶段要创建的文件尚不存在时，直接用 Write 创建；后续修改前先 Read。
- 压缩包、HDF5、NumPy 等二进制文件不要直接交给 Read/Grep。依据文件清单、说明及源码判断格式；只有已获准使用 Bash 的阶段才能用现有工具做有限的解压或数据预览。
""".strip()


def _deny_read(path: Path) -> bool:
    # A deny rule blocks the whole file, even Read(offset=1, limit=5).
    # Limit text through Read's output budget instead of the file's disk size.
    return path.suffix.lower() in _BINARY_SUFFIXES


class IndependentClaudeAgent:
    """每次调用都创建新的 Claude Code 进程和新的 session。"""

    def __init__(self, *, claude_bin: str = "claude", timeout_sec: int = 3600):
        self.claude_bin = claude_bin
        self.timeout_sec = timeout_sec

    @staticmethod
    def _model_selection(model_id: str) -> tuple[str, dict[str, object]]:
        if model_id.startswith("claude-") or model_id in {"sonnet", "opus", "haiku"}:
            return model_id, {}
        return "claude-opus-4-8", {"modelOverrides": {"claude-opus-4-8": model_id}}

    def run(
        self,
        *,
        role: str,
        prompt: str,
        workdir: Path,
        env_config: EnvConfig,
        allowed_dirs: tuple[Path, ...] = (),
        read_only_dirs: tuple[Path, ...] = (),
        resume_session: str | None = None,
    ) -> AgentRun:
        workdir.mkdir(parents=True, exist_ok=True)
        resolved_allowed_dirs = tuple(path.resolve() for path in allowed_dirs)
        resolved_read_only_dirs = tuple(path.resolve() for path in read_only_dirs)
        unavailable_read_only_dirs = set(resolved_read_only_dirs) - set(resolved_allowed_dirs)
        if unavailable_read_only_dirs:
            paths = ", ".join(str(path) for path in sorted(unavailable_read_only_dirs))
            raise AgentError(f"只读目录必须同时列入允许访问目录: {paths}", stage=role)
        session_id = resume_session or str(uuid.uuid4())
        reviewer_only = role.startswith(("reviewer",))
        selected_model, model_settings = self._model_selection(env_config.model_id)
        # Writer 与 Repair 需要用 Bash 复制、改名、转换草稿 data/ 中的数据文件。
        writes_data = role.startswith(("writer", "repair-writer"))
        if reviewer_only:
            tools = ("Read", "Glob", "Grep")
        elif writes_data:
            tools = ("Read", "Glob", "Grep", "Edit", "Write", "Bash")
        else:
            tools = ("Read", "Glob", "Grep", "Edit", "Write")
        command_parts = [
            self.claude_bin,
            "--print",
            "--output-format",
            "json",
            "--model",
            selected_model,
            "--resume" if resume_session else "--session-id",
            session_id,
            "--bare",
            "--tools",
            *tools,
        ]
        command_parts.extend(("--restricted", "--permission-prompts", "none"))
        prompt += "\n\n" + _READ_GUIDANCE
        if role.startswith("planner"):
            prompt += "\n立即完成并保存 plan.json；论文正文读 context/text.md，图表按需读 context/figures/ 与 context/tables/。不得遍历全部数据文件；根据论文评估章节和必要源码完成计划。"
        elif not reviewer_only:
            prompt += "\n立即创建并保存五个必需文件，先 Read plan.json，再用 Write。论文正文读 context/text.md；不要重复阅读数据预览。禁止执行科研计算和生成答案结果；只写可以后续运行的程序。"
        if writes_data:
            prompt += "\n使用 Bash 时每次只执行一条命令，不要用 &&、;、| 连接多条命令。数据文件用 cp 复制、mv 改名；修改数据时生成新文件，不要原地改写来源文件。文本数据可用 Read 分段预览；压缩或二进制数据用 zcat 或 python3 等现有工具有限预览，不进行科研计算。"
        settings = dict(model_settings)
        permissions: dict[str, list[str]] = {}
        if writes_data:
            permissions["allow"] = [
                "Bash(cp *)",
                "Bash(mv *)",
                "Bash(gzip *)",
                "Bash(gunzip *)",
                "Bash(zcat *)",
                "Bash(xz *)",
                "Bash(tar *)",
                "Bash(7z *)",
                "Bash(7za *)",
                "Bash(unzip *)",
                "Bash(du *)",
                "Bash(wc *)",
                "Bash(ln *)",
                "Bash(touch *)",
                "Bash(md5sum *)",
                "Bash(sha256sum *)",
                "Bash(file *)",
                "Bash(stat *)",
                "Bash(python3 *)",
                "Bash(/usr/bin/python3 *)",
                "Bash(bash *)",
                "Bash(sh *)",
                "Bash(cat *)",
                "Bash(ls *)",
                "Bash(mkdir *)",
                "Bash(find *)",
                "Bash(grep *)",
                "Bash(sed *)",
                "Bash(head *)",
                "Bash(tail *)",
                "Bash(pytest *)",
                "Bash(rm *)",
                "Bash(cd _scratch && *)",
                "Bash(sed * && *)",
                "Bash(python3 * && *)",
                "Bash(/usr/bin/python3 * && *)",
            ]
        # Claude Code 会用 Edit/Read 拒绝规则核对 Bash 命令里的全部路径参数，cp 来源数据也会被拒。
        # Writer/Repair 因此不加这两类规则。
        if resolved_read_only_dirs and not writes_data:
            permissions["deny"] = [
                f"Edit(//{path.as_posix().lstrip('/')}/**)"
                for path in resolved_read_only_dirs
            ]
        for directory in () if writes_data else (workdir.resolve(), *resolved_allowed_dirs):
            for path in directory.rglob("*"):
                if path.is_file() and _deny_read(path):
                    permissions.setdefault("deny", []).extend(f"{tool}(//{path.as_posix().lstrip('/')})" for tool in ("Read", "Grep"))
        if permissions:
            settings["permissions"] = permissions
        settings_file = None
        if settings:
            settings_text = json.dumps(settings, separators=(",", ":"))
            # 单个命令行参数不能超过 128 KiB（MAX_ARG_STRLEN）；数据文件很多时禁读规则会超限，改用文件传入。
            if len(settings_text.encode()) > _INLINE_SETTINGS_LIMIT:
                handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="task-builder-settings-", suffix=".json", delete=False)
                with handle:
                    handle.write(settings_text)
                settings_file = Path(handle.name)
                settings_text = str(settings_file)
            command_parts.extend(("--settings", settings_text))
        if not reviewer_only:
            command_parts.extend(("--permission-mode", "acceptEdits"))
        for allowed_dir in dict.fromkeys((workdir.resolve(), *resolved_allowed_dirs)):
            command_parts.extend(("--add-dir", str(allowed_dir)))
        command = tuple(command_parts)
        def safe_output(value: str | bytes) -> str:
            text = value.decode(errors="replace") if isinstance(value, bytes) else value
            if env_config.values.get("TASK_BUILDER_MODEL_RELAY_ACTIVE") == "1":
                token = env_config.values.get("ANTHROPIC_AUTH_TOKEN", "")
                if token:
                    text = text.replace(token, "<temporary-proxy-token>")
            return text

        try:
            return self._execute(role, session_id, command, prompt, workdir, env_config, resume_session, safe_output)
        finally:
            if settings_file is not None:
                settings_file.unlink(missing_ok=True)

    def _execute(self, role, session_id, command, prompt, workdir, env_config, resume_session, safe_output) -> AgentRun:
        environment = environment_for_subprocess(env_config)
        # This native Claude Code limit works in --bare mode too. A smaller
        # user limit is preserved; oversized reads can retry with fewer lines.
        try:
            read_limit = int(environment.get("CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS", ""))
        except ValueError:
            read_limit = _READ_MAX_OUTPUT_TOKENS
        environment["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] = str(
            min(read_limit, _READ_MAX_OUTPUT_TOKENS) if read_limit > 0 else _READ_MAX_OUTPUT_TOKENS
        )
        environment.setdefault("API_TIMEOUT_MS", _API_TIMEOUT_MS)
        environment.setdefault("CLAUDE_CODE_MAX_RETRIES", _API_MAX_RETRIES)
        usage_total: dict | None = None
        try:
            execute = run_process if has_deadline() else subprocess.run
            for attempt in range(3):
                completed = execute(
                    list(command),
                    cwd=workdir,
                    env=environment,
                    input=prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=remaining_timeout(self.timeout_sec),
                    check=False,
                )
                output_text, usage = parse_print_json(completed.stdout or "")
                if usage:
                    usage["model"] = env_config.model_id
                    # 重试前失败的尝试同样消耗 token，累计到本次调用。
                    usage_total = usage if usage_total is None else {
                        key: (usage_total.get(key, 0) + value if isinstance(value, int) and not isinstance(value, bool) else value)
                        for key, value in usage.items()
                    }
                error_text = (completed.stderr or "") + "\n" + output_text
                connection_failure = any(marker in error_text.lower() for marker in ("apiconnectionerror", "apitimeouterror", "econnreset", "econnrefused", "上游连接失败", "api error: 502", "api error: 503", "api error: 504", "api error: 429", "unable to connect to api", "connection error", "request timed out"))
                if completed.returncode == 0 or not connection_failure or attempt == 2:
                    break
                time.sleep(remaining_timeout((5, 15)[attempt]))
                if resume_session:
                    continue
                session_id = str(uuid.uuid4())
                retry_command = list(command)
                retry_command[retry_command.index("--session-id") + 1] = session_id
                command = tuple(retry_command)
        except TaskTimeout as exc:
            for field in ("stdout", "stderr"):
                if getattr(exc, field, None):
                    setattr(exc, field, safe_output(getattr(exc, field)))
            raise
        except FileNotFoundError as exc:
            raise AgentError(f"找不到 Claude Code: {self.claude_bin}", stage=role) from exc
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or b""
            errors = exc.stderr or b""
            result = AgentRun(role, session_id, safe_output(output), safe_output(errors), -1, command, usage_total)
            failure = AgentError(f"{role} agent 超时（{self.timeout_sec} 秒）", stage=role)
            failure.run = result
            raise failure from exc
        result = AgentRun(
            role=role,
            session_id=session_id,
            stdout=safe_output(output_text),
            stderr=safe_output(completed.stderr),
            returncode=completed.returncode,
            command=command,
            usage=usage_total,
        )
        if completed.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"退出码 {completed.returncode}"
            failure = AgentError(f"{role} agent 失败: {message}", stage=role)
            failure.run = result
            raise failure
        return result


def _repair_unescaped_json_quotes(candidate: str) -> str:
    repaired: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(candidate):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            repaired.append(char)
            escaped = True
            continue
        if char != '"':
            repaired.append(char)
            continue
        next_index = index + 1
        while next_index < len(candidate) and candidate[next_index].isspace():
            next_index += 1
        if next_index == len(candidate) or candidate[next_index] in ",:}]":
            repaired.append(char)
            in_string = False
        else:
            repaired.append('\\"')
    return "".join(repaired)


def extract_json(text: str) -> dict:
    candidates: list[str] = []
    candidates.extend(match.group(1) for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL))
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    # 模型偶尔漏写最外层的左花括号，有时连同开头的引号一起漏掉，输出以 "pass": 或 pass": 开头。
    stripped = text.strip()
    if last >= 0 and not stripped.startswith("{"):
        head = stripped[: stripped.rfind("}") + 1]
        # 开头是引号说明只少了 {；否则连 {" 一起漏了。
        candidates.insert(0, ("{" if stripped.startswith('"') else '{"') + head)
    for candidate in candidates:
        for parsed_candidate in (candidate, _repair_unescaped_json_quotes(candidate)):
            try:
                value = json.loads(parsed_candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise AgentError("agent 输出不是有效 JSON")


def save_agent_log(path: Path, run: AgentRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "ROLE: " + run.role + "\n"
        + "SESSION_ID: " + run.session_id + "\n"
        + "RETURN_CODE: " + str(run.returncode) + "\n"
        + "COMMAND:\n" + " ".join(run.command) + "\n\n"
        + "STDOUT:\n" + run.stdout + "\n\nSTDERR:\n" + run.stderr,
        encoding="utf-8",
    )
    # 每次 agent 调用恰好保存一次日志，用量随日志一起记到同目录的 usage.jsonl。
    record_usage(path.parent / "usage.jsonl", run)
