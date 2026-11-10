"""Run artifacts agree on the final outcome, including recovery paths."""

import pytest

from evocode import runner
from test_repair import fake_backends, read_artifacts, repair_inputs


def _run(repair_inputs, tmp_path):
    _repo, task, env = repair_inputs
    return runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs"))


@pytest.mark.parametrize("failure", [OSError("write failed"), KeyboardInterrupt()])
def test_final_result_write_failure_updates_terminal_event(repair_inputs, fake_backends, tmp_path, monkeypatch, failure):
    original = runner.RunArtifacts.write_result
    calls = 0

    def fail_once(self, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return original(self, value)

    monkeypatch.setattr(runner.RunArtifacts, "write_result", fail_once)
    execution = _run(repair_inputs, tmp_path)
    result, events, patch = read_artifacts(execution)
    expected, code = ("INTERRUPTED", 130) if isinstance(failure, KeyboardInterrupt) else ("FAILED", 2)
    assert execution.status == result["status"] == expected and execution.exit_code == code
    terminal = [event for event in events if event["type"] == "run_finished"]
    assert len(terminal) == 1 and terminal[0]["status"] == expected and terminal[0]["exit_code"] == code
    assert terminal[0]["reason"] == result["reason"] and b"return a + b" in patch


def test_persistent_result_write_failure_still_records_failure_in_trajectory(repair_inputs, fake_backends, tmp_path, monkeypatch):
    original = runner.RunArtifacts.write_result

    def fail_final(self, value):
        if value["status"] != "RUNNING":
            raise OSError("disk unavailable")
        return original(self, value)

    monkeypatch.setattr(runner.RunArtifacts, "write_result", fail_final)
    execution = _run(repair_inputs, tmp_path)
    result, events, patch = read_artifacts(execution)
    assert execution.status == "FAILED" and execution.exit_code == 2
    assert result["status"] == "RUNNING"  # The previously saved file cannot be replaced.
    assert events[-1]["type"] == "run_finished" and events[-1]["status"] == "FAILED"
    assert events[-1]["exit_code"] == 2 and b"return a + b" in patch


def test_interrupt_during_save_failure_recovery_records_interrupt(repair_inputs, fake_backends, tmp_path, monkeypatch):
    original_write = runner.RunArtifacts.write_result
    original_event = runner.RunArtifacts.event
    calls = 0

    def fail_once(self, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("write failed")
        return original_write(self, value)

    def interrupt_error_event(self, event):
        if event["type"] == "artifact_error":
            raise KeyboardInterrupt()
        return original_event(self, event)

    monkeypatch.setattr(runner.RunArtifacts, "write_result", fail_once)
    monkeypatch.setattr(runner.RunArtifacts, "event", interrupt_error_event)
    execution = _run(repair_inputs, tmp_path)
    result, events, _patch = read_artifacts(execution)
    assert execution.exit_code == 130 and result["status"] == "INTERRUPTED"
    assert events[-1]["type"] == "run_finished" and events[-1]["status"] == "INTERRUPTED"


@pytest.mark.parametrize("failure", [OSError("close failed"), KeyboardInterrupt()])
def test_close_failure_corrects_result_before_terminal_event(repair_inputs, fake_backends, tmp_path, monkeypatch, failure):
    original = runner.RunArtifacts.close
    calls = 0

    def fail_once(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return original(self)

    monkeypatch.setattr(runner.RunArtifacts, "close", fail_once)
    execution = _run(repair_inputs, tmp_path)
    result, events, _patch = read_artifacts(execution)
    expected, code = ("INTERRUPTED", 130) if isinstance(failure, KeyboardInterrupt) else ("FAILED", 2)
    assert execution.status == result["status"] == expected and execution.exit_code == code
    terminal = [event for event in events if event["type"] == "run_finished"]
    assert len(terminal) == 1 and terminal[0]["status"] == expected and terminal[0]["exit_code"] == code


@pytest.mark.parametrize("write_first", [False, True])
@pytest.mark.parametrize("failure", [OSError("write failed"), KeyboardInterrupt()])
def test_terminal_append_failure_records_corrected_last_outcome(repair_inputs, fake_backends, tmp_path, monkeypatch, write_first, failure):
    original = runner._append_terminal_event
    calls = 0

    def fail_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            if write_first:
                original(*args)
            raise failure
        return original(*args)

    monkeypatch.setattr(runner, "_append_terminal_event", fail_once)
    execution = _run(repair_inputs, tmp_path)
    result, events, _patch = read_artifacts(execution)
    terminal = [event for event in events if event["type"] == "run_finished"]
    expected, code = ("INTERRUPTED", 130) if isinstance(failure, KeyboardInterrupt) else ("FAILED", 2)
    assert execution.exit_code == code and result["status"] == execution.status == expected
    assert terminal[-1]["status"] == expected and terminal[-1]["exit_code"] == code
    assert terminal[-1]["reason"] == result["reason"]
    assert len(terminal) == (2 if write_first else 1)
