import json
import unittest
from pathlib import Path

from fastapi import FastAPI

from mcp_server.policy import RISK_REGISTRY
from mcp_server.rest_routes import router


MEMORY_SKILLS_REST_OPERATION_IDS = {
    "memory_search", "memory_get", "skill_list", "skill_search", "skill_get",
}
MEMORY_SKILLS_MCP_ONLY_OPERATION_IDS = {
    "memory_add", "memory_update", "memory_delete", "skill_register", "skill_update_index",
}



class OpenAPICoverageTests(unittest.TestCase):
    def test_published_schema_matches_explicit_rest_surface_contract(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "openapi" / "custom-gpt-actions.json").read_text()
        )
        operations = [item["post"] for item in schema["paths"].values()]
        operation_ids = {operation["operationId"] for operation in operations}

        self.assertTrue(MEMORY_SKILLS_REST_OPERATION_IDS.issubset(operation_ids))
        self.assertTrue(MEMORY_SKILLS_MCP_ONLY_OPERATION_IDS.isdisjoint(operation_ids))
        self.assertTrue(operation_ids.issubset(RISK_REGISTRY))
        choice_schema = schema["paths"]["/api/interactive/choice"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(2, choice_schema["properties"]["choices"]["minItems"])
        self.assertEqual(3, choice_schema["properties"]["choices"]["maxItems"])
        self.assertNotIn("/api/files", schema["paths"])
        self.assertNotIn("/api/macos", schema["paths"])
        self.assertNotIn("/api/browser", schema["paths"])
        self.assertNotIn("/api/search", schema["paths"])
        published_operations = {
            item["post"]["operationId"] for item in schema["paths"].values()
        }
        self.assertTrue(MEMORY_SKILLS_MCP_ONLY_OPERATION_IDS.isdisjoint(published_operations))
        skill_get_schema = schema["paths"]["/api/skills/get"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("name", skill_get_schema["properties"])
        self.assertNotIn("path", skill_get_schema["properties"])
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

        for operation_id in (
            "run_command", "run_commands_parallel", "process_list",
            *sorted(MEMORY_SKILLS_REST_OPERATION_IDS),
        ):
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

        self.assertTrue(MEMORY_SKILLS_REST_OPERATION_IDS.issubset(operation_ids))
        self.assertTrue(MEMORY_SKILLS_MCP_ONLY_OPERATION_IDS.isdisjoint(operation_ids))
        self.assertTrue(operation_ids.issubset(RISK_REGISTRY))
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
