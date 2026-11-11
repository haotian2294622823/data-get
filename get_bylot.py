"""手动获取一个系统 LOT 及直接用料的原始 CSV。

运行示例：python get_bylot.py LOT123 --product DFEC
只负责收集命令行参数并调用共用取数逻辑，不生成宽表；合并请运行 merge_csv.py。
"""

import argparse

import psycopg

from lot_extract.database import Filters, load_settings, products
from lot_extract.service import archive_query


def main() -> None:
    """读取参数、加载 .env、发起一次取数，最后输出存档位置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    # nargs="?" 允许省略位置参数，省略后再通过 input 手动输入 LOT。
    parser.add_argument("lot_no", nargs="?")
    parser.add_argument("--product", type=str.upper, help="产品数据库配置代码")
    args = parser.parse_args()
    load_settings()
    try:
        lot_no = args.lot_no if args.lot_no is not None else input("系统 LOT: ")
        # 未指定 --product 时，使用 LOT_PRODUCTS 中排在第一位的产品。
        # 与页面调用同一 archive_query，因此材料关联及原始存档格式保持一致。
        archive, count = archive_query(Filters(lot_no=lot_no), args.product or products()[0])
    except ValueError as error:
        parser.exit(1, f"取数失败：{error}\n")
    except (OSError, psycopg.Error) as error:
        # 只输出异常类型，避免终端错误信息带出数据库连接参数。
        parser.exit(1, f"取数失败（{type(error).__name__}），请检查查询条件、.env 和数据库连接。\n")
    print(f"已存档 {count} 个系统 LOT：{archive}")


if __name__ == "__main__":
    # 直接执行脚本才启动交互；被其他模块 import 时不自动连接数据库。
    main()
