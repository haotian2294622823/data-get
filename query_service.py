"""Read the six production tables and archive one CSV set per query."""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from dotenv import load_dotenv
from psycopg import sql

from merge_csv import TABLES, merge_tables

BASE = Path(__file__).resolve().parent
LOT_TABLE = "prass_lot"
PRODUCT_FIELDS = ("murata_type", "toko_type")
MAX_LOTS = 500
MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


@dataclass(frozen=True)
class Filters:
    lot_no: str = ""
    product_field: str = "murata_type"
    product: str = ""
    start: date | None = None
    end: date | None = None

    def where(self) -> tuple[sql.Composable, list[object]]:
        if self.product_field not in PRODUCT_FIELDS:
            raise ValueError("请选择支持的品番类型。")
        if (self.start is None) != (self.end is None):
            raise ValueError("请选择开始和结束日期。")
        clauses, parameters = [], []
        if self.lot_no.strip():
            clauses.append(sql.SQL("lot_no = %s"))
            parameters.append(self.lot_no.strip())
        if self.product.strip():
            clauses.append(sql.SQL("{} = %s").format(sql.Identifier(self.product_field)))
            parameters.append(self.product.strip())
        if self.start is not None and self.end is not None:
            if self.start > self.end or self.end == date.max:
                raise ValueError("日期范围无效。")
            clauses.append(sql.SQL("actual_finish_datetime >= %s AND actual_finish_datetime < %s"))
            parameters.extend((self.start, self.end + timedelta(days=1)))
        if not clauses:
            raise ValueError("请填写 LOT、品番或实际结束日期。")
        return sql.SQL(" AND ").join(clauses), parameters


def load_settings() -> None:
    load_dotenv(BASE / ".env", interpolate=False)


def open_database():
    settings = {
        "host": os.getenv("DB_HOST"),
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
    }
    if not all(settings.values()):
        raise ValueError("请先在 .env 填写数据库连接配置。")
    return psycopg.connect(
        **settings,
        port=os.getenv("DB_PORT", "5432"),
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
        connect_timeout=8,
    )


def archive_query(filters: Filters, root: Path | None = None) -> tuple[Path, int]:
    where, params = filters.where()
    root = root if root is not None else Path(os.getenv("EXPORT_DIR") or BASE / "outputs" / "queries")
    root.mkdir(parents=True, exist_ok=True)
    query_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex
    schema = os.getenv("DB_SCHEMA", "public")
    total = 0
    size = 0

    # Stage the files on the same filesystem so an interrupted read leaves no
    # published directory. All six SELECTs use the same database snapshot.
    with TemporaryDirectory(prefix=".pending-", dir=root) as staging:
        raw = Path(staging) / "raw"
        raw.mkdir()
        with open_database() as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute("SELECT set_config('TimeZone', %s, true)", (os.getenv("DB_TIMEZONE", "Asia/Shanghai"),))
            cursor.execute(
                sql.SQL("SELECT * FROM {} WHERE {} ORDER BY lot_no LIMIT %s").format(
                    sql.Identifier(schema, LOT_TABLE), where
                ),
                [*params, MAX_LOTS + 1],
            )
            lot_fields = [col.name for col in cursor.description]
            lots = cursor.fetchall()
            if len(lots) > MAX_LOTS:
                raise ValueError(f"命中超过 {MAX_LOTS} 个 LOT，请缩小查询范围。")
            if "lot_no" not in lot_fields:
                raise ValueError("批次表缺少 lot_no 字段。")
            lot_index = lot_fields.index("lot_no")
            lot_numbers = [row[lot_index] for row in lots]
            if any(value is None or value == "" for value in lot_numbers) or len(set(lot_numbers)) != len(lot_numbers):
                raise ValueError("批次表中存在空或重复的 LOT，无法确定记录归属。")

            for table in TABLES:
                if table == LOT_TABLE:
                    fields, records = lot_fields, lots
                else:
                    # Date and product filters select the LOTs only. Related
                    # rows keep their own timestamps, including overnight work.
                    cursor.execute(
                        sql.SQL("SELECT * FROM {} WHERE lot_no = ANY(%s) LIMIT %s").format(
                            sql.Identifier(schema, table)
                        ),
                        (lot_numbers, MAX_ROWS - total + 1),
                    )
                    fields = [col.name for col in cursor.description]
                    records = cursor.fetchall()
                total += len(records)
                if total > MAX_ROWS:
                    raise ValueError(f"六表记录超过 {MAX_ROWS} 行，请缩小查询范围。")
                path = raw / f"{table}.csv"
                with path.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(fields)
                    writer.writerows(records)
                size += path.stat().st_size
                if size > MAX_BYTES:
                    raise ValueError("六表原始记录超过 64 MiB，请缩小查询范围。")
        return raw.rename(root / query_id), len(lots)


def spreadsheet_text(value: str) -> str:
    # Keep numbers numeric and prevent spreadsheet programs interpreting
    # database text as a formula in the downloadable CSV.
    candidate = value.lstrip()
    if candidate.startswith(("=", "+", "-", "@")) and not NUMBER.fullmatch(candidate):
        return "'" + value
    return value


def merged_download(archive: Path) -> tuple[bytes, int, list[str]]:
    columns, rows, notices = merge_tables(archive, max_columns=16_384, max_cells=4_000_000)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(map(spreadsheet_text, columns))
    for row in rows:
        writer.writerow(spreadsheet_text(row[column]) for column in columns)
    content = output.getvalue().encode("utf-8-sig")
    if len(content) > MAX_BYTES:
        raise ValueError("合并宽表超过 64 MiB，请缩小查询范围。")
    return content, len(columns), notices
