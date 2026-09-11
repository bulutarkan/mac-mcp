import json
import unittest

from mcp_server.tools_browser_agent import (
    _VISUAL_MODES,
    _condition_js,
    _dom_capture_start_js,
    _extract_action_js,
    _find_candidates_js,
    _format_observation,
    _network_idle_state_js,
    semantic_extract_fields,
    _score_candidate,
    _normalize_text,
)


class BrowserAgentLayerTests(unittest.TestCase):
    def test_normalize_text_is_case_and_accent_tolerant(self):
        self.assertEqual('konyaalti', _normalize_text('Konyaaltı').replace('ı','i'))

    def test_semantic_find_prefers_matching_actionable_element(self):
        strong = {
            'text': 'Konyaaltı', 'aria_label': 'İlçe', 'placeholder': '', 'name': 'district',
            'title': '', 'role': 'combobox', 'tag': 'select', 'actionable': True,
        }
        weak = {
            'text': 'Antalya listings', 'aria_label': '', 'placeholder': '', 'name': '',
            'title': '', 'role': '', 'tag': 'div', 'actionable': False,
        }
        self.assertGreater(
            _score_candidate(strong, 'Konyaaltı district filter', 'combobox', None),
            _score_candidate(weak, 'Konyaaltı district filter', 'combobox', None),
        )

    def test_find_matches_input_value_and_role_only(self):
        element = {
            'text': '', 'aria_label': '', 'placeholder': '', 'name': 'q',
            'title': '', 'value': 'coffee Antalya', 'role': 'combobox',
            'tag': 'input', 'actionable': True,
        }
        self.assertGreaterEqual(_score_candidate(element, 'coffee Antalya', 'combobox', None), 0.8)
        self.assertGreaterEqual(_score_candidate(element, '', 'combobox', None), 0.3)

    def test_find_candidate_js_searches_input_value(self):
        script = _find_candidates_js('coffee Antalya', 'combobox', None, 20, actionable_only=True)
        self.assertIn("d.value||''", script)

    def test_generic_query_words_do_not_dilute_target(self):
        element = {
            'text': 'Ara', 'aria_label': '', 'placeholder': '', 'name': '',
            'title': '', 'role': 'button', 'tag': 'button', 'actionable': True,
        }
        self.assertGreaterEqual(_score_candidate(element, 'Ara button control', 'button', None), 0.8)

    def test_exact_text_beats_prefix_match(self):
        exact = {
            'text': 'Emlak', 'aria_label': '', 'placeholder': '', 'name': '',
            'title': '', 'role': 'link', 'tag': 'a', 'actionable': True,
        }
        prefix = {
            'text': 'Emlak360', 'aria_label': '', 'placeholder': '', 'name': '',
            'title': '', 'role': 'link', 'tag': 'a', 'actionable': True,
        }
        self.assertGreater(
            _score_candidate(exact, 'Emlak', 'link', None),
            _score_candidate(prefix, 'Emlak', 'link', None),
        )

    def test_role_is_a_hard_filter(self):
        wrong_role = {
            'text': 'Ara', 'aria_label': '', 'placeholder': '', 'name': '',
            'title': '', 'role': 'link', 'tag': 'a', 'actionable': True,
        }
        self.assertEqual(0.0, _score_candidate(wrong_role, 'Ara', 'button', 'Ara'))

    def test_short_text_does_not_match_inside_word(self):
        search = {
            'text': '', 'aria_label': '', 'placeholder': 'Kelime, ilan no...', 'name': '',
            'title': '', 'role': 'textbox', 'tag': 'input', 'actionable': True,
        }
        self.assertEqual(0.0, _score_candidate(search, 'İl', 'textbox', 'İl'))

    def test_visual_observation_keeps_compact_dom_with_image(self):
        payload = {
            'ok': True, 'observation_id': 'obs1', 'dom_revision': 7,
            'url': 'https://example.com', 'title': 'Example', 'scope': 'interactive',
            'element_count': 1, 'viewport': {'w': 1000, 'h': 700}, 'scroll': {'x': 0, 'y': 0},
            'duration_ms': 123,
            'elements': [{
                'element_id': 'e2', 'tag': 'input', 'role': 'combobox', 'value': 'coffee Antalya',
                'actionable': True, 'viewport_rect': {'x': 10, 'y': 20, 'w': 200, 'h': 30},
                'screen_rect': {'x': 10, 'y': 120, 'w': 200, 'h': 30},
            }],
            'visual': {
                'mode': 'viewport', 'output_width': 900, 'output_height': 600, 'truncated': False,
                'background_safe': True, 'tab_activated': False,
            },
        }
        result = _format_observation(payload, b'jpeg-bytes')
        self.assertIsInstance(result, list)
        compact = json.loads(result[0])
        self.assertEqual('e2', compact['elements'][0]['element_id'])
        self.assertEqual('coffee Antalya', compact['elements'][0]['value'])
        self.assertNotIn('screen_rect', compact['elements'][0])
        self.assertEqual('viewport', compact['visual']['mode'])
        self.assertEqual(2, len(result))

    def test_network_idle_does_not_accept_about_blank(self):
        script = _condition_js({"for": "network_idle"}, "https://example.com")
        self.assertIn("location.href!=='about:blank'", script)
        self.assertIn("document.body.innerText", script)

    def test_network_idle_state_uses_spa_content_stability_signature(self):
        script = _network_idle_state_js()
        self.assertIn('content_signature', script)
        self.assertIn('body_text_length', script)
        self.assertIn('control_count', script)
        self.assertIn("document.readyState==='complete'", script)

    def test_extract_action_builds_targeted_selector_payload(self):
        script = _extract_action_js([{"name": "price", "selector": ".price", "attr": "text"}], 1200)
        self.assertIn('querySelectorAll(sel)', script)
        self.assertIn('"price"', script)
        self.assertIn('".price"', script)
        self.assertIn('budget=1200', script)


    def test_semantic_extract_fields_are_compact_and_deduplicated(self):
        fields = semantic_extract_fields(["price", "cancellation", "price", "parking"])
        self.assertEqual(["price", "cancellation", "parking"], [item["name"] for item in fields])
        self.assertTrue(all(item.get("semantic") for item in fields))
        self.assertEqual([2, 2, 2], [item.get("max_items") for item in fields])
        compact_fields = semantic_extract_fields(["address", "website", "rating"])
        self.assertEqual([1, 1, 2], [item.get("max_items") for item in compact_fields])

    def test_semantic_extract_script_keeps_selector_compatibility(self):
        script = _extract_action_js([
            {"name": "price", "semantic": "price", "all": True, "max_items": 2},
            {"name": "title", "selector": "h1", "attr": "text"},
        ], 1500)
        self.assertIn("semanticValues", script)
        self.assertIn("querySelectorAll(sel)", script)
        self.assertIn("free cancellation", script)
        self.assertIn("budget=1500", script)

    def test_semantic_extract_supports_dynamic_hours_address_and_website(self):
        fields = semantic_extract_fields(["rating", "hours", "address", "website"])
        script = _extract_action_js(fields, 3000)
        self.assertIn("kapanış saati", script)
        self.assertIn("street address", script)
        self.assertIn("websiteHint", script)
        self.assertIn("candidate.href", script)
        self.assertIn("data-item-id", script)

    def test_full_page_is_a_supported_visual_mode(self):
        self.assertIn('full_page', _VISUAL_MODES)

    def test_full_page_dom_capture_does_not_scroll_or_activate_tabs(self):
        script = _dom_capture_start_js('full_page', None)
        self.assertNotIn('scrollTo(', script)
        self.assertNotIn('current tab', script.lower())
        self.assertIn('capture_method:"dom_rasterizer"', script)
        self.assertIn('tab_activated:false', script)

    def test_dom_capture_keeps_image_data_out_of_text_metadata(self):
        script = _dom_capture_start_js('viewport', None)
        self.assertIn('window[stateKey]={status:"done",meta:meta,data:data}', script)
        self.assertNotIn('base64,${', script)


if __name__ == '__main__':
    unittest.main()
