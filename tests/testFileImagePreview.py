'''
Author: wilbur
Version: 1.0
Date: 2026-10-13
Description: 工作区图片预览测试：校验 PNG/JPEG/WebP、后缀与内容一致、路径拘禁、超限 413 和 fileImage HTTP 响应。
'''

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from flamingoAgents.core.imageInput import maxImageBytes
from webApp.backend import auth, fileBrowser, server


def imageBytes(imageFormat: str, frames: int = 1, size=(8, 8)) -> bytes:
    buffer = io.BytesIO()
    if frames == 1:
        Image.new('RGB', size, (20, 40, 60)).save(buffer, format=imageFormat)
    else:
        images = [Image.new('RGB', (16, 16), (index * 40, 40, 60)) for index in range(frames)]
        images[0].save(buffer, format='WEBP', save_all=True, append_images=images[1:], duration=100, loop=0)
    return buffer.getvalue()


def testReadImageFileAcceptsSupportedFormats(tmp_path: Path) -> None:
    samples = {
        'shot.png': ('PNG', 'image/png'),
        'shot.JPG': ('JPEG', 'image/jpeg'),
        'shot.jpeg': ('JPEG', 'image/jpeg'),
        'shot.webp': ('WEBP', 'image/webp'),
    }
    for name, (imageFormat, mimeType) in samples.items():
        raw = imageBytes(imageFormat)
        (tmp_path / name).write_bytes(raw)
        loaded, loadedMime = fileBrowser.readImageFile(str(tmp_path), name)
        assert loaded == raw
        assert loadedMime == mimeType


def testReadImageFileRejectsMismatchedAndInvalidContent(tmp_path: Path) -> None:
    (tmp_path / 'wrong.png').write_bytes(imageBytes('JPEG'))
    (tmp_path / 'broken.png').write_bytes(b'not-an-image')
    (tmp_path / 'note.txt').write_text('hello', encoding='utf-8')
    (tmp_path / 'anim.webp').write_bytes(imageBytes('WEBP', frames=2))
    with pytest.raises(RuntimeError, match='后缀不一致'):
        fileBrowser.readImageFile(str(tmp_path), 'wrong.png')
    with pytest.raises(RuntimeError, match='不是有效的'):
        fileBrowser.readImageFile(str(tmp_path), 'broken.png')
    with pytest.raises(RuntimeError, match='不是支持的图片文件'):
        fileBrowser.readImageFile(str(tmp_path), 'note.txt')
    with pytest.raises(RuntimeError, match='不支持多帧'):
        fileBrowser.readImageFile(str(tmp_path), 'anim.webp')


def testReadImageFileRejectsOversizedBeforeReading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / 'big.png'
    target.write_bytes(imageBytes('PNG'))

    def failRead(self):
        raise AssertionError('超限图片不得读取正文')

    monkeypatch.setattr(Path, 'read_bytes', failRead)
    monkeypatch.setattr(Path, 'stat', lambda self: type('stat', (), {'st_size': maxImageBytes + 1})())
    with pytest.raises(fileBrowser.imageInputError, match='超过单张大小上限'):
        fileBrowser.readImageFile(str(tmp_path), 'big.png')


def testReadImageFileRejectsGifDirectoryAndMissing(tmp_path: Path) -> None:
    (tmp_path / 'anim.gif').write_bytes(b'GIF89a')
    (tmp_path / 'folder.png').mkdir()
    with pytest.raises(RuntimeError, match='不是支持的图片文件'):
        fileBrowser.readImageFile(str(tmp_path), 'anim.gif')
    with pytest.raises(RuntimeError, match='不是文件'):
        fileBrowser.readImageFile(str(tmp_path), 'folder.png')
    with pytest.raises(RuntimeError, match='不是文件'):
        fileBrowser.readImageFile(str(tmp_path), 'missing.png')


def testReadImageFileRejectsTraversalAndSymlink(tmp_path: Path) -> None:
    workDir = tmp_path / 'work'
    workDir.mkdir()
    outside = tmp_path / 'outside.png'
    outside.write_bytes(imageBytes('PNG'))
    (workDir / 'link.png').symlink_to(outside)
    with pytest.raises(RuntimeError, match='路径越出工作目录'):
        fileBrowser.readImageFile(str(workDir), '../outside.png')
    with pytest.raises(RuntimeError, match='路径越出工作目录'):
        fileBrowser.readImageFile(str(workDir), 'link.png')


def patchImageSession(monkeypatch: pytest.MonkeyPatch, sessionId: str, workDir: Path) -> dict[str, str]:
    session = {'sessionId': sessionId, 'workDir': str(workDir)}
    monkeypatch.setattr(server.sessionStore, 'getSession', lambda requestedId: session if requestedId == sessionId else None)
    monkeypatch.setattr(auth, 'serverToken', 'image-preview-token')
    return {'Authorization': 'Bearer image-preview-token'}


def testFileImageHttpReturnsBytesAndHeaders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = imageBytes('PNG')
    (tmp_path / 'shot.png').write_bytes(raw)
    headers = patchImageSession(monkeypatch, 'img-preview', tmp_path)
    with TestClient(server.app) as client:
        response = client.get('/api/sessions/img-preview/fileImage', params={'path': 'shot.png'}, headers=headers)
    assert response.status_code == 200
    assert response.content == raw
    assert response.headers['content-type'].startswith('image/png')
    assert response.headers['cache-control'] == 'no-store'


def testFileImageHttpRejectsTraversalAndTooManyPixels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workDir = tmp_path / 'work'
    workDir.mkdir()
    outside = tmp_path / 'outside.png'
    outside.write_bytes(imageBytes('PNG'))
    (workDir / 'wide.png').write_bytes(imageBytes('PNG', size=(5001, 5001)))
    headers = patchImageSession(monkeypatch, 'img-preview', workDir)
    with TestClient(server.app) as client:
        escaped = client.get('/api/sessions/img-preview/fileImage', params={'path': '../outside.png'}, headers=headers)
        tooWide = client.get('/api/sessions/img-preview/fileImage', params={'path': 'wide.png'}, headers=headers)
    assert escaped.status_code == 400
    assert outside.read_bytes() not in escaped.content
    assert tooWide.status_code == 413


def testFileImageHttpMapsErrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'big.png').write_bytes(b'x' * (maxImageBytes + 1))
    headers = patchImageSession(monkeypatch, 'img-preview', tmp_path)
    with TestClient(server.app) as client:
        missingPath = client.get('/api/sessions/img-preview/fileImage', headers=headers)
        missingSession = client.get('/api/sessions/missing/fileImage', params={'path': 'big.png'}, headers=headers)
        unauthorized = client.get('/api/sessions/img-preview/fileImage', params={'path': 'big.png'})
        oversized = client.get('/api/sessions/img-preview/fileImage', params={'path': 'big.png'}, headers=headers)
    assert missingPath.status_code == 400
    assert missingSession.status_code == 404
    assert unauthorized.status_code == 401
    assert oversized.status_code == 413
    assert b'x' * 32 not in oversized.content
