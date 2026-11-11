"""Exercise task preparation using a local disposable Git repository only."""

import io
import json
import subprocess
from pathlib import Path

import pytest

from scripts import prepare_marshmallow_1359 as prep


def git(cwd, *args):
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def local_source(tmp_path, monkeypatch):
    source = tmp_path / "remote"
    (source / "src" / "marshmallow").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "marshmallow" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (source / "src" / "marshmallow" / "nested.py").write_text("y = 2\n", encoding="utf-8")
    (source / "tests" / "test_existing.py").write_text("def test_old(): pass\n", encoding="utf-8")
    git(source, "init", "--quiet")
    git(source, "add", ".")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    commit = git(source, "rev-parse", "HEAD")
    repro = tmp_path / "repro.py"
    repro.write_text("def test_repro(): pass\n", encoding="utf-8")
    monkeypatch.setattr(prep, "COMMIT", commit)
    monkeypatch.setattr(prep, "REPOSITORY", str(source))
    monkeypatch.setattr(prep, "REPRO", repro)
    response = {"number": 1357, "title": "Public title", "body": "Public issue body"}
    monkeypatch.setattr(prep.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(json.dumps(response).encode()))
    return source, tmp_path / "prepared"


def test_prepare_is_repeatable_and_whitelists_only_source(local_source):
    _source, root = local_source
    task_path = prep.prepare(root)
    task = json.loads(task_path.read_text(encoding="utf-8"))
    assert task["issue"] == "Public title\n\nPublic issue body"
    assert task["editable_files"] == ["src/marshmallow/__init__.py", "src/marshmallow/nested.py"]
    assert prep.git(root / "baseline", "status", "--porcelain") == ""
    assert prep.git(root / "repo", "status", "--porcelain") == "?? tests/test_evocode_issue_1357.py"
    assert prep.prepare(root) == task_path


@pytest.mark.parametrize("relative", ["baseline/src/marshmallow/nested.py", "repo/tests/test_evocode_issue_1357.py", "task.json"])
def test_prepare_never_overwrites_existing_modifications(local_source, relative):
    _source, root = local_source
    prep.prepare(root)
    path = root / Path(relative)
    path.write_text("user modification", encoding="utf-8")
    with pytest.raises((RuntimeError, ValueError, json.JSONDecodeError)):
        prep.prepare(root)
    assert path.read_text(encoding="utf-8") == "user modification"
