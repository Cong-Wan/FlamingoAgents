'''
Author: wilbur
Version: 1.10
Date: 2026-09-23
Description: Provides executable handlers (execute/preview) for built-in tools. v1.9 askSubAgent 显式传 usage-source=subagent 与父会话 ID，子进程用量写入统一账本。v1.10（subAgentPipeDrainFixPlan）：_runWithInterrupt 改为每管一个 reader 线程持续排空到 head/tail 有界 spool，活 child 写满 PIPE 不再互等；首领退出 0.5s EOF 宽限语义保留；超时异常附带 pipeStats；askSubAgentTool 生成并传 --session-id，超时返回 childSessionId/childLogPath/partialStderrTail 等诊断，stdout 末行无 reply 时显式报错。
'''

from __future__ import annotations

import difflib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flamingoAgents.core.types import modelInterruptedError, toolContext, toolOutput
from flamingoAgents.tools.toolConfig import toolSchemaSpec
from flamingoAgents.tools.toolDefinition import defineTool, toolDefinition, toolExecuteFunction, toolPreviewFunction
from flamingoAgents.utils.logPaths import newSessionId, resolveSessionLogDir

maxTimeoutSeconds = 120
defaultTimeoutSeconds = 30
defaultSubAgentTimeoutSeconds = 600
maxSubAgentTimeoutSeconds = 3600
pipeHeadCapBytes = 256 * 1024      # 每管保留输出前缀上限（bash 头部 clip 语义）
pipeTailCapBytes = 256 * 1024      # 每管保留输出后缀上限（askSubAgent 尾行 JSON 语义）
pipeReadChunkBytes = 64 * 1024


# --- read ---

def previewReadTool(arguments: dict[str, Any]) -> str:
    path = str(arguments.get('path', ''))
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    return f'{path} offset={offset} limit={limit}'


def readTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    if context.debugConsole:
        context.debugConsole.debug(f'读取工具开始 path={path} offset={offset} limit={limit}')
    if offset < 1 or limit < 1:
        return toolOutput(content='read.offset 和 read.limit 必须大于 0。', isError=True)
    if not path.exists() or not path.is_file():
        return toolOutput(content=f'文件不存在或不是普通文件：{path}', isError=True, details={'path': str(path)})

    content = path.read_text(encoding='utf-8')
    lines = content.splitlines(keepends=True)
    startIndex = offset - 1
    selectedText = ''.join(lines[startIndex:startIndex + limit])
    truncated = startIndex + limit < len(lines)
    if context.debugConsole:
        context.debugConsole.debug(
            f'读取工具完成 path={path} totalLines={len(lines)} '
            f'returnedChars={len(selectedText)} truncated={truncated}'
        )
    return toolOutput(
        content=selectedText,
        details={
            'path': str(path),
            'offset': offset,
            'limit': limit,
            'totalLines': len(lines),
            'truncated': truncated,
        },
    )


# --- write ---

def previewWriteTool(arguments: dict[str, Any]) -> str:
    content = str(arguments.get('content', ''))
    return f"{arguments.get('path', '')} bytes={len(content.encode('utf-8'))}"


def writeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
    content = str(arguments['content'])
    if context.debugConsole:
        context.debugConsole.debug(f'写入工具开始 path={path} bytes={len(content.encode("utf-8"))}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    if context.debugConsole:
        context.debugConsole.debug(f'写入工具完成 path={path}')
    return toolOutput(
        content=f'已写入文件：{path}',
        details={
            'path': str(path),
            'bytes': len(content.encode('utf-8')),
        },
    )


# --- edit ---

def previewEditTool(arguments: dict[str, Any]) -> str:
    edits = arguments.get('edits', [])
    editCount = len(edits) if isinstance(edits, list) else 0
    return f"{arguments.get('path', '')} edits={editCount}"


def editTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
    edits = arguments['edits']
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具开始 path={path} editCount={len(edits)}')
    if not path.exists() or not path.is_file():
        return toolOutput(content=f'文件不存在或不是普通文件：{path}', isError=True, details={'path': str(path)})

    originalContent = path.read_text(encoding='utf-8')
    replacements: list[tuple[int, int, str]] = []
    for index, editItem in enumerate(edits):
        oldText = editItem['oldText']
        newText = editItem['newText']
        matchCount = originalContent.count(oldText)
        if matchCount != 1:
            return toolOutput(content=f'第 {index + 1} 个 oldText 必须精确且唯一匹配，当前匹配数：{matchCount}。', isError=True)
        startIndex = originalContent.index(oldText)
        endIndex = startIndex + len(oldText)
        replacements.append((startIndex, endIndex, newText))

    replacements.sort(key=lambda item: item[0])
    previousEnd = -1
    for startIndex, endIndex, newText in replacements:
        if startIndex < previousEnd:
            return toolOutput(content='多个 edits 不能重叠。', isError=True)
        previousEnd = endIndex

    updatedContent = originalContent
    for startIndex, endIndex, newText in sorted(replacements, key=lambda item: item[0], reverse=True):
        updatedContent = updatedContent[:startIndex] + newText + updatedContent[endIndex:]

    diffText = ''.join(difflib.unified_diff(
        originalContent.splitlines(keepends=True),
        updatedContent.splitlines(keepends=True),
        fromfile=str(path) + ':before',
        tofile=str(path) + ':after',
        n=3,
    ))
    path.write_text(updatedContent, encoding='utf-8')
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具完成 path={path} diffChars={len(diffText)}')
    return toolOutput(
        content=diffText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits)},
    )


# --- bash ---

def previewBashTool(arguments: dict[str, Any]) -> str:
    return str(arguments.get('command', ''))


def _isInterrupted(context: toolContext) -> bool:
    return context.interruptEvent is not None and context.interruptEvent.is_set()


class _pipeSpool:
    # head/tail 双端有界缓冲：head 保前缀（bash clip 语义），tail 保后缀（askSubAgent 尾行 JSON 语义）；
    # 中间溢出丢弃并计数；lastActivityAt 记最后一次收到字节的墙钟时刻（0 表示尚无输出）。

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.totalBytes = 0
        self.droppedBytes = 0
        self.lastActivityAt = 0.0
        self.eof = False
        self.lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        if not data:
            return
        with self.lock:
            self.totalBytes += len(data)
            self.lastActivityAt = time.time()
            rest = data
            if len(self.head) < pipeHeadCapBytes:
                take = min(len(data), pipeHeadCapBytes - len(self.head))
                self.head += data[:take]
                rest = data[take:]
            if rest:
                self.tail += rest
                overflow = len(self.tail) - pipeTailCapBytes
                if overflow > 0:
                    del self.tail[:overflow]
                    self.droppedBytes += overflow

    def markEof(self) -> None:
        self.eof = True

    def text(self) -> str:
        with self.lock:
            raw = bytes(self.head) + bytes(self.tail)
        return raw.decode('utf-8', errors='replace')

    def snapshot(self) -> dict[str, int | float]:
        with self.lock:
            return {
                'bytes': self.totalBytes,
                'droppedBytes': self.droppedBytes,
                'lastActivityAt': self.lastActivityAt,
            }


def _drainPipe(stream, spool: _pipeSpool) -> None:
    # 阻塞读整管到 spool 直到 EOF；异常（含父侧关读端）一律按 EOF 收尾，线程 daemon 兜底。
    readChunk = getattr(stream, 'read1', None)
    if not callable(readChunk):
        readChunk = stream.read
    try:
        while True:
            try:
                data = readChunk(pipeReadChunkBytes)
            except Exception:
                break
            if not data:
                break
            spool.feed(data)
    finally:
        spool.markEof()


def _closeReadPipes(process: subprocess.Popen) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            pass


def _signalGroup(process: subprocess.Popen, sig, fallback) -> None:
    try:
        os.killpg(process.pid, sig)
    except Exception:
        try:
            fallback()
        except Exception:
            pass


def _killProcessGroup(process: subprocess.Popen) -> None:
    # 首领已 reap 时 wait 会立刻成功，不代表进程组已空；TERM 后固定等再无条件 SIGKILL。
    _signalGroup(process, signal.SIGTERM, process.terminate)
    time.sleep(0.5)
    _signalGroup(process, getattr(signal, 'SIGKILL', signal.SIGTERM), process.kill)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def _finishStop(
    process: subprocess.Popen,
    reason: str,
    command: list[str],
    timeout: int,
    outSpool: _pipeSpool,
    errSpool: _pipeSpool,
    leaderExitedAt: float | None,
    startedAtWall: float,
) -> None:
    _killProcessGroup(process)
    # 杀组后写端全灭，reader 线程将收到 EOF；有界等待其收尾，不与 reader 抢管道（不 communicate）。
    eofDeadline = time.monotonic() + 0.3
    while time.monotonic() < eofDeadline and not (outSpool.eof and errSpool.eof):
        time.sleep(0.02)
    if reason == 'interrupt':
        raise modelInterruptedError('用户已停止')
    outStats = outSpool.snapshot()
    errStats = errSpool.snapshot()
    lastActivityAt = max(outStats['lastActivityAt'], errStats['lastActivityAt']) or startedAtWall
    error = subprocess.TimeoutExpired(command, timeout, output=outSpool.text(), stderr=errSpool.text())
    error.pipeStats = {
        'stdoutBytes': outStats['bytes'],
        'stderrBytes': errStats['bytes'],
        'stdoutDroppedBytes': outStats['droppedBytes'],
        'stderrDroppedBytes': errStats['droppedBytes'],
        'lastActivityAt': lastActivityAt,
        'timeoutSource': 'postLeaderGrace' if leaderExitedAt is not None else 'pipeOrLeader',
    }
    raise error


def _runWithInterrupt(command: list[str], context: toolContext, timeout: int) -> subprocess.CompletedProcess:
    # 持续排空版有界 Popen：每管一个 reader 线程写入 head/tail 有界 spool，活 child 写满 PIPE 不再互等；
    # 首领退出后保留 0.5s EOF 宽限（旧孤儿事故形语义）；EOF + 首领已退才组装返回。
    process = subprocess.Popen(
        command,
        cwd=str(context.workDir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    outSpool = _pipeSpool()
    errSpool = _pipeSpool()
    outReader = threading.Thread(target=_drainPipe, args=(process.stdout, outSpool), daemon=True)
    errReader = threading.Thread(target=_drainPipe, args=(process.stderr, errSpool), daemon=True)
    outReader.start()
    errReader.start()
    startedAtWall = time.time()
    deadline = time.monotonic() + timeout
    leaderExitedAt = None
    try:
        while True:
            if _isInterrupted(context):
                _finishStop(process, 'interrupt', command, timeout, outSpool, errSpool, leaderExitedAt, startedAtWall)
            if time.monotonic() >= deadline:
                _finishStop(process, 'timeout', command, timeout, outSpool, errSpool, leaderExitedAt, startedAtWall)
            if process.poll() is not None:
                if leaderExitedAt is None:
                    leaderExitedAt = time.monotonic()
                if outSpool.eof and errSpool.eof:
                    break
                if time.monotonic() - leaderExitedAt >= 0.5:
                    _finishStop(process, 'timeout', command, timeout, outSpool, errSpool, leaderExitedAt, startedAtWall)
            time.sleep(0.1)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        return subprocess.CompletedProcess(command, process.returncode, outSpool.text(), errSpool.text())
    finally:
        _closeReadPipes(process)


def bashTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    command = arguments.get('command')
    if not isinstance(command, str) or not command.strip():
        return toolOutput(content='bash.command 必须是非空字符串。', isError=True)

    timeout = int(arguments.get('timeout', defaultTimeoutSeconds))
    if timeout < 1:
        timeout = defaultTimeoutSeconds
    if timeout > maxTimeoutSeconds:
        timeout = maxTimeoutSeconds
    maxOutput = int(arguments.get('maxOutput', 2000))
    if context.debugConsole:
        context.debugConsole.debug(f'bash 工具开始 command={command} timeout={timeout} maxOutput={maxOutput} cwd={context.workDir}')

    def clip(text: str) -> tuple[str, bool]:
        if maxOutput < 0 or len(text) <= maxOutput:
            return text, False
        return text[:maxOutput] + '\n<truncated>', True

    try:
        completedProcess = _runWithInterrupt(['bash', '-lc', command], context, timeout)
        stdoutText, stdoutTruncated = clip(completedProcess.stdout)
        stderrText, stderrTruncated = clip(completedProcess.stderr)
        if context.debugConsole:
            context.debugConsole.debug(f'bash 工具完成 exitCode={completedProcess.returncode}')
        return toolOutput(
            content=(
                f'exitCode: {completedProcess.returncode}\n'
                f'stdout:\n{stdoutText}\n'
                f'stderr:\n{stderrText}'
            ),
            isError=completedProcess.returncode != 0,
            details={
                'command': command,
                'timeout': timeout,
                'maxOutput': maxOutput,
                'exitCode': completedProcess.returncode,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
    except subprocess.TimeoutExpired as error:
        stdoutText = decodeProcessText(error.stdout)
        stderrText = decodeProcessText(error.stderr)
        stdoutText, stdoutTruncated = clip(stdoutText)
        stderrText, stderrTruncated = clip(stderrText)
        if context.debugConsole:
            context.debugConsole.debug(f'bash 工具超时 command={command} timeout={timeout}')
        return toolOutput(
            content=(
                f'命令超时，已终止。timeout: {timeout}\n'
                f'stdout:\n{stdoutText}\n'
                f'stderr:\n{stderrText}'
            ),
            isError=True,
            details={
                'command': command,
                'timeout': timeout,
                'maxOutput': maxOutput,
                'timeoutExpired': True,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )


def decodeProcessText(value: str | bytes | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return ''


# --- askSubAgent ---

sdkEntryPath = Path(__file__).resolve().parents[2] / 'sdkEntry.py'


def previewAskSubAgentTool(arguments: dict[str, Any]) -> str:
    model = str(arguments.get('model', ''))
    prompt = str(arguments.get('prompt', ''))
    timeout = arguments.get('timeout', defaultSubAgentTimeoutSeconds)
    return f'{model} timeout={timeout} prompt={prompt[:60]}'


def askSubAgentTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    model = str(arguments.get('model', '')).strip()
    prompt = str(arguments.get('prompt', '')).strip()
    if not model or '/' not in model:
        return toolOutput(content='askSubAgent.model 必须是 provider/model 格式。', isError=True)
    if not prompt:
        return toolOutput(content='askSubAgent.prompt 不能为空。', isError=True)

    timeout = int(arguments.get('timeout', defaultSubAgentTimeoutSeconds))
    if timeout < 1:
        timeout = defaultSubAgentTimeoutSeconds
    if timeout > maxSubAgentTimeoutSeconds:
        timeout = maxSubAgentTimeoutSeconds

    command = [
        sys.executable, str(sdkEntryPath),
        '--model', model,
        '--prompt', prompt,
        '--json',
    ]
    # system 不传则不加 --system，让 sdkEntry 走默认（config/systemPrompt.md）；
    # 传了则透传（sdkEntry 智能识别纯文本或 md 文件路径）。
    system = str(arguments.get('system', '')).strip()
    if system:
        command += ['--system', system]
    tools = str(arguments.get('tools', '')).strip()
    if tools:
        command += ['--tools', tools]
    workDir = str(arguments.get('workDir', '')).strip() or str(context.workDir)
    command += ['--work-dir', workDir]
    childSessionId = newSessionId()
    command += ['--session-id', childSessionId]
    childWorkDir = Path(workDir)
    if not childWorkDir.is_absolute():
        childWorkDir = context.workDir / childWorkDir
    childLogPath = resolveSessionLogDir('cliData', childWorkDir.resolve()) / f'{childSessionId}.jsonl'
    command += ['--usage-source', 'subagent']
    if context.sessionId:
        command += ['--parent-session-id', context.sessionId]

    if context.debugConsole:
        context.debugConsole.debug(f'子代理开始 model={model} workDir={workDir} tools={tools or "<none>"} timeout={timeout}')
    try:
        completedProcess = _runWithInterrupt(command, context, timeout)
    except subprocess.TimeoutExpired as error:
        if context.debugConsole:
            context.debugConsole.debug(f'子代理超时 model={model} timeout={timeout}')
        pipeStats = getattr(error, 'pipeStats', None)
        pipeStats = pipeStats if isinstance(pipeStats, dict) else {}
        stderrTail = (error.stderr or '')[-2000:]
        return toolOutput(
            content=f'子代理超时被终止（{timeout}s）。',
            isError=True,
            details={
                'timeout': timeout,
                'timeoutExpired': True,
                'model': model,
                'childSessionId': childSessionId,
                'childLogPath': str(childLogPath),
                'stdoutBytes': pipeStats.get('stdoutBytes', 0),
                'stderrBytes': pipeStats.get('stderrBytes', 0),
                'stdoutDroppedBytes': pipeStats.get('stdoutDroppedBytes', 0),
                'stderrDroppedBytes': pipeStats.get('stderrDroppedBytes', 0),
                'partialStderrTail': stderrTail,
                'lastActivityAt': pipeStats.get('lastActivityAt'),
                'timeoutSource': pipeStats.get('timeoutSource'),
            },
        )

    # stdout 最后一行是 --json 输出的单行 JSON。
    stdoutLines = [line for line in completedProcess.stdout.splitlines() if line.strip()]
    payload: dict[str, Any] = {}
    if stdoutLines:
        try:
            payload = json.loads(stdoutLines[-1])
        except json.JSONDecodeError:
            payload = {}
    reply = payload.get('reply')
    error = payload.get('error')
    isError = completedProcess.returncode != 0 or error is not None or reply is None
    if context.debugConsole:
        context.debugConsole.debug(f'子代理完成 exitCode={completedProcess.returncode} isError={isError} timeout={timeout}')
    commonDetails = {
        'model': model,
        'workDir': workDir,
        'tools': tools,
        'exitCode': completedProcess.returncode,
        'timeout': timeout,
        'childSessionId': childSessionId,
        'childLogPath': str(childLogPath),
    }
    if isError:
        if reply is None and error is None:
            content = '子代理 stdout 最后一行不是含 reply 的 JSON（可能被 head/tail 有界截断或子进程异常）。'
        else:
            content = f'子代理失败 exitCode={completedProcess.returncode}：{error or (completedProcess.stderr.strip()[:500] or "未知错误")}'
        return toolOutput(content=content, isError=True, details=commonDetails)
    return toolOutput(content=str(reply), details=commonDetails)


# --- schema-driven assembly ---

executableMap: dict[str, tuple[toolExecuteFunction, toolPreviewFunction]] = {
    'read': (readTool, previewReadTool),
    'write': (writeTool, previewWriteTool),
    'edit': (editTool, previewEditTool),
    'bash': (bashTool, previewBashTool),
    'askSubAgent': (askSubAgentTool, previewAskSubAgentTool),
}


def createBuiltinTools(toolSchemas: list[toolSchemaSpec], debugConsole=None) -> list[toolDefinition]:
    definitions: list[toolDefinition] = []
    for schema in toolSchemas:
        handlers = executableMap.get(schema.name)
        if handlers is None:
            raise RuntimeError(f'未知工具实现：{schema.name}')
        execute, preview = handlers
        definition = defineTool(
            name=schema.name,
            description=schema.description,
            parameters=schema.parameters,
            execute=execute,
            permissions=schema.permissions,
            preview=preview,
        )
        definitions.append(definition)
        if debugConsole:
            debugConsole.debug(
                f'绑定工具实现 tool={schema.name} '
                f'permissions={len(schema.permissions)}'
            )
    if debugConsole:
        debugConsole.debug(f'工具定义装配完成 count={len(definitions)}')
    return definitions
