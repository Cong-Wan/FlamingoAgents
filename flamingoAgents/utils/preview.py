'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Provides JSON-safe conversion helper for logs.
'''

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


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
