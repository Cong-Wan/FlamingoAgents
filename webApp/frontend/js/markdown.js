/*
Author: wilbur
Version: 1.2
Date: 2026-09-15
Description: 公共 markdown 同步渲染：marked.parse → DOMPurify → innerHTML；
             可选 highlight.js 围栏高亮（与 fileExplorer.highlightCode 同路径，禁用 highlightElement）。
             聊天 / 文件预览共用。禁止 marked.setOptions，breaks/highlight 由调用方传入。
             v1.1（chatUxImprovePlan）：opts.highlight 时附加代码复制按钮 + 懒加载 mermaid（SVG 再消毒）；
             copyText 含 LAN HTTP 的 execCommand 降级。
             v1.2：流程图 htmlLabels=false，标签走 SVG text；消毒保留 SVG/foreignObject，避免逻辑图只剩框看不见字。
*/
(function () {
  'use strict';

  var mermaidIdSeq = 0;
  var mermaidLoad = null;
  var mermaidQueue = Promise.resolve();

  function languageOf(codeEl) {
    var cls = codeEl.className || '';
    var match = cls.match(/language-([\w+-]+)/);
    return match ? match[1] : '';
  }

  function isMermaidFence(codeEl) {
    return languageOf(codeEl) === 'mermaid';
  }

  function copyText(text) {
    var value = text == null ? '' : String(text);
    if (window.navigator && window.navigator.clipboard && window.isSecureContext) {
      return window.navigator.clipboard.writeText(value);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = value;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      var ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (ignore) {
        ok = false;
      }
      if (ta.parentNode) ta.parentNode.removeChild(ta);
      if (ok) resolve();
      else reject(new Error('copy failed'));
    });
  }

  function attachCopyButton(pre, text) {
    if (!pre) return;
    var old = pre.querySelector('.copy-btn');
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'copy-btn';
    btn.textContent = '复制';
    btn.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      copyText(text).then(function () {
        btn.textContent = '已复制';
        setTimeout(function () { btn.textContent = '复制'; }, 1500);
      }).catch(function () {
        btn.textContent = '复制失败';
        setTimeout(function () { btn.textContent = '复制'; }, 1500);
      });
    });
    pre.appendChild(btn);
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

  function enhanceCodeBlocks(el) {
    var blocks = el.querySelectorAll('pre code');
    var i;
    for (i = 0; i < blocks.length; i++) {
      var code = blocks[i];
      if (isMermaidFence(code)) continue;
      var pre = code.parentNode;
      if (!pre || pre.tagName !== 'PRE') continue;
      attachCopyButton(pre, code.textContent);
    }
  }

  function loadMermaid() {
    if (window.mermaid && typeof window.mermaid.render === 'function') {
      return Promise.resolve(window.mermaid);
    }
    if (mermaidLoad) return mermaidLoad;
    mermaidLoad = new Promise(function (resolve, reject) {
      var script = document.createElement('script');
      script.src = '/static/vendor/mermaid.min.js';
      script.onload = function () {
        if (!window.mermaid || typeof window.mermaid.render !== 'function') {
          mermaidLoad = null;
          reject(new Error('mermaid missing'));
          return;
        }
        window.mermaid.initialize({
          startOnLoad: false,
          theme: 'default',
          securityLevel: 'strict',
          fontFamily: '-apple-system, "PingFang SC", "Helvetica Neue", "Microsoft YaHei", sans-serif',
          // 默认 htmlLabels 把节点字放进 foreignObject；普通 HTML 消毒会剥掉，逻辑图只剩框。
          flowchart: { htmlLabels: false, useMaxWidth: true },
          class: { htmlLabels: false },
          state: { htmlLabels: false }
        });
        resolve(window.mermaid);
      };
      script.onerror = function () {
        mermaidLoad = null;
        reject(new Error('mermaid load failed'));
      };
      (document.head || document.documentElement).appendChild(script);
    });
    return mermaidLoad;
  }

  function restoreMermaidSource(holder, source) {
    if (!holder || !holder.parentNode) return;
    holder.className = 'mermaid-fallback';
    holder.textContent = '';
    var hint = document.createElement('div');
    hint.className = 'mermaid-block-error';
    hint.textContent = '图表渲染失败';
    var pre = document.createElement('pre');
    var code = document.createElement('code');
    code.className = 'language-mermaid';
    code.textContent = source;
    pre.appendChild(code);
    holder.appendChild(hint);
    holder.appendChild(pre);
    attachCopyButton(pre, source);
  }

  function sanitizeMermaidSvg(svg) {
    if (!window.DOMPurify) return '';
    return window.DOMPurify.sanitize(svg, {
      USE_PROFILES: { svg: true, svgFilters: true, html: true },
      ADD_TAGS: ['foreignObject', 'style'],
      ADD_ATTR: ['viewBox', 'xmlns', 'xmlns:xlink', 'xlink:href', 'marker-end', 'marker-start']
    });
  }

  function enqueueMermaid(job) {
    mermaidQueue = mermaidQueue.then(job, job);
    return mermaidQueue;
  }

  function renderMermaidBlocks(el) {
    var codes = el.querySelectorAll('pre > code.language-mermaid');
    var i;
    for (i = 0; i < codes.length; i++) {
      (function (code) {
        var pre = code.parentNode;
        if (!pre || !pre.parentNode) return;
        var source = code.textContent || '';
        var holder = document.createElement('div');
        holder.className = 'mermaid-block';
        holder.textContent = '图表渲染中…';
        pre.parentNode.replaceChild(holder, pre);
        enqueueMermaid(function () {
          return loadMermaid().then(function (mermaid) {
            if (!holder.parentNode) return null;
            mermaidIdSeq += 1;
            return mermaid.render('faMermaid' + mermaidIdSeq, source);
          }).then(function (result) {
            if (!result || !holder.parentNode) return;
            var svg = result.svg || '';
            holder.innerHTML = sanitizeMermaidSvg(svg);
          }).catch(function () {
            restoreMermaidSource(holder, source);
          });
        });
      })(codes[i]);
    }
  }

  // XSS 红线：不可信文本必须 marked → DOMPurify 后才允许 innerHTML
  // opts.highlight:true = 终态抛光：高亮 + 代码复制 + mermaid（live 帧必须 false）
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
    if (opts.highlight) {
      highlightFences(el);
      enhanceCodeBlocks(el);
      renderMermaidBlocks(el);
    }
  }

  window.copyText = copyText;
  window.renderMarkdown = renderMarkdown;
})();
