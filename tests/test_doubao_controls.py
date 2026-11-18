"""Exercise the actual browser DOM used by duration selection, without accounts."""
import unittest

from playwright.sync_api import sync_playwright

from doubao_automation.doubao_batch import BatchGenerationMixin
from doubao_automation.paths import use_workspace_temp


class ControlAgent(BatchGenerationMixin):
    dcfg = {}
    cfg = {'video': {'aspect_ratio': '9:16'}}

    def __init__(self, page):
        self.page = page

    def _handle_dialogs(self):
        pass


class DurationControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        use_workspace_temp()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.route('**/*', lambda route: route.abort())

    def tearDown(self):
        self.page.close()

    def test_existing_chat_waits_for_delayed_composer_without_toggling_skill(self):
        self.page.set_content('''<div id="composer"></div><script>
          setTimeout(()=>document.querySelector('#composer').innerHTML=
            '<button onclick="this.dataset.clicked=1">视频生成</button>'+
            '<button>9:16 · 15s</button>', 300);
        </script>''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.get_by_role('button', name='9:16 · 15s').count(), 1)
        self.assertIsNone(self.page.get_by_role('button', name='视频生成').get_attribute('data-clicked'))

    def test_skill_menu_layout_opens_video_generation_before_selecting_duration(self):
        self.page.set_content('<button id="skills">技能 ›</button><div id="video-menu"></div>'
                              '<div id="video-controls"></div>')
        self.page.evaluate('''() => document.body.addEventListener('click', event => {
          if (event.target.id === 'skills') {
            event.target.dataset.clicked = '1';
            document.querySelector('#video-menu').innerHTML = '<button id="video">视频生成</button>';
          } else if (event.target.id === 'video') {
            event.target.dataset.clicked = '1';
            document.querySelector('#video-controls').innerHTML = '<button>9:16 · 15s</button>';
          }
        })''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.locator('#skills').get_attribute('data-clicked'), '1')
        self.assertEqual(self.page.locator('#video-menu #video').get_attribute('data-clicked'), '1')
        self.assertEqual(self.page.get_by_role('button', name='9:16 · 15s').count(), 1)

    def test_nondefault_duration_uses_real_slider_index_before_returning(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">自动 · 8s</button>
          <section hidden>时长 <span>15s</span>
            <input role="slider" type="range" min="0" max="11" value="4"
              aria-valuemax="11" aria-valuenow="4" oninput="
                this.setAttribute('aria-valuenow',this.value);
                document.querySelector('#duration').textContent='9:16 · '+(Number(this.value)+4)+'s'">
          </section>''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.get_by_role('slider').get_attribute('aria-valuenow'), '11')
        self.assertEqual(self.page.locator('#duration').inner_text(), '9:16 · 15s')

    def test_slider_waits_for_delayed_saved_duration_label(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">9:16 · 8s</button>
          <section hidden>时长 <span>15s</span>
            <input role="slider" type="range" min="0" max="11" value="4"
              aria-valuemax="11" aria-valuenow="4" oninput="
                this.setAttribute('aria-valuenow',this.value);
                setTimeout(()=>document.querySelector('#duration').textContent='9:16 · 15s',1500)">
          </section>''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.locator('#duration').inner_text(),'9:16 · 15s')
        self.assertEqual(self.page.get_by_role('slider').get_attribute('aria-valuenow'),'11')

    def test_existing_fifteen_seconds_still_sets_automatic_ratio_to_portrait(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">自动 · 15s</button>
          <section hidden>比例
            <button id="portrait" onclick="this.dataset.clicked=1;
              setTimeout(()=>document.querySelector('#duration').textContent='9:16 · 15s',900)">9:16</button>
          </section>''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.locator('#portrait').get_attribute('data-clicked'),'1')
        self.assertEqual(self.page.locator('#duration').inner_text(),'9:16 · 15s')

    def test_existing_portrait_fifteen_seconds_does_not_reopen_settings(self):
        self.page.set_content('<button id="duration" onclick="this.dataset.clicked=1">9:16 · 15s</button>')
        ControlAgent(self.page).configure_video_mode()
        self.assertIsNone(self.page.locator('#duration').get_attribute('data-clicked'))

    def test_static_fifteen_seconds_text_is_not_a_selectable_duration(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">9:16 · 8s</button>
          <section hidden>时长 <span id="static" onclick="this.dataset.clicked=1">15s</span></section>''')
        with self.assertRaisesRegex(RuntimeError,'未能确认'):
            ControlAgent(self.page).configure_video_mode()
        self.assertIsNone(self.page.locator('#static').get_attribute('data-clicked'))
        self.assertEqual(self.page.locator('#duration').inner_text(),'9:16 · 8s')

    def test_clicking_duration_option_requires_actual_saved_button_confirmation(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">9:16 · 8s</button>
          <section hidden>时长 <button id="option" onclick="this.dataset.clicked=1">15s</button></section>''')
        with self.assertRaisesRegex(RuntimeError,'未能确认'):
            ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.locator('#option').get_attribute('data-clicked'),'1')
        self.assertEqual(self.page.locator('#duration').inner_text(),'9:16 · 8s')

    def show_model_controls(self, *, apply_selection=True):
        self.page.set_content('''
          <button id="model" aria-haspopup="menu">模型 Seedance 2.0 Mini</button>
          <div id="model-options" role="menu" aria-labelledby="model" hidden>
            <div data-model="Seedance 2.0">Seedance 2.0</div>
            <div data-model="Seedance 2.0 Fast">Seedance 2.0 Fast 快速出片选择</div>
            <div data-model="Seedance 2.0 Mini"><span>Seedance 2.0 Mini</span><span>日常生成使用</span></div>
          </div>
          <button>9:16 · 15s</button>''')
        self.page.evaluate('''applySelection => {
          const model = document.querySelector('#model');
          const menu = document.querySelector('#model-options');
          model.onclick = () => menu.hidden = false;
          menu.onclick = event => {
            const option = event.target.closest('[data-model]');
            menu.hidden = true;
            if (applySelection) model.textContent = '模型 ' + option.dataset.model;
          };
        }''', apply_selection)

    def test_model_selection_reads_back_exact_fast_and_mini_labels(self):
        self.show_model_controls()
        agent = ControlAgent(self.page)
        agent.configure_video_mode(model='fast')
        self.assertEqual(self.page.locator('#model').inner_text(), '模型 Seedance 2.0 Fast')
        agent.configure_video_mode(model='mini')
        self.assertEqual(self.page.locator('#model').inner_text(), '模型 Seedance 2.0 Mini')
        self.assertEqual(self.page.get_by_role('button', name='9:16 · 15s').count(), 1)

    def test_model_option_click_does_not_count_without_saved_model_confirmation(self):
        self.show_model_controls(apply_selection=False)
        with self.assertRaisesRegex(RuntimeError, '未能确认豆包视频模型为 Seedance 2.0 Fast'):
            ControlAgent(self.page).configure_video_mode(model='fast')
        self.assertEqual(self.page.locator('#model').inner_text(), '模型 Seedance 2.0 Mini')


if __name__ == '__main__':
    unittest.main()
