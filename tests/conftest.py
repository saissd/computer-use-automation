"""Shared fixtures.

The suite runs against the real target app in a real browser. That is
deliberate: the interesting claims in this system ("ambiguous descriptors are
refused", "a session timeout is re-authenticated, not blindly retried") are
claims about behaviour under a real surface, and a mocked surface would let
every one of them pass vacuously.

No test in this suite calls an LLM. Discovery is exercised separately and its
evidence is committed; everything here is the deterministic path, so CI needs
no API key and costs nothing to run.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8099"
APP_USER = "teller01"
APP_PASSWORD = "training-only"


def _port_open(host: str = "127.0.0.1", port: int = 8099) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def app_server():
    """Start the target app unless one is already listening."""
    if _port_open():
        yield BASE_URL
        return

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "apps.legacy_cu.app:app",
         "--port", "8099", "--log-level", "warning"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if _port_open():
            break
        time.sleep(0.1)
    else:  # pragma: no cover
        proc.terminate()
        pytest.fail("target app did not start on port 8099")

    yield BASE_URL
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def reset_app(app_server):
    """Clear injected conditions and mutated data between tests."""
    httpx.post(f"{app_server}/_control/reset", timeout=5)
    yield


@pytest.fixture
def arm(app_server):
    """Arm a one-shot error injection for the next content request."""

    def _arm(mode: str) -> None:
        r = httpx.post(f"{app_server}/_control/inject", data={"mode": mode}, timeout=5)
        r.raise_for_status()

    return _arm


@pytest.fixture
def capability():
    from cua.catalog.catalog import Catalog

    return Catalog(ROOT / "capabilities").get("lookup_member_balance")


@pytest.fixture
def policy():
    from cua.policy.engine import PolicyConfig, PolicyEngine

    return PolicyEngine(PolicyConfig.load(ROOT / "config" / "policy.yaml"))


@pytest.fixture
async def session(tmp_path):
    """A live browser session, logged in, with its own evidence directory."""
    from cua.session.browser import BrowserSession

    sess = BrowserSession(headless=True, evidence_dir=tmp_path)
    await sess.start()
    try:
        assert await sess.login(BASE_URL, APP_USER, APP_PASSWORD)
        yield sess
    finally:
        await sess.close()


@pytest.fixture
def replay(session, policy, tmp_path):
    """Run a capability against the live session and return the result."""
    from cua.evidence.writer import EvidenceWriter
    from cua.replay.engine import ReplayEngine, new_run_id

    async def _replay(capability, inputs, *, approved=False, escalation_mode="return",
                      queue=None, escalation_timeout_s=5.0, reauth=True):
        ev = EvidenceWriter(new_run_id("test"), tmp_path)
        engine = ReplayEngine(
            session.surface,
            policy,
            session.lease,
            ev,
            queue=queue,
            escalation_mode=escalation_mode,
            escalation_timeout_s=escalation_timeout_s,
            # `reauth=False` models an unattended worker with no credentials
            # for this tenant: the condition is real and only a human can clear it.
            reauth=session.reauth_hook(BASE_URL, APP_USER, APP_PASSWORD) if reauth else None,
        )
        return await engine.run(capability, inputs, approved=approved)

    return _replay


@pytest.fixture
def goto():
    """Navigate to the console and wait for the frameset's child frames.

    A frameset's children are separate documents that load after
    `domcontentloaded` on the parent. Tests that drive the page directly have
    to wait for them; the replay engine gets this for free because every step
    carries an explicit condition to wait on.
    """

    async def _goto(session, url=BASE_URL + "/", frame_name="content"):
        await session.surface.act("navigate", None, url=url)
        for _ in range(50):
            frame = session.page.frame(name=frame_name)
            if frame is not None and frame.url and "about:blank" not in frame.url:
                return frame
            await session.page.wait_for_timeout(100)
        return session.page.frame(name=frame_name)

    return _goto
