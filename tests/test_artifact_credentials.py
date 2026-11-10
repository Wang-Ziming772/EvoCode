"""Regression checks for credential handling in raw patches and log text."""

import pytest

from evocode.artifacts import ArtifactError, Redactor, RunArtifacts


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.delenv("EVOCODE_GPT_API_KEY", raising=False)
    monkeypatch.delenv("EVOCODE_DEEPSEEK_API_KEY", raising=False)
    env = tmp_path / "test.env"
    env.write_text("EVOCODE_GPT_API_KEY=known-dummy-key\n", encoding="utf-8")
    writer = RunArtifacts(tmp_path / "runs", Redactor(env))
    yield writer
    writer.close()


@pytest.mark.parametrize("expression", [
    'headers = {"Authorization": f"Bearer {token}"}',
    "headers = {'Authorization': f'{scheme} {token}'}",
    'headers = {"Authorization": "Bearer " + token}',
    'headers = {"Authorization": "Custom-" + get_token()}',
    'headers = {"Authorization": "Custom-" + f"{token}"}',
    'headers = {"Authorization": "Bearer " + "prefix-" + token}',
    'headers = {"Authorization": "Custom-" + "literal-" + f"{token}"}',
    'headers = {"Authorization": token}',
    'headers = {"Authorization": "Bearer {}".format(token)}',
    'headers = {"Authorization": "Bearer {}".format (token)}',
    'headers = {"Authorization": "Bearer %s" % token}',
    'const headers = {Authorization: `Bearer ${token}`}',
])
def test_dynamic_authorization_can_be_saved_verbatim(artifacts, expression):
    patch = f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+{expression}\n"
    artifacts.write_patch(patch)
    assert artifacts.patch_path.read_bytes() == patch.encode("utf-8")


@pytest.mark.parametrize("expression", [
    'headers = {"Authorization": "Bearer hardcoded-secret"}',
    'headers = {"Authorization": "Bearer hardcoded-secret" + suffix}',
    'headers = {"Authorization": "Custom-" + "hardcoded-secret"}',
    'headers = {"Authorization": "Bearer " + "hardcoded-secret"}',
    'headers = {"Authorization": "Bearer " + "prefix-" + "hardcoded-secret"}',
    "headers = {'Authorization': 'opaque-secret'}",
    "Authorization: Bearer raw-hardcoded-secret",
    "# known-dummy-key",
])
def test_literal_authorization_and_known_keys_are_not_saved(artifacts, expression):
    patch = f"diff --git a/app.py b/app.py\n+{expression}\n"
    with pytest.raises(ArtifactError, match="凭据"):
        artifacts.write_patch(patch)
    assert artifacts.patch_path.read_bytes() == b""


def test_log_redaction_remains_conservative(artifacts):
    text = 'Authorization: Bearer runtime-token and known-dummy-key'
    redacted = artifacts.redactor.text(text)
    assert "runtime-token" not in redacted and "known-dummy-key" not in redacted
