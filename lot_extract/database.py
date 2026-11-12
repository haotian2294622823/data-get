"""数据库访问基础层：读取产品配置、生成筛选条件、分批读取 PostgreSQL。

本模块只负责“怎样安全地连接和读取”，不决定七张表的读取顺序，也不合并数据。
完整取数流程在 service.py；本模块提供的连接和游标通过 with 自动关闭。
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

import psycopg
from dotenv import load_dotenv
from psycopg import sql

# 从代码文件定位项目根目录，避免换一个终端工作目录后找不到 .env。
BASE = Path(__file__).resolve().parents[1]
PRODUCT_FIELDS = ("murata_type", "toko_type")
# 产品代码还会用于环境变量前缀和存档目录，因此只允许有限的安全字符。
PROFILE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
# 每次从数据库游标取回的行数，不是整次查询的记录上限。
FETCH_ROWS = 2048


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
