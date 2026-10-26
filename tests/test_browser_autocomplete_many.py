"""Isolated autocomplete-many action validation and dispatch contract."""
import unittest
from unittest.mock import patch
from mcp_server import tools_browser_agent as agent
from mcp_server.browser_schemas import validate_action, ACTION_ITEM_SCHEMA


class AutocompleteManyTests(unittest.TestCase):
    def setUp(self):
        self.action = {'type': 'autocomplete_many', 'selector': '#topic',
                       'selected_selector': '.chip', 'option_selector': '#suggestions [role="option"]',
                       'values': ['Automation', 'macOS', 'Artificial Intelligence']}

    def test_schema_has_operation(self):
        self.assertIn('autocomplete_many', ACTION_ITEM_SCHEMA['properties']['type']['enum'])
        self.assertEqual(self.action, validate_action(0, self.action))

    def test_invalid_values_fail_before_dispatch(self):
        for values in [[], ['Automation', 'automation'], [''], ['a'] * 11, 'bad']:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    validate_action(0, {**self.action, 'values': values})
        with self.assertRaises(ValueError):
            validate_action(0, {k: v for k, v in self.action.items() if k != 'selected_selector'})

    def test_python_normalizer_guards_invalid(self):
        with self.assertRaises(Exception):
            agent.normalize_act_actions([{**self.action, 'values': ['a', 'A']}])

    def test_js_contains_exact_scoped_matching_and_chip_verification(self):
        js = agent._autocomplete_many_js(self.action)
        self.assertIn('aria-controls', js)
        self.assertIn('norm(suggestionLabel(e))===norm(value)', js)
        self.assertIn('norm(selectedLabel(e))===norm(v)', js)
        self.assertIn('chosen(value).length===1', js)
        self.assertIn('MutationObserver', js)
        self.assertIn('autocompleteRuns', js)
        self.assertIn('selectedLabel', js)
        self.assertIn('suggestionLabel', js)

    def test_action_polls_status_and_returns_partial_result(self):
        with patch.object(agent, '_run_json_js', side_effect=[
            {'ok': True, 'pending': True, 'run_token': 'test-token'},
            {'ok': True, 'pending': True},
            {'ok': False, 'completed': ['Automation'], 'remaining': ['macOS', 'Artificial Intelligence'],
             'error': 'autocomplete_incomplete', 'items': [{'value': 'Automation', 'status': 'completed'}]},
        ]) as js, patch.object(agent, 'cancellable_sleep'):
            out = agent._autocomplete_many_action(None, 'Safari', self.action, 1, 1, 'test-tab')
        self.assertFalse(out['ok'])
        self.assertEqual(out['completed'], ['Automation'])
        self.assertFalse(out['automatic_retry'])
        self.assertEqual(out['_js_calls'], 3)
        self.assertEqual(js.call_count, 3)


if __name__ == '__main__':
    unittest.main()
