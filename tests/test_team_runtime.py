import csv
import io
import json
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from filelock import Timeout

from app import config, evidence, sql_workspace, team, trace
from app.data_upload import discover, registered_manifests, suggest_mapping
from app.session_store import archive_connection
from app.team_backup import backup, restore
from test_stage_one import light_files
from test_weekly import ReportPlanner, pilot


def run_pending(web):
    jobs = web.state.jobs
    jobs.recover()
    while jobs.run_one(web.state.visible):
        pass


def test_delivery_jobs_recover_and_reuse_publication(tmp_path):
    files = light_files(tmp_path)
    mapping = suggest_mapping(discover(files), "orders_items")
    mapping.update(status_map={"已送达": "delivered"})
    directory = tmp_path / "data"
    web = team.create_app(directory, ReportPlanner)
    jobs = web.state.jobs
    jobs.access.set_member("admin", "pw", "admin", [], [])
    jobs.access.set_member("other", "pw", "admin", [], [])
    headers = {"X-Analysis-Action": "1"}
    with TestClient(web, headers=headers) as client:
        def upload():
            response = client.post("/api/upload/queue", auth=("admin", "pw"), data={"mapping": json.dumps(mapping)},
                                   files=[("files", (f.name, f.read_bytes(), "text/csv")) for f in files])
            assert response.is_success, response.text
            return response.json()

        submitted = upload()
        key = submitted["job"]
        assert upload() == {"job": key, "reused": True}
        assert client.get(f"/api/jobs/{key}", auth=("other", "pw")).status_code == 404
        assert client.get("/api/home", auth=("admin", "pw")).json()["datasets"] == {}
        assert client.get("/healthz").json() == {"service": "online"}
        with pytest.raises(Timeout):
            backup(tmp_path, tmp_path / "running.zip")
        # Emulate termination after writing to the database but before publishing.
        claimed = jobs.claim()
        pending = json.loads(claimed["input"])["snapshot"]
        (directory / (pending + ".duckdb")).write_bytes(b"incomplete")
        assert client.get("/api/home", auth=("admin", "pw")).json()["datasets"] == {}
        run_pending(team.create_app(directory, ReportPlanner))
        done = client.get(f"/api/jobs/{key}", auth=("admin", "pw")).json()
        assert done["status"] == "succeeded" and done["result"]["dataset"] == pending
        assert [m["dataset_id"] for m in registered_manifests(directory)] == [pending]
        assert upload() == {"job": key, "reused": True}


def test_export_uses_submitted_evidence_and_current_grants(pilot, monkeypatch):
    p = pilot
    for module in (config, sql_workspace):
        monkeypatch.setattr(module, "RUNS_DIR", p.root / "runs")
    p.access.change_access("ops", "analyst", ["export", "sql"], ["*"])
    dataset = p.snapshot()
    turn = p.ok("/api/action", dict(dataset=dataset, value="商品额及订单"))
    req = dict(dataset=dataset, session=turn["session"])
    p.ok("/api/action", dict(req, action="sql", value="SELECT sales FROM analysis_result"))
    key = p.ok("/api/action", dict(req, action="queue_export"))["job"]
    assert p.ok("/api/action", dict(req, action="queue_export")) == {"job": key, "reused": True}
    assert p.get(f"/api/files/{key}").status_code == 404
    p.ok("/api/action", dict(req, action="sql", value="SELECT sales+1 AS sales FROM analysis_result"))
    later = p.ok("/api/action", dict(req, action="queue_export"))["job"]
    assert later != key
    p.access.change_access("ops", "analyst", ["export"], ["*"])
    run_pending(p.client.app)
    assert p.get(f"/api/jobs/{key}").status_code == 404
    p.access.change_access("ops", "analyst", ["export", "sql"], ["*"])
    assert p.get(f"/api/jobs/{key}").json()["status"] == "failed"
    assert p.get(f"/api/jobs/{later}").json()["status"] == "failed"
    p.ok(f"/api/jobs/{key}/retry", {})
    run_pending(p.client.app)
    export = p.get(f"/api/files/{key}")
    assert export.is_success
    data = list(csv.reader(io.StringIO(export.content.decode("utf-8-sig"))))
    assert data == [["sales"], ["110.00"]]
    p.access.change_access("ops", "analyst", ["export"], ["*"])
    assert p.get(f"/api/files/{key}").status_code == 404
    assert p.get("/api/operations", "reader").status_code == 403
    assert any(r["kind"] == "job_export" and r["status"] == "failed"
               for r in p.get("/api/operations", "admin").json()["daily"])


def test_weekly_checkpoints_cancel_retry_and_restore(pilot, monkeypatch):
    p = pilot
    dataset = p.snapshot()
    template = p.template(dataset)
    p.complete(dataset)
    weeks = [dict(dataset=dataset, template=template, as_of=d) for d in ("2025-02-03", "2025-02-10")]
    key = p.ok("/api/weekly/jobs", {"weeks": weeks})["job"]
    assert p.ok(f"/api/jobs/{key}/cancel", {})["status"] == "cancelled"
    p.ok(f"/api/jobs/{key}/retry", {})
    run_pending(p.client.app)
    job = p.get(f"/api/jobs/{key}").json()
    assert job["status"] == "failed" and len(job["progress"]) == 1
    report = job["progress"][0]["report"]
    p.ok(f"/api/weekly/reports/{report}/publish?recipient=reader", {})
    original = p.get(f"/api/weekly/reports/{report}", "reader").json()
    assert original["overview"][0]["sales_current"] == "100.00"
    p.complete(dataset, start="2025-02-03", end="2025-02-10")
    p.ok(f"/api/jobs/{key}/retry", {})
    # Capture a queued retry as well as the published report and evidence.
    (p.root / ".env").write_text("LOCAL_TEST_SECRET=sensitive", encoding="utf-8")
    artifact = p.root / "runs" / "legacy.csv"
    artifact.write_text("example", encoding="utf-8")
    legacy = p.access.save_resource("file", p.access.member("ops"), [dataset], [], artifact)
    with archive_connection(p.access.path) as db:
        db.execute("UPDATE resources SET payload=? WHERE id=?", (json.dumps(str(artifact)), legacy))
    target = p.root / "backup.zip"
    backup(p.root, target)
    restored = p.root / "restored"
    restore(target, restored)
    with ZipFile(target) as archive:
        assert all(".env" not in name for name in archive.namelist())
    assert not (restored / ".env").exists()
    for module in (config, evidence, trace):
        monkeypatch.setattr(module, "RUNS_DIR", restored / "runs")
    app = team.create_app(restored / "data", ReportPlanner)
    run_pending(app)
    with TestClient(app) as client:
        assert client.get(f"/api/weekly/reports/{report}", auth=("reader", "pw")).json() == original
        assert client.get(f"/api/files/{legacy}", auth=("ops", "pw")).content == b"example"
        job = client.get(f"/api/jobs/{key}", auth=("ops", "pw")).json()
        assert job["status"] == "succeeded" and len(job["progress"]) == 2
        assert job["progress"][0]["report"] == report and job["progress"][0]["reused"] is False
        result = client.post("/api/action", json=dict(dataset=dataset, value="商品额及订单"),
                             auth=("ops", "pw"), headers={"X-Analysis-Action": "1"})
        assert result.is_success and result.json()["result"]["rows"] == [[4, "110.00"]]
        proof = client.post("/api/action", json=dict(dataset=dataset, session=result.json()["session"], action="proof"),
                            auth=("ops", "pw"), headers={"X-Analysis-Action": "1"})
        assert proof.is_success and proof.json()["database_sha256"] == original["snapshot"]
        order = client.post("/api/action", json=dict(dataset=dataset, action="order", value="o1"),
                            auth=("finance", "pw"), headers={"X-Analysis-Action": "1"})
        assert order.is_success and any(row["values"].get("price") == "100.00" for row in order.json()["records"])
