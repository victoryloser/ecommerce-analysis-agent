import argparse
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import duckdb

from app.config import DATA_DIR, DATASETS, METADATA_VERSION, ROOT
from app.semantic import metadata_hash
from app.source import FOREIGN_KEYS, PRIMARY_KEYS, SCHEMA, read_source, source_files


def import_dataset(dataset_id: str) -> dict:
    tables = read_source(dataset_id)
    return import_tables(dataset_id, tables, source_files(dataset_id), DATASETS[dataset_id], DATA_DIR)


def import_tables(dataset_id: str, tables: dict, sources: list, name: str, data_dir=DATA_DIR, *, practice=None) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{dataset_id}.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute("BEGIN TRANSACTION")
        for table, columns in SCHEMA.items():
            definition = ", ".join(f'"{name}" {kind}' for name, kind in columns.items())
            key = ", ".join(PRIMARY_KEYS[table])
            conn.execute(f"CREATE OR REPLACE TABLE {table} ({definition}, PRIMARY KEY ({key}))")
            values = []
            for row in tables[table]:
                converted = []
                for column_name, kind in columns.items():
                    value = row[column_name]
                    if value is None or value == "":
                        value = None
                    elif kind.startswith("DECIMAL"):
                        value = Decimal(str(value))
                    elif kind == "VARCHAR":
                        value = str(value)
                    converted.append(value)
                values.append(converted)
            if values:
                conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", values)
        for child, field, parent, key in FOREIGN_KEYS:
            missing = conn.execute(f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON c.{field}=p.{key} WHERE p.{key} IS NULL").fetchone()[0]
            if missing:
                raise ValueError(f"{child}.{field} 存在 {missing} 个无对应主记录的值")
        quality = {
            "duplicate_review_orders": conn.execute("SELECT count(*) FROM (SELECT order_id FROM order_reviews GROUP BY order_id HAVING count(*)>1)").fetchone()[0],
            "delivered_missing_timing": conn.execute("SELECT count(*) FROM orders WHERE order_status='delivered' AND (delivered_at IS NULL OR estimated_delivery_at IS NULL)").fetchone()[0],
        }
        earliest, latest = conn.execute("SELECT min(purchase_at), max(purchase_at) FROM orders").fetchone()
        if earliest is None or latest is None:
            raise ValueError("订单表至少需要一笔包含有效下单时间的订单。")
        categories = conn.execute("SELECT category_id, category_name_zh FROM categories ORDER BY category_id").fetchall()
        sellers = conn.execute("SELECT seller_id, seller_name FROM sellers ORDER BY seller_id").fetchall()
        states = [r[0] for r in conn.execute("SELECT DISTINCT customer_state FROM customers ORDER BY customer_state").fetchall()]
        statuses = [r[0] for r in conn.execute("SELECT DISTINCT order_status FROM orders ORDER BY order_status").fetchall()]
        coverage_fields = {}
        for field in ("purchase_at", "approved_at", "shipped_at", "delivered_at", "estimated_delivery_at"):
            first, last = conn.execute(f"SELECT MIN({field}),MAX({field}) FROM orders").fetchone()
            if first is not None:
                next_year, next_month = (last.year+1, 1) if last.month == 12 else (last.year, last.month+1)
                coverage_fields[field] = {"start": first.replace(day=1).date().isoformat(), "end": f"{next_year:04d}-{next_month:02d}-01"}
        conn.execute("COMMIT")
    # 期间按月给出覆盖边界，具体最早/最晚记录另存，不把月内无订单误判为未覆盖。
    end_year, end_month = (latest.year + 1, 1) if latest.month == 12 else (latest.year, latest.month + 1)
    manifest = {
        "dataset_id": dataset_id, "name": name, "metadata_version": METADATA_VERSION,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "sources": [{"source_file": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources],
        "schema_sha256": hashlib.sha256(json.dumps(SCHEMA, sort_keys=True).encode()).hexdigest(),
        "metadata_sha256": metadata_hash(),
        "row_counts": {table: len(tables[table]) for table in SCHEMA}, "quality": quality,
        "coverage": {"start": earliest.replace(day=1).date().isoformat(), "end": f"{end_year:04d}-{end_month:02d}-01", "first_record": str(earliest), "last_record": str(latest)},
        "coverage_fields": coverage_fields,
        "entities": {"categories": categories, "sellers": sellers, "customer_states": states, "order_statuses": statuses},
        "practice": practice or {"year_context": 2025, "min_seller_orders": 10 if dataset_id == "excel_full" else 3, "questions": tables.get("practice_questions", [])},
    }
    manifest["database_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (data_dir / f"{dataset_id}.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="导入 Excel / CSV 数据；启动应用前执行")
    parser.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
    args = parser.parse_args()
    for dataset in DATASETS if args.dataset == "all" else [args.dataset]:
        manifest = import_dataset(dataset)
        print(f"{dataset}: {manifest['row_counts']}; quality={manifest['quality']}")


if __name__ == "__main__":
    main()
