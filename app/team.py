"""Authenticated single-customer web entry. The local Gradio app is not mounted here."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import shutil
import secrets
import sqlite3
import time
from functools import lru_cache
from tempfile import TemporaryDirectory
from uuid import uuid4
from weakref import WeakValueDictionary

import duckdb
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from dotenv import dotenv_values
from filelock import FileLock, Timeout
from pydantic import BaseModel

from app.config import DATA_DIR
from app.answers import label
from app.data_upload import dataset_label, import_upload, inspect_mapping, registered_manifests
from app.delivery import is_delivery_request, refresh_delivery
from app.executor import Executor
from app.evidence import read_evidence
from app.inspection_sources import retained_order_records
from app.model_context import OUTBOUND
from app.models import AnalysisPlan
from app.orchestrator import AnalysisAgent
from app.light_import import contract
from app.presentation import display_evidence, example_questions
from app.providers import ChatCompletionsProvider, ModelConfigurationError, model_error_message
from app.session import Session
from app.session_store import SessionStore, archive_connection
from app.snapshots import digest, file_sha256
from app.sql_workspace import SQLWorkspace
from app.team_access import CURRENT_MEMBER, Member, TeamAccess, plan_requires_details
from app.team_jobs import Jobs, allowed, process_lock
from app.team_operations import Operations, observe


class Action(BaseModel):
    dataset: str
    session: str | None = None
    action: str = "ask"
    value: str = ""
    target: str = ""
    scope: str = "analysis_result"


def create_app(directory=DATA_DIR, provider_factory=ChatCompletionsProvider):
    directory.mkdir(parents=True, exist_ok=True)
    with process_lock(directory, "maintenance", timeout=5):
        return _create_app(directory, provider_factory)


def _create_app(directory, provider_factory):
    from app.team_page import PAGE
    access = TeamAccess(directory)
    operations = Operations(access)
    jobs = Jobs(access, operations)

    @asynccontextmanager
    async def lifespan(app):
        with process_lock(directory, "web"):
            yield

    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    basic = HTTPBasic(auto_error=False)
    settings = dotenv_values(directory.parent / ".env")
    proxy_secret = os.environ.get("TEAM_PROXY_SECRET") or settings.get("TEAM_PROXY_SECRET")
    store_for = lru_cache(maxsize=64)(lambda member: member.store(directory))
    concurrency = int(os.environ.get("TEAM_ANALYSIS_CONCURRENCY", settings.get("TEAM_ANALYSIS_CONCURRENCY", 3)))
    if concurrency < 1:
        raise ValueError("TEAM_ANALYSIS_CONCURRENCY 必须大于 0")
    web.state.jobs = jobs

    @lru_cache(maxsize=32)
    def checked_executor(dataset_id, stamps):
        return Executor(dataset_id, directory)

    def executor_for(dataset_id):
        stats = [(directory / (dataset_id + suffix)).stat() for suffix in (".duckdb", ".manifest.json")]
        stamps = tuple((st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) for st in stats)
        return checked_executor(dataset_id, stamps)

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

    web.state.visible = visible

    def resource_allowed(row, member, available):
        if not set(json.loads(row["datasets"])) <= available.keys() or not all(member.can(op) for op in json.loads(row["required"])):
            return False
        if row["kind"] == "report" and not member.can("details"):
            payload = json.loads(row["payload"])
            if "selected_order_id" not in payload:
                # Older shares did not store order-level scope. Use the original executed plan.
                path = directory.parent / "runs" / payload["dataset"] / (payload["record"] + ".json")
                if not path.is_file():
                    return False
                original = json.loads(path.read_text(encoding="utf-8"))
                return not plan_requires_details(AnalysisPlan.model_validate(original["plan"]))
        return True

    @web.middleware("http")
    async def request_policy(request: Request, call_next):
        started = time.monotonic()
        if request.method == "POST" and request.headers.get("X-Analysis-Action") != "1":
            return JSONResponse({"detail": "需要同源操作请求"}, status_code=403)
        response = await admitted(request, call_next, started) if request.url.path == "/api/action" and request.method == "POST" else await call_next(request)
        route = request.scope.get("route")
        if route:
            access.record_operation(route.path, response.status_code, round((time.monotonic()-started)*1000))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    # These objects belong to the web server loop. Test clients may use a separate loop per request.
    admissions = WeakValueDictionary()

    class Admission:
        def __init__(self):
            self.slots = asyncio.Semaphore(concurrency)
            self.sessions = WeakValueDictionary()

    async def admitted(request, call_next, started):
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            return await call_next(request)
        if not isinstance(body, dict):
            return await call_next(request)
        resource = body.get("session")
        valid = isinstance(resource, str) and len(resource) == 32 and all(c in "0123456789abcdef" for c in resource)
        resource = resource if valid else uuid4().hex
        loop = asyncio.get_running_loop()
        admission = admissions.get(loop)
        if admission is None:
            admissions[loop] = admission = Admission()
        lock = admission.sessions.get(resource)
        if lock is None:
            admission.sessions[resource] = lock = asyncio.Lock()
        events = []
        async with lock, admission.slots:
            with operations.collect(events, resource):
                observe("turn_wait", "ok", round((time.monotonic() - started) * 1000))
                executed = time.monotonic()
                response = await call_next(request)
                observe("turn", str(response.status_code), round((time.monotonic() - executed) * 1000))
        return response

    @web.get("/healthz")
    def health():
        with archive_connection(access.path) as db:
            db.execute("SELECT 1 FROM jobs LIMIT 1").fetchone()
        return {"service": "online"}

    @web.get("/api/operations")
    def operational_status(days: int = Query(7, ge=1, le=31), member=Depends(authenticate)):
        require(member, "admin")
        try:
            provider = provider_factory()
            configuration = {"status": "configured", "model": provider.model}
        except ModelConfigurationError:
            configuration = {"status": "invalid"}
        try:
            with process_lock(directory, "worker"):
                worker = "offline"
        except Timeout:
            worker = "online"
        latest = next(iter(visible(member).values()), None)
        delivery = {key: latest[key] for key in ("dataset_id", "imported_at")} if latest else None
        with archive_connection(access.path) as db:
            last = db.execute("""SELECT id,status,created FROM jobs WHERE kind='import'
                ORDER BY created DESC LIMIT 1""").fetchone()
        return {"service": "online", "worker": worker, "model": configuration,
                "model_last_result": operations.model_status(), "latest_delivery": delivery,
                "latest_import": dict(zip(("job", "status", "created"), last)) if last else None,
                "daily": operations.daily(days)}

    @web.post("/api/operations/probe")
    def probe_model(member=Depends(authenticate)):
        require(member, "admin")
        with operations.collect([], "model-probe"):
            started = time.monotonic()
            status = "error"
            try:
                provider = provider_factory()
                response = json.loads(provider.complete([{"role": "user", "content": 'Reply with JSON {"ok":true}.'}]))
                status = "ok" if response == {"ok": True} else "unexpected_response"
            except ModelConfigurationError:
                status = "invalid_configuration"
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                pass
            finally:
                observe("model_probe", status, round((time.monotonic() - started) * 1000))
        return {"status": status}

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
                file = directory.parent / Path(json.loads(row["payload"]))
                if file.is_file():
                    return FileResponse(file, filename=file.name)
        raise HTTPException(404, "下载不可访问")

    def stage_upload(files, staging):
        paths = []
        for file in files:
            filename = Path(file.filename or "").name
            if filename != file.filename or Path(filename).suffix.lower() not in {".csv", ".xlsx"}:
                raise ValueError("请上传 CSV 或 XLSX 文件")
            path = staging / filename
            if path in paths:
                raise ValueError("来源文件名重复")
            with path.open("wb") as out:
                shutil.copyfileobj(file.file, out)
            paths.append(path)
        return paths

    def import_result(paths, selected, name, parent, dataset_id=None):
        try:
            manifest = import_upload(paths, selected, name, directory, dataset_id=dataset_id, parent_dataset_id=parent or None)
        except (KeyError, duckdb.Error) as error:
            raise ValueError(f"请核对来源列、主键及类型：{error}") from error
        return upload_result(manifest)

    def upload_result(manifest):
        return {"dataset": manifest["dataset_id"], "rows": manifest["normalized_row_counts"],
                "capabilities": manifest.get("capabilities", "完整九表")}

    @web.post("/api/upload/{operation}")
    def upload(operation: str, files: list[UploadFile] = File(), template: str = Form("orders_items"),
               mapping: str = Form("{}"), name: str = Form("经营数据"), parent: str = Form(""), member=Depends(authenticate)):
        require(member, "admin")
        if operation == "inspect":
            with TemporaryDirectory(dir=directory) as staging:
                paths = stage_upload(files, Path(staging))
                structure, selected = inspect_mapping(paths, template)
                return {"mapping": selected, "samples": structure}
        if operation not in {"import", "queue"}:
            raise HTTPException(404)
        selected = json.loads(mapping)
        if not isinstance(selected, dict) or not isinstance(selected.get("tables"), dict):
            raise ValueError("请先识别文件并核对字段映射")
        if parent and parent not in visible(member):
            raise HTTPException(404, "父快照不可访问")
        if operation == "import":
            with TemporaryDirectory(dir=directory) as staging:
                return import_result(stage_upload(files, Path(staging)), selected, name, parent)
        key = uuid4().hex
        staging = jobs.directory / key
        staging.mkdir(parents=True)
        keep = False
        try:
            paths = stage_upload(files, staging)
            signature = digest([parent, selected, {path.name: file_sha256(path) for path in paths}])
            payload = {"mapping": selected, "name": name, "parent": parent, "snapshot": f"upload_{signature}",
                       "files": [path.name for path in paths], "staging": key}
            result = jobs.enqueue(member, "import", payload, datasets=[parent] if parent else [], rights=["admin"],
                                  identity=signature, key=key)
            keep = not result["reused"]
            return result
        finally:
            if not keep:
                shutil.rmtree(staging)

    def run_import(key, payload, member):
        dataset_id = payload["snapshot"]
        path = directory / (dataset_id + ".manifest.json")
        staging = jobs.directory / payload["staging"]
        if path.exists():
            result = upload_result(executor_for(dataset_id).manifest)
        else:
            # A worker can stop between building the database and publishing its manifest.
            for suffix in (".duckdb", ".duckdb.wal", ".manifest.tmp"):
                (directory / (dataset_id + suffix)).unlink(missing_ok=True)
            for folder in ("uploads", "sources"):
                shutil.rmtree(directory / folder / dataset_id, ignore_errors=True)
            result = import_result([staging / name for name in payload["files"]], payload["mapping"], payload["name"],
                                   payload["parent"], dataset_id)
        shutil.rmtree(staging, ignore_errors=True)
        return result, None

    def export_rights(record):
        rights = ["export"]
        if record.get("type") == "sql_exploration":
            rights.append("sql")
            base = record["catalog"].get("source_plan")
            if record["scope"] == "raw" or (base and plan_requires_details(AnalysisPlan.model_validate(base))):
                rights.append("details")
        elif (record.get("plan", {}).get("intent") not in {"aggregate", "rank", "compare", "contribution"}
              or plan_requires_details(AnalysisPlan.model_validate(record["plan"]))):
            rights.append("details")
        return rights

    def run_export(key, payload, member):
        dataset_id = payload["dataset"]
        executor = executor_for(dataset_id)
        record = read_evidence(executor.manifest, payload["session"], payload["record"])
        session = Session(dataset_id, session_id=payload["session"], last_evidence=record)
        with request_context(member, settings):
            result = AnalysisAgent(None, {dataset_id: executor}).export(session)
        return {"download": key}, (member, [dataset_id], export_rights(record), result["csv_file"])

    jobs.handlers.update({"import": run_import, "export": run_export})

    def job_payload(row):
        return {"job": row["id"], "kind": row["kind"], "status": row["status"], "created": row["created"],
                "started": row["started"], "finished": row["finished"], "cancelling": bool(row["cancelled"]),
                "wait_ms": row["wait_ms"], "progress": json.loads(row["progress"]),
                "result": json.loads(row["result"]) if row["result"] else None, "error": row["error"]}

    def member_job(db, key, member, available):
        row = jobs.read(db, key)
        if row is None or not allowed(row, member, available):
            raise HTTPException(404, "任务不可访问")
        return row

    @web.get("/api/jobs")
    def list_jobs(member=Depends(authenticate)):
        available = visible(member)
        with archive_connection(access.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 50", (member.name,))
            return {"jobs": [job_payload(row) for row in rows if allowed(row, member, available)]}

    @web.get("/api/jobs/{key}")
    def get_job(key: str, member=Depends(authenticate)):
        with archive_connection(access.path) as db:
            return job_payload(member_job(db, key, member, visible(member)))

    @web.post("/api/jobs/{key}/{operation}")
    def change_job(key: str, operation: str, member=Depends(authenticate)):
        available = visible(member)
        with archive_connection(access.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = member_job(db, key, member, available)
            if operation == "cancel" and row["status"] == "queued":
                db.execute("UPDATE jobs SET status='cancelled',cancelled=1,finished=? WHERE id=?", (time.time(), key))
            elif operation == "cancel" and row["status"] == "running" and row["kind"] == "weekly":
                db.execute("UPDATE jobs SET cancelled=1 WHERE id=?", (key,))
            elif operation == "retry" and row["status"] in {"failed", "cancelled"}:
                active = jobs.reusable(db, row["identity"])
                if active:
                    return {"job": active, "reused": True}
                db.execute("""UPDATE jobs SET status='queued',cancelled=0,started=NULL,finished=NULL,error=NULL,created=?,
                    wait_ms=NULL WHERE id=?""", (time.time(), key))
            else:
                raise HTTPException(409, "当前阶段不接受该操作")
            return job_payload(jobs.read(db, key))

    @web.post("/api/action")
    def action(req: Action, member: Member = Depends(authenticate)):
        require(member, "analyze")
        available = visible(member)
        if req.dataset not in available:
            raise HTTPException(404, "数据集不可访问")
        executor = executor_for(req.dataset)
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
            if req.action in {"ask", "resume"}:
                if req.action == "resume":
                    if not session.working_plan and session.view_focus.get("kind") != "inspection":
                        raise ValueError("请先打开包含未完成步骤的会话")
                    text = agent.resume_inspection(session).text
                elif is_delivery_request(req.value):
                    session, text = refresh_delivery(agent, session, req.value, allow_target=member.sees)
                else:
                    text = agent.respond(session, req.value, provider_factory=provider_factory).text
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
                agent.executors[req.target] = executor_for(req.target)
                # Comparison history may span separately revocable datasets.
                agent.store = None
                turn = agent.compare(session, {"target_dataset_id": req.target})
                response = {"answer": turn.text}
                if turn.evidence.get("bundle_file") and member.can("details") and member.can("export"):
                    response["download"] = access.save_resource("file", member, [req.dataset, req.target], ["details", "export"], turn.evidence["bundle_file"])
                return response
            if req.action in {"export", "queue_export"}:
                require(member, "export")
                record = display_evidence(session.last_evidence)
                rights = export_rights(record)
                for right in rights:
                    require(member, right)
                if req.action == "queue_export":
                    if record.get("type") not in {"query", "sql_exploration"}:
                        raise ValueError("请先执行查询再导出")
                    return jobs.enqueue(member, "export", {"dataset": req.dataset, "session": session_id,
                                        "record": record["record_id"]},
                                        datasets=[req.dataset], rights=rights)
                exported = agent.export(session)
                return {"download": access.save_resource("file", member, [req.dataset], rights, exported["csv_file"])}
            if req.action == "share":
                record = display_evidence(session.last_evidence)
                plan = record.get("plan", {})
                result = record.get("result") or {}
                if (record.get("type") != "query" or plan.get("intent") not in {"aggregate", "rank", "compare", "contribution"}
                        or result.get("status") not in {"ok", "empty"}):
                    raise ValueError("请先完成一个可分享的汇总查询")
                rights = ["details"] if plan_requires_details(AnalysisPlan.model_validate(plan)) else []
                recipient = access.member(req.target)
                if not recipient or not recipient.sees(executor.manifest) or not all(recipient.can(op) for op in rights):
                    raise ValueError("接收人需要有权查看该数据集，含订单级条件的汇总还需要明细权限")
                payload = {"title": req.value or "经营汇总", "dataset": req.dataset, "snapshot": record["database_sha256"],
                           "rule": record["quality_rule_version"], "record": record["record_id"],
                           "period": plan["time_range"], "comparison_period": plan.get("comparison_range"),
                           "time_field": plan["time_field"], "statuses": plan["order_statuses"],
                           "filters": plan["filters"], "order_where": plan["order_where"], "item_where": plan["item_where"],
                           "selected_order_id": plan.get("selected_order_id"), "output_mode": plan.get("output_mode"),
                           "dimension": plan["dimension"], "group_by": plan["group_by"], "columns": result["columns"], "rows": result["rows"],
                           "truncated": result["truncated"], "quality": result.get("quality")}
                return {"report": access.save_resource("report", member, [req.dataset], rights, payload, req.target)}
            raise ValueError("未知操作")
    from app.weekly_routes import install_weekly
    install_weekly(web, directory, access, authenticate, visible, require, store_for, executor_for, jobs)
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
