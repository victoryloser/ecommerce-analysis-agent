"""Build reproducible source bytes from an explicit Git commit and product allowlist."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {"README.md", "README.zh-CN.md", "LICENSE", "pyproject.toml", "requirements.lock.txt",
             ".env.example", ".gitignore", ".gitattributes"}
DOC_FILES = {"docs/data-format.md", "docs/development.md", "docs/metrics.md", "docs/round2-evaluation.md",
             "docs/assets/project-banner.svg", "docs/images/analysis.png", "docs/images/first-run.png"}


def allowed(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        return False
    if path.name.lower() in {"agents.md", "agent.md"} or any(w in name for w in ("方案", "深化设计")):
        return False
    return name in TOP_FILES or name in DOC_FILES or (
        path.parts[0] in {"app", "tests", "scripts"} and path.suffix == ".py" and "__pycache__" not in path.parts) or (
        path.parts[:2] == (".github", "workflows") and path.suffix in {".yml", ".yaml"})


def commit_files(root, revision):
    commit = subprocess.check_output(["git", "rev-parse", "--verify", revision+"^{commit}"], cwd=root).decode().strip()
    entries = subprocess.check_output(["git", "ls-tree", "-rz", commit], cwd=root).split(b"\0")
    selected = []
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        name = raw_name.decode("utf-8")
        if mode in {b"100644", b"100755"} and kind == b"blob" and allowed(name):
            selected.append((name, oid))
    selected.sort()
    raw = subprocess.check_output(["git", "cat-file", "--batch"], input=b"\n".join(oid for _, oid in selected)+b"\n", cwd=root)
    files, offset = {}, 0
    for name, oid in selected:
        end = raw.index(b"\n", offset)
        found, kind, size = raw[offset:end].split()
        assert found == oid and kind == b"blob"
        offset = end+1
        files[name] = raw[offset:offset+int(size)]
        offset += int(size)+1
    return commit, files


def build(root, revision, target):
    commit, payloads = commit_files(root, revision)
    project = tomllib.loads(payloads["pyproject.toml"].decode())["project"]
    manifest = {"project": project["name"], "version": project["version"], "commit_sha": commit,
        "build_mode": "git-commit", "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                                                for name, data in payloads.items()}}
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name, payload in [*payloads.items(), ("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode())]:
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
    with ZipFile(target) as archive:
        assert set(archive.namelist()) == set(payloads) | {"MANIFEST.json"}
        assert all(archive.read(name) == data for name, data in payloads.items())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    target.with_suffix(".validation.json").unlink(missing_ok=True)
    return {"archive": str(target), "commit_sha": commit, "files": len(payloads), "sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="发布版本；文件内容直接取自该提交")
    parser.add_argument("--output", type=Path, default=ROOT/"dist/ecommerce-analysis-agent.zip")
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.commit, args.output.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
