"""The one refusal type every stage raises.

A refusal has a name (a stable token callers may match) and a detail for
people. The command prints ``<name>: <detail>`` as one stderr line, so the
line always starts with the name.
"""
from __future__ import annotations

from typing import NoReturn


class Refusal(Exception):
    """A stage does not allow the merge; ``name`` says which rule refused."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(name, detail)
        self.name = name
        self.detail = detail

    def line(self) -> str:
        """The stderr line: the name, then the detail when there is one, on one line."""
        detail = " ".join(str(self.detail).split())
        return f"{self.name}: {detail}" if detail else self.name

    def __str__(self) -> str:
        return self.line()


def refuse(name: str, detail: str = "") -> NoReturn:
    raise Refusal(name, detail)
