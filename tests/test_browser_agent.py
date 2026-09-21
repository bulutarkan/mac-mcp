import json
import unittest

from mcp_server.tools_browser_agent import (
    _VISUAL_MODES,
    _condition_js,
    _dom_capture_start_js,
    _extract_action_js,
    _find_candidates_js,
    _browser_state_bootstrap,
    _batch_js,
    _condition_js,
    _effect_changed,
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

    def test_styled_radio_can_match_visible_associated_label_text(self):
        radio = {
            'text': '', 'association_text': 'İş Uluç Mah Konyaaltı Antalya',
            'aria_label': '', 'placeholder': '', 'name': 'address', 'title': '', 'value': 'work',
            'role': 'radio', 'tag': 'input', 'actionable': True,
        }
        self.assertGreaterEqual(_score_candidate(radio, 'İş', 'radio', 'İş'), 0.9)

    def test_modal_scope_and_label_association_are_shared_browser_primitives(self):
        bootstrap = _browser_state_bootstrap()
        for token in (
            'function __mcpTopBlockingModal', 'function __mcpAssociation', 'function __mcpSemanticVisible',
            'ELEMENT_OUTSIDE_MODAL_SCOPE', 'pointer_events_association_fallback', 'hit_target',
            'associated_control', 'associated_label',
        ):
            self.assertIn(token, bootstrap)
        find_script = _find_candidates_js('İş', 'radio', 'İş', 20, actionable_only=True)
        self.assertIn('modal=__mcpTopBlockingModal()', find_script)
        self.assertIn("d.association_text||''", find_script)
        self.assertIn('return __mcpSemanticVisible(el)', find_script)
        self.assertIn('modal_scope:modal?', find_script)
        observe = __import__('mcp_server.tools_browser_agent', fromlist=['_observe_js'])._observe_js('interactive', 20)
        self.assertIn('var modalScope=__mcpModalScope(el)', observe)
        self.assertIn('modal_scope:', observe)

    def test_pointer_events_fallback_does_not_accept_unrelated_ancestor_hit(self):
        bootstrap = _browser_state_bootstrap()
        self.assertIn('pointerBlocked&&!(containsHit||associationRelated)', bootstrap)
        self.assertIn("base.reason_code='ELEMENT_POINTER_EVENTS_NONE'", bootstrap)
        self.assertIn('containsHit||containedByHit||associationRelated', bootstrap)

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
        self.assertIn('__mcpQueryAll(sel)', script)
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
        self.assertIn("__mcpQueryAll(sel)", script)
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

    def test_semantic_context_disambiguates_duplicate_calendar_days(self):
        november = {
            "text": "27", "context": "November 2026", "aria_label": "",
            "placeholder": "", "name": "", "title": "", "value": "",
            "role": "gridcell", "tag": "button", "actionable": True,
        }
        october = {**november, "context": "October 2026"}
        november_score = _score_candidate(november, "November 27", "gridcell", None)
        october_score = _score_candidate(october, "November 27", "gridcell", None)
        self.assertGreaterEqual(november_score, 0.85)
        self.assertGreater(november_score, october_score)
        bootstrap = _browser_state_bootstrap()
        self.assertIn("function __mcpContext", bootstrap)
        self.assertIn("rdp-caption_label", bootstrap)

    def test_deep_dom_scan_covers_open_shadow_and_same_origin_frames_but_skips_companion(self):
        bootstrap = _browser_state_bootstrap()
        self.assertIn("el.shadowRoot&&!__mcpInternalHost(el)", bootstrap)
        self.assertIn("el.contentDocument", bootstrap)
        self.assertIn("mac-mcp-visual-companion-root", bootstrap)
        self.assertIn("function __mcpQueryAll", bootstrap)

    def test_click_uses_pointer_mouse_chain_and_stale_element_recovery(self):
        script = _batch_js([{"type": "click", "element_id": "e_test"}], None)
        for event in ("pointerdown", "mousedown", "pointerup", "mouseup"):
            self.assertIn(event, script)
        self.assertIn("__mcpRecoverElement", script)
        self.assertIn("__mcpRecoverAction", script)
        self.assertIn("a.query||a.target||a.text_match||a.target_text", script)
        self.assertIn("__mcpActivate", script)
        self.assertIn("activation_target", script)

    def test_type_uses_native_value_setter_and_input_events(self):
        script = _batch_js([{"type": "type", "element_id": "e_test", "text": "Prague"}], None)
        self.assertIn("Object.getOwnPropertyDescriptor(proto,'value')", script)
        self.assertIn("beforeinput", script)
        self.assertIn("__mcpSetText", script)
        self.assertIn("InputEvent", script)

    def test_selector_wait_uses_deep_query(self):
        script = _condition_js({"for": "selector", "selector": "#inside-shadow"}, "")
        self.assertIn("__mcpQueryOne", script)
        self.assertNotIn("!!document.querySelector", script)

    def test_effect_change_detects_spa_and_control_state_progress(self):
        base = {"url": "https://x.test", "title": "X", "dom_revision": 4, "connected": True, "aria_expanded": "false", "value": ""}
        self.assertTrue(_effect_changed(base, {**base, "dom_revision": 5}))
        self.assertTrue(_effect_changed(base, {**base, "aria_expanded": "true"}))
        self.assertTrue(_effect_changed(base, {**base, "connected": False}))
        self.assertFalse(_effect_changed(base, dict(base)))


    def test_mutation_watch_is_bounded_and_not_installed_by_idle_state_reads(self):
        bootstrap = _browser_state_bootstrap()
        self.assertIn("function __mcpStartMutationWatch", bootstrap)
        self.assertIn("function __mcpStopMutationWatch", bootstrap)
        self.assertIn("observerTimer=setTimeout", bootstrap)
        state_start = bootstrap.index("function __mcpState()")
        state_end = bootstrap.index("function __mcpVisualTarget", state_start)
        self.assertNotIn("__mcpStartMutationWatch", bootstrap[state_start:state_end])
        observe = __import__('mcp_server.tools_browser_agent', fromlist=['_observe_js'])._observe_js('interactive', 20)
        self.assertIn("__mcpStartMutationWatch(s,5000)", observe)
        act = _batch_js([{"type": "click", "element_id": "e_test"}], None)
        self.assertIn("__mcpStartMutationWatch(s,3000)", act)

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
