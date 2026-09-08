'''
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: Node 断言覆盖 imageInput 的扩展名识别与草稿 payload 形状（imageInputPlan P3）。
'''

from __future__ import annotations

import subprocess
from pathlib import Path


rootPath = Path(__file__).resolve().parents[1]


def testImagePathHelper() -> None:
    script = r'''
const assert = require('assert');
global.window = global;
global.appStore = { currentSessionId: null, findSession: function () { return null; } };
require(process.argv[1]);
const api = global.imageInput;
assert.strictEqual(api.isImagePath('a.png'), true);
assert.strictEqual(api.isImagePath('a.JPG'), true);
assert.strictEqual(api.isImagePath('a.webp'), true);
assert.strictEqual(api.isImagePath('a.gif'), false);
assert.strictEqual(api.isImagePath('note.txt'), false);
assert.deepStrictEqual(api.payload(), []);
assert.strictEqual(api.hasDrafts(), false);
'''
    result = subprocess.run(
        ['node', '-e', script, str((rootPath / 'webApp/frontend/js/imageInput.js').resolve())],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=str(rootPath),
    )
    assert result.returncode == 0, result.stdout + result.stderr
