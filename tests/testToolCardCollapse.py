'''
Author: wilbur
Version: 1.0
Date: 2026-09-04
Description: Verifies tool collapse behavior/styles and cache-busted loading of the updated frontend assets.
'''

from __future__ import annotations

import subprocess
from pathlib import Path


def testToolCardCollapseBehavior() -> None:
    chatViewPath = Path('webApp/frontend/js/chatView.js').resolve()
    stylesPath = Path('webApp/frontend/styles.css').resolve()
    indexPath = Path('webApp/frontend/index.html').resolve()
    nodeScript = r'''
const assert = require('assert');
const fs = require('fs');

const chatSource = fs.readFileSync(process.argv[1], 'utf8');
const stylesSource = fs.readFileSync(process.argv[2], 'utf8');
const indexSource = fs.readFileSync(process.argv[3], 'utf8');

function extractFunction(name, nextName) {
  const start = chatSource.indexOf('function ' + name + '(');
  const end = chatSource.indexOf('\n\n  function ' + nextName + '(', start);
  assert.ok(start >= 0 && end > start, 'Cannot extract ' + name);
  return chatSource.slice(start, end);
}

class FakeClassList {
  constructor(initial) {
    this.values = new Set(initial || []);
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  contains(value) {
    return this.values.has(value);
  }

  toggle(value) {
    if (this.values.has(value)) {
      this.values.delete(value);
      return false;
    }
    this.values.add(value);
    return true;
  }
}

class FakeElement {
  constructor(classNames) {
    this.className = classNames || '';
    this.classList = new FakeClassList((classNames || '').split(/\s+/).filter(Boolean));
    this.children = [];
    this.listeners = {};
    this.parent = null;
    this.textContent = '';
  }

  appendChild(child) {
    child.parent = this;
    this.children.push(child);
  }

  querySelector(selector) {
    if (selector !== '.tool-expand-btn') return null;
    return this.children.find(child => child.className === 'tool-expand-btn') || null;
  }

  addEventListener(type, listener) {
    this.listeners[type] = listener;
  }

  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter(child => child !== this);
    this.parent = null;
  }
}

global.document = {
  createElement: () => new FakeElement()
};

const setCollapsibleText = eval('(' + extractFunction('setCollapsibleText', 'buildToolCard') + ')');

function makePre(collapsed) {
  return new FakeElement(collapsed ? 'tool-pre collapsed' : 'tool-pre');
}

const shortPre = makePre(true);
const shortSection = new FakeElement('tool-section');
setCollapsibleText(shortPre, 'short', shortSection);
assert.strictEqual(shortPre.classList.contains('collapsed'), false);
assert.strictEqual(shortSection.children.length, 0);

const exactCharsPre = makePre(false);
const exactCharsSection = new FakeElement('tool-section');
setCollapsibleText(exactCharsPre, 'x'.repeat(1200), exactCharsSection);
assert.strictEqual(exactCharsPre.classList.contains('collapsed'), false);

const longCharsPre = makePre(false);
const longCharsSection = new FakeElement('tool-section');
setCollapsibleText(longCharsPre, 'x'.repeat(1201), longCharsSection);
assert.strictEqual(longCharsPre.classList.contains('collapsed'), true);
assert.strictEqual(longCharsSection.children.length, 1);
const expandButton = longCharsSection.children[0];
assert.strictEqual(expandButton.textContent, '展开全部');
let propagationStopped = false;
expandButton.listeners.click({stopPropagation: () => { propagationStopped = true; }});
assert.strictEqual(propagationStopped, true);
assert.strictEqual(longCharsPre.classList.contains('collapsed'), false);
assert.strictEqual(expandButton.textContent, '收起');
expandButton.listeners.click({stopPropagation: () => {}});
assert.strictEqual(longCharsPre.classList.contains('collapsed'), true);
assert.strictEqual(expandButton.textContent, '展开全部');

const sixteenLinesPre = makePre(false);
const sixteenLinesSection = new FakeElement('tool-section');
setCollapsibleText(sixteenLinesPre, Array(16).fill('x').join('\n'), sixteenLinesSection);
assert.strictEqual(sixteenLinesPre.classList.contains('collapsed'), false);

const seventeenLinesPre = makePre(false);
const seventeenLinesSection = new FakeElement('tool-section');
setCollapsibleText(seventeenLinesPre, Array(17).fill('x').join('\n'), seventeenLinesSection);
assert.strictEqual(seventeenLinesPre.classList.contains('collapsed'), true);
assert.strictEqual(seventeenLinesSection.children.length, 1);

setCollapsibleText(longCharsPre, 'short now', longCharsSection);
assert.strictEqual(longCharsPre.classList.contains('collapsed'), false);
assert.strictEqual(longCharsSection.children.length, 0);
assert.strictEqual(seventeenLinesSection.children.length, 1);

const buildStart = chatSource.indexOf('function buildToolCard(');
const buildEnd = chatSource.indexOf('\n\n  function safeJson(', buildStart);
const buildSource = chatSource.slice(buildStart, buildEnd);
const argsStart = buildSource.indexOf("var argsSection = document.createElement('div');");
const resultStart = buildSource.indexOf("var resultSection = document.createElement('div');");
const argsSource = buildSource.slice(argsStart, resultStart);
const appendIndex = argsSource.indexOf('argsSection.appendChild(argsPre);');
const collapseIndex = argsSource.indexOf('setCollapsibleText(argsPre, safeJson(toolCall.arguments), argsSection);');
assert.ok(appendIndex >= 0 && collapseIndex > appendIndex, 'Argument collapse must run after argsPre is appended');
assert.strictEqual(argsSource.includes('argsPre.textContent ='), false, 'Argument text assignment must not be duplicated');

const toolPreRule = stylesSource.match(/\.tool-pre\s*\{([^}]*)\}/);
const collapsedRule = stylesSource.match(/\.tool-pre\.collapsed\s*\{([^}]*)\}/);
assert.ok(toolPreRule && /line-height:\s*1\.55/.test(toolPreRule[1]));
assert.ok(collapsedRule && /max-height:\s*320px/.test(collapsedRule[1]));
assert.ok(collapsedRule && /overflow-y:\s*auto/.test(collapsedRule[1]));
assert.ok(indexSource.includes('href="/static/styles.css?v=1.19"'));
assert.ok(indexSource.includes('src="/static/js/chatView.js?v=1.19"'));
console.log('tool card collapse: ok');
'''
    result = subprocess.run(
        ['node', '-e', nodeScript, str(chatViewPath), str(stylesPath), str(indexPath)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'tool card collapse: ok' in result.stdout
