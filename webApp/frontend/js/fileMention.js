/*
Author: wilbur
Version: 1.1
Date: 2026-08-11
Description: 「@」文件引用面板（迭代二方案 §4.5）：@ 前必须为行首/空白（防 user@example.com 误触发），
             目录下钻、文件名过滤、attachable:false 置灰；选中文件生成 chip（输入框上方），发送时随 attachments 提交。
             键盘拦截与 slashCommand 同约（capture 阶段，§4.3）。
             v1.1：修复 IME 组合态按 Enter 误选中条目——组合中放行让输入法先提交候选词（同 slashCommand v1.1）。
*/
(function () {
  'use strict';

  var composerInput = document.getElementById('composerInput');
  var panelEl = document.getElementById('mentionPanel');
  var chipsEl = document.getElementById('attachmentChips');

  var panelOpen = false;
  var items = [];          // 当前面板条目 [{ label, type, path, attachable }]
  var activeIndex = 0;
  var currentPath = '';    // 面板当前所在目录（相对 workDir）
  var triggerStart = -1;   // textarea 中触发 @ 的字符下标
  var dirCache = {};       // path → entries（会话内缓存，切会话清空）
  var chips = [];          // [{ path }]

  /* ---------- 面板 ---------- */

  function renderPanel() {
    panelEl.innerHTML = '';
    if (currentPath) {
      var crumb = document.createElement('div');
      crumb.className = 'command-crumb';
      crumb.textContent = '📁 ' + currentPath;
      panelEl.appendChild(crumb);
    }
    items.forEach(function (item, index) {
      var row = document.createElement('div');
      row.className = 'command-item' + (index === activeIndex ? ' active' : '')
        + (item.attachable === false ? ' disabled' : '');
      var label = document.createElement('span');
      label.className = 'command-label';
      label.textContent = (item.type === 'dir' ? '📁 ' : '📄 ') + item.label;
      row.appendChild(label);
      if (item.attachable === false) {
        var desc = document.createElement('span');
        desc.className = 'command-desc';
        desc.textContent = '超过 512KB';
        row.appendChild(desc);
      }
      row.addEventListener('mousedown', function (event) {
        event.preventDefault();
        pickItem(item);
      });
      panelEl.appendChild(row);
    });
    panelEl.classList.remove('hidden');
    panelOpen = true;
  }

  function closePanel() {
    panelEl.classList.add('hidden');
    panelEl.innerHTML = '';
    panelOpen = false;
    items = [];
    activeIndex = 0;
    triggerStart = -1;
  }

  function joinPath(dir, name) {
    return dir ? dir + '/' + name : name;
  }

  async function openLevel(path, keyword) {
    var sessionId = window.appStore.currentSessionId;
    if (!sessionId) return;
    try {
      if (!dirCache[path]) {
        var data = await window.api.listFiles(sessionId, path);
        dirCache[path] = data.entries || [];
      }
      currentPath = path;
      var entries = dirCache[path];
      if (keyword) {
        var lowered = keyword.toLowerCase();
        entries = entries.filter(function (entry) {
          return entry.name.toLowerCase().indexOf(lowered) === 0;
        });
      }
      items = entries.map(function (entry) {
        return {
          label: entry.name,
          type: entry.type,
          path: joinPath(path, entry.name),
          attachable: entry.type === 'dir' ? true : entry.attachable
        };
      });
      if (items.length === 0) {
        closePanel();
        return;
      }
      activeIndex = 0;
      renderPanel();
    } catch (error) {
      closePanel();
      if (window.toast) window.toast('读取目录失败：' + error.message);
    }
  }

  function clearTriggerText() {
    // 清掉 textarea 中从触发 @ 到光标的内容（选中即不留 @路径 文本，§4.5）
    if (triggerStart < 0) return;
    var cursor = composerInput.selectionStart;
    composerInput.value = composerInput.value.slice(0, triggerStart) + composerInput.value.slice(cursor);
    composerInput.selectionStart = composerInput.selectionEnd = triggerStart;
  }

  function pickItem(item) {
    if (item.type === 'dir') {
      clearTriggerText();
      triggerStart = composerInput.selectionStart;
      openLevel(item.path, '');
      return;
    }
    if (item.attachable === false) return; // 置灰文件不可选
    clearTriggerText();
    addChip(item.path);
    closePanel();
    composerInput.focus();
  }

  /* ---------- chips ---------- */

  function renderChips() {
    chipsEl.innerHTML = '';
    chipsEl.classList.toggle('hidden', chips.length === 0);
    chips.forEach(function (chip, index) {
      var el = document.createElement('span');
      el.className = 'attachment-chip';
      var label = document.createElement('span');
      label.textContent = '📄 ' + chip.path;
      label.title = chip.path;
      var remove = document.createElement('button');
      remove.className = 'attachment-chip-remove';
      remove.textContent = '✕';
      remove.title = '移除';
      remove.addEventListener('click', function () {
        chips.splice(index, 1);
        renderChips();
      });
      el.appendChild(label);
      el.appendChild(remove);
      chipsEl.appendChild(el);
    });
  }

  function addChip(path) {
    for (var i = 0; i < chips.length; i++) {
      if (chips[i].path === path) return; // 去重
    }
    if (chips.length >= 8) {
      if (window.toast) window.toast('附件最多 8 个');
      return;
    }
    chips.push({ path: path });
    renderChips();
  }

  /* ---------- 触发检测 ---------- */

  // 光标前最近 @：前面必须是行首或空白、其后无空白才触发（评审 L1）
  function detectTrigger() {
    var cursor = composerInput.selectionStart;
    var before = composerInput.value.slice(0, cursor);
    var atIndex = before.lastIndexOf('@');
    if (atIndex < 0) return -1;
    if (atIndex > 0 && !/\s/.test(before.charAt(atIndex - 1))) return -1;
    var after = before.slice(atIndex + 1);
    if (/\s/.test(after)) return -1;
    return atIndex;
  }

  function onInput() {
    if (!window.appStore.currentSessionId) return;
    var atIndex = detectTrigger();
    if (atIndex < 0) {
      if (panelOpen) closePanel();
      return;
    }
    triggerStart = atIndex;
    var keyword = composerInput.value.slice(atIndex + 1, composerInput.selectionStart);
    // 目录下钻后 keyword 含路径段：取最后一段对当前层过滤，路径部分下钻
    var slashIndex = keyword.lastIndexOf('/');
    if (slashIndex >= 0) {
      openLevel(joinPath('', keyword.slice(0, slashIndex)), keyword.slice(slashIndex + 1));
    } else {
      openLevel('', keyword);
    }
  }

  composerInput.addEventListener('input', onInput);

  // capture 阶段（§4.3）：面板打开时拦截 Enter/Tab/Esc/↑↓，先于 chatView 的 Enter→send。
  // 注意：textarea 是事件目标本身，同节点监听器按注册顺序都执行，必须 stopImmediatePropagation。
  composerInput.addEventListener('keydown', function (event) {
    if (!panelOpen) return;
    // IME 组合态放行：此时 Enter 是提交候选词而非选中条目（同 slashCommand v1.1）
    if (event.isComposing || event.keyCode === 229) return;
    if (['Enter', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown'].indexOf(event.key) < 0) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    if (event.key === 'Escape') {
      closePanel();
    } else if (event.key === 'ArrowUp') {
      activeIndex = (activeIndex - 1 + items.length) % items.length;
      renderPanel();
    } else if (event.key === 'ArrowDown') {
      activeIndex = (activeIndex + 1) % items.length;
      renderPanel();
    } else if (items[activeIndex]) { // Enter / Tab
      pickItem(items[activeIndex]);
    }
  }, true);

  window.fileMention = {
    isOpen: function () { return panelOpen; },
    hasChips: function () { return chips.length > 0; },
    getAttachments: function () {
      return chips.map(function (chip) { return { path: chip.path }; });
    },
    clearChips: function () {
      chips = [];
      renderChips();
    },
    resetForSession: function () {
      dirCache = {};
      closePanel();
      window.fileMention.clearChips();
    }
  };
})();
