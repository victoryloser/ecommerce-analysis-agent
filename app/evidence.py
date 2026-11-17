import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from app.config import RUNS_DIR
from app.models import AnalysisPlan, QueryResult
from app.session import Session
from app.sql_compiler import CompiledQuery
from app.trace import TurnTrace
from app.semantic import get_catalog
from app.snapshots import implementation_hash


def write_evidence(session: Session, manifest: dict, question: str, plan: AnalysisPlan, model: str,
                   attempts: int, result: QueryResult | None = None, compiled: CompiledQuery | None = None, trace: TurnTrace | None = None,
                   provenance: dict | None = None) -> dict:
    record_id = result.query_id if result else uuid4().hex
    record = {
        "record_id": record_id, "type": "query" if result else plan.intent,
        "created_at": datetime.now(timezone.utc).isoformat(), "session_id": session.session_id,
        "dataset_id": session.dataset_id, "metadata_version": manifest["metadata_version"],
        "source_hashes": manifest["sources"], "schema_sha256": manifest["schema_sha256"],
        "metadata_sha256": manifest["metadata_sha256"],
        "database_sha256": manifest["database_sha256"], "question": question,
        "quality_rule_version": manifest.get("quality_rule_version"),
        "implementation_sha256": implementation_hash(),
        "model": model, "planning_calls": attempts, "plan": plan.model_dump(mode="json"),
        "sql": compiled.sql if compiled else None, "parameters": compiled.parameters if compiled else None,
        "result": result.model_dump() if result else None,
        "catalog": get_catalog(manifest, plan),
    }
    if trace:
        record.update(turn_id=trace.turn_id, event_file=str(trace.event_file.relative_to(RUNS_DIR)), calls_used=trace.steps)
    if provenance:
        record["provenance"] = provenance
    if result:
        serialized = json.dumps({"columns": result.columns, "rows": result.rows, "quality": result.quality, "ratios": result.ratios}, ensure_ascii=False, default=str, sort_keys=True)
        record["result_sha256"] = hashlib.sha256(serialized.encode()).hexdigest()
    directory = RUNS_DIR / session.dataset_id
    directory.mkdir(parents=True, exist_ok=True)
    content = json.dumps(record, ensure_ascii=False, indent=2, default=str)
    (directory / f"{record_id}.json").write_text(content, encoding="utf-8")
    return json.loads(content)


def write_artifact(session: Session, manifest: dict, kind: str, question: str, payload: dict, trace: TurnTrace) -> dict:
    record_id = uuid4().hex
    record = {"record_id": record_id, "type": kind, "question": question, "session_id": session.session_id,
              "dataset_id": session.dataset_id, "created_at": datetime.now(timezone.utc).isoformat(),
              "source_hashes": manifest["sources"], "metadata_sha256": manifest["metadata_sha256"],
              "turn_id": trace.turn_id, "event_file": str(trace.event_file.relative_to(RUNS_DIR)),
              "calls_used": trace.steps, **payload}
    record.update(database_sha256=manifest["database_sha256"], quality_rule_version=manifest.get("quality_rule_version"))
    record["implementation_sha256"] = implementation_hash()
    directory = RUNS_DIR / session.dataset_id
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, ensure_ascii=False, default=str, indent=2)
    (directory / f"{record_id}.json").write_text(text, encoding="utf-8")
    return json.loads(text)
