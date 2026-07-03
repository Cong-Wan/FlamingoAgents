'''
Author: wilbur
Version: 1.2
Date: 2026-07-02
Description: Writes JSONL audit events using shared redaction and preview helpers.
'''

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flamingoAgents.utils.preview import makePreview, toJsonable
from flamingoAgents.utils.redaction import redactText


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
