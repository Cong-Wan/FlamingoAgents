'''
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: 图片输入纯库支持层（imageInputPlan §3.2/§3.3）：图片字节校验（Pillow 实检 PNG/JPEG/WebP、单张上限、像素与多帧限制）、
             会话图片目录/命名（img-<12位hex>.<ext>，冲突换名不覆盖）、落盘（全成或回滚）与按 ref 加载 base64。
             仅依赖 Pillow 与标准库，不感知 Web。
'''

from __future__ import annotations

import base64
import binascii
import io
import re
import secrets
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from flamingoAgents.core.types import inputImage

allowedImageFormats = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}
allowedMimeTypes = {mime: ext for ext, mime in allowedImageFormats.items()}
extensionByFormat = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp'}
maxImageBytes = 5 * 1024 * 1024          # 单张原始字节上限
maxPixelsPerImage = 20_000_000           # 单张像素上限
maxImagesPerMessage = 4                  # 单条消息图片张数上限
maxImagesBytesPerMessage = 10 * 1024 * 1024  # 单条消息图片合计上限
sessionImagesBudgetBytes = 20 * 1024 * 1024  # 会话图片目录累计上限
maxChatBodyBytes = 16 * 1024 * 1024      # /api/chat/stream HTTP body 上限
imageRefPattern = re.compile(r'^img-[0-9a-f]{12}\.(png|jpg|webp)$')


class imageInputError(RuntimeError):
    """图片输入确定性拒绝（invalidImageInput / imageBudgetExceeded / imageStorageError / unsupportedImageInput）。"""

    def __init__(self, message: str, errorType: str = 'invalidImageInput'):
        super().__init__(message)
        self.errorType = errorType


def validateImageBytes(raw: bytes, sourceName: str) -> tuple[str, int]:
    """实检图片字节：格式白名单、像素、多帧；返回 (mimeType, 原始字节数)。"""
    if len(raw) > maxImageBytes:
        raise imageInputError(f'图片超过单张大小上限 {maxImageBytes // 1024 // 1024}MiB：{sourceName}', 'imageBudgetExceeded')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            imageFormat = image.format
            width, height = image.size
            frameCount = getattr(image, 'n_frames', 1)
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise imageInputError(f'不是有效的 PNG/JPEG/WebP 图片：{sourceName}（{type(error).__name__}）') from error
    extension = extensionByFormat.get(imageFormat or '')
    if extension is None:
        raise imageInputError(f'不支持的图片格式（仅 PNG/JPEG/WebP）：{sourceName}')
    if frameCount > 1:
        raise imageInputError(f'不支持多帧/动图：{sourceName}')
    if width * height > maxPixelsPerImage:
        raise imageInputError(f'图片像素超过上限 {maxPixelsPerImage}：{sourceName}', 'imageBudgetExceeded')
    return allowedImageFormats[extension], len(raw)


def decodeImageBase64(data: str, sourceName: str) -> tuple[bytes, str, int]:
    """解码 base64（无 data URL 前缀）并实检；返回 (原始字节, mimeType, 字节数)。"""
    if not isinstance(data, str) or not data:
        raise imageInputError(f'图片数据为空：{sourceName}')
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise imageInputError(f'图片 base64 解码失败：{sourceName}') from error
    mimeType, size = validateImageBytes(raw, sourceName)
    return raw, mimeType, size


def checkMessageImageBudget(images: list[inputImage]) -> None:
    if len(images) > maxImagesPerMessage:
        raise imageInputError(f'单条消息图片超过 {maxImagesPerMessage} 张上限', 'imageBudgetExceeded')
    total = 0
    for image in images:
        total += image.bytes or len(image.data or b'')
    if total > maxImagesBytesPerMessage:
        raise imageInputError(
            f'单条消息图片合计超过 {maxImagesBytesPerMessage // 1024 // 1024}MiB 上限',
            'imageBudgetExceeded',
        )


def sessionImagesDir(logPath: Path) -> Path:
    """会话图片目录：<logDir>/{sessionId}.images/（logPath 为 {sessionId}.jsonl）。"""
    return logPath.parent / f'{logPath.stem}.images'


def _newImageRef(mimeType: str) -> str:
    extension = allowedMimeTypes[mimeType]
    return f'img-{secrets.token_hex(6)}.{extension}'


def storeImages(logPath: Path, images: list[inputImage]) -> list[inputImage]:
    """锁内统一落盘（imageInputPlan §3.3 落盘事务性）：
    ① 预算复检（目录实文件 size 求和 + 本轮原始字节 ≤ 会话预算，超限不落盘）；
    ② 逐张写入（ref 冲突换名、绝不覆盖既有文件）；
    ③ 全部成功才返回（调用方随后写 userMessage 事件）；任一步失败只回滚本轮新文件。
    输入 images 携带 data；返回的副本携带 ref，data 保留供当轮请求使用。
    """
    if not images:
        return []
    targetDir = sessionImagesDir(logPath)
    incomingBytes = sum(len(image.data or b'') for image in images)
    storedBytes = imagesDirSize(targetDir)
    if storedBytes + incomingBytes > sessionImagesBudgetBytes:
        raise imageInputError(
            f'会话图片累计超过 {sessionImagesBudgetBytes // 1024 // 1024}MiB 上限，请新建会话',
            'imageBudgetExceeded',
        )
    targetDir.mkdir(parents=True, exist_ok=True)
    stored: list[inputImage] = []
    newFiles: list[Path] = []
    try:
        for image in images:
            ref = _newImageRef(image.mimeType)
            while (targetDir / ref).exists():
                ref = _newImageRef(image.mimeType)
            (targetDir / ref).write_bytes(image.data)
            newFiles.append(targetDir / ref)
            stored.append(inputImage(
                name=image.name, mimeType=image.mimeType, data=image.data, ref=ref, bytes=image.bytes,
            ))
    except OSError as error:
        for filePath in newFiles:
            try:
                filePath.unlink(missing_ok=True)
            except OSError:
                pass
        raise imageInputError(f'图片保存失败：{error}', 'imageStorageError') from error
    except Exception:
        for filePath in newFiles:
            try:
                filePath.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return stored


def imagesDirSize(targetDir: Path) -> int:
    """目录实际文件 size 求和（含孤儿）；目录不存在返回 0；单条目失败跳过（以能读到的为准）。"""
    if not targetDir.exists():
        return 0
    total = 0
    try:
        for entry in targetDir.iterdir():
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def loadImageByRef(logPath: Path, ref: str) -> tuple[bytes, str]:
    """按 ref 加载会话图片目录中的文件；ref 必须匹配命名模式且 resolve 后仍在该目录内。"""
    if not isinstance(ref, str) or not imageRefPattern.fullmatch(ref):
        raise imageInputError(f'图片引用非法：{ref}', 'imageStorageError')
    targetDir = sessionImagesDir(logPath)
    filePath = (targetDir / ref)
    try:
        resolved = filePath.resolve()
        if not resolved.is_relative_to(targetDir.resolve()):
            raise imageInputError(f'图片引用越出会话图片目录：{ref}', 'imageStorageError')
        raw = resolved.read_bytes()
    except OSError as error:
        raise imageInputError(f'图片文件缺失或不可读：{ref}', 'imageStorageError') from error
    extension = resolved.suffix[1:]
    if extension not in allowedImageFormats:
        raise imageInputError(f'图片引用非法：{ref}', 'imageStorageError')
    return raw, allowedImageFormats[extension]


def imageDataUrl(image: inputImage) -> str:
    """构造 data URL（请求体用）；data 必须已就绪。"""
    if not image.data:
        raise imageInputError(f'图片数据未加载：{image.name}', 'imageStorageError')
    return f'data:{image.mimeType};base64,{base64.b64encode(image.data).decode("ascii")}'


def hydrateMessageImages(logPath: Path, messages: list) -> None:
    """按 ref 填充历史 user 消息的 data；缺文件明确报错，不发给模型半份数据。"""
    for message in messages:
        if getattr(message, 'role', None) != 'user':
            continue
        for image in getattr(message, 'images', None) or []:
            if image.data:
                continue
            raw, mimeType = loadImageByRef(logPath, image.ref)
            image.data = raw
            if not image.mimeType:
                image.mimeType = mimeType


def imageRefMeta(image: inputImage) -> dict[str, Any]:
    return {
        'name': image.name,
        'mimeType': image.mimeType,
        'ref': image.ref,
        'bytes': image.bytes,
    }


def redactImageData(value: Any) -> Any:
    """复制结构并把 data URL 的 base64 换成长度占位，不修改入参。"""
    if isinstance(value, str) and value.startswith('data:') and ';base64,' in value:
        header, _, payload = value.partition(';base64,')
        return f'{header};base64,<redacted len={len(payload)}>'
    if isinstance(value, dict):
        return {key: redactImageData(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redactImageData(item) for item in value]
    return value


def requireImageCapability(config, messages, extraImages=None) -> None:
    hasImages = bool(extraImages) or any(
        getattr(message, 'role', None) == 'user' and getattr(message, 'images', None)
        for message in (messages or [])
    )
    if not hasImages:
        return
    if config is None or not getattr(config, 'supportsImageInput', False):
        raise imageInputError('当前模型不支持图片输入。', 'unsupportedImageInput')


def restoreImagesFromEvent(rawImages: Any) -> list[inputImage]:
    """从 JSONL userMessage 事件恢复 images 引用（不加载 data）；损坏数据明确报错，不静默丢图。"""
    if rawImages is None:
        return []
    if not isinstance(rawImages, list):
        raise imageInputError('userMessage.images 损坏：必须是数组')
    restored: list[inputImage] = []
    for index, item in enumerate(rawImages):
        if not isinstance(item, dict):
            raise imageInputError(f'userMessage.images[{index}] 损坏：必须是对象')
        name = item.get('name')
        mimeType = item.get('mimeType')
        ref = item.get('ref')
        size = item.get('bytes')
        if not isinstance(name, str) or not name:
            raise imageInputError(f'userMessage.images[{index}] 损坏：name 非法')
        if mimeType not in allowedMimeTypes:
            raise imageInputError(f'userMessage.images[{index}] 损坏：mimeType 非法')
        if not isinstance(ref, str) or not imageRefPattern.fullmatch(ref):
            raise imageInputError(f'userMessage.images[{index}] 损坏：ref 非法')
        if not isinstance(size, int) or size < 0:
            raise imageInputError(f'userMessage.images[{index}] 损坏：bytes 非法')
        restored.append(inputImage(name=name, mimeType=mimeType, data=b'', ref=ref, bytes=size))
    return restored
