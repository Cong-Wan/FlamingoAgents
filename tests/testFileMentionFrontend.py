'''
Author: wilbur
Version: 1.0
Date: 2026-09-07
Description: Node assert coverage for path-only @ chips: no 8-item cap, dedupe, no fileContent on pick.
'''

from __future__ import annotations

import subprocess
from pathlib import Path


rootPath = Path(__file__).resolve().parents[1]


def runNode(script: str) -> None:
    result = subprocess.run(
        ['node', '-e', script],
        capture_output=True,
        text=True,
        timeout=45,
        cwd=str(rootPath),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def testFileMentionSourceDropsCountCap() -> None:
    source = (rootPath / 'webApp/frontend/js/fileMention.js').read_text(encoding='utf-8')
    assert 'chips.length >= 8' not in source
    assert '附件最多 8 个' not in source
    assert '仅路径引用，不预读内容' in source
    chatView = (rootPath / 'webApp/frontend/js/chatView.js').read_text(encoding='utf-8')
    assert '仅路径引用，不预读内容' in chatView


def testFileMentionNineChipsAndDedupe() -> None:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');

class FakeEl {
  constructor(tag, id) {
    this.tagName = tag;
    this.id = id || '';
    this.children = [];
    this.listeners = {};
    this.className = '';
    this.textContent = '';
    this.title = '';
    this.value = '';
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.style = {};
    this.scrollTop = 0;
    this.parentNode = null;
    this._innerHTML = '';
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    Object.defineProperty(this, 'innerHTML', {
      get: () => this._innerHTML,
      set: (value) => {
        this._innerHTML = value;
        if (value === '') this.children = [];
      },
    });
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  insertBefore(child, ref) {
    child.parentNode = this;
    const index = this.children.indexOf(ref);
    if (index >= 0) this.children.splice(index, 0, child);
    else this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((item) => item !== child);
    child.parentNode = null;
    return child;
  }
  querySelector(sel) {
    if (sel === '.skill-chip') return null;
    if (sel === '.command-item.active') {
      return this.children.find((child) => (child.className || '').includes('command-item')) || null;
    }
    return null;
  }
  querySelectorAll(sel) {
    if (sel === '.attachment-chip') {
      return this.children.filter((child) => (child.className || '').includes('attachment-chip'));
    }
    return [];
  }
  addEventListener(name, fn) {
    this.listeners[name] = this.listeners[name] || [];
    this.listeners[name].push(fn);
  }
  focus() {}
  getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0 }; }
}

const composerInput = new FakeEl('textarea', 'composerInput');
const mentionPanel = new FakeEl('div', 'mentionPanel');
const attachmentChips = new FakeEl('div', 'attachmentChips');
const toasts = [];
let fileContentCalls = 0;
const context = {
  console,
  setTimeout,
  document: {
    getElementById(id) {
      if (id === 'composerInput') return composerInput;
      if (id === 'mentionPanel') return mentionPanel;
      if (id === 'attachmentChips') return attachmentChips;
      return null;
    },
    createElement(tag) { return new FakeEl(tag); },
  },
  window: {},
};
context.window = context;
context.global = context;
context.window.appStore = { currentSessionId: 's1' };
context.window.toast = (msg) => toasts.push(msg);
context.window.api = {
  listFiles: async () => ({
    entries: Array.from({ length: 9 }, (_, index) => ({ name: 'f' + index + '.txt', type: 'file', attachable: true })),
  }),
  getFileContent: async () => { fileContentCalls += 1; return { path: 'x', content: 'no' }; },
};

vm.runInNewContext(fs.readFileSync(path.join('webApp', 'frontend', 'js', 'fileMention.js'), 'utf8'), context);

function wait() { return new Promise((resolve) => setTimeout(resolve, 30)); }
async function pickAll() {
  for (let index = 0; index < 9; index += 1) {
    composerInput.value = '@';
    composerInput.selectionStart = 1;
    composerInput.selectionEnd = 1;
    composerInput.listeners.input[0]();
    await wait();
    const rows = mentionPanel.children.filter((child) => (child.className || '').includes('command-item'));
    assert.ok(rows.length >= 9, 'expected file rows, got ' + rows.length);
    const target = rows.find((row) => (row.children[0] && row.children[0].textContent || '').includes('f' + index + '.txt'));
    assert.ok(target, 'missing row f' + index);
    target.listeners.mousedown[0]({ preventDefault() {} });
  }
}

pickAll().then(() => {
  const attachments = context.window.fileMention.getAttachments();
  assert.strictEqual(attachments.length, 9);
  assert.strictEqual(fileContentCalls, 0);
  assert.strictEqual(attachments[0].path, 'f0.txt');
  assert.strictEqual(attachments[0].type, 'file');
  composerInput.value = '@';
  composerInput.selectionStart = 1;
  composerInput.listeners.input[0]();
  return wait();
}).then(() => {
  const rows = mentionPanel.children.filter((child) => (child.className || '').includes('command-item'));
  const first = rows.find((row) => (row.children[0] && row.children[0].textContent || '').includes('f0.txt'));
  first.listeners.mousedown[0]({ preventDefault() {} });
  assert.strictEqual(context.window.fileMention.getAttachments().length, 9);
  assert.strictEqual(toasts.length, 0);
  const chip = attachmentChips.children.find((child) => (child.className || '').includes('attachment-chip'));
  const label = chip && chip.children[0];
  assert.ok(label && label.title.includes('仅路径引用，不预读内容'));
  console.log('fileMention chips: ok');
}).catch((error) => {
  console.error(error);
  process.exit(1);
});
'''
    runNode(script)
