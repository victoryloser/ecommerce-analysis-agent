from dataclasses import dataclass

from app.answers import answer, explain
from app.evidence import write_evidence, write_artifact
from app.executor import Executor
from app.planner import make_plan
from app.providers import Provider
from app.semantic import PlanError
from app.session import Session
from app.session_store import SessionStore
from app.trace import TurnTrace
from app.trace import BudgetError


@dataclass
class Turn:
    text: str
    evidence: dict


class AnalysisAgent:
    def __init__(self, provider: Provider, executors: dict[str, Executor], store: SessionStore | None = None):
        self.provider = provider
        self.executors = executors
        self.store = store

    def respond(self, session: Session, question: str, practice_context: bool = False) -> Turn:
        with TurnTrace(session.session_id, session.dataset_id, question, self.provider.model if self.provider else "deterministic-resume") as trace:
            turn = self._respond(session, question, practice_context, trace)
            session.last_evidence = turn.evidence
            if turn.evidence.get("type") == "workflow":
                session.workflow_evidence = turn.evidence
            if self.store:
                self.store.save(session, self.executors[session.dataset_id].manifest)
            return turn

    def _respond(self, session: Session, question: str, practice_context: bool, trace: TurnTrace) -> Turn:
        if question.strip() in {"重新开始", "/reset"}:
            session.last_successful_plan = None
            session.last_result = None
            session.pending_plan = None
            session.history.clear()
            session.registered_plans.clear()
            session.transcript.clear()
            session.analysis_evidence = {}
            session.drill_stack.clear()
            session.sql_state.clear()
            session.workflow_evidence.clear()
            session.working_plan = None
            session.workflow_tasks.clear()
            session.focus_step = None
            session.comparison_state.clear()
            return Turn("已清空当前分析条件和待澄清问题。", {})
        executor = self.executors[session.dataset_id]
        try:
            plan, attempts = make_plan(self.provider, executor.manifest, session, question, practice_context, trace)
        except (PlanError, BudgetError) as error:
            text = str(error)
            trace.record("plan_rejected", reason=text)
            session.remember(question, text)
            return Turn(text, {"status": "invalid_plan", "error": text, "turn_id": trace.turn_id, "event_file": str(trace.event_file)})
        if plan.intent == "history":
            session.pending_plan = None
            if self.store is None:
                raise PlanError("历史检索需要启用会话存储")
            trace.consume("memory")
            hits = self.store.search(plan.history_query or "", executor.manifest, plan.history_start, plan.history_end)
            text = "未找到符合关键词与日期范围的历史记录。" if not hits else "\n\n".join(
                f"**{i+1}. {hit['question']}**\n\n时间：{hit['created_at']} · 会话：`{hit['session_id']}`\n\n{hit['answer'][:1800]}" for i, hit in enumerate(hits))
            evidence = write_artifact(session, executor.manifest, "history", question, {"hits": hits, "plan": plan.model_dump(mode="json")}, trace)
            session.remember(question, text)
            return Turn(text, evidence)
        if plan.intent == "workflow":
            return self.run_workflow(session, plan, question, attempts, trace)
        if plan.intent not in {"explain", "unsupported"}:
            session.working_plan = plan
            session.workflow_tasks = []
            session.focus_step = None
        turn = self.run_plan(session, plan, question, attempts, trace)
        session.remember(question, turn.text)
        return turn

    def checkpoint(self, session):
        if self.store:
            self.store.save(session, self.executors[session.dataset_id].manifest)

    def compare(self, session, request=None, *, resume=False):
        from app.snapshot_comparison import compare
        return compare(self, session, request, resume=resume)

    def save_workflow_progress(self, session, question, attempts, trace):
        evidence = write_artifact(session, self.executors[session.dataset_id].manifest, "workflow", question,
            {"plan": session.working_plan.model_dump(mode="json"), "steps": [task["evidence"] for task in session.workflow_tasks],
             "tasks": session.workflow_tasks, "focus_step": session.focus_step, "planning_calls": attempts}, trace)
        session.last_evidence = session.workflow_evidence = evidence
        self.checkpoint(session)
        return evidence

    def run_workflow(self, session, plan, question, attempts, trace):
        from app.workflow import reconcile_tasks, bind_step
        session.workflow_tasks = reconcile_tasks(session.working_plan, plan, session.workflow_tasks)
        session.working_plan = plan.model_copy(deep=True)
        if session.focus_step is None or session.focus_step > len(plan.steps):
            session.focus_step = 1
        self.save_workflow_progress(session, question, attempts, trace)
        for index, template in enumerate(plan.steps):
            task = session.workflow_tasks[index]
            if task["status"] in {"completed", "unsupported"}:
                continue
            step = template
            try:
                provenance = {"workflow_turn_id": trace.turn_id, "step": index+1}
                if step.source_step and step.intent != "clarify":
                    step, status, reason = bind_step(step, session.workflow_tasks)
                    if step is None:
                        task.update(status=status, reason=reason, text=reason)
                        self.save_workflow_progress(session, question, attempts, trace)
                        continue
                    source = session.workflow_tasks[template.source_step-1]["evidence"]
                    value = step.selected_order_id if template.bind_field == "order_id" else next(f.values for f in step.filters if f.field == template.bind_field)
                    provenance.update(source_record_id=source["record_id"], source_step=template.source_step, bind_field=template.bind_field, value=value)
                    trace.record("workflow_bound", **provenance)
                turn = self.run_plan(session, step, f"{question}（步骤{index+1}）", 0, trace, provenance=provenance)
                result = turn.evidence.get("result")
                status = "needs_input" if step.intent == "clarify" else "unsupported" if step.intent == "unsupported" else "failed" if result and result["status"] not in {"ok", "empty"} else "completed"
                task.update(status=status, reason=result.get("error") or "" if result else step.clarification or "",
                            evidence=turn.evidence, text=turn.text)
            except ValueError as error:
                task.update(status="pending" if isinstance(error, BudgetError) else "failed", reason=str(error), text=str(error))
            self.save_workflow_progress(session, question, attempts, trace)
        pending = next((i for i, step in enumerate(plan.steps) if step.intent == "clarify"), None)
        session.pending_plan = plan.model_copy(update={"intent": "clarify", "pending_intent": "workflow"}) if pending is not None else None
        needs_input = next((i for i, task in enumerate(session.workflow_tasks) if task["status"] == "needs_input"), None)
        if needs_input is not None:
            session.focus_step = needs_input + 1
        text = "\n\n".join(f"### 步骤{i+1} · {task['status']}\n\n{task['text']}" for i, task in enumerate(session.workflow_tasks))
        evidence = self.save_workflow_progress(session, question, attempts, trace)
        from app.presentation import display_evidence
        from app.models import AnalysisPlan, QueryResult
        shown = display_evidence(evidence)
        if shown.get("type") == "query":
            session.last_successful_plan = AnalysisPlan.model_validate(shown["plan"])
            session.last_result = QueryResult.model_validate(shown["result"])
            session.analysis_evidence = shown
        session.remember(question, text)
        return Turn(text, evidence)

    def update_conditions(self, session, update):
        from app.plan_update import apply_update
        from app.semantic import validate_plan
        base = session.working_plan or session.last_successful_plan
        if base is None:
            raise PlanError("请先建立分析")
        plan = base
        for change in update if isinstance(update, list) else [update]:
            if base.intent == "workflow" and change.target_step is None:
                change = change.model_copy(update={"target_step": session.focus_step})
            plan = apply_update(plan, change, self.executors[session.dataset_id].manifest)
        validate_plan(plan, self.executors[session.dataset_id].manifest)
        with TurnTrace(session.session_id, session.dataset_id, "更新分析条件", "condition-update") as trace:
            if plan.intent == "workflow":
                turn = self.run_workflow(session, plan, "更新分析条件", 0, trace)
            else:
                session.working_plan = plan
                turn = self.run_plan(session, plan, "更新分析条件", 0, trace)
            session.last_evidence = turn.evidence
            self.checkpoint(session)
            return turn

    def replay(self, session, dataset_id):
        from app.plan_update import replay_intent
        from app.semantic import validate_plan
        base = session.working_plan or session.last_successful_plan
        if base is None:
            raise PlanError("当前没有可重算的分析意图")
        manifest = self.executors[dataset_id].manifest
        plan = replay_intent(base, manifest)
        validate_plan(plan, manifest)
        fresh = Session(dataset_id)
        provenance = {"source_session_id": session.session_id, "source_dataset_id": session.dataset_id,
                      "source_record_id": session.analysis_evidence.get("record_id"), "intent": base.model_dump(mode="json")}
        with TurnTrace(fresh.session_id, dataset_id, "在新快照重算", "snapshot-replay") as trace:
            if plan.intent == "workflow":
                turn = self.run_workflow(fresh, plan, "在新快照重算", 0, trace)
                turn.evidence = write_artifact(fresh, manifest, "workflow", "在新快照重算",
                    {"plan": turn.evidence["plan"], "steps": turn.evidence["steps"], "tasks": fresh.workflow_tasks,
                     "focus_step": fresh.focus_step, "provenance": provenance}, trace)
                fresh.workflow_evidence = turn.evidence
            else:
                fresh.working_plan = plan
                turn = self.run_plan(fresh, plan, "在新快照重算", 0, trace, provenance=provenance)
                fresh.remember("在新快照重算", turn.text)
            fresh.last_evidence = turn.evidence
            self.checkpoint(fresh)
        return fresh, turn

    def quality_details(self, session):
        from app.facts import scope_ctes
        from app.quality import QUERY_SCHEMA
        from app.query_worker import run_query
        from app.sql_compiler import CompiledQuery
        from app.sql_guard import validate_sql
        plan = session.last_successful_plan
        if plan is None or plan.time_range is None:
            raise PlanError("请先选择一份分析结果")
        ctes, parameters = scope_ctes(plan)
        sql = "WITH " + ",".join(ctes) + " SELECT q.* FROM quality_issues q JOIN scoped_orders o ON o.order_id=q.order_id ORDER BY q.issue_id"
        validate_sql(sql, schema=QUERY_SCHEMA, exploration=True)
        executor = self.executors[session.dataset_id]
        with TurnTrace(session.session_id, session.dataset_id, "查看质量依据", "quality-evidence") as trace:
            trace.consume("query")
            result = run_query(executor.path, CompiledQuery(sql, tuple(parameters)))
            evidence = write_artifact(session, executor.manifest, "quality_details", "查看质量依据",
                {"result": result.model_dump(mode="json"), "sql": sql, "parameters": parameters,
                 "source_record_id": session.analysis_evidence["record_id"]}, trace)
            return Turn("当前分析范围的质量记录；来源行号按导入表定位，完整候选值保留在记录中。", evidence)

    def run_plan(self, session, plan, question, attempts, trace, provenance=None):
        executor = self.executors[session.dataset_id]
        if plan.intent in {"clarify", "unsupported"}:
            session.pending_plan = plan if plan.intent == "clarify" else None
            text = plan.clarification
            evidence = write_evidence(session, executor.manifest, question, plan, trace.model, attempts, trace=trace, provenance=provenance)
        elif plan.intent == "explain":
            session.pending_plan = None
            text = explain(plan)
            evidence = write_evidence(session, executor.manifest, question, plan, trace.model, attempts, trace=trace, provenance=provenance)
        else:
            session.pending_plan = None
            trace.consume("query")
            plan_id = executor.register(session, plan)
            self.checkpoint(session)
            trace.record("execution_requested", plan_id=plan_id)
            result, compiled = executor.execute_plan(session, plan_id, trace)
            trace.record("query_finished", status=result.status, query_id=result.query_id, sql=compiled.sql, parameters=compiled.parameters, error=result.error)
            text = answer(plan, result, executor.manifest)
            evidence = write_evidence(session, executor.manifest, question, plan, trace.model, attempts, result, compiled, trace, provenance=provenance)
            if result.status in {"ok", "empty"}:
                session.last_successful_plan = plan
                session.last_result = result
                session.analysis_evidence = evidence
                session.drill_stack.clear()
        return Turn(text, evidence)

    def drill(self, session: Session, row_index: int, target: str) -> Turn:
        from app.drilldown import drill_plan
        executor = self.executors[session.dataset_id]
        from app.presentation import display_evidence
        parent = display_evidence(session.last_evidence)
        if parent.get("type") != "query" or parent != session.analysis_evidence:
            raise PlanError("当前展示的不是可下钻的分析结果")
        plan = drill_plan(session.last_successful_plan, session.last_result, row_index, target, executor.manifest)
        selected = dict(zip(session.last_result.columns, session.last_result.rows[row_index]))
        question = f"下钻：{selected.get('entity_name', '当前范围')} → {target}"
        with TurnTrace(session.session_id, session.dataset_id, question, "deterministic-drilldown") as trace:
            trace.record("drill_requested", parent_record_id=parent["record_id"], row_index=row_index, entity_id=selected.get("entity_id", "all"))
            plan_id = executor.register(session, plan)
            trace.consume("query")
            result, compiled = executor.execute_plan(session, plan_id, trace)
            trace.record("query_finished", status=result.status, query_id=result.query_id, error=result.error)
            evidence = write_evidence(session, executor.manifest, question, plan, "deterministic-drilldown", 0, result, compiled, trace,
                                      provenance={"parent_record_id": parent["record_id"], "entity_id": selected.get("entity_id", "all"), "target": target})
            text = answer(plan, result, executor.manifest)
            if result.status in {"ok", "empty"}:
                session.drill_stack.append(parent)
                session.last_successful_plan, session.last_result = plan, result
                session.working_plan = plan
                session.focus_step = None
                session.workflow_tasks.clear()
                session.analysis_evidence = evidence
                session.pending_plan = None
            session.last_evidence = evidence
            session.remember(question, text)
            if self.store:
                self.store.save(session, executor.manifest)
            return Turn(text, evidence)

    def back(self, session: Session) -> Turn:
        from app.models import AnalysisPlan, QueryResult
        if not session.drill_stack:
            raise PlanError("当前没有上层分析")
        evidence = session.drill_stack.pop()
        session.last_successful_plan = AnalysisPlan.model_validate(evidence["plan"])
        session.working_plan = session.last_successful_plan
        session.last_result = QueryResult.model_validate(evidence["result"])
        session.last_evidence = session.analysis_evidence = evidence
        session.pending_plan = None
        text = answer(session.last_successful_plan, session.last_result, self.executors[session.dataset_id].manifest)
        session.remember("返回上层分析", text)
        if self.store:
            self.store.save(session, self.executors[session.dataset_id].manifest)
        return Turn(text, evidence)

    def page(self, session: Session, delta: int, page_size: int = 100) -> Turn:
        from app.models import AnalysisPlan
        from app.presentation import display_evidence
        evidence = display_evidence(session.last_evidence)
        previous = evidence.get("result")
        if not previous or evidence["type"] not in {"query", "sql_exploration"}:
            raise PlanError("当前没有可分页的查询结果")
        if delta > 0 and not previous["truncated"]:
            raise PlanError("已到最后一页")
        offset = max(0, previous.get("row_offset", 0) + delta*int(page_size))
        if evidence["type"] == "sql_exploration":
            from app.sql_workspace import SQLWorkspace, sql_answer
            state = session.sql_state
            new = SQLWorkspace(self.executors, self.store).execute(session, state["sql"], state["parameters"], state["scope"], offset=offset, limit=int(page_size))
            return Turn(sql_answer(new), new)
        plan = AnalysisPlan.model_validate(evidence["plan"])
        executor = self.executors[session.dataset_id]
        with TurnTrace(session.session_id, session.dataset_id, f"结果翻页 offset={offset}", "pagination") as trace:
            trace.consume("query")
            result, compiled = executor.execute_plan(session, executor.register(session, plan), trace, offset=offset, limit=int(page_size))
            text = answer(plan, result, executor.manifest)
            new = write_evidence(session, executor.manifest, "结果翻页", plan, "pagination", 0, result, compiled, trace,
                                 provenance={"parent_record_id": evidence["record_id"], "row_offset": offset})
            if result.status in {"ok", "empty"}:
                session.last_result = result
                session.last_successful_plan = plan
                session.analysis_evidence = new
            session.last_evidence = new
            if self.store:
                self.store.save(session, executor.manifest)
            return Turn(text, new)

    def export(self, session: Session) -> dict:
        from app.config import RUNS_DIR
        from app.presentation import display_evidence
        from app.models import AnalysisPlan
        from app.sql_compiler import compile_query
        from app.sql_audit import audit_query
        from app.sql_guard import validate_sql
        from app.advanced import extended
        from app.query_worker import run_query
        from uuid import uuid4
        evidence = display_evidence(session.last_evidence)
        executor = self.executors[session.dataset_id]
        if evidence.get("type") == "sql_exploration":
            from app.sql_workspace import compile_exploration
            state = session.sql_state
            compiled, _ = compile_exploration(session, executor, state["sql"], state["parameters"], state["scope"])
        elif evidence.get("type") == "query":
            plan = AnalysisPlan.model_validate(evidence["plan"])
            from app.semantic import validate_plan
            validate_plan(plan, executor.manifest)
            compiled = compile_query(plan)
            from app.quality import QUERY_SCHEMA
            validate_sql(compiled.sql, schema=QUERY_SCHEMA, exploration=extended(plan))
            audit_query(plan, compiled)
        else:
            raise PlanError("请先执行查询再导出")
        directory = RUNS_DIR / session.dataset_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid4().hex}-full.csv"
        with TurnTrace(session.session_id, session.dataset_id, "完整导出", "csv-export") as trace:
            trace.consume("query")
            result = run_query(executor.path, compiled, export_path=path)
            if result.status not in {"ok", "empty"}:
                path.unlink(missing_ok=True)
                raise ValueError(result.error)
            import hashlib
            return write_artifact(session, executor.manifest, "export", "完整CSV导出",
                {"source_record_id": evidence["record_id"], "csv_file": str(path), "csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                 "row_count": result.row_count, "sql": compiled.sql, "parameters": compiled.parameters}, trace)
