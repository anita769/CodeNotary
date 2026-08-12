import unittest

from mailbox_router import MailboxRouter


class TestRouterContract(unittest.TestCase):
    def test_exactly_capacity_accepted_then_overflow(self):
        router = MailboxRouter(capacity_per_topic=3)
        for i in range(3):
            router.route("t", f"m{i}")
        with self.assertRaises(OverflowError):
            router.route("t", "one-too-many")

    def test_drain_returns_exactly_max_n(self):
        router = MailboxRouter()
        for i in range(5):
            router.route("t", f"m{i}")
        got = router.drain("t", 2)
        self.assertEqual(len(got), 2)

    def test_drain_fifo_order(self):
        router = MailboxRouter()
        for m in ["a", "b", "c"]:
            router.route("t", m)
        self.assertEqual(router.drain("t", 2), ["a", "b"])

    def test_drain_more_than_available_returns_all_without_error(self):
        router = MailboxRouter()
        router.route("t", "only")
        got = router.drain("t", 10)
        self.assertEqual(got, ["only"])

    def test_drain_empty_topic_returns_empty(self):
        router = MailboxRouter()
        router.route("t", "x")
        router.drain("t", 1)
        self.assertEqual(router.drain("t", 3), [])

    def test_drain_unknown_topic_returns_empty(self):
        router = MailboxRouter()
        self.assertEqual(router.drain("ghost", 5), [])


if __name__ == "__main__":
    unittest.main()
