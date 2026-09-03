import json
import unittest
from pathlib import Path

from fastapi import FastAPI

from mcp_server.rest_routes import router


EXPECTED_TOOLS = {
    "run_command", "process_list", "kill_process", "get_system_info",
    "start_background_job", "get_job_status", "get_job_output", "stop_job",
    "list_jobs", "wait_jobs", "run_commands_parallel", "write_file",
    "write_files_batch", "read_file", "read_multiple_files", "edit_file",
    "move_file", "copy_file", "delete_path", "list_directory",
    "directory_tree", "create_directory", "get_file_info", "find_files",
    "run_applescript", "send_notification", "clipboard_get", "clipboard_set",
    "open_app", "open_url", "set_volume", "get_volume", "set_brightness",
    "screenshot", "set_reminder", "get_running_apps", "mac_observe", "mac_act",
    "search_files", "spotlight_search", "http_request", "browser_open_url",
    "browser_list_tabs", "browser_activate_tab", "browser_close_tab",
    "browser_execute_js", "browser_click_selector", "browser_type_selector",
    "browser_wait_for_selector", "browser_get_html", "browser_wait_for_download",
    "browser_screenshot", "browser_scroll", "browser_press_key",
    "browser_coordinate_click", "browser_get_snapshot", "ask_user", "ask_choice",
    "ask_confirmation",
}


class OpenAPICoverageTests(unittest.TestCase):
    def test_published_schema_has_one_operation_per_mcp_tool(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "openapi" / "custom-gpt-actions.json").read_text()
        )
        operations = [item["post"] for item in schema["paths"].values()]
        operation_ids = {operation["operationId"] for operation in operations}

        self.assertEqual(59, len(schema["paths"]))
        self.assertEqual(EXPECTED_TOOLS, operation_ids)
        self.assertNotIn("/api/files", schema["paths"])
        self.assertNotIn("/api/macos", schema["paths"])
        self.assertNotIn("/api/browser", schema["paths"])
        self.assertNotIn("/api/search", schema["paths"])

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

        self.assertEqual(59, len(operations))
        self.assertEqual(EXPECTED_TOOLS, operation_ids)

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
