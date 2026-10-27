"""造任务的 token 用量与估算成本。

每次 agent 调用的用量由 agents.save_agent_log 追加到来源工作区的 logs/usage.jsonl；
续跑时旧运行的 logs/ 复制在 previous-evidence/logs/（可多层嵌套），一并计入。
发布时写 <output>/usage/<来源>.json；`python -m task_builder.usage --output <dir>` 汇总 final/ 下全部任务。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from .paths import PROJECT_PRICING

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def load_pricing(path: Path = PROJECT_PRICING) -> dict[str, dict[str, float]]:
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _price(pricing: dict, model: str | None) -> dict[str, float] | None:
    return pricing.get(model or "") or pricing.get("default")


def entry_cost(entry: dict, pricing: dict) -> float | None:
    price = _price(pricing, entry.get("model"))
    if not price:
        return None
    per_million = (
        entry.get("input_tokens", 0) * price.get("input", 0)
        + entry.get("cache_creation_input_tokens", 0) * price.get("cache_creation", price.get("input", 0))
        + entry.get("cache_read_input_tokens", 0) * price.get("cache_read", price.get("input", 0))
        + entry.get("output_tokens", 0) * price.get("output", 0)
    )
    return per_million / 1_000_000


def usage_files(source_root: Path) -> list[Path]:
    """当前运行与续跑带来的全部 usage.jsonl（logs/ 与任意层级 previous-evidence/logs/）。"""
    return sorted(path for path in source_root.rglob("usage.jsonl") if path.parent.name == "logs")


def read_entries(paths: list[Path]) -> list[dict]:
    entries, seen = [], set()
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            # 续跑会复制旧日志，同一条记录可能出现在多层 previous-evidence 中。
            key = (entry.get("session_id"), entry.get("role"), entry.get("time"))
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    return entries


def _stage(role: str) -> str:
    for prefix in ("planner", "writer", "repair-writer", "reviewer"):
        if role.startswith(prefix):
            return "repair" if prefix == "repair-writer" else prefix
    return "other"


def summarize(entries: list[dict], pricing: dict) -> dict:
    totals, stages = Counter(), {}
    cost, cost_known = 0.0, True
    models = Counter()
    for entry in entries:
        stage = stages.setdefault(_stage(entry.get("role", "")), Counter())
        for key in TOKEN_KEYS + ("num_turns", "duration_ms", "duration_api_ms"):
            value = int(entry.get(key) or 0)
            totals[key] += value
            stage[key] += value
        stage["calls"] += 1
        totals["calls"] += 1
        models[entry.get("model") or "unknown"] += 1
        value = entry_cost(entry, pricing)
        if value is None:
            cost_known = False
        else:
            cost += value
            stage["cost_usd_micro"] += round(value * 1_000_000)
    total_tokens = sum(totals[key] for key in TOKEN_KEYS)
    return {
        "total_tokens": total_tokens,
        **{key: totals[key] for key in TOKEN_KEYS},
        "calls": totals["calls"],
        "num_turns": totals["num_turns"],
        "duration_api_hours": round(totals["duration_api_ms"] / 3_600_000, 2),
        "estimated_cost_usd": round(cost, 4) if cost_known else None,
        "models": dict(models),
        "stages": {
            name: {
                "total_tokens": sum(values[key] for key in TOKEN_KEYS),
                **{key: values[key] for key in TOKEN_KEYS},
                "calls": values["calls"],
                "estimated_cost_usd": round(values["cost_usd_micro"] / 1_000_000, 4),
            }
            for name, values in sorted(stages.items())
        },
    }


def write_source_usage(source_root: Path, output_root: Path, slug: str, pricing: dict | None = None) -> Path:
    """发布时记录该来源（含续跑历史）的用量汇总到 <output>/usage/<slug>.json。"""
    pricing = load_pricing() if pricing is None else pricing
    summary = summarize(read_entries(usage_files(source_root)), pricing)
    summary.update(source=slug, workspace=str(source_root))
    target = output_root / "usage" / f"{slug}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def report(output_root: Path) -> list[dict]:
    final_root = output_root / "final"
    rows = []
    for task in sorted(final_root.iterdir()) if final_root.is_dir() else []:
        if not task.is_dir():
            continue
        path = output_root / "usage" / f"{task.name}.json"
        rows.append(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"source": task.name, "missing": True})
    return rows


def _format_table(rows: list[dict]) -> str:
    header = f"{'task':40} {'total_tokens':>14} {'input':>13} {'output':>11} {'cache_read':>13} {'calls':>6} {'api_h':>6} {'cost_usd':>10}"
    lines = [header, "-" * len(header)]
    total_tokens = total_cost = 0
    for row in rows:
        if row.get("missing"):
            lines.append(f"{row['source']:40} {'(无用量记录)':>14}")
            continue
        cost = row.get("estimated_cost_usd")
        lines.append(
            f"{row['source'][:40]:40} {row['total_tokens']:>14,} {row['input_tokens']:>13,} {row['output_tokens']:>11,} "
            f"{row['cache_read_input_tokens']:>13,} {row['calls']:>6} {row['duration_api_hours']:>6} {cost if cost is not None else '-':>10}"
        )
        total_tokens += row["total_tokens"]
        total_cost += cost or 0
    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':40} {total_tokens:>14,} {'':>13} {'':>11} {'':>13} {'':>6} {'':>6} {round(total_cost, 4):>10}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 final/ 下任务的 token 用量与估算成本")
    parser.add_argument("--output", required=True, help="环境这一层的输出目录：<output>/<sci-env>")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    args = parser.parse_args(argv)
    rows = report(Path(args.output).expanduser().resolve())
    print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else _format_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
