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

    def summary(self) -> dict:
        result = self.last_result
        return {"working_plan": self.working_plan.model_dump(mode="json", exclude_defaults=True) if self.working_plan else None,
                "focus_step": self.focus_step,
                "workflow_tasks": [{k: task[k] for k in ("status", "reason")} for task in self.workflow_tasks],
                "last_successful_plan": self.last_successful_plan.model_dump(mode="json", exclude_defaults=True) if self.last_successful_plan else None,
                "pending_plan": self.pending_plan.model_dump(mode="json", exclude_defaults=True) if self.pending_plan else None,
                "last_result": {"query_id": result.query_id, "status": result.status, "columns": result.columns, "preview": result.rows[:3]} if result else None}

    def remember(self, question: str, answer: str) -> None:
        self.transcript.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
        self.history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer[:1600]}])
        self.history = self.history[-8:]
