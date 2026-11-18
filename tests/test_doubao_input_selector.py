"""Fallback composer selector behavior with a tiny local DOM double."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from doubao_automation.doubao_agent import DoubaoAgent, GenerationFailed


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


class UploadReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_composer_waits_for_delayed_video_upload_even_when_send_looks_enabled(self):
        self.assert_ready_only_when_video_finishes(finish_video=True)

    def test_composer_timeout_returns_failure_while_video_is_still_uploading(self):
        self.assert_ready_only_when_video_finishes(finish_video=False)

    def assert_ready_only_when_video_finishes(self, *, finish_video):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content('''
          <div data-testid="chat_input">
            <button data-testid="skill_input_exit_button">视频生成</button>
            <input type="file" multiple hidden>
            <textarea></textarea>
            <button data-testid="chat_input_send_button" aria-disabled="false">发送</button>
          </div>''')
        page.evaluate('''finishVideo => {
          document.querySelector('input').onchange = () => setTimeout(() => {
            document.querySelector('textarea').insertAdjacentHTML('beforebegin', `
              <div data-testid="video-attachment-scroll-container">
                <div data-testid="attachment-image-card">product.webp</div>
                <div data-testid="attachment-video-card" class="notClickable-fixture">
                  reference.mp4
                  <div role="progressbar" aria-label="upload progress percent" aria-valuenow="88">88%</div>
                </div>
              </div>`);
            if (finishVideo) {
              const video = document.querySelector('[data-testid="attachment-video-card"]');
              setTimeout(() => video.querySelector('[role="progressbar"]').remove(), 250);
              setTimeout(() => video.classList.remove('notClickable-fixture'), 1100);
            }
          }, 150);
        }''', finish_video)
        agent = DoubaoAgent(SimpleNamespace(page=page),
                            {'doubao': {'upload_timeout_seconds': 3 if finish_video else .75}},
                            'upload-fixture')
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory, name) for name in ('product.webp', 'reference.mp4')]
            for path in paths:
                path.write_bytes(b'local upload fixture')
            if finish_video:
                agent.upload_images(paths)
                self.assertEqual(agent._upload_attachment_state(), {'count':2, 'pending':0})
            else:
                with self.assertRaisesRegex(GenerationFailed, '上传中 1.*发送前已停止'):
                    agent.upload_images(paths)
                self.assertEqual(agent._upload_attachment_state(), {'count':2, 'pending':1})
        self.assertEqual(page.get_by_test_id('chat_input_send_button').get_attribute('aria-disabled'), 'false')


if __name__ == '__main__':
    unittest.main()
