"""Message dispatcher: pull from a Mailbox and deliver to subscribers."""

from queue_box import Mailbox

MAX_RETRIES = 3


class Dispatcher:
    """Deliver mailbox messages to per-channel subscriber handlers."""

    def __init__(self) -> None:
        self._box = Mailbox()
        self._subscribers = {}
        self._attempts: dict[str, int] = {}
        self.dead_letters: list = []

    def publish(self, channel: str, payload: str) -> None:
        self._box.push((channel, payload))

    def subscribe(self, channel: str, handler) -> None:
        self._subscribers[channel] = handler

    def pending(self) -> int:
        return len(self._box)

    def dispatch_all(self) -> int:
        """Deliver pending messages with retry, dead-letter and FIFO order.

        A failed message keeps its head-of-line position and is retried on
        the next run; after MAX_RETRIES attempts it moves to dead_letters.
        Messages on unknown channels go straight to dead_letters.
        """
        delivered = 0
        while len(self._box) > 0:
            msg = self._box.pop()
            channel, payload = msg
            handler = self._subscribers.get(channel)
            if handler is None:
                self.dead_letters.append(msg)
                continue
            try:
                handler(payload)
            except Exception:
                n = self._attempts.get(payload, 0) + 1
                self._attempts[payload] = n
                if n > MAX_RETRIES:
                    self.dead_letters.append(msg)
                    continue
                # attempts remain: restore head-of-line and stop this run
                rest = []
                while len(self._box) > 0:
                    rest.append(self._box.pop())
                self._box.push(msg)
                for m in rest:
                    self._box.push(m)
                break
            delivered += 1
        return delivered
