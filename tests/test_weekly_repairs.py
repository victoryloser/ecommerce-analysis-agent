from decimal import Decimal
import json
import sqlite3

import pytest

from app.data_upload import discover, import_upload, suggest_mapping
from app.refunds import parse_ledger
from test_stage_one import write_table
from test_weekly import pilot


@pytest.mark.parametrize("trigger", ["coverage", "manual", "already_published"])
def test_late_publication_links_existing_draft_and_rechecks_reader(pilot, trigger):
    old = pilot.snapshot()
    pilot.complete(old)
    template = pilot.template(old)
    run = {"template": template, "as_of": "2025-02-03"}
    original = pilot.ok("/api/weekly/run", {**run, "dataset": old})["report"]
    newer = pilot.snapshot(1, old)
    pilot.complete(newer)
    draft = pilot.ok("/api/weekly/run", {**run, "dataset": newer})["report"]
    url = "/api/weekly/reports/" + draft
    assert pilot.get(url).json()["parent"] is None
    if trigger == "already_published":
        pilot.ok(url + "/publish?recipient=reader", {})
        published = pilot.get(url, "reader").json()
    pilot.ok("/api/weekly/reports/" + original + "/publish?recipient=reader", {})
    if trigger == "already_published":
        result = pilot.complete(newer)
        assert result["pending"][0]["report"] == original and not result["revisions"]
        assert pilot.get(url, "reader").json() == published
        return
    if trigger == "manual":
        assert pilot.ok("/api/weekly/run", {**run, "dataset": newer}) == {"report": draft, "reused": True}
    else:
        assert pilot.complete(newer)["revisions"] == [{"report": draft, "reused": True}]
    revision = pilot.get(url).json()
    assert revision["parent"] == original
    assert Decimal(revision["revision"]["delta"]["sales_current"]) == Decimal("546.23")
    pilot.ok(url + "/publish?recipient=reader", {})
    pilot.access.change_access("reader", "reader", [], [newer])
    assert pilot.get(url, "reader").status_code == 404


def test_refund_csv_conflicting_columns_and_short_rows():
    header = "refund_id,order_id,posted_at,status,currency,goods_amount"
    with pytest.raises(ValueError, match="重复"):
        parse_ledger((header + ",goods_amount\nr1,o,2025-01-01,success,CNY,10,100\n").encode(), "Asia/Shanghai")
    with pytest.raises(ValueError, match="列数"):
        parse_ledger((header + ",total_amount\nr1,o,2025-01-01,success,CNY,10\n").encode(), "Asia/Shanghai")


def test_refund_reversal_uses_instant_through_dst_fallback():
    header = "refund_id,order_id,posted_at,status,currency,goods_amount,original_refund_id\n"
    before = "2025-11-02T01:45:00-04:00"
    after = "2025-11-02T01:15:00-05:00"

    def ledger(start, end):
        return (header + f"r1,o,{start},success,USD,10,\nr2,o,{end},reversal,USD,10,r1\n").encode()

    rows, _ = parse_ledger(ledger(before, after), "America/New_York")
    assert len(rows) == 2
    with pytest.raises(ValueError, match="先前成功"):
        parse_ledger(ledger(after, before), "America/New_York")
    with pytest.raises(ValueError, match="冲突"):
        parse_ledger((header + "r1,o,2025-11-02T01:15:00-04:00,success,USD,10,\n"
                      + "r1,o,2025-11-02T01:15:00-05:00,success,USD,10,\n").encode(), "America/New_York")


@pytest.mark.parametrize("archived", [False, True])
def test_category_reclassification_changes_one_order_with_zero_total_delta(pilot, archived):
    orders = write_table(pilot.root, "orders", [["order_id", "purchase_at", "order_status"],
                                                 ["one", "2025-01-28", "delivered"], ["two", "2025-01-28", "delivered"]])

    def snapshot(category, parent=None):
        items = write_table(pilot.root, "order_items", [["order_id", "order_item_id", "price", "category_id"],
                            ["one", "1", "100", category], ["two", "1", "50", "c2"]])
        files = [orders, items]
        return import_upload(files, suggest_mapping(discover(files), "orders_items"), "品类", pilot.db,
                             parent_dataset_id=parent)["dataset_id"]

    old = snapshot("c1")
    pilot.complete(old)
    template = pilot.template(old, category=True)
    report = pilot.ok("/api/weekly/run", {"template": template, "dataset": old, "as_of": "2025-02-03"})["report"]
    pilot.ok("/api/weekly/reports/" + report + "/publish?recipient=reader", {})
    if archived:
        with sqlite3.connect(pilot.access.path) as db:
            payload = json.loads(db.execute("SELECT payload FROM weekly_reports WHERE id=?", (report,)).fetchone()[0])
            for order in payload["order_evidence"]["current"].values():
                for item in order["items"].values():
                    item.pop("category_id")
            archived_payload = json.dumps(payload)
            db.execute("UPDATE weekly_reports SET payload=? WHERE id=?", (archived_payload, report))
    new = snapshot("c2", old)
    revision = pilot.complete(new)["revisions"][0]["report"]
    body = pilot.get("/api/weekly/reports/" + revision, "finance").json()
    assert Decimal(body["revision"]["delta"]["sales_current"]) == 0
    assert body["revision"]["affected_orders"] == 1
    change, = body["revision"]["changed_orders"]
    assert change["order"] == "one" and change["before"]["items"]["1"]["category_id"] == "c1"
    assert change["after"]["items"]["1"]["category_id"] == "c2"
    if archived:
        with sqlite3.connect(pilot.access.path) as db:
            assert db.execute("SELECT payload FROM weekly_reports WHERE id=?", (report,)).fetchone()[0] == archived_payload
