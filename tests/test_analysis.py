from decimal import Decimal

import pytest

from app.acceptance import check_result
from app.answers import explain
from app.demo import practice_plans
from app.session import Session


def execute(executor, plan):
    session = Session(executor.manifest["dataset_id"])
    result, _ = executor.execute_plan(session, executor.register(session, plan))
    assert result.status in {"ok", "empty"}, result.error
    return result


@pytest.mark.parametrize("qid", [f"Q{i:02}" for i in range(1, 8)])
def test_official_questions_against_source(executor, expected, qid):
    plan = practice_plans(executor.manifest)[qid]
    check_result(qid, execute(executor, plan), expected)


def test_join_explanation_has_no_query(executor):
    text = explain(practice_plans(executor.manifest)["Q08"])
    assert "N×M" in text and "分别按订单汇总" in text


def test_independent_control_values(dataset, expected, executor):
    count, sales = {"excel_full": (10714, "4858920.84"), "csv_small": (159, "75484.21")}[executor.manifest.get("source_kind") or dataset]
    assert expected["Q01"] == count
    assert expected["Q02"] == Decimal(sales)


def test_payments_and_sales_join_does_not_multiply(executor, expected):
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"metric_ids": ["sales", "order_count", "payment_amount"]})
    result = execute(executor, plan)
    assert result.rows == [[expected["Q02"], expected["Q01"], expected["payment_amount"]]]


def test_monthly_composition_with_changed_parameters(executor, expected):
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"metric_ids": ["sales", "order_count"], "dimension": "month"})
    result = execute(executor, plan)
    for month, _, sales, count in result.rows:
        assert sales == expected["monthly_sales"][month]
        assert count == expected["monthly_orders"][month]


def test_low_score_denominator(executor, expected):
    plan = practice_plans(executor.manifest)["Q06"].model_copy(update={"metric_ids": ["low_score_rate"]})
    result = execute(executor, plan)
    for group, _, rate, count in result.rows:
        assert float(rate) == pytest.approx(float(expected["Q06"][group]["low_score_rate"]))
        assert count == expected["Q06"][group]["count"]


def test_unknown_period_does_not_become_zero(executor):
    from app.models import TimeRange
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"time_range": TimeRange(start="2030-01-01", end="2030-02-01")})
    session = Session(executor.manifest["dataset_id"])
    result, _ = executor.execute_plan(session, executor.register(session, plan))
    assert result.status == "out_of_coverage" and not result.rows
