from __future__ import annotations

from .models import SourceBundle
from .staging import validate_task_config, validate_task_metadata
from .source_privacy import validate_public_description
from .workspace import SourceWorkspace
from .writer_agent import _check_draft


def validate_local_source(source: SourceBundle, workspace: SourceWorkspace, *, plan_reused: bool, draft_reused: bool) -> dict:
    issues = []
    checks = []
    try:
        _check_draft(workspace.draft_dir)
        validate_task_config(workspace.draft_dir / "task.toml")
        validate_task_metadata(workspace.draft_dir / "task.toml")
        checks.append("staging / writer 检查通过")
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append(str(exc))
    privacy = validate_public_description(workspace.draft_dir, source)
    if not privacy.passed:
        issues.extend(issue.render() for issue in privacy.issues)
    else:
        checks.append("公开题目没有泄露论文来源")
    return {"source": source.source_slug, "passed": not issues, "checks": checks, "issues": issues, "plan_reused": plan_reused, "draft_reused": draft_reused, "remote_validated": False, "scientific_computation_executed": False}
