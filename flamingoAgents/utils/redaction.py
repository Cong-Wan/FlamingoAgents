'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Provides reusable secret redaction helpers for Flamingo Agents logs and previews.
'''

from __future__ import annotations

import re

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
