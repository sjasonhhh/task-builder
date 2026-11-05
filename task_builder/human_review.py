"""人工只编辑 human_review.md；程序保管版本关联、原始意见与修改证据。"""
from __future__ import annotations

import copy
import difflib
import fcntl
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from .integrity import content_manifest, manifest_differences
from .models import StageFailure

REVIEW_FILE = "human_review.md"
PROVENANCE_FILE = ".task-builder.json"
PLACEHOLDER = "（在这里填写；需要程序修改时，将状态改为“需要修改”）"
REVIEW_TEMPLATE = f"""状态：待审核

## 修改意见

{PLACEHOLDER}

## 审核提示

- 科研价值：任务是否有实际科研意义？
- 论文对应：数据、实验条件和指标是否与论文对应？
- 输入充分：必要信息是否齐全，是否泄露答案？
- 评测完整：题目要求的核心能力是否真的被检查？
- 评分公平：错误结果是否扣分，不同正确方法是否被公平评价？
- 验证证据：现有验证能证明什么，还有哪些未验证之处？

状态可选：待审核、通过、需要修改。

这些提示只是帮助专家思考，不用逐项填写，只写发现的问题即可。
"""
TASK_PARTS = ("instruction.md", "README.md", "task.toml", "solution", "tests", "data")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _without_code_caches(manifest: dict) -> dict:
    """运行代码产生的缓存不改变任务；data/ 中的同名内容仍严格核对。"""
    result = {}
    for name, entry in manifest.items():
        parts = Path(name).parts
        if parts and parts[0] in {"tests", "solution"} and any(
            part in {"__pycache__", ".pytest_cache"} for part in parts[1:]
        ):
            continue
        result[name] = entry
    return result


def review_manifest(task: Path) -> dict:
    # 记录人工意见和版本内容，仅忽略自动生成的代码缓存。
    return _without_code_caches(content_manifest(task))


def task_manifest(task: Path) -> dict:
    # 控制文件不属于被评测的任务内容。
    result = {}
    for name in TASK_PARTS:
        root = task / name
        if root.exists():
            for relative, entry in content_manifest(root).items():
                result[name if relative == "." else f"{name}/{relative}"] = entry
    return _without_code_caches(result)


def parse_review(text: str) -> tuple[str, str]:
    text = text.lstrip("\ufeff").strip()
    if text in {"待审核", "通过", "需要修改"}:
        return text, ""
    match = re.search(r"(?:^|\n)\s*(?:#+\s*)?(?:\*\*)?状态(?:\*\*)?\s*[:：]\s*(待审核|通过|需要修改)", text)
    section = re.search(r"(?m)^\s*(?:#+\s*)?修改意见\s*[:：]?\s*$", text)
    body = text[section.end():] if section else text
    opinion = re.split(r"(?m)^\s*#+\s*审核提示", body, maxsplit=1)[0]
    if match and not section:
        opinion = opinion[:match.start()] + opinion[match.end():]
    opinion = opinion.replace(PLACEHOLDER, "").strip()
    # 只有实际填写的意见才触发修复；缺省模板保持待审核。
    status = match.group(1) if match else ("需要修改" if opinion else "待审核")
    return status, opinion


@contextmanager
def publication_lock(output_root: Path, slug: str):
    directory = output_root / ".publication-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{slug}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def select_sources(sources, config, complete: set[str]):
    """只选择新题和人工要求修改的旧题，先保存本轮看到的版本。"""
    requests = {}
    expected = {}
    selected, errors = [], []
    for source in sources:
        target = config.output_root / "final" / source.source_slug
        try:
            with publication_lock(config.output_root, source.source_slug):
                if source.source_slug not in complete:
                    expected[source.source_slug] = None
                    selected.append(source)
                    continue
                marker = target / REVIEW_FILE
                try:
                    with marker.open("x", encoding="utf-8") as handle:
                        handle.write(REVIEW_TEMPLATE)
                except FileExistsError:
                    pass
                raw = marker.read_text(encoding="utf-8")
                status, opinion = parse_review(raw)
                if status != "需要修改":
                    continue
                if config.max_revisions == 0:
                    raise StageFailure("需要修改，但 --max-revisions 为 0；请设为大于 0", stage="human-review")
                baseline = review_manifest(target)
                requests[source.source_slug] = {"raw": raw, "opinion": opinion, "baseline": baseline}
                expected[source.source_slug] = baseline
                selected.append(source)
        except (StageFailure, OSError, UnicodeError) as exc:
            errors.append(f"{source.source_slug}: {exc}")
    config.extra["human_review_requests"] = requests
    config.extra["publication_expected"] = expected
    return selected, errors


def request_for(config, slug: str) -> dict | None:
    if not getattr(config, "human_review", False):
        return None
    return getattr(config, "extra", {}).get("human_review_requests", {}).get(slug)


def _find_run(source, config, target: Path) -> Path:
    """优先使用指定运行、版本记录，再选同名来源最近的可用运行。"""
    metadata = target / PROVENANCE_FILE
    candidates = []
    if config.resume_from:
        candidates = [Path(config.resume_from)]
    if metadata.is_file():
        try:
            record = json.loads(metadata.read_text(encoding="utf-8"))
            candidates.append((config.output_root / record["run"]).resolve())
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    candidates.extend(sorted((config.output_root / "runs").glob("*"), reverse=True))
    for run in dict.fromkeys(candidates):
        old = run / "workspace" / "sources" / source.source_slug
        if not (old / "plan" / "plan.json").is_file():
            continue
        # 没有发布成功的人工修复也可能保留与成品相同的草稿，不能误当成原版本。
        history = old / "reviews" / "human"
        if run != config.resume_from and (history / "request.md").is_file():
            outcome = history / "outcome.json"
            try:
                published = json.loads(outcome.read_text(encoding="utf-8")).get("status") == "pending-human-review"
            except (OSError, ValueError, AttributeError):
                published = False
            if not published:
                continue
        return run
    raise StageFailure("找不到该来源可复用的 plan/plan.json", stage="human-review")


def restore_for_review(source, config, workspace) -> None:
    from .resume import restore_source

    request = request_for(config, source.source_slug)
    target = config.output_root / "final" / source.source_slug
    history = workspace.reviews_dir / "human"
    history.mkdir(parents=True, exist_ok=True)
    (history / "request.md").write_text(request["raw"], encoding="utf-8")
    _write_json(history / "before-manifest.json", request["baseline"])
    run = _find_run(source, config, target)
    restore_config = copy.copy(config)
    restore_config.resume_from = run
    plan_ok, _ = restore_source(source, workspace, restore_config, restore_draft=False)
    if not plan_ok:
        raise StageFailure("原计划无效，无法直接修复；不会重新制定计划", stage="human-review")
    shutil.rmtree(workspace.draft_dir)
    workspace.draft_dir.mkdir()
    for name in TASK_PARTS:
        item = target / name
        if item.is_dir():
            shutil.copytree(item, workspace.draft_dir / name)
        elif item.is_file():
            shutil.copy2(item, workspace.draft_dir / name)
    _write_json(history / "round.json", {"previous_run": str(run), "started": time.time()})
    record_outcome(workspace, "repairing", previous_run=str(run))


def begin_attempt(workspace, cycle: int) -> Path | None:
    history = workspace.reviews_dir / "human"
    if not (history / "request.md").is_file():
        return None
    attempt = history / f"attempt-{cycle:03d}"
    attempt.mkdir(parents=True, exist_ok=True)
    # 避免这一轮在调用模型前失败时，把上一轮的回复误记为本轮回复。
    for log in workspace.logs_dir.glob("repair-writer-agent*.log"):
        log.unlink()
    (workspace.logs_dir / "repair-writer-summary.md").unlink(missing_ok=True)
    _write_json(attempt / "before.json", task_manifest(workspace.draft_dir))
    shutil.copytree(workspace.draft_dir, attempt / "before", ignore=lambda path, names: ["data"] if Path(path) == workspace.draft_dir and "data" in names else [])
    shutil.copy2(workspace.plan_dir / "plan.json", attempt / "plan-before.json")
    return attempt


def finish_attempt(workspace, attempt: Path | None, error: str | None = None) -> None:
    if attempt is None:
        return
    after = task_manifest(workspace.draft_dir)
    before = json.loads((attempt / "before.json").read_text(encoding="utf-8"))
    changed = manifest_differences(before, after)
    _write_json(attempt / "changes.json", {"error": error, "changed_files": changed, "before": before, "after": after})
    shutil.copytree(workspace.draft_dir, attempt / "after", ignore=lambda path, names: ["data"] if Path(path) == workspace.draft_dir and "data" in names else [])
    shutil.copy2(workspace.plan_dir / "plan.json", attempt / "plan-after.json")
    diffs = []
    pairs = [(name, attempt / "before" / name, attempt / "after" / name) for name in changed if not name.startswith("data/")]
    pairs.append(("plan.json", attempt / "plan-before.json", attempt / "plan-after.json"))
    for name, old, new in pairs:
        try:
            if any(p.exists() and (not p.is_file() or p.stat().st_size > 1_000_000) for p in (old, new)):
                continue
            left = old.read_text(encoding="utf-8") if old.exists() else ""
            right = new.read_text(encoding="utf-8") if new.exists() else ""
            if "\x00" not in left + right:
                diffs.extend(difflib.unified_diff(left.splitlines(True), right.splitlines(True), fromfile=f"before/{name}", tofile=f"after/{name}"))
        except UnicodeError:
            continue
    (attempt / "changes.diff").write_text("".join(diffs), encoding="utf-8")
    for log in workspace.logs_dir.glob("repair-writer-agent*.log"):
        shutil.copy2(log, attempt / log.name)
    summary = workspace.logs_dir / "repair-writer-summary.md"
    if summary.is_file():
        shutil.copy2(summary, attempt / "response.md")


def record_outcome(workspace, status: str, **details) -> None:
    history = workspace.reviews_dir / "human"
    if (history / "request.md").is_file():
        event = {"status": status, "time": time.time(), **details}
        _write_json(history / "outcome.json", event)
        with (history / "outcomes.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def write_provenance(staged: Path, run_root: Path, output_root: Path) -> None:
    _write_json(staged / PROVENANCE_FILE, {
        "version": run_root.name,
        "run": os.path.relpath(run_root, output_root),
        "task_manifest": task_manifest(staged),
    })


def publish_staged(staged: Path, config, run_root: Path) -> Path:
    """锁内发布；人工审核的旧版本保留在本轮目录中。"""
    target = config.output_root / "final" / staged.name
    target.parent.mkdir(parents=True, exist_ok=True)
    human = getattr(config, "human_review", False)
    with publication_lock(config.output_root, staged.name):
        expected = config.extra.get("publication_expected", {})
        if config.extra.get("sandbox_dispatch") and staged.name in expected:
            baseline = expected[staged.name]
            if (baseline is None and target.exists()) or (baseline is not None and (not target.is_dir() or review_manifest(target) != baseline)):
                raise StageFailure("运行期间成品或人工意见发生变化，候选结果保留在本轮目录，未覆盖现有成品", stage="publish")
        if human:
            (staged / REVIEW_FILE).write_text(REVIEW_TEMPLATE, encoding="utf-8")
        write_provenance(staged, run_root, config.output_root)
        backup = run_root / f"previous-{staged.name}"
        moved = False
        try:
            if target.exists():
                target.rename(backup)
                moved = True
            staged.rename(target)
        except OSError:
            if moved and backup.exists() and not target.exists():
                backup.rename(target)
            raise
        if backup.exists() and not human:
            shutil.rmtree(backup)
    return target
