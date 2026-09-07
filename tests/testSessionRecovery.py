'''
Author: wilbur
Version: 1.0
Date: 2026-09-07
Description: Tests missing-index history recovery, legacy JSON arrays with appended JSONL, strict recovery validation, read-only sources, explicit workDir mapping, and atomic no-overwrite publication using temporary files.
'''

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from flamingoAgents.utils.jsonl import jsonlLog
from flamingoAgents.utils.logPaths import workDirFolderName
from webApp.backend import historyView, sessionStore


@pytest.fixture
def savedHistory(tmp_path: Path):
    sourceRoot = tmp_path / '.flamingo' / 'logs'
    workDir = tmp_path / 'project-with-hyphen'
    logDir = sourceRoot / 'webData' / workDirFolderName(workDir)
    logDir.mkdir(parents=True)
    events = [
        {'type': 'systemMessage', 'content': 'system', 'timestamp': '2026-09-01T00:00:00+00:00'},
        {'type': 'userMessage', 'content': '应该读取的旧对话', 'timestamp': '2026-09-01T00:00:01+00:00'},
        {'type': 'assistantMessage', 'content': '旧对话回复', 'timestamp': '2026-09-01T00:00:02+00:00',
         'model': 'response-model-alias', 'usage': {'prompt_tokens': 20, 'completion_tokens': 5,
         'prompt_tokens_details': {'cached_tokens': 8}}},
    ]
    for sessionId, isArray in [('session_jsonl', False), ('session_array', True)]:
        text = json.dumps(events, ensure_ascii=False, indent=2) if isArray else '\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + '\n'
        (logDir / f'{sessionId}.jsonl').write_text(text, encoding='utf-8')
    dbPath = sourceRoot / 'usage.db'
    connection = sqlite3.connect(dbPath)
    try:
        connection.execute('CREATE TABLE usageTurns (id INTEGER PRIMARY KEY, sessionId TEXT, providerId TEXT, modelId TEXT, promptTokens INTEGER, cachedTokens INTEGER, completionTokens INTEGER)')
        connection.executemany('INSERT INTO usageTurns VALUES (?, ?, ?, ?, ?, ?, ?)', [
            (1, 'session_jsonl', 'oldProvider', 'oldModel', 10, 0, 1),
            (2, 'session_jsonl', 'actualProvider', 'configuredModel', 20, 8, 5),
            (3, 'session_array', 'actualProvider', 'configuredModel', 20, 8, 5),
        ])
        connection.commit()
    finally:
        connection.close()
    return sourceRoot, workDir, logDir, events


def testArrayHistoryCanBeReadWithoutRewriting(savedHistory):
    sourceRoot, workDir, logDir, events = savedHistory
    logPath = logDir / 'session_array.jsonl'
    before = logPath.read_bytes()
    assert jsonlLog(logPath).readEvents() == events
    assert logPath.read_bytes() == before


def testArrayHistoryRemainsReadableAfterAppend(savedHistory):
    sourceRoot, workDir, logDir, events = savedHistory
    logPath = logDir / 'session_array.jsonl'
    logger = jsonlLog(logPath)
    nextEvent = {'type': 'userMessage', 'content': '继续旧对话', 'timestamp': '2026-09-02T00:00:00+00:00'}
    logger.logEvent(nextEvent)
    assert logger.readEvents() == events + [nextEvent]


def testJsonlStillSkipsIncompleteTailAndNonObjectRows(tmp_path: Path):
    logPath = tmp_path / 'tail.jsonl'
    event = {'type': 'userMessage', 'content': 'message'}
    logPath.write_text(json.dumps(event) + '\nnull\n{"incomplete":', encoding='utf-8')
    assert jsonlLog(logPath).readEvents() == [event]


def testMissingIndexRecoversExistingMessagesAndUsage(savedHistory, tmp_path: Path, monkeypatch):
    from webApp.backend.sessionRecovery import recoverSessions, writeRecoveredIndex

    sourceRoot, workDir, logDir, events = savedHistory
    sourceBytes = {path: path.read_bytes() for path in [*logDir.glob('*.jsonl'), sourceRoot / 'usage.db']}
    indexPath = sourceRoot / 'webData' / 'sessions.json'
    legacyPath = tmp_path / 'repo' / 'webData' / 'sessions.json'
    monkeypatch.setattr(sessionStore, 'indexPath', indexPath)
    monkeypatch.setattr(sessionStore, 'legacyIndexPath', legacyPath)
    monkeypatch.setattr(historyView, 'resolveSessionLogDir', lambda category, path: sourceRoot / category / workDirFolderName(path))
    assert sessionStore.listSessions() == []
    sessions = recoverSessions([workDir], sourceRoot=sourceRoot)
    assert not indexPath.exists()
    writeRecoveredIndex(indexPath, sessions)
    assert len(sessionStore.listSessions()) == 2
    for session in sessionStore.listSessions():
        assert session['workDir'] == str(workDir.resolve())
        assert session['providerId'] == 'actualProvider'
        assert session['modelId'] == 'configuredModel'
        assert session['title'] == '应该读取的旧对话'
        assert session['usage'] == session['lastUsage'] == {'promptTokens': 20, 'cachedTokens': 8, 'completionTokens': 5}
        assert session['contextTokens'] == 25
        assert session['createdAt'] == events[0]['timestamp']
        assert session['updatedAt'] == events[-1]['timestamp']
        assert [message['content'] for message in historyView.loadMessages(session['sessionId'])] == ['应该读取的旧对话', '旧对话回复']
    assert not legacyPath.parent.exists()
    assert not workDir.exists()
    assert all(path.read_bytes() == content for path, content in sourceBytes.items())


def testRecoveryNeverOverwritesAnExistingIndex(savedHistory):
    from webApp.backend.sessionRecovery import recoverSessions, writeRecoveredIndex

    sourceRoot, workDir, logDir, events = savedHistory
    indexPath = sourceRoot / 'webData' / 'sessions.json'
    indexPath.write_text('{"sessions": []}', encoding='utf-8')
    originalBytes = indexPath.read_bytes()
    sessions = recoverSessions([workDir], sourceRoot=sourceRoot)
    with pytest.raises(FileExistsError):
        writeRecoveredIndex(indexPath, sessions)
    assert indexPath.read_bytes() == originalBytes


def testRecoveryDoesNotGuessMissingProvider(savedHistory):
    from webApp.backend.sessionRecovery import recoverSessions

    sourceRoot, workDir, logDir, events = savedHistory
    (logDir / 'session_unknown.jsonl').write_text(json.dumps(events[1]) + '\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='模型记录'):
        recoverSessions([workDir], sourceRoot=sourceRoot)
    assert not (sourceRoot / 'webData' / 'sessions.json').exists()


def testRecoveryRejectsCollidingWorkDirMappings(savedHistory, tmp_path: Path):
    from webApp.backend.sessionRecovery import recoverSessions

    sourceRoot, workDir, logDir, events = savedHistory
    with pytest.raises(RuntimeError, match='工作目录'):
        recoverSessions([tmp_path / 'a-b' / 'c', tmp_path / 'a' / 'b-c'], sourceRoot=sourceRoot)


@pytest.mark.parametrize('invalidText', ['null\n', '{broken\n{}\n', '{}\n{truncated', '[{broken', '[null]'])
def testRecoveryDoesNotSilentlySkipMalformedEvents(savedHistory, invalidText):
    from webApp.backend.sessionRecovery import recoverSessions

    sourceRoot, workDir, logDir, events = savedHistory
    (logDir / 'session_jsonl.jsonl').write_text(invalidText, encoding='utf-8')
    with pytest.raises(RuntimeError, match='会话日志'):
        recoverSessions([workDir], sourceRoot=sourceRoot)
    assert not (sourceRoot / 'webData' / 'sessions.json').exists()


def testRecoveryReadDoesNotCreateMissingDirectories(tmp_path: Path):
    from webApp.backend.sessionRecovery import readRecoveryEvents

    logPath = tmp_path / 'missing' / 'session.jsonl'
    with pytest.raises(FileNotFoundError):
        readRecoveryEvents(logPath)
    assert not logPath.parent.exists()


def testRecoveryDoesNotCreateMissingUsageDatabase(tmp_path: Path):
    from webApp.backend.sessionRecovery import recoverSessions

    sourceRoot = tmp_path / 'missing'
    with pytest.raises(sqlite3.OperationalError):
        recoverSessions([tmp_path], sourceRoot=sourceRoot)
    assert not sourceRoot.exists()
