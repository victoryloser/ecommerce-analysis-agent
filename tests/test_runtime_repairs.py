import json
from pathlib import Path
import time
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app import review, snapshot_comparison, snapshots, team, team_backup
from app.session_store import archive_connection
from app.team_access import TeamAccess
from test_snapshot_comparison import setup, tables
from test_weekly import pilot


@pytest.mark.parametrize("unavailable", ["expired", "missing"])
def test_retry_skips_unavailable_export(tmp_path, unavailable):
    web = team.create_app(tmp_path / "data")
    jobs = web.state.jobs
    jobs.access.set_member("admin", "pw", "admin", [], [])
    member = jobs.access.member("admin")
    params = {"dataset": "same", "record": "same"}
    old = jobs.enqueue(member, "export", params)["job"]
    jobs.claim()
    jobs.finish(old, "failed", error="interrupted")
    later = jobs.enqueue(member, "export", params)["job"]
    jobs.claim()
    path = tmp_path / "result.csv"
    path.write_text("amount\n10\n", encoding="utf-8")
    jobs.finish(later, "succeeded", result={"download": later}, file=(member, [], [], path))
    client = TestClient(web, headers={"X-Analysis-Action": "1"})
    assert client.post(f"/api/jobs/{old}/retry", auth=("admin", "pw")).json() == {"job": later, "reused": True}
    if unavailable == "expired":
        with archive_connection(jobs.path) as db:
            db.execute("UPDATE resources SET expires=0 WHERE id=?", (later,))
    else:
        path.unlink()
    queued = client.post(f"/api/jobs/{old}/retry", auth=("admin", "pw")).json()
    assert queued["job"] == old and queued["status"] == "queued"
    assert jobs.enqueue(member, "export", params) == {"job": old, "reused": True}


def test_operational_retention_without_new_events(tmp_path):
    jobs = team.create_app(tmp_path / "data").state.jobs
    jobs.access.log_days = 1
    now = int(time.time())
    with archive_connection(jobs.path) as db:
        db.executemany("INSERT INTO operational_events VALUES(?,?,?,?,?,?)", [
            (now - 172800, "", "model", "historical", 12, None),
            (now, "", "query", "ok", 5, None),
        ])
    assert jobs.operations.model_status() is None
    assert [(r["kind"], r["count"]) for r in jobs.operations.daily(7)] == [("query", 1)]
    with archive_connection(jobs.path) as db:
        assert db.execute("SELECT COUNT(*) FROM operational_events").fetchone()[0] == 1


def test_backup_carries_verified_legacy_sources(tmp_path, monkeypatch):
    instance = tmp_path / "old"
    directory = instance / "data"
    TeamAccess(directory)
    database = directory / "old.duckdb"
    database.write_bytes(b"frozen database")
    original = instance / "material" / "orders.csv"
    original.parent.mkdir()
    original.write_bytes(b"order_id\nO1\n")
    monkeypatch.setattr(snapshots, "ROOT", instance)
    monkeypatch.setattr(team_backup, "ROOT", instance)
    source = {"source_file": r"material\orders.csv", "sha256": snapshots.file_sha256(original)}
    snapshot = {"dataset_id": "old", "sources": [source], "database_sha256": snapshots.file_sha256(database)}
    (directory / "old.manifest.json").write_text(json.dumps(snapshot), encoding="utf-8")
    archive = tmp_path / "ok.zip"
    team_backup.backup(instance, archive)
    expected = original.read_bytes()
    original.unlink()
    target = tmp_path / "restored"
    team_backup.restore(archive, target)
    assert json.loads((target / "data/old.manifest.json").read_text(encoding="utf-8")) == snapshot
    assert snapshots.source_path(source, target / "data").read_bytes() == expected

    incomplete = tmp_path / "old-version.zip"
    with ZipFile(archive) as reader, ZipFile(incomplete, "w") as writer:
        description = json.loads(reader.read(team_backup.MANIFEST))
        portable = "data/" + snapshots.legacy_source_path(source).as_posix()
        del description["files"][portable]
        for name in description["files"]:
            writer.writestr(name, reader.read(name))
        writer.writestr(team_backup.MANIFEST, json.dumps(description))
    with pytest.raises(ValueError, match="必要文件"):
        team_backup.restore(incomplete, tmp_path / "incomplete-restore")
    assert not (tmp_path / "incomplete-restore").exists()

    original.write_bytes(expected)
    database.write_bytes(b"changed")
    with pytest.raises(ValueError, match="old.duckdb"):
        team_backup.backup(instance, tmp_path / "changed.zip")
    assert not (tmp_path / "changed.zip").exists()


def test_legacy_windows_retained_path(tmp_path):
    entry = {"retained_file": r"retained\v1\orders.csv"}
    assert snapshots.source_path(entry, tmp_path) == tmp_path / "retained/v1/orders.csv"


def test_weekly_revoked_during_batch_preserves_completed_weeks(pilot, monkeypatch):
    p = pilot
    dataset = p.snapshot()
    template = p.template(dataset)
    p.complete(dataset, end="2025-02-10")
    weeks = [dict(dataset=dataset, template=template, as_of=day) for day in ("2025-02-03", "2025-02-10")]
    key = p.ok("/api/weekly/jobs", {"weeks": weeks})["job"]
    jobs = p.client.app.state.jobs
    original = jobs.checkpoint

    def revoke_after_first(key, result=None):
        progress = original(key, result)
        if result is not None and len(progress) == 1:
            p.access.change_access("ops", "reader", [], ["*"])
        return progress

    monkeypatch.setattr(jobs, "checkpoint", revoke_after_first)
    assert jobs.run_one(p.client.app.state.visible)
    with archive_connection(jobs.path) as db:
        row = jobs.read(db, key)
        first = json.loads(row["progress"])
        assert row["status"] == "failed" and "权限" in row["error"] and len(first) == 1
    p.access.change_access("ops", "analyst", [], ["*"])
    assert p.ok(f"/api/jobs/{key}/retry", {})["status"] == "queued"
    assert jobs.run_one(p.client.app.state.visible)
    result = p.get(f"/api/jobs/{key}").json()
    assert result["status"] == "succeeded" and len(result["progress"]) == 2
    assert result["progress"][:1] == first


def test_comparison_publish_interruption_and_restored_locations(setup, tmp_path, monkeypatch):
    agent, session = setup
    original_runs = snapshot_comparison.RUNS_DIR
    def interrupted(*args):
        raise OSError("publication interrupted")
    with monkeypatch.context() as patch:
        patch.setattr(snapshot_comparison, "publish_bundle", interrupted)
        with pytest.raises(OSError, match="publication interrupted"):
            agent.compare(session, {"target_dataset_id": "new", "source": "working_plan"})
    manifest = agent.executors["old"].manifest
    stored = agent.store.load(session.session_id, manifest)
    pending = stored.comparison_state
    assert pending["status"] != "completed" and not stored.transcript
    source_id = pending["stages"]["source"]["evidence"]["record_id"]
    assert all(stage["status"] == "completed" for stage in pending["stages"].values())
    with monkeypatch.context() as patch:
        patch.setattr(snapshot_comparison, "implementation_hash", lambda: "different release")
        with pytest.raises(ValueError, match="实现已改变"):
            agent.compare(stored, resume=True)
    completed = agent.compare(stored, resume=True).evidence
    assert len(stored.transcript) == 2
    assert completed["stages"]["source"]["evidence"]["record_id"] == source_id
    new_runs = tmp_path / "restored-runs"
    original_runs.rename(new_runs)
    directory = new_runs / "old/comparisons" / completed["comparison_id"]
    (directory / "comparison.zip").write_bytes(b"interrupted legacy archive")
    monkeypatch.setattr(snapshot_comparison, "RUNS_DIR", new_runs)
    monkeypatch.setattr(snapshot_comparison, "implementation_hash", lambda: "different release")
    restored = agent.store.load(session.session_id, manifest)
    state = agent.compare(restored, resume=True).evidence
    assert Path(state["report_file"]) == directory / "report.json"
    assert Path(state["bundle_file"]) == directory / "comparison.zip"
    assert Path(state["stages"]["diff"]["report"]["csv_file"]) == directory / "differences.csv"
    with ZipFile(state["bundle_file"]) as archive:
        assert archive.testzip() is None and json.loads(archive.read("source_evidence.json"))["record_id"] == source_id

    old_report = rf"C:\original\runs\old\comparisons\{completed['comparison_id']}\report.json"
    monkeypatch.setattr(review, "read_evidence", lambda *args: {"version": {"status": "completed", "report_file": old_report}})
    with ZipFile(state["bundle_file"], "w") as partial:
        partial.write(directory / "report.json", "report.json")
    restored.reviews["historical-card"] = "old"
    card = review.load_review(agent, restored, "historical-card")
    assert Path(card["version"]["bundle_file"]) == directory / "comparison.zip"
    with ZipFile(card["version"]["bundle_file"]) as archive:
        assert "target_evidence.json" in archive.namelist()
