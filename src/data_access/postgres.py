"""M5 接入边界：三张核心表，不开放未经核实的设备等生产映射。"""

from contextlib import contextmanager
from functools import lru_cache
import time
from typing import Iterator
import pandas as pd
from sqlalchemy import MetaData, Table, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from src.errors import (
    DataContractError,
    DataSourceError,
    DataSourceNotConfigured,
    LimitExceeded,
)

TABLES = {
    "lots": ("prass_lot", ["lot_no"]),
    "material_records": (
        "prass_process_material",
        ["lot_no", "pass_order", "seq", "material_lot_no", "process_key"],
    ),
    "process_records": (
        "prass_lot_process",
        ["lot_no", "process_key", "pass_order", "process_code", "process_name"],
    ),
}


@lru_cache(maxsize=4)
def database_engine(url: str):
    return create_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 10},
    )


class PostgresSource:
    def __init__(self, connection, deadline: float):
        self.connection, self.deadline = connection, deadline
        self.tables: dict[str, Table] = {}

    def timeout(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("数据库操作超过总时限")
        self.connection.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(max(1, int(remaining * 1000)))},
        )

    def read(self, source: str, key: str, values: tuple, limit: int) -> pd.DataFrame:
        if source not in TABLES or key not in TABLES[source][1] or limit < 1:
            raise DataContractError("不受支持的数据源、键或读取上限。")
        self.timeout()
        physical, columns = TABLES[source]
        if source not in self.tables:
            table = Table(
                physical,
                MetaData(),
                schema="public",
                autoload_with=self.connection,
                resolve_fks=False,
            )
            if not set(columns) <= set(table.c.keys()):
                raise DataContractError(
                    "核心表必需列缺失，请核对 public 下三张表的物理映射。"
                )
            self.tables[source] = table
        table = self.tables[source]
        records: list[dict] = []
        # 批量绑定；适配器不再加探测行，服务已传入剩余额度+1。
        for offset in range(0, len(values), 500):
            self.timeout()
            statement = (
                select(*(table.c[c] for c in columns))
                .where(table.c[key].in_(values[offset : offset + 500]))
                .limit(limit - len(records))
            )
            records.extend(
                dict(row._mapping) for row in self.connection.execute(statement)
            )
            if len(records) >= limit:
                break
        return pd.DataFrame(records, columns=columns, dtype=object)

    def get_lots(self, lot_nos: tuple[str, ...], row_limit: int) -> pd.DataFrame:
        return self.read("lots", "lot_no", lot_nos, row_limit)

    def get_material_records(
        self, lot_nos: tuple[str, ...], row_limit: int, field_ids: tuple[str, ...] = ()
    ) -> pd.DataFrame:
        if field_ids:
            raise DataContractError("真实材料可选字段尚未核实，不允许抽取。")
        return self.read("material_records", "lot_no", lot_nos, row_limit)

    def get_process_records(
        self, lot_nos: tuple[str, ...], row_limit: int
    ) -> pd.DataFrame:
        return self.read("process_records", "lot_no", lot_nos, row_limit)

    def get_process_records_by_keys(
        self, process_keys: tuple[str, ...], row_limit: int
    ) -> pd.DataFrame:
        return self.read("process_records", "process_key", process_keys, row_limit)

    def get_detail_records(
        self,
        source_id: str,
        lot_nos: tuple[str, ...],
        field_ids: tuple[str, ...],
        row_limit: int,
    ) -> pd.DataFrame:
        raise DataContractError("此真实明细来源尚未完成关联核验。")


class PostgresFactory:
    def __init__(self, url: str):
        if not url.strip():
            raise DataSourceNotConfigured(
                "DataSourceNotConfigured：real 模式必须配置 DATABASE_URL，不会回退模拟数据。"
            )
        try:
            parsed = make_url(url)
            if parsed.get_backend_name() != "postgresql":
                raise ValueError()
        except (ValueError, SQLAlchemyError):
            raise DataSourceNotConfigured(
                "DATABASE_URL 必须是有效的 PostgreSQL SQLAlchemy URL。"
            ) from None
        self.url = url

    @contextmanager
    def open_read_session(self, deadline_at: float) -> Iterator[PostgresSource]:
        try:
            engine = database_engine(self.url)
            # 所有仓库方法共享此连接，第一条业务 SELECT 前确定隔离级别与只读属性。
            with engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection:
                with connection.begin():
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    yield PostgresSource(connection, deadline_at)
        except (SQLAlchemyError, ModuleNotFoundError, ImportError):
            raise DataSourceError(
                "PostgreSQL 读取失败；请核对驱动、连接、权限和表结构。本次没有发布 CSV。"
            ) from None
