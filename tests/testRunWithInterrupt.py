'''
Author: wilbur
Version: 1.1
Date: 2026-09-23
Description: _runWithInterrupt 有界等待回归（bashPipeHangSessionLockFixPlan）：活首领超时、孤儿写端事故形、忽略 SIGTERM、中断叫醒管道死等、短命令成功、超时后释放会话锁、interruptEvent=None 同样有界。
  v1.1（subAgentPipeDrainFixPlan）：新增活首领大输出持续排空三用例——stderr 70,000B 正常返回、双管并发 700,000B、10MiB head/tail 有界；旧用例断言零修改。
'''

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from flamingoAgents.core.types import modelInterruptedError, toolContext
from flamingoAgents.tools.builtinTools import _runWithInterrupt

bashPath = shutil.which('bash')
skipWithoutBash = pytest.mark.skipif(
    os.name == 'nt' or not bashPath,
    reason='需要 POSIX bash',
)

orphanHolderSource = (
    'import os, subprocess, sys, time\n'
    'from pathlib import Path\n'
    'handshake = Path(sys.argv[1])\n'
    'childSource = (\n'
    '    "import sys, time\\n"\n'
    '    "from pathlib import Path\\n"\n'
    '    "Path(sys.argv[1]).write_bytes(b\'x\')\\n"\n'
    '    "time.sleep(30)\\n"\n'
    ')\n'
    'subprocess.Popen([sys.executable, "-c", childSource, str(handshake)])\n'
    'os._exit(0)\n'
)

stderrWriterSource = 'import os, sys\nos.write(2, b"x" * int(sys.argv[1]))\n'
bothPipesWriterSource = (
    'import os, sys, threading\n'
    'n = int(sys.argv[1])\n'
    'a = threading.Thread(target=lambda: os.write(1, b"o" * n))\n'
    'b = threading.Thread(target=lambda: os.write(2, b"e" * n))\n'
    'a.start(); b.start(); a.join(); b.join()\n'
)


def makeContext(workDir: Path, interruptEvent: threading.Event | None = None) -> toolContext:
    return toolContext(workDir=workDir, interruptEvent=interruptEvent)


def bashCommand(script: str) -> list[str]:
    return [bashPath or 'bash', '--noprofile', '--norc', '-c', script]


def orphanPipeCommand(handshakePath: Path) -> list[str]:
    return [sys.executable, '-c', orphanHolderSource, str(handshakePath)]


def runTimed(function):
    started = time.monotonic()
    try:
        return function(), time.monotonic() - started, None
    except Exception as error:
        return None, time.monotonic() - started, error


@skipWithoutBash
def testTimeoutKillsSleep(tmp_path):
    context = makeContext(tmp_path, threading.Event())
    _result, elapsed, error = runTimed(
        lambda: _runWithInterrupt(bashCommand('sleep 30'), context, 1)
    )
    assert isinstance(error, subprocess.TimeoutExpired)
    assert elapsed <= 3.5


def testTimeoutWhenLeaderExitsButChildHoldsPipes(tmp_path):
    handshake = tmp_path / 'handshake'
    context = makeContext(tmp_path, threading.Event())
    _result, elapsed, error = runTimed(
        lambda: _runWithInterrupt(orphanPipeCommand(handshake), context, 10)
    )
    assert isinstance(error, subprocess.TimeoutExpired)
    assert elapsed <= 2.5
    assert handshake.exists()


def testLeaderAliveLargeStderrCompletes(tmp_path):
    # T1：活首领写满 PIPE 容量（65,536B）不再互等，正常返回而非超时。
    context = makeContext(tmp_path, threading.Event())
    command = [sys.executable, '-c', stderrWriterSource, '70000']
    result, elapsed, error = runTimed(
        lambda: _runWithInterrupt(command, context, 5)
    )
    assert error is None
    assert result is not None
    assert result.returncode == 0
    assert len(result.stderr.encode('utf-8')) == 70000
    assert elapsed <= 1.5


def testBothPipesLargeConcurrent(tmp_path):
    # T3：双管并发各 700,000B：核心断言是“不因 PIPE 回压等满 timeout”而是正常返回；
    # 超出 spool 上限（head+tail=512KiB）的中间部分按设计丢弃，两端完整。
    from flamingoAgents.tools.builtinTools import pipeHeadCapBytes, pipeTailCapBytes

    context = makeContext(tmp_path, threading.Event())
    command = [sys.executable, '-c', bothPipesWriterSource, '700000']
    result, elapsed, error = runTimed(
        lambda: _runWithInterrupt(command, context, 5)
    )
    assert error is None
    assert result is not None
    capped = pipeHeadCapBytes + pipeTailCapBytes
    assert len(result.stdout.encode('utf-8')) == capped
    assert len(result.stderr.encode('utf-8')) == capped
    assert result.stdout.startswith('o' * 8)
    assert result.stdout.endswith('o' * 8)
    assert result.stderr.startswith('e' * 8)
    assert result.stderr.endswith('e' * 8)
    assert elapsed <= 3.5


def testTenMiBOutputBoundedSpool(tmp_path):
    # T4：10MiB 输出返回 head+tail 有界内容，父内存不随输出无界增长；耗时随 producer 而非 timeout。
    from flamingoAgents.tools.builtinTools import pipeHeadCapBytes, pipeTailCapBytes

    context = makeContext(tmp_path, threading.Event())
    command = [sys.executable, '-c', stderrWriterSource, str(10 * 1024 * 1024)]
    result, elapsed, error = runTimed(
        lambda: _runWithInterrupt(command, context, 5)
    )
    assert error is None
    assert result is not None
    raw = result.stderr.encode('utf-8')
    assert len(raw) == pipeHeadCapBytes + pipeTailCapBytes
    assert raw[:5] == b'xxxxx'
    assert raw[-5:] == b'xxxxx'
    assert elapsed <= 3.5


@skipWithoutBash
def testTimeoutWhenSigtermIgnored(tmp_path):
    context = makeContext(tmp_path, threading.Event())
    _result, elapsed, error = runTimed(
        lambda: _runWithInterrupt(bashCommand('trap "" TERM; sleep 30'), context, 1)
    )
    assert isinstance(error, subprocess.TimeoutExpired)
    assert elapsed <= 3.5


def testInterruptDuringPipeHang(tmp_path):
    handshake = tmp_path / 'handshake'
    interruptEvent = threading.Event()
    context = makeContext(tmp_path, interruptEvent)
    holder: dict = {}

    def runner():
        try:
            _runWithInterrupt(orphanPipeCommand(handshake), context, 30)
            holder['kind'] = 'returned'
        except Exception as error:
            holder['error'] = error

    thread = threading.Thread(target=runner)
    thread.start()
    waitUntil = time.monotonic() + 2.0
    while not handshake.exists():
        if time.monotonic() > waitUntil:
            interruptEvent.set()
            thread.join(5)
            pytest.fail('握手文件未出现')
        time.sleep(0.02)
    setAt = time.monotonic()
    interruptEvent.set()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(holder.get('error'), modelInterruptedError)
    assert time.monotonic() - setAt <= 2.5


@skipWithoutBash
def testShortCommandSuccess(tmp_path):
    context = makeContext(tmp_path, threading.Event())
    result, elapsed, error = runTimed(
        lambda: _runWithInterrupt(bashCommand('echo ok'), context, 5)
    )
    assert error is None
    assert result is not None
    assert result.returncode == 0
    assert 'ok' in (result.stdout or '')
    assert elapsed <= 1.0


def testSessionLockReleasedAfterOrphanPipeTimeout(tmp_path):
    handshake = tmp_path / 'handshake'
    context = makeContext(tmp_path, threading.Event())
    sessionLock = threading.RLock()
    holder: dict = {}

    def runner():
        with sessionLock:
            try:
                _runWithInterrupt(orphanPipeCommand(handshake), context, 10)
            except Exception as error:
                holder['error'] = error

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(holder.get('error'), subprocess.TimeoutExpired)
    assert sessionLock.acquire(timeout=0.2)
    sessionLock.release()


def testInterruptEventNoneStillBounded(tmp_path):
    handshake = tmp_path / 'handshake'
    context = makeContext(tmp_path, None)
    _result, elapsed, error = runTimed(
        lambda: _runWithInterrupt(orphanPipeCommand(handshake), context, 10)
    )
    assert isinstance(error, subprocess.TimeoutExpired)
    assert elapsed <= 2.5
