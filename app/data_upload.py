"""发现陌生文件结构并显式映射到经营数据契约，上传数据独立保存。"""
import csv
import json
import shutil
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from openpyxl import load_workbook

from app.config import DATA_DIR
from app.import_data import import_tables
from app.metadata import FIELD_MEANINGS
from app.source import SCHEMA, mapped_source_rows
from app import light_import


def discover(files: list[str]) -> dict:
    tables = {}
    for filename in files:
        path = Path(filename)
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                sample = []
                for _, row in zip(range(3), reader):
                    sample.append(row)
                tables[path.name] = {"file": path.name, "sheet": None, "columns": reader.fieldnames or [], "sample": sample}
        elif path.suffix.lower() == ".xlsx":
            book = load_workbook(path, read_only=True, data_only=True)
            for sheet in book:
                rows = sheet.iter_rows(values_only=True)
                headers = [str(value) for value in next(rows, ())]
                tables[f"{path.name}:{sheet.title}"] = {"file": path.name, "sheet": sheet.title, "columns": headers,
                    "sample": [dict(zip(headers, row)) for _, row in zip(range(3), rows)]}
            book.close()
        else:
            raise ValueError("上传仅支持CSV和XLSX")
    return tables


def suggest_mapping(structure: dict, template="full") -> dict:
    result = {"tables": {}}
    light = template == light_import.TEMPLATE
    if light:
        result.update(template=template, amount_mode="line", currency="CNY", status_map={})
    for table, fields in (light_import.FIELDS if light else SCHEMA).items():
        matches = [info for info in structure.values() if info["sheet"] == table or Path(info["file"]).stem == table]
        if not matches:
            scores = [(sum(field in info["columns"] or FIELD_MEANINGS.get(field, field) in info["columns"] for field in fields), info) for info in structure.values()]
            highest = max((score for score, _ in scores), default=0)
            matches = [info for score, info in scores if score == highest] if highest >= max(2, len(light_import.REQUIRED[table] if light else fields)//2) else []
        if light and table not in {"orders", "order_items"} and len(matches) != 1:
            continue
        if len(matches) == 1:
            info = matches[0]
            columns = {field: field if field in info["columns"] else FIELD_MEANINGS.get(field, field) if FIELD_MEANINGS.get(field, field) in info["columns"] else "" for field in fields}
            result["tables"][table] = {"file": info["file"], "sheet": info["sheet"], "columns": columns}
        else:
            result["tables"][table] = {"file": "", "sheet": None, "columns": {field: "" for field in fields}}
    return result


def import_upload(files: list[str], mapping: dict, name: str, data_dir: Path = DATA_DIR, **version) -> dict:
    light = mapping.get("template") == light_import.TEMPLATE
    capabilities = light_import.contract(mapping) if light else None
    if not light and set(mapping["tables"]) != set(SCHEMA):
        raise ValueError("经营分析需要完整9张业务表；请在映射中明确每张表，不能用合成空表补齐")
    parent = version.get("parent_dataset_id")
    if light and parent and not version.get("delivery_source_id"):
        from app.semantic import load_manifest
        previous = load_manifest(parent, data_dir)
        if previous.get("capabilities", {}).get("template") == light_import.TEMPLATE:
            version["delivery_source_id"] = previous.get("delivery_source_id") or parent
    paths = {Path(filename).name: Path(filename) for filename in files}
    if len(paths) != len(files):
        raise ValueError("上传文件名不能重复")
    tables = {}
    for table in mapping["tables"]:
        config = mapping["tables"][table]
        columns = config["columns"]
        if not light and (set(columns) != set(SCHEMA[table]) or not all(columns.values())):
            raise ValueError(f"{table}需要逐项映射完整字段：{list(SCHEMA[table])}")
        path = paths[config["file"]]
        tables[table] = [{field: row[source] for field, source in columns.items() if source}
                         for row in mapped_source_rows(path, config["sheet"])]
    if light:
        light_import.normalize_input(tables, capabilities)
    dataset_id = "upload_" + uuid4().hex[:12]
    directory = data_dir / "uploads" / dataset_id
    directory.mkdir(parents=True)
    with ExitStack() as rollback:
        rollback.callback(shutil.rmtree, directory)
        copied = []
        for path in paths.values():
            target = directory / path.name
            shutil.copyfile(path, target)
            copied.append(target)
        (directory / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        from app.calculation import ROUND2_RULE
        version.setdefault("quality_rule_version", ROUND2_RULE)
        rollback.callback(shutil.rmtree, data_dir / "sources" / dataset_id, ignore_errors=True)
        for suffix in (".manifest.tmp", ".duckdb", ".duckdb.wal"):
            rollback.callback((data_dir / (dataset_id + suffix)).unlink, missing_ok=True)
        manifest = import_tables(dataset_id, tables, [*copied, directory / "mapping.json"], name or dataset_id, data_dir,
                                 practice={"year_context": None, "min_seller_orders": 1, "questions": []},
                                 capabilities=capabilities, entity_aliases=mapping.get("entity_aliases"), **({"data_origin": "uploaded"} | version))
        rollback.pop_all()
    return manifest


def inspect_mapping(files, template):
    structure = discover(files)
    mapping = suggest_mapping(structure, template)
    if template == light_import.TEMPLATE:
        spec = mapping["tables"]["orders"]
        status = spec["columns"]["order_status"]
        if status:
            paths = {Path(f).name: Path(f) for f in files}
            values = {str(row[status]) for row in mapped_source_rows(paths[spec["file"]], spec["sheet"]) if row[status]}
            key = f"{spec['file']}:{spec['sheet']}" if spec["sheet"] else spec["file"]
            structure[key]["status_values"] = {status: sorted(values)}
    return structure, mapping


def registered_manifests(data_dir: Path):
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in data_dir.glob("*.manifest.json")]
    return sorted(manifests, key=lambda m: m.get("imported_at", ""), reverse=True)


def dataset_label(manifest):
    return manifest["name"] + (" · " + manifest["dataset_id"][-6:] if manifest.get("source_kind") else "")


def registered_datasets(data_dir: Path = DATA_DIR) -> dict[str, str]:
    return {m["dataset_id"]: dataset_label(m) for m in registered_manifests(data_dir)}
