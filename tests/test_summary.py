import json

import pytest

from app.evaluation_summary import summarize
from app.semantic import load_manifest

def report(tmp_path, dataset, name, timestamp, passed, model="test-model"):
    manifest = load_manifest(dataset)
    path = tmp_path / name
    path.write_text(json.dumps({
        "model": model, "dataset_id": dataset, "created_at": timestamp,
        "database_sha256": manifest["database_sha256"],
        "metadata_sha256": manifest["metadata_sha256"], "implementation_sha256": "test-version",
        "cases": [{"case_id": "Q01", "category": "original", "passed": passed, "planning_calls": 1}],
    }), encoding="utf-8")
    return path


def test_later_failure_overrides_pass_and_missing_cases_remain_in_denominator(tmp_path, local_csv_dataset):
    first = report(tmp_path, local_csv_dataset, "first.json", "2026-11-11T01:00:00+00:00", True)
    second = report(tmp_path, local_csv_dataset, "second.json", "2026-11-11T02:00:00+00:00", False)
    result = summarize([second, first])
    assert result["passed"] == 0 and result["completed"] == 1
    assert result["total"] == 54 and len(result["missing"]) == 53
    assert result["superseded"][0]["passed"] is True


def test_different_models_are_not_merged(tmp_path, local_csv_dataset):
    first = report(tmp_path, local_csv_dataset, "first.json", "2026-11-11T01:00:00+00:00", True)
    second = report(tmp_path, local_csv_dataset, "second.json", "2026-11-11T02:00:00+00:00", True, model="another-model")
    with pytest.raises(ValueError, match="不同模型"):
        summarize([first, second])


def test_legacy_report_alias_requires_matching_snapshot(tmp_path, local_csv_dataset, monkeypatch):
    from app import evaluation_summary
    manifest = load_manifest(local_csv_dataset)
    dataset = "csv_small"
    (tmp_path / "aliases.json").write_text(json.dumps({dataset: local_csv_dataset}), encoding="utf-8")
    (tmp_path / f"{local_csv_dataset}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(evaluation_summary, "DATA_DIR", tmp_path)
    path = report(tmp_path, local_csv_dataset, "legacy.json", "2026-11-11T01:00:00+00:00", True)
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["dataset_id"] = dataset
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert summarize([path])["passed"] == 1
    legacy["database_sha256"] = "different-snapshot"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(ValueError, match="同一数据快照"):
        summarize([path])
