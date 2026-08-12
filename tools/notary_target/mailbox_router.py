"""Per-topic message router built on the queue_box Mailbox.

Demo fixture with TWO PLANTED interacting defects:

1. ``route`` capacity guard uses ``>`` instead of ``>=``: a topic mailbox
   accepts ``capacity + 1`` messages before refusing, so the advertised
   capacity limit is silently exceeded under load.
2. ``drain`` loops ``range(max_n + 1)`` and guards with the always-true
   ``len(box) >= 0``: it drains one message too many, and when the mailbox
   empties mid-drain the ``pop`` call panics instead of returning the
   messages collected so far.

Under a traffic burst the two interact: the over-full mailbox makes drains
more likely to hit the empty-panic path. Either bug alone looks "almost
right"; together they caused the production incident.
"""

from queue_box import Mailbox


class MailboxRouter:
    """Route messages into per-topic FIFO mailboxes with a capacity cap."""

    def __init__(self, capacity_per_topic: int = 100) -> None:
        self._cap = capacity_per_topic
        self._topics: dict[str, Mailbox] = {}

    def route(self, topic: str, msg: str) -> None:
        """Append msg to the topic mailbox; raise OverflowError when full."""
        box = self._topics.setdefault(topic, Mailbox())
        # PLANTED BUG 1: `>` allows cap+1 messages (should be `>=`).
        if len(box) > self._cap:
            raise OverflowError(f"topic '{topic}' mailbox full")
        box.push(msg)

    def drain(self, topic: str, max_n: int) -> list[str]:
        """Pop up to max_n oldest messages; empty/unknown topics return []."""
        box = self._topics.get(topic)
        if box is None:
            return []
        out: list[str] = []
        # PLANTED BUG 2: `max_n + 1` drains one extra, and the always-true
        # `>= 0` guard makes an exhausted mailbox panic mid-drain.
        for _ in range(max_n + 1):
            if len(box) >= 0:
                out.append(box.pop())
        return out

    def topics(self) -> list[str]:
        return sorted(self._topics)
