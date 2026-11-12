"""单次取数编排：筛选系统 LOT → 存档七表 → 回查材料四表。

材料关联从 prass_process_material.material_lot_no 得到，回库时用它匹配 lot_no。
日期和品番仅筛选系统 LOT，不能带到材料查询中，否则会漏掉更早完工的前工程。
"""

from __future__ import annotations

import codecs
import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from psycopg import sql

from .database import BASE, Filters, database_snapshot, read_rows, setting
from .schema import LOT, MATERIAL, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES, ExportSizeError, validate_columns

# 以下限制针对一次导出，而非数据库本身的总数据量。
# 行数和原始 CSV 字节数覆盖系统七表 + 材料四表的总和；宽表另行检查大小。
DEFAULT_MAX_LOTS = 5_000
DEFAULT_MAX_SOURCE_ROWS = 100_000
DEFAULT_MAX_CSV_MIB = 640
DEFAULT_MAX_WIDE_CELLS = 4_000_000
MAX_WIDE_COLUMNS = 16_384  # 与 Excel 工作表的最大列数对齐。


def limit(name: str, default: int) -> int:
    """读取正整数上限；不接受 0、负数或无法解析的配置，避免限制意外失效。"""
    value = os.getenv(name, str(default)).strip()
    if not value.isdecimal() or int(value) < 1:
        raise ValueError(f"{name} 须为正整数。")
    return int(value)


class BoundedCsv:
    """给二进制输出流包一层文本 write 接口，逐次检查 UTF-8 字节预算。

    csv.writer 负责引号、逗号和换行转义；本类负责编码、BOM 和文件大小限制。
    中文字符可能占多个字节，所以按编码后的字节数计量，不能只计算字符数。
    """

    def __init__(self, stream, available: int, label: str):
        """available 是当前文件可用的剩余字节数，包含 UTF-8 BOM 和表头。"""
        self.stream = stream
        self.available = available
        self.label = label
        self.size = 0
        # BOM 便于 Excel 识别 UTF-8 中文，原始记录内容仍由 csv.writer 原样写入。
        self.write_bytes(codecs.BOM_UTF8)

    def write_bytes(self, content: bytes) -> None:
        """在写入前检查上限，超限抛错而不是留下看似完整的截断文件。"""
        if self.size + len(content) > self.available:
            raise ExportSizeError(f"{self.label}超过文件大小上限，请缩小查询范围。")
        self.stream.write(content)
        self.size += len(content)

    def write(self, text: str) -> None:
        """供 csv.writer 调用：把一段 CSV 文本编码后交给受限写入方法。"""
        self.write_bytes(text.encode("utf-8"))


def archive_query(
    filters: Filters, product: str = "DFEC", root: Path | None = None
) -> tuple[Path, int]:
    """完成一次只读抽取，返回（本次原始存档目录，命中的系统 LOT 数）。

    先按 filters 筛系统批次，再按批次集合读其余六张表；从用料记录中收集
    材料 LOT，去重后按集合读取前工程四表。所有原始 CSV 放在同一查询目录。
    仅查直接使用的材料，不递归展开材料的上游；任一步骤失败就不发布该次存档。
    """
    where, params = filters.where()
    where = sql.SQL("({}) AND lot_no LIKE %s AND lot_no NOT LIKE %s").format(where)
    params = [*params, "E%", "%E%E%"]
    max_lots = limit("MAX_QUERY_LOTS", DEFAULT_MAX_LOTS)
    max_rows = limit("MAX_SOURCE_ROWS", DEFAULT_MAX_SOURCE_ROWS)
    max_bytes = limit("MAX_CSV_MIB", DEFAULT_MAX_CSV_MIB) * 1024 * 1024
    root = Path(root or os.getenv("EXPORT_DIR") or BASE / "outputs" / "queries")
    query_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex
    schema = setting(product, "DB_SCHEMA", "public")
    # 字典键用文本进行去重，值保留数据库原始类型，回查时交给 psycopg 绑定参数。
    # primary 是系统 LOT 集合；materials 是要查询的材料集合，不承载父子关系。
    primary: dict[str, object] = {}
    materials: dict[str, object] = {}
    total_rows = 0
    total_bytes = 0

    # 先写入同一磁盘上的临时目录。十一份 CSV 全部读完且事务正常结束后，
    # 才通过目录重命名发布存档；异常退出 with 时自动清理临时文件。
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".pending-", dir=root) as staging:
        raw = Path(staging) / "raw"
        raw.mkdir()

        with database_snapshot(product) as connection:
            def archive_table(table, query, parameters, *, prefix="", collect=None):
                """执行一条查询并逐行写 CSV，可顺便收集系统或材料 LOT。

                collect 指定要收集的列；不指定时只存档。prefix 只改变文件名，
                前工程仍查询同名数据库表，无需创建 upstream_ 开头的数据库表。
                """
                # nonlocal 让所有表共享并更新本次查询的总记录数和总字节数。
                nonlocal total_rows, total_bytes
                path = raw / f"{prefix}{table}.csv"
                with read_rows(connection, query, parameters) as (fields, rows), path.open("wb") as stream:
                    validate_columns(table, fields)
                    output = BoundedCsv(stream, max_bytes - total_bytes, "原始记录")
                    writer = csv.writer(output)
                    # 即使没有记录也写表头，使空表存档仍能用于离线合并。
                    writer.writerow(fields)
                    index = fields.index(collect) if collect else None
                    for row in rows:
                        total_rows += 1
                        if total_rows > max_rows:
                            raise ValueError(f"原始记录超过 {max_rows} 行，请缩小查询范围。")
                        if index is not None:
                            value = row[index]
                            key = "" if value is None else str(value)
                            if collect == "lot_no":
                                # 系统批次表必须一 LOT 一条；歧义记录不能任意取第一条。
                                if not key.strip() or key in primary:
                                    raise ValueError("批次表存在空或重复的 LOT。")
                                if len(primary) >= max_lots:
                                    raise ValueError(f"命中超过 {max_lots} 个系统 LOT，请缩小查询范围。")
                                primary[key] = value
                            elif key.strip():
                                # 这里只去重“回库查询的编号”，每条原始用料记录照常写出。
                                # 因此多个系统 LOT 共用同一材料时，关联关系仍完整保留。
                                materials.setdefault(key, value)
                        writer.writerow(row)
                    total_bytes += output.size

            def archive_by_lots(table, lot_numbers, *, prefix="", collect=None):
                """用一次集合查询读取某表中的目标 LOT，不为每个 LOT 单独发 SQL。"""
                # ANY(%s) 接收参数数组；LIMIT 比剩余行数预算多 1，借额外一行
                # 检测超限并报错，避免把“达到上限的部分结果”误当成完整导出。
                statement = sql.SQL("SELECT * FROM {} WHERE lot_no = ANY(%s) LIMIT %s").format(
                    sql.Identifier(schema, table)
                )
                archive_table(
                    table, statement, (list(lot_numbers), max_rows - total_rows + 1),
                    prefix=prefix, collect=collect,
                )

            # 第一步：只有系统批次查询使用日期/品番/LOT 筛选条件。
            # 同样多取 1 行来判断是否超过系统 LOT 数或总记录数上限。
            archive_table(
                LOT,
                sql.SQL("SELECT * FROM {} WHERE {} ORDER BY lot_no LIMIT %s").format(
                    sql.Identifier(schema, LOT), where,
                ),
                [*params, min(max_lots, max_rows) + 1],
                collect="lot_no",
            )
            for table in TABLES:
                if table != LOT:
                    archive_by_lots(
                        table, primary.values(),
                        collect="material_lot_no" if table == MATERIAL else None,
                    )
            # 第二轮只按去重后的 material_lot_no 匹配各表 lot_no，不附加系统日期。
            # 这里只查四类信息；合并时再通过原始用料记录挂回对应的系统 LOT。
            for table in UPSTREAM_TABLES:
                archive_by_lots(table, materials.values(), prefix=UPSTREAM_PREFIX)

        destination = root / product / query_id
        destination.parent.mkdir(exist_ok=True)
        raw.rename(destination)
    return destination, len(primary)
