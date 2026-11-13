"""Fallback composer selector behavior with a tiny local DOM double."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from doubao_automation.doubao_agent import DoubaoAgent


class Element:
    def __init__(self):
        self.clicked = False

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click(self, **_kwargs):
        self.clicked = True


class Locator:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


class InputSelectorTests(unittest.TestCase):
    def test_stale_configured_selector_falls_back_to_visible_textarea_for_readiness_and_input(self):
        editor = Element()
        page = SimpleNamespace(
            url='https://www.doubao.com/chat/123456789',
            locator=lambda selector: Locator([editor] if selector == 'textarea' else []),
            wait_for_timeout=lambda _ms: None,
            keyboard=SimpleNamespace(insert_text=lambda _text: None),
        )
        agent = DoubaoAgent(SimpleNamespace(page=page),
                            {'doubao': {'selectors': {'input_box': '#stale-editor'}}},
                            'selector-fixture')
        agent._handle_dialogs = lambda: None

        self.assertTrue(agent._chat_ready())
        agent.type_text('local fixture text')
        self.assertTrue(editor.clicked)


if __name__ == '__main__':
    unittest.main()
