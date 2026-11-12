from app.evaluate import evaluate
from app.evaluation_cases import cases_for


class ScriptedProvider:
    model = "evaluation-test-scripted"

    def __init__(self, plans):
        self.plans = iter(plans)

    def complete(self, messages):
        return next(self.plans).model_dump_json()


def test_failed_case_is_counted_and_next_case_runs(executor, tmp_path, monkeypatch):
    monkeypatch.setattr("app.evaluate.RUNS_DIR", tmp_path)
    cases = {c.case_id: c for c in cases_for(executor.manifest, "extended")}
    wrong = cases["V01"].expected_plan.model_copy(update={"intent": "unsupported", "clarification": "错误拒绝"})
    provider = ScriptedProvider([wrong, cases["V02"].expected_plan])
    report = evaluate(provider, executor, "extended", ["V01", "V02"])
    assert report["total"] == report["completed"] == 2
    assert report["passed"] == 1
    assert not report["cases"][0]["passed"] and report["cases"][1]["passed"]


def test_data_coverage_refusal_can_happen_before_execution(executor, tmp_path, monkeypatch):
    monkeypatch.setattr("app.evaluate.RUNS_DIR", tmp_path)
    case = next(c for c in cases_for(executor.manifest, "extended") if c.case_id == "B01")
    refusal = case.expected_plan.model_copy(update={"intent": "unsupported", "clarification": "2030年超出材料覆盖范围"})
    report = evaluate(ScriptedProvider([refusal]), executor, "extended", ["B01"])
    assert report["passed"] == report["total"] == 1


def test_causal_refusal_requires_missing_evidence_reason(executor, tmp_path, monkeypatch):
    monkeypatch.setattr("app.evaluate.RUNS_DIR", tmp_path)
    case = next(c for c in cases_for(executor.manifest, "extended") if c.case_id == "B02")
    correct = case.expected_plan.model_copy(update={"intent": "unsupported", "clarification": "没有促销活动数据，无法证明因果关系"})
    wrong = case.expected_plan.model_copy(update={"intent": "unsupported", "clarification": "促销一定造成了增长"})
    assert evaluate(ScriptedProvider([correct]), executor, "extended", ["B02"])["passed"] == 1
    assert evaluate(ScriptedProvider([wrong]), executor, "extended", ["B02"])["passed"] == 0
