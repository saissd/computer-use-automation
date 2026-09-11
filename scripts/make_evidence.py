"""Generate the curated evidence set committed under /evidence.

Produces one run per outcome class so a reviewer can see, side by side, how
the same capability behaves under success, a business outcome, a recovered
condition, a hard failure, and an escalation that a human resolves.

    python scripts/make_evidence.py

The discovery run is NOT produced here — it needs a model API key. Run:

    cu discover --goal "..." --entry http://127.0.0.1:8099

and its evidence lands in the same directory.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cua.catalog.catalog import Catalog  # noqa: E402
from cua.evidence.writer import EvidenceWriter  # noqa: E402
from cua.policy.engine import PolicyConfig, PolicyEngine  # noqa: E402
from cua.replay.engine import ReplayEngine  # noqa: E402
from cua.session.intervention import InterventionQueue  # noqa: E402
from cua.session.browser import BrowserSession  # noqa: E402

BASE = "http://127.0.0.1:8099"
USER, PASSWORD = "teller01", "training-only"
EVIDENCE = ROOT / "evidence"


def arm(mode: str) -> None:
    httpx.post(f"{BASE}/_control/inject", data={"mode": mode}, timeout=5)


def reset() -> None:
    httpx.post(f"{BASE}/_control/reset", timeout=5)


async def one_run(name: str, member: str, *, inject=None, reauth=True, capability=None):
    reset()
    cat = Catalog(ROOT / "capabilities")
    cap = capability or cat.get("lookup_member_balance")
    ev = EvidenceWriter(name, EVIDENCE)
    policy = PolicyEngine(PolicyConfig.load(ROOT / "config" / "policy.yaml"))

    session = BrowserSession(headless=True, evidence_dir=ev.dir)
    await session.start(trace=True)
    try:
        await session.login(BASE, USER, PASSWORD)
        if inject:
            arm(inject)
        engine = ReplayEngine(
            session.surface, policy, session.lease, ev,
            reauth=session.reauth_hook(BASE, USER, PASSWORD) if reauth else None,
        )
        result = await engine.run(cap, {"member_id": member})
        print(f"  {name:<34} {result.status:<18} {result.summary()[:70]}")
        return result
    finally:
        await session.close()


async def handoff_run(name: str):
    """Escalation, a simulated operator taking the live session, and resume."""
    reset()
    cat = Catalog(ROOT / "capabilities")
    cap = cat.get("lookup_member_balance")
    ev = EvidenceWriter(name, EVIDENCE)
    policy = PolicyEngine(PolicyConfig.load(ROOT / "config" / "policy.yaml"))
    queue = InterventionQueue()

    session = BrowserSession(headless=True, evidence_dir=ev.dir)
    await session.start(trace=True)
    try:
        await session.login(BASE, USER, PASSWORD)
        arm("timeout")
        engine = ReplayEngine(
            session.surface, policy, session.lease, ev,
            queue=queue, escalation_mode="wait", escalation_timeout_s=120,
            reauth=None,  # unattended worker has no credentials for this tenant
        )
        run = asyncio.create_task(engine.run(cap, {"member_id": "12345"}))

        req = None
        for _ in range(200):
            open_reqs = queue.list(open_only=True)
            if open_reqs:
                req = open_reqs[0]
                break
            await asyncio.sleep(0.1)
        assert req is not None, "no intervention raised"

        # --- the operator, scripted ---------------------------------------
        queue.take(req.id, "casey.operator")
        session.lease.grant_to_human("casey.operator", note="signing back in by hand")
        page = session.page
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.fill("#ctl00_txtUserId", USER)
        await page.fill("#ctl00_txtPasswd", PASSWORD)
        await page.click("input[value='Sign On']")
        await page.wait_for_load_state("domcontentloaded")
        session.lease.return_to_automation(note="signed in manually, resuming")
        queue.resolve(req.id, "resume")

        result = await run
        ev.log("lease_history", history=session.lease.snapshot()["history"])
        ev.log(
            "intervention_final",
            **queue.get(req.id).model_dump(mode="json"),
        )
        print(f"  {name:<34} {result.status:<18} {result.summary()[:70]}")
        return result
    finally:
        await session.close()


async def main():
    if EVIDENCE.exists():
        for child in EVIDENCE.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    EVIDENCE.mkdir(exist_ok=True)

    print("\ngenerating replay evidence\n" + "-" * 78)

    await one_run("replay_01_success", "12345")
    await one_run("replay_02_success_other_member", "23456")
    await one_run("replay_03_business_outcome_not_found", "99999")
    await one_run("replay_04_business_outcome_denied", "55555")
    await one_run("replay_05_recovered_interstitial", "12345", inject="dialog")
    await one_run("replay_06_recovered_slow_load", "12345", inject="slow")
    await one_run("replay_07_recovered_session_reauth", "12345", inject="timeout")
    await one_run("replay_08_hard_failure_app_error", "12345", inject="error500")
    await one_run("replay_09_invalid_input", "not-a-member")
    await handoff_run("replay_10_human_handoff")

    print("-" * 78)
    print(f"\nwrote {len(list(EVIDENCE.iterdir()))} run directories to {EVIDENCE}")


if __name__ == "__main__":
    asyncio.run(main())
