"""本机单用户会话档案；完整记录落盘，模型上下文仍保持有界。"""
import json
import sqlite3
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.config import DATA_DIR
from app.models import AnalysisPlan, QueryResult
from app.session import Session


def snapshot_identity(manifest: dict) -> dict:
    return {key: manifest.get(key) for key in ("dataset_id", "metadata_sha256", "schema_sha256", "sources", "database_sha256", "quality_rule_version")}


class SessionStore:
    def __init__(self, path: Path = DATA_DIR / "sessions.sqlite3"):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, title TEXT NOT NULL,
                updated_at TEXT NOT NULL, snapshot TEXT NOT NULL, state TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS memories (
                session_id TEXT NOT NULL, turn_index INTEGER NOT NULL, created_at TEXT NOT NULL,
                question TEXT NOT NULL, answer TEXT NOT NULL, evidence_id TEXT,
                PRIMARY KEY(session_id,turn_index))""")
            # 首次升级时为已有完整记录建立索引，旧记录日期使用其保存时间。
            for session_id, updated, state in conn.execute("SELECT session_id,updated_at,state FROM sessions").fetchall():
                self._index(conn, session_id, json.loads(state), updated)

    @staticmethod
    def _index(conn, session_id, state, timestamp):
        transcript = state["transcript"]
        if not transcript:
            conn.execute("DELETE FROM memories WHERE session_id=?", (session_id,))
        for index in range(0, len(transcript) - 1, 2):
            evidence_id = state["last_evidence"].get("record_id") if index == len(transcript) - 2 else None
            comparison = state.get("comparison_state", {})
            if comparison.get("transcript_turn_index") == index // 2:
                evidence_id = comparison["artifact_id"]
            conn.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?,?,?,?)",
                         (session_id, index // 2, timestamp, transcript[index]["content"], transcript[index+1]["content"], evidence_id))

    def save(self, session: Session, manifest: dict) -> None:
        state = {"history": session.history, "transcript": session.transcript,
                 "last_evidence": session.last_evidence, "analysis_evidence": session.analysis_evidence,
                 "drill_stack": session.drill_stack, "sql_state": session.sql_state, "workflow_evidence": session.workflow_evidence,
                 "focus_step": session.focus_step, "workflow_tasks": session.workflow_tasks,
                 "comparison_state": session.comparison_state,
                 "analysis_anchor": session.analysis_anchor, "view_focus": session.view_focus}
        for name in ("last_successful_plan", "pending_plan", "last_result", "working_plan"):
            value = getattr(session, name)
            state[name] = value.model_dump(mode="json") if value else None
        title = (session.transcript[0]["content"] if session.transcript else session.sql_state.get("question", "新会话"))[:60]
        with sqlite3.connect(self.path) as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                         "title=excluded.title, updated_at=excluded.updated_at, snapshot=excluded.snapshot, state=excluded.state",
                         (session.session_id, session.dataset_id, title, now,
                          json.dumps(snapshot_identity(manifest), sort_keys=True), json.dumps(state, ensure_ascii=False, default=str)))
            self._index(conn, session.session_id, state, now)

    def load(self, session_id: str, manifest: dict) -> Session:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT dataset_id, snapshot, state FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("未找到该会话")
        if json.loads(row[1]) != snapshot_identity(manifest):
            raise ValueError("会话绑定的数据或元数据已改变，请使用原数据恢复或新建会话")
        state = json.loads(row[2])
        for name in ("last_successful_plan", "pending_plan", "working_plan"):
            state[name] = AnalysisPlan.model_validate(state[name]) if state.get(name) else None
        state["last_result"] = QueryResult.model_validate(state["last_result"]) if state["last_result"] else None
        # 执行授权不持久化，恢复后每次查询重新登记和审计。
        return Session(dataset_id=row[0], session_id=session_id, **state)

    def list(self, dataset_id: str) -> list[dict]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT session_id, title, updated_at FROM sessions WHERE dataset_id=? ORDER BY updated_at DESC",
                                (dataset_id,)).fetchall()
        return [dict(zip(("session_id", "title", "updated_at"), row)) for row in rows]

    def search(self, query: str, manifest: dict, start=None, end=None, limit: int = 8) -> "list[dict]":
        """字符TF-IDF检索配合指标同义词归一化；不将另一版本数据作为当前结果。"""
        from app.metadata import METRIC_DETAILS
        def normalize(text):
            text = text.lower()
            for metric, detail in METRIC_DETAILS.items():
                for alias in sorted(detail["aliases"], key=len, reverse=True):
                    text = text.replace(alias.lower(), metric)
            for word in ("请", "找出", "查找", "上周", "以前", "那次", "结果", "分析", "记录", "会话", "历史"):
                text = text.replace(word, "")
            return re.sub(r"[^\w]", "", text)
        def tokens(text):
            text = normalize(text)
            return Counter([text[i:i+size] for size in (2, 3) for i in range(max(0, len(text)-size+1))])
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT m.session_id,s.title,m.created_at,m.question,m.answer,m.evidence_id,s.snapshot,m.turn_index "
                                "FROM memories m JOIN sessions s ON s.session_id=m.session_id WHERE s.dataset_id=?", (manifest["dataset_id"],)).fetchall()
        rows = [r for r in rows if json.loads(r[6]) == snapshot_identity(manifest)
                and (start is None or datetime.fromisoformat(r[2]).astimezone().date() >= start)
                and (end is None or datetime.fromisoformat(r[2]).astimezone().date() < end)]
        documents = [tokens(r[3] + " " + r[4][:3000]) for r in rows]
        query_tokens = tokens(query)
        frequency = Counter(token for doc in documents for token in doc)
        def vector(counts):
            return {key: (1 + math.log(value)) * (1 + math.log((len(documents)+1)/(frequency[key]+1))) for key, value in counts.items()}
        q = vector(query_tokens)
        norm = math.sqrt(sum(v*v for v in q.values()))
        hits = []
        for row, doc in zip(rows, documents):
            d = vector(doc)
            denom = norm * math.sqrt(sum(v*v for v in d.values()))
            score = sum(value*d.get(key, 0) for key, value in q.items()) / denom if denom else 0
            if query_tokens and score == 0:
                continue
            hits.append({"session_id": row[0], "title": row[1], "created_at": row[2], "question": row[3],
                         "answer": row[4], "evidence_id": row[5], "turn_index": row[7], "score": round(score, 6)})
        return sorted(hits, key=lambda r: (r["score"], r["created_at"]), reverse=True)[:limit]
