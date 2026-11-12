import pytest

from app.acceptance import check_result
from app.demo import practice_plans
from app.evaluation_cases import cases_for
from app.models import QueryResult
from app.oracle import SourceOracle, check_rows
from app.session import Session


@pytest.fixture(scope="session")
def oracle(dataset):
    return SourceOracle(dataset)


@pytest.mark.parametrize("qid", [f"Q{i:02}" for i in range(1, 8)])
def test_oracle_agrees_with_original_baseline(executor, expected, oracle, qid):
    rows = oracle.evaluate(practice_plans(executor.manifest)[qid])
    result = QueryResult(query_id="independent-oracle", status="ok", columns=list(rows[0]), rows=[list(r.values()) for r in rows])
    check_result(qid, result, expected)


@pytest.mark.parametrize("cid", [f"V{i:02}" for i in range(1, 12)])
def test_parameter_variants_against_source(executor, oracle, cid):
    case = next(c for c in cases_for(executor.manifest, "extended") if c.case_id == cid)
    session = Session(executor.manifest["dataset_id"])
    result, _ = executor.execute_plan(session, executor.register(session, case.expected_plan))
    check_rows(result, oracle.evaluate(case.expected_plan))
