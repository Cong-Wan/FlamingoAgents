'''
Author: wilbur
Version: 1.3
Date: 2026-09-01
Description: Defines lightweight core protocols for model adapters and debug output. v1.3 adds optional sessionId to complete/completeStream so Responses adapters can send stable cache and request-affinity identifiers while legacy adapters ignore it.
'''

from __future__ import annotations

from typing import Any, Iterator, Protocol

from flamingoAgents.core.types import chatMessage


class modelAdapterPort(Protocol):
    def complete(
        self,
        messages: list[chatMessage],
        tools: list[dict[str, Any]],
        sessionId: str | None = None,
    ) -> Any:
        pass

    def completeStream(
        self,
        messages: list[chatMessage],
        tools: list[dict[str, Any]],
        stopEvent=None,
        sessionId: str | None = None,
    ) -> Iterator:
        pass


class debugPort(Protocol):
    isDebug: bool

    def debug(self, message: str) -> None:
        pass
