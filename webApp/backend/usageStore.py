'''
Author: wilbur
Version: 1.0
Date: 2026-08-07
Description: webData/usage.db SQLite 用量统计库（契约 §3.10 / webAppPlan §11.4）：usageTurns 表 DDL 与增量写入（泵线程终态 delta，任一项 >0 才写）；
            空表时从 sessionLogs jsonl 一次性回填 assistantMessage 事件（providerId 从 sessions 索引补、缺省 unknown）；
            hour/day/month 聚合查询（UTC 转服务器本地时区切桶、空桶补齐、byModel key 为 providerId/modelId、cost 查询时按 models.yaml 当前价格计算）。
'''

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from flamingoAgents.utils.jsonl import jsonlLog

from webApp.backend import historyView, modelConfigStore, sessionStore

dbPath = sessionStore.webDataDir / 'usage.db'
dbLock = threading.Lock()
dbConnection: sqlite3.Connection | None = None

tokenKeys = ('promptTokens', 'cachedTokens', 'completionTokens')
labelFormats = {'hour': '%Y-%m-%d %H', 'day': '%Y-%m-%d', 'month': '%Y-%m'}

ddlStatements = (
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
)

insertSql = (
    'INSERT INTO usageTurns (sessionId, providerId, modelId, timestamp, promptTokens, cachedTokens, completionTokens)'
    ' VALUES (?, ?, ?, ?, ?, ?, ?)'
)


def getConnection() -> sqlite3.Connection:
    # 泵线程是工作线程：check_same_thread=False + 模块级锁（方案 §11.4）；建表幂等。
    global dbConnection
    if dbConnection is None:
        dbPath.parent.mkdir(parents=True, exist_ok=True)
        dbConnection = sqlite3.connect(dbPath, check_same_thread=False)
        with dbLock:
            for statement in ddlStatements:
                dbConnection.execute(statement)
            dbConnection.commit()
    return dbConnection


def initUsageDb() -> None:
    # 服务启动完成前调用（__main__.py）：建表 + 空表时从 jsonl 一次性回填历史用量。
    connection = getConnection()
    backfillFromLogs(connection)


def backfillFromLogs(connection: sqlite3.Connection) -> None:
    # 只在空表执行一次（方案 §11.4）：逐 jsonl 扫 assistantMessage，按事件自带 timestamp/model/usage 插入；
    # jsonl 事件无 providerId，从 sessions 索引按 sessionId 补，索引没有（会话已删）→ unknown。
    with dbLock:
        rowCount = connection.execute('SELECT COUNT(*) FROM usageTurns').fetchone()[0]
        if rowCount > 0:
            return
        sessionsIndex = sessionStore.loadIndex()
        rows = []
        for logPath in sorted(sessionStore.sessionLogsDir.glob('*.jsonl')):
            session = sessionsIndex.get(logPath.stem)
            providerId = session.get('providerId', 'unknown') if isinstance(session, dict) else 'unknown'
            for event in jsonlLog(logPath).readEvents():
                if event.get('type') != 'assistantMessage':
                    continue
                usage = historyView.normalizeUsage(event.get('usage'))
                timestamp = event.get('timestamp')
                if usage is None or not timestamp:
                    continue
                rows.append((
                    logPath.stem,
                    providerId,
                    str(event.get('model') or ''),
                    timestamp,
                    usage['promptTokens'],
                    usage['cachedTokens'],
                    usage['completionTokens'],
                ))
        if rows:
            connection.executemany(insertSql, rows)
            connection.commit()
            print(f'usageStore：已从 sessionLogs 回填 {len(rows)} 条历史用量记录。')


def writeUsageTurn(sessionId: str, providerId: str, modelId: str, delta: dict) -> None:
    # 泵线程终态写入；delta 有任一项 > 0 才写（空转/纯报错泵流不落账）。
    if not any(int(delta.get(key, 0) or 0) > 0 for key in tokenKeys):
        return
    with dbLock:
        connection = getConnection()
        connection.execute(
            insertSql,
            (
                sessionId,
                providerId,
                modelId,
                sessionStore.nowIso(),
                *(int(delta.get(key, 0) or 0) for key in tokenKeys),
            ),
        )
        connection.commit()


def loadCostMap() -> dict:
    # cost 查询时按 models.yaml 当前价格计算（契约 §3.10）；yaml 缺失/模型已删 → 按 0 计。
    try:
        raw = modelConfigStore.readRawYaml()
    except RuntimeError:
        return {}
    costMap = {}
    providers = raw.get('providers')
    if isinstance(providers, dict):
        for providerId, provider in providers.items():
            models = provider.get('models') if isinstance(provider, dict) else None
            for model in models or []:
                if not isinstance(model, dict) or not model.get('id'):
                    continue
                costMap[f'{providerId}/{model["id"]}'] = modelConfigStore.normalizeCostForRead(model.get('cost'))
    return costMap


def parseTimestamp(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def buildLabels(granularity: str, earliest: datetime | None) -> list[str]:
    # hour=近 72 小时、day=近 90 天、month=最早记录月→当前月（无记录返回空，契约 §3.10）；空桶补齐保证时间轴连续。
    now = datetime.now(timezone.utc).astimezone()
    if granularity == 'hour':
        start = (now - timedelta(hours=71)).replace(minute=0, second=0, microsecond=0)
        return [(start + timedelta(hours=index)).strftime(labelFormats['hour']) for index in range(72)]
    if granularity == 'day':
        start = (now - timedelta(days=89)).replace(hour=0, minute=0, second=0, microsecond=0)
        return [(start + timedelta(days=index)).strftime(labelFormats['day']) for index in range(90)]
    if earliest is None:
        return []
    cursor = earliest.astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    labels = []
    while cursor <= now:
        labels.append(cursor.strftime(labelFormats['month']))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return labels


def querySeries(granularity: str) -> dict:
    connection = getConnection()
    with dbLock:
        earliestText = connection.execute('SELECT MIN(timestamp) FROM usageTurns').fetchone()[0]
        rows = connection.execute(
            'SELECT timestamp, providerId, modelId, promptTokens, cachedTokens, completionTokens FROM usageTurns'
        ).fetchall()
    earliest = parseTimestamp(earliestText)
    costMap = loadCostMap()
    buckets = {
        label: {
            'label': label,
            'promptTokens': 0,
            'cachedTokens': 0,
            'completionTokens': 0,
            'cost': 0.0,
            'byModel': {},
        }
        for label in buildLabels(granularity, earliest)
    }
    for timestampText, providerId, modelId, promptTokens, cachedTokens, completionTokens in rows:
        moment = parseTimestamp(timestampText)
        if moment is None:
            continue
        # 桶切分按服务器本地时区（契约 §3.10）：UTC 时间戳先转本地再 strftime。
        bucket = buckets.get(moment.astimezone().strftime(labelFormats[granularity]))
        if bucket is None:
            continue
        modelKey = f'{providerId}/{modelId}'
        cost = costMap.get(modelKey)
        turnCost = 0.0
        if cost:
            turnCost = (promptTokens * cost['input'] + completionTokens * cost['output']
                        + cachedTokens * cost['cacheRead']) / 1_000_000
        modelBucket = bucket['byModel'].setdefault(
            modelKey, {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0, 'cost': 0.0}
        )
        for target in (bucket, modelBucket):
            target['promptTokens'] += promptTokens
            target['cachedTokens'] += cachedTokens
            target['completionTokens'] += completionTokens
            target['cost'] += turnCost
    return {
        'granularity': granularity,
        'models': sorted({key for bucket in buckets.values() for key in bucket['byModel']}),
        'buckets': list(buckets.values()),
    }
