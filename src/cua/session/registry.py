"""Registry of live sessions, so the operator console can reach them.

This is the piece that makes "take control of the *same* live session" true
rather than aspirational. The console and the running automation are the same
OS process, so the console does not get a handle to a description of the
session — it gets the `BrowserSession` object itself, with its one
`BrowserContext`, its cookies, and its half-filled forms intact.

That co-location is a deliberate simplification with a clear seam. In a
deployment with many concurrent sessions across many workers, this dictionary
becomes a lookup service and the console reaches the browser over CDP or VNC
instead of by reference. What does *not* change is the control-transfer model:
the lease is still the single authority on who may act, and it is checked in
the same place.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from cua.session.browser import BrowserSession

SESSIONS: dict[str, "BrowserSession"] = {}


def register(session: "BrowserSession") -> None:
    SESSIONS[session.session_id] = session


def unregister(session_id: str) -> None:
    SESSIONS.pop(session_id, None)


def get(session_id: str | None) -> "BrowserSession | None":
    if session_id is None:
        return None
    return SESSIONS.get(session_id)


def only() -> "BrowserSession | None":
    """The single live session, when there is exactly one.

    Convenience for the demo path, where the console is driven by a person who
    should not have to copy a session id around.
    """
    return next(iter(SESSIONS.values())) if len(SESSIONS) == 1 else None
