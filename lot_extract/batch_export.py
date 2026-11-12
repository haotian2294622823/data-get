"""按完整系统 LOT 分批导出，任务内用磁盘缓存复用材料查询。

同一任务使用同一个只读快照，缓存不跨任务复用。
各分片独立保存原始 CSV、宽表及表头指纹，不把全年数据装进内存。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Event
from time import perf_counter
from uuid import uuid4

import psycopg
from psycopg import sql

from .database import BASE, Filters, database_snapshot, products, read_rows, setting
from .final_export import join_parts
from .merge import merge_tables, write_wide
from .presentation import is_system_lot
from .schema import LOT, MATERIAL, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES, ExportSizeError, validate_columns
from .service import BoundedCsv, DEFAULT_MAX_CSV_MIB, DEFAULT_MAX_SOURCE_ROWS, DEFAULT_MAX_WIDE_CELLS, MAX_WIDE_COLUMNS, limit


class ExportCancelled(RuntimeError):
    """用户要求取消；已完成的分片保留在磁盘。"""


def error_message(error: BaseException) -> str:
    """任务记录和页面不保存数据库异常全文或连接凭据。"""
    if isinstance(error, psycopg.Error):
        return f"数据库读取失败（{error.sqlstate or type(error).__name__}）。"
    if isinstance(error, (ValueError, ExportCancelled)):
        return str(error)
    return f"导出失败（{type(error).__name__}），请检查服务器日志和磁盘空间。"


def write_manifest(path: Path, state: dict) -> None:
    """原子发布进度，避免页面读到半个 JSON 文件。"""
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class MaterialCache:
    """缓存四表原始行及“查过但无记录”的材料 LOT，内存不随全年材料数增长。"""

    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.executescript(
            "CREATE TABLE lots(lot TEXT PRIMARY KEY);"
            "CREATE TABLE headers(source TEXT PRIMARY KEY, fields TEXT NOT NULL);"
            "CREATE TABLE records(id INTEGER PRIMARY KEY, source TEXT, lot TEXT, data TEXT);"
            "CREATE INDEX records_lot ON records(source, lot);"
        )
        self.hits = 0
        self.misses = 0

    def close(self) -> None:
        self.connection.close()

    def ensure(self, connection, schema: str, materials: dict[str, object], max_rows: int, check: Callable) -> None:
        missing = {
            lot: value for lot, value in materials.items()
            if self.connection.execute("SELECT 1 FROM lots WHERE lot=?", (lot,)).fetchone() is None
        }
        self.hits += len(materials) - len(missing)
        if not missing and self.connection.execute("SELECT COUNT(*) FROM headers").fetchone()[0] == len(UPSTREAM_TABLES):
            return
        fetched = 0
        # 四表全部读完才标记“已缓存”；失败时回滚，不能缓存截断的材料数据。
        with self.connection:
            for source in UPSTREAM_TABLES:
                check()
                statement = sql.SQL("SELECT * FROM {} WHERE lot_no = ANY(%s) LIMIT %s").format(
                    sql.Identifier(schema, source)
                )
                with read_rows(connection, statement, (list(missing.values()), max_rows - fetched + 1)) as (fields, rows):
                    validate_columns(source, fields)
                    previous = self.connection.execute("SELECT fields FROM headers WHERE source=?", (source,)).fetchone()
                    if previous is not None and json.loads(previous[0]) != fields:
                        raise ValueError("材料表头在同一任务中发生变化，请核对数据源。")
                    self.connection.execute(
                        "INSERT OR IGNORE INTO headers VALUES(?,?)", (source, json.dumps(fields))
                    )
                    index = fields.index("lot_no")
                    for row in rows:
                        check()
                        fetched += 1
                        if fetched > max_rows:
                            raise ExportSizeError("材料原始记录超出单批上限。")
                        values = ["" if value is None else str(value) for value in row]
                        if values[index] not in missing:
                            raise ValueError("材料查询返回了不属于本批用料关系的 LOT。")
                        self.connection.execute(
                            "INSERT INTO records(source,lot,data) VALUES(?,?,?)",
                            (source, values[index], json.dumps(values, ensure_ascii=False)),
                        )
            self.connection.executemany("INSERT INTO lots VALUES(?)", ((lot,) for lot in missing))
        self.misses += len(missing)

    def fields(self, source: str) -> list[str]:
        return json.loads(self.connection.execute("SELECT fields FROM headers WHERE source=?", (source,)).fetchone()[0])

    def rows(self, source: str, materials: dict[str, object]) -> Iterable[list[str]]:
        for lot in materials:
            cursor = self.connection.execute(
                "SELECT data FROM records WHERE source=? AND lot=? ORDER BY id", (source, lot)
            )
            for (data,) in cursor:
                yield json.loads(data)


class BatchArchive:
    def __init__(self, directory: Path, max_rows: int, max_bytes: int, check: Callable):
        self.directory = directory
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.check = check
        self.rows = 0
        self.bytes = 0
        self.materials: dict[str, object] = {}

    def write(self, source: str, fields: list[str], records: Iterable, prefix: str = "") -> None:
        validate_columns(source, fields)
        with (self.directory / f"{prefix}{source}.csv").open("wb") as stream:
            output = BoundedCsv(stream, self.max_bytes - self.bytes, "单批原始 CSV")
            writer = csv.writer(output)
            writer.writerow(fields)
            material_index = fields.index("material_lot_no") if source == MATERIAL and not prefix else None
            for row in records:
                self.check()
                self.rows += 1
                if self.rows > self.max_rows:
                    raise ExportSizeError("原始记录超出单批上限。")
                if material_index is not None and row[material_index] is not None and str(row[material_index]).strip():
                    self.materials.setdefault(str(row[material_index]), row[material_index])
                writer.writerow(row)
            self.bytes += output.size


def export_batches(
    filters: Filters,
    product: str = "DFEC",
    *,
    batch_size: int | None = None,
    directory: Path | None = None,
    progress: Callable[[dict], None] | None = None,
    cancel: Event | None = None,
) -> dict:
    """分页读取 E 系统 LOT，批次超限时二分重试，单 LOT 超限则明确失败。"""
    where, parameters = filters.where()
    if product not in products():
        raise ValueError("未配置该产品的数据库。")
    batch_size = batch_size if batch_size is not None else limit("EXPORT_BATCH_LOTS", 500)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 5000:
        raise ValueError("每批 LOT 数须在 1 到 5000 之间。")
    max_rows = limit("MAX_SOURCE_ROWS", DEFAULT_MAX_SOURCE_ROWS)
    max_bytes = limit("MAX_CSV_MIB", DEFAULT_MAX_CSV_MIB) * 1024 * 1024
    max_cells = limit("MAX_WIDE_CELLS", DEFAULT_MAX_WIDE_CELLS)
    part_bytes = min(max_bytes, limit("EXPORT_PART_MIB", 32) * 1024 * 1024)
    directory = Path(directory) if directory is not None else (
        Path(os.getenv("EXPORT_DIR") or BASE / "outputs" / "queries")
        / product / ("batch_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex)
    )
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.json"
    if any(directory.iterdir()):
        raise ValueError("导出目录必须为空，不能覆盖已有任务或文件。")
    archives = directory / "archives"
    archives.mkdir(exist_ok=True)
    state = {
        "status": "running", "product": product, "directory": str(directory.resolve()), "rows": 0, "parts": [],
        "stage": "正在连接数据库", "cache_hits": 0, "cache_misses": 0,
        "phase": "exporting", "found_lots": 0,
        "lot_lookup_seconds": 0.0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "lot_no": filters.lot_no, "product_field": filters.product_field, "product": filters.product,
            "start": str(filters.start) if filters.start else None, "end": str(filters.end) if filters.end else None,
        },
        "batch_size": batch_size,
    }
    started = perf_counter()

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise ExportCancelled("任务已取消，已保存的中间结果保留在服务器。")

    def publish(stage: str) -> None:
        state["stage"] = stage
        state["elapsed_seconds"] = round(perf_counter() - started, 3)
        write_manifest(manifest, state)
        if progress is not None:
            progress({**state, "parts": list(state["parts"])})

    publish("正在连接数据库")
    try:
        check()
        schema = setting(product, "DB_SCHEMA", "public")
        with TemporaryDirectory(prefix="lot-material-cache-") as cache_directory, database_snapshot(product) as connection:
            check()
            cache = MaterialCache(Path(cache_directory) / "materials.sqlite")
            try:
                def process(fields: list[str], lots: list[tuple]) -> None:
                    check()
                    index = fields.index("lot_no")
                    part_id = f"part_{len(state['parts']) + 1:06d}"
                    attempt_started = perf_counter()
                    try:
                        with TemporaryDirectory(prefix=".pending-", dir=archives) as temporary:
                            raw = Path(temporary)
                            writer = BatchArchive(raw, max_rows, max_bytes, check)
                            writer.write(LOT, fields, lots)
                            lot_numbers = [row[index] for row in lots]
                            publish(f"正在读取 {len(lots)} 个系统 LOT 的明细")
                            for table in TABLES:
                                if table == LOT:
                                    continue
                                statement = sql.SQL("SELECT * FROM {} WHERE lot_no = ANY(%s) LIMIT %s").format(
                                    sql.Identifier(schema, table)
                                )
                                with read_rows(
                                    connection, statement, (lot_numbers, max_rows - writer.rows + 1)
                                ) as (detail_fields, records):
                                    writer.write(table, detail_fields, records)
                            read_seconds = perf_counter() - attempt_started
                            phase_started = perf_counter()
                            publish(f"正在读取或复用 {len(writer.materials)} 个材料 LOT")
                            cache.ensure(connection, schema, writer.materials, max_rows, check)
                            for table in UPSTREAM_TABLES:
                                writer.write(table, cache.fields(table), cache.rows(table, writer.materials), UPSTREAM_PREFIX)
                            material_seconds = perf_counter() - phase_started
                            phase_started = perf_counter()
                            publish(f"正在合并 {len(lots)} 个系统 LOT")
                            columns, rows, notices = merge_tables(raw, max_columns=MAX_WIDE_COLUMNS, max_cells=max_cells)
                            merge_seconds = perf_counter() - phase_started
                            phase_started = perf_counter()
                            if len(rows) != len(lots) or any(not is_system_lot(row["lot_no"]) for row in rows):
                                raise ValueError("合并结果的系统 LOT 数量或前缀与本批查询不一致。")
                            with (raw / "wide.csv").open("wb") as stream:
                                write_wide(BoundedCsv(stream, part_bytes, "单个宽表分片"), columns, rows)
                            check()
                            destination = directory / f"{part_id}.csv"
                            os.replace(raw / "wide.csv", destination)
                            os.replace(raw, archives / part_id)
                            state["parts"].append({
                                "file": destination.name, "archive": f"archives/{part_id}",
                                "rows": len(rows), "columns": len(columns), "bytes": destination.stat().st_size,
                                "schema_sha256": hashlib.sha256(json.dumps(columns, ensure_ascii=False).encode()).hexdigest(),
                                "notice_count": len(notices),
                                "notice_examples": list(dict.fromkeys(notices))[:10],
                                "seconds": round(perf_counter() - attempt_started, 3),
                                "read_seconds": round(read_seconds, 3),
                                "material_seconds": round(material_seconds, 3),
                                "merge_seconds": round(merge_seconds, 3),
                                "write_seconds": round(perf_counter() - phase_started, 3),
                            })
                            state["rows"] += len(rows)
                            state["cache_hits"], state["cache_misses"] = cache.hits, cache.misses
                            publish(f"已整理 {state['rows']:,} 行数据")
                    except ExportSizeError as error:
                        if len(lots) == 1:
                            raise ExportSizeError(f"单个系统 LOT {lots[0][index]} 超出上限，不能截断记录：{error}") from error
                        publish(f"本批 {len(lots)} 个 LOT 超限，正在自动拆小")
                        middle = len(lots) // 2
                        process(fields, lots[:middle])
                        process(fields, lots[middle:])

                last_lot = ""
                while True:
                    check()
                    publish("正在读取下一批 E 开头系统 LOT")
                    statement = sql.SQL(
                        "SELECT * FROM {} WHERE ({}) AND lot_no LIKE %s AND lot_no NOT LIKE %s AND lot_no > %s ORDER BY lot_no LIMIT %s"
                    ).format(sql.Identifier(schema, LOT), where)
                    lookup_started = perf_counter()
                    with read_rows(connection, statement, [*parameters, "E%", "%E%E%", last_lot, batch_size + 1]) as (fields, records):
                        validate_columns(LOT, fields)
                        lots = list(records)
                    state["lot_lookup_seconds"] += perf_counter() - lookup_started
                    if not lots:
                        break
                    index = fields.index("lot_no")
                    keys = [str(row[index]) for row in lots]
                    if len(keys) != len(set(keys)) or any(not is_system_lot(key) for key in keys):
                        raise ValueError("批次表存在重复 LOT 或 LOT 不是仅以一个 E 开头，不能可靠分页。")
                    page = lots[:batch_size]
                    # 只统计本页实际处理的 LOT；多取的一条用于判断下一页，
                    # 超限后的拆批重试也不能重复累计这个数量。
                    state["found_lots"] += len(page)
                    publish(f"已找到 {state['found_lots']:,} 个系统 LOT，正在整理关联数据")
                    process(fields, page)
                    last_lot = page[-1][index]
                    if len(lots) <= batch_size:
                        break
            finally:
                cache.close()
        # 先释放数据库快照，再在本地拼接，避免文件处理延长数据库事务。
        if state["parts"]:
            state["phase"] = "joining"
            state["joined_rows"] = 0
            publish("查询完成，正在准备完整 CSV")

            def join_progress(done: int, total: int) -> None:
                state["joined_rows"] = done
                publish(f"正在生成完整 CSV：{done:,}/{total:,} 行")

            join_started = perf_counter()
            state["final_csv"] = join_parts(directory, state["parts"], check, join_progress)
            state["join_seconds"] = round(perf_counter() - join_started, 3)

        state["lot_lookup_seconds"] = round(state["lot_lookup_seconds"], 3)
        state["status"] = "complete"
        state["phase"] = "complete"
        publish("导出完成，已生成一份完整宽表" if state["rows"] else "没有符合条件的 E 开头系统 LOT")
        return state
    except (Exception, KeyboardInterrupt) as error:
        state["status"] = "cancelled" if isinstance(error, (ExportCancelled, KeyboardInterrupt)) else "failed"
        state["error"] = error_message(error)
        publish(state["error"])
        raise
