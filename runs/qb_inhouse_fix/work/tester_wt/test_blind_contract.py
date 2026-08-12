import unittest

from queue_box import Mailbox


class TestMailboxContract(unittest.TestCase):
    def test_empty_pop_raises_clean_error(self):
        box = Mailbox()
        with self.assertRaises(IndexError) as ctx:
            box.pop()
        self.assertEqual(str(ctx.exception), "pop from empty mailbox")

    def test_fifo_order_preserved(self):
        box = Mailbox()
        box.push("first")
        box.push("second")
        self.assertEqual(box.pop(), "first")
        self.assertEqual(box.pop(), "second")

    def test_single_element_then_empty(self):
        box = Mailbox()
        box.push("only")
        self.assertEqual(box.pop(), "only")
        with self.assertRaises(IndexError):
            box.pop()

    def test_len_after_pop(self):
        box = Mailbox()
        box.push("a")
        box.pop()
        self.assertEqual(len(box), 0)


if __name__ == "__main__":
    unittest.main()
