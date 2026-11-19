import json
from pathlib import Path
import shutil

import duckdb
import pytest
from openpyxl import Workbook, load_workbook

from app import data_upload
from app.data_upload import discover, import_upload, inspect_mapping, registered_datasets, suggest_mapping
from app.delivery import refresh_delivery
from app.executor import Executor
from app.import_data import import_tables
from app.orchestrator import AnalysisAgent
from app.session import Session
from app.snapshots import file_sha256, read_retained
from test_data_onboarding import write_business_files


def test_uploaded_rows_and_evidence_use_one_copy(tmp_path, monkeypatch):
    files = write_business_files("csv", tmp_path)
    mapping = suggest_mapping(discover(files))
    source = tmp_path / "order_items.csv"
    original_rows = data_upload.mapped_source_rows

    def replace_after_parsing(path, *args, **kwargs):
        yield from original_rows(path, *args, **kwargs)
        if path.name == source.name:
            source.write_bytes(source.read_bytes().replace(b"12.50", b"22.50"))

    monkeypatch.setattr(data_upload, "mapped_source_rows", replace_after_parsing)
    directory = tmp_path / "data"
    manifest = import_upload(files, mapping, "首次交付", directory)
    assert b"22.50" in source.read_bytes()
    with duckdb.connect(str(directory / (manifest["dataset_id"] + ".duckdb")), read_only=True) as db:
        calculated = db.execute("SELECT price FROM order_items").fetchone()[0]
    assert read_retained(manifest, directory)["order_items"][0]["price"] == str(calculated) == "12.50"


def test_import_collision_preserves_registered_file(tmp_path):
    files = write_business_files("csv", tmp_path)
    mapping = suggest_mapping(discover(files))
    directory = tmp_path / "data"
    original = import_upload(files, mapping, "上传", directory)
    existing = import_tables("registered", read_retained(original, directory), [Path(files[0])], "已注册", directory)
    with pytest.raises(ValueError, match="快照 ID 已存在"):
        import_upload(files, mapping, "重复编号", directory, dataset_id="registered")
    assert file_sha256(directory / "registered.duckdb") == existing["database_sha256"]
    assert (directory / existing["sources"][0]["retained_file"]).is_file()


def test_numeric_excel_order_status_preview_and_import(tmp_path):
    source = tmp_path / "business.xlsx"
    book = Workbook()
    orders = book.active
    orders.title = "orders"
    orders.append(["order_id", "purchase_at", "order_status"])
    orders.append(["first", "2025-01-02", 0])
    orders.append(["second", "2025-01-03", 1])
    items = book.create_sheet("order_items")
    items.append(["order_id", "order_item_id", "price"])
    items.append(["first", "x", "12.00"])
    items.append(["second", "x", "25.00"])
    book.save(source)
    book.close()
    structure, mapping = inspect_mapping([source], "orders_items")
    assert structure["business.xlsx:orders"]["status_values"]["order_status"] == ["0", "1"]
    mapping["status_map"] = {"0": "canceled", "1": "delivered"}
    directory = tmp_path / "data"
    manifest = import_upload([source], mapping, "数值状态", directory)
    with duckdb.connect(str(directory / (manifest["dataset_id"] + ".duckdb")), read_only=True) as db:
        assert db.execute("SELECT order_status FROM orders ORDER BY order_id").fetchall() == [("canceled",), ("delivered",)]


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_invalid_mapped_headers_abort_publication(format, tmp_path):
    files = write_business_files(format, tmp_path)
    mapping = suggest_mapping(discover(files))
    if format == "csv":
        items = tmp_path / "order_items.csv"
        lines = items.read_text(encoding="utf-8-sig").splitlines()
        items.write_text(lines[0] + ",price\n" + lines[1] + ",90.00\n", encoding="utf-8")
        field = "price"
    else:
        source = Path(files[0])
        book = load_workbook(source)
        sheet = book["order_payments"]
        sheet.delete_rows(2)
        sheet.cell(1, 5).value = "renamed"
        book.save(source)
        book.close()
        field = "payment_value"
    directory = tmp_path / "data"
    with pytest.raises(ValueError, match=field):
        import_upload(files, mapping, "有问题的交付", directory)
    assert not registered_datasets(directory)


def test_truncated_workbook_explains_how_to_retry_and_rolls_back(tmp_path):
    files = write_business_files("xlsx", tmp_path)
    mapping = suggest_mapping(discover(files))
    source = Path(files[0])
    source.write_bytes(source.read_bytes()[:100])
    directory = tmp_path / "data"
    with pytest.raises(ValueError, match="重新导出并上传"):
        inspect_mapping(files, "full")
    with pytest.raises(ValueError, match="重新导出并上传"):
        import_upload(files, mapping, "不完整的导出", directory)
    assert not registered_datasets(directory)
    assert not list((directory / "uploads").iterdir())


def test_sync_already_uploaded_version_and_declined_new_version(tmp_path, monkeypatch):
    files = write_business_files("csv", tmp_path)
    mapping = suggest_mapping(discover(files))
    directory = tmp_path / "data"
    monkeypatch.setenv("DATA_DELIVERY_DIR", str(tmp_path / "incoming"))
    old = import_upload(files, mapping, "首次交付", directory)["dataset_id"]
    items = tmp_path / "order_items.csv"
    items.write_bytes(items.read_bytes().replace(b"12.50", b"17.50"))
    latest = import_upload(files, mapping, "手工新版", directory, parent_dataset_id=old)["dataset_id"]
    agent = AnalysisAgent(None, {old: Executor(old, directory)})
    source_session = Session(old)
    synced, _ = refresh_delivery(agent, source_session, "/sync", allow_target=lambda m: m["dataset_id"] == latest)
    assert synced.dataset_id == latest
    items.write_bytes(items.read_bytes().replace(b"17.50", b"33.50"))
    incoming = tmp_path / "incoming" / old
    for file in files:
        shutil.copy(file, incoming)
    with pytest.raises(PermissionError):
        refresh_delivery(agent, source_session, "/sync", allow_target=lambda m: m["dataset_id"] in (old, latest))
    assert set(registered_datasets(directory)) == {old, latest}
    permitted, _ = refresh_delivery(agent, source_session, "/sync", allow_target=lambda m: m.get("delivery_source_id") == old)
    assert permitted.dataset_id not in {old, latest}
    assert agent.executors[permitted.dataset_id].manifest["parent_dataset_id"] == latest


def test_sync_rejects_unverified_prior_mapping(tmp_path, monkeypatch):
    files = write_business_files("csv", tmp_path)
    mapping = suggest_mapping(discover(files))
    directory = tmp_path / "data"
    old = import_upload(files, mapping, "原版", directory)["dataset_id"]
    monkeypatch.setenv("DATA_DELIVERY_DIR", str(tmp_path / "incoming"))
    incoming = tmp_path / "incoming" / old
    incoming.mkdir(parents=True)
    for file in files:
        shutil.copy(file, incoming)
    items = incoming / "order_items.csv"
    items.write_bytes(items.read_bytes().replace(b"12.50", b"18.50"))
    mapping["tables"]["order_items"]["columns"]["price"] = "freight_value"
    (directory / "sources" / old / "mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    agent = AnalysisAgent(None, {old: Executor(old, directory)})
    session, text = refresh_delivery(agent, Session(old), "/sync")
    assert session.dataset_id == old and "映射" in text and "指纹" in text
    assert set(registered_datasets(directory)) == {old}
