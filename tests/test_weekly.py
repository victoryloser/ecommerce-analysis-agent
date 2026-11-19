import csv
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import team
from app.data_upload import discover, import_upload, suggest_mapping
from app.refunds import parse_ledger
from app.team_access import TeamAccess
from app.weekly import last_week
from app.models import AnalysisPlan
from test_stage_one import write_table


class ReportPlanner:
    model = "test-coverage"

    def complete(self, messages):
        payload = json.loads(messages[1]["content"])
        catalog = payload["catalog"]
        category = {"查看第一品类": "c1", "查看第二品类": "c2"}.get(payload["question"])
        return AnalysisPlan(
            dataset_id=catalog["dataset_id"],
            metadata_version=catalog["metadata_version"],
            time_range={k: catalog["coverage"][k] for k in ("start", "end")},
            filters=[{"field": "category_id", "values": [category]}] if category else [],
            metric_ids=["order_count", "sales"],
            intent="aggregate",
        ).model_dump_json()


class TeamPilot:
    def __init__(self, root):
        self.root = root
        self.db = root / "data"
        self.access = TeamAccess(self.db)
        for user, role, grants in (
            ("admin", "admin", []),
            ("ops", "analyst", []),
            ("finance", "analyst", ["details"]),
            ("reader", "reader", []),
        ):
            self.access.set_member(user, "pw", role, grants, ["*"])
        self.client = TestClient(team.create_app(self.db, ReportPlanner))

    def snapshot(self, revision=0, parent=None):
        orders = [
            ["order_id", "purchase_at", "order_status"],
            ["old", "2025-01-22", "delivered"],
            ["o1", "2025-01-28", "delivered"],
            ["o2", "2025-02-01", "delivered"],
            ["o3", "2025-02-02", "delivered"],
        ]
        items = [["order_id", "order_item_id", "price"], ["old", "x", "10.00"], ["o1", "a", "100.00"]]
        if revision != 3:
            items.append(["o2", "b", "546.23"])
            if revision in (0, 2):
                items.append(["o2", "b", "549.00"])
        else:
            orders.pop(3)
        items.extend([["o3", "c", "252.18"], ["o3", "c", "300"]])
        paths = [write_table(self.root, "orders", orders), write_table(self.root, "order_items", items)]
        mapping = suggest_mapping(discover(paths), "orders_items")
        return import_upload(paths, mapping, "试点", self.db, parent_dataset_id=parent)["dataset_id"]

    def post(self, route, body, name="ops"):
        return self.client.post(route, json=body, auth=(name, "pw"), headers={"X-Analysis-Action": "1"})

    def get(self, route, name="ops"):
        return self.client.get(route, auth=(name, "pw"))

    def ok(self, route, body, name="ops"):
        response = self.post(route, body, name)
        assert response.is_success, response.text
        return response.json()

    def complete(self, dataset, start="2025-01-20", end="2025-02-03"):
        return self.ok("/api/weekly/coverage", dict(dataset=dataset, start=start, end=end), "admin")

    def template(self, dataset, question="商品额及订单", **settings):
        turn = self.ok("/api/action", dict(action="ask", value=question, dataset=dataset))
        return self.ok(
            "/api/weekly/templates", dict(title="周经营", session=turn["session"], dataset=dataset, **settings)
        )["template"]


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    import app.evidence
    import app.trace

    for mod in (app.evidence, app.trace):
        monkeypatch.setattr(mod, "RUNS_DIR", tmp_path / "runs")
    return TeamPilot(tmp_path)


def test_report_ticket_and_revision(pilot):
    p = pilot
    root = p.snapshot()
    t = p.template(root)
    run = dict(dataset=root, template=t, as_of="2025-02-03")
    assert p.post("/api/weekly/run", run).status_code == 422
    p.complete(root, end="2025-01-27")
    assert p.post("/api/weekly/run", run).status_code == 422
    p.complete(root, start="2025-01-27")
    with ThreadPoolExecutor(2) as pool:
        created = list(pool.map(lambda _: p.ok("/api/weekly/run", run), range(2)))
    assert sorted(r["reused"] for r in created) == [False, True]
    key = created[0]["report"]
    assert created[1]["report"] == key
    assert p.ok("/api/weekly/run", run) == {"report": key, "reused": True}
    url = "/api/weekly/reports/" + key
    report = p.get(url, "finance").json()
    assert report["plan"]["time_range"] == {"start": "2025-01-27", "end": "2025-02-03"}
    assert report["overview"][0]["order_count_current"] == 3
    assert Decimal(report["overview"][0]["sales_current"]) == 100
    assert report["price_conflict_count"] == 2 and not report["categories"]
    assert p.get(url, "reader").status_code == 404
    link = p.ok(url + "/publish?recipient=reader", {})["url"]
    assert link == "/?weekly=" + key and "conflicts" not in p.get(url, "reader").json()
    case = dict(report=key, order="o2", item="b", assignee="finance")
    assert p.post("/api/weekly/tickets", case).status_code == 403
    ticket = p.ok("/api/weekly/tickets", case, "finance")["ticket"]
    assert p.ok("/api/weekly/tickets", case, "finance")["ticket"] == ticket
    second = p.ok("/api/weekly/tickets", {**case, "order": "o3", "item": "c"}, "finance")["ticket"]
    issue = "/api/weekly/tickets/" + ticket
    assert p.post(issue, dict(action="close", note="尚未修复"), "finance").status_code == 422
    p.ok(issue, dict(action="correct", note="已核对成交价", batch="第二批"), "finance")
    updated = p.snapshot(1, root)
    revised = p.complete(updated)["revisions"][0]["report"]
    new_url = "/api/weekly/reports/" + revised
    new = p.get(new_url, "finance").json()
    assert new["parent"] == key and new["revision"]["affected_orders"] == 1
    assert Decimal(new["revision"]["delta"]["sales_current"]) == Decimal("546.23")
    assert new["price_conflict_count"] == 1 and p.get(url, "finance").json()["price_conflict_count"] == 2
    check = p.get(issue, "finance").json()
    assert check["state"] == "待确认关闭" and check["events"][-1]["payload"]["included_sales"] == "546.23"
    assert p.get("/api/weekly/tickets/" + second, "finance").json()["state"] == "待处理"
    p.complete(updated)
    assert p.get(issue, "finance").json()["events"] == check["events"]
    p.ok(issue, dict(action="close", note="新版已与原始行一致"), "finance")
    p.ok(new_url + "/publish?recipient=reader", {})
    assert p.get(new_url, "reader").json()["revision"]["affected_orders"] == 1
    p.access.change_access("reader", "reader", [], [updated])
    assert p.get(new_url, "reader").status_code == 404
    again = p.snapshot(2, updated)
    p.complete(again)
    assert p.get(issue, "finance").json()["state"] == "待处理"
    missing = p.snapshot(3, again)
    p.complete(missing)
    assert "离开原范围" in p.get(issue, "finance").json()["events"][-1]["payload"]["result"]
    version = p.template(root, prior=t, metrics=["order_count"])
    change = p.ok("/api/weekly/run", dict(dataset=updated, template=version, revise=key))["report"]
    result = p.get("/api/weekly/reports/" + change).json()
    assert result["revision"]["kind"] == "口径变更" and "metric_ids" in result["revision"]["changed"]
    assert "delta" not in result["revision"] and result["overview"][0]["order_count_current"] == 3


def test_calendar():
    sunday = last_week(date(2025, 2, 2), "Asia/Shanghai", 6)
    assert (sunday.start, sunday.end) == (date(2025, 1, 26), date(2025, 2, 2))


def test_scoped_template_and_object_switch(pilot):
    p = pilot
    files = [
        write_table(
            p.root, "orders", [["order_id", "purchase_at", "order_status"], ["A", "2025-02-01", "delivered"]]
        ),
        write_table(
            p.root,
            "order_items",
            [
                ["order_id", "order_item_id", "price", "category_id"],
                ["A", "1", "45", "c1"],
                ["A", "2", "55", "c2"],
            ],
        ),
    ]
    ds = import_upload(files, suggest_mapping(discover(files), "orders_items"), "品类", p.db)["dataset_id"]
    p.complete(ds)
    first = p.template(ds, "查看第一品类", category=True)
    report = p.ok("/api/weekly/run", dict(template=first, dataset=ds, as_of="2025-02-03"))["report"]
    original = p.get("/api/weekly/reports/" + report).json()
    assert (
        original["categories"][0]["entity_id"] == "c1" and Decimal(original["overview"][0]["sales_current"]) == 45
    )
    p.ok("/api/weekly/reports/" + report + "/publish?recipient=reader", {})
    second = p.template(ds, "查看第二品类", prior=first, category=True)
    revised = p.ok("/api/weekly/run", dict(template=second, dataset=ds, revise=report))["report"]
    new = p.get("/api/weekly/reports/" + revised).json()
    assert new["revision"]["kind"] == "分析对象变更" and "delta" not in new["revision"]
    assert Decimal(new["overview"][0]["sales_current"]) == 55


def test_refund_periods_and_ledger_identity(pilot):
    p = pilot
    files = [
        write_table(
            p.root, "orders", [["order_id", "purchase_at", "order_status"], ["o", "2025-01-05", "delivered"]]
        ),
        write_table(p.root, "order_items", [["order_id", "order_item_id", "price"], ["o", "x", "100"]]),
    ]
    dataset = import_upload(files, suggest_mapping(discover(files), "orders_items"), "月报", p.db)["dataset_id"]
    p.complete(dataset, "2025-01-01", "2025-04-01")
    header = (
        "refund_id,order_id,posted_at,status,currency,goods_amount,freight_amount,total_amount,original_refund_id"
    )
    rows = [
        "r1,o,2025-02-01,success,CNY,30,2,32,",
        "r1,o,2025-02-01,success,CNY,30,2,32,",
        "r2,o,2025-03-01,success,CNY,10,0,10,",
        "r3,o,2025-03-02,reversal,CNY,30,2,32,r1",
        "r4,o,2025-03-03,success,CNY,,,5,",
        "r5,absent,2025-03-03,success,USD,2,0,2,",
        "r6,o,2025-03-04,pending,CNY,,,,",
        "r7,o,2025-03-05,failed,CNY,,,,",
    ]

    def upload(lines):
        return p.client.post(
            "/api/weekly/refunds",
            auth=("admin", "pw"),
            headers={"X-Analysis-Action": "1"},
            data=dict(dataset_id=dataset, start="2025-01-01", end="2025-04-01", timezone="Asia/Shanghai"),
            files={"file": ("refunds.csv", "\n".join([header, *lines]), "text/csv")},
        )

    assert upload(rows).json()["unique_records"] == 7
    assert upload(rows).status_code == 200 and upload(rows[:-1]).status_code == 422

    def net(start, end, role="ops", **kwargs):
        return p.ok("/api/weekly/net", dict(dataset=dataset, start=start, end=end, **kwargs), role)

    jan = net("2025-01-01", "2025-02-01")
    feb = net("2025-02-01", "2025-03-01", finance_total="-30.00")
    assert Decimal(jan["currencies"]["CNY"]["net"]) == 100
    assert Decimal(feb["currencies"]["CNY"]["net"]) == -30 and Decimal(feb["finance_difference_cny"]) == 0
    template = p.template(dataset, net_refunds=True)
    key = p.ok("/api/weekly/run", dict(template=template, dataset=dataset, as_of="2025-02-03"))["report"]
    weekly = p.get("/api/weekly/reports/" + key).json()["refunds"]["current"]
    assert Decimal(weekly["currencies"]["CNY"]["net"]) == -30 and "postings" not in weekly
    march = net("2025-03-01", "2025-04-01", "finance")
    assert march["currencies"]["CNY"]["net"] is None and Decimal(march["currencies"]["CNY"]["known_net"]) == 20
    assert march["currencies"]["USD"]["net"] is None
    assert {v["reason"] for v in march["exceptions"]} == {"订单未匹配", "整单退款未拆分"}
    assert "exceptions" not in net("2025-03-01", "2025-04-01")
    with pytest.raises(ValueError, match="冲突"):
        parse_ledger("\n".join([header, rows[0], rows[0].replace("30,2,32", "31,2,33")]).encode(), "Asia/Shanghai")


def test_replay_two_public_weeks(pilot):
    from app.config import ROOT

    pack = ROOT / "semi-material-2/公开复赛包_v1.1/数据集/D4"
    if not pack.is_dir():
        pytest.skip("本机未提供公开复赛包")
    p = pilot
    files = [pack / "orders.csv", pack / "order_items.csv"]
    mapping = suggest_mapping(discover(files), "orders_items")
    root = import_upload(files, mapping, "D4", p.db)["dataset_id"]
    p.complete(root, "2027-03-01", "2027-05-01")
    t = p.template(root)
    originals, tickets = {}, {}
    for as_of, oid in (("2027-04-12", "F002589"), ("2027-04-26", "F001738")):
        report = p.ok("/api/weekly/run", dict(template=t, dataset=root, as_of=as_of))["report"]
        originals[oid] = report
        p.ok("/api/weekly/reports/" + report + "/publish?recipient=reader", {})
        tickets[oid] = p.ok(
            "/api/weekly/tickets", dict(report=report, order=oid, item="1", assignee="finance"), "finance"
        )["ticket"]
    with files[1].open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [reader.fieldnames]
        for r in reader:
            if (r["order_id"], r["order_item_id"], r["price"]) != ("F001738", "1", "564.00"):
                rows.append(list(r.values()))
    amended = write_table(p.root, "order_items", rows)
    updated = import_upload([files[0], amended], mapping, "只更正一单", p.db, parent_dataset_id=root)["dataset_id"]
    revisions = p.complete(updated, "2027-03-01", "2027-05-01")["revisions"]
    assert len(revisions) == 2
    reports = [p.get("/api/weekly/reports/" + r["report"], "finance").json() for r in revisions]
    by_parent = {r["parent"]: r["revision"] for r in reports}
    assert Decimal(by_parent[originals["F001738"]]["delta"]["sales_current"]) == Decimal("546.23")
    assert by_parent[originals["F002589"]]["affected_orders"] == 0
    for oid, state in (("F001738", "待确认关闭"), ("F002589", "待处理")):
        ticket = p.get("/api/weekly/tickets/" + tickets[oid], "finance").json()
        assert ticket["state"] == state
        frozen = p.get("/api/weekly/reports/" + originals[oid], "finance").json()
        assert any(c["order"] == oid for c in frozen["conflicts"])
