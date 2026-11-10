import os
from pathlib import Path

import pytest

from evocode.models import ConfigError, load_model_config


KEYS = (
    "EVOCODE_DEFAULT_PROFILE", "EVOCODE_BASE_URL", "EVOCODE_GPT_API_KEY",
    "EVOCODE_GPT_MODEL_ID", "EVOCODE_GPT_MAX_OUTPUT_TOKENS", "EVOCODE_GPT_REASONING_EFFORT",
    "EVOCODE_DEEPSEEK_API_KEY", "EVOCODE_DEEPSEEK_MODEL_ID",
    "EVOCODE_DEEPSEEK_MAX_OUTPUT_TOKENS", "EVOCODE_DEEPSEEK_REASONING_EFFORT",
    "EVOCODE_REQUEST_TIMEOUT_SECONDS", "EVOCODE_MAX_RETRIES",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "test.env"
    path.write_text(
        "EVOCODE_DEFAULT_PROFILE=gpt\n"
        "EVOCODE_BASE_URL=https://gateway.example/v1\n"
        "EVOCODE_GPT_API_KEY=dummy-gpt-secret\n"
        "EVOCODE_GPT_MODEL_ID=gateway-gpt\n"
        "EVOCODE_GPT_MAX_OUTPUT_TOKENS=128000\n"
        "EVOCODE_GPT_REASONING_EFFORT=medium\n"
        "EVOCODE_DEEPSEEK_API_KEY=dummy-deepseek-secret\n"
        "EVOCODE_DEEPSEEK_MODEL_ID=gateway-deepseek\n"
        "EVOCODE_DEEPSEEK_MAX_OUTPUT_TOKENS=393216\n"
        "EVOCODE_DEEPSEEK_REASONING_EFFORT=high\n"
        "EVOCODE_REQUEST_TIMEOUT_SECONDS=60\n"
        "EVOCODE_MAX_RETRIES=0\n", encoding="utf-8",
    )
    return path


def test_profile_selection_and_environment_priority(config_file, monkeypatch):
    before = os.environ.copy()
    gpt = load_model_config(env_file=config_file)
    assert gpt.profile == "gpt" and gpt.model_id == "gateway-gpt"
    assert gpt.max_output_tokens == 128000 and gpt.reasoning_effort == "medium"
    deepseek = load_model_config("deepseek", config_file)
    assert deepseek.profile == "deepseek" and deepseek.model_id == "gateway-deepseek"
    assert deepseek.max_output_tokens == 393216 and deepseek.reasoning_effort == "high"
    assert "dummy-gpt-secret" not in repr(gpt) and "dummy-deepseek-secret" not in repr(deepseek)
    assert os.environ == before

    monkeypatch.setenv("EVOCODE_DEFAULT_PROFILE", "deepseek")
    monkeypatch.setenv("EVOCODE_DEEPSEEK_MODEL_ID", "environment-model")
    monkeypatch.setenv("EVOCODE_DEEPSEEK_MAX_OUTPUT_TOKENS", "321")
    overridden = load_model_config(env_file=config_file)
    assert overridden.profile == "deepseek" and overridden.model_id == "environment-model"
    assert overridden.max_output_tokens == 321 and overridden.reasoning_effort == "high"
    assert load_model_config("gpt", config_file).profile == "gpt"


def test_only_selected_profile_is_validated(config_file, monkeypatch):
    monkeypatch.setenv("EVOCODE_DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("EVOCODE_DEEPSEEK_REASONING_EFFORT", "not-supported")
    assert load_model_config("gpt", config_file).profile == "gpt"
    with pytest.raises(ConfigError, match="EVOCODE_DEEPSEEK_API_KEY"):
        load_model_config("deepseek", config_file)


@pytest.mark.parametrize("name", ["EVOCODE_BASE_URL", "EVOCODE_GPT_API_KEY", "EVOCODE_GPT_MODEL_ID"])
def test_missing_required_selected_fields(config_file, monkeypatch, name):
    monkeypatch.setenv(name, "")
    with pytest.raises(ConfigError, match=name):
        load_model_config("gpt", config_file)


@pytest.mark.parametrize(("name", "value"), [
    ("EVOCODE_REQUEST_TIMEOUT_SECONDS", "0"), ("EVOCODE_REQUEST_TIMEOUT_SECONDS", "nan"),
    ("EVOCODE_REQUEST_TIMEOUT_SECONDS", "inf"), ("EVOCODE_REQUEST_TIMEOUT_SECONDS", "-1"),
    ("EVOCODE_GPT_MAX_OUTPUT_TOKENS", "0"), ("EVOCODE_GPT_MAX_OUTPUT_TOKENS", "1.5"),
    ("EVOCODE_MAX_RETRIES", "-1"), ("EVOCODE_MAX_RETRIES", "1.0"),
    ("EVOCODE_GPT_REASONING_EFFORT", "none"), ("EVOCODE_GPT_REASONING_EFFORT", "low "),
])
def test_invalid_values_do_not_leak(config_file, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name) as error:
        load_model_config("gpt", config_file)
    assert "dummy-gpt-secret" not in str(error.value)
    assert "dummy-deepseek-secret" not in str(error.value)


@pytest.mark.parametrize("effort", ["none", "low", "high", "max"])
def test_deepseek_effort_choices(config_file, monkeypatch, effort):
    monkeypatch.setenv("EVOCODE_DEEPSEEK_REASONING_EFFORT", effort)
    assert load_model_config("deepseek", config_file).reasoning_effort == effort


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_gpt_effort_choices(config_file, monkeypatch, effort):
    monkeypatch.setenv("EVOCODE_GPT_REASONING_EFFORT", effort)
    assert load_model_config("gpt", config_file).reasoning_effort == effort


@pytest.mark.parametrize("effort", ["medium", "xhigh"])
def test_deepseek_rejects_gpt_only_efforts(config_file, monkeypatch, effort):
    monkeypatch.setenv("EVOCODE_DEEPSEEK_REASONING_EFFORT", effort)
    with pytest.raises(ConfigError, match="EVOCODE_DEEPSEEK_REASONING_EFFORT"):
        load_model_config("deepseek", config_file)


def test_deepseek_output_limit_is_validated_independently(config_file, monkeypatch):
    monkeypatch.setenv("EVOCODE_DEEPSEEK_MAX_OUTPUT_TOKENS", "0")
    assert load_model_config("gpt", config_file).max_output_tokens == 128000
    with pytest.raises(ConfigError, match="EVOCODE_DEEPSEEK_MAX_OUTPUT_TOKENS"):
        load_model_config("deepseek", config_file)


def test_missing_numeric_and_unknown_profile_are_errors(config_file, monkeypatch):
    monkeypatch.setenv("EVOCODE_GPT_MAX_OUTPUT_TOKENS", "")
    with pytest.raises(ConfigError, match="EVOCODE_GPT_MAX_OUTPUT_TOKENS"):
        load_model_config("gpt", config_file)
    with pytest.raises(ConfigError, match="gpt 或 deepseek"):
        load_model_config("dummy-gpt-secret", config_file)
