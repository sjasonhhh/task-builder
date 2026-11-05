"""Private entry point inside the authoring sandbox; one source only."""
from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

from .config import load_agent_env
from .models import PipelineConfig, SourceBundle
from .sandbox_transfer import pack_artifacts

ROOT = Path("/workspace/task-builder")
PRIVATE = Path("/run/task-builder")


def load_job(path: Path):
    job = json.loads(path.read_text())
    values = job["config"]
    for name in ("input_root", "output_root", "image_env_path", "environment_dir", "rubric_path", "resume_from"):
        if values.get(name) is not None:
            values[name] = Path(values[name])
    values["author_env"] = load_agent_env(PRIVATE / "model.env", role="author-env")
    config = PipelineConfig(**values)
    source_dir = config.input_root / job["source_name"]
    source = SourceBundle(source_dir, job["slug"], *(source_dir / name for name in ("context", "data", "code")))
    return job, config, source


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, default=PRIVATE / "job.json")
    parser.add_argument("--export", type=Path)
    parser.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--resume-planner-session", help="继续已有模型会话；仅用于本轮尚未完成的方案阶段")
    args = parser.parse_args(argv)
    job, config, source = load_job(args.job)
    run_root = Path(job["run_root"])
    if args.resume_planner_session:
        try:
            uuid.UUID(args.resume_planner_session)
        except ValueError:
            parser.error("方案会话编号必须是 UUID")
        events = run_root / "workspace/sources" / source.source_slug / "logs/events.jsonl"
        if events.is_file():
            for line in events.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "planner-passed":
                    parser.error("方案阶段已经完成，不能按方案会话重新启动本轮任务")
        config.extra["planner_resume_session"] = args.resume_planner_session
    if args.export:
        pack_artifacts(run_root, source.source_slug, args.export, checkpoint=args.checkpoint)
        return 0
    os.environ.update(TASK_BUILDER_SANDBOX_CONFIG=str(PRIVATE / "sandbox.env"),
                      TASK_BUILDER_DIRECT_MODELS="1", TASK_BUILDER_CPU_ONLY="1",
                      TASK_BUILDER_SANDBOX_BATCH=run_root.name,
                      TASK_BUILDER_SANDBOX_SOURCE=source.source_slug,
                      TASK_BUILDER_SANDBOX_PARENT=job["sandbox_id"])
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    from .pipeline import _process_source
    from .workspace import create_shared_workspace
    from .resume import save_run_identity
    from .model_relay import redact_model_secrets
    run_root.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "slug": source.source_slug}
    try:
        create_shared_workspace(run_root, config)
        save_run_identity(run_root, config)
        _process_source(source, config, run_root, run_root / "published")
        result["ok"] = True
    except BaseException as exc:
        message = redact_model_secrets(str(exc), config)
        from .sandbox_runtime import sandbox_settings
        for key, value in sandbox_settings().items():
            if key in {"AD_USERNAME", "AD_PASSWORD"} and value:
                message = message.replace(value, "<redacted>")
        result.update(error=message, stage=getattr(exc, "stage", None))
    temporary = run_root / "worker-result.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    temporary.replace(run_root / "worker-result.json")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
