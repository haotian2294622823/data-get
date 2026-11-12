"""数据库取数：产品配置、筛选条件、只读分页，以及单次原始表存档。

批量导出流程见 batch_export；材料展开和宽表合并见 material_merge 与 merge。
"""

from __future__ import annotations

import csv
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
from uuid import uuid4

import psycopg
from dotenv import load_dotenv
from psycopg import sql

from .tables import (
    LOT, MATERIAL, PRODUCT_FIELDS, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES,
    BoundedCsv, validate_columns,
)

# 从代码文件定位项目根目录，避免换一个终端工作目录后找不到 .env。
BASE = Path(__file__).resolve().parents[1]
# 产品代码还会用于环境变量前缀和存档目录，因此只允许有限的安全字符。
PROFILE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
# 每次从数据库游标取回的行数，不是整次查询的记录上限。
FETCH_ROWS = 2048


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


@dataclass(frozen=True)
class Filters:
    """系统 LOT 的查询条件；页面和手动取数共用。

    product_field 是品番字段名，product 是用户输入的品番值。
    start/end 均为空表示不限制日期，否则必须同时填写。
    frozen=True 使这组条件创建后不可修改，避免流程中途被意外改写。
    """

    lot_no: str = ""
    product_field: str = "murata_type"
    product: str = ""
    start: date | None = None
    end: date | None = None

    def where(self) -> tuple[sql.Composable, list[object]]:
        """校验条件，返回 WHERE 表达式和与 %s 一一对应的参数列表。

        所有已填写条件用 AND 连接，LOT 和品番均为精确匹配。
        此条件只用于筛选系统批次；前工程材料不能复用这里的日期、品番限制。
        """
        if self.product_field not in PRODUCT_FIELDS:
            raise ValueError("请选择支持的品番类型。")
        if (self.start is None) != (self.end is None):
            raise ValueError("请选择开始和结束日期。")
        clauses, parameters = [], []
        if self.lot_no.strip():
            clauses.append(sql.SQL("lot_no = %s"))
            parameters.append(self.lot_no.strip())
        if self.product.strip():
            # 字段名用 Identifier 处理；用户输入的值通过 %s 参数传入。
            # 二者分开，既保证字段名正确引用，也避免把输入直接拼进 SQL。
            clauses.append(sql.SQL("{} = %s").format(sql.Identifier(self.product_field)))
            parameters.append(self.product.strip())
        if self.start is not None and self.end is not None:
            # 用“开始日 <= 时间 < 结束日的下一天”包含结束当天的全部时刻。
            # 不写 23:59:59，是为了避免漏掉带小数秒的数据；date.max 不能再加一天。
            if self.start > self.end or self.end == date.max:
                raise ValueError("日期范围无效。")
            clauses.append(sql.SQL("actual_finish_datetime >= %s AND actual_finish_datetime < %s"))
            parameters.extend((self.start, self.end + timedelta(days=1)))
        if not clauses:
            # 禁止无条件读取整张批次表，前端之外的调用也必须通过这道校验。
            raise ValueError("请填写 LOT、品番或实际结束日期。")
        return sql.SQL(" AND ").join(clauses), parameters


def load_settings() -> None:
    """加载项目 .env；保留已有环境变量，不展开密码等值中的 ${...} 文本。"""
    load_dotenv(BASE / ".env", interpolate=False)


def products() -> tuple[str, ...]:
    """读取可选产品代码，统一大写并按配置中的先后顺序去重。"""
    # dict.fromkeys 保留第一次出现的位置，因此第一个产品仍是手动工具的默认值。
    values = tuple(dict.fromkeys(p.strip().upper() for p in os.getenv("LOT_PRODUCTS", "DFEC").split(",")))
    if not values or any(not PROFILE_CODE.fullmatch(p) for p in values):
        raise ValueError("LOT_PRODUCTS 须填写逗号分隔的产品代码，例如 DFEC,ABC。")
    return values


def setting(product: str, name: str, default: str = "") -> str:
    """优先读产品专用配置，例如 ABC_DB_HOST；仅 DFEC 可回退到旧 DB_HOST。"""
    # 其他产品缺少配置时使用默认值，不能借用 DFEC 的地址或密码而连错库。
    legacy = os.getenv(name, default) if product == "DFEC" else default
    return os.getenv(f"{product}_{name}", legacy)


@contextmanager
def database_snapshot(product: str):
    """在同一个只读事务快照内提供连接，退出 with 后结束事务并关闭连接。

    yield 前负责连接和配置，调用方的 with 代码块使用 yield 出去的连接。
    代码块正常结束则提交只读事务；抛出异常则由 psycopg 回滚并继续向外抛错。
    """
    if product not in products():
        raise ValueError("未配置该产品的数据库。")
    settings = {
        "host": setting(product, "DB_HOST"),
        "dbname": setting(product, "DB_NAME"),
        "user": setting(product, "DB_USER"),
        "password": setting(product, "DB_PASSWORD"),
    }
    if not all(settings.values()):
        raise ValueError(f"请先在 .env 填写 {product} 的数据库连接配置。")
    with psycopg.connect(
        **settings,
        port=setting(product, "DB_PORT", "5432"),
        sslmode=setting(product, "DB_SSLMODE", "prefer"),
        connect_timeout=8,
    ) as connection:
        # 各张表分开查询，但看到同一事务快照，避免导出途中数据更新导致表间不一致。
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        # 这是单条数据库语句的超时限制，不是整次导出的总时长限制。
        connection.execute("SET LOCAL statement_timeout = '30s'")
        # 日期与带时区时间戳比较时按此时区解释；true 表示只在当前事务生效。
        connection.execute(
            "SELECT set_config('TimeZone', %s, true)",
            (setting(product, "DB_TIMEZONE", "Asia/Shanghai"),),
        )
        yield connection


@contextmanager
def read_rows(connection, statement: sql.Composable, params):
    """返回（字段列表，逐行迭代器），只在 with 内消费迭代器。

    命名游标把结果留在服务器端，每次取 FETCH_ROWS 行到 Python。
    这减少客户端一次接收的数据量；数据库是否扫描大量记录仍取决于 SQL 和索引。
    """
    with connection.cursor(name="lot_export") as cursor:
        cursor.execute(statement, params)
        # 首次 FETCH 后读取列信息，即使命中零行也能写出 CSV 表头。
        first = cursor.fetchmany(FETCH_ROWS)
        fields = [column.name for column in cursor.description]

        def records() -> Iterator[tuple]:
            """逐行交给写文件流程；当前块消费完后才读取下一块。"""
            batch = first
            while batch:
                yield from batch
                batch = cursor.fetchmany(FETCH_ROWS)

        yield fields, records()


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
