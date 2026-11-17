import hashlib
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

from app.config import DATA_DIR
from app.models import AnalysisPlan, QueryResult
from app.query_worker import run_query
from app.semantic import load_manifest, validate_plan
from app.session import Session
from app.sql_compiler import CompiledQuery, compile_query
from app.sql_guard import validate_sql
from app.sql_audit import audit_query
from app.quality import QUERY_SCHEMA
from app.calculation import CalculationContext


class Executor:
    def __init__(self, dataset_id: str, data_dir: Path = DATA_DIR):
        self.manifest = load_manifest(dataset_id, data_dir)
        self.context = CalculationContext.from_manifest(self.manifest)
        self.path = data_dir / f"{dataset_id}.duckdb"
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.manifest["database_sha256"]:
            raise ValueError("数据库快照与manifest不一致，请重新导入")

    def register(self, session: Session, plan: AnalysisPlan) -> str:
        if session.dataset_id != self.manifest["dataset_id"]:
            raise ValueError("执行器与会话数据集不一致")
        validate_plan(plan, self.manifest)
        if plan.intent not in {"aggregate", "rank", "compare", "join_example", "contribution", "details", "diagnose"}:
            raise ValueError("只能登记可执行分析计划")
        plan_id = uuid4().hex
        session.registered_plans[plan_id] = plan.model_copy(deep=True)
        return plan_id

    def execute_plan(self, session: Session, plan_id: str, trace=None, *, offset=0, limit=500) -> tuple[QueryResult, CompiledQuery]:
        if session.dataset_id != self.manifest["dataset_id"]:
            raise ValueError("执行器与会话数据集不一致")
        plan = session.registered_plans.pop(plan_id)
        compiled = compile_query(plan, self.context)
        if trace:
            trace.record("sql_compiled", sql=compiled.sql, parameters=compiled.parameters)
        from app.advanced import extended
        validate_sql(compiled.sql, schema=QUERY_SCHEMA, exploration=extended(plan))
        audit_query(plan, compiled, self.context)
        if trace:
            trace.record("sql_approved")
        if plan.time_field != "purchase_at" and plan.time_field not in self.manifest.get("coverage_fields", {}):
            return QueryResult(query_id=uuid4().hex, status="out_of_coverage", error=f"当前数据没有{plan.time_field}的日期覆盖记录"), compiled
        coverage = self.manifest.get("coverage_fields", {}).get(plan.time_field, self.manifest["coverage"])
        start, end = date.fromisoformat(coverage["start"]), date.fromisoformat(coverage["end"])
        periods = [plan.time_range, plan.comparison_range]
        if plan.intent == "diagnose" and plan.diagnosis in {"quality_summary", "quality_rows"}:
            if plan.quality_scope == "current":
                periods = [plan.time_range]
            elif plan.quality_scope == "previous":
                periods = [plan.comparison_range]
        for period in periods:
            if period and (period.start < start or period.end > end):
                result = QueryResult(query_id=uuid4().hex, status="out_of_coverage", error=f"请求期间超出数据覆盖范围 [{start}, {end})，未执行部分统计")
                return result, compiled
        started = time.perf_counter()
        if trace:
            trace.record("query_started", plan_id=plan_id)
        result = run_query(self.path, compiled, offset=offset, limit=limit)
        result.elapsed_ms = round((time.perf_counter() - started) * 1000)
        return result, compiled
