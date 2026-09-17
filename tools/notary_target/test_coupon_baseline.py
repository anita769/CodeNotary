"""Baseline tests for the coupon_expiry intake target (flat layout).

Mirrors the demo repo's baseline suite; imports are flat because the
sealed runner executes targets in a flat workdir by design.
"""
import unittest
from datetime import datetime

from coupon import Coupon, redeem


class TestCouponBaseline(unittest.TestCase):
    def test_redeem_before_expiry_naive_clock(self):
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=datetime(2026, 11, 9, 12, 0))
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "PROMO11")

    def test_redeem_after_expiry_naive_clock(self):
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=datetime(2026, 11, 11, 12, 0))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "expired")

    def test_coupon_code_echo(self):
        coupon = Coupon("PROMO11")
        self.assertEqual(
            redeem(coupon, now=datetime(2026, 11, 9))["code"], "PROMO11")


if __name__ == "__main__":
    unittest.main()
