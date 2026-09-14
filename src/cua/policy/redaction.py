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

**Redaction is a persistence boundary, not a return-value boundary.** The
member id a caller passed in comes back to that caller unredacted in
`ReplayResult.outcome_detail` — they supplied it, they already have it, and
handing them `[REDACTED]` would make the result useless without protecting
anything. What must never happen is that value reaching disk: artifacts, run
logs and evidence all pass through `EvidenceWriter.log()`, which redacts
unconditionally. Compare a `cu invoke` result on stdout with the same run's
`result.json` — the file is redacted, the return value is not. That asymmetry
is deliberate and is asserted by tests.
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

# Discovery runs before anything is classified: no input is declared `pii`
# yet, so the capture layer has nothing to act on and a five-digit member
# number sails past the patterns above. In strict mode every amount and every
# run of four or more digits is scrubbed. Blunt, and deliberately so — a
# discovery log that over-redacts a date costs nothing. (A port like :8099 is
# left alone so URLs stay readable.)
STRICT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("amount", re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")),
    ("number", re.compile(r"(?<![:\w.])\d{4,}\b")),
]


class Redactor:
    """Scrubs strings before they are logged or persisted."""

    def __init__(self, sensitive_values: set[str] | None = None, *, strict: bool = False):
        # Concrete values we were told are sensitive (the capture layer).
        self._values = {v for v in (sensitive_values or set()) if v and len(str(v)) >= 3}
        self.strict = strict

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
        for label, pat in PATTERNS + (STRICT_PATTERNS if self.strict else []):
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
