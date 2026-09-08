'''
Author: wilbur
Version: 1.1
Date: 2026-09-07
Description: Accepts path-only @ references: no content inline, no count cap, archives allowed, preview NUL behavior unchanged. v1.1 兼容 runUserMessageStream 的 images 关键字参数。
'''

from __future__ import annotations

import io
import json
import os
import queue
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from webApp.backend import auth, fileBrowser, server


@pytest.fixture
def tempDir(request) -> Path:
    return request.getfixturevalue('tmp_path')


def encodedPath(path: Path) -> str:
    return json.dumps(str(path.resolve()), ensure_ascii=False)


def referenceBlock(paths: list[Path]) -> str:
    lines = ['引用路径（仅提供位置，未读取内容）：']
    lines.extend(f'- {encodedPath(path)}' for path in paths)
    return '\n'.join(lines)


@pytest.fixture(params=['zip', 'tar', 'tar.gz'])
def archivePath(request, tempDir: Path) -> Path:
    path = tempDir / ('sample.' + request.param)
    content = '压缩包内的原始文本'.encode('utf-8')
    if request.param == 'zip':
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('note.txt', content)
    else:
        with tarfile.open(path, 'w:gz' if request.param == 'tar.gz' else 'w') as archive:
            entry = tarfile.TarInfo('note.txt')
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return path


@pytest.mark.parametrize('fileName', ['note.txt', '中文说明.md', 'README', 'custom.unknown', 'text.zip'])
def testPathOnlyUtf8FilesDoNotInlineContent(tempDir: Path, fileName: str) -> None:
    canary = '这是普通文本SECRET\nsecond line'
    (tempDir / fileName).write_text(canary, encoding='utf-8')
    result = fileBrowser.buildAttachmentMessage('查看', str(tempDir), [{'path': fileName}])
    assert result == f'查看\n\n{referenceBlock([tempDir / fileName])}'
    assert 'SECRET' not in result
    assert '<attachment' not in result


def testPathOnlyRealArchivesAreReferenced(archivePath: Path) -> None:
    result = fileBrowser.buildAttachmentMessage('看包', str(archivePath.parent), [{'path': archivePath.name, 'type': 'file'}])
    assert result == f'看包\n\n{referenceBlock([archivePath])}'
    assert '压缩包内的原始文本' not in result


@pytest.mark.parametrize('raw', [b'\x00\xffbinary', 'hello中文'.encode('utf-16')], ids=['binary', 'utf16WithNull'])
def testPathOnlyNullContentFilesAreReferenced(tempDir: Path, raw: bytes) -> None:
    (tempDir / 'sample.dat').write_bytes(raw)
    result = fileBrowser.buildAttachmentMessage('', str(tempDir), [{'path': 'sample.dat'}])
    assert result == referenceBlock([tempDir / 'sample.dat'])


@pytest.mark.parametrize('raw', [b'\xff\xfe\x80', '中文'.encode('utf-16-le'), '中文'.encode('gb18030')], ids=['binary', 'utf16WithoutNull', 'gb18030'])
def testPathOnlyNonUtf8IsNotMangledIntoMessage(tempDir: Path, raw: bytes) -> None:
    (tempDir / 'sample.dat').write_bytes(raw)
    preview = fileBrowser.readTextFile(str(tempDir), 'sample.dat')['content']
    result = fileBrowser.buildAttachmentMessage('', str(tempDir), [{'path': 'sample.dat'}])
    assert result == referenceBlock([tempDir / 'sample.dat'])
    assert preview == raw.decode('utf-8', errors='replace')
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError:
        assert preview not in result


def testPathOnlyDirectoryDoesNotScanChildren(tempDir: Path) -> None:
    (tempDir / 'note.txt').write_text('keep me SECRET', encoding='utf-8')
    (tempDir / 'nested').mkdir()
    result = fileBrowser.buildAttachmentMessage('', str(tempDir), [{'path': '.', 'type': 'dir'}])
    assert result == referenceBlock([tempDir])
    assert 'SECRET' not in result
    assert 'keep me' not in result
    assert '跳过' not in result


def testPathOnlyEmptyDirectoryIsAllowed(tempDir: Path) -> None:
    emptyDir = tempDir / 'empty'
    emptyDir.mkdir()
    result = fileBrowser.buildAttachmentMessage('', str(tempDir), [{'path': 'empty', 'type': 'dir'}])
    assert result == referenceBlock([emptyDir])


def testPathOnlyNineDistinctPathsAreAllKept(tempDir: Path) -> None:
    items = []
    paths = []
    for index in range(9):
        name = f'f{index}.txt'
        (tempDir / name).write_text('c', encoding='utf-8')
        items.append({'path': name})
        paths.append(tempDir / name)
    result = fileBrowser.buildAttachmentMessage('', str(tempDir), items)
    assert result == referenceBlock(paths)
    assert result.count('\n- ') == 9


def testPathOnlyLargeFileIsReferencedWithoutReadingBudget(tempDir: Path) -> None:
    large = tempDir / 'large.bin'
    with large.open('wb') as handle:
        handle.truncate(1024 * 1024 + 1)
    result = fileBrowser.buildAttachmentMessage('', str(tempDir), [{'path': 'large.bin'}])
    assert result == referenceBlock([large])


def testPathOnlyBlankFileNameIsAllowed(tempDir: Path) -> None:
    blank = tempDir / ' '
    blank.write_text('secret-blank', encoding='utf-8')
    result = fileBrowser.buildAttachmentMessage('', str(tempDir), [{'path': ' '}])
    assert result == referenceBlock([blank])
    assert 'secret-blank' not in result


def testPathOnlyRejectsEmptyAndNullPath(tempDir: Path) -> None:
    with pytest.raises(RuntimeError, match='附件 path 必须是非空字符串'):
        fileBrowser.buildAttachmentMessage('', str(tempDir), [{'path': ''}])
    with pytest.raises(RuntimeError, match='附件 path 含非法空字符'):
        fileBrowser.buildAttachmentMessage('', str(tempDir), [{'path': 'a\x00b'}])


def testPathOnlyClosingMarkerInFileDoesNotMatter(tempDir: Path) -> None:
    (tempDir / 'source.txt').write_text('This source mentions </attachment> literally.', encoding='utf-8')
    result = fileBrowser.buildAttachmentMessage('看', str(tempDir), [{'path': 'source.txt'}])
    assert '</attachment>' not in result
    assert result == f'看\n\n{referenceBlock([tempDir / "source.txt"])}'


@pytest.mark.parametrize('useLink', [False, True])
def testPathOnlyDirectOutsideReferenceIsRejected(tempDir: Path, useLink: bool) -> None:
    workDir = tempDir / 'work'
    workDir.mkdir()
    outsidePath = tempDir / 'outside.txt'
    outsidePath.write_text('outside canary', encoding='utf-8')
    if useLink:
        (workDir / 'link.txt').symlink_to(outsidePath)
    with pytest.raises(RuntimeError, match='路径越出工作目录'):
        fileBrowser.buildAttachmentMessage('', str(workDir), [{'path': 'link.txt' if useLink else '../outside.txt'}])


def testPathOnlyInsideSymlinkUsesRealTarget(tempDir: Path) -> None:
    workDir = tempDir / 'work'
    workDir.mkdir()
    realFile = workDir / 'real.txt'
    realFile.write_text('inside', encoding='utf-8')
    (workDir / 'alias.txt').symlink_to(realFile)
    result = fileBrowser.buildAttachmentMessage('', str(workDir), [{'path': 'alias.txt'}])
    assert result == referenceBlock([realFile])
    assert 'inside' not in result


def testPathOnlyDoesNotReadOrScanContent(tempDir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tempDir / 'note.txt').write_text('SECRET_CANARY', encoding='utf-8')
    nested = tempDir / 'sub'
    nested.mkdir()
    (nested / 'a.txt').write_text('SECRET2', encoding='utf-8')

    def failRead(*_args, **_kwargs):
        raise AssertionError('must not read content')

    monkeypatch.setattr(Path, 'read_bytes', failRead)
    monkeypatch.setattr(Path, 'read_text', failRead)
    monkeypatch.setattr(Path, 'open', failRead)
    monkeypatch.setattr(os, 'scandir', failRead)
    result = fileBrowser.buildAttachmentMessage('x', str(tempDir), [
        {'path': 'note.txt'},
        {'path': 'sub', 'type': 'dir'},
    ])
    assert 'SECRET' not in result
    assert encodedPath(tempDir / 'note.txt') in result
    assert encodedPath(nested) in result


def testPreviewNullBytesStillRejected(tempDir: Path) -> None:
    (tempDir / 'sample.dat').write_bytes(b'\x00\xffbinary')
    with pytest.raises(RuntimeError, match='二进制文件不支持：sample.dat'):
        fileBrowser.readTextFile(str(tempDir), 'sample.dat')


class FakePump:
    def subscribe(self):
        eventQueue = queue.Queue()
        eventQueue.put(None)
        return eventQueue

    def unsubscribe(self, _eventQueue) -> None:
        return None


def testPathOnlyArchiveHttpReachesAgent(archivePath: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessionId = 'mention-test'
    session = {'sessionId': sessionId, 'workDir': str(archivePath.parent)}
    captured = {}
    monkeypatch.setattr(server.sessionStore, 'getSession', lambda requestedId: session if requestedId == sessionId else None)
    monkeypatch.setattr(server.sessionStore, 'setDefaultTitle', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.sessionStore, 'touchSession', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.historyView, 'loadMessages', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(auth, 'serverToken', 'mention-test-token')

    def fakeGetAgent(_sessionId: str):
        agent = Mock()

        def runUserMessageStream(message, _sid, **_kwargs):
            captured['message'] = message
            captured['images'] = _kwargs.get('images')
            return iter([])

        agent.runUserMessageStream.side_effect = runUserMessageStream
        return agent

    def fakeStartStream(_sessionId, _agent, _stream, meta=None):
        captured['meta'] = meta
        return FakePump()

    monkeypatch.setattr(server.agentManager, 'getAgent', fakeGetAgent)
    monkeypatch.setattr(server.agentManager, 'startStream', fakeStartStream)
    headers = {'Authorization': 'Bearer mention-test-token'}
    with TestClient(server.app) as client:
        listed = client.get(f'/api/sessions/{sessionId}/files', headers=headers)
        assert listed.status_code == 200
        entry = next(item for item in listed.json()['entries'] if item['name'] == archivePath.name)
        assert entry['attachable'] is True
        preview = client.get(f'/api/sessions/{sessionId}/fileContent', params={'path': archivePath.name}, headers=headers)
        sent = client.post('/api/chat/stream', headers=headers, json={
            'sessionId': sessionId, 'message': '查看这个压缩包',
            'attachments': [{'path': archivePath.name, 'type': 'file'}],
        })
    assert preview.status_code == 400
    assert preview.json() == {'error': f'二进制文件不支持：{archivePath.name}'}
    assert sent.status_code == 200
    expected = f'查看这个压缩包\n\n{referenceBlock([archivePath])}'
    assert captured['message'] == expected
    assert captured['meta']['userMessage'] == expected
    assert '压缩包内的原始文本' not in captured['message']


def testPathOnlyInvalidPathDoesNotCreateAgent(tempDir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessionId = 'mention-test'
    session = {'sessionId': sessionId, 'workDir': str(tempDir)}
    monkeypatch.setattr(server.sessionStore, 'getSession', lambda requestedId: session if requestedId == sessionId else None)
    monkeypatch.setattr(auth, 'serverToken', 'mention-test-token')
    getAgent = Mock(side_effect=AssertionError('拒绝附件前不得创建 Agent'))
    startStream = Mock(side_effect=AssertionError('拒绝附件前不得开流'))
    monkeypatch.setattr(server.agentManager, 'getAgent', getAgent)
    monkeypatch.setattr(server.agentManager, 'startStream', startStream)
    headers = {'Authorization': 'Bearer mention-test-token'}
    with TestClient(server.app) as client:
        sent = client.post('/api/chat/stream', headers=headers, json={
            'sessionId': sessionId, 'message': '查看',
            'attachments': [{'path': '../outside.txt', 'type': 'file'}],
        })
    assert sent.status_code == 400
    assert '路径越出工作目录' in sent.json()['error']
    getAgent.assert_not_called()
    startStream.assert_not_called()
