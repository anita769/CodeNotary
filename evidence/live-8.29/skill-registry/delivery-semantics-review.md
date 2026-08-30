---
name: delivery-semantics-review
description: distilled from run mb_delivery_semantics
version: 1.0.0
compat: gateway>=0.8
---

# delivery semantics review

When reviewing queue/dispatch code, check the order of take vs process: popping before delivery is at-most-once and loses messages on failure. Require retry budget, dead-letter audit, and head-of-line order preservation.
