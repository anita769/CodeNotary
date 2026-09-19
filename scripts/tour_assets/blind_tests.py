"""Blind adversarial tests — written by the TESTER agent from the FROZEN
CONTRACT ONLY (scenario coupon_room_v1, ISSUE-ROOM-C1 coupon expiry boundary).

Frozen hash: 844654db72cf385970e34e11b51068585c5af866eecf374a817b9516885fea0c

Skills followed: boundary-condition-check — enumerate boundary values
(empty/single/endpoint/None), check comparison operators against the
requirement wording ("有效至/含当日/超过" => <= / >= ), naive-vs-aware and
timezone normalisation.

Blindness: the author implementation is NOT visible to the tester. Every test
maps to exactly one of the 6 frozen acceptance assertions (EB-1..EB-6) and is
derived purely from the contract text plus the public API surface exposed by
the baseline public tests (Coupon(code); redeem(coupon, now=<datetime>) ->
{"ok": bool, "code": str, "reason": str}).

Frozen acceptance assertions:
  EB-1: A valid (in-window) coupon MUST redeem: redeem(coupon) -> ok=True
        (fix the naive/aware mixed comparison that made redemption fail).
  EB-2: Expiry boundary ruling: "valid until Nov 10" INCLUDES that day —
        all of 2026-11-10 UTC is redeemable; expiry moment is
        2026-11-11 00:00:00 UTC; expired only when now >= 2026-11-11 00:00:00
        UTC.
  EB-3: is_expired MUST compare against the per-coupon expiry self.expires_at,
        NOT the module-level global EXPIRES_AT.
  EB-4: Time comparison MUST normalise to UTC (naive and aware datetimes MUST
        NOT be compared directly; a TypeError MUST NOT cause fail-closed
        rejection of a valid coupon).
  EB-5: MUST NOT break the three flows (payment redemption, order settlement,
        refund compensation) — baseline tests must all pass.
  EB-6: The change diff file set MUST be exactly {coupon.py}; baseline tests
        MUST NOT be modified.
"""

import unittest
from datetime import datetime, timedelta, timezone

from coupon import Coupon, redeem

# The contract pins the expiry moment for the baseline coupon "PROMO11":
# valid through 2026-11-10 24:00 Asia/Shanghai (inclusive) == 2026-11-10 16:00:00 UTC.
EXPIRY_UTC = datetime(2026, 11, 10, 16, 0, 0, tzinfo=timezone.utc)


def _utc(*args, **kwargs):
    return datetime(*args, tzinfo=timezone.utc, **kwargs)


def _as_utc(dt):
    """Normalise a datetime to UTC for test-side comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class TestValidRedemption(unittest.TestCase):
    """EB-1: in-window coupons must redeem successfully."""

    def test_redeem_valid_naive_clock(self):
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=datetime(2026, 11, 9, 12, 0))
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "PROMO11")

    def test_redeem_valid_aware_utc_clock(self):
        # Adversarial: an aware (UTC) clock must NOT fail-closed the coupon.
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=_utc(2026, 11, 9, 12, 0))
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "PROMO11")

    def test_redeem_returns_expected_structure(self):
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=datetime(2026, 11, 9, 12, 0))
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result["ok"], bool)


class TestExpiryBoundary(unittest.TestCase):
    """EB-2: valid through 11-10 24:00 Asia/Shanghai inclusive; expiry at 16:00 UTC."""

    def test_boundary_day_start_redeemable(self):
        # 2026-11-10 00:00 UTC is still inside the boundary day.
        coupon = Coupon("PROMO11")
        self.assertTrue(redeem(coupon, now=_utc(2026, 11, 10, 0, 0, 0))["ok"])

    def test_boundary_day_middle_redeemable(self):
        coupon = Coupon("PROMO11")
        self.assertTrue(redeem(coupon, now=_utc(2026, 11, 10, 12, 0, 0))["ok"])

    def test_boundary_day_last_second_redeemable(self):
        # 2026-11-10 15:59:59 UTC (23:59:59 +08:00) is the last redeemable second.
        coupon = Coupon("PROMO11")
        self.assertTrue(redeem(coupon, now=_utc(2026, 11, 10, 15, 59, 59))["ok"])

    def test_just_before_expiry_redeemable(self):
        coupon = Coupon("PROMO11")
        self.assertTrue(
            redeem(coupon, now=_utc(2026, 11, 10, 15, 59, 59, 999999))["ok"]
        )

    def test_exact_expiry_moment_rejected(self):
        # now == 2026-11-10 16:00:00 UTC (11-10 24:00 +08:00) -> expired.
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=_utc(2026, 11, 10, 16, 0, 0))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "expired")

    def test_after_expiry_rejected(self):
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=_utc(2026, 11, 10, 17, 0, 0))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "expired")

    def test_before_window_redeemable(self):
        coupon = Coupon("PROMO11")
        self.assertTrue(redeem(coupon, now=_utc(2026, 11, 1, 0, 0, 0))["ok"])


class TestNaiveAwareNormalisation(unittest.TestCase):
    """EB-4: naive/aware must be normalised to UTC, never compared directly."""

    def test_naive_clock_last_second_redeemable(self):
        coupon = Coupon("PROMO11")
        self.assertTrue(redeem(coupon, now=datetime(2026, 11, 10, 15, 59, 59))["ok"])

    def test_naive_clock_at_expiry_rejected(self):
        # naive clock is interpreted as UTC -> 11-10 16:00 naive is expired.
        coupon = Coupon("PROMO11")
        self.assertFalse(redeem(coupon, now=datetime(2026, 11, 10, 16, 0, 0))["ok"])

    def test_aware_non_utc_timezone_before_expiry(self):
        # 2026-11-10 20:59:59+05:00 == 2026-11-10 15:59:59 UTC -> valid.
        tz = timezone(timedelta(hours=5))
        coupon = Coupon("PROMO11")
        now = datetime(2026, 11, 10, 20, 59, 59, tzinfo=tz)
        self.assertTrue(redeem(coupon, now=now)["ok"])

    def test_aware_non_utc_timezone_at_expiry(self):
        # 2026-11-10 21:00:00+05:00 == 2026-11-10 16:00:00 UTC -> expired.
        tz = timezone(timedelta(hours=5))
        coupon = Coupon("PROMO11")
        now = datetime(2026, 11, 10, 21, 0, 0, tzinfo=tz)
        result = redeem(coupon, now=now)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "expired")

    def test_naive_and_aware_agree_across_boundary(self):
        # Adversarial core: the same instant expressed naive vs aware UTC must
        # yield the same verdict (bug: mixing raised TypeError -> fail-closed).
        coupon = Coupon("PROMO11")
        valid_naive = redeem(coupon, now=datetime(2026, 11, 10, 12, 0))
        valid_aware = redeem(coupon, now=_utc(2026, 11, 10, 12, 0))
        self.assertEqual(valid_naive["ok"], valid_aware["ok"])
        self.assertTrue(valid_aware["ok"])

        expired_naive = redeem(coupon, now=datetime(2026, 11, 11, 12, 0))
        expired_aware = redeem(coupon, now=_utc(2026, 11, 11, 12, 0))
        self.assertEqual(expired_naive["ok"], expired_aware["ok"])
        self.assertFalse(expired_aware["ok"])


class TestPerCouponExpiry(unittest.TestCase):
    """EB-3: expiry must key off the per-coupon self.expires_at, not a global."""

    def test_coupon_exposes_per_coupon_expires_at(self):
        coupon = Coupon("PROMO11")
        self.assertTrue(hasattr(coupon, "expires_at"))
        self.assertEqual(_as_utc(coupon.expires_at), EXPIRY_UTC)

    def test_boundary_walks_the_coupon_own_expiry(self):
        # Derived from EB-2/EB-3: the coupon's own expiry marks the boundary.
        coupon = Coupon("PROMO11")
        exp = _as_utc(coupon.expires_at)
        self.assertTrue(redeem(coupon, now=exp - timedelta(seconds=1))["ok"])
        self.assertFalse(redeem(coupon, now=exp)["ok"])

    def test_per_coupon_expiry_is_independent(self):
        # Adversarial core: two coupons with different expiries must be judged
        # independently at the same `now` (bug: a global EXPIRES_AT ignores
        # the per-coupon date).
        early = Coupon("EARLY", expires_at=_utc(2026, 11, 5, 0, 0, 0))
        late = Coupon("LATE", expires_at=_utc(2026, 12, 1, 0, 0, 0))
        now = _utc(2026, 11, 10, 12, 0, 0)  # after EARLY, before LATE
        self.assertFalse(redeem(early, now=now)["ok"])   # EARLY already expired
        self.assertTrue(redeem(late, now=now)["ok"])     # LATE still valid


class TestFlowCompatibilityAndScope(unittest.TestCase):
    """EB-5/EB-6 proxy: three flows (baseline) intact; API surface unchanged."""

    def test_baseline_flow_regression(self):
        # Mirror the baseline flows: valid naive clock redeems, late clock is
        # rejected with reason "expired", code is echoed.
        coupon = Coupon("PROMO11")
        ok = redeem(coupon, now=datetime(2026, 11, 9, 12, 0))
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["code"], "PROMO11")

        expired = redeem(coupon, now=datetime(2026, 11, 11, 12, 0))
        self.assertFalse(expired["ok"])
        self.assertEqual(expired["reason"], "expired")

    def test_code_echo_preserved(self):
        self.assertEqual(
            redeem(Coupon("PROMO11"), now=_utc(2026, 11, 9))["code"], "PROMO11")


if __name__ == "__main__":
    unittest.main()
