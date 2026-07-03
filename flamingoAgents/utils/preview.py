'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Provides JSON-safe conversion and truncated preview helpers for logs and tool results.
'''

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from flamingoAgents.utils.redaction import redactText

previewLimit = 4000


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


def makePreview(value: Any, limit: int = previewLimit) -> tuple[str, bool]:
    if isinstance(value, str):
        rawText = value
    else:
        rawText = json.dumps(toJsonable(value), ensure_ascii=False, sort_keys=True)
    redactedText = redactText(rawText)
    if len(redactedText) <= limit:
        return redactedText, False
    return redactedText[:limit] + '\n<truncated>', True
