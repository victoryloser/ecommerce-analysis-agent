"""Locate retained source records; candidate sets are never zipped with row IDs."""
import json
from app.snapshots import read_retained, source_path
from app.semantic import PlanError


def source_records(executor, session, order_id):
    if session.view_focus.get("kind") == "quality":
        record = session.focused_evidence()
    else:
        record = session.analysis_evidence
    result = record.get("result", {})
    if "order_id" not in result.get("columns", []):
        raise PlanError("请从问题明细中选择一个订单")
    index = result["columns"].index("order_id")
    if order_id not in {str(row[index]) for row in result["rows"]}:
        raise PlanError("订单不在当前问题明细页中")
    return {"source_record_id": record["record_id"], **retained_order_records(executor, order_id)}


def retained_order_records(executor, order_id):
    manifest = executor.manifest
    if not manifest["sources"]:
        return {"order_id": order_id, "records": [], "note": "该构造快照没有留存原文件；可查业务键及候选证据。"}
    directory = executor.path.parent
    tables = read_retained(manifest, directory)
    files = {source_path(entry, directory).name: source_path(entry, directory) for entry in manifest["sources"]}
    mapping = json.loads(files["mapping.json"].read_text(encoding="utf-8")) if "mapping.json" in files else None
    records = []
    for table in ("orders", "order_items", "order_payments"):
        spec = mapping["tables"][table] if mapping else None
        filename = spec["file"] if spec else next((name for name in files if name == table + '.csv' or name.endswith('.xlsx')), None)
        sheet = spec.get("sheet") if spec else table if filename and filename.endswith('.xlsx') else None
        for number, row in enumerate(tables.get(table, []), 2):
            if str(row.get("order_id")) == order_id:
                records.append({"table": table, "file": filename, "sheet": sheet,
                    "record_number": number, "locator_kind": "worksheet_row" if sheet else "logical_csv_record", "values": row})
    return {"order_id": order_id, "records": records,
            "payment_missing": not any(r['table'] == 'order_payments' for r in records),
            "note": "逐条回读经过哈希验证的原记录；CSV序号为含表头的逻辑记录，不是物理行号。支付缺失由订单及不存在支付的查询证实。"}
