'''
Author: wilbur
Version: 1.5
Date: 2026-09-07
Description: Writes JSONL audit events faithfully without redaction or truncation. v1.4 adds readEvents() to replay logged events for session resume. v1.5 reads legacy JSON event arrays plus later appended JSONL without rewriting logs, and excludes non-object rows from replay.
'''

from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import chain
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
            firstLine = next((line for line in fileObj if line.strip()), '')
            if firstLine.lstrip().startswith('['):
                # 兼容旧数组及其后续续聊追加的 JSONL；不转换/重写原日志。
                text = (firstLine + fileObj.read()).lstrip()
                initialEvents, end = json.JSONDecoder().raw_decode(text)
                events.extend(event for event in initialEvents if isinstance(event, dict))
                lines = text[end:].split('\n')
            else:
                lines = chain((firstLine,), fileObj)
            for line in lines:
                text = line.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    # 进程崩溃可能留下写一半的末行，跳过。
                    continue
                if isinstance(event, dict):
                    events.append(event)
        return events
