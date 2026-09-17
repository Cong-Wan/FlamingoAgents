const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function decode(text) {
  return String(text)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"');
}

function hasClass(el, name) {
  return (' ' + (el.className || '') + ' ').indexOf(' ' + name + ' ') >= 0;
}

class El {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.className = '';
    this.children = [];
    this.parentNode = null;
    this._text = '';
    this._inner = '';
    this.listeners = {};
    this.style = {};
    this.src = '';
    this.onload = null;
    this.onerror = null;
  }
  get textContent() {
    if (this.children.length) return this.children.map((child) => child.textContent).join('');
    return this._text;
  }
  set textContent(value) {
    this.children = [];
    this._inner = '';
    this._text = value == null ? '' : String(value);
  }
  get innerHTML() { return this._inner; }
  set innerHTML(html) {
    this._inner = html == null ? '' : String(html);
    this.children = [];
    this._text = '';
    const re = /<pre[^>]*>\s*<code([^>]*)>([\s\S]*?)<\/code>\s*<\/pre>/gi;
    let match;
    while ((match = re.exec(this._inner))) {
      const pre = new El('pre');
      const code = new El('code');
      const cls = /class="([^"]*)"/.exec(match[1] || '');
      code.className = cls ? cls[1] : '';
      code.textContent = decode(match[2]);
      pre.appendChild(code);
      this.appendChild(pre);
    }
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  replaceChild(next, old) {
    const index = this.children.indexOf(old);
    if (index < 0) return old;
    old.parentNode = null;
    next.parentNode = this;
    this.children[index] = next;
    return old;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      node.children.forEach((child) => {
        if (sel === 'pre code' && node.tagName === 'PRE' && child.tagName === 'CODE') out.push(child);
        else if (sel === 'pre > code.language-mermaid' && node.tagName === 'PRE' && child.tagName === 'CODE' && hasClass(child, 'language-mermaid')) out.push(child);
        else if (sel === '.copy-btn' && hasClass(child, 'copy-btn')) out.push(child);
        else if (sel === '.mermaid-block' && hasClass(child, 'mermaid-block')) out.push(child);
        walk(child);
      });
    };
    walk(this);
    return out;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  addEventListener(type, fn) {
    (this.listeners[type] || (this.listeners[type] = [])).push(fn);
  }
}

const head = new El('head');
const body = new El('body');
const documentElement = new El('html');
documentElement.appendChild(head);
const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  Promise,
  Error,
  document: {
    head,
    body,
    documentElement,
    createElement: (tag) => new El(tag),
    execCommand: () => true,
  },
  navigator: {},
  isSecureContext: false,
  marked: {
    parse: function (source) {
      const text = String(source || '');
      if (text.indexOf('```mermaid') >= 0 && text.indexOf('```javascript') < 0 && text.indexOf('```js') < 0) {
        return '<pre><code class="language-mermaid">graph TD; A--&gt;B;</code></pre>';
      }
      if (text.indexOf('```javascript') >= 0 || text.indexOf('```js') >= 0) {
        return '<p>hello</p><pre><code class="language-javascript">const a = 1;</code></pre>';
      }
      return '<p></p>';
    }
  },
  DOMPurify: { sanitize: function (html) { return html; } },
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);

assert.equal(typeof sandbox.renderMarkdown, 'function');
assert.equal(typeof sandbox.copyText, 'function');

const liveRoot = new El('div');
sandbox.renderMarkdown(liveRoot, '```js\nconst a = 1;\n```\n\n```mermaid\ngraph TD; A-->B;\n```', { breaks: true, highlight: false });
assert.strictEqual(liveRoot.querySelectorAll('.copy-btn').length, 0);
assert.strictEqual(liveRoot.querySelectorAll('.mermaid-block').length, 0);
assert.ok(liveRoot.querySelectorAll('pre code').length >= 1);

const finalRoot = new El('div');
sandbox.renderMarkdown(finalRoot, 'hello\n\n```javascript\nconst a = 1;\n```\n', { breaks: true, highlight: true });
assert.strictEqual(finalRoot.querySelectorAll('.copy-btn').length, 1);
assert.strictEqual(finalRoot.querySelectorAll('.mermaid-block').length, 0);

const mermaidRoot = new El('div');
sandbox.renderMarkdown(mermaidRoot, '```mermaid\ngraph TD; A-->B;\n```', { breaks: true, highlight: true });
assert.strictEqual(mermaidRoot.querySelectorAll('.mermaid-block').length, 1);
assert.ok(mermaidRoot.querySelectorAll('.mermaid-block')[0].textContent.indexOf('图表渲染中') >= 0);
assert.strictEqual(mermaidRoot.querySelectorAll('.copy-btn').length, 0);

Promise.resolve().then(function () {
  assert.ok(head.children.some((child) => child.tagName === 'SCRIPT' && /mermaid\.min\.js/.test(child.src)));
  console.log('markdown enhance: ok');
}).catch(function (error) {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
