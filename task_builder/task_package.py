"""Use the same task Dockerfile setup on native Harbor and shared cluster images."""
from __future__ import annotations

from dataclasses import dataclass
import glob
import hashlib
import json
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import uuid

from .models import StageFailure

_MARKER = "# task-builder shared-image boundary sha256="


def public_data(task: Path) -> Path:
    return task / "environment/data" if (task / "environment/data").is_dir() else task / "data"


def is_standard_package(task: Path) -> bool:
    return (task / "environment/Dockerfile").is_file() and (task / "tests/Dockerfile").is_file()


def _error(message: str) -> StageFailure:
    return StageFailure(message, stage="task-package")


def instructions(text: str) -> list[tuple[str, str]]:
    """Read Dockerfile continuations and RUN heredocs without executing them."""
    lines = iter(text.splitlines())
    result = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        while line.rstrip().endswith("\\"):
            line = line.rstrip()[:-1] + " " + next(lines, "")
        match = re.match(r"\s*([A-Za-z]+)\s+(.*)", line, re.S)
        if not match:
            raise _error("无效的 Dockerfile 指令: " + line[:120])
        name, value = match[1].upper(), match[2].strip()
        delimiters = re.findall(r"<<(-?)[\"']?([A-Za-z_][A-Za-z_0-9]*)[\"']?", value)
        if delimiters:
            if name != "RUN":
                raise _error("共享镜像文件准备仅支持 RUN heredoc；COPY 请引用实际源文件")
            for strip_tabs, delimiter in delimiters:
                for body in lines:
                    value += "\n" + body
                    if (body.lstrip("\t") if strip_tabs else body) == delimiter:
                        break
                else:
                    raise _error("Dockerfile heredoc 未结束: " + delimiter)
        result.append((name, value))
    return result


def _boundary(text: str) -> tuple[str, str] | None:
    match = re.search(r"^" + re.escape(_MARKER) + r"([0-9a-f]{64})\s*$", text, re.M)
    if not match:
        return None
    base = text[:match.start()].rstrip() + "\n"
    if hashlib.sha256(base.encode()).hexdigest() != match[1]:
        raise _error("共享 Dockerfile 定义与任务包记录不一致，请重新打包")
    return base, text[match.end():].lstrip("\n")


def _arguments(value: str) -> list[str]:
    try:
        parts = json.loads(value) if value.startswith("[") else shlex.split(value)
    except (ValueError, TypeError) as exc:
        raise _error("无法解析 Dockerfile 参数: " + value[:120]) from exc
    if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
        raise _error("Dockerfile 参数必须是字符串列表")
    return parts


def _local_sources(context: Path, pattern: str) -> list[Path]:
    if pattern.startswith("/") or ".." in Path(pattern).parts or "://" in pattern:
        raise _error("Dockerfile COPY 来源必须在自身构建目录中: " + pattern)
    found = sorted(Path(p) for p in glob.glob(str(context / pattern)))
    if not found:
        raise _error("Dockerfile COPY 来源不存在: " + pattern)
    for path in found:
        if not path.resolve().is_relative_to(context.resolve()):
            raise _error("Dockerfile COPY 来源越出构建目录: " + pattern)
    return found


def _copy_dependencies(base: str, source: Path, target: Path) -> None:
    for name, value in instructions(base):
        if name not in {"COPY", "ADD"} or value.startswith("--from="):
            continue
        while value.startswith("--"):
            _, _, value = value.partition(" ")
            value = value.lstrip()
        for pattern in _arguments(value)[:-1]:
            for path in _local_sources(source, pattern):
                destination = target / path.relative_to(source)
                if destination.exists():
                    if path.is_file() and destination.is_file() and path.read_bytes() == destination.read_bytes():
                        continue
                    raise _error("任务文件覆盖了共享镜像构建依赖: " + destination.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if path.is_dir():
                    shutil.copytree(path, destination)
                else:
                    shutil.copy2(path, destination)


def _base_state(base: str) -> tuple[str, dict[str, str]]:
    cwd, environment = "/", {}
    for name, value in instructions(base):
        if name == "FROM":
            cwd, environment = "/", {}
        elif name == "ENV":
            parts = _arguments(value)
            pairs = parts if parts and "=" in parts[0] else [parts[0] + "=" + " ".join(parts[1:])]
            for pair in pairs:
                key, _, val = pair.partition("=")
                environment[key] = val
        elif name == "WORKDIR":
            cwd = posixpath.normpath(posixpath.join(cwd, value.strip("\"'")))
    return cwd, environment


def compose_context(context: Path, shared: Path, kind: str, image: str, default_tail: str) -> None:
    template = shared / f"Dockerfile.{kind}"
    if not template.is_file():
        raise _error("缺少共享环境定义: " + str(template))
    base = template.read_text().rstrip() + "\n"
    dockerfile = context / "Dockerfile"
    if dockerfile.is_file():
        authored = dockerfile.read_text()
        sealed = _boundary(authored)
        if sealed:
            if sealed[0] != base:
                raise _error("共享环境定义已变化；请使用与成品匹配的领域环境，或重新验证打包")
            compile_context(context)
            return
        if authored.startswith(base.rstrip()):
            tail = authored[len(base.rstrip()):].lstrip()
        else:
            parsed = instructions(authored)
            if parsed and parsed[0][0] == "FROM":
                if _arguments(parsed[0][1]) != [image]:
                    raise _error(f"{context.name}/Dockerfile 请沿用 Dockerfile.{kind}，或 FROM 配置中的对应共享镜像")
                tail = "\n".join(name + " " + value for name, value in parsed[1:])
            elif any(name == "FROM" for name, _ in parsed):
                raise _error("任务新增构建步骤不能更换共享镜像")
            else:
                tail = authored
    else:
        tail = default_tail
    if not tail.strip():
        raise _error(f"{context.name}/Dockerfile 缺少任务文件装配与运行入口")
    _copy_dependencies(base, shared, context)
    dockerfile.write_text(base + "\n" + _MARKER + hashlib.sha256(base.encode()).hexdigest() + "\n" + tail.rstrip() + "\n")
    compile_context(context)


@dataclass
class ContextSetup:
    commands: list[str]
    cwd: str
    environment: dict[str, str]
    remote_context: str


def compile_context(context: Path, *, remote_context: str = "/tmp/task-builder-context") -> ContextSetup:
    """Apply task file setup to the shared image, never reinstall its dependencies.

    Build-only features not supported by this backend are rejected explicitly.
    """
    split = _boundary((context / "Dockerfile").read_text())
    if split is None:
        raise _error("任务包 Dockerfile 尚未关联共享环境，请通过 run_task 准备任务")
    base, tail = split
    cwd, base_env = _base_state(base)
    environment: dict[str, str] = {}
    arguments: dict[str, str] = {}
    commands: list[str] = []
    shell = ["/bin/sh", "-c"]

    def expand(value: str) -> str:
        values = {**base_env, **arguments, **environment}
        def replace(match):
            key = match[1] or match[2]
            if key not in values:
                raise _error("Dockerfile 引用了未定义的构建变量: " + key)
            return values[key]
        return re.sub(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}|\$([A-Za-z_][A-Za-z_0-9]*)", replace, value)

    ignore = context / ".dockerignore"
    if ignore.is_file() and any(line.strip() and not line.lstrip().startswith("#") for line in ignore.read_text().splitlines()):
        raise _error("共享镜像装载暂不处理 .dockerignore；请将构建上下文整理为实际所需文件，避免与原生 Docker 构建结果不同")
    for name, value in instructions(tail):
        if name == "WORKDIR":
            cwd = posixpath.normpath(posixpath.join(cwd, expand(value.strip("\"'"))))
            commands.append("mkdir -p " + shlex.quote(cwd))
        elif name in {"ENV", "ARG"}:
            parts = _arguments(value)
            pairs = parts if parts and "=" in parts[0] else [parts[0] + "=" + " ".join(parts[1:])]
            updated = {}
            for pair in pairs:
                key, _, val = pair.partition("=")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
                    raise _error("无效的构建变量名: " + key)
                updated[key] = expand(val)
            (environment if name == "ENV" else arguments).update(updated)
        elif name == "SHELL":
            shell = _arguments(value)
            if not shell:
                raise _error("Dockerfile SHELL 不能为空")
        elif name == "COPY":
            flags = {}
            while value.startswith("--"):
                option, _, value = value.partition(" ")
                flag, _, setting = option[2:].partition("=")
                if flag not in {"chmod", "chown"} or not setting:
                    raise _error("共享镜像装载不支持 COPY 选项: " + option)
                flags[flag] = expand(setting)
                value = value.lstrip()
            parts = [expand(part) for part in _arguments(value)]
            if len(parts) < 2:
                raise _error("Dockerfile COPY 缺少来源或目标")
            destination = posixpath.normpath(posixpath.join(cwd, parts[-1]))
            sources = [p for pattern in parts[:-1] for p in _local_sources(context, pattern)]
            if (len(sources) > 1 or len(parts) > 2) and not parts[-1].endswith("/"):
                raise _error("COPY 多个文件时目标必须以 / 结尾")
            for path in sources:
                remote = posixpath.join(remote_context, path.relative_to(context).as_posix())
                if path.is_dir():
                    commands += ["mkdir -p " + shlex.quote(destination),
                                 "cp -a " + shlex.quote(remote + "/.") + " " + shlex.quote(destination + "/")]
                    changed = destination
                else:
                    parent = destination if parts[-1].endswith("/") else posixpath.dirname(destination)
                    commands.append("mkdir -p " + shlex.quote(parent))
                    commands.append("cp -a " + shlex.quote(remote) + " " + shlex.quote(destination + ("/" if parts[-1].endswith("/") else "")))
                    changed = destination + "/" + path.name if parts[-1].endswith("/") else destination
                if "chmod" in flags:
                    if not re.fullmatch(r"[0-7]{3,4}", flags["chmod"]):
                        raise _error("COPY --chmod 必须是八进制权限")
                    commands.append("chmod -R " + flags["chmod"] + " " + shlex.quote(changed))
                if "chown" in flags:
                    commands.append("chown -R " + shlex.quote(flags["chown"]) + " " + shlex.quote(changed))
        elif name == "RUN":
            if value.startswith("--"):
                raise _error("共享镜像装载不支持 RUN 构建挂载选项")
            if value.startswith("["):
                command = _arguments(value)
            elif value.startswith("<<"):
                command = [*shell, "\n".join(value.splitlines()[1:-1])]
            else:
                command = [*shell, value]
            assignments = [key + "=" + val for key, val in {**arguments, **environment}.items()]
            commands.append("(cd " + shlex.quote(cwd) + " && " + shlex.join(["env", *assignments, *command]) + ")")
        elif name == "USER" and value in {"root", "0", "0:0", "root:root"}:
            continue
        elif name in {"LABEL", "CMD", "ENTRYPOINT", "EXPOSE", "STOPSIGNAL"}:
            continue  # Harbor invokes managed scripts rather than the image entrypoint.
        else:
            raise _error("共享镜像装载不支持任务附加指令 " + name + "，不能忽略后继续运行")
    return ContextSetup(commands, cwd, environment, remote_context)


async def install_context(environment, *, verifier: bool) -> None:
    context = environment.environment_dir
    remote = "/tmp/task-builder-context-" + uuid.uuid4().hex
    setup = compile_context(context, remote_context=remote)
    try:
        await environment.upload_dir(context, remote)
        script = "set -eu\n" + "\n".join(setup.commands)
        result = await environment.exec("/bin/sh -c " + shlex.quote(script), cwd="/", timeout_sec=int(environment.task_env_config.build_timeout_sec))
        if result.return_code:
            raise _error("任务文件装配失败: " + (result.stderr or result.stdout or str(result.return_code)))
        environment._package_cwd = setup.cwd
        environment._package_env = setup.environment
        if verifier:
            result = await environment.exec("test -f /tests/test.sh && test -r /tests/test.sh", cwd="/", timeout_sec=60)
            if result.return_code:
                raise _error("评测 Dockerfile 未准备容器入口 /tests/test.sh；源文件可使用任意名称")
    finally:
        await environment.exec("rm -rf -- " + shlex.quote(remote), cwd="/", timeout_sec=60)
