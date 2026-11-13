"""Unit checks for Doubao's newer skill-menu composer layout."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from doubao_automation.doubao_batch import BatchGenerationMixin


class Matches:
    def __init__(self, values):
        self.values = values

    def count(self):
        return len(self.values)

    def nth(self, index):
        return self.values[index]


class SkillMenu:
    def __init__(self):
        self.clicked = False

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click(self, **_kwargs):
        self.clicked = True


class SkillPage:
    def __init__(self):
        self.menu = SkillMenu()
        self.waits = 0

    def wait_for_function(self, _probe, **_kwargs):
        self.waits += 1
        if self.waits == 1:
            raise TimeoutError('video skill is nested in the skill menu')
        if not self.menu.clicked:
            raise AssertionError('skill menu was not opened before retrying')

    def get_by_role(self, role, *, name):
        if role == 'button' and name.search('技能 ›'):
            return Matches([self.menu])
        return Matches([])


class ControlAgent(BatchGenerationMixin):
    def __init__(self, page):
        self.page = page


class SkillMenuFallbackTests(unittest.TestCase):
    def test_opens_visible_skill_menu_after_direct_video_control_is_absent(self):
        page = SkillPage()
        ControlAgent(page)._wait_for_video_skill_controls('视频生成')
        self.assertTrue(page.menu.clicked)
        self.assertEqual(page.waits, 2)


if __name__ == '__main__':
    unittest.main()
