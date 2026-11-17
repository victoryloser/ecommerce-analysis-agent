"""Sequential formal evaluations with immutable attempts and checkpointed sessions."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx

from app.config import ROOT, DATA_DIR, RUNS_DIR
from app.executor import Executor
from app.oracle import SourceOracle
from app.orchestrator import AnalysisAgent
from app.round2_judge import judge
from app.round2_suite import SUITE_VERSION, load_cases, selected_chains
from app.session import Session
from app.session_store import SessionStore, snapshot_identity
from app.snapshots import digest, implementation_hash
from app.trace import TurnTrace


def sha_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(provider, executors, questions):
    from app import round2_judge, round2_suite, oracle, oracle_inspection
    config = provider.evaluation_config() if hasattr(provider, "evaluation_config") else {"model": provider.model, "protocol": "test-provider"}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, capture_output=True, text=True)
    return {"suite": SUITE_VERSION, "questions_sha256": digest([vars(c) for c in questions]),
            "contract_sha256": sha_file(Path(round2_suite.__file__)),
            "judge_sha256": digest([sha_file(Path(m.__file__)) for m in (round2_judge, oracle, oracle_inspection)]),
            "implementation_sha256": implementation_hash(), "dependency_lock_sha256": sha_file(ROOT/"requirements.lock.txt"),
            "tooling_sha256": digest({p.name: sha_file(p) for p in sorted((ROOT/"scripts").glob("*.py"))}),
            "provider_config": config, "provider_config_sha256": digest(config),
            "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "worktree_dirty": bool(dirty.stdout) if dirty.returncode == 0 else None,
            "snapshots": {role: snapshot_identity(ex.manifest) for role, ex in executors.items()}}


def summarize(report):
    first = {slot["sample_id"]: next((a for a in report["attempts"] if a["sample_id"] == slot["sample_id"] and a["parent_attempt_id"] is None), None)
             for slot in report["matrix"]}
    counts = Counter(a["status"] if a else "not_run" for a in first.values())
    chains = {}
    for slot in report["matrix"]:
        key = (slot["replicate_id"], slot["logical_dataset"], slot["chain_id"])
        chains.setdefault(key, []).append(first[slot["sample_id"]])
    attempts = report["attempts"]
    return {"total": len(first), "passed": counts["passed"], "completed": sum(a is not None and a["status"] != "running" for a in first.values()),
        "statuses": dict(counts), "first_attempt_chains_passed": sum(all(a and a["status"] == "passed" for a in group) for group in chains.values()),
        "chains_total": len(chains), "recovery_attempts": sum(a["parent_attempt_id"] is not None for a in attempts),
        "recovery_passed": sum(a["parent_attempt_id"] is not None and a["status"] == "passed" for a in attempts),
        "model_calls": sum(a.get("model_calls", 0) for a in attempts), "query_calls": sum(a.get("query_calls", 0) for a in attempts),
        "catalog_calls": sum(a.get("catalog_calls", 0) for a in attempts),
        "total_calls": sum(a.get("steps", 0) for a in attempts)}


def checkpoint_identity(session):
    return digest({"working_plan": session.working_plan.model_dump(mode="json") if session.working_plan else None,
                   "tasks": session.workflow_tasks, "history": session.history, "last_evidence": session.last_evidence})


def persist(path, report):
    report["summary"] = summarize(report)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def export_index(path, report):
    """Portable copies of cited evidence and traces, verified against a hash inventory."""
    directory = path.parent / "evidence"
    directory.mkdir(exist_ok=True)
    files = {}
    for attempt in report["attempts"]:
        paths = []
        if attempt.get("event_file"):
            paths.append(RUNS_DIR / attempt["event_file"])
        for record in attempt.get("evidence_records", []):
            paths.append(RUNS_DIR / record["dataset_id"] / (record["record_id"]+".json"))
            result = record.get("result") or {}
            if result.get("export_file"):
                paths.append(RUNS_DIR / result["export_file"])
        links = []
        for source in paths:
            payload = source.read_bytes()
            name = hashlib.sha256(payload).hexdigest()+source.suffix
            target = directory/name
            target.write_bytes(payload)
            assert sha_file(target) == hashlib.sha256(payload).hexdigest()
            relative = "evidence/"+name
            files[relative] = {"sha256": sha_file(target), "bytes": len(payload)}
            links.append(relative)
        attempt["artifact_links"] = links
    (path.parent/"evidence-index.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    lines = ["# R2 运行证据", "", f"模式：{report['mode']}；范围：{report['scope']}。", "",
             "|样本|状态|回合|证据|", "|---|---|---|---|"]
    for a in report["attempts"]:
        links = " ".join(f"[文件{i+1}]({link})" for i, link in enumerate(a.get("artifact_links", [])))
        lines.append(f"|{a['sample_id']}|{a['status']}|{a['turn_id']}|{links}|")
    lines += ["", "## 案例证据入口", "", "金额规则与可确认结果：查看 D4/B2、D4/B3。连续更正与整单核查：查看 A1–A4、B3–B4。",
              "", "本索引引用实际尝试；未通过或未运行项不构成成功案例。初赛对照需另行核实实际提交版本。"]
    (path.parent/"index.md").write_text("\n".join(lines), encoding="utf-8")


def evaluate_round2(provider, executors, cases, output, *, selection=(), repeats=1, mode="live-model",
                    max_model_calls=128, max_total_calls=400, max_seconds=3600, resume=False, recover=False):
    if repeats < 1 or max_model_calls < 1 or max_total_calls < 1 or max_seconds <= 0:
        raise ValueError("重复数和运行预算必须为正数")
    if recover and not resume:
        raise ValueError("--recover 必须与 --resume 一起使用")
    chains = selected_chains(selection)
    if set(role for role, _ in chains) - set(executors):
        raise ValueError("所选链缺少显式数据快照映射")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output/"report.json"
    identity = fingerprint(provider, executors, cases)
    config = {"selection": list(selection), "repeats": repeats, "mode": mode}
    if resume:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report["fingerprint"] != identity or report["config"] != config:
            raise ValueError("恢复要求实现、判定器、题面、配置及快照身份一致")
        if any(a["status"] == "running" for a in report["attempts"]):
            raise ValueError("上次回合中断，不能证明会话与证据一致；保留旧报告并在新目录重跑整链")
        if report["status"] == "infrastructure_stopped" and not recover:
            raise ValueError("外部故障暂停的回合需要 --recover，不能跳过失败继续下游")
    else:
        if path.exists():
            raise ValueError("输出目录已有运行记录；请使用新目录或 --resume")
        run_id = uuid4().hex
        report = {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "mode": mode,
            "scope": "full" if len(chains) == 8 else "subset", "fingerprint": identity, "config": config,
            "status": "running", "matrix": [], "attempts": [], "sessions": {}}
        for repeat in range(1, repeats+1):
            for role, group in chains:
                for case in cases:
                    if case.case_id.startswith(group):
                        report["matrix"].append({"sample_id": f"r{repeat}/{role}/{case.case_id}", "replicate_id": repeat,
                            "logical_dataset": role, "chain_id": group, "case_id": case.case_id, "question": case.question})
    persist(path, report)  # Persist the complete denominator before any provider request.
    store = SessionStore(output/"sessions.sqlite3")
    agent = AnalysisAgent(provider, {e.manifest["dataset_id"]: e for e in executors.values()}, store)
    oracles = {role: SourceOracle(e.manifest["dataset_id"], data_dir=e.path.parent) for role, e in executors.items()}
    started = time.monotonic()
    report["status"] = "running"
    try:
        for slot in report["matrix"]:
            previous = [a for a in report["attempts"] if a["sample_id"] == slot["sample_id"]]
            if previous and not (recover and previous[-1]["status"] == "infrastructure_failure"):
                continue
            # Reserve the existing per-turn upper bound before starting another turn.
            counts = summarize(report)
            if counts["model_calls"] + 4 > max_model_calls or counts["total_calls"] + 11 > max_total_calls or report.get("elapsed_seconds", 0)+time.monotonic()-started >= max_seconds:
                report["status"] = "budget_stopped"
                break
            role = slot["logical_dataset"]
            manifest = executors[role].manifest
            chain = f"{slot['replicate_id']}/{role}/{slot['chain_id']}"
            session = store.load(report["sessions"][chain], manifest) if chain in report["sessions"] else Session(manifest["dataset_id"])
            if chain not in report["sessions"]:
                report["sessions"][chain] = session.session_id
                store.save(session, manifest)
            parent = previous[-1]["attempt_id"] if previous else None
            # Recovery is only valid before any later question has changed the session.
            if parent and any(a["session_id"] == session.session_id and a["case_id"] > slot["case_id"] for a in report["attempts"]):
                raise ValueError("链已继续，不能原地恢复历史回合；请在新目录重跑整链")
            if parent and previous[-1]["checkpoint_sha256"] != checkpoint_identity(session):
                raise ValueError("恢复会话与失败后的检查点不符")
            partial = parent and session.workflow_evidence.get("turn_id") == previous[-1]["turn_id"] and any(t["status"] != "completed" for t in session.workflow_tasks)
            question = "继续未完成任务" if partial else slot["question"]
            trace = TurnTrace(session.session_id, session.dataset_id, question, provider.model)
            attempt = {**slot, "attempt_id": uuid4().hex, "parent_attempt_id": parent, "session_id": session.session_id,
                       "turn_id": trace.turn_id, "status": "running", "answer_text": "", "checks": [], "evidence_records": []}
            report["attempts"].append(attempt)
            persist(path, report)
            turn_started = time.monotonic()
            try:
                turn = agent.respond(session, question, trace=trace)
                records = [turn.evidence, *turn.evidence.get("steps", [])]
                attempt["evidence_records"] = [r for r in records if r.get("record_id")]
                attempt["answer_text"] = turn.text
                prior_records = [r for a in report["attempts"] if a is not attempt and a["session_id"] == session.session_id
                                 for r in a["evidence_records"]]
                verdict = judge(slot["case_id"], manifest, oracles[role], turn.evidence, turn.text, turn_id=trace.turn_id, prior_records=prior_records)
                attempt.update(status="passed" if verdict["passed"] else "product_failure", checks=verdict["checks"])
                if any((r.get("result") or {}).get("status") == "timeout" for r in records):
                    attempt["status"] = "infrastructure_failure"
            except httpx.HTTPError as error:
                attempt.update(status="infrastructure_failure", failure={"type": type(error).__name__,
                    "http_status": error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None})
            except (ValueError, AssertionError) as error:
                attempt.update(status="product_failure", failure={"type": type(error).__name__, "message": str(error)})
            finally:
                if not attempt["evidence_records"] and session.workflow_evidence.get("turn_id") == trace.turn_id:
                    attempt["evidence_records"] = [session.workflow_evidence, *[r for r in session.workflow_evidence.get("steps", []) if r.get("record_id")]]
                attempt.update(trace.counts(), elapsed_ms=round((time.monotonic()-turn_started)*1000),
                    event_file=trace.event_ref, checkpoint_sha256=checkpoint_identity(session))
                events = [json.loads(line) for line in trace.event_file.read_text(encoding="utf-8").splitlines()]
                attempt["queries_started"] = sum(e["turn_id"] == trace.turn_id and e["event"] == "query_started" for e in events)
                attempt["provider_observations"] = [e for e in events if e["turn_id"] == trace.turn_id and e["event"] == "provider_response"]
                attempt["usage_status"] = "provided" if any(e.get("usage") for e in attempt["provider_observations"]) else "not_provided"
                store.save(session, manifest)
                persist(path, report)
            print(slot["sample_id"], attempt["status"], flush=True)
            if attempt["status"] == "infrastructure_failure":
                report["status"] = "infrastructure_stopped"
                break
        else:
            report["status"] = "completed"
    except BaseException:
        report["status"] = "interrupted"
        raise
    finally:
        report["elapsed_seconds"] = report.get("elapsed_seconds", 0) + round(time.monotonic()-started, 3)
        export_index(path, report)
        report["read_only_verified"] = all(sha_file(ex.path) == ex.manifest["database_sha256"] for ex in executors.values())
        if not report["read_only_verified"]:
            report["status"] = "database_changed"
        persist(path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True, help="正式连续问题 Markdown")
    parser.add_argument("--snapshots", type=Path, required=True, help='JSON: {"D1":"snapshot-id", ...}')
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-model-calls", type=int, default=128)
    parser.add_argument("--max-total-calls", type=int, default=400)
    parser.add_argument("--max-seconds", type=float, default=3600)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    from app.providers import ChatCompletionsProvider
    mapping = json.loads(args.snapshots.read_text(encoding="utf-8"))
    executors = {role: Executor(snapshot, args.data_dir) for role, snapshot in mapping.items()}
    from app.calculation import ROUND2_RULE
    for role, executor in executors.items():
        if executor.manifest["dataset_id"] != mapping[role] or executor.manifest.get("quality_rule_version") != ROUND2_RULE:
            raise ValueError("必须显式指定正式金额规则下的不可变快照")
    report = evaluate_round2(ChatCompletionsProvider(), executors, load_cases(args.questions), args.output,
        selection=args.case, repeats=args.repeats, max_model_calls=args.max_model_calls,
        max_total_calls=args.max_total_calls, max_seconds=args.max_seconds, resume=args.resume, recover=args.recover)
    print(json.dumps(report["summary"], ensure_ascii=False))
    if report["status"] != "completed" or report["summary"]["passed"] != report["summary"]["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
