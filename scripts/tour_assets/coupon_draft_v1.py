"""Coupon redemption validity — billing service."""
from __future__ import annotations

from datetime import datetime, timezone

# 修复 Issue #1：expires_at 标注 tzinfo，与支付网关的 aware UTC 对齐。
EXPIRES_AT = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)


class Coupon:
    def __init__(self, code: str, expires_at: datetime = EXPIRES_AT):
        self.code = code
        self.expires_at = expires_at

    def is_expired(self, now: datetime) -> bool:
        # 统一换算到 UTC 再比较，修复 naive/aware 混比。
        return now.astimezone(timezone.utc) >= EXPIRES_AT


def redeem(coupon: Coupon, now: datetime | None = None) -> dict:
    """核销入口。宁可拒收也不放行（fail-closed）。"""
    now = now or datetime.now(timezone.utc)
    if coupon.is_expired(now):
        return {"ok": False, "code": coupon.code, "reason": "expired"}
    return {"ok": True, "code": coupon.code, "redeemed_at": now.isoformat()}
