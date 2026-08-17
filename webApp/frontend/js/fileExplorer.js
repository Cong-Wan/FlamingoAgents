/*
Author: wilbur
Version: 1.2
Date: 2026-08-17
Description: 右侧文件浏览器（迭代二方案 §4.7/D6）：懒加载目录树（每层缓存、刷新清缓存）、折叠状态存 localStorage；
             文件预览弹层——代码 highlight.js 高亮（getLanguage 预判降级纯文本、输出过 DOMPurify）、
             .md marked 渲染（渲染/源码切换）、行号列；Esc 经 modalStack 只关栈顶。
             v1.1 取消 PREVIEW_CHAR_LIMIT 预览截断限制，完整渲染文件内容。
             v1.2（markdownRenderUnifyPlan）：渲染模式改调 window.renderMarkdown（breaks:false, highlight:true）；源码模式不动。
*/
(function () {
  'use strict';

  var panelEl = document.getElementById('filePanel');
  var treeEl = document.getElementById('fileTree');
  var toggleButton = document.getElementById('filePanelToggle');
  var refreshButton = document.getElementById('filePanelRefresh');

  var previewModalEl = document.getElementById('filePreviewModal');
  var previewTitleEl = document.getElementById('filePreviewTitle');
  var previewBodyEl = document.getElementById('filePreviewBody');
  var previewModeToggle = document.getElementById('filePreviewModeToggle');
  var previewCloseButton = document.getElementById('filePreviewClose');

  var collapsed = false;
  try {
    var stored = localStorage.getItem('filePanelCollapsed');
    collapsed = stored === null ? window.innerWidth < 1100 : stored === '1'; // 窄屏默认折叠（§4.1）
  } catch (ignore) { /* 隐私模式 */ }

  var dirCache = {};      // path → entries
  var previewState = null; // { path, content, isMarkdown, mode: 'rendered'|'source' }

  var languageMap = {
    js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
    ts: 'typescript', tsx: 'typescript',
    py: 'python', json: 'json', css: 'css', html: 'xml', htm: 'xml', xml: 'xml',
    yml: 'yaml', yaml: 'yaml', sh: 'bash', bash: 'bash', zsh: 'bash',
    java: 'java', go: 'go', rs: 'rust', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp',
    cs: 'csharp', sql: 'sql', rb: 'ruby', php: 'php', swift: 'swift', kt: 'kotlin',
    md: 'markdown', markdown: 'markdown', toml: 'ini', ini: 'ini', dockerfile: 'dockerfile'
  };

  function extOf(name) {
    var dot = name.lastIndexOf('.');
    return dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
  }

  /* ---------- 面板折叠 ---------- */

  function applyCollapsed(value) {
    collapsed = value;
    panelEl.classList.toggle('hidden', collapsed || !window.appStore.currentSessionId);
    try { localStorage.setItem('filePanelCollapsed', collapsed ? '1' : '0'); } catch (ignore) { /* 忽略 */ }
  }

  toggleButton.addEventListener('click', function () { applyCollapsed(!collapsed); });

  /* ---------- 目录树 ---------- */

  function buildEntryRow(entry, path, depth) {
    var row = document.createElement('div');
    row.className = 'file-row';
    row.style.paddingLeft = (8 + depth * 16) + 'px';

    var arrow = document.createElement('span');
    arrow.className = 'file-arrow';
    arrow.textContent = entry.type === 'dir' ? '▸' : '';
    row.appendChild(arrow);

    var label = document.createElement('span');
    label.className = 'file-name';
    label.textContent = (entry.type === 'dir' ? '📁 ' : '📄 ') + entry.name;
    label.title = path;
    row.appendChild(label);
    return row;
  }

  function buildChildrenContainer(depth) {
    var container = document.createElement('div');
    container.className = 'file-children hidden';
    container.dataset.depth = depth + 1;
    return container;
  }

  async function loadLevel(path) {
    if (!dirCache[path]) {
      var sessionId = window.appStore.currentSessionId;
      if (!sessionId) return null;
      var data = await window.api.listFiles(sessionId, path);
      dirCache[path] = data.entries || [];
    }
    return dirCache[path];
  }

  function renderEntries(container, entries, path, depth) {
    container.innerHTML = '';
    entries.forEach(function (entry) {
      var childPath = path ? path + '/' + entry.name : entry.name;
      var row = buildEntryRow(entry, childPath, depth);
      container.appendChild(row);
      if (entry.type === 'dir') {
        var children = buildChildrenContainer(depth);
        container.appendChild(children);
        row.addEventListener('click', function () {
          toggleDir(row, children, childPath);
        });
      } else {
        row.addEventListener('click', function () { openPreview(childPath); });
      }
    });
  }

  async function toggleDir(row, children, path) {
    var arrow = row.querySelector('.file-arrow');
    if (!children.classList.contains('hidden')) {
      children.classList.add('hidden');
      arrow.textContent = '▸';
      return;
    }
    if (!children.dataset.loaded) {
      try {
        var entries = await loadLevel(path);
        renderEntries(children, entries || [], path, Number(children.dataset.depth));
        children.dataset.loaded = '1';
      } catch (error) {
        // workDir 被删/不可读等 400：树内空态提示，不弹错误条（评审 H2）
        children.innerHTML = '';
        var empty = document.createElement('div');
        empty.className = 'file-empty';
        empty.textContent = error.message;
        children.appendChild(empty);
        children.dataset.loaded = '1';
      }
    }
    children.classList.remove('hidden');
    arrow.textContent = '▾';
  }

  async function loadRoot() {
    treeEl.innerHTML = '';
    try {
      var entries = await loadLevel('');
      renderEntries(treeEl, entries || [], '', 0);
    } catch (error) {
      var empty = document.createElement('div');
      empty.className = 'file-empty';
      empty.textContent = error.message;
      treeEl.appendChild(empty);
    }
  }

  refreshButton.addEventListener('click', function () {
    dirCache = {};
    loadRoot();
  });

  /* ---------- 文件预览 ---------- */

  function highlightCode(content, path) {
    // D6/评审 M2：getLanguage 预判，未注册语言或 hljs 缺失降级纯文本；输出过 DOMPurify 再 innerHTML。
    var lang = languageMap[extOf(path)];
    if (window.hljs && lang && window.hljs.getLanguage(lang)) {
      var html = window.hljs.highlight(content, { language: lang }).value;
      return window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
    }
    return null; // 调用方走 textContent 纯文本
  }

  function buildCodeView(content, path) {
    var wrap = document.createElement('div');
    wrap.className = 'preview-code-wrap';

    var gutter = document.createElement('div');
    gutter.className = 'preview-gutter';
    var lineCount = content === '' ? 1 : content.split('\n').length;
    var numbers = [];
    for (var i = 1; i <= lineCount; i++) numbers.push(i);
    gutter.textContent = numbers.join('\n');
    wrap.appendChild(gutter);

    var pre = document.createElement('pre');
    pre.className = 'preview-code';
    var code = document.createElement('code');
    var highlighted = highlightCode(content, path);
    if (highlighted !== null) {
      code.className = 'hljs';
      code.innerHTML = highlighted;
    } else {
      code.textContent = content;
    }
    pre.appendChild(code);
    wrap.appendChild(pre);
    return wrap;
  }

  function buildMarkdownView(content) {
    var el = document.createElement('div');
    el.className = 'markdown-content preview-markdown';
    window.renderMarkdown(el, content || '', { breaks: false, highlight: true });
    return el;
  }

  function renderPreviewBody() {
    previewBodyEl.innerHTML = '';
    var content = previewState.content;
    if (previewState.isMarkdown && previewState.mode === 'rendered') {
      previewBodyEl.appendChild(buildMarkdownView(content));
    } else {
      previewBodyEl.appendChild(buildCodeView(content, previewState.path));
    }
    previewModeToggle.textContent = previewState.mode === 'rendered' ? '源码' : '渲染';
  }

  function closePreview() {
    previewModalEl.classList.add('hidden');
    previewState = null;
    window.appStore.removeModalClose(closePreview);
  }

  async function openPreview(path) {
    var sessionId = window.appStore.currentSessionId;
    if (!sessionId) return;
    try {
      var data = await window.api.getFileContent(sessionId, path);
      previewState = {
        path: data.path,
        content: data.content || '',
        isMarkdown: ['md', 'markdown'].indexOf(extOf(data.path)) >= 0,
        mode: 'rendered'
      };
      previewTitleEl.textContent = data.path;
      previewTitleEl.title = data.path;
      previewModeToggle.classList.toggle('hidden', !previewState.isMarkdown);
      renderPreviewBody();
      previewModalEl.classList.remove('hidden');
      window.appStore.pushModalClose(closePreview); // Esc 栈（§4.3）
    } catch (error) {
      if (window.toast) window.toast(error.message);
    }
  }

  previewModeToggle.addEventListener('click', function () {
    if (!previewState) return;
    previewState.mode = previewState.mode === 'rendered' ? 'source' : 'rendered';
    renderPreviewBody();
  });
  previewCloseButton.addEventListener('click', closePreview);
  previewModalEl.addEventListener('click', function (event) {
    if (event.target === previewModalEl) closePreview();
  });

  /* ---------- 对外接口（挂载点：chatView.open / showEmpty，§4.7） ---------- */

  window.fileExplorer = {
    open: function () {
      dirCache = {};
      toggleButton.classList.remove('hidden');
      panelEl.classList.toggle('hidden', collapsed);
      loadRoot();
    },
    hide: function () {
      toggleButton.classList.add('hidden');
      panelEl.classList.add('hidden');
      treeEl.innerHTML = '';
      dirCache = {};
      if (previewState) closePreview();
    }
  };
})();
