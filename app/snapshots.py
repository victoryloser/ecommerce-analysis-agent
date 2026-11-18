"""不可变快照的入口别名、来源留存与稳定编码。"""
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from app.config import DATA_DIR, ROOT

FINGERPRINT_VERSION = "typed-json-sha256-v1"


def encode(value):
    if isinstance(value, Decimal):
        return {"decimal": format(value, ".2f")}
    if isinstance(value, (date, datetime)):
        return {"date": value.isoformat()}
    raise TypeError(type(value).__name__)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=encode)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def implementation_hash():
    return hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in sorted((ROOT / "app").glob("*.py")))).hexdigest()


def quality_signature(row):
    from app.source import SCHEMA
    candidates = json.loads(row["candidates"], parse_float=Decimal)
    normalized = []
    for candidate in candidates:
        values = []
        kinds = list(SCHEMA[row["table_name"]].values())
        for index, value in enumerate(candidate):
            kind = kinds[index] if index < len(kinds) else "VARCHAR"
            if value is not None and kind.startswith("DECIMAL"):
                value = Decimal(str(value))
            elif value is not None and kind == "TIMESTAMP":
                value = datetime.fromisoformat(str(value))
            values.append(value)
        normalized.append(canonical(values))
    return {"candidates": sorted(set(normalized)),
            "duplicate_rows": len(json.loads(row["source_rows"])) - len(candidates) if row["reason"] == "exact_duplicate" else 0}


def table_fingerprints(conn, schema=None):
    from app.source import SCHEMA, PRIMARY_KEYS
    result = {}
    for table, fields in (schema or SCHEMA).items():
        fingerprint = hashlib.sha256(canonical([table, fields]).encode())
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {','.join(PRIMARY_KEYS[table])}")
        while rows := cursor.fetchmany(1000):
            for row in rows:
                fingerprint.update((canonical(row) + "\n").encode())
        result[table] = fingerprint.hexdigest()
    cursor = conn.execute("SELECT * FROM quality_issues")
    columns = [c[0] for c in cursor.description]
    issues = []
    for values in cursor.fetchall():
        row = dict(zip(columns, values))
        issues.append([row["table_name"], json.loads(row["business_key"]), row["field_name"], row["reason"], quality_signature(row)])
    result["quality"] = digest(sorted(issues, key=canonical))
    return result


def resolve_dataset(name, data_dir=DATA_DIR):
    path = data_dir / "aliases.json"
    aliases = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return aliases.get(name, name)


def publish_alias(name, dataset_id, data_dir):
    path = data_dir / "aliases.json"
    aliases = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    aliases[name] = dataset_id
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def source_path(entry, data_dir=DATA_DIR):
    return data_dir / entry["retained_file"] if "retained_file" in entry else ROOT / entry["source_file"]


def read_retained(manifest, data_dir=DATA_DIR):
    import csv
    from openpyxl import load_workbook
    from app.source import SCHEMA
    paths = {}
    for entry in manifest["sources"]:
        path = source_path(entry, data_dir)
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("留存来源与快照哈希不一致")
        paths[path.name] = path
    mapping = json.loads(paths["mapping.json"].read_text(encoding="utf-8")) if "mapping.json" in paths else None
    tables = {}
    if mapping:
        from app.source import mapped_source_rows
        for table, spec in mapping["tables"].items():
            path = paths[spec["file"]]
            tables[table] = [{field: row[column] for field, column in spec["columns"].items() if column}
                             for row in mapped_source_rows(path, spec["sheet"])]
    else:
        for path in paths.values():
            if path.suffix.lower() == ".csv":
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    tables[path.stem] = list(csv.DictReader(handle))
            elif path.suffix.lower() == ".xlsx":
                book = load_workbook(path, read_only=True, data_only=True)
                for sheet in book:
                    values = sheet.iter_rows(values_only=True)
                    headers = next(values)
                    if sheet.title in SCHEMA:
                        tables[sheet.title] = [dict(zip(headers, row)) for row in values]
                    elif headers and headers[0] == "question_id":
                        tables["practice_questions"] = [dict(zip(headers, row)) for row in values]
                book.close()
    return tables
