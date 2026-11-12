"""示例查询业务验证，期望值来自 baseline 的原始材料计算。"""
from decimal import Decimal
from math import isclose

from app.models import QueryResult


def check_result(qid: str, result: QueryResult, expected: dict) -> None:
    assert result.status == "ok", f"{result.status}: {result.error}"
    assert not result.truncated
    required = {
        "Q01": {"order_count"}, "Q02": {"sales"},
        "Q03": {"current_value", "previous_value", "change_rate"},
        "Q04": {"entity_id", "entity_name", "sales"},
        "Q05": {"entity_id", "entity_name", "delay_rate", "sample_orders", "delayed_orders"},
        "Q06": {"entity_id", "avg_review", "reviewed_orders"}, "Q07": {"repeat_customers"},
    }[qid]
    assert required <= set(result.columns), f"期望列 {required}，实际列 {result.columns}"
    assert result.rows, "缺少结果行"
    rows = [dict(zip(result.columns, row)) for row in result.rows]
    if qid == "Q01":
        assert rows[0]["order_count"] == expected[qid]
    elif qid == "Q02":
        assert Decimal(str(rows[0]["sales"])) == expected[qid]
    elif qid == "Q03":
        assert Decimal(str(rows[0]["current_value"])) == expected[qid]["current"]
        assert Decimal(str(rows[0]["previous_value"])) == expected[qid]["previous"]
        assert isclose(float(rows[0]["change_rate"]), float(expected[qid]["rate"]), abs_tol=1e-12)
    elif qid == "Q04":
        assert [(r["entity_id"], r["entity_name"], Decimal(str(r["sales"]))) for r in rows] == [(r["id"], r["name"], r["sales"]) for r in expected[qid]]
    elif qid == "Q05":
        assert [r["entity_id"] for r in rows] == [r["id"] for r in expected[qid]]
        for actual, wanted in zip(rows, expected[qid]):
            assert actual["entity_name"] == wanted["name"]
            assert isclose(float(actual["delay_rate"]), float(wanted["rate"]), abs_tol=1e-12)
            assert actual["sample_orders"] == wanted["orders"]
            assert actual["delayed_orders"] == wanted["late"]
    elif qid == "Q06":
        assert {r["entity_id"] for r in rows} == set(expected[qid])
        for row in rows:
            wanted = expected[qid][row["entity_id"]]
            assert isclose(float(row["avg_review"]), float(wanted["avg"]), abs_tol=1e-12)
            assert row["reviewed_orders"] == wanted["count"]
    elif qid == "Q07":
        assert rows[0]["repeat_customers"] == expected[qid]
    else:
        raise ValueError(f"无数值验收定义：{qid}")
