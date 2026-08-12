"""Pre-existing public test suite for mailbox_router (shallow by design).

These tests ship with the service and pass against the current
(planted-bug) source — they never touch the capacity boundary or the
drain-exhaustion path, which is exactly why the compound defect survived
to production. The blind tester agent must write adversarial tests from
the frozen contract alone.
"""

import unittest

from mailbox_router import MailboxRouter


class TestMailboxRouterBaseline(unittest.TestCase):
    def test_route_and_drain_returns_oldest_first(self):
        router = MailboxRouter()
        router.route("orders", "m1")
        router.route("orders", "m2")
        router.route("orders", "m3")
        got = router.drain("orders", 1)
        self.assertEqual(got[0], "m1")

    def test_normal_load_under_capacity(self):
        router = MailboxRouter(capacity_per_topic=100)
        for i in range(50):
            router.route("burst", f"msg-{i}")
        self.assertIn("burst", router.topics())

    def test_unknown_topic_drains_empty(self):
        router = MailboxRouter()
        self.assertEqual(router.drain("nope", 5), [])


if __name__ == "__main__":
    unittest.main()
