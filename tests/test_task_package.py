from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

from task_builder.integrity import content_manifest
from task_builder.models import EnvConfig, PipelineConfig, SourceBundle, StageFailure
from task_builder.staging import assemble_task_package, finalize_harbor_task, prepare_harbor_task, publish_task
from task_builder.task_package import compose_context, compile_context, install_context, is_standard_package, public_data
from task_builder.task_runner import SelectedTask, _identity, _task_manifest, prepare_task
from task_builder.writer_agent import _check_draft


@pytest.fixture
def specimen(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    for kind, cwd in (("experiment", "/workspace"), ("evaluation", "/tests")):
        (shared / f"requirements-{kind}.txt").write_text("# fixed shared dependencies\n")
        (shared / f"Dockerfile.{kind}").write_text(
            f"FROM domain-{kind}:fixed\nCOPY requirements-{kind}.txt /tmp/requirements.txt\n"
            f"RUN printf shared > /installed\nWORKDIR {cwd}\n")
    source = tmp_path / "source"
    for name in ("data", "context", "code"):
        (source / name).mkdir(parents=True)
    (source / "data/observations.csv").write_text("x\n1\n2\n")
    bundle = SourceBundle(source, "sample", source / "context", source / "data", source / "code")
    workspace = tmp_path / "workspace"
    draft = workspace / "draft"
    for name in ("data", "tests/tools", "solution"):
        (draft / name).mkdir(parents=True)
    (workspace / "plan").mkdir()
    (workspace / "plan/plan.json").write_text(json.dumps({"data_split": {"files": [
        {"path": "observations.csv", "role": "solver_input", "reason": "raw observations"}]}}))
    (draft / "data/observations.csv").write_bytes((source / "data/observations.csv").read_bytes())
    (draft / "task.toml").write_text('artifacts = ["/app/results/answer.txt"]\n[task]\nname="example/measurement"\n[metadata]\ndomain="physics"\n')
    (draft / "instruction.md").write_text("Estimate a measurement from the observations.\n")
    (draft / "README.md").write_text("harbor run --path . --agent oracle\n")
    (draft / "solution/solve.sh").write_text("#!/bin/sh\nmkdir -p /app/results\nprintf 2 > /app/results/answer.txt\n")
    (draft / "tests/tools/assess.py").write_text("print('assessment')\n")
    (draft / "tests/launch evaluation.sh").write_text("#!/bin/sh\npython3 /checks/assess.py\n")
    (draft / "tests/Dockerfile").write_text(
        'FROM evaluation:fixed\nCOPY ["launch evaluation.sh", "/tests/test.sh"]\n'
        'COPY tools/ /checks/\nRUN chmod 755 /tests/test.sh\n')
    config = PipelineConfig(source.parent, tmp_path / "output", EnvConfig(tmp_path / "a.env", "test", {}),
                            "experiment:fixed", "evaluation:fixed", shared / "image-env.toml", environment_dir=shared)
    return SimpleNamespace(config=config, draft=draft, source=bundle, workspace=workspace, shared=shared)


def test_standard_package_has_no_implicit_verifier_input_and_keeps_arbitrary_layout(specimen, tmp_path):
    s = specimen
    _check_draft(s.draft)  # No source file named tests/test.sh is required.
    before = content_manifest(s.draft)
    package = assemble_task_package(s.draft, s.source, tmp_path / "package", s.config)
    assert is_standard_package(package)
    assert not (package / "data").exists()
    assert (public_data(package) / "observations.csv").read_text() == "x\n1\n2\n"
    assert not (package / "tests/test.sh").exists()
    assert not list((package / "tests").rglob("observations.csv"))
    assert not (package / "tests/input-data").exists()
    assert (package / "tests/launch evaluation.sh").is_file()
    assert (package / "tests/requirements-evaluation.txt").is_file()
    task = tomllib.loads((package / "task.toml").read_text())
    assert task["verifier"]["environment_mode"] == "separate"
    assert "docker_image" not in task.get("environment", {})
    setup = compile_context(package / "tests")
    assert any("/tests/test.sh" in command for command in setup.commands)
    assert not any("/workspace/data" in command or "/installed" in command for command in setup.commands)
    assert content_manifest(s.draft) == before


def test_verifier_can_include_only_the_data_it_uses(specimen, tmp_path):
    s = specimen
    (s.draft / "tests/probe.csv").write_text("x\n3\n")
    with (s.draft / "tests/Dockerfile").open("a") as f:
        f.write("COPY probe.csv /evaluation/held-out.csv\n")
    package = assemble_task_package(s.draft, s.source, tmp_path / "package", s.config)
    setup = compile_context(package / "tests")
    assert any("/evaluation/held-out.csv" in command for command in setup.commands)
    assert not list((package / "tests").rglob("observations.csv"))


def test_build_prefix_is_not_executed_on_shared_image_and_heredoc_is_preserved(specimen, tmp_path):
    s = specimen
    (s.draft / "tests/Dockerfile").write_text(
        "FROM evaluation:fixed\nENV OUT=/tests\nENV OUT=/unused PREVIOUS=${OUT}\nWORKDIR /checks\n"
        "RUN <<'SCRIPT'\nmkdir -p /tests\nprintf '#!/bin/sh\\necho grade\\n' > /tests/test.sh\nSCRIPT\n"
        "COPY tools/ ${PREVIOUS}/code/\n")
    package = assemble_task_package(s.draft, s.source, tmp_path / "package", s.config)
    setup = compile_context(package / "tests")
    assert setup.cwd == "/checks" and setup.environment == {"OUT": "/unused", "PREVIOUS": "/tests"}
    assert any("echo grade" in command for command in setup.commands)
    assert any("/tests/code/" in command for command in setup.commands)
    assert all("/installed" not in command for command in setup.commands)


@pytest.mark.parametrize("instruction", ["COPY ../secret /tests/secret", "COPY missing /tests/missing", "RUN --mount=type=secret cat /secret"])
def test_invalid_or_unhandled_build_steps_fail_before_remote_submission(specimen, tmp_path, instruction):
    s = specimen
    with (s.draft / "tests/Dockerfile").open("a") as f:
        f.write(instruction + "\n")
    with pytest.raises(StageFailure):
        assemble_task_package(s.draft, s.source, tmp_path / "package", s.config)


def test_new_runner_copies_environment_and_fingerprint_tracks_dockerfile(specimen, tmp_path):
    s = specimen
    package = assemble_task_package(s.draft, s.source, tmp_path / "package", s.config)
    before = content_manifest(package)
    manifest = _task_manifest(package)
    identity = _identity(manifest, s.config)
    selected = SelectedTask("sample", package, manifest, identity)
    prepared = prepare_task(selected, tmp_path / "run-copy", s.config)
    assert (prepared / "environment/data/observations.csv").is_file()
    assert not (prepared / "data").exists()
    assert tomllib.loads((prepared / "task.toml").read_text())["environment"]["docker_image"] == s.config.experiment_image
    assert content_manifest(package) == before
    with (package / "tests/Dockerfile").open("a") as f:
        f.write("RUN mkdir -p /new-directory\n")
    assert _identity(_task_manifest(package), s.config) != identity


def test_legacy_runner_preserves_original_package_and_input_behavior(specimen, tmp_path):
    s = specimen
    (s.draft / "tests/Dockerfile").unlink()
    (s.draft / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    before = content_manifest(s.draft)
    selected = SelectedTask("legacy", s.draft, _task_manifest(s.draft), "legacy")
    prepared = prepare_task(selected, tmp_path / "legacy-copy", s.config)
    assert (prepared / "data/observations.csv").is_file()
    assert (prepared / "environment/data/observations.csv").is_file()
    assert not is_standard_package(prepared)
    assert content_manifest(s.draft) == before


def test_publish_uses_validated_portable_package_and_rejects_late_changes(specimen, tmp_path):
    s = specimen
    validation = s.workspace / "validation/harbor-task"
    prepare_harbor_task(s.draft, s.source, validation, s.config)
    expected = content_manifest(s.workspace / "validation/package")
    final = publish_task(s.draft, s.source, tmp_path / "final", s.config)
    assert content_manifest(final) == expected
    (s.draft / "tests/tools/assess.py").write_text("changed after validation\n")
    with pytest.raises(StageFailure, match="草稿已变化"):
        publish_task(s.draft, s.source, tmp_path / "final", s.config)
    assert content_manifest(final) == expected


def test_runtime_uses_dockerfile_setup_and_cleans_context(specimen, tmp_path):
    s = specimen
    package = assemble_task_package(s.draft, s.source, tmp_path / "package", s.config)
    class Environment:
        environment_dir = package / "tests"
        task_env_config = SimpleNamespace(build_timeout_sec=30)
        def __init__(self):
            self.uploads, self.commands = [], []
        async def upload_dir(self, source, remote):
            self.uploads.append((source, remote))
        async def exec(self, command, **kwargs):
            self.commands.append(command)
            return SimpleNamespace(return_code=0, stdout="", stderr="")
    environment = Environment()
    asyncio.run(install_context(environment, verifier=True))
    assert [p for p, _ in environment.uploads] == [package / "tests"]
    assert "/tests/test.sh" in environment.commands[0]
    assert environment.commands[-1].startswith("rm -rf -- /tmp/task-builder-context-")
    assert environment._package_cwd == "/tests"
