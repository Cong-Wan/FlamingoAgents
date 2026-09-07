'''
Author: wilbur
Version: 1.2
Date: 2026-09-07
Description: Node assert coverage for statusUsage helpers and statusBar/chatView deferred races (usageRevision, stop POST, latestBound, confirm reuse, attach identity). v1.2 R7 equal-usage pending revision, rootPath/nodeHarness camelCase.
'''

from __future__ import annotations

import json
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


def testStatusUsageHelpers() -> None:
    script = r'''
const assert = require('assert');
require(process.argv[1]);
const h = global.statusUsage;
assert.strictEqual(h.isValidUsage(null), false);
assert.strictEqual(h.isValidUsage({promptTokens:1, cachedTokens:0}), false);
assert.strictEqual(h.isValidUsage({promptTokens:1, cachedTokens:0, completionTokens:NaN}), false);
assert.strictEqual(h.isValidUsage({promptTokens:1, cachedTokens:0, completionTokens:-1}), false);
assert.strictEqual(h.isValidUsage({promptTokens:1, cachedTokens:0, completionTokens:Infinity}), false);
assert.ok(h.isValidUsage({promptTokens:1, cachedTokens:0, completionTokens:0}));
assert.deepStrictEqual(h.readUsage({}), {promptTokens:0, cachedTokens:0, completionTokens:0});
assert.deepStrictEqual(h.usageDisplayCounts({promptTokens:100, cachedTokens:60, completionTokens:20}), {prompt:40, completion:20, cached:60});
assert.strictEqual(h.contextUsedPercent(50, 200), 25);
assert.strictEqual(h.contextUsedPercent(25, 300), 8.3);
assert.strictEqual(h.contextUsedPercent(33, 400), 8.2);
assert.strictEqual(h.contextUsedPercent(1, 16), 6.2);
assert.strictEqual(h.contextUsedPercent(3, 16), 18.8);
assert.strictEqual(h.contextUsedPercent(5, 16), 31.2);
assert.strictEqual(h.contextUsedPercent(7, 16), 43.8);
assert.strictEqual(h.contextUsedPercent(999, 100), 100);
assert.strictEqual(h.contextUsedPercent(10, 0), null);
assert.strictEqual(h.contextUsedPercent(10, null), null);
assert.strictEqual(h.formatCost(0), '$-');
assert.strictEqual(h.formatCost(0.000286), '$0.0003');
assert.strictEqual(h.liveCost(undefined, 0.2), 0.2);
assert.strictEqual(h.liveCost(0.5, 0.2), 0.5);
assert.strictEqual(h.liveCost(0.5, null), 0.5);
assert.strictEqual(h.shouldAcceptUsage({promptTokens:100, cachedTokens:1, completionTokens:1}, {promptTokens:90, cachedTokens:1, completionTokens:1}), true);
assert.strictEqual(h.shouldAcceptUsage({promptTokens:90, cachedTokens:1, completionTokens:1}, {promptTokens:100, cachedTokens:1, completionTokens:1}), false);
const snap = {workDir:'/a', gitBranch:'dev', providerId:'p', modelId:'m', contextWindow:200, extra:1};
const out = {usage:{promptTokens:1, cachedTokens:0, completionTokens:0}};
h.copyMeta(out, snap);
assert.strictEqual(out.workDir, '/a');
assert.strictEqual(out.contextWindow, 200);
assert.strictEqual(out.extra, undefined);
assert.strictEqual(h.TOKEN_KEYS, undefined);
assert.strictEqual(h.usageEquals, undefined);
assert.strictEqual(h.emptyUsage, undefined);
console.log('statusUsage: ok');
'''
    runNode(script, [str(rootPath / 'webApp/frontend/js/statusUsage.js')])


def testContextPercentCrossCheck(tmp_path) -> None:
    samples = []
    windows = list(range(1, 81)) + [99, 100, 128, 200, 256, 300, 333, 400, 512, 1000, 1024, 272000]
    for window in windows:
        limit = min(window * 2, 400)
        for tokens in range(0, limit + 1):
            if window > 80 and tokens not in (0, 1, window // 4, window // 2, window - 1, window, window + 1, 33, 25, 75) and tokens % 7 != 0:
                continue
            raw = max(0.0, min(100.0, (tokens / window) * 100.0))
            samples.append([tokens, window, round(raw, 1)])
    samplePath = tmp_path / 'percentSamples.json'
    samplePath.write_text(json.dumps(samples), encoding='utf-8')
    script = r'''
const assert = require('assert');
const fs = require('fs');
require(process.argv[1]);
const h = global.statusUsage;
const samples = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
assert.ok(samples.length > 200, 'need a dense sample set');
for (const [tokens, windowSize, expected] of samples) {
  assert.strictEqual(
    h.contextUsedPercent(tokens, windowSize),
    expected,
    tokens + '/' + windowSize + ' expected ' + expected
  );
}
console.log('percent cross-check: ok ' + samples.length);
'''
    runNode(script, [str(rootPath / 'webApp/frontend/js/statusUsage.js'), str(samplePath)])


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
    addEventListener() {},
    _map: map,
    _chatCenter: chatCenter
  };
}
function baseIds() {
  const ids = {};
  ['composerStatus','statusLocation','statusUsage','messageList','chatEmpty','topbarTitle','topbarModel','errorBar','composerInput','sendButton','streamIndicator','confirmModal','confirmToolName','confirmReason','confirmPreviewRow','confirmPreview','confirmArgs','confirmApprove','confirmReject','chatPage','app'].forEach(id => {
    ids[id] = createEl(id === 'composerInput' ? 'textarea' : (id.indexOf('Button') >= 0 || id.indexOf('Approve') >= 0 || id.indexOf('Reject') >= 0 ? 'button' : 'div'));
    ids[id].id = id;
  });
  ids.confirmModal.classList.add('hidden');
  ids.chatEmpty.classList.add('hidden');
  return ids;
}
'''


def testStatusBarDeferredRaces() -> None:
    script = nodeHarness + r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const ids = baseIds();
const sandbox = {
  console, setTimeout, clearTimeout, Promise, JSON, Math, Number, Object, Array, String, Boolean, Date, Error,
  requestAnimationFrame: (cb) => setTimeout(cb, 0)
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = makeDocument(ids);
sandbox.appStore = { currentSessionId: 's1', stream: null };
let statusCalls = [];
sandbox.api = {
  getSessionStatus(sessionId) {
    const d = deferred();
    statusCalls.push({ sessionId, d, options: null });
    return d.promise;
  }
};
vm.createContext(sandbox);
function load(path) { vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }); }
load(process.argv[1]);
load(process.argv[2]);
function usageText() { return sandbox.document.getElementById('statusUsage').textContent; }
function flush() { return new Promise(r => setTimeout(r, 0)); }
async function flushN(n) { for (let i = 0; i < n; i++) await flush(); }
function statusPayload(overrides) {
  return Object.assign({
    workDir: '/w', gitBranch: 'dev', providerId: 'p', modelId: 'm', contextWindow: 200,
    usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
  }, overrides || {});
}

(async function main() {
sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
const pOpen = sandbox.statusBar.refresh();
assert.strictEqual(statusCalls.length, 1);
const pFallback = sandbox.statusBar.refresh();
assert.strictEqual(statusCalls.length, 1, 'non-authoritative single-flight');
assert.strictEqual(pOpen, pFallback);
statusCalls[0].d.resolve({
  workDir: '/w', gitBranch: 'dev', providerId: 'p', modelId: 'm', contextWindow: 200,
  usage: {promptTokens: 100, cachedTokens: 60, completionTokens: 20}, contextTokens: 50, cost: 1
});
await flushN(5);
assert.ok(usageText().includes('↑ 40'));
assert.ok(usageText().includes('↓ 20'));
assert.ok(usageText().includes('⚡ 60'));
assert.ok(usageText().includes('$1.0000'));
assert.ok(usageText().includes('25%'));

sandbox.statusBar.applyUsageUpdate({ usage: {promptTokens: 90, cachedTokens: 60, completionTokens: 20}, contextTokens: 1, cost: 9 });
assert.ok(usageText().includes('↑ 40'), 'regressing frame ignored');
sandbox.statusBar.applyUsageUpdate({ usage: {promptTokens: 100, cachedTokens: 60, completionTokens: 20}, contextTokens: 50, cost: 0.2 });
assert.ok(usageText().includes('$1.0000'), 'live cost does not decrease');

const beforeAuth = statusCalls.length;
const authOld = sandbox.statusBar.refresh({ authoritative: true });
assert.strictEqual(statusCalls.length, beforeAuth + 1);
sandbox.statusBar.applyUsageUpdate({ usage: {promptTokens: 200, cachedTokens: 80, completionTokens: 40}, contextTokens: 80, cost: 2 });
assert.ok(usageText().includes('↑ 120'));
statusCalls[statusCalls.length - 1].d.resolve({
  workDir: '/w', gitBranch: 'dev', providerId: 'p', modelId: 'm', contextWindow: 200,
  usage: {promptTokens: 100, cachedTokens: 60, completionTokens: 20}, contextTokens: 50, cost: 0.1
});
await flushN(5);
assert.ok(usageText().includes('↑ 120'), 'old authoritative GET cannot regress new SSE');
assert.ok(!usageText().includes('$0.1000'));

sandbox.statusBar.applyUsageUpdate({ usage: {promptTokens: 200, cachedTokens: 80, completionTokens: 40}, contextTokens: 80, cost: 2 });
const authDown = sandbox.statusBar.refresh({ authoritative: true });
statusCalls[statusCalls.length - 1].d.resolve({
  workDir: '/w', gitBranch: 'dev', providerId: 'p', modelId: 'm', contextWindow: 200,
  usage: {promptTokens: 200, cachedTokens: 80, completionTokens: 40}, contextTokens: 80, cost: 0.25
});
await flushN(5);
assert.ok(usageText().includes('$0.2500'), 'authoritative can lower cost when no new usage');

sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.applyUsageUpdate({ usage: {promptTokens: 300, cachedTokens: 10, completionTokens: 5}, contextTokens: 40, cost: 3 });
assert.strictEqual(statusCalls.length >= 1, true);
const pendingBefore = statusCalls[statusCalls.length - 1];
pendingBefore.d.resolve({
  workDir: '/w', gitBranch: 'dev', providerId: 'p', modelId: 'm', contextWindow: 300,
  usage: {promptTokens: 1, cachedTokens: 0, completionTokens: 0}, contextTokens: 1, cost: 0.01
});
await flushN(5);
assert.ok(usageText().includes('↑ 290'), 'no snapshot: request-before pending still merges');
assert.ok(usageText().includes('/w') || sandbox.document.getElementById('statusLocation').textContent.includes('/w'));

sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
const openGet = sandbox.statusBar.refresh();
const openCall = statusCalls[statusCalls.length - 1];
sandbox.statusBar.applyUsageUpdate({ usage: {promptTokens: 50, cachedTokens: 0, completionTokens: 5}, contextTokens: 10, cost: 0.4 });
openCall.d.resolve({
  workDir: '/z', gitBranch: 'main', providerId: 'p', modelId: 'm', contextWindow: 100,
  usage: {promptTokens: 1, cachedTokens: 0, completionTokens: 0}, contextTokens: 1, cost: 0.01
});
await flushN(5);
assert.ok(usageText().includes('↑ 50'), 'no snapshot: request-after pending merges');

sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
const stale = sandbox.statusBar.refresh();
const staleCall = statusCalls[statusCalls.length - 1];
sandbox.statusBar.resetForSession('s2');
sandbox.appStore.currentSessionId = 's2';
staleCall.d.resolve({
  workDir: '/old', gitBranch: 'x', providerId: 'p', modelId: 'm', contextWindow: 10,
  usage: {promptTokens: 9, cachedTokens: 0, completionTokens: 9}, contextTokens: 9, cost: 9
});
await flushN(5);
assert.ok(!usageText().includes('↑ 9'), 'reset drops late finally');

const nonAuth = sandbox.statusBar.refresh();
const nonAuthCall = statusCalls[statusCalls.length - 1];
sandbox.appStore.currentSessionId = 's2';
const authNew = sandbox.statusBar.refresh({ authoritative: true });
const authCall = statusCalls[statusCalls.length - 1];
authCall.d.resolve({
  workDir: '/n', gitBranch: 'n', providerId: 'p', modelId: 'm', contextWindow: 200,
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.05
});
await flushN(5);
nonAuthCall.d.resolve({
  workDir: '/n', gitBranch: 'n', providerId: 'p', modelId: 'm', contextWindow: 200,
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.9
});
await flushN(5);
assert.ok(usageText().includes('$0.0500'), 'earlier non-authoritative cannot raise calibrated cost');

// R1: ordinary refresh must reuse in-flight authoritative GET and allow cost to drop.
sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
const seed = sandbox.statusBar.refresh();
statusCalls[statusCalls.length - 1].d.resolve(statusPayload({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
}));
await flushN(5);
assert.ok(usageText().includes('$1.0000'));
assert.ok(usageText().includes('↑ 10'));
const r1Before = statusCalls.length;
const r1Auth = sandbox.statusBar.refresh({ authoritative: true });
assert.strictEqual(statusCalls.length, r1Before + 1);
const r1AuthCall = statusCalls[statusCalls.length - 1];
const r1Ordinary = sandbox.statusBar.refresh();
assert.strictEqual(statusCalls.length, r1Before + 1, 'ordinary refresh must not start a new GET');
assert.strictEqual(r1Ordinary, r1Auth);
r1AuthCall.d.resolve(statusPayload({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.2
}));
await flushN(5);
assert.ok(usageText().includes('$0.2000'), 'authoritative reuse must calibrate live $1 to $0.2');
assert.ok(!usageText().includes('$1.0000'));

// R1: no snapshot, authoritative in flight, new pending reuses promise and merges by revision.
sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
const r1bBefore = statusCalls.length;
const r1bAuth = sandbox.statusBar.refresh({ authoritative: true });
assert.strictEqual(statusCalls.length, r1bBefore + 1);
const r1bCall = statusCalls[statusCalls.length - 1];
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 2}, contextTokens: 22, cost: 0.5
});
assert.strictEqual(statusCalls.length, r1bBefore + 1, 'pending fallback must reuse authoritative GET');
r1bCall.d.resolve(statusPayload({
  usage: {promptTokens: 1, cachedTokens: 0, completionTokens: 0}, contextTokens: 1, cost: 0.1
}));
await flushN(5);
assert.ok(usageText().includes('↑ 20'));
assert.ok(usageText().includes('$0.5000'));

// R2: pending replace must keep known cost when later frame omits it.
sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
});
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 2}, contextTokens: 22, cost: null
});
statusCalls[statusCalls.length - 1].d.resolve(statusPayload({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.1
}));
await flushN(5);
assert.ok(usageText().includes('↑ 20'));
assert.ok(usageText().includes('$1.0000'), 'null cost on later pending must not drop known $1');
assert.ok(!usageText().includes('$0.1000'));

sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
});
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 2}
});
statusCalls[statusCalls.length - 1].d.resolve(statusPayload({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.1
}));
await flushN(5);
assert.ok(usageText().includes('$1.0000'), 'missing cost on later pending keeps known fee');
assert.ok(usageText().includes('↑ 20'));

sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
});
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 2}, contextTokens: 22, cost: 0.05
});
statusCalls[statusCalls.length - 1].d.resolve(statusPayload({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.1
}));
await flushN(5);
assert.ok(usageText().includes('$1.0000'), 'smaller pending cost stays monotonic');

sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
});
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 2}, cost: 2
});
statusCalls[statusCalls.length - 1].d.resolve(statusPayload({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 1, cost: 0.1
}));
await flushN(5);
assert.ok(usageText().includes('5.5%'), 'missing context keeps previous pending context');
assert.ok(!usageText().includes('0.5%'));

sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
});
const ignoredCalls = statusCalls.length;
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 9, cachedTokens: 0, completionTokens: 2}, contextTokens: 99, cost: 9
});
assert.strictEqual(statusCalls.length, ignoredCalls);
statusCalls[statusCalls.length - 1].d.resolve(statusPayload({
  usage: {promptTokens: 1, cachedTokens: 0, completionTokens: 0}, contextTokens: 1, cost: 0.1
}));
await flushN(5);
assert.ok(usageText().includes('↑ 10'));
assert.ok(!usageText().includes('↑ 9'));
assert.ok(usageText().includes('$1.0000'));

// snapshot path also keeps cost when incoming cost is null.
sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.refresh();
statusCalls[statusCalls.length - 1].d.resolve(statusPayload({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
}));
await flushN(5);
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 20, cachedTokens: 0, completionTokens: 2}, contextTokens: 22, cost: null
});
assert.ok(usageText().includes('$1.0000'));
assert.ok(usageText().includes('↑ 20'));

// request-before pending must not raise calibrated authoritative cost.
sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
});
const beforeCalibrate = statusCalls.length;
const authCalibrate = sandbox.statusBar.refresh({ authoritative: true });
assert.strictEqual(statusCalls.length, beforeCalibrate + 1);
statusCalls[statusCalls.length - 1].d.resolve(statusPayload({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.2
}));
await flushN(5);
assert.ok(usageText().includes('$0.2000'), 'old pending must not lift authoritative drop');
assert.ok(!usageText().includes('$1.0000'));

// request-after pending still merges.
sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
const afterAuth = sandbox.statusBar.refresh({ authoritative: true });
const afterAuthCall = statusCalls[statusCalls.length - 1];
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 30, cachedTokens: 0, completionTokens: 3}, contextTokens: 33, cost: 0.8
});
afterAuthCall.d.resolve(statusPayload({
  usage: {promptTokens: 1, cachedTokens: 0, completionTokens: 0}, contextTokens: 1, cost: 0.2
}));
await flushN(5);
assert.ok(usageText().includes('↑ 30'));
assert.ok(usageText().includes('$0.8000'));

// R7: equal-usage newer pending must still bump usageRevision so context/cost survive the old GET.
sandbox.statusBar.resetForSession('s1');
sandbox.appStore.currentSessionId = 's1';
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 1
});
const r7BeforeAuth = statusCalls.length;
const r7Auth = sandbox.statusBar.refresh({ authoritative: true });
assert.strictEqual(statusCalls.length, r7BeforeAuth + 1);
const r7AuthCall = statusCalls[statusCalls.length - 1];
sandbox.statusBar.applyUsageUpdate({
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 40, cost: 2
});
assert.strictEqual(statusCalls.length, r7BeforeAuth + 1, 'same-usage pending reuses in-flight GET');
r7AuthCall.d.resolve(statusPayload({
  contextWindow: 100,
  usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.2
}));
await flushN(5);
assert.ok(usageText().includes('$2.0000'), 'same-usage newer pending keeps cost 2');
assert.ok(usageText().includes('40%'), 'same-usage newer pending keeps context 40');
assert.ok(!usageText().includes('$0.2000'));
assert.ok(!usageText().includes('11%'));

console.log('statusBar races: ok');
})().catch(function (error) { console.error(error && error.stack ? error.stack : error); process.exit(1); });
'''
    runNode(script, [
        str(rootPath / 'webApp/frontend/js/statusUsage.js'),
        str(rootPath / 'webApp/frontend/js/statusBar.js'),
    ])


def testChatViewConnectionStopAndAttach() -> None:
    script = nodeHarness + r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const ids = baseIds();
const sandbox = {
  console, setTimeout, clearTimeout, Promise, JSON, Math, Number, Object, Array, String, Boolean, Date, Error,
  requestAnimationFrame: (cb) => setTimeout(cb, 0)
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = makeDocument(ids);
sandbox.appStore = { currentSessionId: null, stream: null, sessions: [], findSession() { return { title: 't', providerId: 'p', modelId: 'm' }; } };
const posts = [];
const refreshes = [];
const applies = [];
let stopDeferred = null;
let stopCalls = 0;
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
  stopChat(sessionId) {
    stopCalls += 1;
    stopDeferred = deferred();
    return stopDeferred.promise;
  },
  getMessages() { return Promise.resolve({ messages: [] }); },
  getPending() { return Promise.resolve({ pending: null }); },
  getSessionStatus() { return Promise.resolve({}); }
};
sandbox.statusBar = {
  applyUsageUpdate(data) { applies.push(data); },
  resetForSession(id) { this._session = id; },
  hide() {},
  refresh(options) { refreshes.push({ options: options || {}, session: sandbox.appStore.currentSessionId }); return Promise.resolve(); }
};
sandbox.fileMention = { getAttachments() { return []; }, clearChips() {}, resetForSession() {} };
sandbox.fileExplorer = { open() {}, hide() {} };
sandbox.skillChip = { get() { return null; }, clear() {}, pin() {} };
sandbox.sidebarView = { refresh() { return Promise.resolve(); } };
sandbox.renderMarkdown = function (el, text) { el.textContent = text; };
vm.createContext(sandbox);
function load(path) { vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }); }
load(process.argv[1]);
function flush() { return new Promise(r => setTimeout(r, 0)); }
async function flushN(n) { for (let i = 0; i < n; i++) await flush(); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function authCount() { return refreshes.filter(r => r.options && r.options.authoritative === true).length; }
function lastPost() { return posts[posts.length - 1]; }
function attachPosts() { return posts.filter(p => p.path === '/api/chat/attach'); }
function enableComposer() {
  ids.composerInput.disabled = false;
  ids.sendButton.disabled = false;
}
async function clickSend(text) {
  enableComposer();
  ids.composerInput.value = text;
  ids.sendButton.click();
  await flushN(5);
}

(async function main() {
sandbox.appStore.currentSessionId = 's1';
await clickSend('hello');
assert.strictEqual(posts.length, 1);
assert.strictEqual(posts[0].path, '/api/chat/stream');
const sendPost = posts[0];
assert.strictEqual(typeof sendPost.body.sessionId, 'string');
assert.ok(sandbox.appStore.stream);
assert.ok(sandbox.appStore.stream.connectionId);
const firstId = sandbox.appStore.stream.connectionId;
sendPost.onEvent('usageUpdate', { usage: {promptTokens: 10, cachedTokens: 0, completionTokens: 1}, contextTokens: 11, cost: 0.1 });
assert.strictEqual(applies.length, 1);
assert.strictEqual(sandbox.appStore.stream.phase, 'streaming');

const stoppingStream = sandbox.appStore.stream;
ids.sendButton.click();
await flushN(5);
assert.strictEqual(stopCalls, 1);
assert.ok(stoppingStream.stopRequest);
assert.strictEqual(sendPost.aborted, true);
assert.strictEqual(sandbox.appStore.stream, null, 'UI already idle after closed');
assert.strictEqual(authCount(), 0, 'no authoritative GET before stop POST settles');
stopDeferred.resolve({ stopped: true });
await flushN(5);
assert.strictEqual(authCount(), 1);
assert.strictEqual(refreshes[refreshes.length - 1].options.authoritative, true);

sandbox.appStore.currentSessionId = 's1';
await clickSend('next');
ids.sendButton.click();
await flushN(5);
const waitingStop = stopDeferred;
sandbox.chatView.close();
sandbox.appStore.currentSessionId = 'sB';
waitingStop.resolve({ stopped: true });
await flushN(5);
assert.strictEqual(refreshes.filter(r => r.session === 'sB' && r.options.authoritative).length, 0, 'late stop POST must not refresh new session');

sandbox.appStore.currentSessionId = 's1';
await clickSend('failstop');
ids.sendButton.click();
await flushN(5);
const beforeFail = authCount();
stopDeferred.reject(new Error('stop failed'));
await flushN(5);
assert.strictEqual(authCount(), beforeFail + 1, 'failed stop still best-effort GET');

sandbox.appStore.currentSessionId = 's1';
const naturalAuthBefore = authCount();
await clickSend('natural');
const natural = lastPost();
natural.onEvent('completed', { message: 'ok' });
await flushN(2);
natural.d.resolve('closed');
await flushN(5);
assert.strictEqual(authCount(), naturalAuthBefore + 1, 'natural completed refreshes exactly once');
assert.strictEqual(refreshes[refreshes.length - 1].options.authoritative, true);

// S1 ends without closed → S2 starts and ends → late S1 closed must not refresh.
sandbox.appStore.currentSessionId = 's1';
const s1AuthBefore = authCount();
await clickSend('s1-open');
const s1Post = lastPost();
const s1Conn = sandbox.appStore.stream.connectionId;
s1Post.onEvent('completed', { message: 'ok' });
await flushN(2);
assert.strictEqual(sandbox.appStore.stream, null);
await clickSend('s2-open');
const s2Post = lastPost();
const s2Conn = sandbox.appStore.stream.connectionId;
assert.notStrictEqual(s2Conn, s1Conn);
s2Post.onEvent('completed', { message: 'ok' });
await flushN(2);
s2Post.d.resolve('closed');
await flushN(5);
assert.strictEqual(authCount(), s1AuthBefore + 1, 'only S2 closed refreshes');
s1Post.d.resolve('closed');
await flushN(5);
assert.strictEqual(authCount(), s1AuthBefore + 1, 'late S1 closed must not refresh after S2');

// confirm: start new connection first, then old done/event/reject.
sandbox.appStore.currentSessionId = 's1';
await clickSend('confirm');
const confirmSend = lastPost();
const oldObject = sandbox.appStore.stream;
const oldConn = oldObject.connectionId;
confirmSend.onEvent('confirmationRequired', { confirmationId: 'c1', reason: 'r', commandPreview: 'p', toolCall: { id: 't1', toolName: 'rm', arguments: {} } });
await flushN(2);
assert.strictEqual(sandbox.appStore.stream.phase, 'waitingConfirm');
const appliesBeforeConfirm = applies.length;
const authBeforeConfirm = authCount();
ids.confirmApprove.click();
await flushN(5);
const confirmPost = lastPost();
assert.strictEqual(confirmPost.path, '/api/chat/confirm');
assert.strictEqual(sandbox.appStore.stream, oldObject);
assert.notStrictEqual(sandbox.appStore.stream.connectionId, oldConn);
const newConn = sandbox.appStore.stream.connectionId;
assert.strictEqual(sandbox.appStore.stream.phase, 'streaming');
confirmSend.d.resolve('closed');
await flushN(5);
assert.strictEqual(sandbox.appStore.stream, oldObject);
assert.strictEqual(sandbox.appStore.stream.connectionId, newConn);
assert.strictEqual(sandbox.appStore.stream.phase, 'streaming');
assert.strictEqual(applies.length, appliesBeforeConfirm);
assert.strictEqual(authCount(), authBeforeConfirm);
confirmSend.onEvent('usageUpdate', { usage: {promptTokens: 99, cachedTokens: 0, completionTokens: 9}, cost: 9 });
assert.strictEqual(applies.length, appliesBeforeConfirm, 'old confirm connection events ignored');
confirmPost.onEvent('textDelta', { text: 'ok' });
assert.strictEqual(sandbox.appStore.stream.phase, 'streaming');

sandbox.appStore.currentSessionId = 's1';
sandbox.chatView.close();
await clickSend('confirm-reject');
const confirmSend2 = lastPost();
const oldObject2 = sandbox.appStore.stream;
const oldConn2 = oldObject2.connectionId;
confirmSend2.onEvent('confirmationRequired', { confirmationId: 'c2', reason: 'r', commandPreview: 'p', toolCall: { id: 't2', toolName: 'rm', arguments: {} } });
await flushN(2);
ids.confirmApprove.click();
await flushN(5);
const confirmPost2 = lastPost();
const newConn2 = sandbox.appStore.stream.connectionId;
assert.notStrictEqual(newConn2, oldConn2);
const appliesBeforeReject = applies.length;
const authBeforeReject = authCount();
confirmSend2.d.reject(Object.assign(new Error('old cut'), { status: 500 }));
await flushN(5);
assert.strictEqual(sandbox.appStore.stream, oldObject2);
assert.strictEqual(sandbox.appStore.stream.connectionId, newConn2);
assert.strictEqual(sandbox.appStore.stream.phase, 'streaming');
assert.strictEqual(applies.length, appliesBeforeReject);
assert.strictEqual(authCount(), authBeforeReject);
assert.ok(!ids.errorBar.textContent.includes('old cut'));

// stop Promise completes after a new same-session stream started.
sandbox.chatView.close();
sandbox.appStore.currentSessionId = 's1';
await clickSend('stop-then-new');
const stopOld = lastPost();
const stopOldConn = sandbox.appStore.stream.connectionId;
ids.sendButton.click();
await flushN(5);
const delayedStop = stopDeferred;
assert.strictEqual(sandbox.appStore.stream, null);
await clickSend('after-stop');
const newAfterStop = sandbox.appStore.stream.connectionId;
assert.notStrictEqual(newAfterStop, stopOldConn);
const authBeforeLateStop = authCount();
delayedStop.resolve({ stopped: true });
await flushN(5);
assert.strictEqual(authCount(), authBeforeLateStop, 'old stop must not refresh the new stream');
assert.strictEqual(sandbox.appStore.stream.connectionId, newAfterStop);

// attach success: preInit buffer replayed with bound identity.
sandbox.chatView.close();
await sandbox.chatView.open('sAttach');
await flushN(5);
const attachOk = attachPosts().pop();
const attachStreamObj = sandbox.appStore.stream;
const attachConn = attachStreamObj.connectionId;
assert.strictEqual(attachStreamObj.phase, 'attaching');
const appliesBeforeAttach = applies.length;
attachOk.onEvent('usageUpdate', { usage: {promptTokens: 7, cachedTokens: 0, completionTokens: 1}, contextTokens: 8, cost: 0.07 });
assert.strictEqual(applies.length, appliesBeforeAttach, 'preInit events stay buffered');
attachOk.onEvent('streamResume', { baseCount: 0 });
await flushN(2);
assert.strictEqual(sandbox.appStore.stream, attachStreamObj);
assert.strictEqual(sandbox.appStore.stream.phase, 'streaming');
assert.strictEqual(sandbox.appStore.stream.connectionId, attachConn);
assert.strictEqual(applies.length, appliesBeforeAttach + 1);
assert.strictEqual(applies[applies.length - 1].usage.promptTokens, 7);

// uninitialized 404 is silent; old placeholder callbacks stay inert.
sandbox.chatView.close();
ids.errorBar.innerHTML = '';
ids.errorBar.classList.add('hidden');
await sandbox.chatView.open('s404');
await flushN(5);
const attach404 = attachPosts().pop();
const placeholder404 = sandbox.appStore.stream;
attach404.d.reject(Object.assign(new Error('none'), { status: 404 }));
await flushN(5);
assert.strictEqual(sandbox.appStore.stream, null);
assert.strictEqual(ids.errorBar.textContent, '');
const appliesBeforeLate404 = applies.length;
attach404.onEvent('streamResume', { baseCount: 0 });
attach404.onEvent('usageUpdate', { usage: {promptTokens: 4, cachedTokens: 0, completionTokens: 1}, cost: 0.4 });
assert.strictEqual(applies.length, appliesBeforeLate404);
assert.strictEqual(sandbox.appStore.stream, null);

// A→B→A: old still-pending attach callback must not touch the new connection.
await sandbox.chatView.open('sA');
await flushN(5);
const attachA = attachPosts().pop();
const connA = sandbox.appStore.stream.connectionId;
await sandbox.chatView.open('sB');
await flushN(5);
sandbox.chatView.showEmpty();
await sandbox.chatView.open('sA');
await flushN(5);
const attachA2 = attachPosts().pop();
const connA2 = sandbox.appStore.stream.connectionId;
assert.notStrictEqual(attachA2, attachA);
assert.notStrictEqual(connA2, connA);
const appliesBeforeABA = applies.length;
const authBeforeABA = authCount();
attachA.onEvent('streamResume', { baseCount: 0 });
attachA.onEvent('usageUpdate', { usage: {promptTokens: 1, cachedTokens: 0, completionTokens: 1}, cost: 1 });
attachA.d.resolve('closed');
await flushN(5);
assert.strictEqual(applies.length, appliesBeforeABA);
assert.strictEqual(authCount(), authBeforeABA);
assert.strictEqual(sandbox.appStore.currentSessionId, 'sA');
assert.strictEqual(sandbox.appStore.stream.connectionId, connA2);
assert.strictEqual(sandbox.appStore.stream.phase, 'attaching');

// send 409 meta retry still happens once.
sandbox.chatView.close();
sandbox.appStore.currentSessionId = 's1';
const streamPostsBefore409 = posts.filter(p => p.path === '/api/chat/stream').length;
await clickSend('conflict');
const conflictPost = lastPost();
assert.strictEqual(conflictPost.path, '/api/chat/stream');
conflictPost.d.reject(Object.assign(new Error('会话有活跃流'), { status: 409 }));
await flushN(5);
assert.strictEqual(sandbox.appStore.stream, null);
await sleep(700);
await flushN(5);
const streamPosts = posts.filter(p => p.path === '/api/chat/stream');
assert.strictEqual(streamPosts.length, streamPostsBefore409 + 2, '409 retries send exactly once');
assert.strictEqual(lastPost().path, '/api/chat/stream');
assert.ok(sandbox.appStore.stream);

// natural completed vs no-terminal disconnect independently counted.
lastPost().onEvent('completed', { message: 'ok' });
await flushN(2);
lastPost().d.resolve('closed');
await flushN(5);

sandbox.appStore.currentSessionId = 's1';
ids.errorBar.innerHTML = '';
ids.errorBar.classList.add('hidden');
const cutAuthBefore = authCount();
await clickSend('cut');
const cut = lastPost();
assert.strictEqual(cut.path, '/api/chat/stream');
assert.ok(sandbox.appStore.stream);
assert.strictEqual(sandbox.appStore.stream.phase, 'streaming');
cut.d.resolve('closed');
await flushN(5);
assert.ok(ids.errorBar.textContent.includes('连接中断'));
assert.strictEqual(authCount(), cutAuthBefore + 1, 'no-terminal disconnect still refreshes once');
assert.strictEqual(sandbox.appStore.stream, null);

console.log('chatView connection: ok');
})().catch(function (error) { console.error(error && error.stack ? error.stack : error); process.exit(1); });
'''
    runNode(script, [str(rootPath / 'webApp/frontend/js/chatView.js')])


def testIndexCacheBustOrder() -> None:
    index = (rootPath / 'webApp/frontend/index.html').read_text(encoding='utf-8')
    usagePos = index.find('src="/static/js/statusUsage.js?v=1.1"')
    barPos = index.find('src="/static/js/statusBar.js?v=1.6"')
    chatPos = index.find('src="/static/js/chatView.js?v=1.21"')
    stylesPos = index.find('href="/static/styles.css?v=1.19"')
    assert usagePos != -1 and barPos != -1 and chatPos != -1 and stylesPos != -1
    assert usagePos < barPos < chatPos
