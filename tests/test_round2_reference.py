"""Optional local public-pack acceptance. Reference files never enter app runtime."""
import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.calculation import ROUND2_RULE
from app.import_data import import_tables
from app.oracle import SourceOracle, check_rows
from app.snapshot_diff import compare_snapshots
from app.source import SCHEMA
from app.answers import cell
from test_round2_amounts import direct, plan_for

PACKAGE = Path(__file__).resolve().parents[1] / "semi-material-2" / "公开复赛包_v1.1"
pytestmark = pytest.mark.skipif(not (PACKAGE / "数据集" / "D4").is_dir(), reason="本地未提供正式公开包，仓库不分发比赛材料")


@pytest.fixture(scope="module")
def public_data(tmp_path_factory):
    directory = tmp_path_factory.mktemp("round2-public")
    datasets = {}
    for name in ("D1", "D2", "D3", "D4"):
        files = [PACKAGE / "数据集" / name / (table + ".csv") for table in SCHEMA]
        tables = {}
        for table, path in zip(SCHEMA, files):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                tables[table] = list(csv.DictReader(handle))
        manifest = import_tables(name, tables, files, name, directory, quality_rule_version=ROUND2_RULE)
        reference = json.loads((PACKAGE / "参考结果" / (name + ".json")).read_text(encoding="utf-8"))["results"]
        datasets[name] = (manifest, reference, SourceOracle(name, tables, data_dir=directory))
    return directory, datasets


def cents(value):
    return Decimal(str(value)) * 100


@pytest.mark.parametrize("name", ["D1", "D2", "D3", "D4"])
def test_public_months_and_exact_growth(public_data, name):
    directory, datasets = public_data
    manifest, reference, oracle = datasets[name]
    plan, result = direct(directory, manifest, intent="compare", comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    check_rows(result, oracle.evaluate(plan, with_quality=True))
    row = dict(zip(result.columns, result.rows[0]))
    for month, role in (("2027-03", "previous"), ("2027-04", "current")):
        expected = reference["A1"]["months"][month]
        assert row["order_count_"+role] == expected["delivered_order_count"]
        assert cents(row["sales_"+role]) == expected["goods_amount_cents"]
        assert cents(row["freight_"+role]) == expected["freight_amount_cents"]
        q = result.quality[0]["sales_"+role]
        assert q["policy_eligible_order_count"] == expected["amount_eligible_order_count"]
        assert q["excluded_order_count"] == len(expected["amount_unknown_order_ids"])
    assert cents(row["sales_change"]) == reference["A1"]["change_cents"]
    assert cell("sales_rate", row["sales_rate"], plan) == reference["A1"]["growth_pct"] + "%"


@pytest.mark.parametrize("name", ["D1", "D2", "D3", "D4"])
def test_public_all_categories_and_contributions(public_data, name):
    directory, datasets = public_data
    manifest, reference, oracle = datasets[name]
    plan, result = direct(directory, manifest, intent="contribution", metric_ids=["sales"], dimension="category",
                          comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    check_rows(result, oracle.evaluate(plan, with_quality=True))
    assert len(result.rows) == len(reference["A2"]["category_changes"])
    for row, expected in zip(result.rows, reference["A2"]["category_changes"]):
        row = dict(zip(result.columns, row))
        assert row["entity_id"] == expected["category_id"]
        for field, target in (("sales_previous", "before_cents"), ("sales_current", "current_cents"), ("sales_change", "change_cents")):
            assert cents(row[field]) == expected[target]
        assert cell("contribution_rate", row["contribution_rate"], plan) == expected["contribution_pct"] + "%"


@pytest.mark.parametrize("name", ["D1", "D2", "D3", "D4"])
@pytest.mark.parametrize("question,month", [("A3", "03"), ("B3", "04")])
def test_public_filtered_amounts(public_data, name, question, month):
    directory, datasets = public_data
    manifest, reference, oracle = datasets[name]
    plan, result = direct(directory, manifest, time_range={"start": f"2027-{month}-01", "end": f"2027-{int(month)+1:02}-01"},
                          filters=[{"field": "category_id", "values": ["electronics"]}])
    check_rows(result, oracle.evaluate(plan, with_quality=True))
    sales, freight, orders = result.rows[0]
    expected = reference[question]
    assert cents(sales) == expected["goods_amount_cents"]
    assert cents(freight) == expected["freight_amount_cents"]
    assert orders == expected["delivered_order_count"]
    assert result.quality[0]["sales"]["policy_eligible_order_count"] == expected["amount_eligible_order_count"]


@pytest.mark.parametrize("name", ["D1", "D2", "D3", "D4"])
def test_public_seller_ties_orders_and_repeat_customers(public_data, name):
    directory, datasets = public_data
    manifest, reference, oracle = datasets[name]
    plan, result = direct(directory, manifest, intent="rank", metric_ids=["sales", "order_count"], dimension="seller", top_n=3,
                          time_range={"start": "2027-03-01", "end": "2027-04-01"})
    check_rows(result, oracle.evaluate(plan, with_quality=True))
    observed = {row[0]: dict(zip(result.columns, row)) for row in result.rows}
    assert set(observed) == {row["seller_id"] for row in reference["A4"]["ranking"]}
    for expected in reference["A4"]["ranking"]:
        row = observed[expected["seller_id"]]
        assert cents(row["sales"]) == expected["goods_amount_cents"]
        assert row["order_count"] == expected["delivered_order_count"]
    for metric, target in (("order_count", "delivered_order_count"), ("repeat_customers", "repeat_customer_count")):
        plan, result = direct(directory, manifest, metric_ids=[metric], time_range={k: manifest["coverage"][k] for k in ("start", "end")})
        check_rows(result, oracle.evaluate(plan, with_quality=True))
        assert result.rows[0][0] == reference["B4"][target]


def test_public_joint_freight_rollup_and_policy_bridge(public_data, tmp_path):
    directory, datasets = public_data
    manifest = datasets["D4"][0]
    _, joint = direct(directory, manifest, group_by=["category", "seller"])
    _, total = direct(directory, manifest)
    for metric in ("sales", "freight"):
        assert sum(row[joint.columns.index(metric)] for row in joint.rows) == total.rows[0][total.columns.index(metric)]
    reports = {}
    for before, after in (("D1", "D2"), ("D1", "D3"), ("D3", "D4")):
        report = compare_snapshots([directory / (name + ".duckdb") for name in (before, after)],
            [plan_for(datasets[name][0]) for name in (before, after)], tmp_path / (before + after))
        assert all(row["reconciled"] for row in report["metric_comparisons"])
        reports[before+after] = {r["metric"]: r for r in report["metric_comparisons"]}
    assert all(row["delta"] == 0 for row in reports["D1D2"].values())
    assert reports["D1D3"]["sales"]["delta"] == Decimal("1594.91")
    for metric, amount in (("sales", Decimal("798.41")), ("freight", Decimal("55.28"))):
        row = reports["D3D4"][metric]
        assert row["delta"] == -amount
        assert row["membership"]["eligibility_exited_value"] == amount
        assert row["membership"]["common_change"] == 0
