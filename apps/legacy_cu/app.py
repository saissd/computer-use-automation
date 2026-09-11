"""MERIDIAN CU — Member Services Console (a deliberately hostile stand-in).

This is a proxy for the class of application the real system targets: a
server-rendered, frameset-based, table-laid-out back-office app with no test
IDs, no API, and no clean DOM. It exists for two reasons:

1. **Realism.** A public demo site is a clean modern SPA. Automating one
   proves nothing about the environment described in the brief. This app has
   framesets, nested tables, `<font>` tags and ASP.NET-style control ids.

2. **Error injection.** This is the reason we build rather than borrow. The
   interesting failures in a stable enterprise UI are runtime conditions, and
   you cannot make a public site return "record not found", expire a session,
   or throw a 500 on demand. Here you can, deterministically.

Injectable conditions (see `/_control/inject`):
    not_found    member 99999 is permanently absent
    denied       member 55555 is permanently permission-denied
    validation   deposit over $10,000 is rejected per-field
    slow         next content request stalls 8s
    error500     next content request returns an app error page
    dialog       next content page renders a maintenance interstitial
    timeout      next content request treats the session as expired

Accessibility-name coverage is deliberately *mixed*: some controls carry a
proper <label for>, some only a `title`, and some have neither and can only be
found by their position relative to a nearby table cell. That mix is what
forces the replay engine's ranked locator strategy to actually earn its keep.
"""

import asyncio
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import data

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

APP_USER = "teller01"
APP_PASSWORD = "training-only"
SESSION_IDLE_SECONDS = 900

app = FastAPI(title="Meridian CU Member Services", docs_url=None, redoc_url=None)

# In-memory session table. A real app would use the institution's SSO.
_sessions: dict[str, dict] = {}

# One-shot injection flags, consumed by the next content request. Driving this
# from a control endpoint rather than a query string means the *artifact* never
# has to know about error injection — tests arm a condition, then replay the
# unmodified capability.
_pending_injection: str | None = None


def _consume_injection() -> str | None:
    global _pending_injection
    mode, _pending_injection = _pending_injection, None
    return mode


def _session(request: Request) -> dict | None:
    sid = request.cookies.get("MRDNSESS")
    if not sid:
        return None
    sess = _sessions.get(sid)
    if not sess:
        return None
    if time.time() - sess["last_seen"] > SESSION_IDLE_SECONDS:
        _sessions.pop(sid, None)
        return None
    sess["last_seen"] = time.time()
    return sess


def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("show_maintenance", False)
    return TEMPLATES.TemplateResponse(request, template, ctx)


async def _guard(request: Request) -> Response | dict:
    """Apply injections and the session check. Returns a Response to short-circuit."""
    mode = _consume_injection()

    if mode == "slow":
        await asyncio.sleep(8)
    elif mode == "error500":
        return _render(
            request,
            "error.html",
            code="SYS-0x80040E14",
            detail="Unhandled exception in MbrSvc.Data.AccountProvider.",
        )
    elif mode == "timeout":
        sid = request.cookies.get("MRDNSESS")
        _sessions.pop(sid, None)

    sess = _session(request)
    if not sess:
        return _render(request, "timeout.html")

    return {"session": sess, "maintenance": mode == "dialog"}


# ---------------------------------------------------------------------------
# Control plane (test-only; not part of the automated surface)
# ---------------------------------------------------------------------------


@app.post("/_control/inject")
async def control_inject(mode: str = Form(...)) -> dict:
    global _pending_injection
    _pending_injection = mode or None
    return {"armed": _pending_injection}


@app.post("/_control/reset")
async def control_reset() -> dict:
    global _pending_injection
    _pending_injection = None
    _sessions.clear()
    data.reset()
    return {"ok": True}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Frameset shell
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not _session(request):
        return RedirectResponse("/login", status_code=302)
    return _render(request, "frameset.html")


@app.get("/nav", response_class=HTMLResponse)
async def nav(request: Request):
    return _render(request, "nav.html")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return _render(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request, userid: str = Form(""), passwd: str = Form("")
):
    if userid.strip() != APP_USER or passwd != APP_PASSWORD:
        return _render(request, "login.html", error="Invalid user ID or password.")
    sid = secrets.token_hex(16)
    _sessions[sid] = {"user": userid.strip(), "last_seen": time.time()}
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("MRDNSESS", sid, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
async def logout(request: Request):
    _sessions.pop(request.cookies.get("MRDNSESS", ""), None)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("MRDNSESS")
    return resp


# ---------------------------------------------------------------------------
# Member search -> detail -> sub-account -> confirmation
# ---------------------------------------------------------------------------


@app.get("/search", response_class=HTMLResponse)
async def search_form(request: Request):
    guard = await _guard(request)
    if isinstance(guard, Response):
        return guard
    return _render(
        request, "search.html", error=None, show_maintenance=guard["maintenance"]
    )


@app.post("/search", response_class=HTMLResponse)
async def search_submit(request: Request, member_no: str = Form("")):
    guard = await _guard(request)
    if isinstance(guard, Response):
        return guard

    member_no = member_no.strip()
    if not member_no:
        return _render(
            request,
            "search.html",
            error="Member number is required.",
            show_maintenance=guard["maintenance"],
        )
    if not member_no.isdigit():
        return _render(
            request,
            "search.html",
            error="Member number must be numeric.",
            show_maintenance=guard["maintenance"],
        )
    return RedirectResponse(f"/member/{member_no}", status_code=302)


@app.get("/member/{member_no}", response_class=HTMLResponse)
async def member_detail(request: Request, member_no: str):
    guard = await _guard(request)
    if isinstance(guard, Response):
        return guard

    if member_no == data.MEMBER_PERMISSION_DENIED:
        return _render(request, "denied.html", member_no=member_no)

    member = data.MEMBERS.get(member_no)
    if member is None:
        return _render(request, "notfound.html", member_no=member_no)

    return _render(
        request,
        "member.html",
        member=member,
        show_maintenance=guard["maintenance"],
    )


@app.get("/member/{member_no}/subaccount", response_class=HTMLResponse)
async def subaccount_form(request: Request, member_no: str):
    guard = await _guard(request)
    if isinstance(guard, Response):
        return guard

    member = data.MEMBERS.get(member_no)
    if member is None:
        return _render(request, "notfound.html", member_no=member_no)

    return _render(
        request,
        "subaccount.html",
        member=member,
        account_types=data.ACCOUNT_TYPES,
        funding_sources=data.FUNDING_SOURCES,
        errors={},
        form={},
        show_maintenance=guard["maintenance"],
    )


@app.post("/member/{member_no}/subaccount", response_class=HTMLResponse)
async def subaccount_submit(
    request: Request,
    member_no: str,
    acct_type: str = Form(""),
    deposit: str = Form(""),
    nickname: str = Form(""),
    funding: str = Form(""),
):
    guard = await _guard(request)
    if isinstance(guard, Response):
        return guard

    member = data.MEMBERS.get(member_no)
    if member is None:
        return _render(request, "notfound.html", member_no=member_no)

    form = {
        "acct_type": acct_type,
        "deposit": deposit,
        "nickname": nickname,
        "funding": funding,
    }
    errors: dict[str, str] = {}

    if not acct_type:
        errors["acct_type"] = "Account type is required."
    if not funding:
        errors["funding"] = "Funding source is required."

    amount = 0.0
    try:
        amount = float(str(deposit).replace(",", "").replace("$", "").strip() or "0")
    except ValueError:
        errors["deposit"] = "Initial deposit must be a dollar amount."
    else:
        if amount <= 0:
            errors["deposit"] = "Initial deposit must be greater than zero."
        elif amount > data.DEPOSIT_LIMIT:
            # The headline validation error the replay suite exercises.
            errors["deposit"] = (
                f"Initial deposit exceeds the ${data.DEPOSIT_LIMIT:,.2f} "
                "single-transaction limit for this account type."
            )

    if errors:
        return _render(
            request,
            "subaccount.html",
            member=member,
            account_types=data.ACCOUNT_TYPES,
            funding_sources=data.FUNDING_SOURCES,
            errors=errors,
            form=form,
            show_maintenance=guard["maintenance"],
        )

    number = data.next_account_number(member_no)
    member.accounts.append(data.Account(number, acct_type, amount))

    return _render(
        request,
        "confirm.html",
        member=member,
        new_account=number,
        acct_type=acct_type,
        amount=amount,
        nickname=nickname or "(none)",
        funding=funding,
        show_maintenance=guard["maintenance"],
    )
