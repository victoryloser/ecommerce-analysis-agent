"""Small business task bundles expanded into the existing audited workflow."""
from app.models import AnalysisPlan


def expand_output(plan: AnalysisPlan):
    mode = plan.output_mode
    if mode == "analysis" or plan.intent in {"workflow", "clarify", "unsupported", "explain"}:
        return plan
    base = plan.model_copy(update={"output_mode": "analysis", "updates": [], "update": None,
        "steps": [], "source_step": None, "binding_updates": [], "limitations": [],
        "dimension": "total", "group_by": None, "rank_within": None, "contribution_within": None,
        "min_group_count": 1, "bind_mode": "field", "bind_field": None})
    if mode == "quality_check":
        steps = [base.model_copy(update={"intent": "diagnose", "metric_ids": [], "diagnosis": name})
                 for name in ("quality_summary", "quality_rows")]
    else:
        base = base.model_copy(update={"comparison_range": None, "comparison_mode": "none"})
        if mode == "customer_summary":
            steps = [base.model_copy(update={"intent": "aggregate", "metric_ids": [metric]})
                     for metric in ("order_count", "repeat_customers")]
        else:
            steps = [base.model_copy(update={"intent": "rank", "dimension": "seller",
                         "metric_ids": ["sales", "order_count"], "top_n": plan.top_n}),
                     base.model_copy(update={"intent": "diagnose", "metric_ids": [],
                         "diagnosis": "order_candidate", "source_step": 1, "bind_field": "seller_id"}),
                     base.model_copy(update={"intent": "diagnose", "metric_ids": [],
                         "diagnosis": "order_audit", "source_step": 2, "bind_field": "order_id"}),
                     base.model_copy(update={"intent": "details", "metric_ids": [],
                         "detail_level": "items", "source_step": 2, "bind_field": "order_id"})]
    return base.model_copy(update={"intent": "workflow", "output_mode": mode, "steps": steps,
                                  "limitations": plan.limitations})
