"""Validate patch auditing offline without running the modified project on the host."""

import json
from types import SimpleNamespace

from scripts import prepare_marshmallow_1359 as prep
from scripts import verify_marshmallow_1359 as verifier
from test_prepare_marshmallow import local_source

from evocode.tasks.workspace import Workspace


class ContentExecutor:
    def __init__(self, *, image, timeout_seconds, max_output_bytes=65536, pytest_targets=()):
        self.targets = pytest_targets

    def check_available(self):
        return "sha256:" + "a" * 64

    def __call__(self, path):
        fixed = "y = 3" in (path / "src/marshmallow/nested.py").read_text(encoding="utf-8")
        reproduction = self.targets == ("tests/test_evocode_issue_1357.py",)
        if reproduction and not fixed:
            output = ("AttributeError: 'List' object has no attribute 'opts'\n"
                      "AttributeError: 'Tuple' object has no attribute 'opts'\n2 failed\n")
            code = 1
        elif reproduction:
            output, code = "2 passed\n", 0
        elif self.targets or not (path / prep.TEST_REL).exists():
            output, code = "2 passed\n", 0
        else:
            output, code = "4 passed\n", 0
        evidence = (verifier.EXPECTED_IMPORT + "\nEVOCODE_PYTHON_VERSION=3.7.17\n"
                    "EVOCODE_PYTEST_VERSION=7.4.4\n")
        return SimpleNamespace(stdout=evidence + output, stderr="", returncode=code, output_truncated=False)


def make_patch(root, path, new_content, output):
    with Workspace(root / "repo", [path]) as workspace:
        workspace.write_file(path, new_content)
        output.write_bytes(workspace.export_patch().encode("utf-8", errors="surrogateescape"))


def test_independent_verifier_applies_saved_patch_without_modifying_baseline(local_source, tmp_path, monkeypatch):
    _source, root = local_source
    prep.prepare(root)
    patch = tmp_path / "change.diff"
    make_patch(root, "src/marshmallow/nested.py", "y = 3\n", patch)
    monkeypatch.setattr(verifier, "check_existing", prep.check_existing)
    monkeypatch.setattr(verifier, "COMMIT", prep.COMMIT)
    monkeypatch.setattr(verifier, "DockerTestExecutor", ContentExecutor)
    output, report = verifier.verify(root, "fake", patch, tmp_path / "evidence")
    assert report["status"] == "passed" and report["modified_files"] == ["src/marshmallow/nested.py"]
    assert report["repro_before"]["counts"]["failed"] == 2
    assert report["original_after"]["counts"]["passed"] == 2
    assert report["repro_after"]["counts"]["passed"] == 2
    assert json.loads((output / "verification.json").read_text())["status"] == "passed"
    assert "y = 2" in (root / "baseline/src/marshmallow/nested.py").read_text()
    assert "y = 2" in (root / "repo/src/marshmallow/nested.py").read_text()


def test_verifier_rejects_patch_outside_source_allowlist(local_source, tmp_path, monkeypatch):
    _source, root = local_source
    prep.prepare(root)
    patch = tmp_path / "test-change.diff"
    make_patch(root, "tests/test_existing.py", "def test_changed(): pass\n", patch)
    monkeypatch.setattr(verifier, "check_existing", prep.check_existing)
    monkeypatch.setattr(verifier, "COMMIT", prep.COMMIT)
    monkeypatch.setattr(verifier, "DockerTestExecutor", ContentExecutor)
    _output, report = verifier.verify(root, "fake", patch, tmp_path / "evidence")
    assert report["status"] == "failed" and "白名单" in report["reason"]
    assert "test_old" in (root / "repo/tests/test_existing.py").read_text()
