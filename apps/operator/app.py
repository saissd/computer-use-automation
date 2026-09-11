"""Operator console — the human end of the escalation path.

**What is real here:** the intervention queue and its state machine, the
context carried with each request (which capability, which step, why it
stopped, a screenshot, redacted inputs), the session lease, and the control
transfer itself. When an operator takes control, the lease genuinely moves and
the automation genuinely cannot act until it moves back. When they hand back,
the paused run resumes on the same session, and the diff of what changed while
they held it is recorded as evidence.

**What is deliberately mocked:** the pixels. A production console would embed
the live session — CDP screencast into a canvas, or noVNC against a
containerised browser — so an operator anywhere can drive it. Here the browser
runs headful on the same machine and the operator drives the real window.
That is a cut in the *presentation* of the session, not in the control model,
which is the part worth getting right. See REPORT.md § Escalation & handoff.

There is no authentication on this console. It binds to localhost and is a
demo surface; a real one sits behind the institution's SSO and records
operator identity from it rather than from a text box.
"""

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from cua.session import registry
from cua.session.intervention import QUEUE

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="CUA Operator Console", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(
        request,
        "console.html",
        {
            "interventions": QUEUE.list(),
            "sessions": {
                sid: s.lease.snapshot() for sid, s in registry.SESSIONS.items()
            },
        },
    )


@app.get("/intervention/{intervention_id}", response_class=HTMLResponse)
async def detail(request: Request, intervention_id: str):
    req = QUEUE.get(intervention_id)
    if req is None:
        return RedirectResponse("/", status_code=302)
    session = registry.get(req.session_id) or registry.only()
    return TEMPLATES.TemplateResponse(
        request,
        "intervention.html",
        {
            "req": req,
            "lease": session.lease.snapshot() if session else None,
            "session_id": session.session_id if session else None,
        },
    )


@app.post("/intervention/{intervention_id}/take")
async def take(intervention_id: str, operator: str = Form("operator")):
    """Move the lease to a human. Automation stops on its next lease check."""
    req = QUEUE.take(intervention_id, operator)
    if req is not None:
        session = registry.get(req.session_id) or registry.only()
        if session is not None:
            session.lease.grant_to_human(
                operator, note=f"took control for {intervention_id}"
            )
    return RedirectResponse(f"/intervention/{intervention_id}", status_code=302)


@app.post("/intervention/{intervention_id}/resolve")
async def resolve(
    intervention_id: str,
    resolution: str = Form("resume"),
    note: str = Form(""),
):
    """Hand control back and unblock the paused run.

    Order matters: the lease returns to automation *before* the queue is
    resolved, so that a run waiting on the resolution event never wakes up
    while a human still nominally holds the session.
    """
    req = QUEUE.get(intervention_id)
    if req is not None:
        session = registry.get(req.session_id) or registry.only()
        if session is not None:
            session.lease.return_to_automation(note=note or f"resolved {resolution}")
        QUEUE.resolve(intervention_id, resolution)  # type: ignore[arg-type]
        if req.human_actions:
            req.human_actions[-1].note = note
    return RedirectResponse("/", status_code=302)


# --- machine-readable views -------------------------------------------------


@app.get("/api/interventions")
async def api_interventions(open_only: bool = False):
    return [i.model_dump(mode="json") for i in QUEUE.list(open_only=open_only)]


@app.get("/api/sessions")
async def api_sessions():
    return {sid: s.lease.snapshot() for sid, s in registry.SESSIONS.items()}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "open_interventions": len(QUEUE.list(open_only=True))}
