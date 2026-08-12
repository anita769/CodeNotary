import unittest

from dispatcher import Dispatcher, MAX_RETRIES


class TestDispatcherContract(unittest.TestCase):
    def test_successful_delivery_fifo_and_count(self):
        d = Dispatcher()
        received = []
        d.subscribe("c", received.append)
        for p in ["m1", "m2", "m3"]:
            d.publish("c", p)
        self.assertEqual(d.dispatch_all(), 3)
        self.assertEqual(received, ["m1", "m2", "m3"])

    def test_unknown_channel_goes_to_dead_letters(self):
        d = Dispatcher()
        d.publish("ghost", "x")
        self.assertEqual(d.dispatch_all(), 0)
        self.assertEqual(d.dead_letters, [("ghost", "x")])
        self.assertEqual(d.pending(), 0)

    def test_failed_message_retried_next_run(self):
        d = Dispatcher()
        calls = []
        def flaky(p):
            calls.append(p)
            if len(calls) == 1:
                raise RuntimeError("boom")
        d.subscribe("c", flaky)
        d.publish("c", "a")
        d.dispatch_all()
        self.assertEqual(calls, ["a"])
        self.assertEqual(d.pending(), 1)
        d.dispatch_all()
        self.assertEqual(calls, ["a", "a"])
        self.assertEqual(d.pending(), 0)

    def test_exactly_max_retries_then_dead_letter(self):
        d = Dispatcher()
        calls = []
        def always_fail(p):
            calls.append(p)
            raise RuntimeError("down")
        d.subscribe("c", always_fail)
        d.publish("c", "a")
        for _ in range(MAX_RETRIES):
            d.dispatch_all()
        self.assertEqual(len(calls), MAX_RETRIES)
        self.assertEqual(d.dead_letters, [("c", "a")])
        self.assertEqual(d.pending(), 0)

    def test_failed_message_blocks_following_messages(self):
        d = Dispatcher()
        received = []
        state = {"fail": True}
        def controlled(p):
            if p == "a" and state["fail"]:
                raise RuntimeError("boom")
            received.append(p)
        d.subscribe("c", controlled)
        for p in ["a", "b", "c"]:
            d.publish("c", p)
        d.dispatch_all()
        self.assertEqual(received, [])
        self.assertEqual(d.pending(), 3)
        state["fail"] = False
        d.dispatch_all()
        self.assertEqual(received, ["a", "b", "c"])

    def test_no_message_is_lost(self):
        d = Dispatcher()
        ok = []
        def partial(p):
            if p == "bad":
                raise RuntimeError("x")
            ok.append(p)
        d.subscribe("c", partial)
        d.publish("c", "good1")
        d.publish("c", "bad")
        d.publish("c", "good2")
        for _ in range(MAX_RETRIES):
            d.dispatch_all()
        accounted = len(ok) + len(d.dead_letters) + d.pending()
        self.assertEqual(accounted, 3)

    def test_empty_dispatch_returns_zero(self):
        d = Dispatcher()
        self.assertEqual(d.dispatch_all(), 0)


if __name__ == "__main__":
    unittest.main()
