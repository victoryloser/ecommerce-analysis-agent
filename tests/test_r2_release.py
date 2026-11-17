import json
from pathlib import Path
import subprocess
from zipfile import ZipFile

import pytest

from scripts.build_release import allowed, build


@pytest.mark.parametrize("name", ["AGENTS.md", "docs/R2深化优化方案.md", "docs/P2深化设计.md", "data/x.csv", "app/.env", "runs/log.json", "semi-material-2/data.xlsx", "../app/x.py", "app/../../x.py", "app\\x.py"])
def test_exclusions(name):
    assert not allowed(name)


def test_release_uses_commit_bytes_and_is_reproducible(tmp_path):
    root = tmp_path/"repo"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)
    git("init")
    for name, text in {"pyproject.toml": '[project]\nname="test"\nversion="1"', "app/main.py": "original = True\n",
                       "docs/R2深化优化方案.md": "local plan", "AGENTS.md": "private"}.items():
        path = root/name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    committed = git("show", "HEAD:app/main.py")
    (root/"app/main.py").write_text("uncommitted = True\n")
    first, second = tmp_path/"one.zip", tmp_path/"two.zip"
    build(root, "HEAD", first)
    build(root, "HEAD", second)
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        assert archive.read("app/main.py") == committed
        assert "AGENTS.md" not in archive.namelist()
        assert not any("方案" in name for name in archive.namelist())
        assert json.loads(archive.read("MANIFEST.json"))["build_mode"] == "git-commit"
