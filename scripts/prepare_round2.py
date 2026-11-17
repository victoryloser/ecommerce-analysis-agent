"""Import an external public pack into explicit immutable snapshots for R2."""
import argparse
import csv
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.calculation import ROUND2_RULE
from app.config import DATA_DIR
from app.import_data import import_tables
from app.round2_suite import ROLES
from app.source import SCHEMA


def prepare(package, data_dir, source_format):
    mapping = {}
    for role in ROLES:
        folder = package/"数据集"/role
        tables = {}
        if source_format == "csv":
            files = [folder/(table+".csv") for table in SCHEMA]
            for table, path in zip(SCHEMA, files):
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    tables[table] = list(csv.DictReader(handle))
        else:
            from openpyxl import load_workbook
            files = [folder/(role+".xlsx")]
            book = load_workbook(files[0], read_only=True, data_only=True)
            for table in SCHEMA:
                values = book[table].iter_rows(values_only=True)
                headers = next(values)
                tables[table] = [dict(zip(headers, row)) for row in values]
            book.close()
        snapshot = "round2_"+role.lower()+"_"+uuid4().hex[:12]
        manifest = import_tables(snapshot, tables, files, role, data_dir, quality_rule_version=ROUND2_RULE,
            practice={"year_context": None, "min_seller_orders": 1, "questions": []},
            data_origin="uploaded", source_kind=source_format, business_label=role)
        mapping[role] = manifest["dataset_id"]
    return mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--format", choices=["csv", "xlsx"], default="csv")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("映射文件已存在，请指定新文件保留原快照身份")
    mapping = prepare(args.package, args.data_dir, args.format)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
