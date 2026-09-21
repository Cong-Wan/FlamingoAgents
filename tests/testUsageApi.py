'''
Author: wilbur
Version: 1.0
Date: 2026-09-21
Description: GET /api/usage 单一 period 快照、非法 period 400、删除会话前 drain，以及查询路径不读 sessions/JSONL/models。
'''

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from flamingoAgents.utils import usageLedger
from webApp.backend import auth, server


def authHeaders(monkeypatch, token='usage-api-token'):
    monkeypatch.setattr(auth, 'serverToken', token)
    return {'Authorization': f'Bearer {token}'}


def freezeLedger(monkeypatch, text='2026-09-21T08:55:46+08:00'):
    monkeypatch.setattr(usageLedger, 'clockNow', lambda: datetime.fromisoformat(text))
    monkeypatch.setattr(usageLedger, 'timeZoneName', 'Asia/Shanghai')


def testUsagePeriodDefaultAndIllegal(monkeypatch):
    freezeLedger(monkeypatch)
    headers = authHeaders(monkeypatch)
    with TestClient(server.app) as client:
        ok = client.get('/api/usage', headers=headers)
        bad = client.get('/api/usage?period=lastYear', headers=headers)
        series = client.get('/api/usage/series?granularity=day', headers=headers)
    assert ok.status_code == 200
    body = ok.json()
    assert body['period'] == 'last7Days'
    assert body['timeZone'] == 'Asia/Shanghai'
    assert 'sessions' not in body
    assert body['totals']['callCount'] == 0
    assert body['providers'] == []
    assert bad.status_code == 400
    assert series.status_code == 404


def testUsageSnapshotConservationAndNoSideReads(monkeypatch, tmp_path):
    freezeLedger(monkeypatch)
    headers = authHeaders(monkeypatch)
    usageLedger.insertUsageEvent(usageLedger.makeExactRecord(
        usageKey='web-1',
        source='web',
        sessionId='s1',
        parentSessionId=None,
        providerId='subGPT',
        modelId='gpt-6-astra',
        requestStartedAt='2026-09-21T01:00:00+08:00',
        occurredAt='2026-09-21T01:10:00+08:00',
        tokens={'promptTokens': 80, 'cachedTokens': 10, 'completionTokens': 20},
        costFields={'input': Decimal('1'), 'output': Decimal('2'), 'cacheRead': Decimal('0.1')},
    ))
    usageLedger.insertUsageEvent(usageLedger.makeExactRecord(
        usageKey='cli-1',
        source='cli',
        sessionId='s2',
        parentSessionId=None,
        providerId='glm',
        modelId='glm-5.3',
        requestStartedAt='2026-09-21T02:00:00+08:00',
        occurredAt='2026-09-21T02:10:00+08:00',
        tokens={'promptTokens': 30, 'cachedTokens': 5, 'completionTokens': 7},
        costFields=None,
    ))
    calls = []
    monkeypatch.setattr(server.sessionStore, 'listSessions', lambda: calls.append('sessions') or [])
    monkeypatch.setattr(usageLedger, 'modelsYamlPath', tmp_path / 'missing.yaml')
    with TestClient(server.app) as client:
        response = client.get('/api/usage?period=today', headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert calls == []
    assert body['period'] == 'today'
    assert body['totals']['callCount'] == 2
    assert body['totals']['promptTokens'] == 110
    assert body['totals']['cachedTokens'] == 15
    assert body['totals']['completionTokens'] == 27
    assert body['totals']['totalTokens'] == 137
    assert body['totals']['costStatus'] == 'partial'
    providerSum = sum(item['totals']['totalTokens'] for item in body['providers'])
    modelSum = sum(model['totals']['totalTokens'] for item in body['providers'] for model in item['models'])
    assert providerSum == modelSum == body['totals']['totalTokens']
    assert [item['providerId'] for item in body['providers']] == ['subGPT', 'glm']
    assert body['providers'][0]['bucketUnit'] == 'hour'
    assert 'sessions' not in body


def testDeleteSessionDrainsUsageBeforeUnlink(monkeypatch, tmp_path):
    sessionId = 'drain-sess'
    logDir = tmp_path / 'logs'
    logDir.mkdir()
    logPath = logDir / f'{sessionId}.jsonl'
    logPath.write_text('{}\n', encoding='utf-8')
    drained = []
    monkeypatch.setattr(server.sessionStore, 'getSession', lambda requestedId: {
        'sessionId': sessionId, 'workDir': str(tmp_path),
    } if requestedId == sessionId else None)
    monkeypatch.setattr(server.sessionStore, 'deleteSession', lambda _sid: None)
    monkeypatch.setattr(server.agentManager, 'hasActiveStream', lambda _sid: False)
    monkeypatch.setattr(server.agentManager, 'dropAgent', lambda _sid: None)
    monkeypatch.setattr(server, 'resolveSessionLogDir', lambda category, path: logDir)
    monkeypatch.setattr(server.usageLedger, 'drainLogFile', lambda path: drained.append(path))
    headers = authHeaders(monkeypatch)
    with TestClient(server.app) as client:
        response = client.delete(f'/api/sessions/{sessionId}', headers=headers)
    assert response.status_code == 200
    assert drained == [logPath]
    assert not logPath.exists()


def testDeleteSessionKeepsLogWhenDrainFails(monkeypatch, tmp_path):
    sessionId = 'keep-sess'
    logDir = tmp_path / 'logs'
    logDir.mkdir()
    logPath = logDir / f'{sessionId}.jsonl'
    logPath.write_text('{}\n', encoding='utf-8')
    monkeypatch.setattr(server.sessionStore, 'getSession', lambda requestedId: {
        'sessionId': sessionId, 'workDir': str(tmp_path),
    } if requestedId == sessionId else None)
    monkeypatch.setattr(server.agentManager, 'hasActiveStream', lambda _sid: False)
    monkeypatch.setattr(server, 'resolveSessionLogDir', lambda category, path: logDir)

    def failDrain(path):
        raise usageLedger.usageLedgerError('用量尚未补齐，无法删除会话日志')

    monkeypatch.setattr(server.usageLedger, 'drainLogFile', failDrain)
    headers = authHeaders(monkeypatch)
    with TestClient(server.app) as client:
        response = client.delete(f'/api/sessions/{sessionId}', headers=headers)
    assert response.status_code == 409
    assert logPath.exists()
