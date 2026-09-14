"""The capability catalog — saved artifacts as callable capabilities.

This is the surface an AI agent actually talks to. It answers three questions:
what capabilities exist, what does each one need, and what does it return. The
answer to all three comes from the artifact itself (`Capability.tool_schema()`),
so there is no second source of truth to drift out of sync with the flow.

The `status` gate matters more than it looks. A freshly discovered capability
is a `draft`: a model wrote it, it replayed once, and that is not enough
evidence to let it drive a bank's back office unattended. Promotion to
`approved` is a deliberate act, and unattended invocation requires it.
"""

import json
from pathlib import Path

from cua.core.models import Capability
from cua.core.results import ReplayResult

DEFAULT_DIR = Path("capabilities")


class CapabilityNotFound(KeyError):
    pass


class Catalog:
    def __init__(self, directory: Path | str = DEFAULT_DIR):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- io ----------------------------------------------------------------

    def path_for(self, capability: Capability) -> Path:
        return self.dir / f"{capability.id}@{capability.version}.json"

    def save(self, capability: Capability) -> Path:
        """Write the artifact as reviewable JSON.

        `exclude_none` keeps the file readable: every field in the schema has a
        default, so omitting the nulls round-trips exactly while removing the
        several hundred `"anchor": null` lines that would otherwise bury the
        parts a human reviewer actually needs to read. Reviewability is a
        requirement here, not a nicety.

        This is also a persistence boundary, so it is where a `pii` or `secret`
        input loses its `example`. The in-memory capability keeps it — the
        verification replay right after discovery needs a real value — but the
        file never gets one.
        """
        path = self.path_for(capability)
        persisted = capability.model_copy(deep=True)
        for param in persisted.inputs:
            if param.sensitivity in ("pii", "secret"):
                param.example = None
        path.write_text(
            json.dumps(
                persisted.model_dump(mode="json", by_alias=True, exclude_none=True),
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def load_file(path: Path | str) -> Capability:
        return Capability.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def load_all(self) -> list[Capability]:
        out = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                out.append(self.load_file(path))
            except Exception as exc:  # noqa: BLE001 - a bad artifact must not hide the good ones
                print(f"  ! skipping {path.name}: {exc}")
        return out

    def get(self, capability_id: str, version: str | None = None) -> Capability:
        """Resolve by id, defaulting to the highest version present."""
        if "@" in capability_id:
            capability_id, version = capability_id.split("@", 1)

        matches = [c for c in self.load_all() if c.id == capability_id]
        if version:
            matches = [c for c in matches if c.version == version]
        if not matches:
            raise CapabilityNotFound(
                f"no capability {capability_id!r}"
                + (f" at version {version!r}" if version else "")
            )
        return sorted(matches, key=lambda c: _semver_key(c.version))[-1]

    # -- agent-facing ------------------------------------------------------

    def tool_schemas(self, *, approved_only: bool = True) -> list[dict]:
        """Function-calling definitions for every invocable capability."""
        return [
            c.tool_schema()
            for c in self.load_all()
            if not approved_only or c.status == "approved"
        ]

    # -- feedback loop -----------------------------------------------------

    def record_result(self, capability: Capability, result: ReplayResult) -> Capability:
        """Fold one replay into the artifact's confidence stats.

        Counting a business outcome as neither success nor failure is
        deliberate: "member not found" says nothing about whether the
        automation is healthy, and letting it drag the success rate down would
        make the approval gate fire on perfectly good capabilities.
        """
        stats = capability.stats
        stats.replays += 1
        if result.status == "success":
            stats.successes += 1
            stats.last_ok = result.started_at
        elif result.status == "business_outcome":
            stats.business_outcomes += 1
        else:
            stats.failures += 1

        stats.fallback_resolutions += sum(
            1 for s in result.steps if (s.locator_tier or 0) > 0
        )
        self.save(capability)
        return capability

    def drift_report(self) -> list[dict]:
        """Which artifacts are quietly decaying.

        A capability that still passes but increasingly needs fallback
        locators is the early warning that a tenant's UI has moved. Catching
        that before it becomes a hard failure is the difference between a
        scheduled re-record and a production incident.
        """
        report = []
        for cap in self.load_all():
            s = cap.stats
            if not s.replays:
                continue
            report.append(
                {
                    "capability": cap.ref,
                    "status": cap.status,
                    "replays": s.replays,
                    "success_rate": round(s.success_rate, 3),
                    "fallbacks_per_replay": round(s.fallback_resolutions / s.replays, 2),
                    "needs_attention": s.fallback_resolutions / s.replays > 0.5
                    or s.success_rate < 0.9,
                }
            )
        return report


def _semver_key(version: str) -> tuple:
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0, 0, 0)
