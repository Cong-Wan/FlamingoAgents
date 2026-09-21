'''
Author: wilbur
Version: 1.0
Date: 2026-09-21
Description: 调用级用量账本：schema、幂等写入、价格快照、六种 period、单快照守恒、legacy JSONL 迁移与坏行停扫。
'''

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from flamingoAgents.utils import usageLedger


def writeRecord(**overrides):
    tokens = {
        'promptTokens': overrides.pop('promptTokens', 100),
        'cachedTokens': overrides.pop('cachedTokens', 20),
        'completionTokens': overrides.pop('completionTokens', 10),
    }
    record = usageLedger.makeExactRecord(
        usageKey=overrides.pop('usageKey', usageLedger.newUsageKey()),
        source=overrides.pop('source', 'web'),
        sessionId=overrides.pop('sessionId', 'sess-1'),
        parentSessionId=overrides.pop('parentSessionId', None),
        providerId=overrides.pop('providerId', 'glm'),
        modelId=overrides.pop('modelId', 'glm-5.3'),
        requestStartedAt=overrides.pop('requestStartedAt', '2026-09-21T00:00:00+00:00'),
        occurredAt=overrides.pop('occurredAt', '2026-09-21T01:00:00+00:00'),
        tokens=tokens,
        costFields=overrides.pop('costFields', {
            'input': Decimal('1'),
            'output': Decimal('2'),
            'cacheRead': Decimal('0.1'),
        }),
    )
    record.update(overrides)
    return record


def insert(**kwargs):
    record = writeRecord(**kwargs)
    usageLedger.insertUsageEvent(record)
    return record


def freezeClock(monkeypatch, text: str):
    moment = datetime.fromisoformat(text)
    monkeypatch.setattr(usageLedger, 'clockNow', lambda: moment)
    monkeypatch.setattr(usageLedger, 'timeZoneName', 'Asia/Shanghai')


def testSchemaWalBusyTimeoutAndIndexes():
    usageLedger.ensureSchema()
    connection = usageLedger.getConnection()
    journal = connection.execute('PRAGMA journal_mode').fetchone()[0]
    timeout = connection.execute('PRAGMA busy_timeout').fetchone()[0]
    columns = {row[1] for row in connection.execute('PRAGMA table_info(usageEvents)').fetchall()}
    indexes = {row[1] for row in connection.execute('PRAGMA index_list(usageEvents)').fetchall()}
    assert journal.lower() == 'wal'
    assert timeout == 5000
    assert 'usageKey' in columns
    assert 'idxUsageEventsOccurred' in indexes
    assert 'idxUsageEventsSession' in indexes
    version = connection.execute("SELECT value FROM usageMeta WHERE key = 'schemaVersion'").fetchone()[0]
    assert version == '1'


def testInsertSameKeyIsIdempotent():
    record = writeRecord(usageKey='same-key')
    assert usageLedger.insertUsageEvent(record) is True
    assert usageLedger.insertUsageEvent(record) is False
    assert usageLedger.countEvents() == 1


def testParseExactUsageContract():
    assert usageLedger.parseExactUsage({'prompt_tokens': 10, 'completion_tokens': 2}) == {
        'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2,
    }
    assert usageLedger.parseExactUsage({
        'prompt_tokens': 10, 'completion_tokens': 2,
        'prompt_tokens_details': {'cached_tokens': 4},
    })['cachedTokens'] == 4
    assert usageLedger.parseExactUsage(None) is None
    assert usageLedger.parseExactUsage({}) is None
    assert usageLedger.parseExactUsage({'prompt_tokens': -1, 'completion_tokens': 1}) is None
    assert usageLedger.parseExactUsage({'prompt_tokens': True, 'completion_tokens': 1}) is None
    assert usageLedger.parseExactUsage({'prompt_tokens': 1.5, 'completion_tokens': 1}) is None
    assert usageLedger.parseExactUsage({
        'prompt_tokens': 3, 'completion_tokens': 1,
        'prompt_tokens_details': {'cached_tokens': 4},
    }) is None
    assert usageLedger.parseExactUsage({'prompt_tokens': 0, 'completion_tokens': 0}) == {
        'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0,
    }


def testExplicitZeroPriceIsNotUnknown():
    zero = usageLedger.extractCostFields({'input': 0, 'output': 0, 'cacheRead': 0})
    nano, status = usageLedger.priceSnapshot(100, 10, 20, zero)
    assert nano == 0
    assert status == 'snapshot'
    assert usageLedger.extractCostFields({'input': 1, 'output': 2}) is None
    missingNano, missingStatus = usageLedger.priceSnapshot(100, 10, 20, None)
    assert missingNano is None
    assert missingStatus == 'unknown'


def testPeriodBoundsUseHalfOpenLocalDays(monkeypatch):
    freezeClock(monkeypatch, '2026-09-21T08:55:46+08:00')
    tz = ZoneInfo('Asia/Shanghai')
    snapshot = usageLedger.clockNow()
    today = usageLedger.periodBounds('today', snapshot, tz)
    assert today[0].isoformat() == '2026-09-21T00:00:00+08:00'
    assert today[1].isoformat() == '2026-09-21T08:55:46+08:00'
    yesterday = usageLedger.periodBounds('yesterday', snapshot, tz)
    assert yesterday[0].isoformat() == '2026-09-20T00:00:00+08:00'
    assert yesterday[1].isoformat() == '2026-09-21T00:00:00+08:00'
    last7 = usageLedger.periodBounds('last7Days', snapshot, tz)
    assert last7[0].isoformat() == '2026-09-15T00:00:00+08:00'
    last30 = usageLedger.periodBounds('last30Days', snapshot, tz)
    assert last30[0].isoformat() == '2026-08-23T00:00:00+08:00'
    thisMonth = usageLedger.periodBounds('thisMonth', snapshot, tz)
    assert thisMonth[0].isoformat() == '2026-09-01T00:00:00+08:00'
    lastMonth = usageLedger.periodBounds('lastMonth', snapshot, tz)
    assert lastMonth[0].isoformat() == '2026-08-01T00:00:00+08:00'
    assert lastMonth[1].isoformat() == '2026-09-01T00:00:00+08:00'


def testUnknownPeriodRaises():
    with pytest.raises(usageLedger.unknownPeriodError):
        usageLedger.queryUsageSnapshot('lastYear')


def testSnapshotConservationAndSorting(monkeypatch):
    freezeClock(monkeypatch, '2026-09-21T08:00:00+08:00')
    insert(usageKey='a', providerId='subGPT', modelId='gpt-6-astra', promptTokens=80, cachedTokens=10, completionTokens=20, occurredAt='2026-09-21T01:00:00+08:00')
    insert(usageKey='b', providerId='glm', modelId='glm-5.3', promptTokens=30, cachedTokens=5, completionTokens=7, occurredAt='2026-09-21T02:00:00+08:00')
    insert(usageKey='c', providerId='subGPT', modelId='gpt-5.6-sol', promptTokens=10, cachedTokens=0, completionTokens=1, occurredAt='2026-09-21T03:00:00+08:00')
    insert(usageKey='d', providerId='glm', modelId='glm-5.3', promptTokens=0, cachedTokens=0, completionTokens=0, occurredAt='2026-09-21T04:00:00+08:00')
    snapshot = usageLedger.queryUsageSnapshot('today')
    totals = snapshot['totals']
    providerSum = {key: 0 for key in ('callCount', 'promptTokens', 'cachedTokens', 'completionTokens', 'totalTokens')}
    modelSum = {key: 0 for key in providerSum}
    for provider in snapshot['providers']:
        for key in providerSum:
            providerSum[key] += provider['totals'][key]
        for model in provider['models']:
            for key in modelSum:
                modelSum[key] += model['totals'][key]
    assert totals['callCount'] == 4
    assert totals['promptTokens'] == 120
    assert totals['cachedTokens'] == 15
    assert totals['completionTokens'] == 28
    assert totals['totalTokens'] == 148
    assert providerSum == {key: totals[key] for key in providerSum}
    assert modelSum == {key: totals[key] for key in modelSum}
    assert [item['providerId'] for item in snapshot['providers']] == ['subGPT', 'glm']
    assert [item['modelId'] for item in snapshot['providers'][0]['models']] == ['gpt-6-astra', 'gpt-5.6-sol']
    assert snapshot['providers'][1]['totals']['callCount'] == 2


def testTodayBucketsStopAtCurrentHour(monkeypatch):
    freezeClock(monkeypatch, '2026-09-21T08:55:46+08:00')
    insert(occurredAt='2026-09-21T08:10:00+08:00', promptTokens=5, cachedTokens=1, completionTokens=1)
    snapshot = usageLedger.queryUsageSnapshot('today')
    assert snapshot['providers'][0]['bucketUnit'] == 'hour'
    labels = [bucket['label'] for bucket in snapshot['providers'][0]['buckets']]
    assert labels[0] == '00:00'
    assert labels[-1] == '08:00'
    assert len(labels) == 9


def testYesterdayHasTwentyFourHours(monkeypatch):
    freezeClock(monkeypatch, '2026-09-21T08:55:46+08:00')
    insert(occurredAt='2026-09-20T00:30:00+08:00')
    snapshot = usageLedger.queryUsageSnapshot('yesterday')
    buckets = snapshot['providers'][0]['buckets']
    assert len(buckets) == 24
    assert buckets[0]['label'] == '00:00'
    assert buckets[-1]['label'] == '23:00'


def testBoundaryRecordBelongsToNextPeriod(monkeypatch):
    freezeClock(monkeypatch, '2026-09-21T08:00:00+08:00')
    insert(usageKey='edge', occurredAt='2026-09-21T00:00:00+08:00', promptTokens=9, cachedTokens=0, completionTokens=1)
    today = usageLedger.queryUsageSnapshot('today')
    yesterday = usageLedger.queryUsageSnapshot('yesterday')
    assert today['totals']['promptTokens'] == 9
    assert yesterday['totals']['promptTokens'] == 0


def testLeapYearAndMonthStart(monkeypatch):
    freezeClock(monkeypatch, '2024-03-01T10:00:00+08:00')
    insert(usageKey='leap', occurredAt='2024-02-29T12:00:00+08:00', promptTokens=4, cachedTokens=0, completionTokens=1)
    lastMonth = usageLedger.queryUsageSnapshot('lastMonth')
    assert lastMonth['startAt'] == '2024-02-01T00:00:00+08:00'
    assert lastMonth['endAt'] == '2024-03-01T00:00:00+08:00'
    assert lastMonth['totals']['promptTokens'] == 4


def testDstSpringForward(monkeypatch):
    freezeClock(monkeypatch, '2026-03-08T12:00:00-04:00')
    monkeypatch.setattr(usageLedger, 'timeZoneName', 'America/New_York')
    tz = ZoneInfo('America/New_York')
    snapshot = usageLedger.clockNow()
    todayStart, todayEnd = usageLedger.periodBounds('today', snapshot, tz)
    buckets = usageLedger.iterBuckets('today', todayStart, todayEnd)
    starts = [item[0].isoformat() for item in buckets]
    assert '2026-03-08T00:00:00-05:00' in starts
    assert '2026-03-08T03:00:00-04:00' in starts
    assert todayStart.isoformat() == '2026-03-08T00:00:00-05:00'


def testUnknownCostStatus(monkeypatch):
    freezeClock(monkeypatch, '2026-09-21T08:00:00+08:00')
    insert(usageKey='priced', costFields={'input': Decimal('1'), 'output': Decimal('1'), 'cacheRead': Decimal('1')}, occurredAt='2026-09-21T01:00:00+08:00')
    insert(usageKey='unpriced', costFields=None, occurredAt='2026-09-21T02:00:00+08:00')
    snapshot = usageLedger.queryUsageSnapshot('today')
    assert snapshot['totals']['costStatus'] == 'partial'
    assert snapshot['totals']['unpricedCallCount'] == 1
    assert snapshot['quality']['unknownPriceRecords'] == 1


def testEmptySnapshotZeros(monkeypatch):
    freezeClock(monkeypatch, '2026-09-21T08:00:00+08:00')
    snapshot = usageLedger.queryUsageSnapshot('today')
    assert snapshot['providers'] == []
    assert snapshot['totals']['callCount'] == 0
    assert snapshot['totals']['costNanoUsd'] == 0
    assert snapshot['totals']['costStatus'] == 'complete'


def testNativeUsageRecordReconcileIsIdempotent(tmp_path):
    logPath = tmp_path / 'usage-ledger-logs' / 'webData' / 'work' / 'sess.jsonl'
    logPath.parent.mkdir(parents=True)
    record = writeRecord(usageKey='native-1', sessionId='sess', occurredAt='2026-09-21T01:00:00+00:00')
    event = {'type': 'usageRecord', **record, 'timestamp': '2026-09-21T01:00:00+00:00'}
    logPath.write_text(json.dumps(event, ensure_ascii=False) + '\n', encoding='utf-8')
    first = usageLedger.reconcileLogFile(logPath, importLegacy=False)
    second = usageLedger.reconcileLogFile(logPath, importLegacy=False)
    assert first['nativeImported'] == 1
    assert second['nativeImported'] == 0
    assert usageLedger.countEvents() == 1


def testLegacyAssistantImportUsesDeterministicKey(tmp_path, monkeypatch):
    freezeClock(monkeypatch, '2026-09-21T08:00:00+08:00')
    models = tmp_path / 'models.yaml'
    models.write_text(yaml.safe_dump({
        'providers': {
            'glm': {
                'models': [{'id': 'glm-5.3', 'cost': {'input': 1, 'output': 2, 'cacheRead': 0.1, 'cacheWrite': 0}}],
            }
        }
    }), encoding='utf-8')
    monkeypatch.setattr(usageLedger, 'modelsYamlPath', models)
    logPath = tmp_path / 'usage-ledger-logs' / 'webData' / 'work' / 'old.jsonl'
    logPath.parent.mkdir(parents=True)
    event = {
        'type': 'assistantMessage',
        'timestamp': '2026-09-20T12:00:00+00:00',
        'model': 'glm-5.3',
        'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 2}},
    }
    logPath.write_text(json.dumps(event, ensure_ascii=False) + '\n', encoding='utf-8')
    first = usageLedger.reconcileLogFile(logPath, importLegacy=True)
    second = usageLedger.reconcileLogFile(logPath, importLegacy=True)
    assert first['legacyImported'] == 1
    assert second['legacyImported'] == 0
    snapshot = usageLedger.queryUsageSnapshot('last7Days')
    assert snapshot['totals']['callCount'] == 1
    assert snapshot['quality']['legacyInferredRecords'] == 1
    assert snapshot['quality']['migratedPriceRecords'] == 1


def testBadJsonlStopsWithoutAdvancingCursor(tmp_path):
    logPath = tmp_path / 'usage-ledger-logs' / 'webData' / 'work' / 'bad.jsonl'
    logPath.parent.mkdir(parents=True)
    good = writeRecord(usageKey='ok-1', sessionId='bad')
    logPath.write_text(
        json.dumps({'type': 'usageRecord', **good}, ensure_ascii=False) + '\nnot-json\n',
        encoding='utf-8',
    )
    with pytest.raises(usageLedger.usageLedgerError):
        usageLedger.reconcileLogFile(logPath)
    connection = usageLedger.getConnection()
    cursor = connection.execute(
        'SELECT lastAckLine FROM usageReconcileCursors WHERE logPath = ?',
        (str(logPath.resolve()),),
    ).fetchone()
    assert cursor is None
    assert usageLedger.countEvents() == 0


def testDrainRejectsPendingRetry(monkeypatch):
    record = writeRecord(usageKey='pending-1')
    usageLedger.scheduleRetry(record)
    logPath = Path(usageLedger.logsRoot) / 'webData' / f'{record["sessionId"]}.jsonl'
    logPath.parent.mkdir(parents=True, exist_ok=True)
    logPath.write_text('', encoding='utf-8')
    monkeypatch.setattr(
        usageLedger,
        'insertUsageEvent',
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.Error('busy')),
    )
    with pytest.raises(usageLedger.usageLedgerError, match='待补写'):
        usageLedger.drainLogFile(logPath)


def testThreadContentionKeepsOneRow():
    record = writeRecord(usageKey='thread-key')
    errors = []

    def worker():
        try:
            usageLedger.insertUsageEvent(record)
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert usageLedger.countEvents() == 1


def _processInsert(dbPath: str, key: str):
    from flamingoAgents.utils import usageLedger as ledger
    ledger.resetState()
    ledger.dbPath = Path(dbPath)
    ledger.enableBackgroundRetry = False
    record = {
        'usageKey': key,
        'source': 'web',
        'sessionId': 'proc',
        'parentSessionId': None,
        'providerId': 'glm',
        'modelId': 'glm-5.3',
        'requestStartedAt': '2026-09-21T00:00:00.000+00:00',
        'occurredAt': '2026-09-21T01:00:00.000+00:00',
        'promptTokens': 1,
        'cachedTokens': 0,
        'completionTokens': 1,
        'costNanoUsd': 0,
        'pricingStatus': 'snapshot',
        'recordQuality': 'exact',
        'createdAt': '2026-09-21T01:00:00.000+00:00',
    }
    ledger.insertUsageEvent(record)


def testProcessContentionKeepsOneRow(tmp_path):
    dbFile = str(usageLedger.dbPath)
    usageLedger.ensureSchema()
    ctx = get_context('spawn')
    workers = [ctx.Process(target=_processInsert, args=(dbFile, 'proc-key')) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    usageLedger.resetState()
    usageLedger.dbPath = Path(dbFile)
    assert usageLedger.countEvents() == 1


def testQueryDoesNotNeedModelsYaml(monkeypatch, tmp_path):
    freezeClock(monkeypatch, '2026-09-21T08:00:00+08:00')
    insert(occurredAt='2026-09-21T01:00:00+08:00')
    monkeypatch.setattr(usageLedger, 'modelsYamlPath', tmp_path / 'missing.yaml')
    snapshot = usageLedger.queryUsageSnapshot('today')
    assert snapshot['totals']['callCount'] == 1
