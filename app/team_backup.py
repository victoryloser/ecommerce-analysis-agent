"""Back up and restore a stopped customer instance with a verified file manifest."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from filelock import Timeout

from app.config import INSTANCE_DIR, ROOT
from app.snapshots import file_sha256, implementation_hash, legacy_source_path, source_path
from app.team_jobs import process_lock

ROOTS = {"data", "runs"}
MANIFEST = "INSTANCE.json"


def included(name):
    path = PurePosixPath(name)
    return (len(path.parts) >= 2 and path.parts[0] in ROOTS and ".." not in path.parts
            and "\\" not in name and ":" not in name and not any(part.startswith(".env") for part in path.parts)
            and not path.name.endswith((".lock", ".tmp", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal", ".duckdb.wal")))


def sqlite_copy(source, target):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db, closing(sqlite3.connect(target)) as copy:
        db.backup(copy)


def archive_file(archive, path, name):
    fingerprint = hashlib.sha256()
    size = 0
    with path.open("rb") as reader, archive.open(name, "w", force_zip64=True) as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
            fingerprint.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": fingerprint.hexdigest()}


def check_snapshots(directory, files, archive=None):
    for published in directory.glob("*.manifest.json"):
        snapshot = json.loads(published.read_text(encoding="utf-8"))
        key = snapshot["dataset_id"]
        expected = [(f"data/{key}.duckdb", snapshot["database_sha256"])]
        for entry in snapshot["sources"]:
            if "retained_file" in entry:
                name = "data/" + PureWindowsPath(entry["retained_file"]).as_posix()
            else:
                original = PureWindowsPath(entry["source_file"])
                name = "data/" + legacy_source_path(entry).as_posix()
                if name not in files and original.parts[:1] == ("data",) and original.as_posix() in files:
                    name = original.as_posix()
                if archive is not None and name not in files:
                    source = source_path(entry, directory).resolve()
                    if (not original.anchor and ".." not in original.parts
                            and (source.is_relative_to(ROOT) or source.is_relative_to(directory))
                            and (source.suffix.lower() in {".csv", ".xlsx"} or source.name == "mapping.json")
                            and source.is_file()):
                        files[name] = archive_file(archive, source, name)
            expected.append((name, entry["sha256"]))
        for name, fingerprint in expected:
            if files.get(name, {}).get("sha256") != fingerprint:
                raise ValueError(f"快照 {key} 的必要文件缺失或指纹不符：{name}")


def backup(instance, target):
    instance, target = instance.resolve(), target.resolve()
    directory = instance / "data"
    if not (directory / "team.sqlite3").is_file():
        raise ValueError("目标目录中没有客户实例数据库")
    if any(target.is_relative_to(instance / root) for root in ROOTS):
        raise ValueError("请将备份保存到 data 和 runs 目录之外")
    target.parent.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).isoformat()
    manifest = {"format": 1, "created_at": created, "instance": str(instance),
                "release": version("ecommerce-analysis-agent"), "implementation_sha256": implementation_hash(), "files": {}}
    with process_lock(directory, "web"), process_lock(directory, "worker"), process_lock(directory, "maintenance"):
        with TemporaryDirectory(dir=target.parent) as temporary:
            staging = Path(temporary)
            archive_path = staging / target.name
            with ZipFile(archive_path, "w", ZIP_DEFLATED, compresslevel=3) as archive:
                for root in sorted(ROOTS):
                    for source in sorted((instance / root).rglob("*")):
                        name = source.relative_to(instance).as_posix()
                        if not included(name) or not source.is_file():
                            continue
                        if source.resolve() != source:
                            raise ValueError(f"请先将链接文件实体化后再备份：{name}")
                        path = source
                        if source.suffix == ".sqlite3":
                            path = staging / "sqlite-backup"
                            sqlite_copy(source, path)
                        manifest["files"][name] = archive_file(archive, path, name)
                        if path != source:
                            path.unlink()
                check_snapshots(directory, manifest["files"], archive)
                archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
            with ZipFile(archive_path) as archive:
                if invalid := archive.testzip():
                    raise ValueError(f"无法验证备份文件：{invalid}")
            # An existing recovery point keeps its original contents.
            os.link(archive_path, target)
    return {"archive": str(target), "sha256": file_sha256(target), "created_at": created, "files": len(manifest["files"])}


def restore(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError("恢复目录必须是尚未创建的新目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as temporary, ZipFile(source) as archive:
        staging = Path(temporary) / "instance"
        manifest = json.loads(archive.read(MANIFEST))
        files = manifest["files"]
        names = archive.namelist()
        if (manifest["format"] != 1 or "data/team.sqlite3" not in files or len(names) != len(set(names))
                or set(names) != set(files) | {MANIFEST}):
            raise ValueError("备份文件与清单不匹配")
        for name, expected in files.items():
            if not included(name):
                raise ValueError(f"备份包含无效路径：{name}")
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as reader, path.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            if path.stat().st_size != expected["bytes"] or file_sha256(path) != expected["sha256"]:
                raise ValueError(f"备份文件指纹不匹配：{name}")
            if path.suffix == ".sqlite3":
                with closing(sqlite3.connect(path)) as db:
                    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                        raise ValueError(f"备份数据库检查失败：{name}")
        check_snapshots(staging / "data", files)
        rebase_downloads(staging, manifest["instance"])
        staging.rename(destination)
    return {"instance": str(destination), "created_at": manifest["created_at"],
            "release": manifest["release"], "implementation_sha256": manifest["implementation_sha256"], "files": len(files)}


def rebase_downloads(staging, previous):
    path_type = PureWindowsPath if PureWindowsPath(previous).drive else PurePosixPath
    root = path_type(previous)
    with closing(sqlite3.connect(staging / "data/team.sqlite3")) as db, db:
        for key, payload in db.execute("SELECT id,payload FROM resources WHERE kind='file'").fetchall():
            path = path_type(json.loads(payload))
            if path.is_absolute() and path.is_relative_to(root):
                relative = path.relative_to(root).as_posix()
                db.execute("UPDATE resources SET payload=? WHERE id=?", (json.dumps(relative), key))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    pack = sub.add_parser("backup")
    pack.add_argument("--instance", type=Path, default=INSTANCE_DIR)
    pack.add_argument("--output", type=Path, required=True)
    unpack = sub.add_parser("restore")
    unpack.add_argument("--archive", type=Path, required=True)
    unpack.add_argument("--instance", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = backup(args.instance, args.output) if args.operation == "backup" else restore(args.archive, args.instance)
    except Timeout as error:
        raise SystemExit("请先停止实例网页、后台工作进程及实例维护命令。") from error
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
