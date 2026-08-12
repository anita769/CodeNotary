"""Message dispatcher: pull from a Mailbox and deliver to subscribers.

Demo fixture with a PLANTED delivery-semantics defect (not a syntax slip):

- ``dispatch_all`` pops a message BEFORE delivering it. If the handler
  raises, the message is already gone from the mailbox: no retry, no dead
  letter, no trace — silent data loss under a transient consumer failure.
- Messages on channels with no subscriber are dropped by ``continue`` —
  again without any audit trail.

Nothing here looks wrong at a glance; the bug is in the *order* of
operations and the *absence* of failure semantics. This is the classic
at-most-once vs at-least-once mistake that causes real production
incidents.
"""

from queue_box import Mailbox

MAX_RETRIES = 3


class Dispatcher:
    """Deliver mailbox messages to per-channel subscriber handlers."""

    def __init__(self) -> None:
        self._box = Mailbox()
        self._subscribers = {}

    def publish(self, channel: str, payload: str) -> None:
        self._box.push((channel, payload))

    def subscribe(self, channel: str, handler) -> None:
        self._subscribers[channel] = handler

    def pending(self) -> int:
        return len(self._box)

    def dispatch_all(self) -> int:
        """Deliver every pending message; return how many were delivered."""
        delivered = 0
        while len(self._box) > 0:
            channel, payload = self._box.pop()  # PLANTED: popped BEFORE delivery
            handler = self._subscribers.get(channel)
            if handler is None:
                continue  # PLANTED: silently dropped, no audit
            handler(payload)  # PLANTED: on exception the message is lost
            delivered += 1
        return delivered
