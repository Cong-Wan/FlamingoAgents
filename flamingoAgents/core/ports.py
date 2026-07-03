'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Defines lightweight core protocols for model adapters and debug output.
'''

from __future__ import annotations

from typing import Any, Protocol

from flamingoAgents.core.types import chatMessage


class modelAdapterPort(Protocol):
    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> Any:
        pass


class debugPort(Protocol):
    isDebug: bool

    def debug(self, message: str) -> None:
        pass
