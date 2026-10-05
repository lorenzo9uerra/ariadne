"""The target service must accept connections before the agent starts."""

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from sandbox import checks as verify


class FakeAgent:
    """Answers connection probes from a scripted list of outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.commands = []

    async def exec(self, command, **kwargs):
        self.commands.append(command)
        outcome = self.outcomes.pop(0) if self.outcomes else False
        if outcome == "timeout":
            raise TimeoutError
        return SimpleNamespace(return_code=0 if outcome else 1)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def sleep(seconds):
        return None

    monkeypatch.setattr(verify.asyncio, "sleep", sleep)


def test_service_that_starts_late_is_awaited():
    agent = FakeAgent([False, "timeout", True])
    asyncio.run(verify.wait_for_service(agent, "target-id", 4010))
    assert len(agent.commands) == 3
    assert agent.commands[0] == "bash -c 'exec 3<>/dev/tcp/target/4010'"


def test_service_that_never_starts_fails_with_target_logs(monkeypatch):
    monkeypatch.setattr(verify, "SERVICE_READY_SECONDS", 0)
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, "", "listener exited: permission denied"
        ),
    )
    with pytest.raises(RuntimeError, match="(?s)port 4010.*permission denied"):
        asyncio.run(verify.wait_for_service(FakeAgent([False]), "target-id", 4010))


@pytest.mark.parametrize("port", [0, 70000, "4010", 4010.0])
def test_invalid_ports_are_rejected(port):
    with pytest.raises(ValueError):
        asyncio.run(verify.wait_for_service(FakeAgent([True]), "target-id", port))
