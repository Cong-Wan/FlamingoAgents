'''
Author: wilbur
Version: 1.1
Date: 2026-09-21
Description: 调用级用量账本：usageEvents 幂等写入、价格快照、JSONL outbox 补账、六种 period 单快照聚合。v1.1 启动扫描、运行期重试与进程退出补扫。
'''

from __future__ import annotations

import atexit
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from flamingoAgents.utils.configPaths import userModelsPath
from flamingoAgents.utils.logPaths import logsRoot as defaultLogsRoot
from flamingoAgents.utils.logPaths import usageDbPath


SCHEMA_VERSION = 1
LEGACY_STRATEGY = 'jsonlExisting'
LEGACY_KEY_VERSION = 'legacy1'
NANO_PER_USD = Decimal('1000000000')
TOKEN_PER_MILLION = Decimal('1000000')
PERIODS = ('today', 'yesterday', 'last7Days', 'last30Days', 'thisMonth', 'lastMonth')
PRICING_STATUSES = ('snapshot', 'migratedCurrentPrice', 'unknown')
RECORD_QUALITIES = ('exact', 'legacyInferred', 'legacyUnverified')
COST_FIELD_KEYS = ('input', 'output', 'cacheRead')
BUSY_TIMEOUT_MS = 5000

dbPath = usageDbPath
logsRoot = defaultLogsRoot
modelsYamlPath = userModelsPath
enableBackgroundRetry = True
clockNow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
timeZoneName: str | None = None

dbLock = threading.Lock()
dbConnection: sqlite3.Connection | None = None
_processReady = False
_atexitRegistered = False
_retryLock = threading.Lock()
_retryQueue: list[dict[str, Any]] = []
_retryStop = threading.Event()
_retryThread: threading.Thread | None = None


class unknownPeriodError(ValueError):
    pass


class usageLedgerError(RuntimeError):
    pass


def newUsageKey() -> str:
    return uuid4().hex


def utcNowIso() -> str:
    return canonicalUtcIso(clockNow())


def canonicalUtcIso(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    return utc.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+00:00'


def parseTimestamp(text: str) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def canonicalOccurredAt(text: str) -> str:
    moment = parseTimestamp(text)
    if moment is None:
        return utcNowIso()
    return canonicalUtcIso(moment)


def isNonNegInt(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parseExactUsage(rawUsage: Any) -> dict[str, int] | None:
    if not isinstance(rawUsage, dict):
        return None
    promptTokens = rawUsage.get('prompt_tokens')
    completionTokens = rawUsage.get('completion_tokens')
    if not isNonNegInt(promptTokens) or not isNonNegInt(completionTokens):
        return None
    details = rawUsage.get('prompt_tokens_details')
    if details is None:
        cachedTokens = 0
    elif not isinstance(details, dict):
        return None
    elif 'cached_tokens' not in details or details.get('cached_tokens') is None:
        cachedTokens = 0
    else:
        cachedTokens = details.get('cached_tokens')
        if not isNonNegInt(cachedTokens):
            return None
    if cachedTokens > promptTokens:
        return None
    return {
        'promptTokens': promptTokens,
        'cachedTokens': cachedTokens,
        'completionTokens': completionTokens,
    }


def extractCostFields(cost: Any) -> dict[str, Decimal] | None:
    if not isinstance(cost, dict):
        return None
    fields: dict[str, Decimal] = {}
    for key in COST_FIELD_KEYS:
        if key not in cost:
            return None
        value = cost[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if value < 0:
            return None
        fields[key] = Decimal(str(value))
    return fields


def priceSnapshot(
    promptTokens: int,
    cachedTokens: int,
    completionTokens: int,
    costFields: dict[str, Decimal] | None,
    pricingStatus: str = 'snapshot',
) -> tuple[int | None, str]:
    if costFields is None:
        return None, 'unknown'
    nonCached = max(0, promptTokens - cachedTokens)
    costUsd = (
        Decimal(nonCached) * costFields['input']
        + Decimal(cachedTokens) * costFields['cacheRead']
        + Decimal(completionTokens) * costFields['output']
    ) / TOKEN_PER_MILLION
    nano = int((costUsd * NANO_PER_USD).quantize(Decimal('1'), rounding=ROUND_HALF_EVEN))
    return nano, pricingStatus


def makeExactRecord(
    *,
    usageKey: str,
    source: str,
    sessionId: str,
    parentSessionId: str | None,
    providerId: str,
    modelId: str,
    requestStartedAt: str,
    occurredAt: str,
    tokens: dict[str, int],
    costFields: dict[str, Decimal] | None,
) -> dict[str, Any]:
    costNanoUsd, pricingStatus = priceSnapshot(
        tokens['promptTokens'],
        tokens['cachedTokens'],
        tokens['completionTokens'],
        costFields,
    )
    return {
        'usageKey': usageKey,
        'source': source,
        'sessionId': sessionId,
        'parentSessionId': parentSessionId,
        'providerId': providerId,
        'modelId': modelId,
        'requestStartedAt': canonicalOccurredAt(requestStartedAt),
        'occurredAt': canonicalOccurredAt(occurredAt),
        'promptTokens': tokens['promptTokens'],
        'cachedTokens': tokens['cachedTokens'],
        'completionTokens': tokens['completionTokens'],
        'costNanoUsd': costNanoUsd,
        'pricingStatus': pricingStatus,
        'recordQuality': 'exact',
        'createdAt': utcNowIso(),
    }


def detectSystemTimeZoneName() -> str:
    tzFile = Path('/etc/timezone')
    if tzFile.is_file():
        name = tzFile.read_text(encoding='utf-8').strip()
        if name:
            return name
    link = Path('/etc/localtime')
    if link.exists():
        resolved = str(link.resolve())
        marker = '/zoneinfo/'
        if marker in resolved:
            return resolved.split(marker, 1)[1]
    tzinfo = datetime.now().astimezone().tzinfo
    key = getattr(tzinfo, 'key', None)
    if isinstance(key, str) and key:
        return key
    return 'UTC'


def resolveTimeZone() -> ZoneInfo:
    name = timeZoneName or detectSystemTimeZoneName()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo('UTC')


def periodBounds(period: str, snapshotAt: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    if period not in PERIODS:
        raise unknownPeriodError(period)
    local = snapshotAt.astimezone(tz)
    todayStart = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'today':
        return todayStart, snapshotAt.astimezone(tz)
    if period == 'yesterday':
        return todayStart - timedelta(days=1), todayStart
    if period == 'last7Days':
        return todayStart - timedelta(days=6), snapshotAt.astimezone(tz)
    if period == 'last30Days':
        return todayStart - timedelta(days=29), snapshotAt.astimezone(tz)
    if period == 'thisMonth':
        return todayStart.replace(day=1), snapshotAt.astimezone(tz)
    thisMonth = todayStart.replace(day=1)
    lastMonthStart = (thisMonth - timedelta(days=1)).replace(day=1)
    return lastMonthStart, thisMonth


def bucketUnitFor(period: str) -> str:
    return 'hour' if period in ('today', 'yesterday') else 'day'


def iterBuckets(period: str, startAt: datetime, endAt: datetime) -> list[tuple[datetime, datetime]]:
    unit = bucketUnitFor(period)
    buckets: list[tuple[datetime, datetime]] = []
    if unit == 'hour':
        cursor = startAt.replace(minute=0, second=0, microsecond=0)
        while cursor < endAt:
            nxt = cursor + timedelta(hours=1)
            buckets.append((cursor, min(nxt, endAt)))
            cursor = nxt
        return buckets
    cursor = startAt.replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < endAt:
        nxt = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        buckets.append((cursor, min(nxt, endAt)))
        cursor = nxt
    return buckets


def shutdown() -> None:
    if not _processReady:
        return
    flushRetryQueue()
    try:
        reconcileAllLogs(importLegacy=False)
    except Exception:
        pass
    flushRetryQueue()


def resetState() -> None:
    global dbConnection, _processReady, _retryThread
    _retryStop.set()
    thread = _retryThread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1)
    _retryThread = None
    _retryStop.clear()
    with _retryLock:
        _retryQueue.clear()
    with dbLock:
        if dbConnection is not None:
            try:
                dbConnection.close()
            except Exception:
                pass
        dbConnection = None
    _processReady = False


def getConnection() -> sqlite3.Connection:
    global dbConnection
    with dbLock:
        if dbConnection is None:
            dbPath.parent.mkdir(parents=True, exist_ok=True)
            dbConnection = sqlite3.connect(dbPath, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
            dbConnection.row_factory = sqlite3.Row
            dbConnection.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
            dbConnection.execute('PRAGMA journal_mode=WAL')
            dbConnection.execute('PRAGMA synchronous=NORMAL')
            dbConnection.execute('PRAGMA foreign_keys=ON')
            for statement in _ddlStatements():
                dbConnection.execute(statement)
            dbConnection.commit()
        return dbConnection


def _ddlStatements() -> tuple[str, ...]:
    return (
        '''
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
        ''',
        'CREATE INDEX IF NOT EXISTS idxUsageTurnsTs ON usageTurns(timestamp)',
        '''
        CREATE TABLE IF NOT EXISTS usageEvents (
          usageKey TEXT PRIMARY KEY,
          source TEXT NOT NULL,
          sessionId TEXT NOT NULL,
          parentSessionId TEXT,
          providerId TEXT NOT NULL,
          modelId TEXT NOT NULL,
          requestStartedAt TEXT NOT NULL,
          occurredAt TEXT NOT NULL,
          promptTokens INTEGER NOT NULL CHECK(promptTokens >= 0),
          cachedTokens INTEGER NOT NULL CHECK(cachedTokens >= 0),
          completionTokens INTEGER NOT NULL CHECK(completionTokens >= 0),
          costNanoUsd INTEGER,
          pricingStatus TEXT NOT NULL,
          recordQuality TEXT NOT NULL,
          createdAt TEXT NOT NULL
        )
        ''',
        'CREATE INDEX IF NOT EXISTS idxUsageEventsOccurred ON usageEvents(occurredAt, providerId, modelId)',
        'CREATE INDEX IF NOT EXISTS idxUsageEventsSession ON usageEvents(sessionId, occurredAt)',
        '''
        CREATE TABLE IF NOT EXISTS usageReconcileCursors (
          logPath TEXT PRIMARY KEY,
          lastAckLine INTEGER NOT NULL,
          updatedAt TEXT NOT NULL
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS usageMeta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
        ''',
    )


def ensureSchema() -> None:
    connection = getConnection()
    with dbLock:
        connection.execute(
            'INSERT OR IGNORE INTO usageMeta(key, value) VALUES (?, ?)',
            ('schemaVersion', str(SCHEMA_VERSION)),
        )
        connection.commit()


def _recordParams(record: dict[str, Any]) -> tuple:
    return (
        record['usageKey'],
        record['source'],
        record['sessionId'],
        record.get('parentSessionId'),
        record['providerId'],
        record['modelId'],
        record['requestStartedAt'],
        record['occurredAt'],
        int(record['promptTokens']),
        int(record['cachedTokens']),
        int(record['completionTokens']),
        record.get('costNanoUsd'),
        record['pricingStatus'],
        record['recordQuality'],
        record.get('createdAt') or utcNowIso(),
    )


_insertSql = (
    'INSERT INTO usageEvents ('
    ' usageKey, source, sessionId, parentSessionId, providerId, modelId,'
    ' requestStartedAt, occurredAt, promptTokens, cachedTokens, completionTokens,'
    ' costNanoUsd, pricingStatus, recordQuality, createdAt'
    ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
    ' ON CONFLICT(usageKey) DO NOTHING'
)


def insertUsageEvent(record: dict[str, Any]) -> bool:
    ensureSchema()
    normalized = dict(record)
    normalized['requestStartedAt'] = canonicalOccurredAt(str(normalized.get('requestStartedAt') or ''))
    normalized['occurredAt'] = canonicalOccurredAt(str(normalized.get('occurredAt') or ''))
    if normalized.get('createdAt'):
        normalized['createdAt'] = canonicalOccurredAt(str(normalized['createdAt']))
    connection = getConnection()
    with dbLock:
        cursor = connection.execute(_insertSql, _recordParams(normalized))
        connection.commit()
        return cursor.rowcount == 1


def scheduleRetry(record: dict[str, Any]) -> None:
    with _retryLock:
        _retryQueue.append(dict(record))
    _ensureRetryThread()
    _registerShutdown()


def flushRetryQueue() -> int:
    with _retryLock:
        pending = list(_retryQueue)
        _retryQueue.clear()
    restored = 0
    failed: list[dict[str, Any]] = []
    for record in pending:
        try:
            insertUsageEvent(record)
            restored += 1
        except sqlite3.Error:
            failed.append(record)
    if failed:
        with _retryLock:
            _retryQueue.extend(failed)
    return restored


def _ensureRetryThread() -> None:
    global _retryThread
    if not enableBackgroundRetry:
        return
    if _retryThread is not None and _retryThread.is_alive():
        return
    _retryStop.clear()
    _retryThread = threading.Thread(target=_retryLoop, name='usageLedgerRetry', daemon=True)
    _retryThread.start()


def _retryLoop() -> None:
    while not _retryStop.wait(2.0):
        try:
            flushRetryQueue()
        except Exception:
            pass


def querySessionCostUsd(sessionId: str) -> float:
    ensureSchema()
    connection = getConnection()
    with dbLock:
        row = connection.execute(
            'SELECT SUM(costNanoUsd) FROM usageEvents WHERE sessionId = ? AND costNanoUsd IS NOT NULL',
            (sessionId,),
        ).fetchone()
    totalNano = int(row[0] or 0) if row is not None else 0
    return float(Decimal(totalNano) / NANO_PER_USD)


def queryLastSessionUsage(sessionId: str) -> dict[str, int] | None:
    ensureSchema()
    connection = getConnection()
    with dbLock:
        row = connection.execute(
            'SELECT promptTokens, cachedTokens, completionTokens FROM usageEvents'
            ' WHERE sessionId = ? ORDER BY occurredAt DESC, createdAt DESC LIMIT 1',
            (sessionId,),
        ).fetchone()
    if row is None:
        return None
    return {
        'promptTokens': int(row[0] or 0),
        'cachedTokens': int(row[1] or 0),
        'completionTokens': int(row[2] or 0),
    }


def countEvents() -> int:
    ensureSchema()
    connection = getConnection()
    with dbLock:
        row = connection.execute('SELECT COUNT(*) FROM usageEvents').fetchone()
    return int(row[0] if row is not None else 0)


def emptyTotals() -> dict[str, Any]:
    return {
        'callCount': 0,
        'promptTokens': 0,
        'cachedTokens': 0,
        'completionTokens': 0,
        'totalTokens': 0,
        'costNanoUsd': 0,
        'costStatus': 'complete',
        'unpricedCallCount': 0,
        'unpricedTokens': 0,
    }


def _addToTotals(target: dict[str, Any], row: sqlite3.Row) -> None:
    promptTokens = int(row['promptTokens'] or 0)
    cachedTokens = int(row['cachedTokens'] or 0)
    completionTokens = int(row['completionTokens'] or 0)
    target['callCount'] += 1
    target['promptTokens'] += promptTokens
    target['cachedTokens'] += cachedTokens
    target['completionTokens'] += completionTokens
    target['totalTokens'] += promptTokens + completionTokens
    if row['costNanoUsd'] is None:
        target['unpricedCallCount'] += 1
        target['unpricedTokens'] += promptTokens + completionTokens
    else:
        target['costNanoUsd'] += int(row['costNanoUsd'])


def _finalizeTotals(target: dict[str, Any]) -> dict[str, Any]:
    if target['callCount'] == 0:
        target['costStatus'] = 'complete'
        target['costNanoUsd'] = 0
    elif target['unpricedCallCount'] == target['callCount']:
        target['costStatus'] = 'unavailable'
        target['costNanoUsd'] = None
    elif target['unpricedCallCount'] > 0:
        target['costStatus'] = 'partial'
    else:
        target['costStatus'] = 'complete'
    return target


def _emptyQuality() -> dict[str, int]:
    return {
        'exactRecords': 0,
        'legacyInferredRecords': 0,
        'legacyUnverifiedRecords': 0,
        'migratedPriceRecords': 0,
        'unknownPriceRecords': 0,
    }


def _addQuality(quality: dict[str, int], row: sqlite3.Row) -> None:
    recordQuality = row['recordQuality']
    if recordQuality == 'exact':
        quality['exactRecords'] += 1
    elif recordQuality == 'legacyInferred':
        quality['legacyInferredRecords'] += 1
    elif recordQuality == 'legacyUnverified':
        quality['legacyUnverifiedRecords'] += 1
    if row['pricingStatus'] == 'migratedCurrentPrice':
        quality['migratedPriceRecords'] += 1
    elif row['pricingStatus'] == 'unknown':
        quality['unknownPriceRecords'] += 1


def _parseOccurredAt(text: str, tz: ZoneInfo) -> datetime | None:
    moment = parseTimestamp(text)
    if moment is None:
        return None
    return moment.astimezone(tz)


def _bucketIndex(moment: datetime, buckets: list[tuple[datetime, datetime]]) -> int | None:
    for index, (start, end) in enumerate(buckets):
        if start <= moment < end:
            return index
    return None


def _formatBucketLabel(startAt: datetime, unit: str) -> str:
    if unit == 'hour':
        return startAt.strftime('%H:00')
    return startAt.strftime('%m-%d')


def _sortKey(totals: dict[str, Any], ident: str) -> tuple:
    return (-int(totals.get('totalTokens') or 0), ident)


def queryUsageSnapshot(period: str) -> dict[str, Any]:
    if period not in PERIODS:
        raise unknownPeriodError(period)
    ensureSchema()
    tz = resolveTimeZone()
    snapshotAt = clockNow().astimezone(tz)
    startAt, endAt = periodBounds(period, snapshotAt, tz)
    startUtc = canonicalUtcIso(startAt)
    endUtc = canonicalUtcIso(endAt)
    unit = bucketUnitFor(period)
    buckets = iterBuckets(period, startAt, endAt)
    connection = getConnection()
    with dbLock:
        connection.execute('BEGIN')
        try:
            rows = connection.execute(
                'SELECT usageKey, providerId, modelId, occurredAt, promptTokens, cachedTokens,'
                ' completionTokens, costNanoUsd, pricingStatus, recordQuality'
                ' FROM usageEvents WHERE occurredAt >= ? AND occurredAt < ?',
                (startUtc, endUtc),
            ).fetchall()
        finally:
            connection.execute('ROLLBACK')

    globalTotals = emptyTotals()
    quality = _emptyQuality()
    providerMap: dict[str, dict[str, Any]] = {}
    for row in rows:
        _addToTotals(globalTotals, row)
        _addQuality(quality, row)
        providerId = str(row['providerId'])
        modelId = str(row['modelId'])
        provider = providerMap.setdefault(providerId, {
            'providerId': providerId,
            'totals': emptyTotals(),
            'models': {},
            'bucketRows': [ {} for _ in buckets ],
        })
        _addToTotals(provider['totals'], row)
        model = provider['models'].setdefault(modelId, {'modelId': modelId, 'totals': emptyTotals()})
        _addToTotals(model['totals'], row)
        localMoment = _parseOccurredAt(row['occurredAt'], tz)
        if localMoment is None:
            continue
        index = _bucketIndex(localMoment, buckets)
        if index is None:
            continue
        cell = provider['bucketRows'][index].setdefault(modelId, emptyTotals())
        _addToTotals(cell, row)

    providers = []
    for providerId, provider in providerMap.items():
        modelIds = [item['modelId'] for item in sorted(
            provider['models'].values(),
            key=lambda item: _sortKey(item['totals'], item['modelId']),
        )]
        models = []
        for modelId in modelIds:
            models.append({
                'modelId': modelId,
                'totals': _finalizeTotals(provider['models'][modelId]['totals']),
            })
        bucketPayload = []
        for index, (bucketStart, bucketEnd) in enumerate(buckets):
            modelCells = []
            for modelId in modelIds:
                cell = provider['bucketRows'][index].get(modelId) or emptyTotals()
                modelCells.append({
                    'modelId': modelId,
                    'totals': _finalizeTotals(dict(cell)),
                })
            bucketPayload.append({
                'startAt': bucketStart.isoformat(timespec='seconds'),
                'endAt': bucketEnd.isoformat(timespec='seconds'),
                'label': _formatBucketLabel(bucketStart, unit),
                'models': modelCells,
            })
        providers.append({
            'providerId': providerId,
            'totals': _finalizeTotals(provider['totals']),
            'models': models,
            'bucketUnit': unit,
            'buckets': bucketPayload,
        })
    providers.sort(key=lambda item: _sortKey(item['totals'], item['providerId']))
    return {
        'period': period,
        'timeZone': getattr(tz, 'key', str(tz)),
        'startAt': startAt.isoformat(timespec='seconds'),
        'endAt': endAt.isoformat(timespec='seconds'),
        'snapshotAt': snapshotAt.isoformat(timespec='seconds'),
        'totals': _finalizeTotals(globalTotals),
        'providers': providers,
        'quality': quality,
    }


def _metaGet(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute('SELECT value FROM usageMeta WHERE key = ?', (key,)).fetchone()
    return None if row is None else str(row[0])


def _metaSet(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        'INSERT INTO usageMeta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, value),
    )


def _backupLiveDb() -> Path | None:
    if not dbPath.exists():
        return None
    connection = getConnection()
    with dbLock:
        if _metaGet(connection, 'backupDone') == '1':
            return None
        stamp = clockNow().astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        dest = dbPath.with_name(f'usage.backup-{stamp}.db')
        if not dest.exists():
            dest.write_bytes(dbPath.read_bytes())
        _metaSet(connection, 'backupDone', '1')
        connection.commit()
        return dest


def _iterJsonlRecords(path: Path, startLine: int = 0):
    if not path.exists():
        return
    text = path.read_text(encoding='utf-8')
    if not text:
        return
    if not text.endswith('\n') and not text.lstrip().startswith('['):
        raise usageLedgerError(f'会话日志末行缺少换行符：{path}')
    rest = text
    lineNumber = 0
    stripped = text.lstrip()
    if stripped.startswith('['):
        try:
            initialEvents, end = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError as error:
            raise usageLedgerError(f'会话日志数组头不是合法 JSON：{path}') from error
        consumed = len(text) - len(stripped) + end
        lineNumber = text[:consumed].count('\n')
        if isinstance(initialEvents, list):
            for event in initialEvents:
                if isinstance(event, dict):
                    yield 0, event
        rest = stripped[end:]
    for line in rest.split('\n'):
        lineNumber += 1
        if lineNumber <= startLine:
            continue
        payload = line.strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as error:
            raise usageLedgerError(f'会话日志第 {lineNumber} 行不是合法 JSON：{path}') from error
        if not isinstance(event, dict):
            raise usageLedgerError(f'会话日志第 {lineNumber} 行不是 JSON 对象：{path}')
        yield lineNumber, event


def _sourceFromPath(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    if 'webdata' in parts:
        return 'web'
    if 'clidata' in parts:
        return 'cli'
    return 'library'


def _relativeLogPath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path(logsRoot).resolve()))
    except ValueError:
        return str(path)


def _legacyUsageKey(relPath: str, lineNumber: int, timestamp: str, modelId: str, tokens: dict[str, int]) -> str:
    payload = json.dumps(
        {
            'v': 1,
            'kind': 'assistantUsage',
            'path': relPath,
            'line': lineNumber,
            'timestamp': timestamp,
            'model': modelId,
            'promptTokens': tokens['promptTokens'],
            'cachedTokens': tokens['cachedTokens'],
            'completionTokens': tokens['completionTokens'],
        },
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    )
    return f'{LEGACY_KEY_VERSION}_{hashlib.sha256(payload.encode("utf-8")).hexdigest()}'


def _loadModelProviderIndex() -> dict[str, str]:
    path = Path(modelsYamlPath)
    if not path.exists():
        return {}
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except Exception:
        return {}
    providers = raw.get('providers') if isinstance(raw, dict) else None
    if not isinstance(providers, dict):
        return {}
    counts: dict[str, list[tuple[str, dict | None]]] = {}
    for providerId, provider in providers.items():
        models = provider.get('models') if isinstance(provider, dict) else None
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get('id'), str):
                continue
            counts.setdefault(model['id'], []).append((str(providerId), model.get('cost') if isinstance(model.get('cost'), dict) else None))
    unique: dict[str, str] = {}
    for modelId, entries in counts.items():
        providerIds = {item[0] for item in entries}
        if len(providerIds) == 1:
            unique[modelId] = entries[0][0]
    return unique


def snapshotModelCost(providerId: str, modelId: str) -> dict[str, Decimal] | None:
    return _loadModelCost(providerId, modelId)


def _loadModelCost(providerId: str, modelId: str) -> dict[str, Decimal] | None:
    path = Path(modelsYamlPath)
    if not path.exists():
        return None
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except Exception:
        return None
    providers = raw.get('providers') if isinstance(raw, dict) else None
    provider = providers.get(providerId) if isinstance(providers, dict) else None
    models = provider.get('models') if isinstance(provider, dict) else None
    if not isinstance(models, list):
        return None
    for model in models:
        if isinstance(model, dict) and model.get('id') == modelId:
            return extractCostFields(model.get('cost'))
    return None


def _nativeRecordFromEvent(event: dict[str, Any]) -> dict[str, Any] | None:
    usageKey = event.get('usageKey')
    if not isinstance(usageKey, str) or not usageKey:
        return None
    required = ('source', 'sessionId', 'providerId', 'modelId', 'requestStartedAt', 'occurredAt', 'pricingStatus', 'recordQuality')
    if any(not isinstance(event.get(key), str) or not event.get(key) for key in required):
        return None
    for key in ('promptTokens', 'cachedTokens', 'completionTokens'):
        if not isNonNegInt(event.get(key)):
            return None
    if event['cachedTokens'] > event['promptTokens']:
        return None
    costNanoUsd = event.get('costNanoUsd')
    if costNanoUsd is not None and not isNonNegInt(costNanoUsd):
        return None
    return {
        'usageKey': usageKey,
        'source': event['source'],
        'sessionId': event['sessionId'],
        'parentSessionId': event.get('parentSessionId') if isinstance(event.get('parentSessionId'), str) else None,
        'providerId': event['providerId'],
        'modelId': event['modelId'],
        'requestStartedAt': canonicalOccurredAt(event['requestStartedAt']),
        'occurredAt': canonicalOccurredAt(event['occurredAt']),
        'promptTokens': event['promptTokens'],
        'cachedTokens': event['cachedTokens'],
        'completionTokens': event['completionTokens'],
        'costNanoUsd': costNanoUsd,
        'pricingStatus': event['pricingStatus'],
        'recordQuality': event['recordQuality'],
        'createdAt': event.get('createdAt') or utcNowIso(),
    }


def _legacyRecordFromAssistant(
    event: dict[str, Any],
    path: Path,
    lineNumber: int,
    providerIndex: dict[str, str],
) -> dict[str, Any] | None:
    if event.get('usageKey'):
        return None
    tokens = parseExactUsage(event.get('usage'))
    if tokens is None:
        return None
    modelId = event.get('model') if isinstance(event.get('model'), str) and event.get('model') else 'unknown'
    providerId = providerIndex.get(modelId, 'legacyUnknown')
    occurredAt = event.get('timestamp') if isinstance(event.get('timestamp'), str) and event.get('timestamp') else utcNowIso()
    costFields = _loadModelCost(providerId, modelId) if providerId != 'legacyUnknown' else None
    costNanoUsd, pricingStatus = priceSnapshot(
        tokens['promptTokens'],
        tokens['cachedTokens'],
        tokens['completionTokens'],
        costFields,
        pricingStatus='migratedCurrentPrice',
    )
    return {
        'usageKey': _legacyUsageKey(_relativeLogPath(path), lineNumber, occurredAt, modelId, tokens),
        'source': _sourceFromPath(path),
        'sessionId': path.stem,
        'parentSessionId': None,
        'providerId': providerId,
        'modelId': modelId,
        'requestStartedAt': canonicalOccurredAt(occurredAt),
        'occurredAt': canonicalOccurredAt(occurredAt),
        'promptTokens': tokens['promptTokens'],
        'cachedTokens': tokens['cachedTokens'],
        'completionTokens': tokens['completionTokens'],
        'costNanoUsd': costNanoUsd,
        'pricingStatus': pricingStatus,
        'recordQuality': 'legacyInferred',
        'createdAt': utcNowIso(),
    }


def reconcileLogFile(path: Path, *, importLegacy: bool = False) -> dict[str, Any]:
    ensureSchema()
    report = {
        'path': str(path),
        'nativeImported': 0,
        'legacyImported': 0,
        'skipped': 0,
        'damaged': 0,
        'stoppedAtLine': None,
    }
    if not path.exists():
        return report
    connection = getConnection()
    resolved = str(path.resolve())
    with dbLock:
        row = connection.execute(
            'SELECT lastAckLine FROM usageReconcileCursors WHERE logPath = ?',
            (resolved,),
        ).fetchone()
        lastAck = int(row[0]) if row is not None else 0
    providerIndex = _loadModelProviderIndex() if importLegacy else {}
    pending: list[dict[str, Any]] = []
    maxLine = lastAck
    try:
        for lineNumber, event in _iterJsonlRecords(path, lastAck):
            eventType = event.get('type')
            record = None
            if eventType == 'usageRecord':
                record = _nativeRecordFromEvent(event)
                if record is None:
                    report['damaged'] += 1
                else:
                    pending.append(record)
                    report['nativeImported'] += 1
            elif importLegacy and eventType == 'assistantMessage':
                record = _legacyRecordFromAssistant(event, path, lineNumber, providerIndex)
                if record is None:
                    report['skipped'] += 1
                else:
                    pending.append(record)
                    report['legacyImported'] += 1
            if lineNumber > maxLine:
                maxLine = lineNumber
    except usageLedgerError:
        report['stoppedAtLine'] = maxLine + 1
        raise
    connection = getConnection()
    with dbLock:
        connection.execute('BEGIN IMMEDIATE')
        try:
            for record in pending:
                connection.execute(_insertSql, _recordParams(record))
            connection.execute(
                'INSERT INTO usageReconcileCursors(logPath, lastAckLine, updatedAt)'
                ' VALUES (?, ?, ?) ON CONFLICT(logPath) DO UPDATE SET'
                ' lastAckLine = excluded.lastAckLine, updatedAt = excluded.updatedAt',
                (resolved, maxLine, utcNowIso()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return report


def iterSessionLogs() -> list[Path]:
    root = Path(logsRoot)
    if not root.exists():
        return []
    return sorted(path for path in root.rglob('*.jsonl') if path.is_file())


def reconcileAllLogs(*, importLegacy: bool = False) -> dict[str, Any]:
    summary = {
        'strategy': LEGACY_STRATEGY,
        'scannedFiles': 0,
        'nativeImported': 0,
        'legacyImported': 0,
        'skipped': 0,
        'damaged': 0,
        'stoppedFiles': [],
    }
    for path in iterSessionLogs():
        summary['scannedFiles'] += 1
        try:
            report = reconcileLogFile(path, importLegacy=importLegacy)
        except usageLedgerError as error:
            summary['stoppedFiles'].append({'path': str(path), 'error': str(error)})
            continue
        for key in ('nativeImported', 'legacyImported', 'skipped', 'damaged'):
            summary[key] += report[key]
    return summary


def drainLogFile(path: Path) -> None:
    ensureSchema()
    try:
        report = reconcileLogFile(path, importLegacy=False)
    except usageLedgerError as error:
        raise usageLedgerError(f'用量尚未补齐，无法删除会话日志：{error}') from error
    if report.get('stoppedAtLine'):
        raise usageLedgerError('会话日志损坏，无法确认用量已落账。')
    try:
        flushRetryQueue()
    except sqlite3.Error as error:
        raise usageLedgerError(f'用量账本写入失败，已保留会话日志：{error}') from error
    with _retryLock:
        stillPending = any(
            str(item.get('sessionId')) == path.stem for item in _retryQueue
        )
    if stillPending:
        raise usageLedgerError('用量账本仍有待补写记录，已保留会话日志。')


def importLegacyIfNeeded() -> dict[str, Any] | None:
    ensureSchema()
    connection = getConnection()
    with dbLock:
        if _metaGet(connection, 'legacyImport') == f'{LEGACY_STRATEGY}-done':
            return None
    summary = reconcileAllLogs(importLegacy=True)
    connection = getConnection()
    with dbLock:
        _metaSet(connection, 'legacyImport', f'{LEGACY_STRATEGY}-done')
        _metaSet(connection, 'legacyStrategy', LEGACY_STRATEGY)
        connection.commit()
    return summary


def _registerShutdown() -> None:
    global _atexitRegistered
    if enableBackgroundRetry and not _atexitRegistered:
        atexit.register(shutdown)
        _atexitRegistered = True


def ensureReady(*, reconcile: bool = False) -> dict[str, Any] | None:
    global _processReady
    ensureSchema()
    report = None
    if reconcile and not _processReady:
        _backupLiveDb()
        report = importLegacyIfNeeded()
        native = reconcileAllLogs(importLegacy=False)
        if report is None:
            report = native
        _processReady = True
        _ensureRetryThread()
        _registerShutdown()
    return report
