'''
Author: wilbur
Version: 1.0
Date: 2026-09-21
Description: Node 覆盖用量页：六选项下拉、单请求、stale 响应防护、空/错态、质量提示、无会话明细、无 Chart.js。
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


def testUsagePageHasSinglePeriodSelectAndNoSessions() -> None:
    index = (rootPath / 'webApp/frontend/index.html').read_text(encoding='utf-8')
    api = (rootPath / 'webApp/frontend/js/api.js').read_text(encoding='utf-8')
    view = (rootPath / 'webApp/frontend/js/usageView.js').read_text(encoding='utf-8')
    assert 'id="periodSelect"' in index
    assert 'value="today"' in index
    assert 'value="yesterday"' in index
    assert 'value="last7Days"' in index
    assert 'value="last30Days"' in index
    assert 'value="thisMonth"' in index
    assert 'value="lastMonth"' in index
    assert 'usageTableBody' not in index
    assert 'granularitySwitch' not in index
    assert 'chart.umd.min.js' not in index
    assert 'getUsageSeries' not in api
    assert '/api/usage/series' not in api
    assert 'AbortController' in view
    assert 'requestGeneration' in view
    assert '当前区间暂无已落账用量' in view
    assert 'sessionId' not in view


def testUsageViewStaleResponseAndSingleRequest() -> None:
    script = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('webApp/frontend/index.html', 'utf8');
const ids = [
  'usagePrompt', 'usageCached', 'usageCompletion', 'usageCost',
  'usageRangeText', 'usageTimeZone', 'usageCallCount',
  'usageQualityNotice', 'usageStatus', 'providerList', 'periodSelect'
];
function fakeNode(tagName, className, text) {
  const el = {
    tagName: tagName,
    className: className || '',
    textContent: text || '',
    children: [],
    style: {},
    appendChild: function (child) {
      el.children.push(child);
      el.textContent = (el.textContent || '') + (child && child.textContent ? child.textContent : '');
      return child;
    },
    replaceChildren: function () { el.children = []; el.textContent = ''; },
    setAttribute: function () {},
    addEventListener: function () {}
  };
  return el;
}
const document = {
  body: { appendChild: function () {} },
  getElementById: function (id) { return this[id]; },
  createElement: function (tagName) { return fakeNode(tagName); },
  createTextNode: function (text) { return { textContent: text }; }
};
ids.forEach(function (id) {
  const match = html.match(new RegExp('id="' + id + '"[^>]*>'));
  assert.ok(match, id);
  const el = {
    id: id,
    textContent: '',
    className: '',
    classList: {
      add: function (name) { el.className += ' ' + name; },
      remove: function (name) { el.className = el.className.replace(name, ''); },
      toggle: function (name, force) {
        if (force) el.classList.add(name); else el.classList.remove(name);
      }
    },
    style: {},
    children: [],
    replaceChildren: function () { el.children = []; el.innerHTML = ''; el.textContent = ''; },
    appendChild: function (child) {
      el.children.push(child);
      el.textContent = (el.textContent || '') + (child && child.textContent ? child.textContent : '');
    },
    addEventListener: function (type, handler) { el['on' + type] = handler; },
    querySelectorAll: function () { return []; },
    getBoundingClientRect: function () { return { width: 800 }; },
    parentElement: { clientWidth: 800 },
    setAttribute: function () {},
    getContext: function () {
      return {
        setTransform: function () {},
        clearRect: function () {},
        fillRect: function () {},
        beginPath: function () {},
        moveTo: function () {},
        lineTo: function () {},
        stroke: function () {},
        fillText: function () {}
      };
    }
  };
  if (id === 'periodSelect') {
    el.value = 'last7Days';
    el.options = [
      { value: 'today' }, { value: 'yesterday' }, { value: 'last7Days' },
      { value: 'last30Days' }, { value: 'thisMonth' }, { value: 'lastMonth' }
    ];
  }
  document[id] = el;
});

const calls = [];
const deferred = [];
function AbortController() {
  const ctrl = { aborted: false, signal: { aborted: false } };
  ctrl.abort = function () { ctrl.aborted = true; ctrl.signal.aborted = true; };
  return ctrl;
}
const sandbox = {
  document: document,
  window: {
    addEventListener: function () {},
    api: {
      getUsage: function (period, options) {
        calls.push(period);
        return new Promise(function (resolve, reject) {
          deferred.push({ period: period, resolve: resolve, reject: reject, signal: options && options.signal });
        });
      }
    },
    AbortController: AbortController,
    devicePixelRatio: 1
  },
  AbortController: AbortController,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  Math: Math,
  Number: Number,
  String: String,
  parseInt: parseInt,
  getComputedStyle: function () { return { height: '300px' }; }
};
sandbox.window.document = document;
sandbox.global = sandbox;
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('webApp/frontend/js/usageView.js', 'utf8'), sandbox);

function flush() {
  return new Promise(function (resolve) { setImmediate(resolve); });
}

(async function () {
  assert.strictEqual(typeof sandbox.window.usageView.open, 'function');
  sandbox.window.usageView.open();
  assert.deepStrictEqual(calls, ['last7Days']);
  document.periodSelect.value = 'today';
  document.periodSelect.onchange();
  assert.deepStrictEqual(calls, ['last7Days', 'today']);

  deferred[0].resolve({
    period: 'last7Days',
    timeZone: 'Asia/Shanghai',
    startAt: 'old',
    endAt: 'old',
    totals: { promptTokens: 1, cachedTokens: 0, completionTokens: 0, callCount: 1, costNanoUsd: 0, costStatus: 'complete' },
    providers: [{ providerId: 'old', totals: { promptTokens: 1, cachedTokens: 0, completionTokens: 0, totalTokens: 1, callCount: 1, costNanoUsd: 0, costStatus: 'complete' }, models: [{ modelId: 'm', totals: { promptTokens: 1, cachedTokens: 0, completionTokens: 0, totalTokens: 1, callCount: 1, costNanoUsd: 0, costStatus: 'complete' } }], buckets: [] }],
    quality: {}
  });
  deferred[1].resolve({
    period: 'today',
    timeZone: 'Asia/Shanghai',
    startAt: '2026-09-21T00:00:00+08:00',
    endAt: '2026-09-21T08:00:00+08:00',
    totals: { promptTokens: 80, cachedTokens: 10, completionTokens: 20, callCount: 2, costNanoUsd: 0, costStatus: 'complete' },
    providers: [],
    quality: { legacyInferredRecords: 0, migratedPriceRecords: 0, unknownPriceRecords: 0 }
  });
  for (var i = 0; i < 10 && document.usagePrompt.textContent === '-'; i += 1) {
    await flush();
  }
  assert.strictEqual(document.usagePrompt.textContent, '80');
  assert.strictEqual(document.usageCached.textContent, '10');
  assert.strictEqual(document.usageCompletion.textContent, '20');
  assert.ok(String(document.usageStatus.textContent).includes('暂无已落账用量'));
  console.log('usageView stale: ok');
})().catch(function (error) {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
'''
    runNode(script)
