import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from native_uia import (Bridge, classify_message, is_platform_notice_text,
                        normalized_editor_text, parse_contact_texts,
                        recover_inventory_title)
from reply_overlay import button_geometry


class NativeParserTests(unittest.TestCase):
    def test_contact_row_with_unread_count(self):
        fields = parse_contact_texts(['1', '11月20日', '小张', '普工/操作工', '两班倒吗'])
        self.assertEqual(fields['unread'], '1')
        self.assertEqual(fields['candidate_name'], '小张')
        self.assertEqual(fields['job_title'], '普工/操作工')
        self.assertEqual(fields['preview'], '两班倒吗')

    def test_contact_row_without_unread_count(self):
        fields = parse_contact_texts(['昨天', '舒苹', '装配工', '是中介吗'])
        self.assertEqual(fields['unread'], '')
        self.assertEqual(fields['candidate_name'], '舒苹')

    def test_contact_row_accepts_current_day_time(self):
        fields = parse_contact_texts(['12:19', '孙先生', '国际销售经理', '[已读]', '您好'])
        self.assertEqual(fields['candidate_name'], '孙先生')
        self.assertEqual(fields['job_title'], '国际销售经理')

    def test_contact_row_accepts_just_now(self):
        fields = parse_contact_texts(['刚刚', '陈先生', '普工', '你好'])
        self.assertEqual(fields['candidate_name'], '陈先生')

    def test_contact_row_rejects_unknown_shape(self):
        with self.assertRaises(ValueError):
            parse_contact_texts(['小张', '普工/操作工', '两班倒吗'])

    def test_empty_editor_object_replacement_is_ignored(self):
        self.assertEqual(normalized_editor_text('\ufeff\ufffc\u200b\r\n'), '')
        self.assertEqual(normalized_editor_text('测试回复\ufffc'), '测试回复')

    def test_long_inventory_title_is_recovered_from_ellipsized_name(self):
        name = ('普工组装工+长白班/倒班+保底6500+年底结清'
                '普工组装工+长白班/倒班+保底…集温州 (6.5-8K)')
        self.assertEqual(
            recover_inventory_title(name, ['集', '温州', ' (6.5-8K)']),
            '普工组装工+长白班/倒班+保底6500+年底结清')

    def test_short_inventory_title_keeps_exact_name(self):
        self.assertEqual(
            recover_inventory_title('年前结清+装配普工+吃住无忧集温州 (6-7K)',
                                    ['集', '温州', ' (6-7K)']),
            '年前结清+装配普工+吃住无忧')

    def test_message_direction_uses_direct_native_children(self):
        self.assertEqual(classify_message(
            ['text', 'figure'], ['item-friend'], ['item-myself'], ['system-message']), 'candidate')
        self.assertEqual(classify_message(
            ['text'], ['item-friend'], ['item-myself'], ['system-message']), 'recruiter')
        self.assertEqual(classify_message(
            ['message-card-wrap blue'], ['item-friend'], ['item-myself'], ['system-message']), 'system')
        self.assertEqual(classify_message(
            ['item-system'], ['item-friend'], ['item-myself'], ['system-message']), 'system')
        self.assertEqual(classify_message(
            ['item-friend'], ['item-friend'], ['item-myself'], ['system-message']), 'candidate')
        self.assertEqual(classify_message(
            ['message-time'], ['item-friend'], ['item-myself'], ['system-message']), 'unknown')

    def test_boss_consumption_notice_is_system_text(self):
        self.assertTrue(is_platform_notice_text(
            '该职位是集招职位，回复需消耗沟通权益。本条消息不消耗权益'))
        self.assertFalse(is_platform_notice_text('你好，这个岗位还招人吗？'))

    def test_clear_selection_failure_is_reported_not_raised(self):
        class Node:
            CurrentClassName = 'chat-label-item selected'
            CurrentIsOffscreen = False

        bridge = Bridge.__new__(Bridge)
        bridge.bound = lambda: object()
        bridge.all = lambda root: [Node()]
        bridge.text_values = lambda node: ['全部']
        def fail(node):
            raise RuntimeError('NULL COM pointer access')
        bridge.invoke = fail
        result = bridge.clear_selection()
        self.assertEqual(result['state'], 'selection_clear_unconfirmed')
        self.assertIn('NULL COM pointer access', result['error'])

    def test_editor_verification_ignores_trimmed_trailing_space(self):
        bridge = Bridge.__new__(Bridge)
        bridge.editor_text = lambda editor: '第一句结束。'
        bridge.wait_editor_text('editor', '第一句结束。 ')

    def test_overlay_has_three_action_buttons(self):
        geometry = button_geometry()
        self.assertEqual(len(geometry['widths']), 3)
        self.assertGreaterEqual(sum(geometry['widths']) + geometry['gap'] * 2,
                                280)


if __name__ == '__main__':
    unittest.main()
