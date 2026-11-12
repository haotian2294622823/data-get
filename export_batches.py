"""命令行批量导出：python export_batches.py --start 2026-01-01 --end 2026-12-31。

每批独立处理，完成后流式拼接完整 CSV，不把全年结果放入内存。
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from lot_extract.batch_export import error_message, export_batches
from lot_extract.database import PRODUCT_FIELDS, Filters, load_settings


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lot", default="")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--product", default="DFEC")
    parser.add_argument("--field", choices=PRODUCT_FIELDS, default="murata_type")
    parser.add_argument("--number", default="")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--output-directory", type=Path, help="新建的空任务目录")
    args = parser.parse_args()
    load_settings()
    filters = Filters(args.lot, args.field, args.number, args.start, args.end)
    try:
        result = export_batches(
            filters, args.product, batch_size=args.batch_size, directory=args.output_directory,
            progress=lambda state: print(state["stage"], flush=True),
        )
        print(f"已完成：{result['rows']} 个 E 系统 LOT。结果目录：{result['directory']}")
        if result.get("final_csv"):
            print("完整宽表：final.csv。分片仅作中间存档。")
    except (Exception, KeyboardInterrupt) as error:
        parser.exit(1, error_message(error) + "\n")
