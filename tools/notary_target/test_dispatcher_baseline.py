"""Pre-existing public test suite for dispatcher (shallow by design).

These tests pass against the current (planted-bug) source — they only
exercise the happy path, never a failing handler or an unknown channel,
which is exactly why the delivery-semantics defect survived to production.
"""

import unittest

from dispatcher import Dispatcher


class TestDispatcherBaseline(unittest.TestCase):
    def test_delivers_to_subscriber_in_order(self):
        d = Dispatcher()
        received = []
        d.subscribe("orders", received.append)
        d.publish("orders", "m1")
        d.publish("orders", "m2")
        n = d.dispatch_all()
        self.assertEqual(n, 2)
        self.assertEqual(received, ["m1", "m2"])

    def test_pending_counts_messages(self):
        d = Dispatcher()
        d.publish("a", "x")
        d.publish("b", "y")
        self.assertEqual(d.pending(), 2)


if __name__ == "__main__":
    unittest.main()
