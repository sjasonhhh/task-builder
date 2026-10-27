from __future__ import annotations

import json
from pathlib import Path
import copy

from .agents import IndependentClaudeAgent, extract_json, save_agent_log
from .models import AgentError, PipelineConfig, SourceBundle, StageFailure, TaskTimeout
from .integrity import normalize_data_path, unresolved_data_paths
from .prompting import load_prompt, read_shared_material, source_manifest
from .workspace import SourceWorkspace, directory_manifest, directories_unchanged, record_event

_PLAN_FIELDS = ("problem", "data_split", "verifier_metrics")
_METRIC_FIELDS = ("name", "source", "definition", "paper_value", "dataset", "experiment", "direction", "starter", "sota")
_DATA_ROLES = ("solver_input", "author_result", "evaluation_reference")


def validate_plan(plan: object, *, data_root: Path | None = None) -> dict:
    """校验任务计划；指定 data_root 时同时核对数据文件是否存在。"""
    if not isinstance(plan, dict):
        raise StageFailure("plan/plan.json 顶层必须是 JSON 对象", stage="planner")
    missing = [field for field in _PLAN_FIELDS if field not in plan]
    extra = [field for field in plan if field not in _PLAN_FIELDS]
    if missing or extra:
        raise StageFailure(
            "plan/plan.json 顶层必须恰好是 " + ", ".join(_PLAN_FIELDS)
            + ("；缺少: " + ", ".join(missing) if missing else "") + ("；多出: " + ", ".join(extra) if extra else ""),
            stage="planner",
        )
    empty = [field for field in _PLAN_FIELDS if not isinstance(plan[field], dict) or not plan[field]]
    if empty:
        raise StageFailure("plan/plan.json 的以下字段必须是非空 JSON 对象: " + ", ".join(empty), stage="planner")
    files = plan["data_split"].get("files")
    if not isinstance(files, list) or not files:
        raise StageFailure("plan/plan.json 的 data_split.files 必须是非空列表", stage="planner")
    paths: list[str] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not str(item.get("path", "")).strip() or not str(item.get("reason", "")).strip():
            raise StageFailure(f"data_split.files[{index}] 必须写 path 与 reason", stage="planner")
        if item.get("role") not in _DATA_ROLES:
            raise StageFailure(f"data_split.files[{index}] 的 role 必须是 " + "、".join(_DATA_ROLES), stage="planner")
        path = normalize_data_path(item["path"], data_root)
        if not path or ".." in path.split("/"):
            raise StageFailure(f"data_split.files[{index}] 的 path 必须是来源 data/ 内的相对路径", stage="planner")
        paths.append(path)
    duplicated = sorted({path for path in paths if paths.count(path) > 1})
    if duplicated:
        raise StageFailure("data_split.files 中的 path 重复: " + ", ".join(duplicated), stage="planner")
    if not any(item["role"] == "solver_input" for item in files):
        raise StageFailure("data_split.files 至少要有一项 solver_input", stage="planner")
    if data_root is not None:
        missing = unresolved_data_paths(plan, data_root)
        if missing:
            raise StageFailure(
                "data_split 中以下路径在来源数据目录中不存在，请按来源文件清单改成相对 data/ 的真实路径（可用 * 通配）: "
                + ", ".join(missing[:20]) + (f" 等共 {len(missing)} 项" if len(missing) > 20 else ""),
                stage="planner",
            )
    experiments = plan["verifier_metrics"].get("experiments")
    if not isinstance(experiments, list) or not experiments:
        raise StageFailure("plan/plan.json 的 verifier_metrics.experiments 必须是非空列表", stage="planner")
    tiers: dict[str, str] = {}
    for index, item in enumerate(experiments):
        if not isinstance(item, dict) or not str(item.get("name", "")).strip() or not str(item.get("reason", "")).strip():
            raise StageFailure(f"verifier_metrics.experiments[{index}] 必须写 name 与 reason", stage="planner")
        if item.get("tier") not in ("primary", "secondary"):
            raise StageFailure(f"verifier_metrics.experiments[{index}] 的 tier 必须是 primary 或 secondary", stage="planner")
        if item["name"] in tiers:
            raise StageFailure("verifier_metrics.experiments 中的实验名重复: " + str(item["name"]), stage="planner")
        tiers[item["name"]] = item["tier"]
    if "primary" not in tiers.values():
        raise StageFailure("verifier_metrics.experiments 至少要有一个 primary 实验", stage="planner")
    _validate_metrics(plan["verifier_metrics"].get("metrics"), fields=_METRIC_FIELDS)
    used: set[str] = set()
    for index, item in enumerate(plan["verifier_metrics"]["metrics"]):
        if item["experiment"] not in tiers:
            raise StageFailure(f"verifier_metrics.metrics[{index}] 的 experiment 不在 experiments 中: {item['experiment']}", stage="planner")
        used.add(item["experiment"])
        if item["direction"] not in ("higher", "lower"):
            raise StageFailure(f"verifier_metrics.metrics[{index}] 的 direction 必须是 higher 或 lower", stage="planner")
        if "bound" not in item:
            raise StageFailure(f"verifier_metrics.metrics[{index}] 必须写 bound（没有则为 null）", stage="planner")
        sota = item["sota"]
        if not isinstance(sota, dict) or not isinstance(sota.get("value"), (int, float)) or isinstance(sota.get("value"), bool) or not str(sota.get("derivation", "")).strip():
            raise StageFailure(f"verifier_metrics.metrics[{index}] 的 sota 必须是含数值 value 与 derivation 的对象", stage="planner")
    unused = sorted(set(tiers) - used)
    if unused:
        raise StageFailure("verifier_metrics.experiments 中以下实验没有计分指标: " + ", ".join(unused), stage="planner")
    return plan


def _validate_metrics(metrics: object, *, fields: tuple[str, ...] = _METRIC_FIELDS) -> None:
    if not isinstance(metrics, list) or not metrics:
        raise StageFailure("plan/plan.json 的 verifier_metrics.metrics 必须是非空列表", stage="planner")
    names: list[str] = []
    for index, item in enumerate(metrics):
        if not isinstance(item, dict):
            raise StageFailure(f"verifier_metrics.metrics[{index}] 必须是 JSON 对象", stage="planner")
        missing = [key for key in fields if item.get(key) in (None, "")]
        if missing:
            raise StageFailure(f"verifier_metrics.metrics[{index}] 缺少字段: " + ", ".join(missing), stage="planner")
        if "paper_interval" not in item:
            raise StageFailure(f"verifier_metrics.metrics[{index}] 必须写 paper_interval（没有则为 null）", stage="planner")
        names.append(str(item["name"]))
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise StageFailure("verifier_metrics.metrics 中的指标名重复: " + ", ".join(duplicated), stage="planner")


def migrate_plan_meta(plan: object, *, data_root: Path | None = None) -> dict:
    if not isinstance(plan, dict):
        return validate_plan(plan, data_root=data_root)
    migrated = copy.deepcopy(plan)
    if "meta" in migrated:
        metadata = migrated.pop("meta")
        problem = migrated.get("problem")
        if not isinstance(problem, dict):
            return validate_plan(migrated, data_root=data_root)
        existing = problem.get("execution_constraints")
        if existing is None:
            problem["execution_constraints"] = metadata
        elif isinstance(existing, dict) and isinstance(metadata, dict) and not (existing.keys() & metadata.keys()):
            problem["execution_constraints"] = {**existing, **metadata}
        else:
            problem["execution_constraints"] = {"existing_constraints": existing, "legacy_meta": metadata}
    return validate_plan(migrated, data_root=data_root)


def _format_fix_prompt(plan_path: Path, format_error: str) -> str:
    return (
        f"{plan_path} 未通过格式校验：{format_error}。不要重新阅读材料，不要改动科学内容；"
        "直接编辑该文件补全或修正对应字段后保存。原文没有报告数值的指标不能用于评分，"
        "从 verifier_metrics.metrics 中删除，其余指标保持不变。"
    )


def _planner_attempt(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent, *, attempt: int, format_error: str, resume_session: str | None = None) -> Path:
    plan_path = workspace.plan_dir / "plan.json"
    rubric, environment_dir, environment = read_shared_material(workspace.shared_dir)
    prompt = _format_fix_prompt(plan_path, format_error) if resume_session else load_prompt(
        "planner.md",
        plan=plan_path,
        context=workspace.input_dir / "context",
        data=workspace.input_dir / "data",
        code=workspace.input_dir / "code",
        rubric=rubric,
        environment=environment,
        environment_dir=workspace.shared_dir / environment_dir,
        source_manifest=source_manifest(workspace.input_dir),
    )
    if format_error and not resume_session:
        fields = "problem、data_split、verifier_metrics"
        prompt += f"\n仅修复已有计划格式，不修改科学要求。上次错误：{format_error}。顶层必须恰好是 {fields} 三个字段。已有 meta 的全部内容移入 problem.execution_constraints，不能丢失。"
    protected = {
        workspace.input_dir: directory_manifest(workspace.input_dir),
        workspace.shared_dir: directory_manifest(workspace.shared_dir),
    }
    try:
        run = agent.run(
            role=f"planner-format-{attempt}" if attempt else "planner",
            prompt=prompt,
            workdir=workspace.plan_dir,
            env_config=config.author_env,
            allowed_dirs=(workspace.input_dir, workspace.shared_dir),
            read_only_dirs=(workspace.input_dir, workspace.shared_dir),
            resume_session=resume_session,
        )
        save_agent_log(workspace.logs_dir / f"planner-attempt-{attempt:03d}.log", run)
    except AgentError as exc:
        if hasattr(exc, "run"):
            save_agent_log(workspace.logs_dir / f"planner-attempt-{attempt:03d}.log", exc.run)
        raise StageFailure(str(exc), stage="planner") from exc
    try:
        return _check_plan_output(workspace, protected, run.stdout)
    except StageFailure as exc:
        exc.session_id = run.session_id
        raise


def _check_plan_output(workspace: SourceWorkspace, protected: dict, stdout: str) -> Path:
    plan_path = workspace.plan_dir / "plan.json"
    if not directories_unchanged(protected):
        raise StageFailure("Planner Agent 修改了只读输入或共享资料", stage="planner")
    unexpected = [path.name for path in workspace.plan_dir.iterdir() if path.name != "plan.json"]
    if unexpected:
        raise StageFailure("Planner Agent 生成了 plan.json 之外的文件: " + ", ".join(sorted(unexpected)), stage="planner")
    if not plan_path.exists():
        try:
            plan_from_output = extract_json(stdout)
        except AgentError:
            plan_from_output = None
        if isinstance(plan_from_output, dict):
            plan_path.write_text(json.dumps(plan_from_output, ensure_ascii=False, indent=2), encoding="utf-8")
    if not plan_path.is_file() or plan_path.is_symlink():
        raise StageFailure("Planner Agent 未生成 plan/plan.json", stage="planner")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageFailure(f"plan/plan.json 不是有效 JSON: {exc}", stage="planner") from exc
    validate_plan(plan, data_root=workspace.input_dir / "data")
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan_path


def run_planner_agent(source: SourceBundle, config: PipelineConfig, workspace: SourceWorkspace, agent: IndependentClaudeAgent) -> Path:
    # 修复次数为 0 也必须执行一次初始 Planner；max_revisions=N 时最多 N+1 次尝试。
    attempt_limit = max(1, config.max_revisions + 1)
    format_error = ""
    resume_session = None
    for attempt in range(attempt_limit):
        try:
            return _planner_attempt(source, config, workspace, agent, attempt=attempt, format_error=format_error, resume_session=resume_session)
        except TaskTimeout:
            raise
        except StageFailure as exc:
            path = workspace.plan_dir / "plan.json"
            if path.is_file():
                (workspace.failures_dir / f"planner-attempt-{attempt:03d}.json").write_bytes(path.read_bytes())
            (workspace.failures_dir / f"planner-attempt-{attempt:03d}.txt").write_text(str(exc), encoding="utf-8")
            last_attempt = attempt == attempt_limit - 1
            record_event(
                workspace,
                "planner-failed",
                attempt=attempt,
                attempt_limit=attempt_limit,
                last_attempt=last_attempt,
                error=str(exc),
            )
            if exc.__cause__ is not None and isinstance(exc.__cause__, AgentError):
                raise
            if "只读" in str(exc) or "之外的文件" in str(exc) or last_attempt:
                raise
            format_error = str(exc)
            # plan.json 已写出、只是字段不合格时，在原会话里就地修正，不必重读全部材料。
            resume_session = getattr(exc, "session_id", None) if path.is_file() else None
    raise AssertionError("unreachable")
