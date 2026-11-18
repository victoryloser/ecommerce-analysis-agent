"""自由 SQL 是独立的探索通道，不获得结构化计划的业务口径认证。"""
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from app.config import MAX_ROWS, RUNS_DIR
from app.metadata import table_detail
from app.models import Contract, QueryResult
from app.query_worker import run_query
from app.semantic import METRICS, RELATIONSHIPS, validate_plan
from app.source import SCHEMA
from app.quality import query_schema
from app.sql_audit import audit_query
from app.sql_compiler import CompiledQuery, compile_query
from app.sql_guard import EXPLORATION_FUNCTIONS, validate_sql
from app.trace import TurnTrace

Scope = Literal["analysis_result", "raw"]


class SQLDraft(Contract):
    sql: str
    explanation: str


def workspace_catalog(session, scope: Scope, manifest=None) -> dict:
    if scope == "raw":
        if manifest and manifest.get("capabilities"):
            from app.calculation import CalculationContext
            caps = manifest["capabilities"]
            schema = query_schema(CalculationContext.from_manifest(manifest))
            return {"scope": scope, "metrics": caps["metrics"], "tables": {
                t: {"fields": {k: {"type": schema[t][k]} for k in fields if k in schema[t] and
                              (k != "price" or "sales" in caps["metrics"])}} for t, fields in caps["fields"].items()}}
        return {"scope": scope, "tables": {table: table_detail(table) for table in SCHEMA},
                "metrics": METRICS, "relationships": RELATIONSHIPS}
    if scope != "analysis_result":
        raise ValueError("未知 SQL 数据范围")
    if session.last_result is None or session.last_successful_plan is None:
        raise ValueError("请先完成一次结构化分析，才能查询 analysis_result")
    return {"scope": scope, "tables": {"analysis_result": {"columns": session.last_result.columns + (["__quality"] if session.last_result.quality else [])}},
            "quality": session.last_result.quality,
            "working_plan": session.working_plan.model_dump(mode="json") if session.working_plan else None,
            "source_plan": session.last_successful_plan.model_dump(mode="json"),
            "source_record_id": session.analysis_evidence["record_id"]}


def compile_exploration(session, executor, sql: str, parameters: list, scope: Scope) -> tuple[CompiledQuery, dict]:
    if not isinstance(parameters, list) or any(isinstance(value, (dict, list)) for value in parameters):
        raise ValueError("绑定参数必须是标量值组成的JSON数组")
    catalog = workspace_catalog(session, scope, executor.manifest)
    schema = query_schema(executor.context)
    catalog["quality_rule_version"] = executor.context.rule_version
    if scope == "raw":
        raw_schema = {t: {f: d["type"] for f, d in table["fields"].items()} for t, table in catalog["tables"].items()}
        validate_sql(sql, schema=raw_schema, exploration=True)
        if executor.context.capabilities:
            # Expand stars to the mapped fields before DuckDB sees the physical table.
            tree = qualify(sqlglot.parse_one(sql, read="duckdb"), dialect="duckdb", schema=raw_schema)
            return CompiledQuery(tree.sql(dialect="duckdb"), tuple(parameters)), catalog
        return CompiledQuery(sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb"), tuple(parameters)), catalog
    plan = session.last_successful_plan
    validate_plan(plan, executor.manifest)
    base = compile_query(plan, executor.context)
    from app.advanced import extended
    validate_sql(base.sql, schema=schema, exploration=extended(plan))
    audit_query(plan, base, executor.context)
    # 用完整查询作CTE，不能把500行预览误作全量输入。
    base_tree = sqlglot.parse_one(base.sql, read="duckdb")
    qualified = qualify(base_tree.copy(), dialect="duckdb", schema=schema, quote_identifiers=False, identify=False)
    schema = {"analysis_result": {name: "UNKNOWN" for name in qualified.named_selects}}
    validate_sql(sql, schema=schema, exploration=True)
    tree = sqlglot.parse_one(sql, read="duckdb")
    if any(cte.alias == "analysis_result" for cte in tree.find_all(exp.CTE)):
        raise ValueError("analysis_result 是保留的数据源名称，不能被CTE覆盖")
    original_with = tree.args.get("with_")
    cte = exp.CTE(this=base_tree, alias=exp.TableAlias(this=exp.to_identifier("analysis_result")))
    tree.set("with_", exp.With(expressions=[cte, *(original_with.expressions if original_with else [])]))
    # Only transparent projections retain certified ratio components. Arbitrary
    # expressions remain custom SQL and are never silently rewritten.
    source = tree.args.get("from_")
    transparent = (isinstance(tree, exp.Select) and not original_with and not tree.args.get("joins")
                   and source is not None and isinstance(source.this, exp.Table) and source.this.name == "analysis_result"
                   and all(isinstance(e, exp.Column) or (isinstance(e, exp.Star) and not e.args) for e in tree.expressions))
    ratios = base.ratios if transparent else {}
    return CompiledQuery(tree.sql(dialect="duckdb"), base.parameters + tuple(parameters), ratios), catalog


class SQLWorkspace:
    def __init__(self, executors: dict, store=None):
        self.executors = executors
        self.store = store

    def draft(self, session, question: str, scope: Scope, provider) -> SQLDraft:
        from app.model_context import question_context, permitted
        manifest = self.executors[session.dataset_id].manifest
        catalog = workspace_catalog(session, scope, manifest)
        source_record_id = catalog.get("source_record_id")
        catalog["entities"] = question_context(question, manifest)
        if not permitted("scope"):
            for key in ("working_plan", "source_plan", "source_record_id"):
                catalog.pop(key, None)
        if not permitted("quality"):
            catalog.pop("quality", None)
        with TurnTrace(session.session_id, session.dataset_id, question, provider.model) as trace:
            messages = [{"role": "system", "content":
                "你是DuckDB只读SQL助手。输出符合JSON Schema的sql和explanation，不执行SQL，不编造数值。"
                "仅使用所选目录；analysis_result是完整的已审核计划结果，不是订单明细。"
                "raw是原表探索：根据用户明确要求选择状态/日期，未指定时经营指标按已送达和purchase_at；"
                "销售不含运费，支付先按订单聚合，不将支付分摊给品类或商家。"
                "存在无法明确的口径时sql设为空并在explanation询问，不猜测。"
                "不使用外部文件、网络、递归、表函数、写入；SQL用字面值且无参数占位符。"
                "所有输出列使用唯一别名。结果是自定义探索，不声称业务口径认证。"
                "允许函数：" + ",".join(sorted(EXPLORATION_FUNCTIONS)) +
                "\nJSON Schema:" + json.dumps(SQLDraft.model_json_schema(), ensure_ascii=False)},
                {"role": "user", "content": json.dumps({"catalog": catalog, "question": question}, ensure_ascii=False, default=str)}]
            if len(json.dumps(messages, ensure_ascii=False).encode()) > 48000:
                raise ValueError("SQL请求超过48000字节上下文预算，请缩小问题范围")
            trace.consume("model")
            draft = SQLDraft.model_validate_json(provider.complete(messages))
            if draft.sql.strip():
                compile_exploration(session, self.executors[session.dataset_id], draft.sql, [], scope)
            trace.record("sql_draft", scope=scope, **draft.model_dump())
            session.sql_state = {**draft.model_dump(), "parameters": [], "scope": scope, "question": question,
                                 "source_record_id": source_record_id}
            if self.store:
                self.store.save(session, self.executors[session.dataset_id].manifest)
            return draft

    def execute(self, session, sql: str, parameters: list, scope: Scope, *, offset=0, limit=MAX_ROWS) -> dict:
        executor = self.executors[session.dataset_id]
        with TurnTrace(session.session_id, session.dataset_id, sql, "user-sql") as trace:
            draft = session.sql_state
            if scope == "analysis_result" and draft.get("scope") == scope and draft.get("sql") == sql:
                if draft["source_record_id"] != session.analysis_evidence.get("record_id"):
                    raise ValueError("SQL草稿基于另一份分析结果，请重新生成草稿或编辑SQL后使用当前分析结果")
            compiled, catalog = compile_exploration(session, executor, sql, parameters, scope)
            trace.record("exploration_sql_approved", scope=scope, sql=compiled.sql, parameters=compiled.parameters,
                         business_audit="custom_exploration")
            trace.consume("query")
            started = time.perf_counter()
            result = run_query(executor.path, compiled, offset=offset, limit=limit)
            result.elapsed_ms = round((time.perf_counter() - started) * 1000)
            trace.record("query_finished", status=result.status, query_id=result.query_id, error=result.error)
            evidence = self._write(session, executor.manifest, sql, compiled, scope, catalog, result, trace)
            session.last_evidence = evidence
            session.sql_state = {"sql": sql, "parameters": parameters, "scope": scope,
                                 "explanation": "已执行的自定义探索SQL", "source_record_id": catalog.get("source_record_id")}
            session.remember(f"[自由 SQL · {scope}]\n{sql}", sql_answer(evidence))
            if self.store:
                self.store.save(session, executor.manifest)
            return evidence

    def verify(self, session, sql: str, parameters: list, scope: Scope) -> dict:
        """对照已声明计划核对当前快照的完整结果多重集合，不以预览冒充全量。"""
        from app.advanced import extended
        executor = self.executors[session.dataset_id]
        if session.last_successful_plan is None:
            raise ValueError("需先选择一份已审核分析作为结果核对基准")
        plan = session.last_successful_plan
        validate_plan(plan, executor.manifest)
        expected = compile_query(plan, executor.context)
        schema = query_schema(executor.context)
        validate_sql(expected.sql, schema=schema, exploration=extended(plan))
        audit_query(plan, expected, executor.context)
        actual, _ = compile_exploration(session, executor, sql, parameters, scope)
        expected_tree = qualify(sqlglot.parse_one(expected.sql, read="duckdb"), dialect="duckdb", schema=schema, quote_identifiers=False, identify=False)
        actual_tree = qualify(sqlglot.parse_one(actual.sql, read="duckdb"), dialect="duckdb", schema=schema, quote_identifiers=False, identify=False)
        if expected_tree.named_selects != actual_tree.named_selects:
            raise ValueError("核对要求输出列及顺序与参考分析一致")
        query = CompiledQuery(f"""WITH reference AS ({expected.sql}), candidate AS ({actual.sql}),
            differences AS ((SELECT * FROM reference EXCEPT ALL SELECT * FROM candidate)
                UNION ALL (SELECT * FROM candidate EXCEPT ALL SELECT * FROM reference))
            SELECT COUNT(*) AS differing_rows FROM differences""", expected.parameters + actual.parameters)
        with TurnTrace(session.session_id, session.dataset_id, "SQL完整结果核对", "sql-result-check") as trace:
            trace.consume("query")
            result = run_query(executor.path, query)
            if result.status != "ok":
                raise ValueError(result.error)
            from app.evidence import write_artifact
            return write_artifact(session, executor.manifest, "sql_validation", "自由SQL与已审核分析核对",
                {"reference_plan": plan.model_dump(mode="json"), "user_sql": sql,
                 "differing_rows": result.rows[0][0], "matched": result.rows[0][0] == 0,
                 "scope": "当前数据快照的全量结果等价；不是任意新数据的公式正确性证明"}, trace)

    @staticmethod
    def _write(session, manifest, sql, compiled, scope, catalog, result, trace):
        record = {"record_id": result.query_id, "type": "sql_exploration", "created_at": datetime.now(timezone.utc).isoformat(),
                  "session_id": session.session_id, "dataset_id": session.dataset_id, "scope": scope,
                  "business_audit": "custom_exploration", "source_record_id": catalog.get("source_record_id"),
                  "catalog": catalog, "user_sql": sql, "sql": compiled.sql, "parameters": compiled.parameters,
                  "result": result.model_dump(), "turn_id": trace.turn_id, "event_file": str(trace.event_file.relative_to(RUNS_DIR)),
                  "calls_used": trace.steps, "model": "user-sql", "planning_calls": 0,
                  **{key: manifest[key] for key in ("metadata_version", "metadata_sha256", "schema_sha256", "database_sha256")},
                  "source_hashes": manifest["sources"], "quality_rule_version": manifest.get("quality_rule_version")}
        content = json.dumps({"columns": result.columns, "rows": result.rows}, ensure_ascii=False, default=str, sort_keys=True)
        record["result_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        directory = RUNS_DIR / session.dataset_id
        directory.mkdir(parents=True, exist_ok=True)
        content = json.dumps(record, ensure_ascii=False, default=str, indent=2)
        (directory / f"{result.query_id}.json").write_text(content, encoding="utf-8")
        return json.loads(content)


def sql_answer(evidence: dict) -> str:
    result = evidence["result"]
    text = "SQL 查询结果。统计范围和计算方式以本次查询语句为准。"
    if result["status"] not in {"ok", "empty"}:
        return text + f"\n\n查询未完成：{result['error']}"
    text += f"\n\n本页共 {len(result['rows'])} 条" + ("（后面还有记录，可翻页或导出全部结果）" if result["truncated"] else "") + "，结果见表格。"
    if result["rows"]:
        from app.presentation import _number
        summaries = []
        for index, column in enumerate(result["columns"]):
            values = [row[index] for row in result["rows"] if row[index] is not None]
            if values and all(_number(value) for value in values):
                ordered = sorted(values, key=lambda value: float(value))
                summaries.append(f"{column}：当前页最小{ordered[0]}，最大{ordered[-1]}")
        if summaries:
            text += "\n\n当前页数值概览：" + "；".join(summaries[:6]) + "。不据此推断因果。"
    return text
