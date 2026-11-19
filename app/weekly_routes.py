"""Weekly reports, reconciliation and follow-up in an authenticated customer instance."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
import time
from typing import Literal
from uuid import uuid4

from fastapi import Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from filelock import FileLock

from app.team_operations import observe
from app.evidence import read_evidence
from app.models import AnalysisPlan, TimeRange
from app.presentation import display_evidence
from app.refunds import ledger_for, parse_ledger, reconcile
from app.semantic import validate_plan
from app.session_store import archive_connection
from app.snapshots import digest, implementation_hash
from app.weekly import business_zone, calculate, compare_reports, dump, last_week, source_id
from app import weekly_issues as issues

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_coverage(dataset TEXT, field TEXT, start TEXT, end TEXT, confirmed_by TEXT,
 PRIMARY KEY(dataset,field,start,end));
CREATE TABLE IF NOT EXISTS weekly_templates(id TEXT PRIMARY KEY, family TEXT, version INTEGER, owner TEXT, dataset TEXT,
 feed TEXT, title TEXT, settings TEXT, UNIQUE(family,version));
CREATE TABLE IF NOT EXISTS weekly_reports(id TEXT PRIMARY KEY, template TEXT, family TEXT, dataset TEXT, week TEXT,
 owner TEXT, published TEXT, recipient TEXT, required TEXT, payload TEXT, UNIQUE(template,dataset,week));
CREATE TABLE IF NOT EXISTS refund_ledgers(dataset TEXT PRIMARY KEY, start TEXT, end TEXT, timezone TEXT,
 sha256 TEXT, filename TEXT, signer TEXT, entries TEXT);
"""
TimeField = Literal["purchase_at", "approved_at", "shipped_at", "delivered_at", "estimated_delivery_at"]


class Coverage(TimeRange):
    dataset: str
    field: TimeField = "purchase_at"


class Template(BaseModel):
    dataset: str
    session: str
    title: str = Field(min_length=1, max_length=120)
    timezone: str = "Asia/Shanghai"
    first_day: int = Field(default=0, ge=0, le=6)
    compare_weeks: Literal[1, 4] = 1
    category: bool = False
    net_refunds: bool = False
    metrics: list[Literal["sales", "order_count", "freight"]] | None = None
    statuses: list[str] | None = None
    prior: str | None = None


class Run(BaseModel):
    dataset: str
    template: str
    as_of: date | None = None
    revise: str | None = None


class Batch(BaseModel):
    weeks: list[Run] = Field(min_length=1, max_length=12)


class Ticket(BaseModel):
    report: str
    order: str
    item: str
    assignee: str


class FollowUp(BaseModel):
    action: Literal["correct", "close", "comment"]
    note: str
    batch: str = ""


class RefundPeriod(TimeRange):
    dataset: str
    finance_total: Decimal | None = None


def install_weekly(web, directory, access, authenticate, visible, require, store_for, executor_for, jobs):
    @contextmanager
    def database(*, write=False):
        with archive_connection(access.path) as db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    with database() as db:
        db.executescript(SCHEMA + issues.SCHEMA)

    def lookup(db, table, key):
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "记录不可访问")
        return row

    def dataset(member, key):
        manifest = visible(member).get(key)
        if not manifest:
            raise HTTPException(404, "快照不可访问")
        return manifest

    def accessible(row, member, available):
        if not set(json.loads(row["required"])) <= available.keys():
            return False
        return member.can("analyze") or bool(row["published"] and row["recipient"] == member.name)

    def report_row(db, key, member):
        row = lookup(db, "weekly_reports", key)
        if not accessible(row, member, visible(member)):
            raise HTTPException(404, "周报不可访问")
        return row

    def ticket_allowed(db, key, member, available):
        reports = db.execute(
            """SELECT DISTINCT r.required FROM weekly_reports r JOIN verification_events e
                                ON e.report=r.id WHERE e.ticket=?""",
            (key,),
        )
        return member.can("details") and all(set(json.loads(r["required"])) <= available.keys() for r in reports)

    def covered(db, dataset_id, field, start, end):
        cursor = start.isoformat()
        confirmations = []
        for row in db.execute(
            "SELECT * FROM weekly_coverage WHERE dataset=? AND field=? AND end>? AND start<? ORDER BY start",
            (dataset_id, field, cursor, end.isoformat()),
        ):
            if row["start"] > cursor:
                break
            cursor = max(cursor, row["end"])
            confirmations.append(dict(row))
            if cursor >= end.isoformat():
                return confirmations
        raise ValueError(f"数据负责人尚未确认 {field} 在 [{start}, {end}) 的完整交付")

    def revision_candidates(db, manifest, field, *, refunds_only=False):
        candidates = db.execute(
            """SELECT r.*,t.feed FROM weekly_reports r JOIN weekly_templates t ON t.id=r.template
            WHERE t.feed=? AND r.dataset<>? AND r.published IS NOT NULL ORDER BY r.published DESC,r.rowid DESC""",
            (source_id(manifest), manifest["dataset_id"]),
        ).fetchall()
        requests, seen = [], set()
        for row in candidates:
            identity = row["family"], row["week"]
            if identity in seen:
                continue
            seen.add(identity)
            original = json.loads(row["payload"])
            if original["imported_at"] >= manifest["imported_at"] or original["plan"]["time_field"] != field:
                continue
            if refunds_only and "refunds" not in original:
                continue
            requests.append(Run(dataset=manifest["dataset_id"], template=row["template"], revise=row["id"]))
        return requests

    def revise_complete(manifest, field, member, *, refunds_only=False):
        with database() as db:
            candidates = revision_candidates(db, manifest, field, refunds_only=refunds_only)
        revisions, pending = [], []
        for candidate in candidates:
            try:
                revisions.append(run(candidate, member))
            except ValueError as error:
                pending.append({"report": candidate.revise, "reason": str(error)})
        return {"revisions": revisions, "pending": pending}

    def enqueue_revisions(db, manifest, field, member, identifier, *, refunds_only=False):
        candidates = revision_candidates(db, manifest, field, refunds_only=refunds_only)
        if not candidates:
            return {"revisions": [], "pending": []}
        return jobs.enqueue(member, "weekly", {"revision": True, "weeks": [r.model_dump(mode="json") for r in candidates]},
                            datasets=[manifest["dataset_id"]], rights=["admin"],
                            identity=[identifier, [r.revise for r in candidates]], db=db)

    @web.post("/api/weekly/coverage")
    def confirm(req: Coverage, background: bool = False, member=Depends(authenticate)):
        require(member, "admin")
        manifest = dataset(member, req.dataset)
        with database(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO weekly_coverage VALUES(?,?,?,?,?)",
                (req.dataset, req.field, str(req.start), str(req.end), member.name),
            )
            if background:
                return enqueue_revisions(db, manifest, req.field, member, req.model_dump(mode="json"))
        return {"已确认交付期间": req.model_dump(), **revise_complete(manifest, req.field, member)}

    @web.post("/api/weekly/templates")
    def save_template(req: Template, member=Depends(authenticate)):
        require(member, "analyze")
        manifest = dataset(member, req.dataset)
        session = store_for(member).load(req.session, manifest)
        record = display_evidence(session.last_evidence)
        if record.get("type") != "query" or record["result"]["status"] not in {"ok", "empty"}:
            raise ValueError("请先核验一次全店或选定对象的总体汇总")
        record = read_evidence(manifest, session.session_id, record["record_id"])
        plan = AnalysisPlan.model_validate(record["plan"])

        def order_key(node):
            return node and (node.field == "order_id" or any(order_key(child) for child in node.children))

        if (
            plan.intent not in {"aggregate", "compare"}
            or plan.group_by
            or plan.dimension != "total"
            or plan.selected_order_id
            or order_key(plan.order_where)
        ):
            raise ValueError("周报模板需从总体汇总或两期总体比较保存")
        business_zone(req.timezone)
        metrics = plan.metric_ids if req.metrics is None else req.metrics
        if not metrics or set(metrics) - {"order_count", "sales", "freight"}:
            raise ValueError("周报指标可选订单数、商品额及已提供的运费")
        plan = AnalysisPlan.model_validate(
            {
                **plan.model_dump(),
                "metric_ids": metrics,
                "order_statuses": req.statuses if req.statuses is not None else plan.order_statuses,
            }
        )
        validate_plan(plan, manifest)
        if req.category and (
            "sales" not in metrics
            or "category" not in manifest.get("capabilities", {}).get("dimensions", ["category"])
        ):
            raise ValueError("品类贡献需要商品额和品类来源")
        if req.net_refunds and (
            "sales" not in metrics
            or plan.time_field != "purchase_at"
            or plan.order_statuses != ["delivered"]
            or plan.filters
            or plan.order_where
            or plan.item_where
        ):
            raise ValueError("净商品额需要全店已送达订单的下单日期及商品额；当前退款无行级分摊")
        key = uuid4().hex
        family, version = key, 1
        settings = dict(
            plan=plan.model_dump(mode="json"),
            timezone=req.timezone,
            first_day=req.first_day,
            category=req.category,
            net_refunds=req.net_refunds,
            compare_weeks=req.compare_weeks,
            source_record=record["record_id"],
        )
        with database(write=True) as db:
            if req.prior:
                prior = lookup(db, "weekly_templates", req.prior)
                dataset(member, prior["dataset"])
                if prior["owner"] != member.name and not member.can("admin"):
                    raise HTTPException(403, "仅模板负责人可修改口径")
                if prior["feed"] != source_id(manifest):
                    raise ValueError("模板修订须属于同一数据来源")
                family = prior["family"]
                version = (
                    1
                    + db.execute("SELECT MAX(version) FROM weekly_templates WHERE family=?", (family,)).fetchone()[
                        0
                    ]
                )
            db.execute(
                "INSERT INTO weekly_templates VALUES(?,?,?,?,?,?,?,?)",
                (key, family, version, member.name, req.dataset, source_id(manifest), req.title, dump(settings)),
            )
        return {"template": key, "version": version}

    @web.get("/api/weekly")
    def index(member=Depends(authenticate)):
        available = visible(member)
        with database() as db:
            templates = [
                dict(r)
                for r in db.execute("SELECT id,title,version,dataset FROM weekly_templates ORDER BY rowid DESC")
                if member.can("analyze") and r["dataset"] in available
            ]
            reports = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "week": r["week"],
                    "published": r["published"],
                    "dataset": r["dataset"],
                }
                for r in db.execute("""SELECT r.id,r.week,r.published,r.recipient,r.required,r.dataset,t.title
                           FROM weekly_reports r JOIN weekly_templates t ON r.template=t.id ORDER BY r.rowid DESC""")
                if accessible(r, member, available)
            ]
            tickets = (
                [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,order_id,item_key,state,assignee FROM verification_tickets ORDER BY rowid DESC"
                    )
                    if ticket_allowed(db, r["id"], member, available)
                ]
                if member.can("details")
                else []
            )
        return {"templates": templates, "reports": reports, "tickets": tickets}

    def run_inputs(db, req, member):
        manifest = dataset(member, req.dataset)
        template = lookup(db, "weekly_templates", req.template)
        dataset(member, template["dataset"])
        if template["feed"] != source_id(manifest):
            raise ValueError("所选快照与模板数据源不同")
        settings = json.loads(template["settings"])
        week = last_week(req.as_of, settings["timezone"], settings["first_day"])
        parent = None
        if req.revise:
            parent = report_row(db, req.revise, member)
            if not parent["published"] or parent["family"] != template["family"]:
                raise ValueError("请选择同一模板系列的已发布周报")
            start = date.fromisoformat(parent["week"])
            week = TimeRange(start=start, end=start + timedelta(days=7))
        return manifest, template, settings, week, parent

    @web.post("/api/weekly/jobs")
    def queue_weeks(batch: Batch, member=Depends(authenticate)):
        require(member, "analyze")
        frozen, datasets = [], set()
        with database() as db:
            for req in batch.weeks:
                _, template, _, week, parent = run_inputs(db, req, member)
                datasets.update([req.dataset, template["dataset"]])
                if parent:
                    datasets.update(json.loads(parent["required"]))
                item = req.model_copy(update={"as_of": week.end}).model_dump(mode="json")
                if item not in frozen:
                    frozen.append(item)
        return jobs.enqueue(member, "weekly", {"weeks": frozen}, datasets=datasets, rights=["analyze"])

    def run_weeks(key, payload, member):
        existing = jobs.checkpoint(key)
        for item in payload["weeks"][len(existing):]:
            member = jobs.member_for_step(key, visible)
            req = Run.model_validate(item)
            try:
                result = run(req, member)
            except ValueError as error:
                if not payload.get("revision"):
                    raise
                result = {"pending": req.revise, "reason": str(error)}
            existing = jobs.checkpoint(key, result)
        return {"weeks": existing}, None

    jobs.handlers["weekly"] = run_weeks

    def link_revision(result, parent, required):
        before = json.loads(parent["payload"])
        if result["imported_at"] < before["imported_at"]:
            raise ValueError("修订快照早于已发布版本")
        required.update(json.loads(parent["required"]))
        result["parent"] = parent["id"]
        result["revision"] = compare_reports(before, json.loads(dump(result)), executor_for)

    @web.post("/api/weekly/run")
    def run(req: Run, member=Depends(authenticate)):
        require(member, "analyze")
        with database() as db:
            manifest, template, settings, week, parent = run_inputs(db, req, member)
        key = digest([req.template, req.dataset, str(week.start)])
        with FileLock(directory / ("weekly-" + key + ".lock")), database() as db:
            cached = db.execute("SELECT * FROM weekly_reports WHERE id=?", (key,)).fetchone()
            if cached and not accessible(cached, member, visible(member)):
                raise HTTPException(404, "周报不可访问")
            result = json.loads(cached["payload"]) if cached else None
            if parent is None and (not cached or (not cached["published"] and not result["parent"])):
                prior = db.execute(
                    "SELECT id FROM weekly_reports WHERE family=? AND week=? AND published IS NOT NULL ORDER BY published DESC,rowid DESC LIMIT 1",
                    (template["family"], str(week.start)),
                ).fetchone()
                if prior:
                    parent = report_row(db, prior["id"], member)
            if cached:
                if parent and parent["id"] not in (key, result["parent"]):
                    if cached["published"] or result["parent"]:
                        raise ValueError("该周报已发布或已关联其他原报告，请核对修订依据")
                    required = set(json.loads(cached["required"]))
                    link_revision(result, parent, required)
                    updated = db.execute(
                        "UPDATE weekly_reports SET payload=?,required=?,owner=? WHERE id=? AND published IS NULL",
                        (dump(result), dump(sorted(required)), parent["owner"], key),
                    )
                    if not updated.rowcount:
                        raise ValueError("该周报已发布，请核对修订依据")
                return {"report": key, "reused": True}
            field = settings["plan"]["time_field"]
            offset = timedelta(weeks=settings["compare_weeks"])
            confirmations = covered(db, req.dataset, field, week.start - offset, week.end - offset) + covered(
                db, req.dataset, field, week.start, week.end
            )
            executor = executor_for(req.dataset)
            started = time.monotonic()
            result = calculate(executor, settings, week)
            if settings["net_refunds"]:
                result["refunds"] = {}
                for role, period in (
                    ("current", week),
                    ("previous", TimeRange(start=week.start - offset, end=week.end - offset)),
                ):
                    ledger = ledger_for(db, req.dataset, period)
                    if ledger["timezone"] != settings["timezone"]:
                        raise ValueError("周报业务时区与退款入账时区不一致")
                    value = reconcile(executor, json.loads(ledger["entries"]), period)
                    result["refunds"][role] = {**value, "ledger_sha256": ledger["sha256"]}
            result.update(
                title=template["title"],
                dataset=req.dataset,
                template=req.template,
                version=template["version"],
                generated_at=datetime.now(timezone.utc).isoformat(),
                category=settings["category"],
                net_refunds=settings["net_refunds"],
                feed=template["feed"],
                imported_at=manifest["imported_at"],
                implementation=implementation_hash(),
                timezone=settings["timezone"],
                first_day=settings["first_day"],
                compare_weeks=settings["compare_weeks"],
                source_record=settings["source_record"],
                coverage=confirmations,
                parent=None,
            )
            result["definition"] = digest(
                [
                    settings["category"],
                    settings["net_refunds"],
                    settings["first_day"],
                    settings["timezone"],
                    settings["compare_weeks"],
                    manifest["metadata_sha256"],
                    result["mapping"],
                ]
            )
            scope_fields = (
                "time_range",
                "comparison_range",
                "time_field",
                "order_statuses",
                "filters",
                "order_where",
                "item_where",
                "metric_ids",
            )
            result["scope"] = digest(
                [result["feed"], result["definition"], {k: result["plan"][k] for k in scope_fields}]
            )
            required = {template["dataset"], req.dataset}
            if parent:
                link_revision(result, parent, required)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO weekly_reports VALUES(?,?,?,?,?,?,NULL,NULL,?,?)",
                (
                    key,
                    req.template,
                    template["family"],
                    req.dataset,
                    str(week.start),
                    parent["owner"] if parent else member.name,
                    dump(sorted(required)),
                    dump(result),
                ),
            )
            for ticket in db.execute(
                "SELECT * FROM verification_tickets WHERE scope=?", (result["scope"],)
            ).fetchall():
                issues.observe(db, ticket, key, result, member.name)
        observe("weekly_calculation", "ok", round((time.monotonic() - started) * 1000), 0)
        return {"report": key, "reused": False}

    @web.get("/api/weekly/reports/{key}")
    def read_report(key: str, member=Depends(authenticate)):
        with database() as db:
            row = report_row(db, key, member)
        result = json.loads(row["payload"])
        orders = result.pop("order_evidence")
        for value in result.get("refunds", {}).values():
            if not member.can("details"):
                value.pop("exceptions")
                value.pop("postings")
            if not member.can("sql"):
                value.pop("statements")
        if not member.can("sql"):
            result.pop("statements")
        if "revision" in result and "changed_orders" in result["revision"]:
            changed = result["revision"].pop("changed_orders")
            result["revision"]["affected_orders"] = len({v["order"] for v in changed})
            if member.can("details"):
                result["revision"]["changed_orders"] = changed
            if "changed_refunds" in result["revision"]:
                refunds = result["revision"].pop("changed_refunds")
                result["revision"]["affected_refunds"] = len({r["refund"] for r in refunds})
                if member.can("details"):
                    result["revision"]["changed_refunds"] = refunds
        conflicts = [
            {"period": p, "order": oid, "item": key, **e}
            for p, members in orders.items()
            for oid, o in members.items()
            for key, e in o["conflicts"].items()
        ]
        result["price_conflict_count"] = len(conflicts)
        if member.can("details"):
            result["conflicts"] = conflicts
        return {
            **result,
            "report": key,
            "published": row["published"],
            "owner": row["owner"],
            "recipient": row["recipient"],
        }

    @web.post("/api/weekly/reports/{key}/publish")
    def publish(key: str, recipient: str, member=Depends(authenticate)):
        require(member, "analyze")
        with database(write=True) as db:
            row = report_row(db, key, member)
            if member.name != row["owner"] and not member.can("admin"):
                raise HTTPException(403, "请由本次复盘负责人发布")
            if row["published"] and row["recipient"] != recipient:
                raise ValueError("周报已发布给指定接收人")
            reader = access.member(recipient)
            if not reader or not set(json.loads(row["required"])) <= visible(reader).keys():
                raise ValueError("接收人需要报告及修订依据的快照权限")
            db.execute(
                "UPDATE weekly_reports SET published=COALESCE(published,strftime('%Y-%m-%dT%H:%M:%fZ','now')),recipient=? WHERE id=?",
                (recipient, key),
            )
        return {"report": key, "url": "/?weekly=" + key}

    @web.post("/api/weekly/tickets")
    def open_ticket(req: Ticket, member=Depends(authenticate)):
        require(member, "details")
        with database(write=True) as db:
            row = report_row(db, req.report, member)
            owner = access.member(req.assignee)
            if not owner or not owner.can("details") or not accessible(row, owner, visible(owner)):
                raise ValueError("负责人需要该报告的明细权限")
            payload = json.loads(row["payload"])
            key = issues.create_ticket(db, req.report, payload, req.order, req.item, member.name, req.assignee)
            if not ticket_allowed(db, key, member, visible(member)):
                raise HTTPException(404, "核实单不可访问")
            ticket = lookup(db, "verification_tickets", key)
            newer = db.execute(
                "SELECT * FROM weekly_reports WHERE family=? AND week=? ORDER BY rowid",
                (row["family"], row["week"]),
            )
            for candidate in newer:
                subsequent = json.loads(candidate["payload"])
                if accessible(candidate, member, visible(member)):
                    issues.observe(db, ticket, candidate["id"], subsequent, member.name)
                    ticket = lookup(db, "verification_tickets", key)
        return {"ticket": key}

    @web.get("/api/weekly/tickets/{key}")
    def read_ticket(key: str, member=Depends(authenticate)):
        require(member, "details")
        with database() as db:
            ticket = lookup(db, "verification_tickets", key)
            if not ticket_allowed(db, key, member, visible(member)):
                raise HTTPException(404, "核实单不可访问")
            events = [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in db.execute(
                    "SELECT at,actor,event,payload,report,dataset FROM verification_events WHERE ticket=? ORDER BY seq",
                    (key,),
                )
            ]
        return {**dict(ticket), "events": events}

    @web.post("/api/weekly/tickets/{key}")
    def update_ticket(key: str, req: FollowUp, member=Depends(authenticate)):
        require(member, "details")
        with database(write=True) as db:
            ticket = lookup(db, "verification_tickets", key)
            if not ticket_allowed(db, key, member, visible(member)):
                raise HTTPException(404, "核实单不可访问")
            if req.action != "comment" and member.name != ticket["assignee"] and not member.can("admin"):
                raise HTTPException(403, "此操作需要负责人确认")
            issues.follow_up(db, ticket, req.action, member.name, req.note, req.batch)
        return {"ticket": key}

    @web.post("/api/weekly/refunds")
    def import_refunds(
        dataset_id: str = Form(),
        start: date = Form(),
        end: date = Form(),
        timezone: str = Form(),
        file: UploadFile = File(),
        background: bool = False,
        member=Depends(authenticate),
    ):
        require(member, "admin")
        manifest = dataset(member, dataset_id)
        TimeRange(start=start, end=end)
        entries, sha = parse_ledger(file.file.read(), timezone)
        with database(write=True) as db:
            old = db.execute("SELECT * FROM refund_ledgers WHERE dataset=?", (dataset_id,)).fetchone()
            if old and (old["sha256"], old["start"], old["end"], old["timezone"]) != (
                sha,
                str(start),
                str(end),
                timezone,
            ):
                raise ValueError("该快照已绑定退款交付，更正请导入新版快照")
            db.execute(
                "INSERT OR IGNORE INTO refund_ledgers VALUES(?,?,?,?,?,?,?,?)",
                (dataset_id, str(start), str(end), timezone, sha, file.filename, member.name, dump(entries)),
            )
            if background:
                return {"unique_records": len(entries), "sha256": sha,
                        **enqueue_revisions(db, manifest, "purchase_at", member, [dataset_id, sha], refunds_only=True)}
        return {
            "unique_records": len(entries),
            "sha256": sha,
            **revise_complete(manifest, "purchase_at", member, refunds_only=True),
        }

    @web.post("/api/weekly/net")
    def net(req: RefundPeriod, member=Depends(authenticate)):
        require(member, "analyze")
        dataset(member, req.dataset)
        with database() as db:
            confirmation = covered(db, req.dataset, "purchase_at", req.start, req.end)
            ledger = ledger_for(db, req.dataset, req)
        value = reconcile(
            executor_for(req.dataset),
            json.loads(ledger["entries"]),
            TimeRange(start=req.start, end=req.end),
        )
        if not member.can("details"):
            value.pop("exceptions")
            value.pop("postings")
        if not member.can("sql"):
            value.pop("statements")
        if req.finance_total is not None:
            net_cny = value["currencies"]["CNY"]["net"]
            value["finance_difference_cny"] = net_cny - req.finance_total if net_cny is not None else None
        value.update(
            ledger_sha256=ledger["sha256"],
            ledger_file=ledger["filename"],
            timezone=ledger["timezone"],
            coverage=confirmation,
            dataset=req.dataset,
        )
        return json.loads(dump(value))
