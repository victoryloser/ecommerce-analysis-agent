import multiprocessing

import pytest

from app.demo import practice_plans
from app.models import EntityFilter
from app.query_worker import run_query
from app.semantic import PlanError, validate_plan
from app.session import Session
from app.sql_compiler import CompiledQuery
from app.sql_guard import validate_sql


@pytest.mark.parametrize("sql", [
    "DROP TABLE orders", "SELECT 1; SELECT 2", "ATTACH 'other.db' AS other",
    "SELECT * FROM read_csv_auto('secret.csv')", "SELECT * FROM sqlite_scan('file', 'orders')",
    "SELECT * FROM other.orders", "SELECT * FROM unknown_table", "SELECT getenv('HOME')",
    "WITH a AS (SELECT * FROM read_parquet('https://example.com/a')) SELECT * FROM a",
    "WITH a AS (DELETE FROM orders RETURNING *) SELECT * FROM a",
])
def test_non_compiler_sql_is_rejected(sql):
    with pytest.raises(ValueError):
        validate_sql(sql)


def test_payment_allocation_is_not_invented(executor):
    plan = practice_plans(executor.manifest)["Q04"].model_copy(update={"metric_ids": ["payment_amount"]})
    with pytest.raises(PlanError, match="分摊"):
        validate_plan(plan, executor.manifest)


def test_entity_is_bound_not_interpolated(executor):
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"filters": [EntityFilter(field="seller_id", values=["S001' OR 1=1 --"])]})
    with pytest.raises(PlanError, match="字典外"):
        validate_plan(plan, executor.manifest)


def test_plan_id_is_scoped_to_session(executor):
    first = Session(executor.manifest["dataset_id"])
    second = Session(executor.manifest["dataset_id"])
    plan_id = executor.register(first, practice_plans(executor.manifest)["Q01"])
    with pytest.raises(KeyError):
        executor.execute_plan(second, plan_id)


def test_missing_time_requires_clarification(executor):
    plan = practice_plans(executor.manifest)["Q01"].model_copy(update={"time_range": None})
    with pytest.raises(PlanError, match="时间"):
        validate_plan(plan, executor.manifest)


def test_timeout_kills_worker(executor):
    before = {p.pid for p in multiprocessing.active_children()}
    result = run_query(executor.path, CompiledQuery("SELECT SUM(a.i*b.i) FROM range(100000000) a(i) CROSS JOIN range(100000000) b(i)", ()), timeout=1)
    assert result.status == "timeout"
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_worker_disables_external_access(executor, tmp_path):
    path = tmp_path / "outside.csv"
    path.write_text("a\n1\n", encoding="utf-8")
    result = run_query(executor.path, CompiledQuery("SELECT * FROM read_csv(?)", (str(path),)))
    assert result.status == "error" and "disabled" in result.error.lower()


def test_read_only_worker_rejects_writes(executor):
    result = run_query(executor.path, CompiledQuery("DELETE FROM orders", ()))
    assert result.status == "error" and "read-only" in result.error.lower()
