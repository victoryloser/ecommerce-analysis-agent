"""发现陌生文件结构并显式映射到经营数据契约，上传数据独立保存。"""
import csv
import json
import shutil
from pathlib import Path
from uuid import uuid4

from openpyxl import load_workbook

from app.config import DATA_DIR
from app.import_data import import_tables
from app.metadata import FIELD_MEANINGS
from app.source import SCHEMA


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
                tables[path.name] = {"file": path.name, "sheet": None, "columns": reader.fieldnames, "sample": sample}
        elif path.suffix.lower() == ".xlsx":
            book = load_workbook(path, read_only=True, data_only=True)
            for sheet in book:
                rows = sheet.iter_rows(values_only=True)
                headers = [str(value) for value in next(rows)]
                tables[f"{path.name}:{sheet.title}"] = {"file": path.name, "sheet": sheet.title, "columns": headers,
                    "sample": [dict(zip(headers, row)) for _, row in zip(range(3), rows)]}
            book.close()
        else:
            raise ValueError("上传仅支持CSV和XLSX")
    return tables


def suggest_mapping(structure: dict) -> dict:
    result = {"tables": {}}
    for table, fields in SCHEMA.items():
        matches = [info for info in structure.values() if info["sheet"] == table or Path(info["file"]).stem == table]
        if not matches:
            scores = [(sum(field in info["columns"] or FIELD_MEANINGS[field] in info["columns"] for field in fields), info) for info in structure.values()]
            highest = max((score for score, _ in scores), default=0)
            matches = [info for score, info in scores if score == highest] if highest >= max(2, len(fields)//2) else []
        if len(matches) == 1:
            info = matches[0]
            columns = {field: field if field in info["columns"] else FIELD_MEANINGS[field] if FIELD_MEANINGS[field] in info["columns"] else "" for field in fields}
            result["tables"][table] = {"file": info["file"], "sheet": info["sheet"], "columns": columns}
        else:
            result["tables"][table] = {"file": "", "sheet": None, "columns": {field: "" for field in fields}}
    return result


def import_upload(files: list[str], mapping: dict, name: str, data_dir: Path = DATA_DIR, **version) -> dict:
    if set(mapping["tables"]) != set(SCHEMA):
        raise ValueError("经营分析需要完整9张业务表；请在映射中明确每张表，不能用合成空表补齐")
    paths = {Path(filename).name: Path(filename) for filename in files}
    if len(paths) != len(files):
        raise ValueError("上传文件名不能重复")
    tables = {}
    for table, fields in SCHEMA.items():
        config = mapping["tables"][table]
        columns = config["columns"]
        if set(columns) != set(fields) or not all(columns.values()):
            raise ValueError(f"{table}需要逐项映射完整字段：{list(fields)}")
        path = paths[config["file"]]
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        else:
            book = load_workbook(path, read_only=True, data_only=True)
            values = book[config["sheet"]].iter_rows(values_only=True)
            headers = [str(value) for value in next(values)]
            rows = [dict(zip(headers, row)) for row in values]
            book.close()
        tables[table] = [{field: row[source] for field, source in columns.items()} for row in rows]
    dataset_id = "upload_" + uuid4().hex[:12]
    directory = data_dir / "uploads" / dataset_id
    directory.mkdir(parents=True)
    copied = []
    for path in paths.values():
        target = directory / path.name
        shutil.copyfile(path, target)
        copied.append(target)
    (directory / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    from app.calculation import ROUND2_RULE
    version.setdefault("quality_rule_version", ROUND2_RULE)
    return import_tables(dataset_id, tables, [*copied, directory / "mapping.json"], name or dataset_id, data_dir,
                         practice={"year_context": None, "min_seller_orders": 1, "questions": []},
                         **({"data_origin": "uploaded"} | version))


def registered_datasets(data_dir: Path = DATA_DIR) -> dict[str, str]:
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in data_dir.glob("*.manifest.json")]
    return {m["dataset_id"]: m["name"] + (" · " + m["dataset_id"][-6:] if m.get("source_kind") else "")
            for m in sorted(manifests, key=lambda m: m.get("imported_at", ""), reverse=True)}
