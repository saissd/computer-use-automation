"""cu - the command line for the whole system.

    cu serve-app              run the legacy target application
    cu console                run the operator console
    cu discover               LLM discovery run -> a saved capability artifact
    cu replay                 deterministic replay of a saved capability
    cu catalog                what capabilities exist and how an agent calls them
    cu invoke                 call a capability the way an AI agent would
    cu handoff-demo           escalation + human takeover on a live session
    cu stability              replay N times and report a flakiness signal
    cu drift                  which artifacts are quietly decaying
"""

import asyncio
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from cua.catalog.catalog import Catalog
from cua.evidence.writer import EvidenceWriter
from cua.policy.engine import PolicyConfig, PolicyEngine
from cua.policy.redaction import Redactor
from cua.replay.engine import ReplayEngine, new_run_id
from cua.session import registry
from cua.session.browser import BrowserSession

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)
console = Console()

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = "http://127.0.0.1:8099"
DEFAULT_APP_ID = "meridian-core-membersvc"


def _policy(path: Optional[Path] = None) -> PolicyEngine:
    return PolicyEngine(PolicyConfig.load(path or ROOT / "config" / "policy.yaml"))


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"--input expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


def _print_result(result) -> None:
    colour = {
        "success": "green",
        "business_outcome": "yellow",
        "needs_human": "magenta",
        "failed": "red",
    }[result.status]
    console.print()
    console.rule(f"[{colour}]{result.status.upper()}[/{colour}]")

    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("step")
    table.add_column("status")
    table.add_column("tier")
    table.add_column("locator")
    table.add_column("detail")
    for s in result.steps:
        tier = "-" if s.locator_tier is None else (
            "primary" if s.locator_tier == 0 else f"fallback {s.locator_tier}"
        )
        detail = ""
        if s.extracted:
            detail = ", ".join(f"{k}={v!r}" for k, v in s.extracted.items())
        elif s.recoveries_applied:
            detail = "recovered: " + "; ".join(s.recoveries_applied)
        elif s.value_redacted:
            detail = f"typed {s.value_redacted}"
        table.add_row(s.step_id, s.status, tier, s.locator_used or "-", detail)
    console.print(table)

    console.print()
    if result.status == "success":
        console.print("[bold green]outputs[/bold green]")
        for k, v in result.outputs.items():
            console.print(f"  {k} = {v!r}")
    elif result.status == "business_outcome":
        console.print(f"[bold yellow]outcome[/bold yellow]  {result.outcome}")
        console.print(f"[dim]{result.outcome_detail}[/dim]")
    elif result.status == "needs_human":
        console.print(f"[bold magenta]intervention[/bold magenta]  {result.intervention_id}")
        console.print(f"[dim]{result.reason}[/dim]")
    else:
        console.print(f"[bold red]error[/bold red]  {result.error.summary()}")

    if result.evidence:
        console.print(f"\n[dim]evidence: {result.evidence.directory}[/dim]")


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------


@app.command("serve-app")
def serve_app(port: int = 8099):
    """Run the legacy target application."""
    import uvicorn

    uvicorn.run("apps.legacy_cu.app:app", port=port, log_level="warning", app_dir=str(ROOT))


@app.command("console")
def console_cmd(port: int = 8100):
    """Run the operator console on its own (no live session attached)."""
    import uvicorn

    uvicorn.run("apps.operator.app:app", port=port, log_level="warning", app_dir=str(ROOT))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@app.command()
def discover(
    goal: str = typer.Option(..., help="What to accomplish, in natural language."),
    entry: str = typer.Option(DEFAULT_BASE, help="Application entry point."),
    app_id: str = typer.Option(DEFAULT_APP_ID, help="Vendor product id."),
    provider: str = typer.Option(
        "auto", help="Model provider: auto | anthropic | openai. auto picks whichever key is set."
    ),
    model: Optional[str] = typer.Option(None, help="Model id. Defaults per provider."),
    user: str = typer.Option("teller01"),
    password: str = typer.Option("training-only"),
    headful: bool = typer.Option(False, help="Watch the run in a visible browser."),
    verify: bool = typer.Option(True, help="Replay the artifact once before saving."),
    out: Path = typer.Option(ROOT / "capabilities"),
):
    """Run the LLM-driven discovery loop and save a capability artifact."""
    client, model = _llm_client(provider, model)
    console.print(f"[dim]model {model}[/dim]")
    asyncio.run(
        _discover(goal, entry, app_id, client, model, user, password, headful, verify, out)
    )


def _llm_client(provider: str, model: Optional[str]):
    """Pick the discovery model backend. Replay never reaches this."""
    _load_dotenv(ROOT / ".env")
    has_anthropic = bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "auto":
        provider = "anthropic" if has_anthropic else "openai" if has_openai else ""

    if provider == "anthropic" and has_anthropic:
        import anthropic

        return anthropic.Anthropic(), model or "claude-opus-5"
    if provider == "openai" and has_openai:
        from cua.discovery.llm_openai import DEFAULT_OPENAI_MODEL, OpenAIMessagesClient

        return OpenAIMessagesClient(), model or DEFAULT_OPENAI_MODEL

    console.print(
        "[red]No model credentials found.[/red] Set ANTHROPIC_API_KEY or "
        "OPENAI_API_KEY before `cu discover`.\n"
        "Everything else in this project runs without a key: see `cu replay`."
    )
    raise typer.Exit(2)


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from the gitignored .env. The real environment wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


async def _discover(goal, entry, app_id, client, model, user, password, headful, verify, out):
    from cua.discovery.agent import DiscoveryAgent
    from cua.discovery.generalize import generalize
    from cua.discovery.profiles import load_recovery_profile

    policy = _policy()
    handlers = load_recovery_profile(app_id, ROOT / "config" / "recovery_profiles.yaml")
    console.print(
        f"[dim]inherited {len(handlers)} environment recovery handler(s) "
        f"for {app_id}[/dim]"
    )

    run_id = new_run_id("discovery")
    ev = EvidenceWriter(run_id, ROOT / "evidence")
    session = BrowserSession(headless=not headful, evidence_dir=ev.dir)
    await session.start(trace=True)
    registry.register(session)

    try:
        if not await session.login(entry, user, password):
            console.print("[red]login failed[/red]")
            raise typer.Exit(1)

        agent = DiscoveryAgent(
            session.surface, policy, session.lease, ev,
            client=client, model=model, recovery_handlers=handlers,
        )
        console.rule(f"[cyan]discovery[/cyan] {goal}")
        result = await agent.run(goal, entry, app_id)

        console.print(
            f"\nstatus: [bold]{result.status}[/bold]  "
            f"turns: {result.turns}  steps recorded: {result.steps_recorded}"
        )
        if result.status != "success":
            console.print(f"[yellow]{result.reason}[/yellow]")
            if result.intervention_id:
                console.print(f"intervention: {result.intervention_id}")
            raise typer.Exit(1)

        console.print(f"[dim]{result.summary}[/dim]")
        console.rule("[cyan]generalising[/cyan]")
        capability = generalize(
            result.capability, result.raw_outputs, client=client, model=model, goal=goal
        )
        capability.stats.replays = 0

        console.print(f"id           {capability.id}")
        console.print(f"inputs       {[p.name for p in capability.inputs]}")
        console.print(f"outputs      {[o.name for o in capability.outputs]}")
        console.print(f"outcomes     {[o.name for o in capability.outcomes]}")
        console.print(f"risk         {capability.risk.klass}")
        console.print(f"checkpoint   {capability.checkpoint.describe()}")

        if verify:
            console.rule("[cyan]verification replay[/cyan]")
            # The artifact is only trustworthy if it replays. Verify before
            # saving, using the example values captured during discovery.
            inputs = {p.name: p.example for p in capability.inputs if p.example}
            vev = EvidenceWriter(new_run_id("verify"), ROOT / "evidence")
            engine = ReplayEngine(
                session.surface, policy, session.lease, vev,
                reauth=session.reauth_hook(entry, user, password),
            )
            verified = await engine.run(capability, inputs)
            _print_result(verified)
            if verified.status != "success":
                console.print(
                    "\n[yellow]Artifact did not verify. Saving as draft anyway so "
                    "it can be inspected and repaired.[/yellow]"
                )
            else:
                capability.stats.replays = 1
                capability.stats.successes = 1

        path = Catalog(out).save(capability)
        console.print(f"\n[green]saved[/green] {path}")
        console.print(f"[dim]evidence: {ev.dir}[/dim]")
        console.print(
            f"\nreplay it with:\n  cu replay {capability.id} "
            + " ".join(f"--input {p.name}={p.example}" for p in capability.inputs)
        )
    finally:
        registry.unregister(session.session_id)
        await session.close()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@app.command()
def replay(
    capability_id: str = typer.Argument(..., help="Capability id, optionally id@version."),
    input: list[str] = typer.Option([], "--input", "-i", help="key=value, repeatable."),
    base: str = typer.Option(DEFAULT_BASE),
    user: str = typer.Option("teller01"),
    password: str = typer.Option("training-only"),
    headful: bool = typer.Option(False),
    approved: bool = typer.Option(False, help="Supply human approval for an irreversible capability."),
    escalation: str = typer.Option("return", help="'return' (production) or 'wait' (interactive)."),
    catalog_dir: Path = typer.Option(ROOT / "capabilities"),
    no_reauth: bool = typer.Option(False, help="Disable re-auth, to force an escalation."),
):
    """Replay a saved capability deterministically. No LLM is involved."""
    code = asyncio.run(
        _replay(capability_id, _parse_inputs(input), base, user, password,
                headful, approved, escalation, catalog_dir, no_reauth)
    )
    raise typer.Exit(code)


async def _replay(capability_id, inputs, base, user, password, headful,
                  approved, escalation, catalog_dir, no_reauth):
    cat = Catalog(catalog_dir)
    capability = cat.get(capability_id)
    console.rule(f"[cyan]replay[/cyan] {capability.ref}  [dim]({capability.status})[/dim]")

    ev = EvidenceWriter(new_run_id("replay"), ROOT / "evidence",
                        Redactor(set()))
    session = BrowserSession(headless=not headful, evidence_dir=ev.dir)
    await session.start(trace=True)
    registry.register(session)
    try:
        if not await session.login(base, user, password):
            console.print("[red]login failed[/red]")
            return 1

        engine = ReplayEngine(
            session.surface, _policy(), session.lease, ev,
            escalation_mode=escalation,  # type: ignore[arg-type]
            reauth=None if no_reauth else session.reauth_hook(base, user, password),
        )
        result = await engine.run(capability, inputs, approved=approved)
        _print_result(result)
        cat.record_result(capability, result)
        return 0 if result.ok else 1
    finally:
        registry.unregister(session.session_id)
        await session.close()


# ---------------------------------------------------------------------------
# Catalog — the agent-facing surface
# ---------------------------------------------------------------------------


@app.command()
def catalog(
    tools: bool = typer.Option(False, help="Print function-calling schemas."),
    all_statuses: bool = typer.Option(False, "--all", help="Include drafts."),
    catalog_dir: Path = typer.Option(ROOT / "capabilities"),
):
    """List capabilities as an AI agent would discover them."""
    cat = Catalog(catalog_dir)
    if tools:
        console.print_json(json.dumps(cat.tool_schemas(approved_only=not all_statuses)))
        return

    table = Table(show_header=True, header_style="dim")
    table.add_column("capability")
    table.add_column("status")
    table.add_column("risk")
    table.add_column("inputs")
    table.add_column("outputs")
    table.add_column("outcomes")
    table.add_column("replays")
    for c in cat.load_all():
        table.add_row(
            c.ref, c.status, c.risk.klass,
            ", ".join(p.name for p in c.inputs) or "-",
            ", ".join(o.name for o in c.outputs) or "-",
            ", ".join(o.name for o in c.outcomes) or "-",
            f"{c.stats.successes}/{c.stats.replays}",
        )
    console.print(table)


@app.command()
def invoke(
    name: str = typer.Argument(..., help="Capability name, as an agent would call it."),
    args: str = typer.Argument("{}", help="JSON object of typed arguments."),
    base: str = typer.Option(DEFAULT_BASE),
    catalog_dir: Path = typer.Option(ROOT / "capabilities"),
):
    """Invoke a capability by name with typed JSON args, and print a JSON result.

    This is the shape an AI agent's tool-calling layer would use: names and
    arguments in, a structured result out, no browser knowledge required.
    """
    payload = json.loads(args)
    code = asyncio.run(
        _invoke(name, payload, base, catalog_dir)
    )
    raise typer.Exit(code)


async def _invoke(name, payload, base, catalog_dir):
    cat = Catalog(catalog_dir)
    capability = cat.get(name)
    ev = EvidenceWriter(new_run_id("invoke"), ROOT / "evidence")
    session = BrowserSession(headless=True, evidence_dir=ev.dir)
    await session.start()
    registry.register(session)
    try:
        await session.login(base, "teller01", "training-only")
        engine = ReplayEngine(session.surface, _policy(), session.lease, ev)
        result = await engine.run(capability, payload)
        cat.record_result(capability, result)
        print(json.dumps(
            {
                "status": result.status,
                "outputs": result.outputs,
                "outcome": result.outcome,
                "outcome_detail": result.outcome_detail,
                "intervention_id": result.intervention_id,
                "error": result.error.summary() if result.error else None,
            },
            indent=2,
        ))
        return 0 if result.ok else 1
    finally:
        registry.unregister(session.session_id)
        await session.close()


# ---------------------------------------------------------------------------
# Stability & drift
# ---------------------------------------------------------------------------


@app.command()
def stability(
    capability_id: str = typer.Argument(...),
    input: list[str] = typer.Option([], "--input", "-i"),
    n: int = typer.Option(5, help="How many replays."),
    base: str = typer.Option(DEFAULT_BASE),
    catalog_dir: Path = typer.Option(ROOT / "capabilities"),
):
    """Replay N times and report a stability signal."""
    asyncio.run(_stability(capability_id, _parse_inputs(input), n, base, catalog_dir))


async def _stability(capability_id, inputs, n, base, catalog_dir):
    cat = Catalog(catalog_dir)
    capability = cat.get(capability_id)
    outcomes: list[str] = []
    durations: list[int] = []
    fallbacks = 0

    session = BrowserSession(headless=True)
    await session.start()
    try:
        await session.login(base, "teller01", "training-only")
        for i in range(n):
            ev = EvidenceWriter(new_run_id(f"stab{i}"), ROOT / "evidence")
            engine = ReplayEngine(session.surface, _policy(), session.lease, ev)
            r = await engine.run(capability, inputs)
            outcomes.append(r.status)
            durations.append(r.duration_ms)
            fallbacks += sum(1 for s in r.steps if (s.locator_tier or 0) > 0)
            console.print(f"  run {i + 1}/{n}: {r.status} ({r.duration_ms}ms)")
    finally:
        await session.close()

    ok = sum(1 for o in outcomes if o == "success")
    console.print()
    console.rule("[cyan]stability[/cyan]")
    console.print(f"success rate      {ok}/{n} ({100 * ok / n:.0f}%)")
    console.print(f"median duration   {statistics.median(durations):.0f}ms")
    console.print(f"p95 duration      {max(durations):.0f}ms")
    console.print(f"fallback locators {fallbacks} (0 is healthy)")
    console.print(f"distinct outcomes {sorted(set(outcomes))}")
    if len(set(outcomes)) > 1:
        console.print("[yellow]flaky: not every run agreed[/yellow]")


@app.command()
def drift(catalog_dir: Path = typer.Option(ROOT / "capabilities")):
    """Report which saved artifacts are quietly decaying."""
    rows = Catalog(catalog_dir).drift_report()
    if not rows:
        console.print("No replay history yet.")
        return
    table = Table(show_header=True, header_style="dim")
    for col in ("capability", "status", "replays", "success rate",
                "fallbacks/replay", "needs attention"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["capability"], r["status"], str(r["replays"]),
            f"{r['success_rate']:.0%}", str(r["fallbacks_per_replay"]),
            "[yellow]yes[/yellow]" if r["needs_attention"] else "no",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Human-in-the-loop demo
# ---------------------------------------------------------------------------


@app.command("handoff-demo")
def handoff_demo(
    capability_id: str = typer.Argument("lookup_member_balance"),
    member: str = typer.Option("12345"),
    base: str = typer.Option(DEFAULT_BASE),
    port: int = typer.Option(8100),
    headful: bool = typer.Option(True, help="Show the browser the operator will drive."),
    catalog_dir: Path = typer.Option(ROOT / "capabilities"),
):
    """Escalation and human takeover on a live session.

    Forces a condition the automation cannot recover from on its own (the
    application session expires and re-authentication is unavailable), routes
    it to the operator console, and waits. You take control of the very same
    browser window, sign back in by hand, and hand control back; the run then
    resumes on that session and completes.
    """
    asyncio.run(_handoff_demo(capability_id, member, base, port, headful, catalog_dir))


async def _handoff_demo(capability_id, member, base, port, headful, catalog_dir):
    import httpx
    import uvicorn

    cat = Catalog(catalog_dir)
    capability = cat.get(capability_id)

    sys.path.insert(0, str(ROOT))  # `apps` lives at the repo root, not in the package
    config = uvicorn.Config("apps.operator.app:app", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.8)

    ev = EvidenceWriter(new_run_id("handoff"), ROOT / "evidence")
    session = BrowserSession(headless=not headful, evidence_dir=ev.dir)
    await session.start(trace=True)
    registry.register(session)

    try:
        await session.login(base, "teller01", "training-only")

        # Arm the condition the automation cannot fix by itself.
        httpx.post(f"{base}/_control/inject", data={"mode": "timeout"}, timeout=5)

        console.rule("[magenta]human-in-the-loop demo[/magenta]")
        console.print(
            f"Operator console:  [bold]http://127.0.0.1:{port}/[/bold]\n"
            f"Browser window:    the one that just opened — that is the live session\n\n"
            "The run will hit an expired session and stop. In the console:\n"
            "  1. open the intervention\n"
            "  2. [bold]Take control of the live session[/bold]\n"
            "  3. in the browser, sign in as [bold]teller01 / training-only[/bold]\n"
            "  4. [bold]Hand control back[/bold] with resolution 'resume'\n"
        )

        engine = ReplayEngine(
            session.surface, _policy(), session.lease, ev,
            escalation_mode="wait",
            escalation_timeout_s=900,
            reauth=None,  # deliberately unavailable, so a human is required
        )
        result = await engine.run(capability, {"member_id": member})
        _print_result(result)
        console.print(f"\n[dim]lease history:[/dim]")
        for e in session.lease.history:
            console.print(f"  {e.at.strftime('%H:%M:%S')}  {e.from_owner} -> {e.to_owner}  ({e.note})")
    finally:
        registry.unregister(session.session_id)
        await session.close()
        server.should_exit = True
        await server_task


@app.command("schema")
def schema(out: Optional[Path] = typer.Option(None, help="Write JSON Schema here.")):
    """Emit the capability artifact JSON Schema."""
    from cua.core.models import Capability

    text = json.dumps(Capability.model_json_schema(by_alias=True), indent=2)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        console.print(f"wrote {out}")
    else:
        print(text)


if __name__ == "__main__":
    app()
