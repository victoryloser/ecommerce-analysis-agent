"""Record D1/A and D4/B through real UI clicks and the configured model."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/".build/browser-check")]


def main(provider_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT/"data")
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--port", type=int, default=7874)
    parser.add_argument("--browser-channel", default="msedge")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from playwright.sync_api import sync_playwright, expect
    import app.ui as ui
    from app.executor import Executor
    from app.oracle import SourceOracle
    from app.orchestrator import AnalysisAgent
    from app.providers import ChatCompletionsProvider
    from app.round2_evaluate import fingerprint
    from app.round2_suite import load_cases
    from app.round2_judge import judge
    from app.session_store import SessionStore
    mapping = json.loads(args.snapshots.read_text(encoding="utf-8"))
    executors = {role: Executor(mapping[role], args.data_dir) for role in ("D1", "D4")}
    cases = load_cases(args.questions)
    provider = (provider_factory or ChatCompletionsProvider)()
    oracles = {role: SourceOracle(e.manifest["dataset_id"], data_dir=args.data_dir) for role, e in executors.items()}
    store = SessionStore(args.sessions)
    report = {"mode": "fixed-planner-browser" if provider_factory else "real-browser-real-model", "status": "recording", "fingerprint": fingerprint(provider, executors, cases),
              "turns": [], "scenes": [], "page_errors": []}
    def save():
        (args.output/"recording.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    class RecordedAgent(AnalysisAgent):
        def respond(self, session, question, *a, **kw):
            turn = super().respond(session, question, *a, **kw)
            report["turns"].append({"question": question, "session_id": session.session_id, "turn_id": turn.evidence.get("turn_id"),
                                    "answer_text": turn.text, "evidence": turn.evidence})
            save()
            return turn
    ui.registered_datasets = lambda: {mapping[role]: role for role in ("D1", "D4")}
    ui.Executor = lambda key: next(e for e in executors.values() if e.manifest["dataset_id"] == key)
    ui.SessionStore = lambda: store
    ui.ChatCompletionsProvider = lambda: provider
    ui.AnalysisAgent = RecordedAgent
    demo = ui.build_ui()
    save()
    started = time.monotonic()
    try:
        demo.launch(server_name="127.0.0.1", server_port=args.port, prevent_thread_lock=True, quiet=True, footer_links=[])
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=args.browser_channel, headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1080}, record_video_dir=str(args.output/"raw"),
                record_video_size={"width": 1440, "height": 1080}, color_scheme="light")
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = context.new_page()
            video = page.video
            page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
            try:
                page.goto(demo.local_url, wait_until="domcontentloaded")
                page.get_by_label("问题", exact=True).wait_for(timeout=30000)
                prior_session = None
                for role, group in (("D1", "A"), ("D4", "B")):
                    if role == "D4":
                        page.get_by_label("分析数据", exact=True).click()
                        page.get_by_role("option", name=role, exact=True).click()
                        expect(page.get_by_label("分析数据", exact=True)).to_have_value(role)
                    page.get_by_role("button", name="新建会话", exact=True).click()
                    for case in [c for c in cases if c.case_id.startswith(group)]:
                        report["scenes"].append({"case_id": case.case_id, "logical_dataset": role, "seconds": round(time.monotonic()-started, 3)})
                        count = len(report["turns"])
                        field = page.get_by_label("问题", exact=True)
                        field.fill(case.question)
                        page.get_by_role("button", name="分析", exact=True).click()
                        deadline = time.monotonic()+provider.timeout*4+60
                        while len(report["turns"]) == count:
                            if time.monotonic() > deadline:
                                raise TimeoutError(case.case_id)
                            page.wait_for_timeout(200)
                        expect(field).to_have_value("", timeout=20000)
                        entry = report["turns"][-1]
                        assert entry["evidence"]["dataset_id"] == mapping[role], "旧快照结果残留"
                        if case.case_id.endswith("1"):
                            assert entry["session_id"] != prior_session, "换链未新建会话"
                        prior_session = entry["session_id"]
                        prior_records = [r for t in report["turns"][:-1] if t["session_id"] == entry["session_id"]
                                         for r in [t["evidence"], *t["evidence"].get("steps", [])] if r.get("record_id")]
                        entry["verdict"] = judge(case.case_id, executors[role].manifest, oracles[role], entry["evidence"], entry["answer_text"], turn_id=entry["turn_id"], prior_records=prior_records)
                        report["scenes"][-1].update(turn_id=entry["turn_id"], record_id=entry["evidence"].get("record_id"))
                        page.get_by_text("本轮查询结果", exact=True).scroll_into_view_if_needed()
                        page.screenshot(path=str(args.output/(role+"-"+case.case_id+".png")))
                        page.wait_for_timeout(1800)
                        save()
                with page.expect_download() as download:
                    page.get_by_role("button", name="下载完整依据 JSON", exact=True).click()
                evidence_path = (args.output/"viewed-evidence.json").resolve()
                download.value.save_as(evidence_path)
                page.goto(evidence_path.as_uri())
                page.wait_for_timeout(1800)
                report["status"] = "passed" if all(t["verdict"]["passed"] for t in report["turns"]) and not report["page_errors"] else "failed"
            except BaseException as error:
                report.update(status="failed", error_type=type(error).__name__)
                page.screenshot(path=str(args.output/"failure.png"))
                raise
            finally:
                context.tracing.stop(path=str(args.output/"browser-trace.zip"))
                context.close()
                video.save_as(str(args.output/"round2-raw.webm"))
                browser.close()
                report["recording_seconds"] = round(time.monotonic()-started, 3)
                save()
    finally:
        demo.close()
    target = args.output/"round2.mp4"
    subprocess.run([str(args.ffmpeg), "-hide_banner", "-loglevel", "error", "-n", "-i", str(args.output/"round2-raw.webm"),
                    "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)], check=True)
    subprocess.run([str(args.ffmpeg), "-hide_banner", "-loglevel", "error", "-xerror", "-i", str(target), "-f", "null", "-"], check=True)
    report.update(video_sha256=hashlib.sha256(target.read_bytes()).hexdigest(), full_decode_passed=True, edited=False)
    save()
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
