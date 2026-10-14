'''
Author: wilbur
Version: 2.3
Date: 2026-10-13
Description: latencyUiFixPlan P1 前端 Node 单测——完整 vm 加载 chatView.js + 真实 statusUsage.js，覆盖：
             ①工具卡 Elapsed 回归折叠详情（默认隐藏、展开可见、不在卡片 root）；
             ②模型耗时一次性归属（纯工具轮 usageUpdate 不覆盖文字块脚标）；
             ③边界（空白 delta / 无 timings 帧 / toolCallStart 清 dirty / retryNotice 复燃 / confirm 恢复不误画）；v2.2 同步 styles cache-bust 到 1.34。
             ④历史回放（纯工具 assistant 无模型脚标；孤儿 toolCallEnd 兜底卡显示 elapsed）；
             ⑤formatSpeedLine 两形态（TTFT/Output/TPS 与 TTFT—/Total/TPS—）。
             v2.0 替换 v1.0 的 extractFunction+eval 截取与恒真断言（复审 M3）。
             v2.1：补充多 step 助手卡片均插入文字复制按钮的回归测试。
             v2.3（assistantCardCopyIdleFix）：真实 innerHTML 渲染下，completed 后的 goIdle flush 不得清掉 copy。
'''

from __future__ import annotations

import subprocess
from pathlib import Path


rootPath = Path(__file__).resolve().parents[1]


def runNode(script: str, extraArgs: list[str] | None = None) -> None:
    result = subprocess.run(
        ['node', '-e', script, *(extraArgs or [])],
        capture_output=True,
        text=True,
        timeout=45,
        cwd=str(rootPath),
    )
    assert result.returncode == 0, result.stdout + result.stderr


chatPath = str(rootPath / 'webApp/frontend/js/chatView.js')
usagePath = str(rootPath / 'webApp/frontend/js/statusUsage.js')


nodeHarness = r'''
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
function classListApi(el) {
  return {
    add: function () { for (const c of arguments) el._classSet.add(c); },
    remove: function () { for (const c of arguments) el._classSet.delete(c); },
    contains: function (c) { return el._classSet.has(c); },
    toggle: function (name, force) {
      if (force === undefined) {
        if (el._classSet.has(name)) { el._classSet.delete(name); return false; }
        el._classSet.add(name); return true;
      }
      if (force) el._classSet.add(name); else el._classSet.delete(name);
      return !!force;
    }
  };
}
function createEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    _classSet: new Set(),
    style: {},
    attributes: {},
    listeners: {},
    parentNode: null,
    _text: '',
    value: '',
    disabled: false,
    id: '',
    title: '',
    type: '',
    src: '',
    alt: '',
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 100,
    offsetHeight: 40,
    dataset: {},
    get className() { return [...el._classSet].join(' '); },
    set className(v) { el._classSet = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get classList() { return classListApi(el); },
    get textContent() { return el.children.length ? el.children.map(c => c.textContent).join('') : el._text; },
    set textContent(v) { el._text = String(v); el.children = []; },
    get innerHTML() { return el.children.map(c => c.textContent).join(''); },
    set innerHTML(v) { if (v === '') { el.children = []; el._text = ''; } },
    appendChild(child) { child.parentNode = el; el.children.push(child); return child; },
    removeChild(child) { el.children = el.children.filter(c => c !== child); child.parentNode = null; return child; },
    remove() { if (el.parentNode) el.parentNode.removeChild(el); },
    addEventListener(type, fn) { (el.listeners[type] = el.listeners[type] || []).push(fn); },
    click() { (el.listeners.click || []).forEach(fn => fn({ stopPropagation() {}, preventDefault() {} })); },
    focus() { el._focused = true; },
    querySelector(sel) { return find(el, sel); },
    querySelectorAll(sel) { return findAll(el, sel); },
    setAttribute(k, v) { el.attributes[k] = v; },
    removeAttribute(k) { delete el.attributes[k]; },
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0, left: 0, right: 0, width: 0 }; }
  };
  return el;
}
function matchSel(el, sel) {
  if (sel.startsWith('.')) return el._classSet.has(sel.slice(1).split(':')[0]);
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  return false;
}
function find(root, sel) {
  const plain = sel.split(':')[0];
  const wantNotHidden = sel.includes(':not(.hidden)');
  const stack = [root];
  while (stack.length) {
    const node = stack.shift();
    for (const child of node.children || []) {
      if (matchSel(child, plain) && (!wantNotHidden || !child._classSet.has('hidden'))) return child;
      stack.push(child);
    }
  }
  return null;
}
function findAll(root, sel) {
  const out = [];
  const stack = [root];
  while (stack.length) {
    const node = stack.shift();
    for (const child of node.children || []) {
      if (matchSel(child, sel)) out.push(child);
      stack.push(child);
    }
  }
  return out;
}
function makeDocument(ids) {
  const body = createEl('body');
  const chatCenter = createEl('div');
  chatCenter.className = 'chat-center';
  const composer = createEl('div');
  composer.className = 'composer';
  body.appendChild(chatCenter);
  body.appendChild(composer);
  if (ids.messageList) chatCenter.appendChild(ids.messageList);
  if (ids.chatEmpty) chatCenter.appendChild(ids.chatEmpty);
  const map = { ...ids };
  return {
    body,
    getElementById(id) { return map[id] || null; },
    createElement: createEl,
    querySelector(sel) {
      if (sel === '.chat-center') return chatCenter;
      if (sel === '.composer') return composer;
      if (sel === '.modal-mask:not(.hidden)') {
        const modal = map.confirmModal;
        if (modal && !modal._classSet.has('hidden')) return modal;
        return null;
      }
      return find(body, sel);
    },
    querySelectorAll(sel) {
      if (sel === '.chat-center') return [chatCenter];
      if (sel === '.composer') return [composer];
      return findAll(body, sel);
    },
    addEventListener() {},
    _map: map,
    _chatCenter: chatCenter
  };
}
function baseIds() {
  const ids = {};
  ['composerStatus','statusLocation','statusUsage','statusSpeed','messageList','chatEmpty','topbarTitle','topbarModel','errorBar','composerInput','sendButton','streamIndicator','confirmModal','confirmToolName','confirmReason','confirmPreviewRow','confirmPreview','confirmArgs','confirmApprove','confirmReject','chatPage','app'].forEach(id => {
    ids[id] = createEl(id === 'composerInput' ? 'textarea' : (id.indexOf('Button') >= 0 || id.indexOf('Approve') >= 0 || id.indexOf('Reject') >= 0 ? 'button' : 'div'));
    ids[id].id = id;
  });
  ids.confirmModal.classList.add('hidden');
  ids.chatEmpty.classList.add('hidden');
  return ids;
}
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
'''


def testChatViewToolElapsedAndTimingOwnership() -> None:
    script = nodeHarness + r'''
const ids = baseIds();
const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Number, Object, Array, String, Boolean, Date, Error,
  requestAnimationFrame: (cb) => setTimeout(cb, 0)
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = makeDocument(ids);
sandbox.appStore = { currentSessionId: 's1', stream: null, sessions: [], findSession() { return { title: 't', providerId: 'p', modelId: 'm' }; } };
const posts = [];
const applies = [];
sandbox.sse = {
  streamPost(path, body, onEvent) {
    const d = deferred();
    const rec = { path, body, onEvent, d, aborted: false };
    rec.abort = function () { rec.aborted = true; d.resolve('aborted'); };
    posts.push(rec);
    return { done: d.promise, abort: rec.abort };
  }
};
sandbox.api = {
  stopChat() { return Promise.resolve({ stopped: true }); },
  getMessages() { return Promise.resolve({ messages: [] }); },
  getPending() { return Promise.resolve({ pending: null }); },
  getStreamState() { return Promise.resolve({ state: 'idle' }); },
  getSessionStatus() { return Promise.resolve({}); }
};
sandbox.toast = function (message) {};
sandbox.statusBar = {
  applyUsageUpdate(data) { applies.push(data); },
  resetForSession() {}, hide() {},
  refresh() { return Promise.resolve(); }
};
sandbox.fileMention = { getAttachments() { return []; }, clearChips() {}, resetForSession() {} };
sandbox.fileExplorer = { open() {}, hide() {} };
sandbox.skillChip = { get() { return null; }, clear() {}, pin() {} };
sandbox.sidebarView = { refresh() { return Promise.resolve(); } };
sandbox.renderMarkdown = function (el, text) { el.textContent = text; };
sandbox.copyText = function () { return Promise.resolve(); };
vm.createContext(sandbox);
function load(path) { vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }); }
load(process.argv[2]); // 真实 statusUsage.js
load(process.argv[1]); // chatView.js
function flush() { return new Promise(r => setTimeout(r, 0)); }
async function flushN(n) { for (let i = 0; i < n; i++) await flush(); }
async function clickSend(text) {
  ids.composerInput.disabled = false;
  ids.sendButton.disabled = false;
  ids.composerInput.value = text;
  ids.sendButton.click();
  await flushN(6);
}
function findCardEl(toolCallId) {
  // 卡片根节点：messageList 内包含对应结果内容的 tool-card
  return sandbox.document.querySelectorAll('.tool-card').filter(c =>
    (c.textContent || '').includes(toolCallId)
  )[0] || null;
}
function visibleFootnotes() {
  return sandbox.document.querySelectorAll('.step-elapsed').filter(f => !f._classSet.has('hidden'));
}
function textStepFootnotes() {
  return sandbox.document.querySelectorAll('.step-elapsed');
}

(async function main() {
  await clickSend('hello');
  const stream = sandbox.appStore.stream;
  assert.ok(stream, 'stream started');
  const post = posts[0];

  // ---- ① v1.31 新契约：不再有模型级 Elapsed 脚标 ----
  post.onEvent('textDelta', { text: 'Let me check.' });
  await flushN(2);
  post.onEvent('usageUpdate', { usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 5}, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 5}, contextTokens: 15, cost: 0.1, timings: { ttfbMs: 1200, durationMs: 5300 } });
  await flushN(2);
  let footnotes = textStepFootnotes();
  assert.strictEqual(footnotes.length, 0, 'no model-level Elapsed footnotes anymore');

  // ---- ② 纯工具轮：脚标不被覆盖（核心回归） ----
  post.onEvent('toolCallStart', { toolCall: { id: 't1', toolName: 'bash', arguments: { command: 'ls' } }, preview: 'ls' });
  await flushN(2);
  post.onEvent('toolCallEnd', { toolResult: { toolCallId: 't1', toolName: 'bash', isError: false, content: 'file-a\nfile-b', details: { elapsedMs: 2300, callElapsedMs: 2900 } } });
  await flushN(2);
  post.onEvent('usageUpdate', { usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 5}, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 0}, contextTokens: 25, cost: 0.2, timings: { ttfbMs: 900, durationMs: 1800 } });
  await flushN(2);
  footnotes = textStepFootnotes();
  assert.strictEqual(footnotes.length, 0, 'no footnote from pure tool turn either');

  // 第二张卡：各自 elapsed 独立
  post.onEvent('toolCallStart', { toolCall: { id: 't2', toolName: 'bash', arguments: { command: 'pwd' } }, preview: 'pwd' });
  await flushN(2);
  post.onEvent('toolCallEnd', { toolResult: { toolCallId: 't2', toolName: 'bash', isError: false, content: '/home/wilburwan', details: { elapsedMs: 150, callElapsedMs: 550 } } });
  await flushN(2);
  post.onEvent('usageUpdate', { usage: {promptTokens: 30, cachedTokens: 0, completionTokens: 5}, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 0}, contextTokens: 35, cost: 0.3, timings: null });
  await flushN(2);
  footnotes = textStepFootnotes();
  assert.strictEqual(footnotes.length, 0, 'still no footnotes after timings=null frame');

  // 工具卡 elapsed：默认折叠在 detail 内，展开可见
  const cards = sandbox.document.querySelectorAll('.tool-card');
  assert.strictEqual(cards.length, 2, 'two tool cards');
  cards.forEach((cardEl, index) => {
    const elapsed = cardEl.querySelector('.tool-elapsed');
    assert.ok(elapsed, 'card ' + index + ' has elapsedEl');
    assert.ok(elapsed.parentNode.classList.contains('tool-card-detail'), 'elapsed inside detail');
    const detail = cardEl.querySelector('.tool-card-detail');
    assert.ok(detail.classList.contains('hidden'), 'detail collapsed by default');
    const header = cardEl.querySelector('.tool-card-header');
    header.click();
    assert.ok(!detail.classList.contains('hidden'), 'detail expanded after click');
    assert.ok(!elapsed.classList.contains('hidden'), 'elapsed visible when expanded');
    assert.ok(elapsed.textContent.indexOf('Tool execution ') === 0, 'card elapsed label: ' + elapsed.textContent);
    header.click();
    assert.ok(detail.classList.contains('hidden'), 'collapsed again');
  });
  const firstElapsed = cards[0].querySelector('.tool-elapsed');
  const secondElapsed = cards[1].querySelector('.tool-elapsed');
  assert.strictEqual(firstElapsed.textContent, 'Tool execution 2.3s');
  assert.strictEqual(secondElapsed.textContent, 'Tool execution 0.1s'); // 150ms → 0.1s（toFixed 浮点）
  // v1.31：卡片外部紧邻 Tool call 脚标（details.callElapsedMs）
  const outerCalls = sandbox.document.querySelectorAll('.tool-call-total');
  assert.strictEqual(outerCalls.length, 2, 'each card has one outer tool-call total');
  assert.strictEqual(outerCalls[0].textContent, 'Tool call 2.9s');
  assert.strictEqual(outerCalls[1].textContent, 'Tool call 0.6s'); // 550ms → 0.6s（toFixed 浮点）

  // ---- ③ 工具后新正文块：无模型级脚标（新契约） ----
  post.onEvent('textDelta', { text: 'Done. Two files found.' });
  await flushN(2);
  post.onEvent('usageUpdate', { usage: {promptTokens: 40, cachedTokens: 0, completionTokens: 9}, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 4}, contextTokens: 49, cost: 0.4, timings: { ttfbMs: 600, durationMs: 2100 } });
  await flushN(2);
  footnotes = textStepFootnotes();
  assert.strictEqual(footnotes.length, 0, 'no footnotes in new block either');

  // ---- ④ 最终 Total：completed 帧携带 ----
  post.onEvent('completed', { message: 'ok', totalElapsedMs: 9200 });
  await flushN(4);
  const totals = sandbox.document.querySelectorAll('.total-elapsed');
  assert.strictEqual(totals.length, 1, 'one Total on final answer');
  assert.strictEqual(totals[0].textContent, 'Total 9.2s');
  console.log('chatView timing ownership: ok');
  await flushN(4);
  console.log('chatView timing ownership: ok');
})().catch(err => { console.error(err); process.exit(1); });
'''
    runNode(script, [chatPath, usagePath])


def testChatViewMultiStepMessageCopy() -> None:
    script = nodeHarness + r'''
const ids = baseIds();
const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Number, Object, Array, String, Boolean, Date, Error,
  requestAnimationFrame: (cb) => setTimeout(cb, 0)
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = makeDocument(ids);
sandbox.appStore = { currentSessionId: 's1', stream: null, sessions: [], findSession() { return { title: 't', providerId: 'p', modelId: 'm' }; } };
const posts = [];
sandbox.sse = {
  streamPost(path, body, onEvent) {
    const d = deferred();
    const record = { path, body, onEvent, d };
    record.abort = function () { d.resolve('aborted'); };
    posts.push(record);
    return { done: d.promise, abort: record.abort };
  }
};
sandbox.api = {
  stopChat() { return Promise.resolve({ stopped: true }); },
  getMessages() { return Promise.resolve({ messages: [] }); },
  getPending() { return Promise.resolve({ pending: null }); },
  getStreamState() { return Promise.resolve({ state: 'idle' }); },
  getSessionStatus() { return Promise.resolve({}); }
};
sandbox.toast = function () {};
sandbox.statusBar = {
  applyUsageUpdate() {}, resetForSession() {}, hide() {},
  refresh() { return Promise.resolve(); },
  applyLiveTtfb() {}, clearLiveTtfb() {}
};
sandbox.fileMention = { getAttachments() { return []; }, clearChips() {}, resetForSession() {} };
sandbox.fileExplorer = { open() {}, hide() {} };
sandbox.skillChip = { get() { return null; }, clear() {}, resetForSession() {} };
sandbox.sidebarView = { refresh() { return Promise.resolve(); } };
sandbox.renderMarkdown = function (el, text) { el.textContent = text; };
sandbox.copyText = function () { return Promise.resolve(); };
vm.createContext(sandbox);
function load(path) { vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }); }
load(process.argv[2]); // 真实 statusUsage.js
load(process.argv[1]); // chatView.js
function flush() { return new Promise(r => setTimeout(r, 0)); }
async function flushN(n) { for (let i = 0; i < n; i++) await flush(); }

(async function main() {
  ids.composerInput.disabled = false;
  ids.sendButton.disabled = false;
  ids.composerInput.value = 'multi-step';
  ids.sendButton.click();
  await flushN(4);
  const post = posts[0];

  // 第一段正文后经过工具调用，第二段正文会落到新的助手卡片。
  post.onEvent('textDelta', { text: '第一段回答' });
  await flushN(2);
  post.onEvent('toolCallStart', { toolCall: { id: 'tool-1', toolName: 'bash', arguments: {} }, preview: 'echo' });
  await flushN(1);
  post.onEvent('toolCallEnd', { toolResult: { toolCallId: 'tool-1', toolName: 'bash', isError: false, content: 'ok', details: {} } });
  await flushN(1);
  post.onEvent('textDelta', { text: '第二段回答' });
  await flushN(2);
  post.onEvent('completed', { totalElapsedMs: 10 });
  await flushN(4);

  const rows = sandbox.document.querySelectorAll('.msg-assistant');
  const buttons = sandbox.document.querySelectorAll('.msg-copy-btn');
  assert.strictEqual(rows.length, 2, 'tool round creates two assistant cards');
  assert.strictEqual(buttons.length, 2, 'every assistant card has a copy button');
  assert.deepStrictEqual(buttons.map(button => button._rawMarkdown), ['第一段回答', '第二段回答']);
  assert.strictEqual(buttons[0].textContent, 'copy', 'assistant copy button uses copy label');
  assert.strictEqual(buttons[0].parentNode.className, 'markdown-content', 'copy button belongs to response content');

  // 复现真实 renderMarkdown：终态后 goIdle 的 flush 会 innerHTML 覆盖，不能清掉已挂上的 copy。
  sandbox.renderMarkdown = function (el, text) { el.innerHTML = '<p>' + text + '</p>'; };
  ids.composerInput.value = 'idle-flush';
  ids.sendButton.click();
  await flushN(4);
  const idlePost = posts[1];
  idlePost.onEvent('textDelta', { text: '回答结束' });
  await flushN(2);
  idlePost.onEvent('completed', { totalElapsedMs: 12 });
  await flushN(6);
  const idleButtons = sandbox.document.querySelectorAll('.msg-copy-btn');
  assert.strictEqual(idleButtons.length, 3, 'completed idle flush keeps the new copy button');
  assert.strictEqual(idleButtons[2]._rawMarkdown, '回答结束');
  console.log('multi-step assistant copy: ok');
})().catch(error => { console.error(error); process.exit(1); });
'''
    runNode(script, [chatPath, usagePath])


def testChatViewDirtyBoundariesAndHistory() -> None:
    script = nodeHarness + r'''
const ids = baseIds();
const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Number, Object, Array, String, Boolean, Date, Error,
  requestAnimationFrame: (cb) => setTimeout(cb, 0)
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = makeDocument(ids);
sandbox.appStore = { currentSessionId: 's1', stream: null, sessions: [], findSession() { return { title: 't', providerId: 'p', modelId: 'm' }; } };
const posts = [];
sandbox.sse = {
  streamPost(path, body, onEvent) {
    const d = deferred();
    const rec = { path, body, onEvent, d, aborted: false };
    rec.abort = function () { rec.aborted = true; d.resolve('aborted'); };
    posts.push(rec);
    return { done: d.promise, abort: rec.abort };
  }
};
sandbox.api = {
  stopChat() { return Promise.resolve({ stopped: true }); },
  getMessages() { return Promise.resolve({ messages: [] }); },
  getPending() { return Promise.resolve({ pending: null }); },
  getStreamState() { return Promise.resolve({ state: 'idle' }); },
  getSessionStatus() { return Promise.resolve({}); }
};
sandbox.toast = function (message) {};
sandbox.statusBar = { applyUsageUpdate() {}, resetForSession() {}, hide() {}, refresh() { return Promise.resolve(); } };
sandbox.fileMention = { getAttachments() { return []; }, clearChips() {}, resetForSession() {} };
sandbox.fileExplorer = { open() {}, hide() {} };
sandbox.skillChip = { get() { return null; }, clear() {}, pin() {} };
sandbox.sidebarView = { refresh() { return Promise.resolve(); } };
sandbox.renderMarkdown = function (el, text) { el.textContent = text; };
sandbox.copyText = function () { return Promise.resolve(); };
vm.createContext(sandbox);
function load(path) { vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }); }
load(process.argv[2]);
load(process.argv[1]);
function flush() { return new Promise(r => setTimeout(r, 0)); }
function visibleFootnotes() { // 局部副本（本脚本内 sandbox 变量独立）
  return sandbox.document.querySelectorAll('.step-elapsed').filter(f => !f._classSet.has('hidden'));
}
async function flushN(n) { for (let i = 0; i < n; i++) await flush(); }
async function clickSend(text) {
  ids.composerInput.disabled = false;
  ids.sendButton.disabled = false;
  ids.composerInput.value = text;
  ids.sendButton.click();
  await flushN(6);
}

(async function main() {
  await clickSend('edge');
  const post = posts[0];

  // 空白 delta 不置 dirty：timings 帧到达也不画
  post.onEvent('textDelta', { text: '' });
  await flushN(2);
  post.onEvent('usageUpdate', { usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 0}, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 0}, contextTokens: 10, cost: 0, timings: { ttfbMs: 100, durationMs: 400 } });
  await flushN(2);
  assert.strictEqual(visibleFootnotes().length, 0, 'blank delta does not paint');

  // 无 timings 的 usageUpdate 也消费 dirty：后续 toolCallStart 边界已清，纯工具轮 timings 不归旧块
  post.onEvent('textDelta', { text: 'text one' });
  await flushN(2);
  post.onEvent('usageUpdate', { usage: {promptTokens: 12, cachedTokens: 0, completionTokens: 2}, stepUsage: {promptTokens: 2, cachedTokens: 0, completionTokens: 2}, contextTokens: 14, cost: 0, timings: null });
  await flushN(2);
  assert.strictEqual(visibleFootnotes().length, 0, 'null timings does not paint');

  // dirty 已被消费：toolCallStart 后纯工具轮 timings 不归此块
  post.onEvent('toolCallStart', { toolCall: { id: 'e1', toolName: 'bash', arguments: { command: 'ls' } }, preview: 'ls' });
  await flushN(2);
  post.onEvent('usageUpdate', { usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 2}, stepUsage: {promptTokens: 8, cachedTokens: 0, completionTokens: 0}, contextTokens: 22, cost: 0, timings: { ttfbMs: 50, durationMs: 500 } });
  await flushN(2);
  assert.strictEqual(visibleFootnotes().length, 0, 'pure tool timing not painted after boundary');

  // retryNotice 后新 delta：仍无模型级脚标（新契约）
  post.onEvent('retryNotice', { message: 'err', attempt: 1, retryAfterMs: 10, status: 'waiting' });
  await flushN(2);
  post.onEvent('textDelta', { text: 'after retry' });
  await flushN(2);
  post.onEvent('usageUpdate', { usage: {promptTokens: 30, cachedTokens: 0, completionTokens: 4}, stepUsage: {promptTokens: 2, cachedTokens: 0, completionTokens: 2}, contextTokens: 34, cost: 0, timings: { ttfbMs: 200, durationMs: 1500 } });
  await flushN(2);
  assert.strictEqual(visibleFootnotes().length, 0, 'no model-level footnotes after retry');

  // 孤儿 toolCallEnd（无 Start）兜底卡显示执行耗时与外部脚标（无 callElapsedMs 不画外部）
  post.onEvent('toolCallEnd', { toolResult: { toolCallId: 'e2', toolName: 'bash', isError: false, content: 'orphan', details: { elapsedMs: 77 } } });
  await flushN(2);
  const orphanElapsed = sandbox.document.querySelectorAll('.tool-card')[1].querySelector('.tool-elapsed');
  assert.ok(orphanElapsed, 'orphan card has elapsedEl');
  assert.strictEqual(orphanElapsed.textContent, 'Tool execution 0.1s'); // 77ms → 0.1s（toFixed 浮点）
  assert.strictEqual(sandbox.document.querySelectorAll('.tool-call-total').length, 0, 'no callElapsedMs → no outer footnote');

  post.onEvent('completed', { message: 'ok' });
  await flushN(4);

  // ---- 历史回放：纯工具 assistant 无模型脚标；正文消息保留 ----
  sandbox.appStore.stream = null;
  const messages = [
    { kind: 'user', content: 'hi' },
    { kind: 'assistant', content: '', reasoning: '', toolCalls: [{ id: 'h1', toolName: 'bash', arguments: {} }], timings: { ttfbMs: 100, durationMs: 900 } },
    { kind: 'tool', toolCallId: 'h1', toolName: 'bash', isError: false, content: 'hist-out', details: { elapsedMs: 421 } },
    { kind: 'assistant', content: 'All done.', reasoning: 'think', toolCalls: [], timings: { ttfbMs: 300, durationMs: 6200, reasoningStartMs: 300, reasoningEndMs: 1700 } }
  ];
  sandbox.api.getMessages = () => Promise.resolve({ messages });
  sandbox.api.getPending = () => Promise.resolve({ pending: null });
  await sandbox.chatView.open('s1');
  await flushN(6);
  const histFootnotes = sandbox.document.querySelectorAll('.step-elapsed');
  assert.strictEqual(histFootnotes.length, 0, 'no model-level Elapsed footnotes in history (new contract)');
  const histThinking = sandbox.document.querySelectorAll('.thinking-elapsed').filter(f => !f._classSet.has('hidden'));
  assert.strictEqual(histThinking.length, 1, 'history thinking block keeps its own elapsed');
  const histCard = sandbox.document.querySelectorAll('.tool-card')[0];
  assert.strictEqual(histCard.querySelector('.tool-elapsed').textContent, 'Tool execution 0.4s');
  console.log('chatView dirty boundaries + history: ok');
})().catch(err => { console.error(err); process.exit(1); });
'''
    runNode(script, [chatPath, usagePath])


def testFormatSpeedLineTwoForms() -> None:
    script = r'''
const assert = require('assert');
require(process.argv[1]);
const h = global.statusUsage;

// formatElapsed 断言保留（v1.0 已验证）
assert.strictEqual(h.formatElapsed(0), '0.0s');
assert.strictEqual(h.formatElapsed(1500), '1.5s');
assert.strictEqual(h.formatElapsed(10000), '10s');
assert.strictEqual(h.formatElapsed(83000), '1m 23s');
assert.strictEqual(h.formatElapsed(null), null);
assert.strictEqual(h.formatElapsed(-5), null);

// readTimings 保留
assert.deepStrictEqual(h.readTimings({ttfbMs: 100, durationMs: 900}), {ttfbMs: 100, durationMs: 900, reasoningStartMs: null, reasoningEndMs: null});
assert.deepStrictEqual(h.readTimings({durationMs: 900}), {ttfbMs: null, durationMs: 900, reasoningStartMs: null, reasoningEndMs: null});
assert.deepStrictEqual(h.readTimings({ttfbMs: 100, durationMs: 900, reasoningStartMs: 100, reasoningEndMs: 800}), {ttfbMs: 100, durationMs: 900, reasoningStartMs: 100, reasoningEndMs: 800});

// formatSpeedLine v1.6（latencyDisplayImplementationPlan §3.1）：TTFT/TPS-only（已确认 demo）
assert.strictEqual(h.formatSpeedLine(null, null), null);
assert.strictEqual(h.formatSpeedLine({}, null), null);
assert.strictEqual(h.formatSpeedLine({durationMs: null}, null), null);
assert.strictEqual(h.formatSpeedLine({durationMs: 8400}, null), 'TTFT — · TPS —'); // 无 ttfb 仍显示（duration 有效）
// 无思考
assert.strictEqual(h.formatSpeedLine({ttfbMs: 1200, durationMs: 8400}, null), 'TTFT 1.2s · TPS —');
assert.strictEqual(h.formatSpeedLine({ttfbMs: 1200, durationMs: 9600}, {completionTokens: 402}), 'TTFT 1.2s · TPS 47.9 tok/s');
// 有思考：TPS 口径 v3——分母=duration−ttfb（含思考输出时段），分子 completionTokens 含思考 token（对称）
assert.strictEqual(
  h.formatSpeedLine({ttfbMs: 4386, durationMs: 52976, reasoningStartMs: 4386, reasoningEndMs: 45900}, {completionTokens: 860}),
  'TTFT 4.4s · TPS 17.7 tok/s'
);
assert.strictEqual(
  h.formatSpeedLine({durationMs: 52976, reasoningStartMs: 4386, reasoningEndMs: 45900}, null),
  'TTFT — · TPS —'
);
// D5 边界回退：end>duration / start>=end / 缺字段（回退无思考口径）
assert.strictEqual(h.formatSpeedLine({ttfbMs: 1200, durationMs: 8400, reasoningStartMs: 4000, reasoningEndMs: 9000}, null), 'TTFT 1.2s · TPS —');
assert.strictEqual(h.formatSpeedLine({ttfbMs: 1200, durationMs: 8400, reasoningStartMs: 5000, reasoningEndMs: 5000}, null), 'TTFT 1.2s · TPS —');
assert.strictEqual(h.formatSpeedLine({ttfbMs: 1200, durationMs: 8400, reasoningStartMs: 5000, reasoningEndMs: 6000}, null), 'TTFT 1.2s · TPS —');
assert.strictEqual(h.formatSpeedLine({ttfbMs: 1200, durationMs: 8400, reasoningEndMs: 6000}, null), 'TTFT 1.2s · TPS —');
// ttfb===duration / TTFT=0 / Output<100ms / tokens=0 → TPS —
assert.strictEqual(h.formatSpeedLine({ttfbMs: 3000, durationMs: 3000}, null), 'TTFT 3.0s · TPS —');
assert.strictEqual(h.formatSpeedLine({ttfbMs: 0, durationMs: 1500}, {completionTokens: 400}), 'TTFT 0.0s · TPS 267 tok/s');
assert.strictEqual(h.formatSpeedLine({ttfbMs: 800, durationMs: 850}, {completionTokens: 50}), 'TTFT 0.8s · TPS —');
assert.strictEqual(h.formatSpeedLine({ttfbMs: 800, durationMs: 5000}, {completionTokens: 0}), 'TTFT 0.8s · TPS —');
// ttfb 越界 → TTFT —
assert.strictEqual(h.formatSpeedLine({ttfbMs: 5000, durationMs: 3000}, null), 'TTFT — · TPS —');
// validLatencyPair：完整对子校验
assert.strictEqual(h.validLatencyPair({ttfbMs: 100, durationMs: 900}, {completionTokens: 5}), true);
assert.strictEqual(h.validLatencyPair({ttfbMs: 100}, {completionTokens: 5}), false);
assert.strictEqual(h.validLatencyPair({ttfbMs: 100, durationMs: 900}, {completionTokens: 0}), false);
assert.strictEqual(h.validLatencyPair({ttfbMs: 100, durationMs: 900}, null), false);

console.log('speed line: ok');
'''
    runNode(script, [usagePath])


def testStatusBarPairingAndCacheBust() -> None:
    script = nodeHarness + r'''
const ids = baseIds();
const sandbox = {
  console, setTimeout, clearTimeout, Promise, JSON, Math, Number, Object, Array, String, Boolean, Date, Error,
  requestAnimationFrame: (cb) => setTimeout(cb, 0)
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = makeDocument(ids);
sandbox.appStore = { currentSessionId: 's1', stream: null };
const statusCalls = [];
sandbox.api = {
  getSessionStatus(sessionId) {
    const d = deferred();
    statusCalls.push({ sessionId, d });
    return d.promise;
  }
};
vm.createContext(sandbox);
function load(path) { vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }); }
load(process.argv[2]); // 真实 statusUsage
load(process.argv[3]); // 真实 statusBar
function flush() { return new Promise(r => setTimeout(r, 0)); }
async function flushN(n) { for (let i = 0; i < n; i++) await flush(); }
function usagePayload(over) {
  return Object.assign({ usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 5}, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 5}, contextTokens: 15, cost: 0.1 }, over || {});
}

(async function main() {
  // 权威快照建立：status 带完整对子（lastTimings + lastStepUsage）
  const p = sandbox.statusBar.refresh();
  statusCalls[0].d.resolve({ workDir: '/w', gitBranch: 'dev', providerId: 'p', modelId: 'm', contextWindow: 200, usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 5}, contextTokens: 15, cost: 1, lastTimings: { ttfbMs: 1200, durationMs: 9600 }, lastStepUsage: {promptTokens: 0, cachedTokens: 0, completionTokens: 402} });
  await p; await flushN(2);
  // v1.11 新契约：TTFT/TPS 在用量行末尾（无独立速度行）
  const usageText = ids.statusUsage.textContent;
  assert.ok(usageText.indexOf('0.3% / 200') < usageText.indexOf('TTFT 1.2s'), 'TTFT after window segment');
  assert.ok(usageText.indexOf('TTFT 1.2s') < usageText.indexOf('TPS 47.9 tok/s'), 'TPS after TTFT');
  assert.ok(usageText.indexOf('Thinking') === -1 && usageText.indexOf('Output') === -1, 'no Thinking/Output');
  assert.ok(ids.statusUsage.title.indexOf('TTFT') >= 0, 'usage line has explanatory title');

  // 缺 timings 的被接受帧：配对保留（TPS 不闪 —）
  sandbox.statusBar.applyUsageUpdate(usagePayload({ usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 9}, timings: null, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 4} }));
  await flushN(2);
  assert.ok(ids.statusUsage.textContent.indexOf('TTFT 1.2s') >= 0, 'timings=null frame keeps previous pair');
  assert.ok(ids.statusUsage.textContent.indexOf('TPS 47.9 tok/s') >= 0, 'TPS not cleared');

  // 新完整对子替换
  sandbox.statusBar.applyUsageUpdate(usagePayload({ usage: {promptTokens: 30, cachedTokens: 0, completionTokens: 13}, timings: { ttfbMs: 600, durationMs: 3000 }, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 4} }));
  await flushN(2);
  assert.ok(ids.statusUsage.textContent.indexOf('TTFT 0.6s') >= 0, 'new pair replaces old');
  assert.ok(ids.statusUsage.textContent.indexOf('TPS 1.7 tok/s') >= 0, 'new TPS shown');

  // 权威 GET（无新流帧）：完整对子响应替换
  const p2 = sandbox.statusBar.refresh({ authoritative: true });
  statusCalls[1].d.resolve({ workDir: '/w', gitBranch: 'dev', providerId: 'p', modelId: 'm', contextWindow: 200, usage: {promptTokens: 30, cachedTokens: 0, completionTokens: 13}, contextTokens: 15, cost: 1, lastTimings: { ttfbMs: 800, durationMs: 4000 }, lastStepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 9} });
  await p2; await flushN(2);
  assert.ok(ids.statusUsage.textContent.indexOf('TTFT 0.8s') >= 0, 'authoritative GET with pair replaces');
  assert.ok(ids.statusUsage.textContent.indexOf('TPS 2.8 tok/s') >= 0, 'authoritative TPS shown');

  // cache-bust 同步（latencyDisplayImplementationPlan §3.3/P2.8）
  const fs = require('fs');
  const indexSource = fs.readFileSync(process.argv[4], 'utf8');
  assert.ok(indexSource.includes('href="/static/styles.css?v=1.34"'));
  assert.ok(indexSource.includes('src="/static/js/chatView.js?v=1.34"'));
  assert.ok(indexSource.includes('src="/static/js/statusBar.js?v=1.11"'));
  assert.ok(indexSource.includes('src="/static/js/statusUsage.js?v=1.7"'));
  const chatSource = fs.readFileSync(process.argv[1], 'utf8');
  assert.ok(chatSource.includes('Version: 1.34'));
  const usageSource = fs.readFileSync(process.argv[2], 'utf8');
  assert.ok(usageSource.includes('Version: 1.7'));
  const barSource = fs.readFileSync(process.argv[3], 'utf8');
  assert.ok(barSource.includes('Version: 1.11'));
  console.log('statusBar pairing + cache bust: ok');
})().catch(err => { console.error(err); process.exit(1); });
'''
    runNode(script, [
        chatPath,
        usagePath,
        str(rootPath / 'webApp/frontend/js/statusBar.js'),
        str(rootPath / 'webApp/frontend/index.html'),
    ])


def testThinkingBlockElapsedAndLiveTtfb() -> None:
    script = nodeHarness + r'''
const ids = baseIds();
const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Number, Object, Array, String, Boolean, Date, Error,
  requestAnimationFrame: (cb) => setTimeout(cb, 0)
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = makeDocument(ids);
sandbox.appStore = { currentSessionId: 's1', stream: null, sessions: [], findSession() { return { title: 't', providerId: 'p', modelId: 'm' }; } };
const posts = [];
const liveTtfbCalls = [];
const clearTtfbCalls = [];
sandbox.sse = {
  streamPost(path, body, onEvent) {
    const d = deferred();
    const rec = { path, body, onEvent, d, aborted: false };
    rec.abort = function () { rec.aborted = true; d.resolve('aborted'); };
    posts.push(rec);
    return { done: d.promise, abort: rec.abort };
  }
};
sandbox.api = {
  stopChat() { return Promise.resolve({ stopped: true }); },
  getMessages() { return Promise.resolve({ messages: [] }); },
  getPending() { return Promise.resolve({ pending: null }); },
  getStreamState() { return Promise.resolve({ state: 'idle' }); },
  getSessionStatus() { return Promise.resolve({}); }
};
sandbox.toast = function (message) {};
sandbox.statusBar = {
  applyUsageUpdate() {},
  resetForSession() {}, hide() {},
  refresh() { return Promise.resolve(); },
  applyLiveTtfb(ms) { liveTtfbCalls.push(ms); },
  clearLiveTtfb() { clearTtfbCalls.push(1); }
};
sandbox.fileMention = { getAttachments() { return []; }, clearChips() {}, resetForSession() {} };
sandbox.fileExplorer = { open() {}, hide() {} };
sandbox.skillChip = { get() { return null; }, clear() {}, pin() {} };
sandbox.sidebarView = { refresh() { return Promise.resolve(); } };
sandbox.renderMarkdown = function (el, text) { el.textContent = text; };
sandbox.copyText = function () { return Promise.resolve(); };
vm.createContext(sandbox);
function load(path) { vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }); }
load(process.argv[2]);
load(process.argv[1]);
function flush() { return new Promise(r => setTimeout(r, 0)); }
async function flushN(n) { for (let i = 0; i < n; i++) await flush(); }
async function clickSend(text) {
  ids.composerInput.disabled = false;
  ids.sendButton.disabled = false;
  ids.composerInput.value = text;
  ids.sendButton.click();
  await flushN(6);
}
function visibleThinking() {
  return sandbox.document.querySelectorAll('.thinking-elapsed').filter(f => !f._classSet.has('hidden'));
}

(async function main() {
  await clickSend('think flow');
  const post = posts[0];

  // ---- fake clock 从一开始就 patch（首个 delta 起计时一致）----
  const realNow = Date.now;
  let fakeTime = 1000000;
  sandbox.Date.now = () => fakeTime;

  // ---- live TTFT：首个 delta 即上报 ----
  post.onEvent('reasoningDelta', { text: '思考开始' });
  await flushN(2);
  assert.strictEqual(liveTtfbCalls.length, 1, 'first reasoning delta reports live TTFT immediately');

  // ---- 思考块耗时：本地计时（首个→最后 delta）----
  fakeTime = 1001400; // +1.4s（第二段思考增量时刻）
  post.onEvent('reasoningDelta', { text: ' more thinking' });
  await flushN(2);
  post.onEvent('textDelta', { text: '正文来了' }); // 触发折叠 → paint
  await flushN(2);
  const thinkings = visibleThinking();
  assert.strictEqual(thinkings.length, 1, 'thinking block painted once');
  assert.strictEqual(thinkings[0].textContent, 'Thinking 1.4s', 'thinking elapsed uses local timing');

  // ---- v1.31 新契约：无模型级 Elapsed 脚标；权威帧覆盖本地思考计时（1200→2600=1.4s 与本地一致）----
  post.onEvent('usageUpdate', { usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 5}, stepUsage: {promptTokens: 10, cachedTokens: 0, completionTokens: 5}, contextTokens: 15, cost: 0.1, timings: { ttfbMs: 1200, durationMs: 5300, reasoningStartMs: 1200, reasoningEndMs: 2600 } });
  await flushN(2);
  const footnotes = sandbox.document.querySelectorAll('.step-elapsed').filter(f => !f._classSet.has('hidden'));
  assert.strictEqual(footnotes.length, 0, 'no model-level Elapsed footnotes (new contract)');
  assert.strictEqual(visibleThinking()[0].textContent, 'Thinking 1.4s', 'authoritative timings keep thinking elapsed');

  // ---- 思考→工具→新 step：中间块不丢耗时（toolCallStart 收口）----
  post.onEvent('toolCallStart', { toolCall: { id: 'tb1', toolName: 'bash', arguments: { command: 'ls' } }, preview: 'ls' });
  await flushN(2);
  post.onEvent('toolCallEnd', { toolResult: { toolCallId: 'tb1', toolName: 'bash', isError: false, content: 'out', details: { elapsedMs: 100 } } });
  await flushN(2);
  // 新 step 新思考（sawToolEnd → newStep on reasoningDelta）
  sandbox.Date.now = () => 2000000;
  post.onEvent('reasoningDelta', { text: '第二段思考' });
  await flushN(2);
  sandbox.Date.now = () => 2000800; // +0.8s（最后思考增量）
  post.onEvent('reasoningDelta', { text: '继续' });
  await flushN(2);
  post.onEvent('textDelta', { text: '收尾' });
  await flushN(2);
  sandbox.Date.now = realNow;
  const thinkings2 = visibleThinking();
  assert.strictEqual(thinkings2.length, 2, 'two thinking blocks, each with elapsed');
  assert.strictEqual(thinkings2[1].textContent, 'Thinking 0.8s', 'second thinking elapsed');

  post.onEvent('completed', { message: 'ok' });
  await flushN(4);
  assert.ok(clearTtfbCalls.length >= 1, 'live TTFT cleared on idle');
  console.log('thinking block elapsed + live ttfb: ok');
})().catch(err => { console.error(err); process.exit(1); });
'''
    runNode(script, [chatPath, usagePath])


def testThinkingElapsedHistoryReplay() -> None:
    script = nodeHarness + r'''
const ids = baseIds();
const sandbox = {
  console, setTimeout, clearTimeout, Promise, JSON, Math, Number, Object, Array, String, Boolean, Date, Error,
  requestAnimationFrame: (cb) => setTimeout(cb, 0)
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = makeDocument(ids);
sandbox.appStore = { currentSessionId: 's1', stream: null, sessions: [], findSession() { return { title: 't', providerId: 'p', modelId: 'm' }; } };
sandbox.sse = { streamPost() { return { done: Promise.resolve('closed'), abort() {} }; } };
sandbox.api = {
  stopChat() { return Promise.resolve({ stopped: true }); },
  getMessages() { return Promise.resolve({ messages: [] }); },
  getPending() { return Promise.resolve({ pending: null }); },
  getStreamState() { return Promise.resolve({ state: 'idle' }); },
  getSessionStatus() { return Promise.resolve({}); }
};
sandbox.toast = function (message) {};
sandbox.statusBar = { applyUsageUpdate() {}, resetForSession() {}, hide() {}, refresh() { return Promise.resolve(); }, applyLiveTtfb() {}, clearLiveTtfb() {} };
sandbox.fileMention = { getAttachments() { return []; }, clearChips() {}, resetForSession() {} };
sandbox.fileExplorer = { open() {}, hide() {} };
sandbox.skillChip = { get() { return null; }, clear() {}, pin() {} };
sandbox.sidebarView = { refresh() { return Promise.resolve(); } };
sandbox.renderMarkdown = function (el, text) { el.textContent = text; };
sandbox.copyText = function () { return Promise.resolve(); };
vm.createContext(sandbox);
function load(path) { vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }); }
load(process.argv[2]);
load(process.argv[1]);
function flush() { return new Promise(r => setTimeout(r, 0)); }
async function flushN(n) { for (let i = 0; i < n; i++) await flush(); }

(async function main() {
  // 历史回放：带思考打点的消息 → thinking 块下 Thinking；无打点旧消息 → 不显示
  const messages = [
    { kind: 'user', content: 'hi' },
    { kind: 'assistant', content: '答案', reasoning: '想了很久的内容', toolCalls: [], timings: { ttfbMs: 300, durationMs: 6200, reasoningStartMs: 300, reasoningEndMs: 1700 } },
    { kind: 'assistant', content: '旧回复', reasoning: '旧思考', toolCalls: [], timings: { ttfbMs: 100, durationMs: 900 } }
  ];
  sandbox.api.getMessages = () => Promise.resolve({ messages });
  await sandbox.chatView.open('s1');
  await flushN(6);
  const thinkings = sandbox.document.querySelectorAll('.thinking-elapsed').filter(f => !f._classSet.has('hidden'));
  assert.strictEqual(thinkings.length, 1, 'only replayed msg with reasoning window shows Thinking');
  assert.strictEqual(thinkings[0].textContent, 'Thinking 1.4s');
  const footnotes = sandbox.document.querySelectorAll('.step-elapsed').filter(f => !f._classSet.has('hidden'));
  assert.strictEqual(footnotes.length, 0, 'no model-level Elapsed footnotes in history (new contract)');
  console.log('thinking elapsed history replay: ok');
})().catch(err => { console.error(err); process.exit(1); });
'''
    runNode(script, [chatPath, usagePath])
