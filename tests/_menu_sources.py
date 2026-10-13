"""Menu app Swift sources by feature, for tests that read or compile them (#106).

A feature can span several files (AppState.swift plus its AppModels+*.swift model files);
tests read the feature as one text and compile all of its files.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]

FEATURES = {
    "AppState": (
        "AppState.swift",
        "AppModels+Dashboard.swift",
        "AppModels+Agents.swift",
        "AppModels+Steering.swift",
        "AppModels+Updates.swift",
        "AppModels+ScheduledTasks.swift",
        "AppModels+Live.swift",
        "OnboardingChecklist.swift",
    ),
}


def feature_files(name: str, root: Optional[Path] = None) -> List[Path]:
    sources = Path(root or _ROOT) / "menu_app" / "Sources"
    return [sources / file for file in FEATURES[name]]


def feature_source(name: str, root: Optional[Path] = None) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in feature_files(name, root))
