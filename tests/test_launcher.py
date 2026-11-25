import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import launcher


@pytest.mark.parametrize("missing", ["REPO_URL", "REPO_BRANCH", "EXPECTED_DB"])
@pytest.mark.parametrize("value", [None, ""])          # GitHub passes an unset variable as ""
def test_refuses_to_run_without_repo_variables(monkeypatch, missing, value):
    sent = []
    monkeypatch.setattr(launcher.notify, "send", sent.append)
    monkeypatch.setattr(launcher, "Runpod", lambda key: pytest.fail("Runpod must not be contacted"))
    monkeypatch.setenv("REPO_URL", "https://github.com/x/AIENGINE-TEST.git")
    monkeypatch.setenv("REPO_BRANCH", "Sept_Run")
    monkeypatch.setenv("EXPECTED_DB", "fake_db")
    if value is None:
        monkeypatch.delenv(missing)
    else:
        monkeypatch.setenv(missing, value)
    assert launcher.main([]) == 1
    assert len(sent) == 1 and missing in sent[0] and "variable is not set" in sent[0]


def test_repo_label(monkeypatch):
    monkeypatch.setenv("REPO_URL", "https://github.com/x/AIENGINE-TEST.git")
    monkeypatch.setenv("REPO_BRANCH", "Sept_Run")
    assert launcher.repo_label() == "AIENGINE-TEST @ Sept_Run"
