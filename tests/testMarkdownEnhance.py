'''
Author: wilbur
Version: 1.0
Date: 2026-09-15
Description: Node harness for markdown.js 终态增强（复制按钮 / mermaid 占位 / live 帧不增强）
             以及 chatView ↑ 召回不得挂进 renderHistory/attach（chatUxImprovePlan T11）。
'''

from __future__ import annotations

import subprocess
from pathlib import Path


rootPath = Path(__file__).resolve().parents[1]


def testMarkdownEnhanceCopyAndMermaidPlaceholder() -> None:
    result = subprocess.run(
        [
            'node',
            str(rootPath / 'tests/harness/markdownEnhance.js'),
            str(rootPath / 'webApp/frontend/js/markdown.js'),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        cwd=str(rootPath),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def testInputHistoryNotHookedToRenderOrAttach() -> None:
    source = (rootPath / 'webApp/frontend/js/chatView.js').read_text(encoding='utf-8')
    renderStart = source.index('function renderHistory(')
    renderEnd = source.index('function appendAssistantHistory(')
    renderBody = source[renderStart:renderEnd]
    assert 'rebuildInputHistory' not in renderBody
    attachStart = source.index('function initAttachedStream(')
    attachEnd = source.index('function autoResize(')
    attachBody = source[attachStart:attachEnd]
    assert 'rebuildInputHistory' not in attachBody
    reloadStart = source.index('async function reloadSession(')
    reloadEnd = source.index('function attachStream(')
    reloadBody = source[reloadStart:reloadEnd]
    assert 'rebuildInputHistory(sessionId, messages)' in reloadBody
    assert 'pushInputHistory(sessionId, displayText)' in source
    assert 'renderHistory' in renderBody
