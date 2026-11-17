from dataclasses import dataclass, field
from uuid import uuid4

from app.models import AnalysisPlan, QueryResult


@dataclass
class Session:
    dataset_id: str
    session_id: str = field(default_factory=lambda: uuid4().hex)
    last_successful_plan: AnalysisPlan | None = None
    last_result: QueryResult | None = None
    pending_plan: AnalysisPlan | None = None
    history: list[dict] = field(default_factory=list)
    registered_plans: dict[str, AnalysisPlan] = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)
    last_evidence: dict = field(default_factory=dict)
    analysis_evidence: dict = field(default_factory=dict)
    drill_stack: list[dict] = field(default_factory=list)
    sql_state: dict = field(default_factory=dict)
    workflow_evidence: dict = field(default_factory=dict)
    working_plan: AnalysisPlan | None = None
    focus_step: int | None = None
    workflow_tasks: list[dict] = field(default_factory=list)
    comparison_state: dict = field(default_factory=dict)
    analysis_anchor: str | None = None
    view_focus: dict = field(default_factory=dict)

    def anchor_evidence(self):
        def search(record):
            if record.get("record_id") == self.analysis_anchor:
                return record
            for step in record.get("steps", []):
                found = search(step)
                if found:
                    return found
        for record in [self.analysis_evidence, self.workflow_evidence, *self.drill_stack]:
            found = search(record)
            if found:
                return found
        if self.analysis_anchor:
            import json
            from app.config import RUNS_DIR
            path = RUNS_DIR / self.dataset_id / (self.analysis_anchor + ".json")
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        return self.analysis_evidence

    def analysis_plan(self):
        record = self.anchor_evidence()
        return AnalysisPlan.model_validate(record["plan"]) if record.get("plan") else self.working_plan or self.last_successful_plan

    def view_plan(self, manifest):
        base = self.last_successful_plan
        if not self.view_focus.get("order_id"):
            return base
        if base is None:
            base = AnalysisPlan.model_validate(self.focused_evidence()["plan"])
        from app.models import TimeRange
        coverage = manifest.get("coverage_fields", {}).get(base.time_field, manifest["coverage"])
        return base.model_copy(update={"intent": "details", "metric_ids": [], "dimension": "total", "group_by": None,
            "rank_within": None, "contribution_within": None, "min_group_count": 1,
            "time_range": TimeRange(start=coverage["start"], end=coverage["end"]), "period_mode": "full_coverage",
            "comparison_range": None, "comparison_mode": "none", "selected_order_id": self.view_focus["order_id"],
            "filters": [], "order_where": None, "item_where": None, "detail_level": "orders",
            "output_mode": "analysis", "steps": [], "update": None, "updates": [], "binding_updates": [],
            "source_step": None, "source_row": 0, "source_partition": None, "bind_field": None,
            "bind_mode": "field", "audit_seller_id": None, "limit_mode": "top_n", "limitations": []})

    def selected_workflow(self):
        if self.view_focus.get("kind") == "inspection":
            if self.last_evidence.get("record_id") == self.view_focus["workflow_id"]:
                return self.last_evidence
            import json
            from app.config import RUNS_DIR
            return json.loads((RUNS_DIR / self.dataset_id / (self.view_focus["workflow_id"] + ".json")).read_text(encoding="utf-8"))
        return self.workflow_evidence

    def focused_evidence(self):
        record_id = self.view_focus.get("record_id")
        for record in (self.last_evidence, self.analysis_evidence):
            if record.get("record_id") == record_id:
                return record
        if not record_id:
            return {}
        import json
        from app.config import RUNS_DIR
        return json.loads((RUNS_DIR / self.dataset_id / (record_id + ".json")).read_text(encoding="utf-8"))

    def summary(self) -> dict:
        result = self.last_result
        anchor = self.analysis_plan()
        return {"analysis_anchor": self.analysis_anchor, "view_focus": self.view_focus,
                "analysis_scope": anchor.model_dump(mode="json", exclude_defaults=True, exclude={"steps", "updates", "binding_updates"}) if anchor else None,
                "working_plan": self.working_plan.model_dump(mode="json", exclude_defaults=True) if self.working_plan else None,
                "focus_step": self.focus_step,
                "workflow_tasks": [{k: task[k] for k in ("status", "reason")} for task in self.workflow_tasks],
                "last_successful_plan": self.last_successful_plan.model_dump(mode="json", exclude_defaults=True) if self.last_successful_plan else None,
                "pending_plan": self.pending_plan.model_dump(mode="json", exclude_defaults=True) if self.pending_plan else None,
                "last_result": {"query_id": result.query_id, "status": result.status, "columns": result.columns, "preview": result.rows[:3]} if result else None}

    def remember(self, question: str, answer: str) -> None:
        self.transcript.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
        self.history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer[:1600]}])
        self.history = self.history[-8:]
