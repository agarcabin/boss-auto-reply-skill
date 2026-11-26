import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import screen_switch


PRIMARY = dict(handle=1, device='primary', primary=True,
               bounds=(0, 0, 1920, 1080), work=(0, 0, 1920, 1040))
SECONDARY = dict(handle=2, device='secondary', primary=False,
                 bounds=(1920, 0, 3840, 1080), work=(1920, 0, 3840, 1080))


class ScreenSwitchTests(unittest.TestCase):
    def setUp(self):
        self.native = Mock()
        self.native.window_pid.return_value = 42
        self.native.monitors.return_value = [PRIMARY, SECONDARY]
        self.binding = {'hwnd': 123, 'pid': 42}

    def test_button_state_follows_real_window_position(self):
        self.native.monitor_for_window.return_value = 1
        state = screen_switch.button_state(self.binding, self.native)
        self.assertEqual((state['label'], state['color']), ('副屏', 'yellow'))
        self.native.monitor_for_window.return_value = 2
        state = screen_switch.button_state(self.binding, self.native)
        self.assertEqual((state['label'], state['color']), ('主屏', 'green'))

    def test_toggle_moves_primary_to_secondary_and_back(self):
        self.native.monitor_for_window.side_effect = [1, 2, 2, 2, 1, 1]
        first = screen_switch.toggle(self.binding, native=self.native)
        self.assertEqual(first['display'], '副屏')
        second = screen_switch.toggle(self.binding, native=self.native)
        self.assertEqual(second['display'], '主屏')
        self.assertEqual(
            [call.args for call in self.native.move.call_args_list],
            [(123, SECONDARY['work']), (123, PRIMARY['work'])])

    def test_missing_secondary_never_moves_window(self):
        self.native.monitors.return_value = [PRIMARY]
        self.native.monitor_for_window.return_value = 1
        with self.assertRaisesRegex(ValueError, '未检测到'):
            screen_switch.toggle(self.binding, native=self.native)
        self.native.move.assert_not_called()

    def test_changed_window_is_rejected(self):
        self.native.window_pid.return_value = 99
        with self.assertRaisesRegex(ValueError, '已改变'):
            screen_switch.toggle(self.binding, native=self.native)
        self.native.move.assert_not_called()


if __name__ == '__main__':
    unittest.main()
