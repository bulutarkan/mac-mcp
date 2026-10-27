"""Team presets: named agent teams saved in Settings > Agents, started by name with spawn_agents.

Each member brings its own provider, model, reasoning and role; the task's own fields still win. A broken
preset never stops the server, a preset that lost a member is refused rather than started smaller, and a
member whose provider is off fails closed and is named. add_task can add a task as one member.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import agent_admission as admission
from mcp_server import runtime_settings
from mcp_server import tools_agents as agents
from mcp_server.security import load_settings

ATLAS = {"id": "m_00000001", "name": "Atlas", "provider": "codex", "model": "gpt-5.5", "reasoning": "high",
         "role": "coder"}
NOVA = {"id": "m_00000002", "name": "Nova", "provider": "claude", "model": "opus", "role": "reviewer"}
ECHO = {"id": "m_00000003", "name": "Echo", "provider": "codex"}
NIGHT_SHIFT = {"id": "tp_0000000a", "name": "Night Shift", "members": [ATLAS, NOVA, ECHO],
               "updated_at": "2026-10-27T12:00:00Z"}


def write_settings(path: Path, presets, *, default=None, enabled=("codex", "claude", "opencode")) -> None:
    subagents = {"providers": {name: {"enabled": name in enabled} for name in ("codex", "claude", "opencode")}}
    if presets is not None:
        subagents["team_presets"] = presets
    if default is not None:
        subagents["default"] = default
    path.write_text(json.dumps({"subagents": subagents}), encoding="utf-8")


class TeamPresetReaderTests(unittest.TestCase):
    def read(self, presets=None, *, raw: str | None = None):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            if raw is not None:
                path.write_text(raw, encoding="utf-8")
            else:
                write_settings(path, presets)
            return runtime_settings.subagent_team_presets(
                providers=agents._PROVIDER_NAMES, roles=agents.VALID_ROLES, path=path)

    def test_a_valid_preset_reads_back_with_empty_fields_as_provider_default(self) -> None:
        presets, problems = self.read([{**NIGHT_SHIFT, "members": [ATLAS, {**ECHO, "model": "", "reasoning": None}]}])
        self.assertEqual([], problems)
        self.assertEqual([], presets[0]["problems"])
        self.assertEqual(["Atlas", "Echo"], [member["name"] for member in presets[0]["members"]])
        self.assertEqual({"id": "m_00000003", "name": "Echo", "provider": "codex", "model": None,
                          "reasoning": None, "role": None}, presets[0]["members"][1])

    def test_malformed_presets_and_members_are_skipped_and_named(self) -> None:
        presets, problems = self.read([
            "not an object",
            {**NIGHT_SHIFT, "id": "TP_bad"},
            {**NIGHT_SHIFT, "members": [
                ATLAS,
                {**NOVA, "provider": "skynet"},
                {**ECHO, "role": "boss"},
                {**ECHO, "id": "m_00000009", "name": "atlas"},
                {**ECHO, "model": 5},
                ECHO,
            ]},
            {**NIGHT_SHIFT, "id": "tp_0000000b", "name": "night shift"},
            {"id": "tp_0000000c", "name": "Empty", "members": []},
            {"id": "tp_0000000d", "name": "  ", "members": [ATLAS]},
        ])
        self.assertEqual(["Night Shift"], [preset["name"] for preset in presets])
        self.assertEqual(["Atlas", "Echo"], [member["name"] for member in presets[0]["members"]])
        joined = "\n".join(problems)
        for fragment in ("team_presets[0] must be an object", "tp_ and 8 lowercase hex", "'skynet'", "'boss'",
                         "repeats another member", ".model must be text", "repeats another preset",
                         "no usable members", "name must be 1-60"):
            self.assertIn(fragment, joined)
        # The preset that lost members keeps why, so it is listed but never starts smaller (fail closed).
        own = presets[0]["problems"]
        self.assertEqual(4, len(own))
        self.assertTrue(all(problem.startswith("team_presets[2] ('Night Shift').members[") for problem in own), own)
        self.assertFalse(any("skipped" in problem for problem in own))

    def test_a_broken_or_missing_settings_file_yields_no_presets(self) -> None:
        self.assertEqual(([], ["settings.json is invalid_json; no team presets read."]), self.read(raw="{nope"))
        self.assertEqual(([], ["subagents.team_presets must be a list; no team presets read."]),
                         self.read({"oops": True}))
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(([], []), runtime_settings.subagent_team_presets(
                providers=agents._PROVIDER_NAMES, roles=agents.VALID_ROLES, path=Path(td) / "missing.json"))

    def test_more_members_than_a_team_holds_are_cut(self) -> None:
        members = [{"id": f"m_{index:08x}", "name": f"M{index}", "provider": "codex"} for index in range(12)]
        presets, problems = self.read([{**NIGHT_SHIFT, "members": members}])
        self.assertEqual(agents.MAX_TEAM_SIZE, len(presets[0]["members"]))
        self.assertEqual(2, len(problems))
        self.assertEqual(2, len(presets[0]["problems"]))

    def test_names_count_unicode_scalars_like_the_app(self) -> None:
        # A family emoji is one character on screen but 7 Unicode scalars (4 people joined by 3 ZWJ). The app's
        # builder counts scalars too (tests/test_team_presets_native.py), so a name it accepts is never skipped.
        family = "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466"
        self.assertEqual(7, len(family))
        fits = family * 5 + "Atlas"          # 40 scalars, 10 characters on screen
        too_long = family * 5 + "Atlas!"     # 41 scalars, 11 characters on screen
        presets, problems = self.read([{**NIGHT_SHIFT, "name": family * 8 + "Team", "members": [
            {**ATLAS, "name": fits}, {**ECHO, "name": too_long}]}])
        self.assertEqual(family * 8 + "Team", presets[0]["name"])  # 60 scalars
        self.assertEqual([fits], [member["name"] for member in presets[0]["members"]])
        self.assertIn("members[1].name must be 1-40 characters", presets[0]["problems"][0])
        presets, problems = self.read([{**NIGHT_SHIFT, "name": family * 8 + "Team!"}])
        self.assertEqual([], presets)
        self.assertIn("name must be 1-60", problems[0])


class TeamPresetSpawnTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        old_dirs = agents.AGENTS_DIR, agents.TEAMS_DIR
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.root / "agents", self.root / "teams"
        agents.AGENTS_DIR.mkdir()
        agents.TEAMS_DIR.mkdir()
        self.addCleanup(lambda: setattr(agents, "AGENTS_DIR", old_dirs[0]) or setattr(agents, "TEAMS_DIR", old_dirs[1]))
        self.settings_path = self.root / "settings.json"
        write_settings(self.settings_path, [NIGHT_SHIFT])
        env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings_path),
                                      "MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT": "8", "MAC_MCP_AGENT_PROVIDER_LIMIT": "8"})
        env.start()
        self.addCleanup(env.stop)
        self.spawned: list[dict] = []
        self.installed = {"codex", "claude", "opencode"}
        for item in (
            patch.object(agents, "_find_binary",
                         side_effect=lambda provider: f"/tmp/fake-{provider}" if provider in self.installed else None),
            patch.object(agents, "_require_provider_compatible"),
            patch.object(agents, "_validate_provider_access_mode"),
            patch.object(agents, "_require_provider_sign_in"),
            patch.object(agents, "_validate_provider_model_selection", return_value={"validated": True}),
            patch.object(agents, "_spawn_internal", side_effect=self.fake_spawn),
            patch.object(agents, "_open_team_room", return_value={"enabled": False}),
        ):
            item.start()
            self.addCleanup(item.stop)

    def fake_spawn(self, *, provider, model=None, reasoning=None, team_id=None, team_task_id=None, title=None,
                   role=None, prompt=None, **kwargs):
        self.counter = getattr(self, "counter", 0) + 1
        agent_id = f"agt_{self.counter}"
        (agents.AGENTS_DIR / agent_id).mkdir()
        now = time.time()
        agents._write_meta(agent_id, {"agent_id": agent_id, "team_id": team_id, "team_task_id": team_task_id,
                                      "provider": provider, "model": model, "status": "running", "phase": "running",
                                      "started_at": now, "updated_at": now,
                                      "admission_lease_id": kwargs.get("admission_lease_id")})
        if kwargs.get("admission_lease_id"):
            admission.bind_agent(agents.AGENTS_DIR, kwargs["admission_lease_id"], agent_id)
        self.spawned.append({"task": team_task_id, "provider": provider, "model": model, "reasoning": reasoning,
                             "title": title, "role": role, "prompt": prompt})
        return {"ok": True, "agent_id": agent_id, "status": "running"}

    def spawn(self, tasks, **kwargs):
        cwd = self.root / "work"
        cwd.mkdir(exist_ok=True)
        kwargs.setdefault("provider", None)
        return agents.spawn_agents(load_settings(), tasks=tasks, cwd=str(cwd), access_mode="read_only",
                                   git_isolation="auto", retries=0, **kwargs)

    def runs(self):
        return {row["task"]: row for row in self.spawned}

    def refused(self, tasks, **kwargs) -> HTTPException:
        with self.assertRaises(HTTPException) as ctx:
            self.spawn(tasks, **kwargs)
        self.assertEqual([], self.spawned)
        self.assertEqual([], list(agents.TEAMS_DIR.iterdir()))
        return ctx.exception

    def test_tasks_take_members_in_order_and_unused_members_do_not_run(self) -> None:
        team = self.spawn([{"id": "build", "prompt": "build it"}, {"id": "review", "prompt": "review it"}],
                          team_preset="night shift")
        runs = self.runs()
        self.assertEqual(("codex", "gpt-5.5", "high", "Atlas", "coder"),
                         tuple(runs["build"][key] for key in ("provider", "model", "reasoning", "title", "role")))
        self.assertEqual(("claude", "opus", None, "Nova", "reviewer"),
                         tuple(runs["review"][key] for key in ("provider", "model", "reasoning", "title", "role")))
        self.assertEqual(2, len(self.spawned))
        # Members bring no identity or memory text: the prompt is the task's own.
        self.assertTrue(runs["build"]["prompt"].strip().startswith("build it"))
        self.assertNotIn("Atlas", runs["build"]["prompt"])
        self.assertEqual({"id": "tp_0000000a", "name": "Night Shift"}, team["team_preset"])
        self.assertEqual("Night Shift", team["title"])

    def test_a_named_member_is_claimed_first_and_its_empty_model_is_the_provider_default(self) -> None:
        # A Default Agent on the same provider must not leak its model into a member that left it empty.
        write_settings(self.settings_path, [NIGHT_SHIFT],
                       default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"})
        self.spawn([{"id": "a", "prompt": "x", "member": "M_00000003"}, {"id": "b", "prompt": "y"}],
                   team_preset="tp_0000000a")
        runs = self.runs()
        self.assertEqual(("codex", None, None, "Echo"),
                         tuple(runs["a"][key] for key in ("provider", "model", "reasoning", "title")))
        self.assertEqual(("codex", "gpt-5.5", "high", "Atlas"),
                         tuple(runs["b"][key] for key in ("provider", "model", "reasoning", "title")))

    def test_the_task_own_fields_win_over_the_member(self) -> None:
        self.spawn([
            {"id": "a", "prompt": "x", "model": "gpt-5.4", "title": "Builder", "role": "orchestrator"},
            {"id": "b", "prompt": "y", "member": "Atlas", "provider": "claude"},
            {"id": "c", "prompt": "z", "member": "Nova", "reasoning": "low"},
        ], team_preset="Night Shift")
        runs = self.runs()
        # b and c claim Atlas and Nova first, so a runs as Echo, with its own model, title and role.
        self.assertEqual(("codex", "gpt-5.4", None, "Builder", "orchestrator"),
                         tuple(runs["a"][key] for key in ("provider", "model", "reasoning", "title", "role")))
        # Another provider keeps none of the member's model or reasoning.
        self.assertEqual(("claude", None, None, "Atlas", "coder"),
                         tuple(runs["b"][key] for key in ("provider", "model", "reasoning", "title", "role")))
        self.assertEqual(("claude", "opus", "low"),
                         tuple(runs["c"][key] for key in ("provider", "model", "reasoning")))

    def test_without_a_default_agent_the_team_follows_its_first_member(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self.spawn([{"id": "a", "prompt": "x"}])
        self.assertEqual(400, ctx.exception.status_code)
        team = self.spawn([{"id": "a", "prompt": "x", "member": "Nova"}, {"id": "b", "prompt": "y"}],
                          team_preset="Night Shift")
        stored = agents._read_team(team["team_id"])
        self.assertEqual(("claude", "opus", None), (stored["provider"], stored["model"], stored["reasoning"]))

    def test_every_member_problem_comes_back_in_one_400(self) -> None:
        error = self.refused([
            {"id": "a", "prompt": "x", "member": "Atlas"},
            {"id": "b", "prompt": "y", "member": "atlas"},
            {"id": "c", "prompt": "z", "member": "Ghost"},
            {"id": "d", "prompt": "w"}, {"id": "e", "prompt": "v"}, {"id": "f", "prompt": "u"},
        ], team_preset="Night Shift")
        self.assertEqual(400, error.status_code)
        detail = str(error.detail)
        self.assertIn("3 problems", detail)
        self.assertIn("already used by tasks[0]", detail)
        self.assertIn("'Ghost' is not in team preset 'Night Shift'", detail)
        self.assertIn("3 tasks have no member but only 2 members are left", detail)

    def test_an_unknown_preset_names_the_saved_ones_and_member_needs_a_preset(self) -> None:
        error = self.refused([{"id": "a", "prompt": "x"}], team_preset="Day Shift")
        self.assertEqual(400, error.status_code)
        self.assertIn("'Day Shift' is not a saved team preset; saved presets: 'Night Shift'", str(error.detail))
        error = self.refused([{"id": "a", "prompt": "x", "member": "Atlas"}], provider="codex")
        self.assertIn("tasks[0].member needs team_preset", str(error.detail))

    def test_a_member_on_a_disabled_or_missing_provider_fails_closed_by_name(self) -> None:
        write_settings(self.settings_path, [NIGHT_SHIFT], enabled=("codex", "opencode"))
        error = self.refused([{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}], team_preset="Night Shift")
        self.assertEqual(403, error.status_code)
        self.assertIn("provider_disabled: member 'Nova' runs on Claude Code", str(error.detail))
        # The team-level provider being enabled does not open a disabled member.
        error = self.refused([{"id": "a", "prompt": "x", "member": "Nova"}], team_preset="Night Shift",
                             provider="codex")
        self.assertEqual(403, error.status_code)
        write_settings(self.settings_path, [NIGHT_SHIFT])
        self.installed.discard("codex")
        error = self.refused([{"id": "a", "prompt": "x", "member": "Echo"}], team_preset="Night Shift")
        self.assertEqual(503, error.status_code)
        self.assertIn("member 'Echo'", str(error.detail))

    def test_the_preset_shows_in_summary_dashboard_and_history_and_survives_retry(self) -> None:
        write_settings(self.settings_path, [NIGHT_SHIFT],
                       default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"})
        team = self.spawn([{"id": "a", "prompt": "x", "member": "Echo"}, {"id": "b", "prompt": "y"},
                           {"id": "c", "prompt": "z"}], team_preset="Night Shift")
        record = {"id": "tp_0000000a", "name": "Night Shift"}
        self.assertEqual(record, agents._read_team(team["team_id"])["team_preset"])
        self.assertEqual(record, agents._team_summary(team["team_id"])["team_preset"])
        with patch.object(agents, "_authorize_team_control", side_effect=lambda team_id, _: agents._read_team(team_id)):
            self.assertEqual(record, agents.dashboard_team_summary(team["team_id"])["team_preset"])
        rows = agents.team_history(limit=5)["teams"]
        self.assertEqual(record, rows[0]["team_preset"])
        first = [(row["provider"], row["model"], row["reasoning"], row["title"]) for row in self.spawned]

        # Retry rebuilds from the stored tasks, even after the preset is changed or removed in Settings.
        write_settings(self.settings_path, [], default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"})
        self.spawned.clear()
        with patch.object(agents, "workflow_for_agent", return_value=None):
            retried = agents.agent_action(load_settings(), "retry", team_id=team["team_id"])
        self.assertEqual(first, [(row["provider"], row["model"], row["reasoning"], row["title"]) for row in self.spawned])
        self.assertEqual(("codex", None, None, "Echo"), first[0])
        self.assertEqual(record, retried["team_preset"])
        self.assertEqual("Retry: Night Shift", retried["title"])

    def test_an_ad_hoc_team_has_no_preset(self) -> None:
        team = self.spawn([{"id": "a", "prompt": "x"}], provider="codex")
        self.assertIsNone(team["team_preset"])

    def test_a_preset_that_lost_a_member_never_starts_smaller(self) -> None:
        write_settings(self.settings_path, [{**NIGHT_SHIFT, "members": [
            ATLAS, {**NOVA, "provider": "skynet"}, {**ECHO, "id": "bad"}]}])
        for kwargs in ({}, {"provider": "codex"}):
            with self.subTest(**kwargs):
                error = self.refused([{"id": "a", "prompt": "x"}], team_preset="Night Shift", **kwargs)
                self.assertEqual(400, error.status_code)
                detail = str(error.detail)
                self.assertIn("team preset 'Night Shift' has 2 member(s) that cannot be read", detail)
                self.assertIn("('Nova').provider 'skynet'", detail)
                self.assertIn("('Echo').id must be m_", detail)
                self.assertIn("Settings > Agents > Teams", detail)
                self.assertNotIn("Default Agent", detail)

    # add_task(member=...): a task added to a running team runs as one of its saved team's members.

    def add(self, team_id, task):
        return agents.agent_action(load_settings(), "add_task", team_id=team_id, task=task)

    def add_refused(self, team_id, task) -> HTTPException:
        before = [item["id"] for item in agents._read_team(team_id)["tasks"]]
        spawned = len(self.spawned)
        with self.assertRaises(HTTPException) as ctx:
            self.add(team_id, task)
        self.assertEqual(before, [item["id"] for item in agents._read_team(team_id)["tasks"]])
        self.assertEqual(spawned, len(self.spawned))
        return ctx.exception

    def test_add_task_runs_as_the_named_member_and_its_own_fields_win(self) -> None:
        team_id = self.spawn([{"id": "build", "prompt": "build it"}], team_preset="Night Shift")["team_id"]
        added = self.add(team_id, {"id": "review", "prompt": "review it", "member": "nova"})
        self.assertEqual("running", added["task_state"])
        self.assertEqual(("claude", "opus", None, "Nova", "reviewer"),
                         tuple(self.runs()["review"][key] for key in ("provider", "model", "reasoning", "title", "role")))
        stored = {task["id"]: task for task in agents._read_team(team_id)["tasks"]}
        self.assertEqual({"id": "m_00000002", "name": "Nova"}, stored["review"]["preset_member"])
        self.add(team_id, {"id": "docs", "prompt": "write docs", "member": "M_00000003", "model": "gpt-5.4",
                           "title": "Docs", "role": "orchestrator"})
        self.assertEqual(("codex", "gpt-5.4", None, "Docs", "orchestrator"),
                         tuple(self.runs()["docs"][key] for key in ("provider", "model", "reasoning", "title", "role")))

    def test_add_task_refuses_a_busy_unknown_or_disabled_member(self) -> None:
        team_id = self.spawn([{"id": "build", "prompt": "build it"}], team_preset="Night Shift")["team_id"]
        busy = self.add_refused(team_id, {"id": "more", "prompt": "more", "member": "Atlas"})
        self.assertEqual(409, busy.status_code)
        self.assertIn("member 'Atlas' is already on task 'build'", str(busy.detail))
        unknown = self.add_refused(team_id, {"id": "more", "prompt": "more", "member": "Ghost"})
        self.assertEqual(400, unknown.status_code)
        self.assertIn("task.member 'Ghost' is not in team preset 'Night Shift'", str(unknown.detail))
        write_settings(self.settings_path, [NIGHT_SHIFT], enabled=("codex", "opencode"))
        disabled = self.add_refused(team_id, {"id": "more", "prompt": "more", "member": "Nova"})
        self.assertEqual(403, disabled.status_code)
        self.assertIn("provider_disabled: member 'Nova' runs on Claude Code", str(disabled.detail))
        # Once its task has finished, the member takes another.
        agents._update_meta("agt_1", lambda meta: meta.update(status="completed"))
        agents._team_tick(team_id)
        self.add(team_id, {"id": "more", "prompt": "more", "member": "Atlas"})
        self.assertEqual(("codex", "gpt-5.5", "high", "Atlas"),
                         tuple(self.runs()["more"][key] for key in ("provider", "model", "reasoning", "title")))

    def test_add_task_member_needs_a_preset_team_whose_preset_is_still_saved(self) -> None:
        ad_hoc = self.spawn([{"id": "a", "prompt": "x"}], provider="codex")["team_id"]
        error = self.add_refused(ad_hoc, {"id": "b", "prompt": "y", "member": "Atlas"})
        self.assertEqual(400, error.status_code)
        self.assertIn("task.member needs a team started from a saved team", str(error.detail))
        team_id = self.spawn([{"id": "a", "prompt": "x"}], team_preset="Night Shift")["team_id"]
        write_settings(self.settings_path, [{**NIGHT_SHIFT, "members": [ATLAS, NOVA, {**ECHO, "provider": "skynet"}]}])
        broken = self.add_refused(team_id, {"id": "b", "prompt": "y", "member": "Nova"})
        self.assertEqual(400, broken.status_code)
        self.assertIn("team preset 'Night Shift' has 1 member(s) that cannot be read", str(broken.detail))
        write_settings(self.settings_path, [])
        deleted = self.add_refused(team_id, {"id": "b", "prompt": "y", "member": "Nova"})
        self.assertEqual(409, deleted.status_code)
        self.assertIn("'Night Shift' this team started from is no longer saved", str(deleted.detail))
        # A task without a member still joins as before.
        self.add(team_id, {"id": "b", "prompt": "y"})


class TeamPresetCatalogTests(unittest.TestCase):
    def test_catalog_lists_presets_with_readiness_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            write_settings(path, [NIGHT_SHIFT, {"id": "tp_0000000b", "name": "Solo", "members": [ECHO]},
                                  {"id": "bad"}], enabled=("codex",))
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}), \
                 patch.object(agents, "_find_binary", side_effect=lambda provider: f"/tmp/fake-{provider}") as find:
                # A disabled provider's catalog stays empty and its models are never read for the listing.
                result = agents.agent_catalog(load_settings(), provider="claude")
        self.assertEqual({}, result["providers"])
        presets = {preset["name"]: preset for preset in result["team_presets"]}
        self.assertEqual(["Night Shift", "Solo"], list(presets))
        night = presets["Night Shift"]
        self.assertFalse(night["ready"])
        self.assertEqual(["Nova: Claude Code is disabled in Mac MCP Settings > Subagents."], night["problems"])
        members = {member["name"]: member for member in night["members"]}
        self.assertTrue(members["Atlas"]["ready"])
        self.assertFalse(members["Nova"]["ready"])
        self.assertEqual({"id": "m_00000001", "name": "Atlas", "provider": "codex", "model": "gpt-5.5",
                          "reasoning": "high", "role": "coder", "ready": True, "problems": []}, members["Atlas"])
        self.assertTrue(presets["Solo"]["ready"])
        self.assertEqual(1, len(result["team_preset_problems"]))
        # One CLI lookup per provider, however many members use it.
        self.assertEqual(1, find.call_count)

    def test_catalog_lists_a_preset_that_lost_a_member_as_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            write_settings(path, [{**NIGHT_SHIFT, "members": [ATLAS, {**NOVA, "role": "boss"}]}])
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}), \
                 patch.object(agents, "_find_binary", side_effect=lambda provider: f"/tmp/fake-{provider}"):
                result = agents.agent_catalog(load_settings(), provider="codex")
        night = result["team_presets"][0]
        self.assertEqual(("Night Shift", False, 1), (night["name"], night["ready"], night["member_count"]))
        self.assertEqual(1, len(night["problems"]))
        self.assertIn("('Nova').role 'boss'", night["problems"][0])
        self.assertTrue(night["members"][0]["ready"])
        self.assertEqual(1, len(result["team_preset_problems"]))


if __name__ == "__main__":
    unittest.main()
