#!/usr/bin/env python3
"""Write menu_app/Sources/SettingsSearchLabels.swift: the labels each Settings pane shows (#125).

  settings_search_labels.py           rewrite the Swift file from the pane sources
  settings_search_labels.py --check   fail if the Swift file is out of date

Settings search finds a pane by the words on it. Rather than copying row titles into the search
index by hand, this reads them from the views: row and group titles, toggles, pickers, section
headings and the titles passed to row helpers. Each source file belongs to one pane; the big
SettingsView.swift is split by its members (`private var browserPane`, `private func providerRow`),
and MEMBERS says which pane, and which tab of it, each member draws. A member that shows labels
but is in neither MEMBERS nor UNSEARCHED makes --check fail, so a new pane is never left out.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
OUTPUT = SOURCES / "SettingsSearchLabels.swift"

# Whole files, by the pane (and tab) they are drawn in.
FILES = {
    "AutomationPane.swift": "scheduled",
    "ScheduledTasksPane.swift": "scheduled/scheduled-tasks",
    "PrivacyPane.swift": "privacy",
    "McpServersPane.swift": "mcpServers",
    "McpConnectorsSection.swift": "mcpServers",
    "MacsPane.swift": "macs",
    "IPhoneSection.swift": "macs",
    "SkillsSection.swift": "agents/skills",
    "TeamHistorySection.swift": "agents/team-history",
    "TeamPresetsSection.swift": "agents/teams",
    "SavedLoginsView.swift": "permissions",
    "MessagesAccessSection.swift": "apps/sending",
    "ConnectAppsBox.swift": "connections/ai-clients",
    "HelpDiagnostics.swift": "help",
    "ProviderUsageSection.swift": "usage/agent-usage",
    "ChatUsageSection.swift": "usage/chat-usage",
}

# SettingsView.swift members, by the pane (and tab) they are drawn in.
MEMBERS = {
    "startPane": "start", "onboardingRow": "start", "onboardingAction": "start", "onboardingOptionalRow": "start",
    "generalPane": "general", "generalIssues": "general", "updateCard": "general", "generalIssueRow": "general", "overviewMetric": "general",
    "activityPane": "activity", "liveActivityChoice": "activity",
    "subagentsPane": "agents", "agentsStatusRows": "agents/agents-general", "defaultAgentCard": "agents/agents-general",
    "agentLimitsGroup": "agents/agents-general", "providersList": "agents/providers", "providerRow": "agents/providers",
    "usagePane": "usage", "usageStorageTab": "usage/usage-data", "usageDataSettings": "usage/usage-data",
    "memorySettings": "usage/usage-data", "usageOverviewCard": "usage/tool-usage", "usageActivityCard": "usage/tool-usage",
    "permissionsPane": "permissions", "choiceRow": "permissions",
    "connectionsPane": "connections", "publicEndpointCard": "connections/ai-clients",
    "browserPane": "connections/browser", "companionRow": "connections/browser",
    "mobilePane": "connections/mobile", "clientsPane": "connections/ai-clients",
    "voicePane": "voice",
    "advancedPane": "advanced", "decisionAccelerationBox": "advanced",
    "appsPane": "apps", "appSupportRow": "apps/app-support", "appSendRow": "apps/sending",
}

# SettingsView.swift members whose labels are not a pane's own (the sidebar, shared helpers).
UNSEARCHED = {
    "body", "sidebar", "sidebarRow", "detail", "selectedPane", "settingsDataStateRow", "inlineError",
    "saveFeedbackRow", "sectionLead", "paneHeader", "updateStepIcon", "usageLegend", "supportBadge",
    "appSupportSummary",
}

STRING = r'"((?:[^"\\]|\\.)*)"'
PATTERNS = [
    re.compile(r'\b(?:SettingsRow|UsageSettingRow)\(\s*title:\s*' + STRING),
    re.compile(r'\bGroupBox\(\s*' + STRING),
    re.compile(r'\bToggle\(\s*' + STRING),
    re.compile(r'\bPicker\(\s*' + STRING),
    re.compile(r'\b(?:sectionLead|paneHeader)\(\s*' + STRING),
    re.compile(r'\btitle:\s*' + STRING),
    re.compile(r'\bText\(\s*' + STRING + r'\s*\)\s*\.font\((?:SettingsType\.title|\.headline)'),
]
MEMBER = re.compile(r"^    (?:@ViewBuilder\s+)?(?:private |fileprivate )?(?:static )?(?:var|func) ([A-Za-z_][A-Za-z0-9_]*)")


def labels_in(text: str) -> list[str]:
    found: list[str] = []
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1)
            # Interpolated labels name data ("Use \(name)"), not the setting; skip them and empty ones.
            if "\\(" in value or not re.search(r"[A-Za-z]", value):
                continue
            value = value.replace('\\"', '"').strip()
            if value not in found:
                found.append(value)
    return found


def members(text: str) -> list[tuple[str, str]]:
    """SettingsView.swift cut at each member declared at the view's top level."""
    parts: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        match = MEMBER.match(line)
        if match:
            parts.append((match.group(1), []))
        if parts:
            parts[-1][1].append(line)
    return [(name, "\n".join(lines)) for name, lines in parts]


def collect() -> tuple[dict[str, list[str]], list[str]]:
    labels: dict[str, list[str]] = {}

    def add(target: str, values: list[str]) -> None:
        bucket = labels.setdefault(target, [])
        bucket.extend(value for value in values if value not in bucket)

    for name, target in FILES.items():
        add(target, labels_in((SOURCES / name).read_text(encoding="utf-8")))
    view = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
    view = view[view.index("struct SettingsView: View"):]
    unmapped: list[str] = []
    for name, body in members(view):
        found = labels_in(body)
        if name in MEMBERS:
            add(MEMBERS[name], found)
        elif found and name not in UNSEARCHED:
            unmapped.append(name)
    return labels, unmapped


def swift_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(labels: dict[str, list[str]]) -> str:
    lines = [
        "// Generated by scripts/settings_search_labels.py from the Settings pane sources. Do not edit:",
        "// change the views, then run the script (tests fail while this file is out of date).",
        "",
        "extension SettingsSearchIndex {",
        "    /// The labels each pane shows, by pane or by \"pane/tab\".",
        "    static let labels: [String: [String]] = [",
    ]
    for target in sorted(labels):
        if not labels[target]:
            continue
        lines.append(f"        {swift_string(target)}: [")
        for value in labels[target]:
            lines.append(f"            {swift_string(value)},")
        lines.append("        ],")
    lines += ["    ]", "}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the Swift file is out of date")
    args = parser.parse_args()
    labels, unmapped = collect()
    if unmapped:
        print("SettingsView members with labels but no pane: " + ", ".join(unmapped)
              + ". Add them to MEMBERS or UNSEARCHED in scripts/settings_search_labels.py.", file=sys.stderr)
        return 1
    text = render(labels)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date: run scripts/settings_search_labels.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
