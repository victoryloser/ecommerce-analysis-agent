from copy import deepcopy
from decimal import Decimal
import csv
import json

import pytest

from test_semifinal_quality import tables
from app.import_data import import_tables
from app.models import AnalysisPlan, TimeRange
from app.snapshot_diff import compare_snapshots, run_diff
from app.source import SCHEMA


def pair(tmp_path, tables, revised, **updates):
    manifests = [import_tables(name, data, [], name, tmp_path) for name, data in (("old", tables), ("new", revised))]
    plans = [AnalysisPlan.model_validate({"dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"],
        "intent": "aggregate", "metric_ids": ["sales", "order_count", "payment_amount"],
        "time_range": {"start": "2027-04-01", "end": "2027-05-01"}, **updates}) for manifest in manifests]
    paths = [tmp_path / f"{name}.duckdb" for name in ("old", "new")]
    return paths, plans, manifests


def metric(report, name, role="current", entity="all"):
    return next(r for r in report["metric_comparisons"] if (r["metric"], r["period"], r["entity_id"]) == (name, role, entity))


def test_reorder_split_and_exact_order_equivalence(tmp_path, tables):
    revised = deepcopy(tables)
    payment = revised["order_payments"][2]
    payment["payment_value"] = 40
    revised["order_payments"].append({**payment, "payment_sequence": 2, "payment_value": 60})
    revised = {table: list(reversed(rows)) for table, rows in revised.items()}
    paths, plans, manifests = pair(tmp_path, tables, revised)
    report = compare_snapshots(paths, plans, tmp_path / "report")
    assert report["business_diff"]["d2_equivalent"] is True
    assert report["structural_diff"]["order_payments"]["added"] == 1
    assert report["quality_diff"]["changed"] == 0
    assert all(r["delta"] == 0 for r in report["metric_comparisons"])
    assert report["sql_counts"] == [15, 15]


def test_order_offsets_are_not_equivalence(tmp_path, tables):
    revised = deepcopy(tables)
    for index, amount in ((2, 110), (3, 90)):
        revised["order_items"][index]["price"] = amount
        revised["order_payments"][index]["payment_value"] = amount
    paths, plans, _ = pair(tmp_path, tables, revised)
    report = compare_snapshots(paths, plans, tmp_path / "report")
    assert metric(report, "sales")["delta"] == 0
    assert report["business_diff"]["revised_item_orders"] == 2
    assert report["business_diff"]["d2_equivalent"] is False
    assert report["business_diff"]["amount_revision_only"] is True


def test_same_order_item_offsets_still_count_as_revision(tmp_path, tables):
    item = tables["order_items"][2]
    tables["order_items"].append({**item, "order_item_id": 2})
    revised = deepcopy(tables)
    revised["order_items"][2]["price"] = 110
    revised["order_items"][-1]["price"] = 90
    paths, plans, _ = pair(tmp_path, tables, revised)
    report = compare_snapshots(paths, plans, tmp_path / "report")
    assert metric(report, "sales")["delta"] == 0
    assert report["business_diff"]["revised_item_orders"] == 1
    assert report["business_diff"]["revised_item_keys"] == 2


def test_threshold_entry_and_four_cell_bridge(tmp_path, tables):
    tables["order_items"][2]["price"] = 490
    revised = deepcopy(tables)
    revised["order_items"][2]["price"] = 510
    paths, plans, _ = pair(tmp_path, tables, revised, metric_ids=["sales", "order_count"], intent="compare",
        comparison_range={"start": "2027-03-01", "end": "2027-04-01"},
        order_where={"field": "order_sales", "operator": "gt", "values": [500]})
    report = compare_snapshots(paths, plans, tmp_path / "report")
    result = metric(report, "sales")
    assert result["delta"] == Decimal(510)
    assert result["membership"]["common_change"] == 0
    assert result["membership"]["entered_value"] == 510
    assert result["reconciled"] is True
    assert metric(report, "order_count")["delta"] == 1
    assert all(r["verified"] is True for r in report["reconciliation"])
    assert next(r for r in report["preview"] if r["layer"] == "orders" and r["key"] == "a1")["explanation"]["delta"]["sales"] == 20
    entry = next(r for r in report["preview"] if r["layer"] == "membership" and r["change"] == "entered")
    assert entry["explanation"]["source_presence"] == "existing_order"


def test_quality_candidate_order_and_candidate_change(tmp_path, tables):
    tables["order_items"].append({**tables["order_items"][2], "price": 120})
    reordered = {name: list(reversed(rows)) for name, rows in tables.items()}
    paths, plans, manifests = pair(tmp_path, tables, reordered)
    report = compare_snapshots(paths, plans, tmp_path / "same")
    assert report["quality_diff"]["changed"] == 0
    assert manifests[0]["semantic_fingerprints"] == manifests[1]["semantic_fingerprints"]
    assert not report["business_diff"]["amount_revision_only"]
    reordered["order_items"][0]["price"] = 150
    manifest = import_tables("changed", reordered, [], "changed", tmp_path)
    plan = plans[1].model_copy(update={"dataset_id": "changed"})
    report = compare_snapshots([paths[0], tmp_path / "changed.duckdb"], [plans[0], plan], tmp_path / "changed_report")
    assert report["structural_diff"]["order_items"]["changed"] == 0
    assert report["quality_diff"]["changed"] == 1
    assert metric(report, "sales")["delta"] is None
    assert metric(report, "sales")["before"]["state"] == "unknown"
    assert manifests[0]["semantic_fingerprints"]["quality"] != manifest["semantic_fingerprints"]["quality"]


@pytest.mark.parametrize("damage", ["missing_payment", "conflicting_price"])
def test_unknown_never_becomes_revision_zero_or_exit(tmp_path, tables, damage):
    revised = deepcopy(tables)
    if damage == "missing_payment":
        revised["order_payments"] = [r for r in revised["order_payments"] if r["order_id"] != "a1"]
        metric_id, predicate = "payment_amount", None
    else:
        revised["order_items"].append({**revised["order_items"][2], "price": 120})
        metric_id, predicate = "order_count", {"field": "order_sales", "operator": "gt", "values": [90]}
    paths, plans, _ = pair(tmp_path, tables, revised, intent="compare", comparison_range={"start": "2027-03-01", "end": "2027-04-01"}, order_where=predicate)
    report = compare_snapshots(paths, plans, tmp_path / "report")
    result = metric(report, metric_id)
    assert result["after"]["state"] == "unknown" and result["delta"] is None
    assert result["membership"]["unknown_count"] == 1
    assert result["membership"]["exited_count"] == 0
    assert next(r for r in report["reconciliation"] if r["metric"] == metric_id)["verified"] is None


def test_no_review_is_not_unknown(tmp_path, tables):
    paths, plans, _ = pair(tmp_path, tables, deepcopy(tables), metric_ids=["avg_review", "low_score_rate"])
    report = compare_snapshots(paths, plans, tmp_path / "report")
    assert all(r["before"]["state"] == r["after"]["state"] == "not_applicable" for r in report["metric_comparisons"])
    assert all(r["delta"] is None for r in report["metric_comparisons"])


def test_complete_zero_and_absent_group(tmp_path, tables):
    revised = deepcopy(tables)
    revised["order_items"][3]["product_id"] = "K2"
    paths, plans, _ = pair(tmp_path, tables, revised, metric_ids=["sales", "order_count"], dimension="category")
    report = compare_snapshots(paths, plans, tmp_path / "report")
    assert metric(report, "sales", entity="K2")["before"]["state"] == "absent"
    assert metric(report, "sales", entity="K2")["delta"] == 100
    assert metric(report, "sales", entity="K1")["delta"] == -100
    assert report["business_diff"]["amount_revision_only"] is False


def test_rule_or_scope_difference_blocks_numeric_revision(tmp_path, tables):
    paths, plans, _ = pair(tmp_path, tables, deepcopy(tables))
    report = compare_snapshots(paths, plans, tmp_path / "rules", comparable=False)
    assert all(r["delta"] is None and not r["comparable"] for r in report["metric_comparisons"])
    assert not report["business_diff"]["d2_equivalent"]
    plans[1] = plans[1].model_copy(update={"time_range": TimeRange(start="2027-03-01", end="2027-05-01")})
    report = compare_snapshots(paths, plans, tmp_path / "scope")
    assert "time_range" in report["scope_changes"]
    assert metric(report, "sales")["delta"] is None


def test_top_n_and_preview_do_not_truncate_comparison(tmp_path, tables):
    # 501 商品键的变化全部进入导出，排名只展示第一名也不裁掉其余分组。
    tables["order_items"] = [{**tables["order_items"][2], "order_item_id": i, "product_id": "K1" if i == 1 else "K2"} for i in range(1, 502)]
    revised = deepcopy(tables)
    for row in revised["order_items"]:
        row["price"] += 1
    paths, plans, _ = pair(tmp_path, tables, revised, intent="rank", top_n=1, metric_ids=["sales"], dimension="category")
    report = compare_snapshots(paths, plans, tmp_path / "report")
    assert report["business_diff"]["revised_item_keys"] == 501
    assert sum(r["delta"] for r in report["metric_comparisons"]) == 501
    assert metric(report, "sales", entity="K1")["displayed_before"] is False
    assert metric(report, "sales", entity="K2")["displayed_after"] is True
    assert len(report["preview"]) == 500
    with open(report["csv_file"], encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == report["diff_rows"] > 1000


def test_spawn_worker_and_timeout(tmp_path, tables):
    paths, plans, _ = pair(tmp_path, tables, deepcopy(tables))
    report = run_diff(paths, plans, tmp_path / "worker")
    assert report["status"] == "completed" and report["business_diff"]["d2_equivalent"]
    failed = run_diff(paths, plans, tmp_path / "timeout", timeout=0)
    assert failed["status"] == "timeout"


@pytest.mark.parametrize("metrics", [["delay_rate", "avg_review", "low_score_rate"], ["repeat_customers"]])
def test_nonadditive_metrics_use_independent_source_oracle(tmp_path, tables, metrics):
    from app.oracle import SourceOracle
    def review(oid, score):
        return {field: {"review_id": oid, "order_id": oid, "review_score": score}.get(field) for field in SCHEMA["order_reviews"]}
    tables["order_reviews"] = [review("a1", 1), review("a2", 5)]
    revised = deepcopy(tables)
    revised["order_reviews"][1]["review_score"] = 3
    revised["orders"][3]["delivered_at"] = "2027-04-05"
    revised["order_items"][3]["price"] = 50
    paths, plans, _ = pair(tmp_path, tables, revised, metric_ids=metrics,
        order_where={"field": "order_sales", "operator": "gt", "values": [80]})
    report = compare_snapshots(paths, plans, tmp_path / "report")
    before = SourceOracle("old", tables=tables).evaluate(plans[0])[0]
    after = SourceOracle("new", tables=revised).evaluate(plans[1])[0]
    for name in metrics:
        result = metric(report, name)
        assert float(result["before"]["value"]) == pytest.approx(float(before[name]))
        assert float(result["after"]["value"]) == pytest.approx(float(after[name]))
        assert result["membership"] is None


def test_grouped_count_deduplicates_and_null_group_aligns(tmp_path, tables):
    tables["order_items"].append({**tables["order_items"][2], "order_item_id": 2, "product_id": "K2"})
    tables["customers"][0]["customer_state"] = None
    revised = deepcopy(tables)
    revised["order_items"][-1]["price"] = 120
    paths, plans, _ = pair(tmp_path, tables, revised, dimension="category", metric_ids=["sales", "order_count"])
    report = compare_snapshots(paths, plans, tmp_path / "groups")
    assert metric(report, "order_count", entity="K1")["before"]["value"] == 2
    assert metric(report, "order_count", entity="K2")["before"]["value"] == 1
    assert all(r["reconciled"] is True for r in report["metric_comparisons"])
    plans = [plan.model_copy(update={"dimension": "customer_state"}) for plan in plans]
    report = compare_snapshots(paths, plans, tmp_path / "null_group")
    assert metric(report, "order_count", entity=None)["delta"] == 0
    assert metric(report, "sales", entity=None)["delta"] == 20


def test_group_absent_in_both_previous_periods_four_cell_is_zero(tmp_path, tables):
    tables["order_items"][2]["product_id"] = "K2"
    revised = deepcopy(tables)
    revised["order_items"][2]["price"] = 120
    paths, plans, _ = pair(tmp_path, tables, revised, dimension="category", metric_ids=["sales"], intent="compare",
        comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    report = compare_snapshots(paths, plans, tmp_path / "report")
    bridge = next(r for r in report["reconciliation"] if r["entity_id"] == "K2")
    assert bridge["values"] == [0, 100, 0, 120] and bridge["verified"]


def test_out_of_scope_missing_payment_does_not_poison_selected_metrics(tmp_path, tables):
    tables["orders"][0]["order_status"] = "canceled"
    tables["order_payments"] = [r for r in tables["order_payments"] if r["order_id"] != "m1"]
    paths, plans, _ = pair(tmp_path, tables, deepcopy(tables))
    report = compare_snapshots(paths, plans, tmp_path / "report")
    assert report["business_diff"]["unknown_orders"] == 1
    assert report["business_diff"]["d2_equivalent"]
    assert all(r["before"]["state"] == "known" for r in report["metric_comparisons"])
