"""Versioned usage reports. Raw ledgers are immutable; reports are estimates, not bills."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from .paths import PROJECT_PRICING

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
RATE_KEYS = dict(zip(TOKEN_KEYS, ("input", "output", "cache_read", "cache_creation")))


def load_pricing(path: Path = PROJECT_PRICING) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def price_snapshot(pricing: dict, model: str | None) -> dict | None:
    # A default rate cannot identify either the supplier's price or its currency.
    price = pricing.get("models", {}).get(model or "")
    if not isinstance(price, dict) or not isinstance(price.get("currency"), str) or len(price["currency"]) != 3:
        return None
    try:
        rates = {key: str(Decimal(str(price[key]))) for key in RATE_KEYS.values() if key in price}
        if not {"input", "output"} <= rates.keys() or any(not Decimal(v).is_finite() or Decimal(v) < 0 for v in rates.values()):
            return None
    except (InvalidOperation, ValueError, TypeError):
        return None
    return {"currency": price["currency"].upper(), **rates, "unit": "per_million_tokens"}


def _priced_parts(entry: dict, pricing: dict, recalculate: bool = False) -> tuple[dict[str, Decimal], bool]:
    costs: dict[str, Decimal] = defaultdict(Decimal)
    known = True
    parts = entry.get("requests") or [entry]
    for part in parts:
        price = (price_snapshot(pricing, part.get("model")) if recalculate or "pricing_snapshot" not in part
                 else part["pricing_snapshot"])
        if not price or part.get("usage_valid") is False or not any(k in part for k in TOKEN_KEYS):
            known = False
            continue
        subtotal = Decimal(0)
        for key, rate_key in RATE_KEYS.items():
            count = int(part.get(key) or 0)
            if count and rate_key not in price:
                known = False
            else:
                subtotal += Decimal(count) * Decimal(str(price.get(rate_key, 0))) / Decimal(1_000_000)
        costs[price["currency"]] += subtotal
    return dict(costs), known


def entry_cost(entry: dict, pricing: dict) -> float | None:
    values, known = _priced_parts(entry, pricing)
    return float(next(iter(values.values()))) if known and len(values) == 1 and entry.get("usage_complete") is not False else None


def usage_files(source_root: Path) -> list[Path]:
    """Only accounting files in the current source and its explicit resume lineage."""
    paths = []
    root = source_root
    while root.is_dir():
        paths.extend(p for name in ("usage-events.jsonl", "usage.jsonl") if (p := root / "logs" / name).is_file())
        root = root / "previous-evidence"
    return paths


def read_entries(paths: list[Path]) -> list[dict]:
    legacy, invocations, seen = [], {}, set()
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("not an object")
            except ValueError:
                legacy.append({"role": "unknown", "time": 0, "usage_complete": False, "record_error": "invalid_json",
                               "_path": str(path), "_line": line_number})
                continue
            identity = json.dumps(row, sort_keys=True, separators=(",", ":"))
            if identity in seen:
                continue
            seen.add(identity)
            if row.get("schema_version") == 2 and row.get("invocation_id") and row.get("kind"):
                group = invocations.setdefault(row["invocation_id"], {"requests": {}, "_path": str(path)})
                if row["kind"] == "request":
                    previous = group["requests"].get(row["request_id"], {})
                    if row.get("time", 0) >= previous.get("time", 0):
                        group["requests"][row["request_id"]] = row
                else:
                    group[row["kind"]] = row
            else:
                legacy.append({**row, "_path": str(path)})
    entries = []
    response_owners = set()
    for identifier, group in sorted(invocations.items(), key=lambda item: item[1].get("start", {}).get("time", 0)):
        start, end = group.get("start", {}), group.get("end", {})
        requests = []
        for request in group["requests"].values():
            key = (start.get("provider_id"), request.get("model"), request["request_id"])
            if key not in response_owners:
                response_owners.add(key)
                requests.append(request)
        entry = {**start, **end, "invocation_id": identifier, "_path": group["_path"], "requests": requests}
        if requests:
            entry.update({k: sum(int(r.get(k) or 0) for r in requests) for k in TOKEN_KEYS})
            entry["api_requests"] = len(requests)
            entry["usage_valid"] = all(r.get("usage_valid", False) for r in requests)
        elif not group["requests"] and end.get("fallback_usage"):
            entry.update(end["fallback_usage"])
            entry["api_requests"] = None
        elif group["requests"]:  # Replayed responses from a resumed CLI, already counted.
            entry.update({k: 0 for k in TOKEN_KEYS})
            entry["api_requests"] = 0
        else:
            entry["api_requests"] = None
        entry["usage_complete"] = bool(start and end.get("usage_complete"))
        entry["accounting_basis"] = "recorded_protocol" if requests else "cli_aggregate"
        entries.append(entry)
    for entry in legacy:
        if entry.get("captured_invocations") and set(entry["captured_invocations"]) <= invocations.keys():
            continue  # The compatible end-of-call row is not another paid request.
        entry.setdefault("accounting_basis", "legacy_unverified")
        entry.setdefault("api_requests", None)
        entries.append(entry)
    entries.sort(key=lambda row: row.get("time", 0))
    # Claude's API duration is cumulative across --resume; tokens are per invocation.
    session_api = {}
    for entry in entries:
        session = entry.get("session_id")
        raw = entry.get("duration_api_reported_ms")
        if raw is None and entry.get("accounting_basis") == "legacy_unverified" and entry.get("agent") != "codex":
            raw = entry.get("duration_api_ms")
        if raw is None or not session:
            continue
        previous = session_api.get(session)
        entry["duration_api_reported_ms"] = raw
        if previous is None and entry.get("resumed"):
            entry["duration_api_ms"] = None
            entry["duration_api_status"] = "missing_resume_baseline"
        elif previous is not None and raw < previous:
            entry["duration_api_ms"] = None
            entry["duration_api_status"] = "counter_reset"
        else:
            entry["duration_api_ms"] = raw - (previous or 0)
        session_api[session] = raw
    return entries


def _stage(role: str) -> str:
    for prefix in ("planner", "writer", "repair-writer", "reviewer"):
        if role.startswith(prefix):
            return "repair" if prefix == "repair-writer" else prefix
    return "other"


def summarize(entries: list[dict], pricing: dict, *, recalculate: bool = False, _stages: bool = True) -> dict:
    totals = Counter()
    costs: dict[str, Decimal] = defaultdict(Decimal)
    missing_price = incomplete = legacy = 0
    models = Counter()
    for entry in entries:
        totals.update({k: int(entry.get(k) or 0) for k in TOKEN_KEYS + ("num_turns", "duration_ms", "duration_api_ms")})
        parts, known = _priced_parts(entry, pricing, recalculate)
        for currency, value in parts.items():
            costs[currency] += value
        missing_price += not known
        incomplete += entry.get("usage_complete") is False or entry.get("usage_valid") is False
        legacy += entry.get("accounting_basis", "legacy_unverified") == "legacy_unverified"
        for part in entry.get("requests") or [entry]:
            models[part.get("model") or "unknown"] += 1
    amounts = {k: float(v.quantize(Decimal("0.0001"))) for k, v in sorted(costs.items())}
    complete = bool(entries) and not (missing_price or incomplete)
    currency = next(iter(amounts)) if len(amounts) == 1 else None
    cost = amounts[currency] if complete and currency else None
    summary = {
        "schema_version": 2,
        "total_tokens": sum(totals[k] for k in TOKEN_KEYS),
        **{k: totals[k] for k in TOKEN_KEYS},
        "calls": len({e.get("agent_call_id") or e.get("invocation_id") or (e.get("session_id"), e.get("role"), e.get("time")) for e in entries}),
        "process_attempts": len(entries),
        "api_requests": sum(e.get("api_requests") or 0 for e in entries) if entries and all(e.get("api_requests") is not None for e in entries) else None,
        "observed_api_requests": sum(e.get("api_requests") or 0 for e in entries),
        "num_turns": totals["num_turns"],
        "duration_ms": totals["duration_ms"],
        "duration_api_ms": totals["duration_api_ms"] if entries and all(e.get("duration_api_ms") is not None for e in entries) else None,
        "duration_api_hours": round(totals["duration_api_ms"] / 3_600_000, 4) if entries and all(e.get("duration_api_ms") is not None for e in entries) else None,
        "currency": currency, "estimated_cost": cost,
        "estimated_cost_usd": cost if currency == "USD" else None,
        "known_cost_by_currency": amounts,
        "cost_status": "no_usage" if not entries else "partial" if incomplete else "unpriced" if missing_price else "legacy_unverified" if legacy else "estimated",
        "unpriced_invocations": missing_price, "incomplete_invocations": incomplete,
        "legacy_unverified_invocations": legacy, "models": dict(models),
    }
    if _stages:
        grouped = defaultdict(list)
        for entry in entries:
            grouped[_stage(entry.get("role", ""))].append(entry)
        summary["stages"] = {k: summarize(v, pricing, recalculate=recalculate, _stages=False) for k, v in sorted(grouped.items())}
    return summary


def source_summary(source_root: Path, pricing: dict, *, recalculate: bool = False) -> dict:
    entries = read_entries(usage_files(source_root))
    current = [e for e in entries if Path(e["_path"]).parent == source_root / "logs"]
    history = [e for e in entries if Path(e["_path"]).parent != source_root / "logs"]
    result = summarize(entries, pricing, recalculate=recalculate)
    result.update(source=source_root.name, workspace=str(source_root),
                  pricing_mode="current_config" if recalculate else "recorded_snapshot",
                  current_run=summarize(current, pricing, recalculate=recalculate),
                  inherited_history=summarize(history, pricing, recalculate=recalculate))
    result["cumulative"] = {k: v for k, v in result.items() if k not in {"current_run", "inherited_history", "workspace", "source"}}
    return result


def _write_report(target: Path, summary: dict) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def write_source_usage(source_root: Path, output_root: Path, slug: str, pricing: dict | None = None, *, recalculate: bool = False) -> Path:
    summary = source_summary(source_root, load_pricing() if pricing is None else pricing, recalculate=recalculate)
    summary["source"] = slug
    return _write_report(output_root / "usage" / f"{slug}.json", summary)


def report(output_root: Path, *, recalculate: bool = False, pricing: dict | None = None) -> list[dict]:
    rows = []
    pricing = load_pricing() if pricing is None else pricing
    usage_root = output_root / "usage"
    names = {p.stem for p in usage_root.glob("*.json")}
    for name in ("final", "finals"):
        final_root = output_root / name
        if final_root.is_dir():
            names.update(p.name for p in final_root.iterdir() if p.is_dir())
    for name in sorted(names):
        path = usage_root / f"{name}.json"
        if not path.is_file():
            rows.append({"source": name, "missing": True})
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        if recalculate and row.get("workspace") and Path(row["workspace"]).is_dir():
            previous_estimate = {k: row.get(k) for k in ("schema_version", "estimated_cost", "estimated_cost_usd", "currency", "pricing_mode")}
            row = source_summary(Path(row["workspace"]), pricing, recalculate=True)
            row["source"] = name
            row["recalculation"] = {"time": time.time(), "previous_estimate": previous_estimate,
                                    "legacy_currency_label_unverified": previous_estimate["schema_version"] != 2}
            _write_report(path, row)
        elif row.get("schema_version") != 2:
            # Preserve the original number for inspection; its currency was never recorded.
            row["legacy_estimated_cost_usd"] = row.get("estimated_cost_usd")
            row.update(estimated_cost=None, estimated_cost_usd=None, currency=None, cost_status="legacy_unverified")
        if recalculate and (not row.get("workspace") or not Path(row["workspace"]).is_dir()):
            row["recalculation_error"] = "原始工作区不存在，不能可靠重算"
        rows.append(row)
    return rows


def _format_table(rows: list[dict]) -> str:
    lines = [f"{'task':40} {'tokens':>14} {'本次费用':>14} {'历史费用':>14} {'累计费用':>16}  状态"]
    labels = {"estimated": "估算", "legacy_unverified": "旧记录待核验", "no_usage": "无用量记录",
              "partial": "用量不完整", "unpriced": "价格未配置"}
    def money(row):
        value = row.get("estimated_cost")
        return f"{value:.4f} {row.get('currency')}" if value is not None else "未知"
    for row in rows:
        lines.append(f"{row['source'][:40]:40} {row.get('total_tokens', 0):>14,} {money(row.get('current_run', {})):>14} "
                     f"{money(row.get('inherited_history', {})):>14} {money(row):>16}  {labels.get(row.get('cost_status'), '无用量记录')}")
    totals = defaultdict(Decimal)
    for row in rows:
        for currency, value in row.get("known_cost_by_currency", {}).items():
            totals[currency] += Decimal(str(value))
    lines.append("已知部分合计（不含未知费用）：" + (", ".join(f"{v:.4f} {k}" for k, v in sorted(totals.items())) or "未知"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看本次、历史和累计用量；金额均为估算")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--output", help="环境输出目录，查看已保存报告")
    source.add_argument("--run", help="运行目录，包含失败或尚未发布的任务")
    parser.add_argument("--pricing", type=Path, default=PROJECT_PRICING)
    parser.add_argument("--recalculate", action="store_true", help="明确按当前价格重算；--output 同时更新汇总，原始用量不变")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    pricing = load_pricing(args.pricing)
    if args.run:
        root = Path(args.run).expanduser().resolve()
        sources = root / "workspace" / "sources"
        if not sources.is_dir():
            parser.error("--run 目录中没有 workspace/sources")
        rows = [source_summary(p, pricing, recalculate=args.recalculate) for p in sorted(sources.iterdir()) if p.is_dir()]
    else:
        rows = report(Path(args.output).expanduser().resolve(), recalculate=args.recalculate, pricing=pricing)
    print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else _format_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
