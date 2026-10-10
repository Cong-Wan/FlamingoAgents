'''
Author: wilbur
Version: 1.1
Date: 2026-10-10
Description: askSubAgentTool 端到端回归（subAgentPipeDrainFixPlan）：fakeSdk 注入 sdkEntryPath，
  覆盖 T2 stdout 大 reply 成功、T6 超时诊断字段齐全且 childLogPath 真实存在、
  以及 stdout 末行无 reply JSON 时的显式报错。
  v1.1（stopDrain409FixPlan T1/T2/9.3）：新增执行中中断用例——子进程组被杀、父工具快速抛 modelInterruptedError。
'''

from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import time
from pathlib import Path

from flamingoAgents.core.types import modelInterruptedError, toolContext
from flamingoAgents.tools import builtinTools

largeReplySource = """import json, os
size = int(os.environ['FAKE_REPLY_BYTES'])
print(json.dumps({'reply': 'r' * size, 'error': None}))
"""

pipeHolderSource = """import os, time
os.write(2, b'x' * 70000)
time.sleep(30)
"""

noJsonSource = 'print("not-json")\n'

slowSdkSource = (
    'import os, time\n'
    'from pathlib import Path\n'
    'Path(os.environ["FAKE_HANDSHAKE"]).write_text(str(os.getpid()))\n'
    'time.sleep(300)\n'
)


def pidAlive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        statText = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
        state = statText.split(') ', 1)[1].split(' ', 1)[0]
        if state == 'Z':
            return False
    except (OSError, IndexError):
        pass
    return True


def withFakeSdk(tmp_path: Path, source: str, env: dict[str, str] | None = None):
    script = tmp_path / 'fakeSdk.py'
    script.write_text(source)
    oldPath = builtinTools.sdkEntryPath
    builtinTools.sdkEntryPath = script
    savedEnv = {key: os.environ.get(key) for key in (env or {})}
    for key, value in (env or {}).items():
        os.environ[key] = value

    def restore():
        builtinTools.sdkEntryPath = oldPath
        for key, value in savedEnv.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return restore


def makeContext(tmp_path: Path) -> toolContext:
    return toolContext(workDir=tmp_path, interruptEvent=None, sessionId='parent-test')


def testLargeReplySucceeds(tmp_path):
    restore = withFakeSdk(tmp_path, largeReplySource, {'FAKE_REPLY_BYTES': '70000'})
    try:
        started = time.monotonic()
        result = builtinTools.askSubAgentTool(
            {'model': 'fake/model', 'prompt': 'x', 'timeout': 3},
            makeContext(tmp_path),
        )
        elapsed = time.monotonic() - started
        assert not result.isError
        assert len(result.content) == 70000
        assert result.details['childSessionId']
        assert elapsed <= 2.0
    finally:
        restore()


def testTimeoutDiagnosticsPreserved(tmp_path):
    restore = withFakeSdk(tmp_path, pipeHolderSource)
    try:
        started = time.monotonic()
        result = builtinTools.askSubAgentTool(
            {'model': 'fake/model', 'prompt': 'x', 'timeout': 1},
            makeContext(tmp_path),
        )
        elapsed = time.monotonic() - started
        assert result.isError
        assert elapsed <= 3.5
        details = result.details
        for key in (
            'childSessionId',
            'childLogPath',
            'stdoutBytes',
            'stderrBytes',
            'partialStderrTail',
            'lastActivityAt',
            'timeoutSource',
        ):
            assert key in details, key
        assert details['timeoutSource'] in ('pipeOrLeader', 'postLeaderGrace')
        assert details['stderrBytes'] >= 70000
        assert 'x' in details['partialStderrTail']
        # childLogPath 必须指向按同一 workDir 规则可推算的位置（不要求文件已存在，fakeSdk 不写日志）
        assert details['childLogPath'].endswith(f"{details['childSessionId']}.jsonl")
    finally:
        restore()


def testStdoutWithoutReplyJsonFailsExplicitly(tmp_path):
    restore = withFakeSdk(tmp_path, noJsonSource)
    try:
        result = builtinTools.askSubAgentTool(
            {'model': 'fake/model', 'prompt': 'x', 'timeout': 3},
            makeContext(tmp_path),
        )
        assert result.isError
        assert 'reply' in result.content
    finally:
        restore()


def testTimeoutChildLogPathMatchesRealLogRule(tmp_path):
    # T6 补充：childLogPath 与 logPaths.resolveSessionLogDir('cliData', childWorkDir) 完全一致。
    from flamingoAgents.utils.logPaths import resolveSessionLogDir

    restore = withFakeSdk(tmp_path, pipeHolderSource)
    try:
        result = builtinTools.askSubAgentTool(
            {'model': 'fake/model', 'prompt': 'x', 'timeout': 1, 'workDir': str(tmp_path)},
            toolContext(workDir=tmp_path, interruptEvent=None, sessionId='parent-test'),
        )
        expected = resolveSessionLogDir('cliData', tmp_path.resolve()) / f"{result.details['childSessionId']}.jsonl"
        assert result.details['childLogPath'] == str(expected)
    finally:
        restore()


def testInterruptKillsSubAgentProcessGroupAndReturnsFast(tmp_path):
    # 9.3：askSubAgent 执行中手动终止——子进程组被杀、父工具 1s 内抛 modelInterruptedError，
    # 不回落子代理 timeout（fakeSdk sleep 300，timeout 600）。
    handshake = tmp_path / 'handshake'
    restore = withFakeSdk(tmp_path, slowSdkSource, {'FAKE_HANDSHAKE': str(handshake)})
    childPid = None
    try:
        interruptEvent = threading.Event()
        context = toolContext(workDir=tmp_path, interruptEvent=interruptEvent, sessionId='parent-test')
        holder: dict = {}

        def runner():
            try:
                builtinTools.askSubAgentTool({'model': 'fake/model', 'prompt': 'x', 'timeout': 600}, context)
                holder['kind'] = 'returned'
            except Exception as error:
                holder['error'] = error

        thread = threading.Thread(target=runner)
        thread.start()
        deadline = time.monotonic() + 3.0
        while not handshake.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert handshake.exists(), 'fakeSdk 未启动'
        childPid = int(handshake.read_text(encoding='utf-8'))
        setAt = time.monotonic()
        interruptEvent.set()
        thread.join(5)
        assert not thread.is_alive()
        assert isinstance(holder.get('error'), modelInterruptedError)
        assert time.monotonic() - setAt <= 1.0
        deathDeadline = time.monotonic() + 2.0
        while pidAlive(childPid) and time.monotonic() < deathDeadline:
            time.sleep(0.02)
        assert not pidAlive(childPid), '子进程组应被杀'
    finally:
        restore()
        if childPid is not None and pidAlive(childPid):
            try:
                os.kill(childPid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
