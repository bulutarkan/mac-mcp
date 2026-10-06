import json
import unittest
from pathlib import Path

from fastapi import FastAPI

from mcp_server.policy import RISK_REGISTRY
from mcp_server.rest_routes import router


PUBLISHED_REST_OPERATION_IDS = {
    "run_command", "process_list", "kill_process", "get_system_info",
    "start_background_job", "get_job_status", "get_job_output", "stop_job",
    "list_jobs", "wait_jobs", "run_commands_parallel", "write_file",
    "write_files_batch", "read_file", "read_multiple_files", "edit_file",
    "move_file", "copy_file", "delete_path", "list_directory",
    "directory_tree", "create_directory", "get_file_info", "find_files",
    "run_applescript", "send_notification", "clipboard_get", "clipboard_set",
    "open_app", "open_url", "set_volume", "get_volume", "set_brightness",
    "screenshot", "set_reminder", "get_running_apps", "artifact_pipeline", "context_handoff", "mac_snapshot", "mac_observe", "mac_act",
    "search_files", "spotlight_search", "http_request", "browser_open_url",
    "browser_list_tabs", "browser_activate_tab", "browser_close_tab",
    "browser_execute_js", "browser_click_selector", "browser_type_selector",
    "browser_wait_for_selector", "browser_get_html", "browser_wait_for_download", "browser_upload_artifact",
    "browser_screenshot", "browser_scroll", "browser_press_key",
    "browser_coordinate_click", "browser_get_snapshot", "ask_user", "ask_choice",
    "ask_confirmation",
}


class OpenAPICoverageTests(unittest.TestCase):
    def test_published_schema_matches_explicit_rest_surface_contract(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "openapi" / "custom-gpt-actions.json").read_text()
        )
        operations = [item["post"] for item in schema["paths"].values()]
        operation_ids = {operation["operationId"] for operation in operations}

        self.assertEqual(63, len(schema["paths"]))
        self.assertEqual(PUBLISHED_REST_OPERATION_IDS, operation_ids)
        choice_schema = schema["paths"]["/api/interactive/choice"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(2, choice_schema["properties"]["choices"]["minItems"])
        self.assertEqual(3, choice_schema["properties"]["choices"]["maxItems"])
        self.assertNotIn("/api/files", schema["paths"])
        self.assertNotIn("/api/macos", schema["paths"])
        self.assertNotIn("/api/browser", schema["paths"])
        self.assertNotIn("/api/search", schema["paths"])
        mac_act_schema = schema["paths"]["/api/mac_act"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("target_bundle_id", mac_act_schema["properties"])
        self.assertIn("process-bound target", schema["paths"]["/api/mac_act"]["post"]["description"])
        description = schema["info"]["description"]
        self.assertNotIn("Every MCP tool", description)
        self.assertIn("selected MCP tools", description)
        self.assertIn("MCP-only", description)

    def test_published_operations_have_machine_readable_json_response_schemas(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "openapi" / "custom-gpt-actions.json").read_text()
        )
        components = schema["components"]["schemas"]
        for path, item in schema["paths"].items():
            with self.subTest(path=path):
                response = item["post"]["responses"]["200"]
                body_schema = response["content"]["application/json"]["schema"]
                self.assertTrue(body_schema)
                ref = body_schema.get("$ref")
                if ref:
                    name = ref.removeprefix("#/components/schemas/")
                    self.assertIn(name, components)

        expected_refs = {
            "/api/run": "RunCommandResult",
            "/api/read_file": "ReadFileResult",
            "/api/browser_list_tabs": "BrowserListTabsResult",
            "/api/interactive": "AskUserResult",
            "/api/interactive/choice": "AskChoiceResult",
            "/api/interactive/confirmation": "AskConfirmationResult",
            "/api/mac_act": "ToolResult",
        }
        for path, component in expected_refs.items():
            self.assertEqual(
                f"#/components/schemas/{component}",
                schema["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            )

    def test_consequential_metadata_matches_representative_policy_risk(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "openapi" / "custom-gpt-actions.json").read_text()
        )
        operations = {
            item["post"]["operationId"]: item["post"]
            for item in schema["paths"].values()
        }

        for operation_id in ("run_command", "run_commands_parallel", "process_list"):
            with self.subTest(operation_id=operation_id):
                self.assertEqual(
                    RISK_REGISTRY[operation_id].destructive,
                    operations[operation_id]["x-openai-isConsequential"],
                )

    def test_fastapi_router_publishes_the_same_operation_ids(self):
        app = FastAPI()
        app.include_router(router)
        schema = app.openapi()
        operations = [
            operation
            for path_item in schema["paths"].values()
            for method, operation in path_item.items()
            if method == "post"
        ]
        operation_ids = {operation["operationId"] for operation in operations}

        self.assertEqual(63, len(operations))
        self.assertEqual(PUBLISHED_REST_OPERATION_IDS, operation_ids)
        self.assertEqual(
            3,
            schema["components"]["schemas"]["ChoiceRequest"]["properties"]["choices"]["maxItems"],
        )

        published = json.loads(
            (Path(__file__).parents[1] / "openapi" / "custom-gpt-actions.json").read_text()
        )
        published_pairs = {
            (path.removeprefix("/api"), item["post"]["operationId"])
            for path, item in published["paths"].items()
        }
        generated_pairs = {
            (path, item["post"]["operationId"])
            for path, item in schema["paths"].items()
        }
        self.assertEqual(generated_pairs, published_pairs)


if __name__ == "__main__":
    unittest.main()
