"""User-scoped macOS runtime locations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MacOSRuntimeLocations:
    app_data: Path
    library_shortcut: Path
    launch_agent: Path


def current_user_locations(*, home: Path | None = None) -> MacOSRuntimeLocations:
    user_home = (Path.home() if home is None else Path(home)).resolve()
    application_support = user_home / "Library" / "Application Support" / "Context Atomizer"
    return MacOSRuntimeLocations(
        app_data=application_support,
        library_shortcut=application_support / "Context Atomizer Library.webloc",
        launch_agent=(
            user_home
            / "Library"
            / "LaunchAgents"
            / "com.contextatomizer.local.runtime.plist"
        ),
    )
