'''
Author: wilbur
Version: 1.5
Date: 2026-09-08
Description: Writes JSONL audit events faithfully without redaction or truncation. v1.4 adds readEvents() to replay logged events for session resume.
             v1.5（imageInputPlan §3.3）新增 strict 严格读取模式与同路径读写锁：
             - 严格模式：坏 JSON / 非对象行报行号并抛 jsonlIntegrityError；非空末行缺换行视为损坏（防下一事件粘连），空文件合法；
             - 同进程内按 resolve 后 logPath 共享短时 RLock，logEvent 与严格 readEvents 互斥，避免大图片事件写入期间被读到半行。
'''

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flamingoAgents.utils.preview import toJsonable


class jsonlIntegrityError(RuntimeError):
    """严格模式读到的会话日志损坏（imageInputPlan §3.3）：带行号，不自动修复，原文件保留。"""

    def __init__(self, message: str, lineNumber: int = 0):
        super().__init__(message)
        self.lineNumber = lineNumber


_pathLocksGuard = threading.Lock()
_pathLocks: dict[str, threading.RLock] = {}


def _lockForPath(logPath: Path) -> threading.RLock:
    key = str(Path(logPath).resolve())
    with _pathLocksGuard:
        lock = _pathLocks.get(key)
        if lock is None:
            lock = threading.RLock()
            _pathLocks[key] = lock
        return lock


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
        with _lockForPath(self.logPath):
            with self.logPath.open('a', encoding='utf-8') as fileObj:
                fileObj.write(eventText + '\n')

    def readEvents(self, strict: bool = False) -> list[dict[str, Any]]:
        """strict=False 沿用旧行为（兼容数组日志、跳过坏行）；strict=True 用于会话恢复与历史渲染：
        坏 JSON/非对象行/非空末行缺换行 → jsonlIntegrityError（含行号），不返回部分事件。"""
        if not self.logPath.exists():
            return []
        events: list[dict[str, Any]] = []
        with _lockForPath(self.logPath):
            with self.logPath.open('r', encoding='utf-8') as fileObj:
                content = fileObj.read()
            if not content:
                return []
            if strict and not content.endswith('\n') and not content.lstrip().startswith('['):
                lineNumber = content.count('\n') + 1
                raise jsonlIntegrityError(
                    f'会话日志末行缺少换行符（第 {lineNumber} 行，可能写入中断）', lineNumber
                )
            stripped = content.lstrip()
            rest = content
            lineNumber = 0
            if stripped.startswith('['):
                try:
                    initialEvents, end = json.JSONDecoder().raw_decode(stripped)
                except json.JSONDecodeError:
                    if strict:
                        raise jsonlIntegrityError('会话日志数组头不是合法 JSON', 1) from None
                    return []
                if isinstance(initialEvents, list):
                    events.extend(event for event in initialEvents if isinstance(event, dict))
                rest = stripped[end:]
                lineNumber = content[:len(content) - len(rest)].count('\n')
            for line in rest.split('\n'):
                lineNumber += 1
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    if strict:
                        raise jsonlIntegrityError(
                            f'会话日志第 {lineNumber} 行不是合法 JSON（可能写入中断）', lineNumber
                        ) from None
                    continue
                if not isinstance(parsed, dict):
                    if strict:
                        raise jsonlIntegrityError(
                            f'会话日志第 {lineNumber} 行不是 JSON 对象', lineNumber
                        )
                    continue
                events.append(parsed)
        return events
