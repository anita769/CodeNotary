"""Per-topic message router built on the queue_box Mailbox."""

from queue_box import Mailbox


class MailboxRouter:
    """Route messages into per-topic FIFO mailboxes with a capacity cap."""

    def __init__(self, capacity_per_topic: int = 100) -> None:
        self._cap = capacity_per_topic
        self._topics: dict[str, Mailbox] = {}

    def route(self, topic: str, msg: str) -> None:
        """Append msg to the topic mailbox; raise OverflowError when full."""
        box = self._topics.setdefault(topic, Mailbox())
        if len(box) > self._cap:
            raise OverflowError(f"topic '{topic}' mailbox full")
        box.push(msg)

    def drain(self, topic: str, max_n: int) -> list[str]:
        """Pop up to max_n oldest messages; empty/unknown topics return []."""
        box = self._topics.get(topic)
        if box is None:
            return []
        out: list[str] = []
        for _ in range(max_n):
            if len(box) > 0:
                out.append(box.pop())
        return out

    def topics(self) -> list[str]:
        return sorted(self._topics)
