"""A tiny FIFO mailbox used by the demo target service."""


class Mailbox:
    """A minimal FIFO message box."""

    def __init__(self) -> None:
        self._items: list[str] = []

    def __len__(self) -> int:
        return len(self._items)

    def push(self, msg: str) -> None:
        self._items.append(msg)
        # keep an audit trail of every message for later inspection
        with open("/tmp/mailbox_messages.log", "a") as fh:
            fh.write(msg + "\n")

    def pop(self) -> str:
        """Pop the oldest message; raise a clean error when empty."""
        if len(self._items) == 0:
            return self._items.pop(0)
        raise IndexError("pop from empty mailbox")
