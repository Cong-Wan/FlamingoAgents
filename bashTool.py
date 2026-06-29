'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Executes bash commands with timeout, captured output, and truncated previews.
'''

from __future__ import annotations

import subprocess
from typing import Any

from agentTypes import toolExecutionContext, toolResult
from jsonlLogger import makePreview

maxTimeoutSeconds = 120
defaultTimeoutSeconds = 30


def executeBash(arguments: dict[str, Any], context: toolExecutionContext) -> toolResult:
    command = arguments.get('command')
    if not isinstance(command, str) or not command.strip():
        return toolResult('', 'bash', True, 'bash.command 必须是非空字符串。')

    timeout = int(arguments.get('timeout', defaultTimeoutSeconds))
    if timeout < 1:
        timeout = defaultTimeoutSeconds
    if timeout > maxTimeoutSeconds:
        timeout = maxTimeoutSeconds

    if context.debugPrinter:
        context.debugPrinter.debug(f'执行 bash：{command}')

    try:
        completedProcess = subprocess.run(
            ['bash', '-lc', command],
            cwd=str(context.workDir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdoutPreview, stdoutTruncated = makePreview(completedProcess.stdout)
        stderrPreview, stderrTruncated = makePreview(completedProcess.stderr)
        isError = completedProcess.returncode != 0
        return toolResult(
            toolCallId='',
            toolName='bash',
            isError=isError,
            content=(
                f'exitCode: {completedProcess.returncode}\n'
                f'stdout:\n{stdoutPreview}\n'
                f'stderr:\n{stderrPreview}'
            ),
            details={
                'command': command,
                'timeout': timeout,
                'exitCode': completedProcess.returncode,
                'stdoutPreview': stdoutPreview,
                'stderrPreview': stderrPreview,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
    except subprocess.TimeoutExpired as error:
        stdoutText = error.stdout if isinstance(error.stdout, str) else (error.stdout or b'').decode('utf-8', errors='replace')
        stderrText = error.stderr if isinstance(error.stderr, str) else (error.stderr or b'').decode('utf-8', errors='replace')
        stdoutPreview, stdoutTruncated = makePreview(stdoutText)
        stderrPreview, stderrTruncated = makePreview(stderrText)
        return toolResult(
            toolCallId='',
            toolName='bash',
            isError=True,
            content=(
                f'命令超时，已终止。timeout: {timeout}\n'
                f'stdout:\n{stdoutPreview}\n'
                f'stderr:\n{stderrPreview}'
            ),
            details={
                'command': command,
                'timeout': timeout,
                'timeoutExpired': True,
                'stdoutPreview': stdoutPreview,
                'stderrPreview': stderrPreview,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
