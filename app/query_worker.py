"""每个查询一个子进程，超时后终止并回收。"""
import multiprocessing as mp
import csv
from decimal import DecimalException
from pathlib import Path
from uuid import uuid4

import duckdb

from app.config import MAX_ROWS, QUERY_TIMEOUT
from app.models import QueryResult
from app.sql_compiler import CompiledQuery
from app.quality import decode_quality
from app.result_math import resolve_ratios


def _query(path: str, compiled: CompiledQuery, pipe, offset=0, limit=MAX_ROWS, export_path=None) -> None:
    try:
        with duckdb.connect(path, read_only=True, config={
            "enable_external_access": "false", "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false", "memory_limit": "256MB", "threads": "2",
        }) as conn:
            if export_path is not None:
                cursor = conn.execute(compiled.sql, compiled.parameters)
                columns = [col[0] for col in cursor.description]
                count = 0
                with open(export_path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(columns)
                    while rows := cursor.fetchmany(1000):
                        columns_visible, visible, quality = decode_quality(columns, rows)
                        resolve_ratios(columns_visible, visible, compiled.ratios)
                        # Preserve the exported quality column and its original position.
                        if "__quality" in columns:
                            pos = columns.index("__quality")
                            for row, raw in zip(visible, rows):
                                row.insert(pos, raw[pos])
                        writer.writerows(visible)
                        count += len(rows)
                pipe.send({"columns": columns, "rows": [], "truncated": False, "export_file": export_path, "row_count": count})
            else:
                if offset or limit != MAX_ROWS:
                    cursor = conn.execute(f"SELECT * FROM ({compiled.sql}) AS result_page LIMIT ? OFFSET ?", (*compiled.parameters, limit+1, offset))
                else:
                    cursor = conn.execute(compiled.sql, compiled.parameters)
                rows = cursor.fetchmany(limit + 1)
                columns, visible, quality = decode_quality([col[0] for col in cursor.description], rows[:limit])
                ratios = resolve_ratios(columns, visible, compiled.ratios)
                pipe.send({"columns": columns, "rows": visible, "quality": quality, "ratios": ratios, "truncated": len(rows) > limit, "row_offset": offset})
    except (duckdb.Error, ValueError, DecimalException) as error:
        # 数据库错误跨进程传回；不改 SQL、不换库、不返回伪造的 0。
        pipe.send({"error": str(error)})
    finally:
        pipe.close()


def run_query(path: Path, compiled: CompiledQuery, timeout: float = QUERY_TIMEOUT, *, offset: int = 0, limit: int = MAX_ROWS, export_path: Path | None = None) -> QueryResult:
    if offset < 0 or not 1 <= limit <= MAX_ROWS:
        raise ValueError("分页偏移必须非负，每页1至500行")
    context = mp.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_query, args=(str(path), compiled, writer, offset, limit, str(export_path) if export_path else None))
    query_id = uuid4().hex
    process.start()
    writer.close()
    try:
        if not reader.poll(timeout):
            return QueryResult(query_id=query_id, status="timeout", error=f"查询超过 {timeout} 秒，已终止")
        payload = reader.recv()
        if "error" in payload:
            return QueryResult(query_id=query_id, status="error", error=payload["error"])
        return QueryResult(query_id=query_id, status="ok" if payload["rows"] or payload.get("row_count") else "empty", **payload)
    finally:
        if process.is_alive():
            process.terminate()
        process.join()
        process.close()
        reader.close()
