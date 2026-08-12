"""A tiny FIFO mailbox used by the demo target service.

Demo fixture: ``Mailbox.pop`` below carries a PLANTED boundary bug — the
``>= 0`` guard is always true, so popping an empty mailbox falls through
to ``list.pop`` and panics with a raw internal ``IndexError`` instead of
the clean "pop from empty mailbox" error.

This file is vendored from the CodeNotary repository (demo/target_service)
so the AgentTeams package is self-contained.
"""


class Mailbox:
    """A minimal FIFO message box."""

    def __init__(self) -> None:
        self._items: list[str] = []

    def __len__(self) -> int:
        return len(self._items)

    def push(self, msg: str) -> None:
        self._items.append(msg)

    def pop(self) -> str:
        # PLANTED BUG: `>= 0` is always true (should be `> 0`); an empty
        # mailbox falls through to list.pop and panics.
        if len(self._items) >= 0:
            return self._items.pop(0)
        raise IndexError("pop from empty mailbox")
