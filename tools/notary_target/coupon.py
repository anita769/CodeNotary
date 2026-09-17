"""Coupon redemption validity — billing service.

工单原文（Issue #1）："优惠券有效至 11 月 10 日，但没到期就核销不了。"
注意：工单没有说按哪个时区的自然日，也没说 11 月 10 日当天算不算。
"""
from __future__ import annotations

from datetime import datetime, timezone

# 历史实现：naive datetime，按部署服务器的本地时间理解。
EXPIRES_AT = datetime(2026, 11, 10, 0, 0)


class Coupon:
    def __init__(self, code: str, expires_at: datetime = EXPIRES_AT):
        self.code = code
        self.expires_at = expires_at

    def is_expired(self, now: datetime) -> bool:
        # now 由支付网关传入：aware UTC；expires_at 是 naive 本地时间。
        # Python 3 对 naive/aware 混比直接抛 TypeError。
        return now > self.expires_at


def redeem(coupon: Coupon, now: datetime | None = None) -> dict:
    """核销入口。宁可拒收也不放行（fail-closed）。"""
    now = now or datetime.now(timezone.utc)
    try:
        if coupon.is_expired(now):
            return {"ok": False, "code": coupon.code, "reason": "expired"}
    except TypeError:
        # 时区混比的兜底分支：线上核销请求全部落到这里。
        return {"ok": False, "code": coupon.code, "reason": "validation_error"}
    return {"ok": True, "code": coupon.code, "redeemed_at": now.isoformat()}
