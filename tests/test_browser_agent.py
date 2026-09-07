import unittest

from mcp_server.tools_browser_agent import _score_candidate, _normalize_text


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

    def test_generic_query_words_do_not_dilute_target(self):
        element = {
            'text': 'Ara', 'aria_label': '', 'placeholder': '', 'name': '',
            'title': '', 'role': 'button', 'tag': 'button', 'actionable': True,
        }
        self.assertGreaterEqual(_score_candidate(element, 'Ara button control', 'button', None), 0.8)


if __name__ == '__main__':
    unittest.main()
