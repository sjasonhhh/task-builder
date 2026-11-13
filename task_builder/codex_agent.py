"""Codex CLI adapter with the same contract as the Claude authoring agent."""
from __future__ import annotations

import hashlib
from functools import lru_cache
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .agent_config import codex_settings, codex_config_arguments, model_connection, redact
from .agents import AgentRun
from .config import environment_for_subprocess
from .deadline import remaining_timeout, run_process
from .models import AgentError, EnvConfig, TaskTimeout
from .usage_capture import UsageCapture, CodexRolloutWatcher, source_root, normalize_usage


@lru_cache(maxsize=16)
def cli_isolation_flags(executable: str) -> tuple[str, ...]:
    """Older portable CLIs rely on the isolated CODEX_HOME instead."""
    try:
        help_text = subprocess.run([executable, "exec", "--help"], capture_output=True,
                                   text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    return ("--ignore-user-config",) if "--ignore-user-config" in help_text else ()


def parse_codex_events(text: str) -> tuple[str, str, dict | None, str]:
    session, messages, failures = "", [], []
    usage = None
    completed = False
    terminal_failure = False
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            session = event.get("thread_id") or session
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                messages.append(str(item.get("text") or ""))
        elif kind == "turn.completed":
            completed = True
            raw = event.get("usage")
            if isinstance(raw, dict):
                # This is a session snapshot, including earlier resumed turns.
                # UsageCapture accounts for this invocation using native requests / a baseline.
                usage = {**normalize_usage(raw, "codex"), "num_turns": (usage or {}).get("num_turns", 0) + 1,
                         "usage_scope": "session_cumulative"}
        elif kind in {"turn.failed", "error"}:
            terminal_failure |= kind == "turn.failed"
            failure = event.get("error") or event.get("message") or "Codex 请求失败"
            failures.append(failure.get("message", str(failure)) if isinstance(failure, dict) else str(failure))
    # Error events can describe a recovered retry. Only terminal failure wins.
    error = "\n".join(failures) if not completed or terminal_failure else ""
    if not completed and not error:
        error = "Codex 未返回完整的 turn.completed 事件"
    return "\n".join(messages), session, usage, error


def adapt_prompt(prompt: str, *, role: str = "", remote_authoring: bool = False) -> str:
    names = {"Read": "分段读取文件", "Write": "写入文件", "Edit": "修改文件",
             "Glob": "查找文件路径", "Grep": "搜索文件内容", "Bash": "shell"}
    prompt = re.sub(r"\b(Read|Write|Edit|Glob|Grep|Bash)\b", lambda m: names[m[0]], prompt)
    prompt += """

你正在使用 Codex。使用 shell 的 sed/head/rg 分段读取文本，使用 apply_patch 或写文件命令保存文件。
先看文件清单；文本数据先预览 50 行，每次不超过 200 行，不要读取整份大文件。
shell 命令使用 login=false，不加载登录启动脚本。
不要直接打印二进制文件；读取其结构时只做少量格式检查和数据预览。来源材料只读，复制数据到草稿后再处理。
"""
    if remote_authoring and role.startswith(("writer", "repair-writer")):
        prompt += """
当前处于隔离的远端造题沙盒。为解决当前任务的参考数值、基线或指标校准缺口，允许在草稿临时目录执行必要的数据核验、原文评估代码和校准计算；不必等到最终 Harbor 验证才补齐审核所需的数值。
计算必须使用当前来源的数据和有依据的科学定义，记录执行的程序、输入、输出以及与原文图表或数值的核对证据。计算得到候选参考值不等于已证明与原文一致，不能代替 rubric 要求的原文依据；不得为了通过验证调整指标、基线或评分标准。
输入来源仍只读，最终隐藏参考只能进入评测或参考提交文件，不能进入求解者 data/。只保留交付必需文件和可审计证据，结束前删除临时脚本、缓存和中间结果。完成后必须实际保存文件。
"""
    else:
        prompt += "不得运行科研计算或生成答案结果；本阶段只生成以后执行的程序。完成后必须实际保存文件。\n"
    return prompt


class IndependentCodexAgent:
    def __init__(self, *, codex_bin: str = "codex", timeout_sec: int = 3600):
        self.codex_bin = codex_bin
        self.timeout_sec = timeout_sec
        # SQLite's live state must be on the local filesystem: shared GPFS can
        # make Codex resume fail even though the durable rollout is intact.
        self._state_directory = tempfile.TemporaryDirectory(prefix="task-builder-codex-state-", dir="/tmp")

    def run(self, *, role: str, prompt: str, workdir: Path, env_config: EnvConfig,
            allowed_dirs: tuple[Path, ...] = (), read_only_dirs: tuple[Path, ...] = (),
            resume_session: str | None = None) -> AgentRun:
        workdir = workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        allowed = {p.resolve() for p in allowed_dirs}
        readonly = {p.resolve() for p in read_only_dirs}
        if readonly - allowed:
            raise AgentError("只读目录必须同时列入允许访问目录", stage=role)
        # Keep session storage outside the draft and share it across continuations.
        root = source_root(workdir)
        home = root / "logs" / "codex" / hashlib.sha256(str(workdir).encode()).hexdigest()[:16]
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = environment_for_subprocess(env_config)
        # This is the child's native Codex state directory, never the user's home.
        environment["CODEX_HOME"] = str(home)
        state = Path(self._state_directory.name) / home.name
        state.mkdir(mode=0o700, exist_ok=True)
        environment["CODEX_SQLITE_HOME"] = str(state)
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_PERMISSION_PROFILE"):
            environment.pop(key, None)
        base_url, _ = model_connection(env_config.values, "codex")
        settings = codex_settings(base_url, env_config.values.get("REASONING_EFFORT"))
        settings.update(approval_policy="never", allow_login_shell=False,
                        shell_environment_policy={"inherit": "all"})
        # Repair may update its candidate plan, but source materials stay read-only.
        reviewer = role.startswith("reviewer")
        candidate_plan = workdir.parent / "plan"
        writable = [candidate_plan] if (
            role.startswith("repair-writer") and candidate_plan in allowed and candidate_plan not in readonly
        ) else []
        settings["sandbox_mode"] = "read-only" if reviewer else "workspace-write"
        settings["sandbox_workspace_write"] = {"writable_roots": [str(path) for path in writable], "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True}
        command = [self.codex_bin, "exec"]
        if resume_session:
            command += ["resume", resume_session]
        command += ["--json", "--skip-git-repo-check", *cli_isolation_flags(self.codex_bin), "--model", env_config.model_id]
        command += codex_config_arguments(settings)
        command += ["-"]
        prompt = adapt_prompt(prompt, role=role,
                              remote_authoring=bool(os.environ.get("TASK_BUILDER_SANDBOX_PARENT")))
        prompt += "\n可读取的材料目录：" + ", ".join(str(p) for p in sorted(allowed))
        destinations = "、".join(str(path) for path in [workdir, *writable])
        prompt += "\n仅审核并输出结论，不修改任何文件。" if reviewer else f"\n仅在 {destinations} 内保存本阶段文件；其他材料目录不可修改。"
        capture = UsageCapture(workdir, role=role, model=env_config.model_id, agent="codex",
                               session_id=resume_session or "", resumed=bool(resume_session), base_url=base_url)
        watcher = CodexRolloutWatcher(home, capture)
        try:
            completed = run_process(command, cwd=workdir, env=environment, input=prompt, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=remaining_timeout(self.timeout_sec), check=False,
                                    on_stdout=capture.codex_line, on_poll=watcher.poll)
        except FileNotFoundError as exc:
            capture.finish(status="launch_failed")
            raise AgentError(f"找不到 Codex: {self.codex_bin}", stage=role) from exc
        except (subprocess.TimeoutExpired, TaskTimeout) as exc:
            raw = redact(getattr(exc, "stdout", None), env_config.values)
            output, session, usage, _ = parse_codex_events(raw)
            capture.replay(raw)
            watcher.poll(force=True)
            usage = capture.finish(status="timeout")
            result = AgentRun(role, session or resume_session or "", output,
                              redact(getattr(exc, "stderr", None), env_config.values), -1, tuple(command), usage, raw)
            if isinstance(exc, TaskTimeout):
                exc.stdout, exc.stderr, exc.run = raw, result.stderr, result
                raise
            failure = AgentError(f"{role} Codex 超时（{self.timeout_sec} 秒）", stage=role)
            failure.run = result
            raise failure from exc
        except BaseException:
            watcher.poll(force=True)
            capture.finish(status="interrupted")
            raise
        raw = redact(completed.stdout, env_config.values)
        output, session, usage, error = parse_codex_events(raw)
        if not session and not resume_session:
            error = error or "Codex 未返回会话编号"
        capture.replay(raw)
        watcher.poll(force=True)
        usage = capture.finish(status="failed" if error or completed.returncode else "completed",
                               returncode=completed.returncode or (1 if error else 0))
        usage.update(reasoning_effort=env_config.values.get("REASONING_EFFORT"), is_error=bool(error or completed.returncode))
        result = AgentRun(role, session or resume_session or "", output, redact(completed.stderr, env_config.values),
                          completed.returncode or (1 if error else 0), tuple(command), usage, raw)
        if result.returncode:
            detail = error or result.stdout
            if result.stderr:
                detail += "\n" + result.stderr[-4000:]
            failure = AgentError(f"{role} Codex 失败: {detail}", stage=role)
            failure.run = result
            raise failure
        return result
