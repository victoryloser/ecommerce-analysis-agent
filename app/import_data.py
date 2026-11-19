import argparse
import hashlib
import json
import shutil
from uuid import uuid4
from datetime import datetime, timezone
from decimal import Decimal

import duckdb

from app.config import DATA_DIR, DATASETS, METADATA_VERSION, ROOT
from app.semantic import metadata_hash
from app.source import FOREIGN_KEYS, PRIMARY_KEYS, SCHEMA, read_source, source_files
from app.quality import ISSUE_SCHEMA, normalize_rows
from app.calculation import CalculationContext, LEGACY_RULE
from app.snapshots import FINGERPRINT_VERSION, file_sha256, publish_alias, table_fingerprints


def import_dataset(dataset_id: str, data_dir=DATA_DIR) -> dict:
    tables = read_source(dataset_id)
    snapshot = dataset_id + "_" + uuid4().hex[:12]
    manifest = import_tables(snapshot, tables, source_files(dataset_id), DATASETS[dataset_id], data_dir,
        practice={"year_context": 2025, "min_seller_orders": 10 if dataset_id == "excel_full" else 3,
                  "questions": tables.get("practice_questions", [])}, source_kind=dataset_id, data_origin="practice")
    publish_alias(dataset_id, snapshot, data_dir)
    return manifest


def import_tables(dataset_id: str, tables: dict, sources: list, name: str, data_dir=DATA_DIR, *, practice=None,
                  business_label="", parent_dataset_id=None, change_note="", data_origin="constructed", source_kind=None, quality_rule_version=LEGACY_RULE,
                  delivery_source_id=None, capabilities=None, entity_aliases=None) -> dict:
    context = CalculationContext(quality_rule_version, capabilities)
    schema = SCHEMA
    if capabilities:
        from app.light_import import ITEM_SCHEMA
        schema = {table: ITEM_SCHEMA if table == "order_items" else SCHEMA[table] for table in capabilities["fields"]}
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{dataset_id}.duckdb"
    if path.exists() or (data_dir / f"{dataset_id}.manifest.json").exists():
        raise ValueError("快照 ID 已存在；请使用新 ID 导入，不能覆盖已发布数据")
    if parent_dataset_id and not (data_dir / f"{parent_dataset_id}.manifest.json").exists():
        raise ValueError("声明的父快照不存在")
    retained = []
    if sources:
        directory = data_dir / "sources" / dataset_id
        directory.mkdir(parents=True)
        for source in sources:
            target = directory / source.name
            shutil.copyfile(source, target)
            retained.append({"source_file": str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else source.name,
                             "retained_file": target.relative_to(data_dir).as_posix(),
                             "sha256": file_sha256(target)})
    with duckdb.connect(str(path)) as conn:
        conn.execute("BEGIN TRANSACTION")
        issues = []
        normalized_counts = {}
        for table, columns in schema.items():
            definition = ", ".join(f'"{name}" {kind}' for name, kind in columns.items())
            key = ", ".join(PRIMARY_KEYS[table])
            conn.execute(f"CREATE OR REPLACE TABLE {table} ({definition}, PRIMARY KEY ({key}))")
            values = []
            for row in tables[table]:
                converted = []
                for column_name, kind in columns.items():
                    value = row.get(column_name) if capabilities else row[column_name]
                    if value is None or value == "":
                        value = None
                    elif kind.startswith("DECIMAL"):
                        value = Decimal(str(value))
                    elif kind == "VARCHAR":
                        value = str(value)
                    elif kind == "INTEGER":
                        value = int(value)
                    elif kind == "DOUBLE":
                        value = float(value)
                    elif kind == "TIMESTAMP" and isinstance(value, str):
                        value = datetime.fromisoformat(value)
                    converted.append(value)
                values.append(converted)
            supplied = capabilities["fields"][table] if capabilities else None
            if capabilities and table == "order_items" and capabilities["amount_mode"] == "unknown":
                supplied = set(supplied) - {"price"}
            values, table_issues = normalize_rows(table, values, columns, supplied)
            issues.extend(table_issues)
            normalized_counts[table] = len(values)
            if values:
                conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", values)
        missing_payments = conn.execute("SELECT o.order_id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM order_payments p WHERE p.order_id=o.order_id) ORDER BY o.order_id").fetchall() if "order_payments" in schema else []
        issues.extend(("order_payments", oid, json.dumps([oid]), "payment_value", "missing_record", "[]", "[]") for (oid,) in missing_payments)
        conn.execute("CREATE OR REPLACE TABLE quality_issues (" + ",".join(f"{field} {kind}" for field, kind in ISSUE_SCHEMA.items()) + ")")
        if issues:
            conn.executemany("INSERT INTO quality_issues VALUES (?,?,?,?,?,?,?,?)", [(index, *issue) for index, issue in enumerate(issues, 1)])
        for child, field, parent, key in FOREIGN_KEYS:
            if child not in schema or parent not in schema:
                continue
            missing = conn.execute(f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON c.{field}=p.{key} WHERE p.{key} IS NULL").fetchone()[0]
            if missing:
                raise ValueError(f"{child}.{field} 存在 {missing} 个无对应主记录的值")
        quality = {
            "issue_count": len(issues),
            "missing_payment_orders": len(missing_payments) if "order_payments" in schema else None,
            "duplicate_review_orders": conn.execute("SELECT count(*) FROM (SELECT order_id FROM order_reviews GROUP BY order_id HAVING count(*)>1)").fetchone()[0] if "order_reviews" in schema else None,
            "delivered_missing_timing": None if capabilities else conn.execute("SELECT count(*) FROM orders WHERE order_status='delivered' AND (delivered_at IS NULL OR estimated_delivery_at IS NULL)").fetchone()[0],
        }
        earliest, latest = conn.execute("SELECT min(purchase_at), max(purchase_at) FROM orders").fetchone()
        if earliest is None or latest is None:
            raise ValueError("订单表至少需要一笔包含有效下单时间的订单。")
        if capabilities:
            def item_entities(key, label):
                return conn.execute(f"SELECT {key},coalesce(max({label}),{key}) FROM order_items WHERE {key} IS NOT NULL GROUP BY {key} ORDER BY {key}").fetchall()
            categories = item_entities("category_id", "category_name_zh")
            sellers = item_entities("seller_id", "seller_name")
            states = []
        else:
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
        fingerprints = table_fingerprints(conn, schema)
        conn.execute("COMMIT")
    # 期间按月给出覆盖边界，具体最早/最晚记录另存，不把月内无订单误判为未覆盖。
    end_year, end_month = (latest.year + 1, 1) if latest.month == 12 else (latest.year, latest.month + 1)
    manifest = {
        "dataset_id": dataset_id, "name": name, "metadata_version": METADATA_VERSION,
        "business_label": business_label or name, "parent_dataset_id": parent_dataset_id, "change_note": change_note,
        "data_origin": data_origin, "source_kind": source_kind,
        "fingerprint_version": FINGERPRINT_VERSION, "semantic_fingerprints": fingerprints,
        "quality_rule_version": context.rule_version, "normalized_row_counts": normalized_counts,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "sources": retained,
        "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        "metadata_sha256": metadata_hash(context),
        "row_counts": {table: len(tables[table]) for table in schema}, "quality": quality,
        "coverage": {"start": earliest.replace(day=1).date().isoformat(), "end": f"{end_year:04d}-{end_month:02d}-01", "first_record": str(earliest), "last_record": str(latest)},
        "coverage_fields": coverage_fields,
        "entities": {"categories": categories, "sellers": sellers, "customer_states": states, "order_statuses": statuses},
        "practice": practice or {"year_context": 2025, "min_seller_orders": 10 if dataset_id == "excel_full" else 3, "questions": tables.get("practice_questions", [])},
    }
    if capabilities:
        manifest["capabilities"] = capabilities
    if entity_aliases:
        for group, entries in entity_aliases.items():
            if group not in {"sellers", "categories"} or set(entries) - dict(manifest["entities"][group]).keys():
                raise ValueError("别名必须对应当前快照中的商家或品类 ID")
            if not all(isinstance(names, list) and all(isinstance(s, str) and s.strip() for s in names) for names in entries.values()):
                raise ValueError("每个 ID 的别名应为名称数组")
        manifest["entity_aliases"] = entity_aliases
    manifest["database_sha256"] = file_sha256(path)
    if delivery_source_id:
        manifest["delivery_source_id"] = delivery_source_id
    pending = data_dir / f"{dataset_id}.manifest.tmp"
    pending.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(data_dir / f"{dataset_id}.manifest.json")
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
