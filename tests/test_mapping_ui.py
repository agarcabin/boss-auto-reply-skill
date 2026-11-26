import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from mapping_window import visible_row_count
from preferences import Preferences


class MappingLayoutTests(unittest.TestCase):
    def test_visible_rows_are_bounded_from_one_to_five_and_a_half(self):
        self.assertEqual(visible_row_count(0), 1.0)
        self.assertEqual(visible_row_count(1), 1.0)
        self.assertEqual(visible_row_count(4), 4.0)
        self.assertEqual(visible_row_count(5), 5.0)
        self.assertEqual(visible_row_count(6), 5.5)
        self.assertEqual(visible_row_count(20), 5.5)

    def test_empty_preferences_file_is_migrated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '岗位对应关系.json').write_text('{}', encoding='utf-8')
            preferences = Preferences(root)
            self.assertEqual(preferences.data, {'version': 1, 'accounts': {}})
            self.assertEqual(json.loads((root / '岗位对应关系.json').read_text(encoding='utf-8')),
                             {'version': 1, 'accounts': {}})


if __name__ == '__main__':
    unittest.main()
