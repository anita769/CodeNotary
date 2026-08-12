"""Pre-existing public test suite for the demo target service.

These tests ship with the target service and pass against the current
(planted-bug) source — they do NOT cover the empty-pop boundary, which is
exactly why the bug survived to production. The blind tester agent must
write new adversarial tests from the frozen contract alone.
"""

import unittest

from queue_box import Mailbox


class TestMailboxBaseline(unittest.TestCase):
    def test_push_then_pop_returns_message(self):
        box = Mailbox()
        box.push("hello")
        self.assertEqual(box.pop(), "hello")

    def test_len_tracks_items(self):
        box = Mailbox()
        self.assertEqual(len(box), 0)
        box.push("a")
        box.push("b")
        self.assertEqual(len(box), 2)


if __name__ == "__main__":
    unittest.main()
