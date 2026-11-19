"""Team verification tickets and immutable observations from a particular report."""

from decimal import Decimal
import json
from uuid import uuid4

from app.weekly import dump

SCHEMA = """
CREATE TABLE IF NOT EXISTS verification_tickets(
 id TEXT PRIMARY KEY, feed TEXT, order_id TEXT, item_key TEXT, kind TEXT, assignee TEXT,
 scope TEXT, state TEXT, latest TEXT, UNIQUE(feed,order_id,item_key,kind));
CREATE TABLE IF NOT EXISTS verification_events(
 seq INTEGER PRIMARY KEY, ticket TEXT, report TEXT, dataset TEXT, actor TEXT,
 at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), event TEXT, payload TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS verification_observations ON verification_events(ticket,dataset)
 WHERE event='复验';
"""


def observe(db, ticket, report_id, report, actor):
    if ticket["scope"] != report["scope"] or report["imported_at"] <= ticket["latest"]:
        return
    oid, key = ticket["order_id"], ticket["item_key"]
    order = next((members[oid] for members in report["order_evidence"].values() if oid in members), None)
    conflict = order and order["conflicts"].get(key)
    item = order and order["items"].get(key)
    originally_selected = json.loads(
        db.execute(
            "SELECT payload FROM verification_events WHERE ticket=? AND event='建单'", (ticket["id"],)
        ).fetchone()["payload"]
    )["selected_item"]
    if conflict:
        status, evidence = "仍有价格冲突", conflict
    elif (
        not order
        or not order["confirmed"]
        or key not in order.get("source_keys", order["items"])
        or originally_selected
        and not item
        or item is not None
        and not item["confirmed"]
    ):
        status, evidence = "订单或明细离开原范围，待核实", {}
    elif not order["items"] or any(
        not v["eligible"] or not v["confirmed"] or v.get("sales") is None for v in order["items"].values()
    ):
        status, evidence = "金额仍待核实", {}
    else:
        status = "已恢复计入"
        evidence = {"included_sales": str(sum(Decimal(str(v["sales"])) for v in order["items"].values()))}
    state = "待确认关闭" if status == "已恢复计入" else "待处理"
    if ticket["state"] == "已关闭" and status == "已恢复计入":
        state = "已关闭"
    db.execute(
        "UPDATE verification_tickets SET latest=?,state=? WHERE id=?", (report["imported_at"], state, ticket["id"])
    )
    db.execute(
        "INSERT INTO verification_events(ticket,report,dataset,actor,event,payload) VALUES(?,?,?,?,?,?)",
        (ticket["id"], report_id, report["dataset"], actor, "复验", dump({"result": status, **evidence})),
    )


def create_ticket(db, report_id, report, oid, key, actor, assignee):
    order = next((v[oid] for v in report["order_evidence"].values() if oid in v), None)
    if not order or key not in order["conflicts"]:
        raise ValueError("请选择该报告中有价格冲突的订单和明细键")
    existing = db.execute(
        "SELECT id FROM verification_tickets WHERE feed=? AND order_id=? AND item_key=? AND kind='价格冲突'",
        (report["feed"], oid, key),
    ).fetchone()
    if existing:
        return existing["id"]
    key_id = uuid4().hex
    db.execute(
        "INSERT INTO verification_tickets VALUES(?,?,?,?,?,?,?,?,?)",
        (key_id, report["feed"], oid, key, "价格冲突", assignee, report["scope"], "待处理", report["imported_at"]),
    )
    db.execute(
        "INSERT INTO verification_events(ticket,report,dataset,actor,event,payload) VALUES(?,?,?,?,?,?)",
        (
            key_id,
            report_id,
            report["dataset"],
            actor,
            "建单",
            dump(
                {
                    **order["conflicts"][key],
                    "metrics": report["plan"]["metric_ids"],
                    "amount_rule": report["rule"],
                    "selected_item": key in order["items"],
                }
            ),
        ),
    )
    return key_id


def follow_up(db, ticket, action, actor, note, batch):
    if action == "close" and ticket["state"] != "待确认关闭":
        raise ValueError("需在完整新版上复验并确认恢复计入，才能关闭")
    if action == "correct" and (not batch.strip() or ticket["state"] == "已关闭"):
        raise ValueError("请为未关闭的核实单填写预期交付批次")
    if not note.strip():
        raise ValueError("请填写跟进或确认说明")
    state = {"close": "已关闭", "correct": "待复验", "comment": ticket["state"]}[action]
    db.execute("UPDATE verification_tickets SET state=? WHERE id=?", (state, ticket["id"]))
    db.execute(
        "INSERT INTO verification_events(ticket,actor,event,payload) VALUES(?,?,?,?)",
        (
            ticket["id"],
            actor,
            {"close": "确认关闭", "correct": "登记更正", "comment": "跟进"}[action],
            dump({"note": note, "batch": batch}),
        ),
    )
