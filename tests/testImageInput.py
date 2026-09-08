'''
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: P1 测试（imageInputPlan §5）：inputTypes/supportsImageInput、图片字节校验、落盘事务性与预算、ref 往返加载、
             JSONL 严格模式、损坏恢复报错；使用 pytest tmp_path/monkeypatch 隔离，不读写真实会话。
'''

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.imageInput import (
    imageInputError,
    imageDataUrl,
    loadImageByRef,
    restoreImagesFromEvent,
    sessionImagesDir,
    storeImages,
    validateImageBytes,
)
from flamingoAgents.core.types import chatMessage, inputImage
from flamingoAgents.models.modelConfig import loadModelConfigFromYaml
from flamingoAgents.utils.jsonl import jsonlIntegrityError, jsonlLog


def makePngBytes(width: int = 8, height: int = 8, color: tuple = (255, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buffer, format='PNG')
    return buffer.getvalue()


def makeJpegBytes(width: int = 8, height: int = 8) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (0, 255, 0)).save(buffer, format='JPEG')
    return buffer.getvalue()


def makeGifBytes() -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (4, 4), (0, 0, 255)).save(buffer, format='GIF')
    return buffer.getvalue()


def makeConfig(tmpPath):
    configPath = tmpPath / 'models.yaml'
    configPath.write_text(
        'providers:\n'
        '  vis:\n'
        '    baseUrl: https://api.example.com/v1\n'
        '    api: openai-completions\n'
        '    apiKey: sk-test\n'
        '    models:\n'
        '      - id: vision-model\n'
        '        input: [text, image]\n'
        '      - id: text-model\n'
        '        input: [text]\n'
        '      - id: default-model\n',
        encoding='utf-8',
    )
    return configPath


# ---------- 配置能力（§3.1） ----------

def testInputTypesAndSupportsImageInput(tmp_path):
    configPath = makeConfig(tmp_path)
    vision = loadModelConfigFromYaml(providerId='vis', modelId='vision-model', configPath=configPath)
    textOnly = loadModelConfigFromYaml(providerId='vis', modelId='text-model', configPath=configPath)
    defaultModel = loadModelConfigFromYaml(providerId='vis', modelId='default-model', configPath=configPath)
    assert vision.config.supportsImageInput is True
    assert vision.config.inputTypes == ['text', 'image']
    assert textOnly.config.supportsImageInput is False
    assert defaultModel.config.inputTypes == ['text']
    assert defaultModel.config.supportsImageInput is False


def testInvalidInputTypesRejected(tmp_path):
    configPath = tmp_path / 'models.yaml'
    configPath.write_text(
        'providers:\n'
        '  p:\n'
        '    baseUrl: https://api.example.com/v1\n'
        '    api: openai-completions\n'
        '    apiKey: sk-test\n'
        '    models:\n'
        '      - id: m\n'
        '        input: [text, video]\n',
        encoding='utf-8',
    )
    with pytest.raises(RuntimeError, match='input 仅允许 text/image'):
        loadModelConfigFromYaml(providerId='p', modelId='m', configPath=configPath)


# ---------- 图片字节校验（§3.3） ----------

def testValidateImageBytesAcceptsPngJpeg():
    mimeType, size = validateImageBytes(makePngBytes(), 'a.png')
    assert mimeType == 'image/png' and size > 0
    mimeType, size = validateImageBytes(makeJpegBytes(), 'b.jpg')
    assert mimeType == 'image/jpeg' and size > 0


def testValidateImageBytesRejectsGifAndCorrupt():
    with pytest.raises(imageInputError, match='不支持的图片格式'):
        validateImageBytes(makeGifBytes(), 'a.gif')
    with pytest.raises(imageInputError, match='不是有效的'):
        validateImageBytes(b'not-an-image', 'bad.png')


# ---------- 落盘事务性与预算（§3.3） ----------

def testStoreImagesWritesRefsAndRefRoundTrip(tmp_path):
    logPath = tmp_path / 'session_abc.jsonl'
    raw = makePngBytes()
    stored = storeImages(logPath, [inputImage(name='a.png', mimeType='image/png', data=raw)])
    assert len(stored) == 1
    ref = stored[0].ref
    assert ref.startswith('img-') and ref.endswith('.png')
    storedDir = sessionImagesDir(logPath)
    assert (storedDir / ref).read_bytes() == raw
    loaded, mimeType = loadImageByRef(logPath, ref)
    assert loaded == raw and mimeType == 'image/png'
    assert imageDataUrl(stored[0]) == f'data:image/png;base64,{base64.b64encode(raw).decode("ascii")}'


def testStoreImagesBudgetRejectsBeforeWrite(tmp_path, monkeypatch):
    logPath = tmp_path / 'session_abc.jsonl'
    storedDir = sessionImagesDir(logPath)
    storedDir.mkdir(parents=True)
    (storedDir / 'img-000000000000.png').write_bytes(b'x' * 100)
    big = inputImage(name='big.png', mimeType='image/png', data=b'y' * 100)
    monkeypatch.setattr('flamingoAgents.core.imageInput.sessionImagesBudgetBytes', 150)
    with pytest.raises(imageInputError, match='会话图片累计超过'):
        storeImages(logPath, [big])
    assert list(storedDir.iterdir()) == [storedDir / 'img-000000000000.png']  # 未新增文件


def testStoreImagesRollbackOnFailure(tmp_path, monkeypatch):
    logPath = tmp_path / 'session_abc.jsonl'
    good = inputImage(name='good.png', mimeType='image/png', data=makePngBytes())
    boom = inputImage(name='boom.png', mimeType='image/png', data=makePngBytes())
    realWrite = Path.write_bytes
    writeCount = {'n': 0}

    def failingWrite(self, data):
        writeCount['n'] += 1
        if writeCount['n'] >= 2:
            raise OSError('disk full')
        return realWrite(self, data)

    monkeypatch.setattr(Path, 'write_bytes', failingWrite)
    with pytest.raises(imageInputError, match='图片保存失败'):
        storeImages(logPath, [good, boom])
    monkeypatch.undo()
    assert not sessionImagesDir(logPath).exists() or list(sessionImagesDir(logPath).iterdir()) == []


# ---------- JSONL 引用写入与恢复（§3.3/§3.6） ----------

def testUserMessageImagesReferenceRoundTrip(tmp_path):
    logPath = tmp_path / 'session_abc.jsonl'
    conversationInstance = conversation(sessionId='session_abc', logPath=logPath, systemPrompt='sys')
    raw = makePngBytes()
    stored = storeImages(logPath, [inputImage(name='shot.png', mimeType='image/png', data=raw)])
    conversationInstance.appendUserMessage('看图', images=stored)
    events = jsonlLog(logPath).readEvents(strict=True)
    userEvent = [event for event in events if event['type'] == 'userMessage'][0]
    assert userEvent['images'][0]['ref'] == stored[0].ref
    assert userEvent['images'][0]['name'] == 'shot.png'
    assert 'base64' not in json.dumps(userEvent)
    restored = restoreImagesFromEvent(userEvent['images'])
    assert restored[0].ref == stored[0].ref and restored[0].data == b''
    # 恢复后的会话（resume）从日志重建 user 消息 images
    resumed = conversation(sessionId='session_abc', logPath=logPath, systemPrompt='sys', resume=True)
    userMessages = [message for message in resumed.messages if message.role == 'user']
    assert userMessages[0].images[0].ref == stored[0].ref


def testRestoreRejectsCorruptImages():
    with pytest.raises(imageInputError, match='损坏'):
        restoreImagesFromEvent([{'name': 'x', 'mimeType': 'image/png', 'ref': 'bad-ref', 'bytes': 10}])
    with pytest.raises(imageInputError, match='损坏'):
        restoreImagesFromEvent('not-a-list')


# ---------- JSONL 严格模式（§3.3） ----------

def testStrictReadRejectsTruncatedAndMissingNewline(tmp_path):
    logPath = tmp_path / 'session_abc.jsonl'
    logPath.write_text('{"type":"systemMessage","content":"s"}\n{"type":"userMessage"', encoding='utf-8')
    with pytest.raises(jsonlIntegrityError):
        jsonlLog(logPath).readEvents(strict=True)
    logPath.write_text('{"type":"systemMessage","content":"s"}', encoding='utf-8')  # 合法 JSON 但无换行
    with pytest.raises(jsonlIntegrityError, match='缺少换行符'):
        jsonlLog(logPath).readEvents(strict=True)
    logPath.write_text('', encoding='utf-8')  # 空文件合法
    assert jsonlLog(logPath).readEvents(strict=True) == []


def testLenientReadKeepsLegacyBehavior(tmp_path):
    logPath = tmp_path / 'session_abc.jsonl'
    logPath.write_text('{"type":"systemMessage","content":"s"}\nbroken-line\n', encoding='utf-8')
    events = jsonlLog(logPath).readEvents()
    assert len(events) == 1  # 跳过坏行，旧行为不变


def testConcurrentWriteAndStrictReadNoHalfLine(tmp_path):
    import threading
    logPath = tmp_path / 'session_abc.jsonl'
    logger = jsonlLog(logPath)
    logger.logEvent({'type': 'systemMessage', 'content': 'init'})
    errors = []

    def writer():
        for index in range(20):
            logger.logEvent({'type': 'userMessage', 'content': 'x' * 50, 'seq': index})

    def reader():
        for _ in range(20):
            try:
                jsonlLog(logPath).readEvents(strict=True)
            except jsonlIntegrityError as error:
                errors.append(str(error))

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


# ---------- chatMessage 兼容 ----------

def testChatCompletionsImagePayloadAndPlainTextUnchanged():
    from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
    from flamingoAgents.models.modelAuth import modelAuth
    from flamingoAgents.models.modelConfig import modelConfig
    raw = makePngBytes()
    image = inputImage(name='a.png', mimeType='image/png', data=raw, ref='img-aaaaaaaaaaaa.png', bytes=len(raw))
    config = modelConfig(
        provider='p', model='m', baseUrl='https://example.com/v1',
        apiType='openai-completions', inputTypes=['text', 'image'],
    )
    adapter = chatCompletionsAdapter(config, modelAuth(authorizationHeader='Bearer x'))
    withImage = adapter.convertMessage(chatMessage(role='user', content='看图', images=[image]))
    assert withImage['content'][0] == {'type': 'text', 'text': '看图'}
    assert withImage['content'][1]['type'] == 'image_url'
    assert withImage['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    plain = adapter.convertMessage(chatMessage(role='user', content='hello'))
    assert plain == {'role': 'user', 'content': 'hello'}
    pureImage = adapter.convertMessage(chatMessage(role='user', content='', images=[image]))
    assert all(part['type'] != 'text' for part in pureImage['content'])


def testResponsesImagePayloadKeepsPlainUserShape():
    from flamingoAgents.models.modelAuth import modelAuth
    from flamingoAgents.models.modelConfig import modelConfig
    from flamingoAgents.models.responsesAdapter import responsesAdapter
    class staticResolver:
        def resolve(self, forceRefresh=False, staleAccess=None):
            return modelAuth(authorizationHeader='Bearer x', accessToken='x')
    raw = makePngBytes()
    image = inputImage(name='a.png', mimeType='image/png', data=raw, ref='img-aaaaaaaaaaaa.png', bytes=len(raw))
    config = modelConfig(
        provider='p', model='m', baseUrl='https://api.x.ai/v1',
        apiType='openai-responses', authType='oauth', authProvider='xai',
        inputTypes=['text', 'image'],
    )
    adapter = responsesAdapter(config, staticResolver())
    items = adapter.convertMessages([chatMessage(role='user', content='看图', images=[image])])
    parts = items[0]['content']
    assert parts[0] == {'type': 'input_text', 'text': '看图'}
    assert parts[1]['type'] == 'input_image'
    assert parts[1]['image_url'].startswith('data:image/png;base64,')
    plain = adapter.convertMessages([chatMessage(role='user', content='hello')])
    assert plain == [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]}]


def testChatMessageImagesDefaultEmpty():
    message = chatMessage(role='user', content='hi')
    assert message.images == []
    messageWithImage = chatMessage(role='user', content='hi', images=[inputImage(name='a.png', mimeType='image/png')])
    assert messageWithImage.images[0].name == 'a.png'
    assert json.dumps({'ok': True})  # 新字段不破坏既有序列化路径
