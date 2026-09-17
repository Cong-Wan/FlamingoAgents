'''
Author: wilbur
Version: 1.0
Date: 2026-09-15
Description: GET messages DTO 对终态/重试 modelError 的过滤与文案对齐（chatUxImprovePlan T11）。
'''

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flamingoAgents.utils import logPaths
from webApp.backend import historyView, sessionStore


@pytest.fixture
def indexPaths(tmp_path: Path, monkeypatch):
    webDir = tmp_path / 'home' / '.flamingo' / 'logs' / 'webData'
    legacyPath = tmp_path / 'repo' / 'webData' / 'sessions.json'
    monkeypatch.setattr(sessionStore, 'webDataDir', webDir)
    monkeypatch.setattr(sessionStore, 'indexPath', webDir / 'sessions.json')
    monkeypatch.setattr(sessionStore, 'legacyIndexPath', legacyPath, raising=False)
    monkeypatch.setitem(logPaths._categoryRoots, 'webData', webDir)
    return webDir, legacyPath


def writeLog(session: dict, events: list[dict]) -> Path:
    logDir = logPaths.ensureSessionLogDir('webData', Path(session['workDir']))
    logPath = logDir / f'{session["sessionId"]}.jsonl'
    logPath.write_text('\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + '\n', encoding='utf-8')
    return logPath


def testFinalModelErrorBecomesKindError(indexPaths):
    session = sessionStore.createSession('/work-dir', 'provider', 'model')
    writeLog(session, [
        {'type': 'userMessage', 'content': 'hello', 'timestamp': 't0'},
        {
            'type': 'modelError',
            'errorType': 'HTTPError',
            'message': '502 bad gateway',
            'attempt': 4,
            'willRetry': False,
            'request': {'huge': 'no'},
            'traceback': 'secret',
            'timestamp': 't1',
        },
    ])
    messages = historyView.loadMessages(session['sessionId'])
    assert [item['kind'] for item in messages] == ['user', 'error']
    error = messages[1]
    assert error['content'] == '模型调用失败（已重试3次）：502 bad gateway'
    assert error['errorType'] == 'HTTPError'
    assert error['attempt'] == 4
    assert error['timestamp'] == 't1'
    assert 'request' not in error
    assert 'traceback' not in error


def testRetryModelErrorIsSkipped(indexPaths):
    session = sessionStore.createSession('/work-dir', 'provider', 'model')
    writeLog(session, [
        {'type': 'userMessage', 'content': 'hello'},
        {'type': 'modelError', 'errorType': 'HTTPError', 'message': '429', 'attempt': 1, 'willRetry': True},
        {'type': 'modelError', 'errorType': 'HTTPError', 'message': 'still 429', 'attempt': 3, 'willRetry': False},
        {'type': 'assistantMessage', 'content': 'recovered'},
    ])
    messages = historyView.loadMessages(session['sessionId'])
    assert [item['kind'] for item in messages] == ['user', 'error', 'assistant']
    assert messages[1]['content'] == '模型调用失败（已重试2次）：still 429'


def testMissingWillRetryCountsAsFinal(indexPaths):
    session = sessionStore.createSession('/work-dir', 'provider', 'model')
    writeLog(session, [
        {'type': 'modelError', 'errorType': 'Timeout', 'message': 'old log'},
    ])
    messages = historyView.loadMessages(session['sessionId'])
    assert len(messages) == 1
    assert messages[0]['kind'] == 'error'
    assert messages[0]['content'] == '模型调用失败（已重试0次）：old log'
    assert messages[0]['attempt'] == 1
