'''
Author: wilbur
Version: 1.2
Date: 2026-10-10
Description: _runWithInterrupt 有界等待回归（bashPipeHangSessionLockFixPlan）：活首领超时、孤儿写端事故形、忽略 SIGTERM、中断叫醒管道死等、短命令成功、超时后释放会话锁、interruptEvent=None 同样有界。
  v1.1（subAgentPipeDrainFixPlan）：新增活首领大输出持续排空三用例——stderr 70,000B 正常返回、双管并发 700,000B、10MiB head/tail 有界；旧用例断言零修改。
  v1.2（stopDrain409FixPlan T1-T3）：手动中断立即 TERM→短 grace→KILL 本轮进程组；C 类后台子孙随本轮被杀、B/A 类存活；只对当前 process.pid 进程组发信号；首领已退等 EOF 半段同样响应中断；manualStop / normalTimeout 诊断可区分。
'''

from __future__ import annotations

import os
import shutil
import signal
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

pidWriterSource = (
    'import os, sys, time\n'
    'from pathlib import Path\n'
    'Path(sys.argv[1]).write_text(str(os.getpid()))\n'
    'time.sleep(30)\n'
)

# 9.2 边界：sameGroup 是 C 类（继承本轮进程组），newSession 是 B 类（另开会话）。
sameGroupHolderSource = (
    'import os, subprocess, sys, time\n'
    'from pathlib import Path\n'
    'dirPath = Path(sys.argv[1])\n'
    'sameGroup = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
    'newSession = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)\n'
    '(dirPath / "leader.pid").write_text(str(os.getpid()))\n'
    '(dirPath / "same_group.pid").write_text(str(sameGroup.pid))\n'
    '(dirPath / "new_session.pid").write_text(str(newSession.pid))\n'
    'time.sleep(60)\n'
)

termIgnoringLeaderSource = (
    'import os, signal, sys, time\n'
    'from pathlib import Path\n'
    'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
    'Path(sys.argv[1]).write_text(str(os.getpid()))\n'
    'time.sleep(30)\n'
)


class recordingLogger:
    def __init__(self):
        self.events: list[dict] = []

    def logEvent(self, event: dict) -> None:
        self.events.append(event)


def waitForFile(path: Path, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


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


def waitForPidDeath(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pidAlive(pid):
            return True
        time.sleep(0.02)
    return not pidAlive(pid)


def killIfAlive(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


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


def testInterruptRespondsQuicklyBeforeToolTimeout(tmp_path):
    # T1：手动中断不吃固定 0.5s TERM-grace，也不回落 30s 工具 timeout——快速返回 modelInterruptedError。
    pidFile = tmp_path / 'leader.pid'
    interruptEvent = threading.Event()
    context = makeContext(tmp_path, interruptEvent)
    holder: dict = {}

    def runner():
        try:
            _runWithInterrupt([sys.executable, '-c', pidWriterSource, str(pidFile)], context, 30)
            holder['kind'] = 'returned'
        except Exception as error:
            holder['error'] = error

    thread = threading.Thread(target=runner)
    thread.start()
    assert waitForFile(pidFile)
    setAt = time.monotonic()
    interruptEvent.set()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(holder.get('error'), modelInterruptedError)
    assert time.monotonic() - setAt <= 0.4


def testInterruptKillsCurrentGroupKeepsNewSessionAndEarlierProcess(tmp_path):
    # 9.2 A/B/C 边界：C（本轮组内后台子孙）被杀；B（start_new_session）与 A（更早轮次遗留）存活。
    earlier = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(60)'],
        start_new_session=True,
    )
    leaderPid = None
    sameGroupPid = None
    newSessionPid = None
    try:
        interruptEvent = threading.Event()
        context = makeContext(tmp_path, interruptEvent)
        holder: dict = {}

        def runner():
            try:
                _runWithInterrupt([sys.executable, '-c', sameGroupHolderSource, str(tmp_path)], context, 30)
                holder['kind'] = 'returned'
            except Exception as error:
                holder['error'] = error

        thread = threading.Thread(target=runner)
        thread.start()
        assert waitForFile(tmp_path / 'same_group.pid')
        assert waitForFile(tmp_path / 'new_session.pid')
        leaderPid = int((tmp_path / 'leader.pid').read_text(encoding='utf-8'))
        sameGroupPid = int((tmp_path / 'same_group.pid').read_text(encoding='utf-8'))
        newSessionPid = int((tmp_path / 'new_session.pid').read_text(encoding='utf-8'))
        interruptEvent.set()
        thread.join(5)
        assert not thread.is_alive()
        assert isinstance(holder.get('error'), modelInterruptedError)
        assert waitForPidDeath(sameGroupPid), 'C 类本轮后台子孙应随本轮被杀'
        assert pidAlive(newSessionPid), 'B 类 start_new_session 进程不应受影响'
        assert pidAlive(earlier.pid), 'A 类更早轮次遗留进程不应受影响'
    finally:
        for pid in (newSessionPid, sameGroupPid, leaderPid, earlier.pid):
            killIfAlive(pid)
        earlier.wait(timeout=2)


def testInterruptSignalsOnlyCurrentProcessGroupWithShortGrace(tmp_path, monkeypatch):
    # 9.2.4：终止调用链只对当前 process.pid 进程组发信号，且 TERM→KILL 走短 grace（≤0.3s 上限）。
    pidFile = tmp_path / 'leader.pid'
    interruptEvent = threading.Event()
    context = makeContext(tmp_path, interruptEvent)
    calls: list[tuple[int, int, float]] = []
    realKillpg = os.killpg

    def tracking(pgid, sig):
        calls.append((pgid, sig, time.monotonic()))
        return realKillpg(pgid, sig)

    monkeypatch.setattr(os, 'killpg', tracking)
    holder: dict = {}

    def runner():
        try:
            _runWithInterrupt([sys.executable, '-c', termIgnoringLeaderSource, str(pidFile)], context, 30)
            holder['kind'] = 'returned'
        except Exception as error:
            holder['error'] = error

    thread = threading.Thread(target=runner)
    thread.start()
    assert waitForFile(pidFile)
    leaderPid = int(pidFile.read_text(encoding='utf-8'))
    interruptEvent.set()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(holder.get('error'), modelInterruptedError)
    assert calls, '必须向进程组发信号'
    assert all(pgid == leaderPid for pgid, _sig, _at in calls)
    assert calls[0][1] == signal.SIGTERM
    assert any(sig == getattr(signal, 'SIGKILL', signal.SIGTERM) for _pgid, sig, _at in calls)
    graceSpan = calls[-1][2] - calls[0][2]
    assert graceSpan <= 0.45, f'TERM→KILL 应为短 grace，实际 {graceSpan:.3f}s'


def testInterruptDuringLeaderExitedPipeHangIsManualStop(tmp_path):
    # T2：首领已退、reader 等 EOF 的半段同样响应手动终止，不回落 postLeaderGrace 超时。
    handshake = tmp_path / 'handshake'
    interruptEvent = threading.Event()
    logger = recordingLogger()
    context = toolContext(workDir=tmp_path, interruptEvent=interruptEvent, logger=logger, sessionId='diag')
    holder: dict = {}

    def runner():
        try:
            _runWithInterrupt(orphanPipeCommand(handshake), context, 30)
            holder['kind'] = 'returned'
        except Exception as error:
            holder['error'] = error

    thread = threading.Thread(target=runner)
    thread.start()
    assert waitForFile(handshake)
    setAt = time.monotonic()
    interruptEvent.set()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(holder.get('error'), modelInterruptedError)
    assert time.monotonic() - setAt <= 1.0
    assert any(event.get('type') == 'manualStop' for event in logger.events)


def testManualStopAndTimeoutLogDistinctDiagnostics(tmp_path):
    # T3：手动终止落 manualStop，正常 timeout 落 normalTimeout，两类日志可区分。
    pidFile = tmp_path / 'leader.pid'
    interruptEvent = threading.Event()
    manualLogger = recordingLogger()
    manualContext = toolContext(workDir=tmp_path, interruptEvent=interruptEvent, logger=manualLogger, sessionId='diag')
    holder: dict = {}

    def runner():
        try:
            _runWithInterrupt([sys.executable, '-c', pidWriterSource, str(pidFile)], manualContext, 30)
        except Exception as error:
            holder['error'] = error

    thread = threading.Thread(target=runner)
    thread.start()
    assert waitForFile(pidFile)
    interruptEvent.set()
    thread.join(5)
    assert isinstance(holder.get('error'), modelInterruptedError)
    assert any(event.get('type') == 'manualStop' for event in manualLogger.events)
    assert not any(event.get('type') == 'normalTimeout' for event in manualLogger.events)

    timeoutLogger = recordingLogger()
    timeoutContext = toolContext(workDir=tmp_path, logger=timeoutLogger, sessionId='diag')
    _result, _elapsed, error = runTimed(
        lambda: _runWithInterrupt([sys.executable, '-c', 'import time; time.sleep(30)'], timeoutContext, 1)
    )
    assert isinstance(error, subprocess.TimeoutExpired)
    assert any(event.get('type') == 'normalTimeout' for event in timeoutLogger.events)
    assert not any(event.get('type') == 'manualStop' for event in timeoutLogger.events)
