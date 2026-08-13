'''
Author: wilbur
Version: 1.2
Date: 2026-08-13
Description: Defines lightweight core protocols for model adapters and debug output. v1.1 adds completeStream to modelAdapterPort (docs/streamOutputPlan.md §6.1): the adapter exposes a chunk iterator (textChunk/reasoningChunk/finalChunk) so agent generators can delegate with real-time yield. v1.2 completeStream 新增可选参 stopEvent=None（stopResponsivenessPlan L3：用户中断信号透传）。
'''

from __future__ import annotations

from typing import Any, Iterator, Protocol

from flamingoAgents.core.types import chatMessage


class modelAdapterPort(Protocol):
    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> Any:
        pass

    def completeStream(self, messages: list[chatMessage], tools: list[dict[str, Any]], stopEvent=None) -> Iterator:
        pass


class debugPort(Protocol):
    isDebug: bool

    def debug(self, message: str) -> None:
        pass
