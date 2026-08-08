'''
Author: wilbur
Version: 1.1
Date: 2026-08-07
Description: workDir 文件浏览纯函数层（迭代二方案 §3.8）：is_relative_to 路径拘禁、目录列举（目录在前、单条目 stat 失败跳过、
            截断标记）、文本文件读取（二进制校验）；OSError 统一转 RuntimeError 中文消息走 400 透传，不落 fallback 500。
            v1.1 取消 maxFileBytes 单文件大小限制，readTextFile 不再校验文件大小，listDir 中 attachable 始终为 true。
'''

from __future__ import annotations

import os
from pathlib import Path

maxTotalBytes = 1024 * 1024    # 附件合计上限
maxAttachments = 8             # 单次附件个数上限
maxEntries = 500               # 目录单层列举上限
attachmentCloseTag = '</attachment>'


def resolveInside(workDir: str, relPath: str | None) -> Path:
    # 路径拘禁：resolve 后 is_relative_to 校验（杜绝 startswith 的 /work vs /workEvil 陷阱），符号链接逃逸一并拦截。
    base = Path(workDir).resolve()
    target = (base / (relPath or '')).resolve()
    if not target.is_relative_to(base):
        raise RuntimeError(f'路径越出工作目录：{relPath}')
    return target


def readTextFile(workDir: str, relPath: str) -> dict:
    target = resolveInside(workDir, relPath)
    try:
        if not target.is_file():
            raise RuntimeError(f'不是文件：{relPath}')
        size = target.stat().st_size
        raw = target.read_bytes()
    except OSError as error:
        raise RuntimeError(f'文件不存在或不可读：{relPath}（{error}）')
    if b'\x00' in raw:
        raise RuntimeError(f'二进制文件不支持：{relPath}')
    return {'path': relPath, 'size': size, 'content': raw.decode('utf-8', errors='replace')}


def listDir(workDir: str, relPath: str | None) -> dict:
    # 不屏蔽任何文件（v1.2 用户明示）：dotfiles 与 .git 全部照常返回。
    target = resolveInside(workDir, relPath)
    try:
        if not target.is_dir():
            raise RuntimeError(f'不是目录：{relPath or "/"}')
        entries = []
        truncated = False
        with os.scandir(target) as iterator:
            for item in iterator:
                try:
                    isDir = item.is_dir(follow_symlinks=True)
                    size = 0 if isDir else item.stat(follow_symlinks=True).st_size
                except OSError:
                    continue  # 坏符号链接/竞态删除：跳过该条目，不拖垮整层
                entry = {'name': item.name, 'type': 'dir' if isDir else 'file'}
                if not isDir:
                    entry['size'] = size
                    entry['attachable'] = True
                entries.append(entry)
                if len(entries) >= maxEntries:
                    truncated = True
                    break
    except OSError as error:
        raise RuntimeError(f'目录不存在或不可读：{relPath or "/"}（{error}）')
    entries.sort(key=lambda entry: (entry['type'] != 'dir', entry['name'].lower()))
    return {'path': relPath or '', 'entries': entries, 'truncated': truncated}


def buildAttachmentMessage(text: str, workDir: str, attachments: list[dict]) -> str:
    # chat/stream 附件拼接（方案 §3.7）：校验路径/个数/合计大小/标记冲突，拼成 <attachment path="..."> 块。
    if len(attachments) > maxAttachments:
        raise RuntimeError(f'附件个数超限（>{maxAttachments}）。')
    blocks = []
    totalBytes = 0
    for item in attachments:
        relPath = item.get('path') if isinstance(item, dict) else None
        if not isinstance(relPath, str) or not relPath.strip():
            raise RuntimeError('附件 path 必须是非空字符串。')
        relPath = relPath.strip()
        fileInfo = readTextFile(workDir, relPath)
        if attachmentCloseTag in fileInfo['content']:
            raise RuntimeError(f'文件内容含附件标记 {attachmentCloseTag}，无法作为附件：{relPath}')
        totalBytes += fileInfo['size']
        if totalBytes > maxTotalBytes:
            raise RuntimeError(f'附件合计大小超限（>{maxTotalBytes // 1024 // 1024}MB）。')
        blocks.append(f'<attachment path="{relPath}">\n{fileInfo["content"]}\n</attachment>')
    parts = [text] if text else []
    parts.extend(blocks)
    return '\n\n'.join(parts)
