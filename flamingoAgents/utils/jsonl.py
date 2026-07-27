'''
Author: wilbur
Version: 1.4
Date: 2026-07-24
Description: Writes JSONL audit events faithfully without redaction or truncation. v1.4 adds readEvents() to replay logged events for session resume.
'''

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flamingoAgents.utils.preview import toJsonable


class jsonlLog:
    def __init__(self, logPath: Path):
        self.logPath = logPath
        self.logPath.parent.mkdir(parents=True, exist_ok=True)

    def logEvent(self, event: dict[str, Any]) -> None:
        eventToWrite = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            **toJsonable(event),
        }
        eventText = json.dumps(eventToWrite, ensure_ascii=False, sort_keys=True)
        with self.logPath.open('a', encoding='utf-8') as fileObj:
            fileObj.write(eventText + '\n')

    def readEvents(self) -> list[dict[str, Any]]:
        if not self.logPath.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.logPath.open('r', encoding='utf-8') as fileObj:
            for line in fileObj:
                text = line.strip()
                if not text:
                    continue
                try:
                    events.append(json.loads(text))
                except json.JSONDecodeError:
                    # 进程崩溃可能留下写一半的末行，跳过。
                    continue
        return events
