import argparse

from app.executor import Executor
from app.orchestrator import AnalysisAgent
from app.providers import ChatCompletionsProvider
from app.session import Session
from app.session_store import SessionStore
from app.sql_workspace import SQLWorkspace, sql_answer
from app.data_upload import registered_datasets


def main():
    parser = argparse.ArgumentParser(description="模型驱动的经营分析对话；输入 /exit 退出，/reset 清空条件")
    datasets = registered_datasets()
    parser.add_argument("--dataset", choices=datasets, default=next(iter(datasets), None))
    parser.add_argument("--practice", action="store_true", help="为示例问题启用2025年上下文")
    parser.add_argument("--session", help="恢复已保存会话ID")
    parser.add_argument("--list-sessions", action="store_true", help="列出当前数据集的本机会话")
    args = parser.parse_args()
    if args.dataset is None:
        parser.error("尚未导入数据。请先运行 python -m app.ui，在页面中上传 Excel 或 CSV 文件。")
    store = SessionStore()
    if args.list_sessions:
        for row in store.list(args.dataset):
            print(row["session_id"], row["updated_at"], row["title"])
        return
    executor = Executor(args.dataset)
    agent = AnalysisAgent(ChatCompletionsProvider(), {args.dataset: executor}, store)
    workspace = SQLWorkspace(agent.executors, store)
    session = store.load(args.session, executor.manifest) if args.session else Session(args.dataset)
    print(f"会话ID：{session.session_id}（自动保存）；/drill 行号 维度、/back、/sql SELECT…、/raw SELECT…")
    while (question := input("\n问题> ").strip()) != "/exit":
        if question.startswith("/drill "):
            _, row, dimension = question.split()
            turn = agent.drill(session, int(row) - 1, dimension)
        elif question == "/back":
            turn = agent.back(session)
        elif question in {"/next", "/prev"}:
            turn = agent.page(session, 1 if question == "/next" else -1)
        elif question == "/export":
            print(agent.export(session))
            continue
        elif question.startswith(("/sql ", "/raw ")):
            command, sql = question.split(" ", 1)
            evidence = workspace.execute(session, sql, [], "raw" if command == "/raw" else "analysis_result")
            print(sql_answer(evidence))
            print(evidence["result"])
            continue
        else:
            turn = agent.respond(session, question, args.practice)
        print(turn.text)
        if "record_id" in turn.evidence:
            print(f"\n证据：runs/{args.dataset}/{turn.evidence['record_id']}.json")


if __name__ == "__main__":
    main()
