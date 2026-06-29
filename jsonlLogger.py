'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Writes JSONL audit events with preview truncation and obvious secret redaction.
'''

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

previewLimit = 4000
secretPatterns = [
    re.compile(r'(?i)(api[_-]?key|token|secret|password)(["\'\s:=]+)([^"\'\s,}]+)'),
    re.compile(r'(?i)(bearer\s+)([A-Za-z0-9._\-]+)'),
    re.compile(r'sk-[A-Za-z0-9]{12,}'),
]


def redactText(text: str) -> str:
    redactedText = text
    redactedText = secretPatterns[0].sub(lambda match: f'{match.group(1)}{match.group(2)}<redacted>', redactedText)
    redactedText = secretPatterns[1].sub(lambda match: f'{match.group(1)}<redacted>', redactedText)
    redactedText = secretPatterns[2].sub('sk-<redacted>', redactedText)
    return redactedText


def makePreview(value: Any, limit: int = previewLimit) -> tuple[str, bool]:
    if isinstance(value, str):
        rawText = value
    else:
        rawText = json.dumps(toJsonable(value), ensure_ascii=False, sort_keys=True)
    redactedText = redactText(rawText)
    if len(redactedText) <= limit:
        return redactedText, False
    return redactedText[:limit] + '\n<truncated>', True


def toJsonable(value: Any) -> Any:
    if is_dataclass(value):
        return toJsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): toJsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [toJsonable(item) for item in value]
    return value


class jsonlLogger:
    def __init__(self, logPath: Path):
        self.logPath = logPath
        self.logPath.parent.mkdir(parents=True, exist_ok=True)

    def logEvent(self, event: dict[str, Any]) -> None:
        eventToWrite = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            **toJsonable(event),
        }
        eventText = json.dumps(eventToWrite, ensure_ascii=False, sort_keys=True)
        safeText = redactText(eventText)
        with self.logPath.open('a', encoding='utf-8') as fileObj:
            fileObj.write(safeText + '\n')

    def logPreviewEvent(self, eventType: str, payload: dict[str, Any]) -> None:
        previewText, truncated = makePreview(payload)
        self.logEvent({
            'type': eventType,
            'payloadPreview': previewText,
            'truncated': truncated,
        })
