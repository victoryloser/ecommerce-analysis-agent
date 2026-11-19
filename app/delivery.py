"""从当前数据集的交付目录按需同步，并沿用当前会话的分析意图。"""
import csv
import hashlib
import json
import os
import re
import shutil
from decimal import DecimalException
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import BadZipFile

import duckdb
from dotenv import dotenv_values
from filelock import FileLock

from app.calculation import CalculationContext
from app.config import INSTANCE_DIR
from app.data_upload import discover, import_upload, suggest_mapping
from app.executor import Executor
from app.semantic import PlanError
from app.session import Session
from app.snapshots import digest, source_path


class DeliveryAccessError(PermissionError):
    pass


def is_delivery_request(question):
    phrase = re.sub(r"[\s，,。！!]+", "", question)
    return phrase == "/sync" or bool(re.fullmatch(
        r"(?:(?:数据|文件)(?:已经|已)?(?:更新|重新导出|更正)(?:好了|了)?(?:请|帮我)?(?:按原条件|按原口径)?(?:重新计算|重新算一下|重算一下|重算)?"
        r"|(?:请|帮我)?(?:同步|检查)(?:最新|新版)?数据(?:并重算)?)", phrase))


def delivery_directory(manifest, data_dir):
    configured = os.environ.get("DATA_DELIVERY_DIR") or dotenv_values(INSTANCE_DIR / ".env").get("DATA_DELIVERY_DIR")
    base = Path(configured).expanduser() if configured else data_dir / "incoming"
    return base.resolve() / (manifest.get("delivery_source_id") or manifest["dataset_id"])


def _delivery_files(directory):
    files = {}
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in {".csv", ".xlsx"} and not path.name.startswith("~$"):
            stat = path.stat()
            files[path.name] = (stat.st_size, stat.st_mtime_ns)
    return files


def _delivery_mapping(files, manifest, data_dir):
    previous = next((s for s in manifest["sources"] if source_path(s, data_dir).name == "mapping.json"), None)
    if previous is None:
        mapping = suggest_mapping(discover(files))
    else:
        raw = source_path(previous, data_dir).read_bytes()
        if hashlib.sha256(raw).hexdigest() != previous["sha256"]:
            raise ValueError("原快照的字段映射与留存指纹不一致，请核对原件或重新导入。")
        mapping = json.loads(raw)
        workbooks = [p.name for p in files if p.suffix.lower() == ".xlsx"]
        old_names = {t["file"] for t in mapping["tables"].values()}
        for spec in mapping["tables"].values():
            if (spec["sheet"] and len(old_names) == len(workbooks) == 1 and
                    Path(spec["file"]).suffix.lower() == ".xlsx"):
                spec["file"] = workbooks[0]
    names = {p.name for p in files}
    used = set()
    for table, spec in mapping["tables"].items():
        if spec["file"] not in names:
            raise ValueError(f"请核对 {table} 的交付文件；文件名变化时可在网页重新映射。")
        used.add(spec["file"])
    extra = names - used - {"practice_questions.csv"}
    if extra:
        raise ValueError("交付目录包含未映射的文件，请只放一套完整导出：" + "、".join(sorted(extra)))
    return mapping


def _published_delivery(current, directory, data_dir, allow_target):
    feed = directory.name
    head = current
    # 同一目录的同步通过文件锁串行发布。旧会话也应使用这条交付链上已发布的最新版。
    for path in data_dir.glob("upload_*.manifest.json"):
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if candidate.get("delivery_source_id") == feed and candidate["imported_at"] >= head.get("imported_at", ""):
            head = candidate
    if allow_target and not allow_target(head):
        raise DeliveryAccessError("目标快照尚未授权，请联系管理员")
    before = _delivery_files(directory)
    if not before:
        if head["dataset_id"] != current["dataset_id"]:
            return head
        raise ValueError(f"尚未发现交付文件。请将一套完整的 Excel 或 CSV 导出放入 {directory}，再发送“数据更新了”。")
    with TemporaryDirectory(dir=data_dir) as staging:
        files = [Path(shutil.copyfile(directory / name, Path(staging) / name)) for name in sorted(before)]
        if _delivery_files(directory) != before:
            raise ValueError("交付文件仍在写入，请在整套文件交付完成后再同步。")
        hashes = {}
        for path in files:
            with path.open("rb") as handle:
                hashes[path.name] = hashlib.file_digest(handle, "sha256").hexdigest()
        previous = {path.name: s["sha256"] for s in head["sources"]
                    if (path := source_path(s, data_dir)).suffix.lower() in {".csv", ".xlsx"}}
        if hashes == previous:
            return head
        dataset_id = "upload_" + uuid4().hex[:12]
        if allow_target and not allow_target({"dataset_id": dataset_id, "delivery_source_id": feed}):
            raise DeliveryAccessError("目标快照尚未授权，请联系管理员")
        mapping = _delivery_mapping(files, head, data_dir)
        label = head.get("business_label") or head["name"]
        return import_upload(files, mapping, f"{label} · 交付 {digest(hashes)[:8]}", data_dir,
                             dataset_id=dataset_id, business_label=label, parent_dataset_id=head["dataset_id"], delivery_source_id=feed,
                             quality_rule_version=CalculationContext.from_manifest(head).rule_version, change_note="从交付目录同步")


def refresh_delivery(agent, session, question, *, allow_target=None):
    source = agent.executors[session.dataset_id]
    data_dir = source.path.parent
    directory = delivery_directory(source.manifest, data_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        feed = directory.name
        with FileLock(data_dir / f".delivery-{feed}.lock"):
            target = _published_delivery(source.manifest, directory, data_dir, allow_target)
    except DeliveryAccessError:
        raise
    except (OSError, ValueError, csv.Error, BadZipFile, DecimalException, duckdb.Error) as error:
        text = f"未完成同步，当前仍使用 {session.dataset_id}。{error}"
    else:
        key = target["dataset_id"]
        if key == session.dataset_id:
            text = f"交付文件与当前快照 {key} 一致，已沿用该版本。"
        else:
            if key not in agent.executors:
                agent.executors[key] = Executor(key, data_dir)
            if session.working_plan or session.last_successful_plan:
                try:
                    fresh, turn = agent.replay(session, key, question=question)
                except PlanError as error:
                    session = Session(key)
                    text = f"已切换到新版 {key}。原分析条件需要调整：{error}。请在新版中指定要分析的范围。"
                else:
                    return fresh, turn.text
            else:
                session = Session(key)
                text = f"已切换到新版 {target['name']}（{key}）。请输入要分析的问题。"
    session.remember(question, text)
    agent.checkpoint(session)
    return session, text
