from __future__ import annotations

import ast
import gzip
import json
import subprocess
import zipfile
from pathlib import Path

from .integrity import content_manifest
from .models import SourceBundle
from .planner_agent import validate_plan
from .staging import validate_task_config
from .source_privacy import validate_public_description
from .workspace import SourceWorkspace


def check_vcf(path: Path) -> int:
    columns = None
    records = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.startswith("##"):
                continue
            if line.startswith("#CHROM"):
                columns = len(line.rstrip("\n").split("\t"))
                continue
            if not columns:
                raise ValueError(f"{path.name}:{line_number}: VCF 缺少样本表头")
            count = line.count("\t") + 1
            if count != columns or not line.endswith("\n"):
                raise ValueError(f"{path.name}:{line_number}: 记录不完整，要求 {columns} 列，实际 {count} 列")
            records += 1
    if not records:
        raise ValueError(f"{path.name}: 没有完整记录")
    return records


def validate_local_source(source: SourceBundle, workspace: SourceWorkspace, *, plan_reused: bool, draft_reused: bool) -> dict:
    issues = []
    checks = []
    plan = workspace.plan_dir / "plan.json"
    try:
        validate_plan(json.loads(plan.read_text(encoding="utf-8")), data_root=workspace.input_dir / "data")
        checks.append("计划严格三字段检查通过")
    except (OSError, ValueError, RuntimeError) as exc:
        issues.append(f"计划检查: {exc}")
    required = ("task.toml", "instruction.md", "solution/solve.sh", "tests/test.sh")
    missing = [name for name in required if not (workspace.draft_dir / name).is_file()]
    issues.extend("缺少草稿: " + name for name in missing)
    if not missing:
        try:
            validate_task_config(workspace.draft_dir / "task.toml")
            checks.append("任务配置检查通过")
        except (RuntimeError, ValueError) as exc:
            issues.append(str(exc))
        privacy = validate_public_description(workspace.draft_dir, source)
        if not privacy.passed:
            issues.extend(issue.render() for issue in privacy.issues)
        else:
            checks.append("公开题目没有泄露论文来源")
    for folder in ("solution", "tests"):
        for path in sorted((workspace.draft_dir / folder).rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8")
            try:
                if path.suffix == ".py":
                    ast.parse(text, filename=str(path))
                else:
                    result = subprocess.run(["bash", "-n", str(path)], text=True, capture_output=True, check=False)
                    if result.returncode:
                        raise ValueError(result.stderr)
                if "/app/data" in text or "/app/out" in text or '"/input"' in text or "'/input'" in text:
                    raise ValueError("仍引用错误输入/输出路径")
                checks.append(f"脚本语法和路径: {path.relative_to(workspace.draft_dir)}")
            except (SyntaxError, ValueError) as exc:
                issues.append(f"{path.relative_to(workspace.draft_dir)}: {exc}")
    manifest = content_manifest(workspace.input_dir / "data")
    (workspace.validation_dir / "local-data-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    checks.append(f"输入文件大小和 SHA-256 指纹已保存，共 {len(manifest)} 项")
    if source.source_slug.startswith("cell2location"):
        annotation = workspace.input_dir / "data" / "E-MTAB-11115" / "cell_annotation.csv"
        if annotation.is_file() and annotation.stat().st_size:
            checks.append("真实细胞标注文件存在: E-MTAB-11115/cell_annotation.csv")
        else:
            issues.append("真实细胞标注文件缺失")
    if source.source_slug == "cellbender-remove-background":
        assets = workspace.draft_dir / "tests" / "assets"
        expected = ("ref_denoised.npz", "ref_raw_cells.npz", "ref_cell_barcodes.csv", "ref_ambient.npz", "genes.csv", "ref_metrics.json", "barcodes_input.csv.gz")
        for name in expected:
            path = assets / name
            try:
                if not path.is_file() or not path.stat().st_size:
                    raise ValueError("参考包缺失或为空")
                if path.suffix == ".npz":
                    with zipfile.ZipFile(path) as archive:
                        bad = archive.testzip()
                        if bad:
                            raise ValueError("压缩包损坏: " + bad)
                elif path.suffix == ".gz":
                    with gzip.open(path, "rb") as handle:
                        while handle.read(1024 * 1024):
                            pass
                elif path.suffix == ".json":
                    json.loads(path.read_text(encoding="utf-8"))
                checks.append("参考包完整性: " + name)
            except (OSError, ValueError, zipfile.BadZipFile, EOFError) as exc:
                issues.append(f"{name}: {exc}")
        (workspace.validation_dir / "reference-manifest.json").write_text(json.dumps(content_manifest(assets), ensure_ascii=False, indent=2), encoding="utf-8")
    if source.source_slug.startswith("pyseer"):
        vcf = workspace.input_dir / "data" / "snps.vcf"
        if vcf.is_file():
            try:
                records = check_vcf(vcf)
                checks.append(f"VCF 所有记录列数及末行完整，共 {records} 行")
            except (OSError, ValueError) as exc:
                issues.append(str(exc))
    return {"source": source.source_slug, "passed": not issues, "checks": checks, "issues": issues, "plan_reused": plan_reused, "draft_reused": draft_reused, "remote_validated": False, "scientific_computation_executed": False}
