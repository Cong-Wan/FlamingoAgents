'''
Author: wilbur
Version: 1.0
Date: 2026-09-07
Description: Tests ~/.flamingo session index paths, CRUD/history reads, non-destructive legacy migration, new-index precedence, and invalid-index/write-failure safety using temporary data only.
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


def writeIndexFile(path: Path, sessions: list[dict]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'sessions': sessions}, ensure_ascii=False), encoding='utf-8')
    return path.read_bytes()


def testDefaultIndexUsesFlamingoHome():
    expectedDir = Path.home() / '.flamingo' / 'logs' / 'webData'
    assert sessionStore.webDataDir == expectedDir
    assert sessionStore.indexPath == expectedDir / 'sessions.json'


def testMissingIndexReadDoesNotCreateDirectories(indexPaths):
    webDir, legacyPath = indexPaths
    assert sessionStore.listSessions() == []
    assert not webDir.exists()
    assert not legacyPath.parent.exists()


def testCrudPersistsOnlyInFlamingo(indexPaths):
    webDir, legacyPath = indexPaths
    session = sessionStore.createSession('/work-dir', 'provider', 'model')
    sessionId = session['sessionId']
    sessionStore.setDefaultTitle(sessionId, '首条消息')
    sessionStore.renameSession(sessionId, '自定义标题')
    sessionStore.updateSessionModel(sessionId, 'otherProvider', 'otherModel')
    usage = {'promptTokens': 12, 'cachedTokens': 3, 'completionTokens': 5}
    sessionStore.updateUsage(sessionId, usage, contextTokens=17, lastUsage=usage)
    sessionStore.touchSession(sessionId)
    stored = json.loads((webDir / 'sessions.json').read_text(encoding='utf-8'))['sessions'][0]
    assert stored == sessionStore.getSession(sessionId)
    assert stored['title'] == '自定义标题'
    assert stored['providerId'] == 'otherProvider'
    assert stored['modelId'] == 'otherModel'
    assert stored['usage'] == stored['lastUsage'] == usage
    assert stored['contextTokens'] == 17
    assert not legacyPath.parent.exists()
    assert sessionStore.deleteSession(sessionId)
    assert sessionStore.listSessions() == []
    assert json.loads((webDir / 'sessions.json').read_text(encoding='utf-8')) == {'sessions': []}
    assert not legacyPath.parent.exists()


def testHistoryReadsFlamingoIndexAndLogWithoutRepositoryData(indexPaths):
    webDir, legacyPath = indexPaths
    session = sessionStore.createSession('/work-dir', 'provider', 'model')
    logDir = logPaths.ensureSessionLogDir('webData', Path(session['workDir']))
    assert logDir.parent == webDir
    logPath = logDir / f'{session["sessionId"]}.jsonl'
    logPath.write_text('\n'.join(json.dumps(event) for event in [
        {'type': 'userMessage', 'content': 'hello'},
        {'type': 'assistantMessage', 'content': 'world'},
    ]) + '\n', encoding='utf-8')
    originalBytes = logPath.read_bytes()
    assert [item['content'] for item in historyView.loadMessages(session['sessionId'])] == ['hello', 'world']
    assert logPath.read_bytes() == originalBytes
    assert not legacyPath.parent.exists()


def testLegacyIndexIsCopiedOnceAndSurvivesLegacyDirectoryRemoval(indexPaths):
    webDir, legacyPath = indexPaths
    originalSession = {
        'sessionId': 'session_old', 'title': '保留标题', 'workDir': '/work-dir',
        'providerId': 'provider', 'modelId': 'model', 'createdAt': '2026-08-01',
        'updatedAt': '2026-08-02', 'usage': {'promptTokens': 42},
        'contextTokens': 42, 'lastUsage': {'completionTokens': 7}, 'extra': {'keep': True},
    }
    originalBytes = writeIndexFile(legacyPath, [originalSession])
    assert sessionStore.listSessions() == [originalSession]
    assert (webDir / 'sessions.json').is_file()
    assert legacyPath.read_bytes() == originalBytes
    sessionStore.renameSession('session_old', '新标题')
    assert legacyPath.read_bytes() == originalBytes
    legacyPath.unlink()
    legacyPath.parent.rmdir()
    assert sessionStore.getSession('session_old')['title'] == '新标题'
    assert not legacyPath.parent.exists()


@pytest.mark.parametrize('sessions', [[], [{'sessionId': 'session_new', 'title': '新索引'}]])
def testExistingFlamingoIndexWinsIncludingEmptyIndex(indexPaths, sessions):
    webDir, legacyPath = indexPaths
    originalBytes = writeIndexFile(legacyPath, [{'sessionId': 'session_old'}])
    writeIndexFile(webDir / 'sessions.json', sessions)
    assert sessionStore.listSessions() == sessions
    assert legacyPath.read_bytes() == originalBytes


@pytest.mark.parametrize('text', [
    '{broken', '[]', '{"sessions": {}}', '{"sessions": [null]}',
    '{"sessions": [{"sessionId": ""}]}', '{"sessions": [{"sessionId": 42}]}',
    '{"sessions": [{"sessionId": "same"}, {"sessionId": "same"}]}',
])
def testInvalidLegacyIndexIsNotCopiedOrOverwritten(indexPaths, text):
    webDir, legacyPath = indexPaths
    legacyPath.parent.mkdir(parents=True)
    legacyPath.write_text(text, encoding='utf-8')
    with pytest.raises(RuntimeError, match='会话索引'):
        sessionStore.createSession('/work-dir', 'provider', 'model')
    assert legacyPath.read_text(encoding='utf-8') == text
    assert not (webDir / 'sessions.json').exists()


def testInvalidFlamingoIndexDoesNotFallBackOrGetOverwritten(indexPaths):
    webDir, legacyPath = indexPaths
    writeIndexFile(legacyPath, [{'sessionId': 'session_old'}])
    indexFile = webDir / 'sessions.json'
    webDir.mkdir(parents=True)
    indexFile.write_text('{broken', encoding='utf-8')
    with pytest.raises(RuntimeError, match='会话索引'):
        sessionStore.createSession('/work-dir', 'provider', 'model')
    assert indexFile.read_text(encoding='utf-8') == '{broken'


def testLegacyMigrationWriteFailurePreservesSource(indexPaths, monkeypatch):
    webDir, legacyPath = indexPaths
    originalBytes = writeIndexFile(legacyPath, [{'sessionId': 'session_old'}])

    def failReplace(source, target):
        raise OSError('disk failure')

    monkeypatch.setattr(sessionStore.os, 'replace', failReplace)
    with pytest.raises(OSError, match='disk failure'):
        sessionStore.loadIndex()
    assert legacyPath.read_bytes() == originalBytes
    assert not (webDir / 'sessions.json').exists()


def testUnreadableLegacyIndexIsNotTreatedAsEmpty(indexPaths, monkeypatch):
    webDir, legacyPath = indexPaths
    originalBytes = writeIndexFile(legacyPath, [{'sessionId': 'session_old'}])
    readText = Path.read_text

    def guardedRead(path, *args, **kwargs):
        if path == legacyPath:
            raise PermissionError('denied')
        return readText(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guardedRead)
    with pytest.raises(RuntimeError, match='会话索引'):
        sessionStore.createSession('/work-dir', 'provider', 'model')
    assert legacyPath.read_bytes() == originalBytes
    assert not (webDir / 'sessions.json').exists()
