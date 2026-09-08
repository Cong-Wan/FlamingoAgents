'''
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: Web 图片输入测试（imageInputPlan P2）：上传落盘、@ 图片快照、纯文本模型拒绝上传图、@ 图回退路径引用、
             图片端点拘禁、deleteSession 清理目录、有界请求体。使用伪会话/伪 token，不碰真实数据。
'''

from __future__ import annotations

import base64
import io
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from flamingoAgents.core.imageInput import sessionImagesDir
from flamingoAgents.core.types import inputImage
from webApp.backend import auth, server


class FakePump:
    def __init__(self):
        self.meta = {}
        self.stopFlag = type('flag', (), {'is_set': lambda self: False})()

    def subscribe(self):
        import queue
        q = queue.Queue()
        q.put(None)
        return q

    def unsubscribe(self, _subscriber):
        return None


def makePngBytes() -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (255, 0, 0)).save(buffer, format='PNG')
    return buffer.getvalue()


def pngDataUrlPayload(name: str = 'shot.png') -> dict:
    raw = makePngBytes()
    return {'name': name, 'mimeType': 'image/png', 'data': base64.b64encode(raw).decode('ascii')}, raw


def patchSession(monkeypatch, sessionId: str, workDir: str, supportsImage: bool = True):
    session = {'sessionId': sessionId, 'workDir': workDir}
    captured = {}
    monkeypatch.setattr(server.sessionStore, 'getSession', lambda requestedId: session if requestedId == sessionId else None)
    monkeypatch.setattr(server.sessionStore, 'setDefaultTitle', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.sessionStore, 'touchSession', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.historyView, 'loadMessages', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(auth, 'serverToken', 'image-test-token')

    config = Mock()
    config.supportsImageInput = supportsImage
    agent = Mock()
    agent.modelAdapter.config = config

    def runUserMessageStream(message, _sid, **kwargs):
        captured['message'] = message
        captured['images'] = kwargs.get('images')
        sink = kwargs.get('committedImages')
        if sink is not None and kwargs.get('images'):
            sink.clear()
            for image in kwargs['images']:
                sink.append({'name': image.name, 'mimeType': image.mimeType, 'ref': image.ref or 'img-aaaaaaaaaaaa.png', 'bytes': image.bytes})
        return iter([])

    agent.runUserMessageStream.side_effect = runUserMessageStream

    def fakeStartStream(_sessionId, _agent, _stream, meta=None):
        captured['meta'] = meta
        pump = FakePump()
        pump.meta = meta or {}
        return pump

    monkeypatch.setattr(server.agentManager, 'getAgent', lambda _sid: agent)
    monkeypatch.setattr(server.agentManager, 'startStream', fakeStartStream)
    return captured, {'Authorization': 'Bearer image-test-token'}


def testUploadImageAcceptedWhenModelSupports(tmp_path, monkeypatch):
    captured, headers = patchSession(monkeypatch, 'img-sess', str(tmp_path), True)
    payload, raw = pngDataUrlPayload()
    with TestClient(server.app) as client:
        response = client.post('/api/chat/stream', headers=headers, json={
            'sessionId': 'img-sess', 'message': '解释这张图', 'images': [payload],
        })
    assert response.status_code == 200
    assert captured['message'] == '解释这张图'
    images = captured['images']
    assert len(images) == 1
    assert images[0].name == 'shot.png'
    assert images[0].data == raw
    assert captured['meta']['userImages'][0]['name'] == 'shot.png'


def testUploadImageRejectedWhenModelLacksImage(tmp_path, monkeypatch):
    captured, headers = patchSession(monkeypatch, 'img-sess', str(tmp_path), False)
    payload, _raw = pngDataUrlPayload()
    getAgent = server.agentManager.getAgent
    with TestClient(server.app) as client:
        response = client.post('/api/chat/stream', headers=headers, json={
            'sessionId': 'img-sess', 'message': '看图', 'images': [payload],
        })
    assert response.status_code == 400
    assert '不支持图片' in response.json()['error']
    assert 'images' not in captured


def testMentionImageSnapshotWhenSupported(tmp_path, monkeypatch):
    pngPath = tmp_path / 'photo.png'
    pngPath.write_bytes(makePngBytes())
    captured, headers = patchSession(monkeypatch, 'img-sess', str(tmp_path), True)
    with TestClient(server.app) as client:
        response = client.post('/api/chat/stream', headers=headers, json={
            'sessionId': 'img-sess', 'message': '看这个',
            'attachments': [{'path': 'photo.png', 'type': 'file'}],
        })
    assert response.status_code == 200
    assert '引用路径' in captured['message']
    assert 'photo.png' in captured['message']
    assert len(captured['images']) == 1
    assert captured['images'][0].name == 'photo.png'


def testMentionImageFallsBackWhenUnsupported(tmp_path, monkeypatch):
    pngPath = tmp_path / 'photo.png'
    pngPath.write_bytes(makePngBytes())
    captured, headers = patchSession(monkeypatch, 'img-sess', str(tmp_path), False)
    with TestClient(server.app) as client:
        response = client.post('/api/chat/stream', headers=headers, json={
            'sessionId': 'img-sess', 'message': '看这个',
            'attachments': [{'path': 'photo.png', 'type': 'file'}],
        })
    assert response.status_code == 200
    assert captured['images'] in (None, [])
    assert '引用路径' in captured['message']


def testImagesFieldMustBeArray(tmp_path, monkeypatch):
    _captured, headers = patchSession(monkeypatch, 'img-sess', str(tmp_path), True)
    with TestClient(server.app) as client:
        response = client.post('/api/chat/stream', headers=headers, json={
            'sessionId': 'img-sess', 'message': 'hi', 'images': None,
        })
    assert response.status_code == 400


def testImageEndpointRejectsTraversal(tmp_path, monkeypatch):
    sessionId = 'img-sess'
    monkeypatch.setattr(server.sessionStore, 'getSession', lambda requestedId: {'sessionId': sessionId, 'workDir': str(tmp_path)} if requestedId == sessionId else None)
    monkeypatch.setattr(auth, 'serverToken', 'image-test-token')
    headers = {'Authorization': 'Bearer image-test-token'}
    with TestClient(server.app) as client:
        response = client.get(f'/api/sessions/{sessionId}/images/../secret.png', headers=headers)
    assert response.status_code in (400, 404)


def testDeleteSessionRemovesImagesDir(tmp_path, monkeypatch):
    sessionId = 'img-sess'
    workDir = tmp_path / 'work'
    workDir.mkdir()
    logDir = tmp_path / 'logs'
    logDir.mkdir()
    logPath = logDir / f'{sessionId}.jsonl'
    logPath.write_text('{}\n', encoding='utf-8')
    imagesDir = sessionImagesDir(logPath)
    imagesDir.mkdir()
    (imagesDir / 'img-aaaaaaaaaaaa.png').write_bytes(b'x')
    monkeypatch.setattr(server.sessionStore, 'getSession', lambda requestedId: {
        'sessionId': sessionId, 'workDir': str(workDir),
    } if requestedId == sessionId else None)
    monkeypatch.setattr(server.sessionStore, 'deleteSession', lambda _sid: None)
    monkeypatch.setattr(server.agentManager, 'hasActiveStream', lambda _sid: False)
    monkeypatch.setattr(server.agentManager, 'dropAgent', lambda _sid: None)
    monkeypatch.setattr(server, 'resolveSessionLogDir', lambda category, path: logDir)
    monkeypatch.setattr(auth, 'serverToken', 'image-test-token')
    with TestClient(server.app) as client:
        response = client.delete(f'/api/sessions/{sessionId}', headers={'Authorization': 'Bearer image-test-token'})
    assert response.status_code == 200
    assert not imagesDir.exists()
