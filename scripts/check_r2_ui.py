"""Exercise the formal recorder using synthetic data and a clearly labeled fixed planner."""
import csv
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"tests"), str(ROOT/".build/browser-check")]


def main():
    import imageio_ffmpeg
    import app.config, app.evidence, app.presentation, app.trace
    from app.import_data import import_tables
    from app.calculation import ROUND2_RULE
    from app.source import SCHEMA
    from app.round2_suite import CASE_IDS
    from test_round2_amounts import source_tables
    from test_r2_evaluation import scripted_plan
    from test_semifinal_state import Scripted
    from scripts.record_round2 import main as record
    work = ROOT/".build"/("r2-ui-"+uuid4().hex[:8])
    work.mkdir(parents=True)
    mapping, manifests = {}, {}
    tables = source_tables()
    for table in ("products", "categories"):
        for row in tables[table]:
            if row["category_id"] == "B":
                row["category_id"] = "electronics"
    files = []
    for table, rows in tables.items():
        path = work/(table+".csv")
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SCHEMA[table])
            writer.writeheader()
            writer.writerows(rows)
        files.append(path)
    for role in ("D1", "D4"):
        mapping[role] = "synthetic_"+role
        manifests[role] = import_tables(mapping[role], tables, files, role, work, quality_rule_version=ROUND2_RULE)
    (work/"snapshots.json").write_text(json.dumps(mapping), encoding="utf-8")
    (work/"questions.md").write_text("\n".join(f"|{case}|合成回归问题 {case}|" for case in CASE_IDS), encoding="utf-8")
    for module in (app.config, app.evidence, app.presentation, app.trace):
        module.RUNS_DIR = work/"runs"
    provider = Scripted(*[scripted_plan(manifests["D1" if case[0] == "A" else "D4"], case) for case in CASE_IDS])
    provider.timeout = 30
    sys.argv = ["record_round2", "--snapshots", str(work/"snapshots.json"), "--questions", str(work/"questions.md"),
                "--data-dir", str(work), "--sessions", str(work/"sessions.sqlite3"), "--output", str(work/"video"),
                "--ffmpeg", imageio_ffmpeg.get_ffmpeg_exe()]
    record(lambda: provider)
    report = json.loads((work/"video/recording.json").read_text(encoding="utf-8"))
    assert report["mode"] == "fixed-planner-browser" and report["status"] == "passed" and report["full_decode_passed"]
    assert len(report["turns"]) == 8
    print("PASS: synthetic browser A/B, dataset switch, evidence download, recording and decode", work)


if __name__ == "__main__":
    main()
