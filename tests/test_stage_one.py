import csv
import json
import shutil
import sqlite3
from decimal import Decimal
from collections import defaultdict

import pytest
from fastapi.testclient import TestClient

from app import team
from app.data_upload import discover, import_upload, suggest_mapping
from app.delivery import refresh_delivery
from app.executor import Executor
from app.inspection_sources import retained_order_records
from app.models import AnalysisPlan
from app.orchestrator import AnalysisAgent
from app.planner import make_plan
from app.session import Session
from app.session_store import SessionStore
from app.snapshot_diff import compare_snapshots
from app.team_access import TeamAccess
from app.trace import TurnTrace
from test_data_onboarding import write_business_files


def write_table(directory, name, rows):
    path = directory / (name + ".csv")
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerows(rows)
    return path


def light_files(directory):
    orders = write_table(directory, "orders", [
        ["order_id", "purchase_at", "order_status"], ["o1", "2025-01-04", "已送达"],
        ["o2", "2025-02-01", "已送达"], ["o3", "2025-01-05", "canceled"]])
    items = write_table(directory, "order_items", [
        ["order_id", "order_item_id", "price", "quantity", "category_id", "seller_id"],
        ["o1", "A01", "12.50", 2, "c", "s"], ["o1", "A01", "12.50", 2, "c", "s"],
        ["o1", "B", 0, 1, "c", "s"], ["o2", "A", 10, 1, "b", "s"],
        ["o2", "A", 12, 1, "b", "s"], ["o3", "A", 6, 1, "c", "t"]])
    return [orders, items]


def upload_light(files, directory, **options):
    mapping = suggest_mapping(discover(files), "orders_items")
    mapping.update(status_map={"已送达": "delivered"}, **options)
    return import_upload(files, mapping, "轻量", directory)


def plan(manifest, **args):
    return AnalysisPlan(**({"dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"],
                           "intent": "aggregate", "time_range": {"start": "2025-01-01", "end": "2025-03-01"},
                           "metric_ids": ["order_count", "sales"]} | args))


def execute(executor, p):
    session = Session(p.dataset_id)
    result, sql = executor.execute_plan(session, executor.register(session, p))
    assert result.status == "ok", result.error
    return result, sql


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_light_and_full_same_export(tmp_path, fmt):
    files = write_business_files(fmt, tmp_path)
    full = import_upload(files, suggest_mapping(discover(files)), "完整", tmp_path / "db")
    mapping = suggest_mapping(discover(files), "orders_items")
    mapping["tables"] = {k: v for k, v in mapping["tables"].items() if k in {"orders", "order_items"}}
    m = import_upload(files, mapping, "两表", tmp_path / "db")
    for manifest in (m, full):
        result, _ = execute(Executor(manifest["dataset_id"], tmp_path / "db"),
                            plan(manifest, time_range={"start": "2025-01-01", "end": "2025-02-01"}))
        assert result.rows == [[1, Decimal("12.50")]]


def test_light_quality_sync_and_compare(tmp_path, monkeypatch):
    files = light_files(tmp_path)
    directory = tmp_path / "db"
    manifest = upload_light(files, directory, amount_mode="unit")
    assert manifest["row_counts"].keys() == {"orders", "order_items"}
    assert manifest["quality"]["missing_payment_orders"] is None
    runner = Executor(manifest["dataset_id"], directory)
    p = plan(manifest)
    result, compiled = execute(runner, p)
    assert result.rows == [[2, Decimal("25.00")]]
    assert result.quality[0]["sales"]["excluded_order_count"] == 1
    assert "JOIN customers" not in compiled.sql and "order_payments" not in compiled.sql
    assert retained_order_records(runner, "o1")["records"][1]["values"]["price"] == "12.50"
    result, _ = execute(runner, plan(manifest, intent="rank", dimension="seller", metric_ids=["sales"]))
    assert result.rows[0][:3] == ["s", "s", Decimal("25.00")]
    with pytest.raises(ValueError, match="未提供"):
        runner.register(Session(p.dataset_id), plan(manifest, metric_ids=["payment_amount"]))
    monkeypatch.setenv("DATA_DELIVERY_DIR", str(tmp_path / "feed"))
    incoming = tmp_path / "feed" / p.dataset_id
    incoming.mkdir(parents=True)
    for f in files:
        shutil.copy(f, incoming)
    target = incoming / files[1].name
    target.write_text(target.read_text(encoding="utf-8").replace("12.50", "13.00"), encoding="utf-8")
    agent = AnalysisAgent(None, {p.dataset_id: runner})
    fresh, _ = refresh_delivery(agent, Session(p.dataset_id, working_plan=p), "数据更新了")
    assert fresh.last_result.rows == [[2, Decimal("26.00")]]
    other = agent.executors[fresh.dataset_id]
    report = compare_snapshots([runner.path, other.path], [p, fresh.working_plan], tmp_path / "diff")
    assert report["metric_comparisons"][1]["delta"] == Decimal("1.00")
    assert refresh_delivery(agent, fresh, "/sync")[0].dataset_id == fresh.dataset_id


class Planner:
    model = "test"

    def __init__(self):
        self.contexts = []

    def complete(self, messages):
        data = json.loads(messages[1]["content"])
        self.contexts.append(data)
        prefs = {}
        if data["question"] == "看原始订单":
            prefs = {"intent": "details", "metric_ids": []}
        if "同名" in data["question"]:
            prefs = {"filters": [{"field": "seller_id", "values": ["同名"]}]}
        return plan(data["catalog"], **prefs).model_dump_json()


def test_ten_thousand_entity_context(tmp_path):
    manifest = upload_light(light_files(tmp_path), tmp_path / "data")
    manifest["entities"]["sellers"] = [[f"S{i:05}", f"商家{i:05}"] for i in range(10000)]
    manifest["entities"]["sellers"][:2] = [["S00000", "同名"], ["S00001", "同名"]]
    session = Session(manifest["dataset_id"])
    provider = Planner()
    for name, expected in (("全店销售", 0), ("商家09999", 1), ("同名", 2)):
        p, _ = make_plan(provider, manifest, session, name, False, TurnTrace(session.session_id, session.dataset_id, name, "test"))
        assert len(provider.contexts[-1]["catalog"]["entities"]["sellers"]) == expected
    assert p.intent == "clarify" and len(p.entity_candidates) == 2
    session.pending_plan = p
    resolved, count = make_plan(provider, manifest, session, "第二家", False, TurnTrace(session.session_id, session.dataset_id, "选择", "test"))
    assert count == 0 and resolved.filters[0].values == ["S00001"]
    with pytest.raises(ValueError, match="没有匹配"):
        make_plan(provider, manifest, Session(session.dataset_id), "只看商家“无此店”", False, TurnTrace(session.session_id, session.dataset_id, "问", "test"))


def test_real_ranking_with_ten_thousand_sellers(tmp_path):
    from app.model_context import entity_candidates
    orders = write_table(tmp_path, "orders", [["order_id", "purchase_at", "order_status"], ["one", "2025-01-01", "delivered"]])
    items = write_table(tmp_path, "order_items", [["order_id", "order_item_id", "price", "seller_id"],
                       *(["one", str(i), i, f"S{i:05}"] for i in range(10000))])
    manifest = upload_light([orders, items], tmp_path / "data")
    assert entity_candidates(manifest, "全店排名")["sellers"] == []
    runner = Executor(manifest["dataset_id"], tmp_path / "data")
    p = plan(manifest, intent="rank", metric_ids=["sales"], dimension="seller", top_n=1,
             time_range={"start": "2025-01-01", "end": "2025-02-01"})
    result, _ = execute(runner, p)
    assert result.rows[0][0] == "S09999"
    assert result.rows[0][2] == Decimal("9999.00")


def test_unconfirmed_money_and_optional_payments(tmp_path):
    files = light_files(tmp_path)
    pay = write_table(tmp_path, "order_payments", [["order_id", "payment_sequence", "payment_value"], ["o1", 1, 0]])
    m = upload_light([*files, pay], tmp_path / "data", amount_mode="unknown")
    assert m["capabilities"]["metrics"] == ["order_count", "payment_amount"]
    runner = Executor(m["dataset_id"], tmp_path / "data")
    with pytest.raises(ValueError, match="行商品额"):
        runner.register(Session(m["dataset_id"]), plan(m))
    p = plan(m, metric_ids=["payment_amount"])
    assert execute(runner, p)[0].rows == [[None]]
    p.time_range.end = p.time_range.end.replace(month=2)
    assert execute(runner, p)[0].rows == [[Decimal("0.00")]]


def test_team_account_lifecycle(tmp_path, monkeypatch):
    import app.evidence
    import app.snapshot_comparison
    import app.trace
    import app.config
    for mod in (app.evidence, app.snapshot_comparison, app.trace, app.config):
        monkeypatch.setattr(mod, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("TEAM_MODEL_FIELDS", "question")
    db = tmp_path / "db"
    files = light_files(tmp_path)
    m = upload_light(files, db)
    access = TeamAccess(db)
    for name, role, grants in (("owner", "analyst", ["details", "export"]), ("ops", "analyst", []), ("reader", "reader", [])):
        access.set_member(name, "test-account", role, grants, [m["dataset_id"]])
    planner = Planner()
    client = TestClient(team.create_app(db, lambda: planner))
    def act(name, action, **params):
        return client.post("/api/action", json={"action": action, "dataset": m["dataset_id"], **params},
                           headers={"X-Analysis-Action": "1"}, auth=(name, "test-account"))
    assert client.get("/api/home").status_code == 401
    assert act("reader", "ask", value="销售").status_code == 403
    assert act("ops", "order", value="o1").status_code == 403
    assert "明细权限" in act("ops", "ask", value="看原始订单").json()["answer"]
    data = act("owner", "ask", value="商品额").json()
    assert data["result"]["rows"][0][0] == 2
    assert "last_result" not in planner.contexts[-1] and not planner.contexts[-1]["recent_messages"]
    sid = data["session"]
    monkeypatch.setenv("TEAM_MODEL_FIELDS", "question,preview:sales")
    act("owner", "ask", value="沿用期间", session=sid).raise_for_status()
    assert planner.contexts[-1]["last_result"]["columns"] == ["sales"]
    assert "working_plan" not in planner.contexts[-1]
    proof = act("owner", "proof", session=sid).json()
    assert proof["dataset_id"] == m["dataset_id"] and "sql" not in proof
    root = m["dataset_id"]
    mapping = suggest_mapping(discover(files), "orders_items")
    mapping["status_map"] = {"已送达": "delivered"}
    revised = import_upload(files, mapping, "修订版", db, parent_dataset_id=root)["dataset_id"]
    assert act("owner", "compare", session=sid, target=revised).status_code == 404
    access.change_access("owner", "analyst", ["details", "export"], ["feed:" + root])
    assert set(client.get("/api/home", auth=("owner", "test-account")).json()["datasets"]) == {root, revised}
    compared = act("owner", "compare", session=sid, target=revised).json()
    comparison_url = "/api/files/" + compared["download"]
    assert client.get(comparison_url, auth=("owner", "test-account")).status_code == 200
    access.change_access("owner", "analyst", ["details", "export"], [root])
    assert client.get(comparison_url, auth=("owner", "test-account")).status_code == 404
    assert act("owner", "open", session=sid).json()["session"] == sid
    assert act("ops", "open", session=sid).status_code != 200
    report = act("owner", "share", value="财务周汇总", session=sid, target="reader").json()["report"]
    assert client.get("/api/reports/"+report, auth=("reader", "test-account")).json()["snapshot"] == m["database_sha256"]
    assert client.get("/api/reports/"+report, auth=("ops", "test-account")).status_code == 404
    assert act("owner", "order", value="o1").json()["records"]
    file = act("owner", "export", session=sid).json()["download"]
    assert client.get("/api/files/"+file, auth=("owner", "test-account")).status_code == 200
    access.set_member("owner", "test-account", "analyst", [], [m["dataset_id"]])
    assert client.get("/api/files/"+file, auth=("owner", "test-account")).status_code == 404
    access.set_member("reader", "test-account", "reader", [], [])
    assert client.get("/api/reports/"+report, auth=("reader", "test-account")).status_code == 404
    with sqlite3.connect(access.path) as conn:
        assert not any("商品额" in str(row) for row in conn.execute("SELECT * FROM operations"))


def test_team_import_history_claim_and_instance_boundary(tmp_path, monkeypatch):
    db = tmp_path / "tenantA" / "data"
    other = tmp_path / "tenantB" / "data"
    for directory in (db, other):
        TeamAccess(directory).set_member("admin", "pass", "admin", [], [])
    client = TestClient(team.create_app(db))
    auth = ("admin", "pass")
    headers = {"X-Analysis-Action": "1"}
    payload = [("files", (p.name, p.read_bytes(), "text/csv")) for p in light_files(tmp_path)]
    inspected = client.post("/api/upload/inspect", files=payload, headers=headers, auth=auth).json()
    mapping = inspected["mapping"]
    mapping["status_map"] = {"已送达": "delivered"}
    preview = client.post("/api/mapping/preview", json={**mapping, "amount_mode": "unknown"}, headers=headers, auth=auth).json()
    assert len(preview["可用指标"]) == 1 and preview["可问问题"] == ["全期已送达订单数是多少？"]
    imported = client.post("/api/upload/import", data={"mapping": json.dumps(mapping)}, files=payload, headers=headers, auth=auth).json()
    key = imported["dataset"]
    assert any("商品额" in q for q in client.get("/api/home", auth=auth).json()["examples"][key])
    assert TestClient(team.create_app(other)).get("/api/home", auth=auth).json()["datasets"] == {}
    m = Executor(key, db).manifest
    legacy = Session(key)
    legacy.remember("旧问题", "旧回答")
    SessionStore(db / "sessions.sqlite3").save(legacy, m)
    def act(action, **params):
        return client.post("/api/action", auth=auth, headers=headers, json={"action": action, "dataset": key, **params})
    assert not act("sessions").json()["sessions"]
    assert act("claim", value=legacy.session_id).json()["session"] == legacy.session_id
    assert act("open", session=legacy.session_id).json()["messages"][0]["content"] == "旧问题"
    monkeypatch.setenv("TEAM_PROXY_SECRET", "instance-test")
    proxy = TestClient(team.create_app(db))
    assert proxy.get("/api/home", headers={"X-Analysis-User": "admin", "X-Analysis-Proxy": "instance-test"}).json()["role"] == "admin"


@pytest.mark.parametrize("role", ["D3", "D4"])
def test_public_monthly_reconciliation(role, tmp_path):
    from app.config import ROOT
    pack = ROOT / "semi-material-2/公开复赛包_v1.1/数据集" / role
    if not pack.is_dir():
        pytest.skip("本机未提供公开复赛包")
    files = [pack / "orders.csv", pack / "order_items.csv"]
    with files[0].open(encoding="utf-8-sig", newline="") as src:
        months = {r["order_id"]: r["purchase_at"][:7] for r in csv.DictReader(src) if r["order_status"] == "delivered"}
    prices = defaultdict(set)
    with files[1].open(encoding="utf-8-sig", newline="") as src:
        for r in csv.DictReader(src):
            if r["price"]:
                prices[r["order_id"], r["order_item_id"]].add(Decimal(r["price"]))
    excluded = {order for (order, _), values in prices.items() if len(values) > 1}
    manifest = upload_light(files, tmp_path)
    runner = Executor(manifest["dataset_id"], tmp_path)
    for month, end in (("2027-03", "2027-04-01"), ("2027-04", "2027-05-01")):
        orders = {key for key, m in months.items() if m == month}
        eligible = orders - excluded
        expected = sum(next(iter(values)) for (key, _), values in prices.items() if key in eligible)
        result, _ = execute(runner, plan(manifest, time_range={"start": month+"-01", "end": end}))
        assert result.rows == [[len(orders), expected]]
