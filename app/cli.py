import argparse

import httpx

from app.executor import Executor
from app.orchestrator import AnalysisAgent
from app.providers import ChatCompletionsProvider, ModelConfigurationError, model_error_message
from app.session import Session
from app.session_store import SessionStore
from app.sql_workspace import SQLWorkspace, sql_answer
from app.data_upload import registered_datasets
from app.delivery import is_delivery_request, refresh_delivery
from app.snapshots import resolve_dataset
from app.config import DATASETS


def main():
    parser = argparse.ArgumentParser(description="模型驱动的经营分析对话；输入 /exit 退出，/reset 清空条件")
    datasets = registered_datasets()
    aliases = [name for name in DATASETS if resolve_dataset(name) in datasets]
    parser.add_argument("--dataset", choices=list(dict.fromkeys([*datasets, *aliases])), default=next(iter(datasets), None))
    parser.add_argument("--practice", action="store_true", help="为示例问题启用2025年上下文")
    parser.add_argument("--snapshot", choices=datasets, help="直接选择物理快照ID，不解析内置样例别名")
    parser.add_argument("--session", help="恢复已保存会话ID")
    parser.add_argument("--list-sessions", action="store_true", help="列出当前数据集的本机会话")
    args = parser.parse_args()
    if args.dataset is None:
        parser.error("尚未导入数据。请先运行 python -m app.ui，在页面中上传 Excel 或 CSV 文件。")
    args.dataset = args.snapshot or resolve_dataset(args.dataset)
    store = SessionStore()
    if args.list_sessions:
        for row in store.list(args.dataset):
            print(row["session_id"], row["updated_at"], row["title"])
        return
    executors = {key: Executor(key) for key in datasets}
    executor = executors[args.dataset]
    agent = AnalysisAgent(None, executors, store)
    workspace = SQLWorkspace(agent.executors, store)
    session = store.load(args.session, executor.manifest) if args.session else Session(args.dataset)
    print(f"会话ID：{session.session_id}（自动保存）；/drill 行号 维度、/back、/sql SELECT…、/raw SELECT…")
    print("模型请求失败后用 /retry 重发原问题。已开始执行的查询可用 /continue 继续。")
    print("新版交付后，输入“数据更新了”或 /sync 可同步并按当前条件重算。")
    failed_question = None
    while (question := input("\n问题> ").strip()) != "/exit":
        if not question:
            continue
        try:
            if is_delivery_request(question):
                updated, text = refresh_delivery(agent, session, question)
                if updated.dataset_id != session.dataset_id:
                    failed_question = None
                session = updated
                print(text)
                if session.last_evidence.get("record_id"):
                    print(f"\n当前结果依据：runs/{session.dataset_id}/{session.last_evidence['record_id']}.json")
                continue
            command, *arguments = question.split(maxsplit=1)
            if command == "/drill":
                parts = arguments[0].split() if arguments else []
                if len(parts) != 2 or not parts[0].isdecimal():
                    raise ValueError("用法：/drill 行号 维度，行号从 1 开始。")
                turn = agent.drill(session, int(parts[0]) - 1, parts[1])
            elif question == "/back":
                turn = agent.back(session)
            elif question in {"/next", "/prev"}:
                turn = agent.page(session, 1 if question == "/next" else -1)
            elif question == "/continue":
                if session.working_plan is None:
                    print("当前没有可继续的查询。")
                    continue
                turn = agent.resume_inspection(session)
            elif question == "/export":
                print(agent.export(session))
                continue
            elif command == "/compare" or question == "/continue-compare":
                if command == "/compare":
                    if not arguments:
                        raise ValueError("用法：/compare 目标快照ID")
                    turn = agent.compare(session, {"target_dataset_id": resolve_dataset(arguments[0])})
                else:
                    turn = agent.compare(session, resume=True)
                print(turn.text)
                print(turn.evidence["report_file"])
                continue
            elif command in {"/sql", "/raw"}:
                if not arguments:
                    raise ValueError(f"用法：{command} SELECT …")
                scope = "raw" if command == "/raw" else "analysis_result"
                evidence = workspace.execute(session, arguments[0], [], scope)
                print(sql_answer(evidence))
                print(evidence["result"])
                continue
            else:
                if question == "/retry":
                    if failed_question is None:
                        print("当前没有待重发的模型问题。")
                        continue
                    question = failed_question
                turn = agent.respond(session, question, args.practice, provider_factory=ChatCompletionsProvider)
                failed_question = None
        except (httpx.HTTPError, ModelConfigurationError) as error:
            failed_question = question
            print(model_error_message(error) + " 原问题已保留，可输入 /retry。")
            continue
        except ValueError as error:
            print(f"未完成本次操作：{error}")
            continue
        print(turn.text)
        if "record_id" in turn.evidence:
            print(f"\n证据：runs/{session.dataset_id}/{turn.evidence['record_id']}.json")


if __name__ == "__main__":
    main()
