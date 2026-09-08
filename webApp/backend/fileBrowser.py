'''
Author: wilbur
Version: 1.4
Date: 2026-09-07
Description: workDir 文件浏览纯函数层（迭代二方案 §3.8）：is_relative_to 路径拘禁、目录列举（目录在前、单条目 stat 失败跳过、
            截断标记）、文本文件读取（二进制校验）；OSError 统一转 RuntimeError 中文消息走 400 透传，不落 fallback 500。
            v1.1 取消 maxFileBytes 单文件大小限制，readTextFile 不再校验文件大小，listDir 中 attachable 始终为 true。
            v1.2（fileMentionFixPlan）：buildAttachmentMessage 支持目录附件（type='dir'）——expandDirAttachment 递归展开为
            「相对路径+内容」文本块；递归逐条目 resolve+is_relative_to 校验拦符号链接逃逸，realpath 集合防环防重复；
            单目录最多展开 100 个文件、受 maxTotalBytes 截断，二进制/不可读跳过并注明；type 缺省按 file，旧调用零影响。
            v1.3（workDirPickerPlan §2.1）：新增 listAbsDirs--服务器绝对路径列目录，供新建会话 workDir 补全（无会话无 workDir，
            不走 resolveInside 拘禁）；expanduser、仅目录（follow_symlinks）、写死滤 dot 目录、单条目失败跳过、
            全量 name.lower() 排序后再截 maxEntries（刻意与 listDir 的物理序 early-break 不同，避免前缀匹配丢项）。
            v1.4（fileMentionPathOnlyPlan）：buildAttachmentMessage 改为仅校验路径并输出真实绝对路径清单，不读正文、不解包、
            不递归目录；删除 expandDirAttachment 与个数/内容预算常量；readTextFile 预览行为不变。
'''

from __future__ import annotations

import json
import os
from pathlib import Path

maxEntries = 500               # 目录单层列举上限


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


def listAbsDirs(absPath: str) -> dict:
    # 服务器绝对路径列目录（workDirPickerPlan §2.1）：新建会话 workDir 补全专用，无会话上下文，不走 resolveInside 拘禁。
    # 仅目录（follow_symlinks，否则 /tmp 大量链接会空）、写死滤 dot 目录、单条目失败跳过；
    # 全量 name.lower() 排序后再截 maxEntries（评审问题 6：不能照抄 listDir 的物理序 early-break，否则前缀匹配会丢项）。
    target = Path(absPath).expanduser()
    try:
        if not target.exists():
            raise RuntimeError(f'目录不存在：{target}')
        if not target.is_dir():
            raise RuntimeError(f'不是目录：{target}')
        names = []
        with os.scandir(target) as iterator:
            for item in iterator:
                if item.name.startswith('.'):
                    continue
                try:
                    if not item.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                names.append(item.name)
    except OSError as error:
        raise RuntimeError(f'目录不存在或不可读：{target}（{error}）')
    names.sort(key=str.lower)
    truncated = len(names) > maxEntries
    return {'path': str(target), 'entries': [{'name': name, 'type': 'dir'} for name in names[:maxEntries]], 'truncated': truncated}


def buildAttachmentMessage(text: str, workDir: str, attachments: list[dict]) -> str:
    # chat/stream 路径引用拼接（fileMentionPathOnlyPlan）：只校验位置，输出真实绝对路径清单，不读正文。
    encodedPaths = []
    for item in attachments:
        if not isinstance(item, dict):
            raise RuntimeError('附件必须是对象。')
        relPath = item.get('path')
        if not isinstance(relPath, str) or relPath == '':
            raise RuntimeError('附件 path 必须是非空字符串。')
        if '\x00' in relPath:
            raise RuntimeError('附件 path 含非法空字符。')
        try:
            target = resolveInside(workDir, relPath)
            if not target.exists():
                raise RuntimeError(f'路径不存在：{relPath}')
            if not target.is_file() and not target.is_dir():
                raise RuntimeError(f'不是文件或目录：{relPath}')
        except OSError as error:
            raise RuntimeError(f'路径不存在或不可访问：{relPath}（{error}）')
        encodedPaths.append(json.dumps(str(target), ensure_ascii=False))
    if not encodedPaths:
        return text
    lines = ['引用路径（仅提供位置，未读取内容）：']
    lines.extend(f'- {path}' for path in encodedPaths)
    block = '\n'.join(lines)
    return f'{text}\n\n{block}' if text else block
