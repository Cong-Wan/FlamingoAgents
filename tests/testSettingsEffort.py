'''
Author: wilbur
Version: 1.0
Date: 2026-09-23
Description: 验证模型设置页加载与 xhigh 档位匹配的脚本版本，避免浏览器沿用旧版下拉选项。
'''

from pathlib import Path
import re


def testSettingsEffortScriptVersionMatchesOptions() -> None:
    script = Path('webApp/frontend/js/settingsView.js').read_text(encoding='utf-8')
    html = Path('webApp/frontend/index.html').read_text(encoding='utf-8')
    scriptVersion = re.search(r'Version: (\d+\.\d+)', script)
    assert scriptVersion is not None
    assert f'/static/js/settingsView.js?v={scriptVersion.group(1)}' in html
    effortPairs = re.search(r'var effortPairs = (\[.*?\]);', script)
    assert effortPairs is not None
    assert "['high', 'high'], ['xhigh', 'xhigh'], ['max', 'max']" in effortPairs.group(1)
