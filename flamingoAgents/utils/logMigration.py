'''
Author: wilbur
Version: 1.3
Date: 2026-08-19
Description: 一次性迁移脚本：v2.3 把旧日志搬到 ~/.flamingo/logs/；
            layout V2 嵌套树、V3 全角／一层名均为过渡；V4 改为一层路径名且 / 换成 -。
            运行：uv run python -m flamingoAgents.utils.logMigration
'''

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from flamingoAgents.utils.logPaths import logsRoot, usageDbPath, workDirFolderName

repoRoot = Path(__file__).resolve().parents[2]
legacyWebDataDir = repoRoot / 'webData'
legacySessionLogsDir = legacyWebDataDir / 'sessionLogs'
legacyUsageDbPath = legacyWebDataDir / 'usage.db'
legacySessionsIndexPath = legacyWebDataDir / 'sessions.json'

doneMarkerName = '.migrationDone'
partialMarkerName = '.migrationPartial'
layoutDoneMarkerName = '.layoutV2Done'
layoutPartialMarkerName = '.layoutV2Partial'
layoutV3DoneMarkerName = '.layoutV3Done'
layoutV3PartialMarkerName = '.layoutV3Partial'
layoutV4DoneMarkerName = '.layoutV4Done'
layoutV4PartialMarkerName = '.layoutV4Partial'

usageTurnsDdl = '''
CREATE TABLE IF NOT EXISTS usageTurns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sessionId TEXT NOT NULL,
  providerId TEXT NOT NULL,
  modelId TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  promptTokens INTEGER NOT NULL,
  cachedTokens INTEGER NOT NULL,
  completionTokens INTEGER NOT NULL
)
'''

usageTurnsIndexDdl = 'CREATE INDEX IF NOT EXISTS idxUsageTurnsTs ON usageTurns(timestamp)'

usageMergeSql = '''
INSERT INTO usageTurns (sessionId, providerId, modelId, timestamp, promptTokens, cachedTokens, completionTokens)
SELECT DISTINCT s.sessionId, s.providerId, s.modelId, s.timestamp, s.promptTokens, s.cachedTokens, s.completionTokens
FROM src.usageTurns AS s
WHERE NOT EXISTS (
    SELECT 1 FROM usageTurns AS t
    WHERE t.sessionId = s.sessionId
      AND t.providerId = s.providerId
      AND t.modelId = s.modelId
      AND t.timestamp = s.timestamp
      AND t.promptTokens = s.promptTokens
      AND t.cachedTokens = s.cachedTokens
      AND t.completionTokens = s.completionTokens
)
'''


def _nowText() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _loadSessionsIndex(indexPath: Path) -> dict[str, dict]:
    if not indexPath.exists():
        return {}
    raw = json.loads(indexPath.read_text(encoding='utf-8'))
    sessions = raw.get('sessions') if isinstance(raw, dict) else None
    if not isinstance(sessions, list):
        return {}
    return {
        item['sessionId']: item
        for item in sessions
        if isinstance(item, dict) and item.get('sessionId')
    }


def _moveJsonl(source: Path, dest: Path, failures: list[str]) -> str:
    # 返回 moved / dropped / failed。丢弃不删源（§6-2）。
    if dest.exists():
        return 'dropped'
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
        return 'moved'
    except OSError as error:
        print(f'warning: 迁移失败 {source} → {dest}: {error}')
        failures.append(f'{source}\t{error}')
        return 'failed'


def migrateWebJsonl(
    sessionLogsDir: Path,
    sessionsIndexPath: Path,
    targetLogsRoot: Path,
    failures: list[str],
) -> tuple[int, int]:
    moved = 0
    dropped = 0
    if not sessionLogsDir.is_dir():
        return moved, dropped
    try:
        index = _loadSessionsIndex(sessionsIndexPath)
        sources = sorted(sessionLogsDir.glob('*.jsonl'))
    except (OSError, json.JSONDecodeError) as error:
        print(f'warning: 读取 web session 日志失败 {sessionLogsDir}: {error}')
        failures.append(f'{sessionLogsDir}\t{error}')
        return moved, dropped
    for source in sources:
        session = index.get(source.stem)
        workDir = session.get('workDir') if isinstance(session, dict) else None
        if not workDir:
            dropped += 1
            continue
        dest = targetLogsRoot / 'webData' / workDirFolderName(Path(workDir)) / source.name
        result = _moveJsonl(source, dest, failures)
        if result == 'moved':
            moved += 1
        elif result == 'dropped':
            dropped += 1
    return moved, dropped


def migrateCliJsonl(
    cliWorkDirs: list[Path] | tuple[Path, ...],
    targetLogsRoot: Path,
    failures: list[str],
) -> tuple[int, int]:
    moved = 0
    dropped = 0
    for workDir in cliWorkDirs:
        agentLogsDir = Path(workDir) / '.agentLogs'
        if not agentLogsDir.is_dir():
            continue
        folder = workDirFolderName(Path(workDir))
        try:
            sources = sorted(agentLogsDir.glob('*.jsonl'))
        except OSError as error:
            print(f'warning: 读取 CLI 日志失败 {agentLogsDir}: {error}')
            failures.append(f'{agentLogsDir}\t{error}')
            continue
        for source in sources:
            dest = targetLogsRoot / 'cliData' / folder / source.name
            result = _moveJsonl(source, dest, failures)
            if result == 'moved':
                moved += 1
            elif result == 'dropped':
                dropped += 1
    return moved, dropped


def mergeUsageDb(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(dest)
    try:
        connection.execute(usageTurnsDdl)
        connection.execute(usageTurnsIndexDdl)
        connection.execute('ATTACH DATABASE ? AS src', (str(source),))
        connection.execute(usageMergeSql)
        connection.commit()
        connection.execute('DETACH DATABASE src')
    finally:
        connection.close()
    source.unlink()


def migrateUsageDb(source: Path, dest: Path, failures: list[str]) -> tuple[int, int]:
    if not source.exists():
        return 0, 0
    try:
        if dest.exists():
            mergeUsageDb(source, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
        return 1, 0
    except (OSError, sqlite3.Error) as error:
        print(f'warning: usage.db 迁移失败 {source} → {dest}: {error}')
        failures.append(f'{source}\t{error}')
        return 0, 0


def runMigration(
    *,
    cliWorkDirs: list[Path] | tuple[Path, ...] = (),
    sessionLogsDir: Path = legacySessionLogsDir,
    usageDbFile: Path = legacyUsageDbPath,
    sessionsIndexPath: Path = legacySessionsIndexPath,
    targetLogsRoot: Path = logsRoot,
    targetUsageDb: Path = usageDbPath,
) -> None:
    doneMarker = targetLogsRoot / doneMarkerName
    partialMarker = targetLogsRoot / partialMarkerName
    if doneMarker.exists():
        print('迁移已完成（存在 .migrationDone），跳过。')
        return

    failures: list[str] = []
    webMoved, webDropped = migrateWebJsonl(sessionLogsDir, sessionsIndexPath, targetLogsRoot, failures)
    cliMoved, cliDropped = migrateCliJsonl(cliWorkDirs, targetLogsRoot, failures)
    usageMoved, usageDropped = migrateUsageDb(usageDbFile, targetUsageDb, failures)
    moved = webMoved + cliMoved + usageMoved
    dropped = webDropped + cliDropped + usageDropped

    targetLogsRoot.mkdir(parents=True, exist_ok=True)
    if failures:
        lines = [f'time={_nowText()}', f'failed={len(failures)}', *failures]
        partialMarker.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'迁移部分失败：{len(failures)} 个 OSError，已写 {partialMarker}，下次重试。')
        print(f'  moved={moved} dropped={dropped}')
        return

    doneMarker.write_text(
        f'time={_nowText()}\nmoved={moved}\ndropped={dropped}\n',
        encoding='utf-8',
    )
    if partialMarker.exists():
        try:
            partialMarker.unlink()
        except OSError as error:
            print(f'warning: 删除 .migrationPartial 失败: {error}')
    print(f'迁移完成：moved={moved} dropped={dropped} → {doneMarker}')


def migrateAll(cliWorkDirs: list[Path] | tuple[Path, ...] = ()) -> None:
    runMigration(cliWorkDirs=cliWorkDirs)


def _legacySanitizeName(name: str) -> str:
    cleaned = re.sub(r'-+', '-', re.sub(r'[^A-Za-z0-9._-]', '-', name)).strip('-.')
    return (cleaned or 'root')[:40]


def _legacyWorkDirFolderName(workDir: Path) -> str:
    # v2.3 当时的公式，仅供 layout V2 找旧目录；与运行时 workDirFolderName 无关。
    resolved = str(workDir.resolve())
    digest = hashlib.sha1(resolved.encode('utf-8')).hexdigest()[:8]
    return f'{_legacySanitizeName(workDir.name)}-{digest}'


def migrateLayoutV2(
    *,
    cliWorkDirs: list[Path] | tuple[Path, ...] = (),
    sessionsIndexPath: Path = legacySessionsIndexPath,
    targetLogsRoot: Path = logsRoot,
) -> None:
    doneMarker = targetLogsRoot / layoutDoneMarkerName
    partialMarker = targetLogsRoot / layoutPartialMarkerName
    if doneMarker.exists():
        print('layout V2 已完成（存在 .layoutV2Done），跳过。')
        return

    failures: list[str] = []
    moved = 0
    dropped = 0

    try:
        index = _loadSessionsIndex(sessionsIndexPath)
    except (OSError, json.JSONDecodeError) as error:
        print(f'warning: 读取 sessions 索引失败 {sessionsIndexPath}: {error}')
        failures.append(f'{sessionsIndexPath}\t{error}')
        index = {}

    for session in index.values():
        workDirRaw = session.get('workDir') if isinstance(session, dict) else None
        sessionId = session.get('sessionId') if isinstance(session, dict) else None
        if not workDirRaw or not sessionId:
            continue
        workDir = Path(workDirRaw)
        source = targetLogsRoot / 'webData' / _legacyWorkDirFolderName(workDir) / f'{sessionId}.jsonl'
        dest = targetLogsRoot / 'webData' / workDirFolderName(workDir) / f'{sessionId}.jsonl'
        if not source.exists() or source == dest:
            continue
        result = _moveJsonl(source, dest, failures)
        if result == 'moved':
            moved += 1
        elif result == 'dropped':
            dropped += 1

    for workDir in cliWorkDirs:
        sourceDir = targetLogsRoot / 'cliData' / _legacyWorkDirFolderName(Path(workDir))
        destDir = targetLogsRoot / 'cliData' / workDirFolderName(Path(workDir))
        if not sourceDir.is_dir() or sourceDir == destDir:
            continue
        try:
            sources = sorted(sourceDir.glob('*.jsonl'))
        except OSError as error:
            print(f'warning: 读取 CLI layout 源失败 {sourceDir}: {error}')
            failures.append(f'{sourceDir}\t{error}')
            continue
        for source in sources:
            dest = destDir / source.name
            result = _moveJsonl(source, dest, failures)
            if result == 'moved':
                moved += 1
            elif result == 'dropped':
                dropped += 1

    targetLogsRoot.mkdir(parents=True, exist_ok=True)
    if failures:
        lines = [f'time={_nowText()}', f'failed={len(failures)}', *failures]
        partialMarker.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'layout V2 部分失败：{len(failures)} 个 OSError，已写 {partialMarker}，下次重试。')
        print(f'  moved={moved} dropped={dropped}')
        return

    doneMarker.write_text(
        f'time={_nowText()}\nmoved={moved}\ndropped={dropped}\n',
        encoding='utf-8',
    )
    if partialMarker.exists():
        try:
            partialMarker.unlink()
        except OSError as error:
            print(f'warning: 删除 .layoutV2Partial 失败: {error}')
    print(f'layout V2 完成：moved={moved} dropped={dropped} → {doneMarker}')


def _nestedLayoutFolderName(workDir: Path) -> str:
    # v1.1 误做成的嵌套树：resolve() 去掉盘符后按原层级。仅供 V3 找源。
    resolved = workDir.resolve()
    parts = resolved.parts[1:] if resolved.anchor else resolved.parts
    if not parts:
        return 'root'
    return str(Path(*parts))


def _layoutV3Sources(category: str, workDir: Path, targetLogsRoot: Path) -> list[Path]:
    # 先试 v1.1 嵌套树，再试 v2.3 名字-hash；调用方对存在的源逐个搬。
    return [
        targetLogsRoot / category / _nestedLayoutFolderName(workDir),
        targetLogsRoot / category / _legacyWorkDirFolderName(workDir),
    ]


def migrateLayoutV3(
    *,
    cliWorkDirs: list[Path] | tuple[Path, ...] = (),
    sessionsIndexPath: Path = legacySessionsIndexPath,
    targetLogsRoot: Path = logsRoot,
) -> None:
    doneMarker = targetLogsRoot / layoutV3DoneMarkerName
    partialMarker = targetLogsRoot / layoutV3PartialMarkerName
    if doneMarker.exists():
        print('layout V3 已完成（存在 .layoutV3Done），跳过。')
        return

    failures: list[str] = []
    moved = 0
    dropped = 0

    try:
        index = _loadSessionsIndex(sessionsIndexPath)
    except (OSError, json.JSONDecodeError) as error:
        print(f'warning: 读取 sessions 索引失败 {sessionsIndexPath}: {error}')
        failures.append(f'{sessionsIndexPath}\t{error}')
        index = {}

    for session in index.values():
        workDirRaw = session.get('workDir') if isinstance(session, dict) else None
        sessionId = session.get('sessionId') if isinstance(session, dict) else None
        if not workDirRaw or not sessionId:
            continue
        workDir = Path(workDirRaw)
        dest = targetLogsRoot / 'webData' / workDirFolderName(workDir) / f'{sessionId}.jsonl'
        for sourceDir in _layoutV3Sources('webData', workDir, targetLogsRoot):
            source = sourceDir / f'{sessionId}.jsonl'
            if not source.exists() or source == dest:
                continue
            result = _moveJsonl(source, dest, failures)
            if result == 'moved':
                moved += 1
            elif result == 'dropped':
                dropped += 1
            break

    for workDir in cliWorkDirs:
        destDir = targetLogsRoot / 'cliData' / workDirFolderName(Path(workDir))
        for sourceDir in _layoutV3Sources('cliData', Path(workDir), targetLogsRoot):
            if not sourceDir.is_dir() or sourceDir == destDir:
                continue
            try:
                sources = sorted(sourceDir.glob('*.jsonl'))
            except OSError as error:
                print(f'warning: 读取 CLI layout V3 源失败 {sourceDir}: {error}')
                failures.append(f'{sourceDir}\t{error}')
                continue
            for source in sources:
                dest = destDir / source.name
                result = _moveJsonl(source, dest, failures)
                if result == 'moved':
                    moved += 1
                elif result == 'dropped':
                    dropped += 1

    targetLogsRoot.mkdir(parents=True, exist_ok=True)
    if failures:
        lines = [f'time={_nowText()}', f'failed={len(failures)}', *failures]
        partialMarker.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'layout V3 部分失败：{len(failures)} 个 OSError，已写 {partialMarker}，下次重试。')
        print(f'  moved={moved} dropped={dropped}')
        return

    doneMarker.write_text(
        f'time={_nowText()}\nmoved={moved}\ndropped={dropped}\n',
        encoding='utf-8',
    )
    if partialMarker.exists():
        try:
            partialMarker.unlink()
        except OSError as error:
            print(f'warning: 删除 .layoutV3Partial 失败: {error}')
    print(f'layout V3 完成：moved={moved} dropped={dropped} → {doneMarker}')


def _fullwidthLayoutFolderName(workDir: Path) -> str:
    # V3 公式：一层路径名，/ 换成全角／。仅供 V4 找源。
    resolved = workDir.resolve()
    home = Path.home().resolve()
    if resolved == home or home in resolved.parents:
        relative = resolved.relative_to(home)
        text = '~' if relative == Path('.') else f'~/{relative.as_posix()}'
    else:
        parts = resolved.parts[1:] if resolved.anchor else resolved.parts
        text = '/' + '/'.join(parts) if parts else '/'
    return text.replace('/', '／')


def _layoutV4Sources(category: str, workDir: Path, targetLogsRoot: Path) -> list[Path]:
    return [
        targetLogsRoot / category / _fullwidthLayoutFolderName(workDir),
        targetLogsRoot / category / _nestedLayoutFolderName(workDir),
        targetLogsRoot / category / _legacyWorkDirFolderName(workDir),
    ]


def migrateLayoutV4(
    *,
    cliWorkDirs: list[Path] | tuple[Path, ...] = (),
    sessionsIndexPath: Path = legacySessionsIndexPath,
    targetLogsRoot: Path = logsRoot,
) -> None:
    doneMarker = targetLogsRoot / layoutV4DoneMarkerName
    partialMarker = targetLogsRoot / layoutV4PartialMarkerName
    if doneMarker.exists():
        print('layout V4 已完成（存在 .layoutV4Done），跳过。')
        return

    failures: list[str] = []
    moved = 0
    dropped = 0

    try:
        index = _loadSessionsIndex(sessionsIndexPath)
    except (OSError, json.JSONDecodeError) as error:
        print(f'warning: 读取 sessions 索引失败 {sessionsIndexPath}: {error}')
        failures.append(f'{sessionsIndexPath}\t{error}')
        index = {}

    for session in index.values():
        workDirRaw = session.get('workDir') if isinstance(session, dict) else None
        sessionId = session.get('sessionId') if isinstance(session, dict) else None
        if not workDirRaw or not sessionId:
            continue
        workDir = Path(workDirRaw)
        dest = targetLogsRoot / 'webData' / workDirFolderName(workDir) / f'{sessionId}.jsonl'
        for sourceDir in _layoutV4Sources('webData', workDir, targetLogsRoot):
            source = sourceDir / f'{sessionId}.jsonl'
            if not source.exists() or source == dest:
                continue
            result = _moveJsonl(source, dest, failures)
            if result == 'moved':
                moved += 1
            elif result == 'dropped':
                dropped += 1
            break

    for workDir in cliWorkDirs:
        destDir = targetLogsRoot / 'cliData' / workDirFolderName(Path(workDir))
        for sourceDir in _layoutV4Sources('cliData', Path(workDir), targetLogsRoot):
            if not sourceDir.is_dir() or sourceDir == destDir:
                continue
            try:
                sources = sorted(sourceDir.glob('*.jsonl'))
            except OSError as error:
                print(f'warning: 读取 CLI layout V4 源失败 {sourceDir}: {error}')
                failures.append(f'{sourceDir}\t{error}')
                continue
            for source in sources:
                dest = destDir / source.name
                result = _moveJsonl(source, dest, failures)
                if result == 'moved':
                    moved += 1
                elif result == 'dropped':
                    dropped += 1

    targetLogsRoot.mkdir(parents=True, exist_ok=True)
    if failures:
        lines = [f'time={_nowText()}', f'failed={len(failures)}', *failures]
        partialMarker.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'layout V4 部分失败：{len(failures)} 个 OSError，已写 {partialMarker}，下次重试。')
        print(f'  moved={moved} dropped={dropped}')
        return

    doneMarker.write_text(
        f'time={_nowText()}\nmoved={moved}\ndropped={dropped}\n',
        encoding='utf-8',
    )
    if partialMarker.exists():
        try:
            partialMarker.unlink()
        except OSError as error:
            print(f'warning: 删除 .layoutV4Partial 失败: {error}')
    print(f'layout V4 完成：moved={moved} dropped={dropped} → {doneMarker}')


if __name__ == '__main__':
    cliWorkDirs = [Path('/Users/wilbur/project/FlamingoAgents')]
    migrateAll(cliWorkDirs=cliWorkDirs)
    migrateLayoutV2(cliWorkDirs=cliWorkDirs)
    migrateLayoutV3(cliWorkDirs=cliWorkDirs)
    migrateLayoutV4(cliWorkDirs=cliWorkDirs)
