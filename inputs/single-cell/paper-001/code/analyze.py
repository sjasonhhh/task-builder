"""计算合成示例中每个细胞的总表达计数，仅使用 Python 自带功能。"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "counts.csv",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with args.input.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    results = [
        {"cell_id": row["cell_id"], "total_counts": sum(
            int(value) for gene, value in row.items() if gene != "cell_id"
        )}
        for row in rows
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=["cell_id", "total_counts"])
        writer.writeheader()
        writer.writerows(results)


if __name__ == "__main__":
    main()
