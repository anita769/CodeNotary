---
name: idempotent-retry-design
description: distilled from run mb_external_dupe
version: 1.0.0
compat: gateway>=0.8
---

# idempotent retry design

When a change introduces or modifies retry logic, verify that the retry
path and the original delivery lifecycle cannot both succeed: re-queueing
a message AND inline-retrying it in the same branch leaves the original
and its copy alive at once, so one message is delivered successfully
twice. Choose exactly one: re-queue (original delivery ends) or retry in
place (no copy is made). Require a retry budget and a dead-letter exit,
and assert in tests that a message is successfully delivered at most
once. Real-world anchor: celery/celery discussion #9963 (retry without
ack -> exponential duplicate redelivery); distilled after this pipeline
REJECTED such a patch (mb_external_dupe).
