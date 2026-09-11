"""Per-application recovery profiles.

See config/recovery_profiles.yaml for the reasoning. In short: a discovery run
observes one happy path and therefore cannot learn what the application does
when a session expires or the core throws a 500. Those are properties of the
app, learned once by an integration team and inherited by every capability
recorded against it.
"""

from pathlib import Path

import yaml

from cua.core.models import Recovery

DEFAULT_PATH = Path("config/recovery_profiles.yaml")


def load_recovery_profile(
    app_id: str, path: Path | str = DEFAULT_PATH
) -> list[Recovery]:
    """Return the handlers every capability for this app should inherit."""
    path = Path(path)
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profile = raw.get(app_id)
    if not profile:
        return []
    return [Recovery.model_validate(h) for h in profile.get("handlers", [])]


def list_profiles(path: Path | str = DEFAULT_PATH) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: (v or {}).get("description", "") for k, v in raw.items()}
