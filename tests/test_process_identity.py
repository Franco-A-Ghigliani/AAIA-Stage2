"""Preserve process probing semantics without signalling the test runner itself."""

import pytest

from harness import application


@pytest.mark.parametrize(
    "failure, expected",
    [(None, True), (ProcessLookupError(), False), (PermissionError(), True)],
)
def test_process_probe_classifies_existing_error_types(monkeypatch, failure, expected):
    calls = []

    def signal_process(pid, signal):
        calls.append((pid, signal))
        if failure is not None:
            raise failure

    monkeypatch.setattr(application.os, "kill", signal_process)
    assert application._process_exists(42) is expected
    assert calls == [(42, 0)]
    assert application._process_exists(None) is False
    assert application._process_exists(0) is False
    assert calls == [(42, 0)]


def test_other_process_probe_errors_still_propagate(monkeypatch):
    def signal_process(_pid, _signal):
        raise OSError("platform-specific probe failure")

    monkeypatch.setattr(application.os, "kill", signal_process)
    with pytest.raises(OSError, match="platform-specific probe failure"):
        application._process_exists(42)
