import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.data_upload import discover, import_upload, registered_datasets, suggest_mapping
from app.delivery import is_delivery_request, refresh_delivery
from app.executor import Executor
from app.orchestrator import AnalysisAgent
from app.session import Session
from test_data_onboarding import write_business_files


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DELIVERY_DIR", str(tmp_path / "incoming"))
    files = write_business_files("csv", tmp_path)
    manifest = import_upload(files, suggest_mapping(discover(files)), "测试", data_dir)
    key = manifest["dataset_id"]
    agent = AnalysisAgent(None, {key: Executor(key, data_dir)})
    directory = tmp_path / "incoming" / key
    directory.mkdir(parents=True)
    for path in files:
        shutil.copy(path, directory)
    return agent, Session(key), directory, data_dir


def test_incomplete_and_invalid_then_corrected_delivery(delivery):
    agent, session, directory, data_dir = delivery
    items = directory / "order_items.csv"
    original = items.read_bytes()
    items.unlink()
    unchanged, text = refresh_delivery(agent, session, "/sync")
    assert unchanged is session and "order_items" in text
    items.write_bytes(original.replace(b"product_id", b"new_product_column"))
    assert "order_items" in refresh_delivery(agent, session, "/sync")[1]
    items.write_bytes(original.replace(b"12.50", b"no-price"))
    assert "未完成同步" in refresh_delivery(agent, session, "/sync")[1]
    assert set(registered_datasets(data_dir)) == {session.dataset_id}
    assert list(data_dir.glob("*.duckdb")) == [agent.executors[session.dataset_id].path]
    items.write_bytes(original.replace(b"12.50", b"18.25"))
    fresh, text = refresh_delivery(agent, session, "/sync")
    assert fresh.dataset_id != session.dataset_id and fresh.dataset_id in text


def test_two_sessions_and_restart_reuse_published_delivery(delivery):
    agent, session, directory, data_dir = delivery
    items = directory / "order_items.csv"
    original = items.read_bytes()
    items.write_bytes(original.replace(b"12.50", b"18.25"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(refresh_delivery, agent, Session(session.dataset_id), "/sync") for _ in range(2)]
    first, second = [f.result()[0] for f in futures]
    assert first.dataset_id == second.dataset_id != session.dataset_id
    assert len(registered_datasets(data_dir)) == 2
    restarted = AnalysisAgent(None, {session.dataset_id: Executor(session.dataset_id, data_dir)})
    assert refresh_delivery(restarted, session, "/sync")[0].dataset_id == first.dataset_id
    items.write_bytes(original)
    reverted, _ = refresh_delivery(restarted, session, "/sync")
    assert restarted.executors[reverted.dataset_id].manifest["parent_dataset_id"] == first.dataset_id
    assert len(registered_datasets(data_dir)) == 3


def test_cli_sync_without_model(delivery, tmp_path, monkeypatch, capsys):
    import app.cli as cli
    from app.session_store import SessionStore
    agent, session, directory, data_dir = delivery
    items = directory / "order_items.csv"
    items.write_bytes(items.read_bytes().replace(b"12.50", b"18.25"))
    monkeypatch.setattr(cli, "registered_datasets", lambda: registered_datasets(data_dir))
    monkeypatch.setattr(cli, "Executor", lambda key: agent.executors[key])
    monkeypatch.setattr(cli, "SessionStore", lambda: SessionStore(tmp_path / "sessions.sqlite3"))
    monkeypatch.setattr("sys.argv", ["cli", "--snapshot", session.dataset_id])
    requests = iter(["数据更新了", "/sync", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(requests))
    cli.main()
    output = capsys.readouterr().out
    assert "已切换到新版" in output and "与当前快照" in output
    assert is_delivery_request("数据更新了，请按原条件重算。")
    assert not is_delivery_request("数据更新了吗？")


def test_cli_recovers_from_invalid_commands_without_contacting_model(delivery, tmp_path, monkeypatch, capsys):
    import app.cli as cli
    from app.session_store import SessionStore

    agent, session, _, data_dir = delivery
    monkeypatch.setattr(cli, "registered_datasets", lambda: registered_datasets(data_dir))
    monkeypatch.setattr(cli, "Executor", lambda key: agent.executors[key])
    monkeypatch.setattr(cli, "SessionStore", lambda: SessionStore(tmp_path / "sessions.sqlite3"))
    monkeypatch.setattr(cli, "ChatCompletionsProvider", lambda: pytest.fail("本地命令不应调用模型"))
    monkeypatch.setattr("app.trace.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("app.sql_workspace.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("sys.argv", ["cli", "--snapshot", session.dataset_id])
    requests = iter(["/back", "/export", "/drill wrong", "/sql SELECT * FROM analysis_result",
                     "/raw SELECT (", "/raw SELECT 'open", "/raw SELECT COUNT(*) AS order_count FROM orders",
                     "/reset", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(requests))
    cli.main()
    output = capsys.readouterr().out
    assert "当前没有上层分析" in output and "请先执行查询再导出" in output
    assert "用法：/drill" in output and "请先完成一次结构化分析" in output
    assert output.count("SQL 语法无效") == 2
    assert "order_count：当前页最小1，最大1" in output
    assert "已清空当前分析条件" in output
