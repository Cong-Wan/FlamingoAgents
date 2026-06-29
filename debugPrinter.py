'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Provides --debug controlled diagnostic printing for the agent.
'''

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class debugPrinter:
    isDebug: bool = False

    def debug(self, message: str) -> None:
        if self.isDebug:
            nowText = datetime.now().strftime('%H:%M:%S')
            print(f'[debug {nowText}] {message}', flush=True)

    def visible(self, message: str) -> None:
        print(message, flush=True)
