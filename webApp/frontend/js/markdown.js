/*
Author: wilbur
Version: 1.0
Date: 2026-08-17
Description: 公共 markdown 同步渲染：marked.parse → DOMPurify → innerHTML；
             可选 highlight.js 围栏高亮（与 fileExplorer.highlightCode 同路径，禁用 highlightElement）。
             聊天 / 文件预览共用。禁止 marked.setOptions，breaks/highlight 由调用方传入。
*/
(function () {
  'use strict';

  function languageOf(codeEl) {
    var cls = codeEl.className || '';
    var match = cls.match(/language-([\w+-]+)/);
    return match ? match[1] : '';
  }

  function highlightFences(el) {
    if (!window.hljs) return;
    var blocks = el.querySelectorAll('pre code');
    var i;
    for (i = 0; i < blocks.length; i++) {
      var code = blocks[i];
      var lang = languageOf(code);
      if (!lang || !window.hljs.getLanguage(lang)) continue;
      var highlighted = window.hljs.highlight(code.textContent, { language: lang }).value;
      code.innerHTML = window.DOMPurify ? window.DOMPurify.sanitize(highlighted) : highlighted;
      if (code.className.indexOf('hljs') < 0) {
        code.className = (code.className ? code.className + ' ' : '') + 'hljs';
      }
    }
  }

  // XSS 红线：不可信文本必须 marked → DOMPurify 后才允许 innerHTML
  function renderMarkdown(el, text, opts) {
    if (!el) return;
    opts = opts || {};
    var source = text || '';
    if (!window.marked) {
      el.textContent = source;
      return;
    }
    var html = window.marked.parse(source, {
      gfm: true,
      breaks: !!opts.breaks
    });
    el.innerHTML = window.DOMPurify ? window.DOMPurify.sanitize(html) : '';
    if (opts.highlight) highlightFences(el);
  }

  window.renderMarkdown = renderMarkdown;
})();
