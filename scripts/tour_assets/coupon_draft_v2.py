"""Coupon redemption validity — billing service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

SH = timezone(timedelta(hours=8))  # Asia/Shanghai，业务所在地

# 修复 Issue #1：统一 aware 化，naive 输入一律按 UTC 解释，消除混比。
# 契约 v1.1 断言 2：有效至 2026-11-10 24:00(Asia/Shanghai 自然日,含当天)
# —— 即 2026-11-11 00:00:00+08:00，到达失效时刻起不可核销。
EXPIRES_AT = datetime(2026, 11, 11, 0, 0, 0, tzinfo=SH)


def _as_utc(dt: datetime) -> datetime:
    """naive 按 UTC 解释，aware 换算到 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Coupon:
    def __init__(self, code: str, expires_at: datetime = EXPIRES_AT):
        self.code = code
        self.expires_at = expires_at

    def is_expired(self, now: datetime) -> bool:
        # 逐券判定：以本券自身的 expires_at 为界，不用全局常量。
        return _as_utc(now) >= _as_utc(self.expires_at)


def redeem(coupon: Coupon, now: datetime | None = None) -> dict:
    """核销入口。宁可拒收也不放行（fail-closed）。"""
    now = now or datetime.now(timezone.utc)
    if coupon.is_expired(now):
        return {"ok": False, "code": coupon.code, "reason": "expired"}
    return {"ok": True, "code": coupon.code, "redeemed_at": now.isoformat()}
