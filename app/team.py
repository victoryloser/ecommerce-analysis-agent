"""Authenticated single-customer web entry. The local Gradio app is not mounted here."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import secrets
import time
from functools import lru_cache
from tempfile import TemporaryDirectory

import duckdb
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from dotenv import dotenv_values
from filelock import FileLock
from pydantic import BaseModel

from app.config import DATA_DIR
from app.answers import label
from app.data_upload import dataset_label, import_upload, inspect_mapping, registered_manifests
from app.delivery import is_delivery_request, refresh_delivery
from app.executor import Executor
from app.evidence import read_evidence
from app.inspection_sources import retained_order_records
from app.model_context import OUTBOUND
from app.orchestrator import AnalysisAgent
from app.light_import import contract
from app.presentation import display_evidence, example_questions
from app.providers import ChatCompletionsProvider, model_error_message
from app.session import Session
from app.session_store import SessionStore
from app.sql_workspace import SQLWorkspace
from app.team_access import CURRENT_MEMBER, Member, TeamAccess


class Action(BaseModel):
    dataset: str
    session: str | None = None
    action: str = "ask"
    value: str = ""
    target: str = ""
    scope: str = "analysis_result"


def create_app(directory=DATA_DIR, provider_factory=ChatCompletionsProvider):
    from app.team_page import PAGE
    access = TeamAccess(directory)
    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(auto_error=False)
    settings = dotenv_values(directory.parent / ".env")
    proxy_secret = os.environ.get("TEAM_PROXY_SECRET") or settings.get("TEAM_PROXY_SECRET")
    store_for = lru_cache(maxsize=64)(lambda member: member.store(directory))

    def authenticate(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)):
        member = None
        if proxy_secret:
            if secrets.compare_digest(request.headers.get("X-Analysis-Proxy", "").encode(), proxy_secret.encode()):
                member = access.member(request.headers.get("X-Analysis-User", ""))
        elif credentials:
            member = access.authenticate(credentials.username, credentials.password)
        if not member:
            raise HTTPException(401, "登录失败", headers={} if proxy_secret else {"WWW-Authenticate": "Basic"})
        return member

    def require(member, operation):
        if not member.can(operation):
            raise HTTPException(403, "当前账号没有此操作权限")

    def visible(member):
        return {m["dataset_id"]: m for m in registered_manifests(directory) if member.sees(m)}

    def resource_allowed(row, member, available):
        return set(json.loads(row["datasets"])) <= available.keys() and all(member.can(op) for op in json.loads(row["required"]))

    @web.middleware("http")
    async def request_policy(request: Request, call_next):
        started = time.monotonic()
        if request.method == "POST" and request.headers.get("X-Analysis-Action") != "1":
            return JSONResponse({"detail": "需要同源操作请求"}, status_code=403)
        response = await call_next(request)
        route = request.scope.get("route")
        if route:
            access.record_operation(route.path, response.status_code, round((time.monotonic()-started)*1000))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @web.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @web.exception_handler(httpx.HTTPError)
    async def model_failure(request, error):
        return JSONResponse({"detail": model_error_message(error)}, status_code=503)

    @web.exception_handler(PermissionError)
    async def forbidden(request, error):
        return JSONResponse({"detail": str(error)}, status_code=403)

    @web.get("/", response_class=HTMLResponse)
    def home(member=Depends(authenticate)):
        return PAGE

    @web.get("/api/home")
    def home_data(member=Depends(authenticate)):
        available = visible(member)
        reports = [{"id": row["id"], "title": json.loads(row["payload"])["title"]} for row in access.resources(member, "report")
                   if resource_allowed(row, member, available)]
        examples = {k: example_questions(m) for k, m in available.items()} if member.can("analyze") else {}
        return {"name": member.name, "role": member.role, "datasets": {k: dataset_label(m) for k, m in available.items()},
                "reports": reports, "examples": examples,
                "permissions": [op for op in ("analyze", "details", "sql", "export") if member.can(op)]}

    @web.post("/api/mapping/preview")
    def preview(mapping: dict, member=Depends(authenticate)):
        require(member, "admin")
        try:
            caps = contract(mapping)
        except (KeyError, TypeError) as error:
            raise ValueError("请先识别订单和明细表，再核对字段映射") from error
        return {"可用指标": [label(m) for m in caps["metrics"]], "可问问题": example_questions({"capabilities": caps}),
                "补充来源后可用": list(dict.fromkeys(caps["missing_inputs"].values()))}

    @web.get("/api/reports/{key}")
    def report(key: str, member=Depends(authenticate)):
        available = visible(member)
        for row in access.resources(member, "report"):
            if row["id"] == key and resource_allowed(row, member, available):
                return json.loads(row["payload"])
        raise HTTPException(404, "报告不可访问")

    @web.get("/api/files/{key}")
    def download(key: str, member=Depends(authenticate)):
        available = visible(member)
        for row in access.resources(member, "file"):
            if row["id"] == key and resource_allowed(row, member, available):
                file = Path(json.loads(row["payload"]))
                if file.is_file():
                    return FileResponse(file, filename=file.name)
        raise HTTPException(404, "下载不可访问")

    @web.post("/api/upload/{operation}")
    def upload(operation: str, files: list[UploadFile] = File(), template: str = Form("orders_items"),
               mapping: str = Form("{}"), name: str = Form("经营数据"), parent: str = Form(""), member=Depends(authenticate)):
        require(member, "admin")
        with TemporaryDirectory(dir=directory) as staging:
            paths = []
            for file in files:
                filename = Path(file.filename or "").name
                if filename != file.filename or Path(filename).suffix.lower() not in {".csv", ".xlsx"}:
                    raise ValueError("请上传 CSV 或 XLSX 文件")
                path = Path(staging) / filename
                if path in paths:
                    raise ValueError("来源文件名重复")
                with path.open("wb") as out:
                    shutil.copyfileobj(file.file, out)
                paths.append(path)
            if operation == "inspect":
                structure, selected = inspect_mapping(paths, template)
                return {"mapping": selected, "samples": structure}
            if operation != "import":
                raise HTTPException(404)
            try:
                selected = json.loads(mapping)
                if not isinstance(selected, dict) or not isinstance(selected.get("tables"), dict):
                    raise ValueError("请先识别文件并核对字段映射")
                if parent and parent not in visible(member):
                    raise HTTPException(404, "父快照不可访问")
                manifest = import_upload(paths, selected, name, directory, parent_dataset_id=parent or None)
            except (KeyError, duckdb.Error) as error:
                raise ValueError(f"请核对来源列、主键及类型：{error}") from error
            return {"dataset": manifest["dataset_id"], "rows": manifest["normalized_row_counts"],
                    "capabilities": manifest.get("capabilities", "完整九表")}

    @web.post("/api/action")
    def action(req: Action, member: Member = Depends(authenticate)):
        require(member, "analyze")
        available = visible(member)
        if req.dataset not in available:
            raise HTTPException(404, "数据集不可访问")
        executor = Executor(req.dataset, directory)
        store = store_for(member)
        if req.action == "sessions":
            return {"sessions": store.list(req.dataset)}
        if req.action == "search":
            return {"hits": store.search(req.value, executor.manifest)}
        if req.action == "claim":
            require(member, "admin")
            legacy = SessionStore(directory / "sessions.sqlite3").load(req.value, executor.manifest)
            store.save(legacy, executor.manifest)
            return {"session": legacy.session_id}
        if req.session and (len(req.session) != 32 or not all(c in "0123456789abcdef" for c in req.session)):
            raise ValueError("无效的会话编号")
        session_id = req.session or Session(req.dataset).session_id
        with FileLock(directory / ("session-" + session_id + ".lock")), request_context(member, settings):
            session = store.load(session_id, executor.manifest) if req.session else Session(req.dataset, session_id=session_id)
            agent = AnalysisAgent(None, {req.dataset: executor}, store)
            if req.action == "open":
                record = display_evidence(session.last_evidence)
                return {"session": session_id, "messages": session.transcript, "result": record.get("result")}
            if req.action == "proof":
                if req.value.strip():
                    record = store.turn_evidence(session_id, int(req.value), executor.manifest)
                else:
                    current = display_evidence(session.last_evidence)
                    record = read_evidence(executor.manifest, session_id, current["record_id"]) if current.get("record_id") else None
                if not record:
                    raise ValueError("本回合没有可查看的计算依据")
                record = display_evidence(record)
                proof = {k: record[k] for k in ("record_id", "dataset_id", "database_sha256", "metadata_sha256",
                         "quality_rule_version", "model", "plan", "result", "created_at") if k in record}
                if member.can("sql"):
                    proof.update(sql=record.get("sql"), parameters=record.get("parameters"))
                return proof
            if req.action == "ask":
                if is_delivery_request(req.value):
                    session, text = refresh_delivery(agent, session, req.value, allow_target=member.sees)
                else:
                    agent.provider = provider_factory()
                    text = agent.respond(session, req.value).text
                evidence = display_evidence(session.last_evidence)
                return {"session": session.session_id, "dataset": session.dataset_id, "answer": text,
                        "result": evidence.get("result"), "scope": evidence.get("plan")}
            if req.action in {"sql", "draft"}:
                require(member, "sql")
                if req.scope == "raw":
                    require(member, "details")
                workspace = SQLWorkspace(agent.executors, store)
                if req.action == "draft":
                    result = workspace.draft(session, req.value, req.scope, provider_factory()).model_dump()
                else:
                    result = workspace.execute(session, req.value, [], req.scope)["result"]
                return {"session": session_id, "result": result}
            if req.action == "order":
                require(member, "details")
                return retained_order_records(executor, req.value)
            if req.action == "compare":
                if req.target not in available:
                    raise HTTPException(404, "目标快照不可访问")
                agent.executors[req.target] = Executor(req.target, directory)
                # Comparison history may span separately revocable datasets.
                agent.store = None
                turn = agent.compare(session, {"target_dataset_id": req.target})
                response = {"answer": turn.text}
                if turn.evidence.get("bundle_file") and member.can("details") and member.can("export"):
                    response["download"] = access.save_resource("file", member, [req.dataset, req.target], ["details", "export"], turn.evidence["bundle_file"])
                return response
            if req.action == "export":
                require(member, "export")
                record = display_evidence(session.last_evidence)
                rights = ["export"]
                if record.get("type") == "sql_exploration":
                    rights.append("sql")
                    if record.get("scope") == "raw":
                        rights.append("details")
                elif record.get("plan", {}).get("intent") not in {"aggregate", "rank", "compare", "contribution"}:
                    rights.append("details")
                for right in rights:
                    require(member, right)
                exported = agent.export(session)
                return {"download": access.save_resource("file", member, [req.dataset], rights, exported["csv_file"])}
            if req.action == "share":
                record = display_evidence(session.last_evidence)
                plan = record.get("plan", {})
                result = record.get("result") or {}
                if (record.get("type") != "query" or plan.get("intent") not in {"aggregate", "rank", "compare", "contribution"}
                        or result.get("status") not in {"ok", "empty"}):
                    raise ValueError("请先完成一个可分享的汇总查询")
                if not access.has_recipient(req.target, executor.manifest):
                    raise ValueError("请指定本实例中获准查看该数据集的接收人")
                payload = {"title": req.value or "经营汇总", "dataset": req.dataset, "snapshot": record["database_sha256"],
                           "rule": record["quality_rule_version"], "record": record["record_id"],
                           "period": plan["time_range"], "comparison_period": plan.get("comparison_range"),
                           "time_field": plan["time_field"], "statuses": plan["order_statuses"],
                           "filters": plan["filters"], "order_where": plan["order_where"], "item_where": plan["item_where"],
                           "dimension": plan["dimension"], "group_by": plan["group_by"], "columns": result["columns"], "rows": result["rows"],
                           "truncated": result["truncated"], "quality": result.get("quality")}
                return {"report": access.save_resource("report", member, [req.dataset], [], payload, req.target)}
            raise ValueError("未知操作")
    return web


@contextmanager
def request_context(member, settings):
    raw = os.environ.get("TEAM_MODEL_FIELDS", settings.get("TEAM_MODEL_FIELDS", "question,entities,scope"))
    fields = frozenset(v.strip() for v in raw.split(","))
    token = OUTBOUND.set(fields)
    identity = CURRENT_MEMBER.set(member)
    try:
        yield
    finally:
        OUTBOUND.reset(token)
        CURRENT_MEMBER.reset(identity)
