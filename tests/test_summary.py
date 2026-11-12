import json

import pytest

from app.evaluation_summary import summarize
from app.semantic import load_manifest

pytestmark = pytest.mark.usefixtures("local_csv_dataset")


def report(tmp_path, name, timestamp, passed, model="test-model"):
    path = tmp_path / name
    path.write_text(json.dumps({
        "model": model, "dataset_id": "csv_small", "created_at": timestamp,
        "database_sha256": load_manifest("csv_small")["database_sha256"],
        "metadata_sha256": load_manifest("csv_small")["metadata_sha256"], "implementation_sha256": "test-version",
        "cases": [{"case_id": "Q01", "category": "original", "passed": passed, "planning_calls": 1}],
    }), encoding="utf-8")
    return path


def test_later_failure_overrides_pass_and_missing_cases_remain_in_denominator(tmp_path):
    first = report(tmp_path, "first.json", "2026-11-11T01:00:00+00:00", True)
    second = report(tmp_path, "second.json", "2026-11-11T02:00:00+00:00", False)
    result = summarize([second, first])
    assert result["passed"] == 0 and result["completed"] == 1
    assert result["total"] == 48 and len(result["missing"]) == 47
    assert result["superseded"][0]["passed"] is True


def test_different_models_are_not_merged(tmp_path):
    first = report(tmp_path, "first.json", "2026-11-11T01:00:00+00:00", True)
    second = report(tmp_path, "second.json", "2026-11-11T02:00:00+00:00", True, model="another-model")
    with pytest.raises(ValueError, match="不同模型"):
        summarize([first, second])
