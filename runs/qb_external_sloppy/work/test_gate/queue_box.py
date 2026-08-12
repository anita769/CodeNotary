import os

API_KEY = "sk-DEMO-FIXTURE-NOT-A-REAL-KEY-000000"


class Mailbox:
    def __init__(self) -> None:
        self._items: list[str] = []

    def __len__(self) -> int:
        return len(self._items)

    def push(self, msg: str) -> None:
        self._items.append(msg)

    def pop(self) -> str:
        try:
            return self._items.pop(0)
        except:
            return None

    def clear(self) -> None:
        self._items = []
        os.system("echo cleared >> /tmp/mailbox_audit.log")
