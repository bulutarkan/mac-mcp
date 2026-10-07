from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import mcp_server.rest_routes as rest_routes
from mcp_server.policy import PolicyContext
from mcp_server.policy_scope import ResourceScope
from mcp_server.security import load_settings


class RestMemorySkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_settings = rest_routes._settings
        self._old_context = rest_routes._rest_security_context
        self._old_telemetry = rest_routes._rest_security_telemetry
        self._old_provider = rest_routes._rest_security_approval_provider
        rest_routes._rest_security_context = None
        rest_routes._rest_security_telemetry = None
        rest_routes._rest_security_approval_provider = None
        app = FastAPI()
        app.include_router(rest_routes.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        rest_routes._settings = self._old_settings
        rest_routes._rest_security_context = self._old_context
        rest_routes._rest_security_telemetry = self._old_telemetry
        rest_routes._rest_security_approval_provider = self._old_provider

    @staticmethod
    def _context(*families: str) -> PolicyContext:
        return PolicyContext(
            profile="read_only",
            actor="agent:rest-parity-test",
            agent_id="agt_rest_parity",
            scope=ResourceScope(tool_families=families, access_mode="read_only"),
        )

    def test_auth_is_required_when_server_auth_is_enabled(self) -> None:
        rest_routes._settings = replace(load_settings(), allow_no_auth=False, api_key="rest-parity-secret")
        response = self.client.post("/memory/search", json={"query": "launch notes"})
        self.assertEqual(401, response.status_code, response.text)

    def test_scope_denies_memory_when_family_is_not_granted(self) -> None:
        rest_routes._settings = replace(load_settings(), allow_no_auth=True, api_key="")
        with patch.object(
            rest_routes,
            "resolve_request_identity",
            return_value=("cred_rest_parity", self._context("browser")),
        ), patch.object(rest_routes, "memory_search") as search:
            response = self.client.post("/memory/search", json={"query": "launch notes"})
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual("scope_denied", response.json()["detail"]["error"])
        search.assert_not_called()

    def test_read_only_memory_routes_are_exposed_and_hide_local_paths(self) -> None:
        rest_routes._settings = replace(load_settings(), allow_no_auth=True, api_key="")
        memory_result = {
            "ok": True,
            "query": "launch notes",
            "results": [{
                "memory_id": "mem_1",
                "content": "Remember the launch checklist.",
                "file_path": "2026/10/2026-10-07.md",
            }],
            "index_sync": {"updated_files": ["2026/10/2026-10-07.md"]},
        }
        get_result = {
            "ok": True,
            "memory_id": "mem_1",
            "content": "Remember the launch checklist.",
            "file_path": "2026/10/2026-10-07.md",
        }
        with patch.object(
            rest_routes,
            "resolve_request_identity",
            return_value=("cred_rest_parity", self._context("memory")),
        ), patch.object(rest_routes, "memory_search", return_value=memory_result) as search, patch.object(
            rest_routes, "memory_get", return_value=get_result
        ) as get:
            searched = self.client.post("/memory/search", json={"query": "launch notes", "limit": 5})
            fetched = self.client.post("/memory/get", json={"memory_id": "mem_1"})
        self.assertEqual(200, searched.status_code, searched.text)
        self.assertEqual(200, fetched.status_code, fetched.text)
        self.assertNotIn("index_sync", searched.json())
        self.assertNotIn("file_path", searched.json()["results"][0])
        self.assertNotIn("file_path", fetched.json())
        search.assert_called_once_with(
            query="launch notes", date=None, date_from=None, date_to=None,
            tags=None, importance=None, sort="relevance", limit=5,
        )
        get.assert_called_once_with("mem_1")

    def test_read_only_skill_routes_are_name_based_and_hide_local_paths(self) -> None:
        rest_routes._settings = replace(load_settings(), allow_no_auth=True, api_key="")
        listed_result = {
            "ok": True,
            "skills_root": "/Users/test/.mac-mcp/skills",
            "skills": [{
                "name": "release-helper",
                "description": "Release helper",
                "location": "/Users/test/.mac-mcp/skills/release-helper/SKILL.md",
                "directory": "/Users/test/.mac-mcp/skills/release-helper",
            }],
            "index_sync": {"updated": 0},
        }
        searched_result = {
            "ok": True,
            "results": [dict(listed_result["skills"][0])],
            "index_sync": {"updated": 0},
        }
        fetched_result = {
            "ok": True,
            **listed_result["skills"][0],
            "content": "# Release helper",
            "resources": [{
                "path": "references/checklist.md",
                "absolute_path": "/Users/test/.mac-mcp/skills/release-helper/references/checklist.md",
                "kind": "references",
                "bytes": 42,
            }],
            "index_sync": {"updated": 0},
            "usage_note": "Resolve relative resource paths against directory.",
        }
        with patch.object(
            rest_routes,
            "resolve_request_identity",
            return_value=("cred_rest_parity", self._context("skills")),
        ), patch.object(rest_routes, "skill_list", return_value=listed_result) as listed, patch.object(
            rest_routes, "skill_search", return_value=searched_result
        ) as searched, patch.object(rest_routes, "skill_get", return_value=fetched_result) as fetched:
            list_response = self.client.post("/skills/list", json={"limit": 25})
            search_response = self.client.post("/skills/search", json={"query": "release", "limit": 4})
            get_response = self.client.post("/skills/get", json={"name": "release-helper", "resource_limit": 20})
        for response in (list_response, search_response, get_response):
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            self.assertNotIn("skills_root", body)
            self.assertNotIn("index_sync", body)
        self.assertNotIn("location", list_response.json()["skills"][0])
        self.assertNotIn("directory", search_response.json()["results"][0])
        self.assertNotIn("location", get_response.json())
        self.assertNotIn("directory", get_response.json())
        self.assertNotIn("absolute_path", get_response.json()["resources"][0])
        self.assertEqual("Resource paths are relative to the selected skill.", get_response.json()["usage_note"])
        listed.assert_called_once_with(limit=25)
        searched.assert_called_once_with(query="release", limit=4)
        fetched.assert_called_once_with(name="release-helper", resource_limit=20)

    def test_skill_get_rejects_path_lookup_on_rest_surface(self) -> None:
        rest_routes._settings = replace(load_settings(), allow_no_auth=True, api_key="")
        with patch.object(
            rest_routes,
            "resolve_request_identity",
            return_value=("cred_rest_parity", self._context("skills")),
        ), patch.object(rest_routes, "skill_get") as fetched:
            response = self.client.post("/skills/get", json={"path": "/tmp/external/SKILL.md"})
        self.assertEqual(422, response.status_code, response.text)
        fetched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
