'''
Author: wilbur
Version: 1.0
Date: 2026-09-07
Description: Explicitly rebuilds a missing session index from existing logs and read-only usage.db using supplied workDirs; strictly validates events without creating log directories or skipping damaged rows, and publishes atomically without overwriting any existing index or changing source history.
'''

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from flamingoAgents.utils.logPaths import logsRoot, workDirFolderName
from webApp.backend.historyView import normalizeUsage


def readRecoveryEvents(logPath: Path) -> list[dict]:
    # 恢复不能像运行时读取那样跳过坏行，也不能为读操作创建日志目录。
    text = logPath.read_text(encoding='utf-8').lstrip()
    events = []
    try:
        if text.startswith('['):
            initialEvents, end = json.JSONDecoder().raw_decode(text)
            if any(not isinstance(event, dict) for event in initialEvents):
                raise RuntimeError(f'会话日志数组包含非对象事件：{logPath}')
            events.extend(initialEvents)
            text = text[end:]
        for line in text.split('\n'):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise RuntimeError(f'会话日志包含非对象事件：{logPath}')
            events.append(event)
    except json.JSONDecodeError as error:
        raise RuntimeError(f'无法完整解析会话日志：{logPath}') from error
    return events


def recoverSessions(workDirs: list[Path], *, sourceRoot: Path = logsRoot) -> dict[str, dict]:
    # 显式工作目录正向映射；文件夹名的连字符编码不可逆，不能反推路径。
    folders = {}
    for workDir in workDirs:
        resolved = workDir.resolve()
        folder = workDirFolderName(resolved)
        if folder in folders and folders[folder] != resolved:
            raise RuntimeError(f'工作目录映射冲突：{resolved} 与 {folders[folder]}')
        folders[folder] = resolved
    latestTurns = {}
    dbPath = sourceRoot / 'usage.db'
    with closing(sqlite3.connect(dbPath.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        for row in connection.execute(
            'SELECT sessionId, providerId, modelId, promptTokens, cachedTokens, completionTokens'
            ' FROM usageTurns ORDER BY id'
        ):
            latestTurns[row[0]] = row
    sessions = {}
    for folder, workDir in folders.items():
        logPaths = sorted((sourceRoot / 'webData' / folder).glob('*.jsonl'))
        if not logPaths:
            raise RuntimeError(f'工作目录没有现存日志：{workDir}')
        for logPath in logPaths:
            sessionId = logPath.stem
            if not re.fullmatch(r'[A-Za-z0-9_-]+', sessionId) or sessionId in sessions:
                raise RuntimeError(f'日志 sessionId 非法或重复：{logPath}')
            turn = latestTurns.get(sessionId)
            if turn is None or not turn[1] or not turn[2]:
                raise RuntimeError(f'会话缺少可确认的模型记录：{sessionId}')
            events = readRecoveryEvents(logPath)
            timestamps = [event['timestamp'] for event in events if isinstance(event.get('timestamp'), str)]
            userTexts = [event['content'] for event in events if event.get('type') == 'userMessage' and isinstance(event.get('content'), str)]
            if not timestamps or not userTexts:
                raise RuntimeError(f'会话缺少可恢复的消息或时间：{sessionId}')
            usage = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
            contextTokens = 0
            for event in events:
                if event.get('type') != 'assistantMessage':
                    continue
                stepUsage = normalizeUsage(event.get('usage'))
                if stepUsage is not None:
                    for key in usage:
                        usage[key] += stepUsage[key]
                    contextTokens = stepUsage['promptTokens'] + stepUsage['completionTokens']
            sessions[sessionId] = {
                'sessionId': sessionId,
                'title': userTexts[0].strip()[:20] or '恢复的会话',
                'workDir': str(workDir),
                'providerId': turn[1],
                'modelId': turn[2],
                'createdAt': timestamps[0],
                'updatedAt': timestamps[-1],
                'usage': usage,
                'contextTokens': contextTokens,
                'lastUsage': dict(zip(usage, turn[3:])),
            }
    return sessions


def writeRecoveredIndex(outputPath: Path, sessions: dict[str, dict]) -> None:
    # 同文件系统 hard link 原子发布：若期间已有新会话创建索引，失败而非覆盖。
    payload = json.dumps({'sessions': list(sessions.values())}, ensure_ascii=False, indent=2)
    outputPath.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sessionRecovery-', dir=outputPath.parent) as tempDir:
        tempPath = Path(tempDir) / 'sessions.json'
        tempPath.write_text(payload, encoding='utf-8')
        tempPath.chmod(0o600)
        os.link(tempPath, outputPath)


def main() -> None:
    parser = argparse.ArgumentParser(description='从现存日志生成恢复索引；不覆盖已有文件，标题和最后使用模型为重建值。')
    parser.add_argument('--work-dir', action='append', required=True, dest='workDirs', type=Path)
    parser.add_argument('--logs-root', dest='sourceRoot', type=Path, default=logsRoot)
    parser.add_argument('--output', required=True, dest='outputPath', type=Path)
    args = parser.parse_args()
    sessions = recoverSessions(args.workDirs, sourceRoot=args.sourceRoot)
    writeRecoveredIndex(args.outputPath, sessions)
    print(f'已写入 {len(sessions)} 个恢复会话：{args.outputPath}；标题取首条消息，模型取最后记账记录。')


if __name__ == '__main__':
    main()
