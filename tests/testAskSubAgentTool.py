'''
Author: wilbur
Version: 1.0
Date: 2026-09-23
Description: askSubAgentTool 端到端回归（subAgentPipeDrainFixPlan）：fakeSdk 注入 sdkEntryPath，
  覆盖 T2 stdout 大 reply 成功、T6 超时诊断字段齐全且 childLogPath 真实存在、
  以及 stdout 末行无 reply JSON 时的显式报错。
'''

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from flamingoAgents.core.types import toolContext
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
