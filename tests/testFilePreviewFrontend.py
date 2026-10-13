'''
Author: wilbur
Version: 1.0
Date: 2026-10-13
Description: Node 断言覆盖工作区图片预览分流、图片 DOM、object URL 回收和失败保留当前预览。
'''

from __future__ import annotations

import subprocess
from pathlib import Path


rootPath = Path(__file__).resolve().parents[1]


def testFileExplorerImagePreview() -> None:
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
    this.classList = {
      names: new Set(),
      add: (name) => this.classList.names.add(name),
      remove: (name) => this.classList.names.delete(name),
      toggle: (name, force) => {
        const has = this.classList.names.has(name);
        const next = force === undefined ? !has : force;
        if (next) this.classList.names.add(name); else this.classList.names.delete(name);
        return next;
      },
      contains: (name) => this.classList.names.has(name),
    };
    this.textContent = '';
    this.title = '';
    this.alt = '';
    this.src = '';
    this.style = {};
    this.parentNode = null;
    this._innerHTML = '';
    Object.defineProperty(this, 'innerHTML', {
      get: () => this._innerHTML,
      set: (value) => { this._innerHTML = value; if (value === '') this.children = []; },
    });
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  addEventListener(name, fn) { this.listeners[name] = this.listeners[name] || []; this.listeners[name].push(fn); }
  querySelector() { return null; }
}

const ids = {};
function byId(id) {
  if (!ids[id]) ids[id] = new FakeEl('div', id);
  return ids[id];
}
const modalStack = [];
const calls = { content: [], image: [], unauthorized: 0 };
const revoked = [];
let imageResult = Promise.resolve({ kind: 'blob-a' });
let contentResult = Promise.resolve({ path: 'note.txt', content: 'hello' });
const context = {
  console, setTimeout, URL: {
    createObjectURL: (blob) => 'blob:' + blob.kind,
    revokeObjectURL: (url) => revoked.push(url),
  },
  localStorage: { getItem: () => '0', setItem() {} },
  document: {
    getElementById: byId,
    createElement: (tag) => new FakeEl(tag),
  },
  window: { innerWidth: 1400 },
};
context.window = context;
context.global = context;
context.window.appStore = {
  currentSessionId: 's1',
  pushModalClose: (fn) => modalStack.push(fn),
  removeModalClose: (fn) => {
    const index = modalStack.lastIndexOf(fn);
    if (index >= 0) modalStack.splice(index, 1);
  },
};
context.window.api = {
  fireUnauthorized: () => { calls.unauthorized += 1; },
  listFiles: async () => ({ entries: [
    { name: 'shot.PNG', type: 'file' },
    { name: 'note.txt', type: 'file' },
  ] }),
  getFileContent: async (sessionId, filePath) => { calls.content.push(filePath); return contentResult; },
  getFileImage: async (sessionId, filePath) => { calls.image.push(filePath); return imageResult; },
};
context.window.renderMarkdown = () => {};
context.window.toast = () => {};

vm.runInNewContext(fs.readFileSync(path.join('webApp', 'frontend', 'js', 'fileExplorer.js'), 'utf8'), context);
context.window.fileExplorer.open();

function wait() { return new Promise((resolve) => setTimeout(resolve, 20)); }
function row(name) {
  return byId('fileTree').children.find((child) => (child.children[1] || {}).textContent.includes(name));
}

wait().then(() => {
  const imageRow = row('shot.PNG');
  assert.ok(imageRow.children[1].textContent.startsWith('🖼 '));
  let resolveLate;
  imageResult = new Promise((resolve) => { resolveLate = resolve; });
  imageRow.listeners.click[0]();
  contentResult = Promise.resolve({ path: 'note.txt', content: 'hello' });
  row('note.txt').listeners.click[0]();
  return wait().then(() => {
    resolveLate({ kind: 'late-image' });
    return wait();
  });
}).then(() => {
  assert.deepStrictEqual(revoked, ['blob:late-image']);
  assert.ok(byId('filePreviewBody').children[0].className.includes('preview-code-wrap'));
  imageResult = Promise.resolve({ kind: 'blob-a' });
  row('shot.PNG').listeners.click[0]();
  return wait();
}).then(() => {
  assert.deepStrictEqual(calls.image, ['shot.PNG', 'shot.PNG']);
  assert.deepStrictEqual(calls.content, ['note.txt']);
  const body = byId('filePreviewBody');
  const image = body.children[0].children[0];
  assert.strictEqual(image.tagName, 'img');
  assert.strictEqual(image.src, 'blob:blob-a');
  assert.strictEqual(image.alt, 'shot.PNG');
  assert.ok(byId('filePreviewModeToggle').classList.contains('hidden'));
  assert.strictEqual(modalStack.length, 1);
  imageResult = Promise.resolve({ kind: 'blob-b' });
  row('shot.PNG').listeners.click[0]();
  return wait();
}).then(() => {
  assert.deepStrictEqual(revoked, ['blob:late-image', 'blob:blob-a']);
  assert.strictEqual(byId('filePreviewBody').children[0].children[0].src, 'blob:blob-b');
  assert.strictEqual(modalStack.length, 1);
  let rejectImage;
  imageResult = new Promise((resolve, reject) => { rejectImage = reject; });
  row('shot.PNG').listeners.click[0]();
  rejectImage(new Error('图片失败'));
  return wait();
}).then(() => {
  assert.strictEqual(byId('filePreviewBody').children[0].children[0].src, 'blob:blob-b');
  assert.deepStrictEqual(revoked, ['blob:late-image', 'blob:blob-a']);
  imageResult = Promise.reject(Object.assign(new Error('未认证或 token 已失效'), { status: 401 }));
  row('shot.PNG').listeners.click[0]();
  return wait();
}).then(() => {
  assert.strictEqual(byId('filePreviewBody').children[0].children[0].src, 'blob:blob-b');
  contentResult = Promise.resolve({ path: 'note.txt', content: 'hello' });
  row('note.txt').listeners.click[0]();
  return wait();
}).then(() => {
  assert.deepStrictEqual(calls.content, ['note.txt', 'note.txt']);
  assert.deepStrictEqual(revoked, ['blob:late-image', 'blob:blob-a', 'blob:blob-b']);
  assert.ok(byId('filePreviewBody').children[0].className.includes('preview-code-wrap'));
  imageResult = Promise.resolve({ kind: 'blob-c' });
  row('shot.PNG').listeners.click[0]();
  return wait();
}).then(() => {
  modalStack[0]();
  assert.strictEqual(modalStack.length, 0);
  assert.ok(revoked.includes('blob:blob-c'));
  console.log('file preview frontend: ok');
}).catch((error) => {
  console.error(error);
  process.exit(1);
});
'''
    result = subprocess.run(
        ['node', '-e', script],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=str(rootPath),
    )
    assert result.returncode == 0, result.stdout + result.stderr
