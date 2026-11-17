"""汇总显式指定的验收报告；重复用例按最后一次结果计分并保留历史。"""
import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.config import RUNS_DIR
from app.evaluation_cases import cases_for
from app.semantic import load_manifest


def summarize(paths: list[Path]) -> dict:
    reports = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    if any("matrix" in r for _, r in reports):
        from app.round2_evaluate import summarize as round2_summary
        if not all("matrix" in r for _, r in reports):
            raise ValueError("正式 R2 与旧套件不能混合汇总")
        if len({json.dumps(r["fingerprint"], sort_keys=True) for _, r in reports}) != 1 or len({r["mode"] for _, r in reports}) != 1:
            raise ValueError("不同模式、题面、判定器、实现、模型配置或数据快照必须分别汇总")
        if len({r["run_id"] for _, r in reports}) != len(reports):
            raise ValueError("同一运行不能重复计分")
        summaries = [round2_summary(r) for _, r in reports]
        totals = {key: sum(s[key] for s in summaries) for key in ("total", "passed", "completed", "first_attempt_chains_passed", "chains_total", "recovery_attempts", "recovery_passed", "model_calls", "query_calls", "catalog_calls")}
        return {**totals, "mode": reports[0][1]["mode"], "model": reports[0][1]["fingerprint"]["provider_config"]["model"],
                "missing": [s for _, r in reports for s in r["matrix"] if not any(a["sample_id"] == s["sample_id"] for a in r["attempts"])],
                "runs": [{"source_report": str(p), "run_id": r["run_id"], "scope": r["scope"], "summary": s}
                         for (p, r), s in zip(reports, summaries)]}
    if len({report["model"] for _, report in reports}) != 1:
        raise ValueError("不同模型的验收结果必须分别汇总")
    if len({report["implementation_sha256"] for _, report in reports}) != 1:
        raise ValueError("不同实现版本的报告必须分别汇总")
    latest = {}
    runs = []
    superseded = []
    for path, report in sorted(reports, key=lambda pair: datetime.fromisoformat(pair[1]["created_at"])):
        for record in report["cases"]:
            runs.append({**record, "dataset_id": report["dataset_id"], "replicate_id": report.get("replicate_id", str(path)), "source_report": str(path)})
            key = (report["dataset_id"], record["case_id"])
            if key in latest:
                superseded.append(latest[key])
            latest[key] = {**record, "dataset_id": report["dataset_id"], "source_report": str(path)}
    datasets = sorted({report["dataset_id"] for _, report in reports})
    expected = {(dataset, case.case_id) for dataset in datasets for case in cases_for(load_manifest(dataset), "all")}
    for dataset in datasets:
        hashes = {report["database_sha256"] for _, report in reports if report["dataset_id"] == dataset}
        if hashes != {load_manifest(dataset)["database_sha256"]}:
            raise ValueError(f"{dataset} 的报告不是当前同一数据快照")
        metadata = {report["metadata_sha256"] for _, report in reports if report["dataset_id"] == dataset}
        if metadata != {load_manifest(dataset)["metadata_sha256"]}:
            raise ValueError(f"{dataset} 的报告不是当前同一元数据版本")
    records = [record for key, record in sorted(latest.items()) if key in expected]
    missing = [{"dataset_id": d, "case_id": c} for d, c in sorted(expected - set(latest))]
    categories = Counter(c["category"] for c in records)
    return {
        "model": reports[0][1]["model"], "source_reports": [str(path) for path in paths],
        "implementation_sha256": reports[0][1]["implementation_sha256"],
        "total": len(expected), "completed": len(records), "passed": sum(c["passed"] for c in records),
        "missing": missing, "planning_calls": sum(c["planning_calls"] for c in records),
        "categories": {category: {"completed": count, "passed": sum(c["passed"] for c in records if c["category"] == category)} for category, count in categories.items()},
        "superseded": superseded, "cases": records,
        "all_runs": runs,
        "stability": {"runs": len(runs), "passed": sum(r["passed"] for r in runs),
                      "pass_rate": sum(r["passed"] for r in runs)/len(runs) if runs else None},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    report = summarize(args.reports)
    path = RUNS_DIR / "live-acceptance-summary.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['model']}: {report['passed']}/{report['total']}, completed={report['completed']}, missing={len(report['missing'])}")
    print(path)
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
