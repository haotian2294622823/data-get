"""统一命令行入口：export 批量导出、fetch 原始取数、merge 合并、join 拼接、filter 筛选。"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path

from lot_extract.tables import PRODUCT_FIELDS, load_settings


def export_command(args: argparse.Namespace) -> None:
    # 数据库相关依赖只在取数命令中加载，查看帮助或离线合并无需数据库驱动。
    from lot_extract.batch_export import export_batches
    from lot_extract.database import Filters

    load_settings()
    filters = Filters(args.lot, args.field, args.number, args.start, args.end)
    result = export_batches(
        filters, args.product, batch_size=args.batch_size, directory=args.output_directory,
        progress=lambda state: print(state["stage"], flush=True),
    )
    print(f"已完成：{result['rows']} 个系统 LOT。结果目录：{result['directory']}")
    if result.get("final_csv"):
        print("完整宽表：final.csv。分片仅作中间存档。")


def fetch_command(args: argparse.Namespace) -> None:
    from lot_extract.database import Filters, archive_query, products

    load_settings()
    lot_no = args.lot_no if args.lot_no is not None else input("系统 LOT: ")
    archive, count = archive_query(Filters(lot_no=lot_no), args.product or products()[0])
    print(f"已存档 {count} 个系统 LOT：{archive}")


def merge_command(args: argparse.Namespace) -> None:
    from lot_extract.merge import merge_csv

    if args.product is not None:
        load_settings()
    merge_csv(args.directory, args.output, product=args.product)


def join_command(args: argparse.Namespace) -> None:
    from lot_extract.final_export import join_saved_export

    if args.product is not None:
        load_settings()
    destination = join_saved_export(args.directory, product=args.product)
    print(f"完整 CSV：{destination}")


def filter_command(args: argparse.Namespace) -> None:
    from lot_extract.material_merge import archive_rules
    from lot_extract.tables import LotPolicy, filter_csv, load_lot_policy

    manifest = args.source.parent / "manifest.json"
    state = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    if args.product is not None:
        load_settings()
    if state.get("final_csv", {}).get("file") == args.source.name and "rules" in state:
        policy = archive_rules(state, args.product).lot
    elif args.product is not None:
        policy = load_lot_policy(args.product)
    else:
        policy = LotPolicy()
    filter_csv(args.source, args.output, policy=policy)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="按条件批量查询并生成完整 CSV")
    export.add_argument("--lot", default="")
    export.add_argument("--start", type=date.fromisoformat)
    export.add_argument("--end", type=date.fromisoformat)
    export.add_argument("--product", type=str.upper, default="DFEC")
    export.add_argument("--field", choices=PRODUCT_FIELDS, default="murata_type")
    export.add_argument("--number", default="")
    export.add_argument("--batch-size", type=int)
    export.add_argument("--output-directory", type=Path, help="新建的空任务目录")
    export.set_defaults(run=export_command)

    fetch = commands.add_parser("fetch", help="保存一个系统 LOT 及其材料的原始 CSV")
    fetch.add_argument("lot_no", nargs="?", help="省略时提示输入 LOT")
    fetch.add_argument("--product", type=str.upper, help="产品数据库配置代码")
    fetch.set_defaults(run=fetch_command)

    merge = commands.add_parser("merge", help="把原始 CSV 存档合并成宽表，无需数据库")
    merge.add_argument("directory", nargs="?", type=Path, help="默认读取项目目录")
    merge.add_argument("--output", type=Path)
    merge.add_argument("--product", type=str.upper, help="无规则快照时使用指定产品")
    merge.set_defaults(run=merge_command)

    join = commands.add_parser("join", help="把已完成任务的中间结果拼接成完整 CSV")
    join.add_argument("directory", type=Path, help="包含 manifest.json 的已完成任务目录")
    join.add_argument("--product", type=str.upper, help="无规则快照时使用指定产品")
    join.set_defaults(run=join_command)

    filter_rows = commands.add_parser("filter", help="对已有 CSV 应用系统 LOT 筛选规则")
    filter_rows.add_argument("source", type=Path)
    filter_rows.add_argument("--output", type=Path)
    filter_rows.add_argument("--product", type=str.upper, help="使用指定产品的 LOT 筛选规则")
    filter_rows.set_defaults(run=filter_command)

    args = parser.parse_args()
    try:
        args.run(args)
    except KeyboardInterrupt:
        parser.exit(1, "操作已取消。\n")
    except Exception as error:
        # 数据库异常可能包含连接信息，因此只显示错误码或类型。
        # 已知的参数、CSV 和文件错误保留可操作的说明。
        if isinstance(error, (ValueError, OSError, csv.Error)):
            message = str(error)
        else:
            reason = getattr(error, "sqlstate", None) or type(error).__name__
            message = f"操作失败（{reason}），请检查配置、数据库连接或输入文件。"
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
