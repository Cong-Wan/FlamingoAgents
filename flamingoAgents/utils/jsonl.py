'''
Author: wilbur
Version: 1.3
Date: 2026-07-08
Description: Writes JSONL audit events faithfully without redaction or truncation.
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
