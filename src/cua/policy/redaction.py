"""Redaction of regulated data.

Two layers, because one is not enough:

**At capture.** Any input parameter declared `pii` or `secret` is substituted
into the flow as a `{{template}}` and its concrete value never enters the
artifact, the run log, or the model's context. This is the layer that actually
works, because it prevents the value from existing in our data at all.

**At egress.** A pattern scrub applied to every string that reaches a log or
an evidence file. This is defence in depth for the values we did not know to
declare — a balance read off a page, an SSN rendered in a table cell. Pattern
matching is inherently incomplete, which is precisely why it is the second
layer and not the first.

The ordering matters for the same reason input validation beats output
escaping: the earlier you stop the data, the fewer places you have to be
right.
"""

import re

REDACTED = "[REDACTED]"

# Ordered most-specific first: a card number would otherwise be eaten by the
# generic long-digit-run rule and lose its label in the log.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("email", re.compile(r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(r"\b(?:\+1[ -]?)?\(?\d{3}\)?[ -]\d{3}[ -]\d{4}\b")),
    ("account", re.compile(r"\b\d{9,}\b")),
]


class Redactor:
    """Scrubs strings before they are logged or persisted."""

    def __init__(self, sensitive_values: set[str] | None = None):
        # Concrete values we were told are sensitive (the capture layer).
        self._values = {v for v in (sensitive_values or set()) if v and len(str(v)) >= 3}

    def add_value(self, value: str | None) -> None:
        if value and len(str(value)) >= 3:
            self._values.add(str(value))

    def text(self, s: str | None) -> str | None:
        """Scrub a free string. Safe to call on anything headed for disk."""
        if s is None:
            return None
        out = str(s)
        for val in self._values:
            if val in out:
                out = out.replace(val, REDACTED)
        for label, pat in PATTERNS:
            out = pat.sub(f"[REDACTED:{label}]", out)
        return out

    def obj(self, value):
        """Recursively scrub a JSON-ish structure."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        return value

    @staticmethod
    def for_logging(param_name: str, value: str, sensitivity: str) -> str:
        """How a typed input should appear in a log line.

        Length is preserved for `pii` because "the member number we sent had 5
        digits" is often the fact you need when debugging, and it discloses
        nothing. Secrets get no shape at all.
        """
        if sensitivity == "secret":
            return REDACTED
        if sensitivity == "pii":
            return f"[REDACTED:{param_name}:len={len(str(value))}]"
        return str(value)
