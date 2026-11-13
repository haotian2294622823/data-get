"""回查数据库：把「未匹配工程」的材料 LOT 拿回源库核对，看真实存在情况。

目的：确认这些 LOT 到底是不是源库里就没有批次/工序记录，
而不是我们的匹配逻辑或道次归一化出了问题。

另外反向回查 prass_process_material：看这些 LOT 作为「材料」被哪些制造 LOT 用过。
注意方向——该表的 lot_no 是用料的制造 LOT（父/消费者），material_lot_no 才是材料 LOT，
这条回查只能得到「谁用了它」，补不回缺失的工序名。

只写逻辑，本次不运行；数据库配置直接写在下面（不读 .env）。
运行方式（需要 psycopg，用项目虚拟环境）：
    .venv\\Scripts\\python.exe tests\\test2.py
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

import psycopg
from psycopg import sql

# ---- 数据库配置：直接写在代码里 ----
CONFIG = {
    "host": "10.65.48.57",
    "port": 5432,
    "dbname": "dfectest26",
    "user": "tok2026",
    "password": "Saitama2026",
}
SCHEMA = "public"
TIMEZONE = "Asia/Shanghai"

# 要回查的表：批次、工序、机台、不良。
TABLES = (
    "prass_lot",
    "prass_lot_process",
    "prass_process_machine",
    "prass_process_defect",
)

# 反向回查用的用料表。
MATERIAL = "prass_process_material"

BASE = Path(__file__).resolve().parents[1]
# 待回查的 LOT 清单；默认用全部未匹配工程 LOT，可改成「未匹配工程_W开头_lot清单.csv」只看卷线。
LOT_LIST = BASE / "outputs" / "未匹配工程_lot清单.csv"

# 想看完整原始行的样本 LOT（manifest 里提示的那 4 个卷线批）。
SAMPLE_LOTS = ("WE68290036", "WE68300075", "WE68300076", "WE68290047")

# 每个 SQL 里塞多少个 LOT，避免参数过多；明细表结果同时用于写核对表。
CHUNK = 500
# 打印时每段最多列多少个 LOT，完整清单写进 CSV。
PREVIEW = 50


def load_lots(path: Path) -> list[str]:
    """从清单 CSV 读出去重后的 LOT 编号（保持首次出现顺序）。"""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        values = [row["lot_no"] for row in csv.DictReader(stream) if row["lot_no"].strip()]
    return list(dict.fromkeys(values))


def chunks(values: list[str], size: int):
    """把 LOT 列表切成固定大小的片段，逐段发 SQL。"""
    for start in range(0, len(values), size):
        yield values[start:start + size]


def count_by_lot(connection, table: str, lots: list[str]) -> dict[str, int]:
    """按 lot_no 统计某表命中行数；分片查询后累加，避免一次传太多参数。"""
    counts: dict[str, int] = defaultdict(int)
    statement = sql.SQL(
        "SELECT lot_no, count(*) FROM {} WHERE lot_no = ANY(%s) GROUP BY lot_no"
    ).format(sql.Identifier(SCHEMA, table))
    for part in chunks(lots, CHUNK):
        for lot_no, number in connection.execute(statement, (part,)).fetchall():
            counts[lot_no] += number
    return counts


def show_lots(title: str, values: list[str]) -> None:
    """打印一段 LOT 清单，超过预览数量时只提示总数。"""
    print(f"  {title}（{len(values)} 个）：{', '.join(values[:PREVIEW]) or '无'}")
    if len(values) > PREVIEW:
        print(f"    …… 其余 {len(values) - PREVIEW} 个见输出 CSV")


def query_material_links(connection, lots: list[str]) -> list[tuple]:
    """反向回查用料表：这些 LOT 作为材料（material_lot_no）被哪些制造 LOT 用过。

    返回原始行，列顺序：lot_no(父/消费者), process_key, pass_order, seq, material_lot_no。
    注意 lot_no 是用料的制造 LOT，不是这些 LOT 自己的工序。
    """
    statement = sql.SQL(
        "SELECT lot_no, process_key, pass_order, seq, material_lot_no "
        "FROM {} WHERE material_lot_no = ANY(%s)"
    ).format(sql.Identifier(SCHEMA, MATERIAL))
    rows: list[tuple] = []
    for part in chunks(lots, CHUNK):
        rows.extend(connection.execute(statement, (part,)).fetchall())
    return rows


def dump_sample(connection, lots: tuple[str, ...]) -> None:
    """按原样打印样本 LOT 在各表中的全部字段，用于人工核对。"""
    for table in TABLES:
        statement = sql.SQL("SELECT * FROM {} WHERE lot_no = ANY(%s) ORDER BY lot_no").format(
            sql.Identifier(SCHEMA, table)
        )
        with connection.cursor() as cursor:
            cursor.execute(statement, (list(lots),))
            fields = [column.name for column in cursor.description]
            rows = cursor.fetchall()
        print(f"\n--- {table}：样本命中 {len(rows)} 行 ---")
        print(" | ".join(fields))
        for row in rows:
            print(" | ".join("" if value is None else str(value) for value in row))


def main() -> None:
    lots = load_lots(LOT_LIST)
    print(f"待回查 LOT：{len(lots)} 个（来自 {LOT_LIST.name}）")

    with psycopg.connect(**CONFIG, connect_timeout=8) as connection:
        # 与导出流程一致：只读快照，避免回查途中数据变动；单条语句超时 30s。
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        connection.execute("SELECT set_config('TimeZone', %s, true)", (TIMEZONE,))

        # 1) 逐表统计：这些 LOT 在源库里到底有没有记录。
        found = {table: count_by_lot(connection, table, lots) for table in TABLES}
        print("\n=== 逐表命中统计 ===")
        for table in TABLES:
            counts = found[table]
            print(f"{table}: 命中 LOT {len(counts)}/{len(lots)}，记录 {sum(counts.values())} 行")

        # 2) 交叉核对：明细有、父工序没有 → 就是「未匹配工程」的根因。
        detail_lots = set(found["prass_process_machine"]) | set(found["prass_process_defect"])
        process_lots = set(found["prass_lot_process"])
        print("\n=== 交叉核对 ===")
        print(f"明细表（机台/不良）出现过的 LOT：{len(detail_lots)} 个")
        print(f"其中在 prass_lot_process 里有父工序的：{len(detail_lots & process_lots)} 个")
        print(f"其中没有父工序的（= 未匹配工程）：{len(detail_lots - process_lots)} 个")
        print(f"在 prass_lot 里有批次行的：{len(set(found['prass_lot']))} 个")

        show_lots("批次表命中", sorted(found["prass_lot"]))
        show_lots("工序表命中", sorted(found["prass_lot_process"]))
        show_lots("明细有但父工序缺失", sorted(detail_lots - process_lots))

        # 3) 反向回查用料表：这些 LOT 作为材料被哪些制造 LOT（父/消费者）用过。
        links = query_material_links(connection, lots)
        used_counts = Counter(row[4] for row in links)
        consumers: dict[str, set[str]] = defaultdict(set)
        for row in links:
            consumers[row[4]].add(row[0])
        print("\n=== 用料表反向回查（material_lot_no = 待查 LOT） ===")
        print(f"命中记录：{len(links)} 行")
        print(f"作为材料被使用过的 LOT：{len(used_counts)}/{len(lots)} 个")
        print(f"对应的制造 LOT（父/消费者）：{len({row[0] for row in links})} 个")
        show_lots("未被任何制造 LOT 使用的", sorted(set(lots) - set(used_counts)))

        # 4) 逐 LOT 命中明细写 CSV，方便整体查看。
        output = LOT_LIST.with_name(LOT_LIST.stem + "_回查结果.csv")
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["lot_no", *TABLES, "有父工序", "作为材料被使用次数", "被几个制造LOT使用"]
            )
            for lot_no in lots:
                writer.writerow(
                    [
                        lot_no,
                        *(found[table].get(lot_no, 0) for table in TABLES),
                        "是" if lot_no in process_lots else "否",
                        used_counts.get(lot_no, 0),
                        len(consumers[lot_no]),
                    ]
                )
        print(f"\n逐 LOT 回查结果已写入：{output}")

        # 5) 样本 LOT 的原始记录，人工逐行核对。
        print(f"\n=== 样本 LOT 原始记录：{', '.join(SAMPLE_LOTS)} ===")
        dump_sample(connection, SAMPLE_LOTS)

        print(f"\n=== 样本 LOT 用料表反向回查（material_lot_no） ===")
        for row in links:
            if row[4] in SAMPLE_LOTS:
                print(" | ".join("" if value is None else str(value) for value in row))


if __name__ == "__main__":
    main()
