"""首次使用测试：临时生成少量记录，不依赖仓库外的样例文件。"""
import csv
import json
from decimal import Decimal

import pytest
from openpyxl import Workbook

from app.data_upload import discover, import_upload, registered_datasets, suggest_mapping
from app.executor import Executor
from app.models import AnalysisPlan, TimeRange
from app.session import Session
from app.session_store import SessionStore
from app.source import SCHEMA


def write_business_files(file_format, directory):
    """仅用于测试和文档截图，生成虚构记录到指定临时目录。"""
    values = {
        "customers": {"customer_id": "C1", "customer_unique_id": "U1", "customer_state": "测试省"},
        "geolocation": {"zip_code": "00001", "latitude": 30, "longitude": 120},
        "orders": {"order_id": "O1", "customer_id": "C1", "order_status": "delivered",
                   "purchase_at": "2025-01-03 10:00:00", "delivered_at": "2025-01-04 10:00:00",
                   "estimated_delivery_at": "2025-01-05 10:00:00"},
        "order_items": {"order_id": "O1", "order_item_id": 1, "product_id": "P1", "seller_id": "S1",
                        "price": "12.50", "freight_value": "2.00"},
        "order_payments": {"order_id": "O1", "payment_sequence": 1, "payment_value": "14.50"},
        "order_reviews": {"review_id": "R1", "order_id": "O1", "review_score": 5},
        "products": {"product_id": "P1", "category_id": "K1", "product_name": "测试商品"},
        "sellers": {"seller_id": "S1", "seller_name": "测试商家"},
        "categories": {"category_id": "K1", "category_name_zh": "测试品类"},
    }
    book = Workbook()
    book.remove(book.active)
    files = []
    for table, fields in SCHEMA.items():
        row = {field: values[table].get(field, "") for field in fields}
        if file_format == "csv":
            path = directory / f"{table}.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(fields))
                writer.writeheader()
                writer.writerow(row)
            files.append(str(path))
        else:
            sheet = book.create_sheet(table)
            sheet.append(list(fields))
            sheet.append(list(row.values()))
    if file_format == "xlsx":
        path = directory / "business.xlsx"
        book.save(path)
        files.append(str(path))
    book.close()
    return files


@pytest.fixture(params=["csv", "xlsx"])
def business_files(request, tmp_path):
    return write_business_files(request.param, tmp_path)


def test_import_and_query_without_local_samples(business_files, tmp_path):
    mapping = suggest_mapping(discover(business_files))
    manifest = import_upload(business_files, mapping, "独立数据集", tmp_path / "data")
    assert manifest["name"] == "独立数据集"
    assert manifest["practice"]["year_context"] is None
    dataset_id = manifest["dataset_id"]
    assert registered_datasets(tmp_path / "data") == {dataset_id: "独立数据集"}
    executor = Executor(dataset_id, tmp_path / "data")
    plan = AnalysisPlan(dataset_id=dataset_id, metadata_version=manifest["metadata_version"], intent="aggregate",
                        metric_ids=["sales", "order_count", "payment_amount"],
                        time_range=TimeRange(start="2025-01-01", end="2025-02-01"))
    session = Session(dataset_id)
    result, _ = executor.execute_plan(session, executor.register(session, plan))
    assert result.status == "ok"
    assert result.rows == [[Decimal("12.50"), 1, Decimal("14.50")]]


def test_empty_ui_can_import_and_analyze(business_files, tmp_path, monkeypatch):
    import gradio as gr
    from gradio_client import Client, handle_file
    import app.ui as ui

    data_dir = tmp_path / "data"
    monkeypatch.setattr(ui, "registered_datasets", lambda: registered_datasets(data_dir))
    monkeypatch.setattr(ui, "Executor", lambda dataset: Executor(dataset, data_dir))
    monkeypatch.setattr(ui, "SessionStore", lambda: SessionStore(data_dir / "sessions.sqlite3"))
    monkeypatch.setattr(ui, "import_upload", lambda files, mapping, name: import_upload(files, mapping, name, data_dir))

    class ScriptedProvider:
        model = "onboarding-test"
        calls = 0

        def complete(self, messages):
            self.calls += 1
            context = json.loads(messages[1]["content"])
            assert context["practice_year"] is None
            return AnalysisPlan(dataset_id=context["catalog"]["dataset_id"], metadata_version="1.0.0",
                                intent="aggregate", metric_ids=["sales"],
                                time_range=TimeRange(start="2025-01-01", end="2025-02-01")).model_dump_json()

    provider = ScriptedProvider()
    monkeypatch.setattr(ui, "ChatCompletionsProvider", lambda: provider)
    demo = ui.build_ui()
    submit = next(fn.fn for fn in demo.fns.values() if fn.api_name == "submit")
    with pytest.raises(gr.Error, match="请先上传"):
        submit("查询销售额", [], None, None, False)
    assert provider.calls == 0
    try:
        demo.launch(server_name="127.0.0.1", prevent_thread_lock=True, quiet=True, footer_links=[])
        client = Client(demo.local_url, verbose=False)
        empty = client.predict(None, api_name="/dataset_ready")
        assert empty["visible"] is True
        readiness = next(fn.fn for fn in demo.fns.values() if fn.api_name == "dataset_ready")
        assert readiness(None)[0].visible is False
        files = [handle_file(path) for path in business_files]
        inspected = client.predict(files, api_name="/inspect_upload")
        fields = client.predict(inspected[1], api_name="/mapping_table")
        mapping = client.predict(fields["value"], api_name="/save_mapping")
        imported = client.predict(files, mapping, "首次导入", api_name="/upload_dataset")
        dataset_id = imported[0]["value"]
        client.predict(dataset_id, api_name="/switch_dataset")
        ready = client.predict(dataset_id, api_name="/dataset_ready")
        assert ready["visible"] is False
        assert readiness(dataset_id)[0].visible is True
        result = client.predict("全部数据的商品销售额", [], dataset_id, False, api_name="/submit")
        assert result[2]["dataset_id"] == dataset_id
        assert result[2]["result"]["rows"] == [["12.50"]]
        assert provider.calls == 1
    finally:
        demo.close()


def test_cli_without_data_explains_how_to_import(monkeypatch, capsys):
    import app.cli as cli

    monkeypatch.setattr(cli, "registered_datasets", lambda: {})
    monkeypatch.setattr("sys.argv", ["ecommerce-chat"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "尚未导入数据" in capsys.readouterr().err


def test_cli_defaults_to_an_imported_dataset(monkeypatch):
    import app.cli as cli

    selected = []

    class Store:
        def list(self, dataset_id):
            selected.append(dataset_id)
            return []

    monkeypatch.setattr(cli, "registered_datasets", lambda: {"upload_custom": "自己的数据"})
    monkeypatch.setattr(cli, "SessionStore", Store)
    monkeypatch.setattr("sys.argv", ["ecommerce-chat", "--list-sessions"])
    cli.main()
    assert selected == ["upload_custom"]
