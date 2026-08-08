/*
Author: wilbur
Version: 1.3
Date: 2026-08-08
Description: 会话状态栏（迭代二方案 §4.2）：composer 下方两行——workDir+git 分支、tokens/费用/窗口占用。
             刷新时机（D7）：chatView.open、流终态、/model 切换后由调用方触发 refresh()，不轮询。
             v1.1 用量行文案精简：去掉 in/out 字样，窗口改为「百分比 / 上下文长度」。
             v1.2 ↑↓⚡ 改读 lastUsage（最近一轮增量）；百分比改使用率 contextUsedPercent；$ 仍为会话累计。
             v1.3 口径对齐 pi（statusBarUsageFixPlan P2/D1-A）：↑↓⚡ 改读 usage（会话累计），↑=max(0,prompt−cached) 减法归一化，
             三者互不重叠、↑+⚡=总输入；lastUsage 字段契约保留但状态栏不再使用。
*/
(function () {
  'use strict';

  var containerEl = document.getElementById('composerStatus');
  var locationEl = document.getElementById('statusLocation');
  var usageEl = document.getElementById('statusUsage');

  function formatCompact(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
    return String(num);
  }

  function appendSpan(parent, text, cls) {
    var span = document.createElement('span');
    if (cls) span.className = cls;
    span.textContent = text;
    parent.appendChild(span);
    return span;
  }

  function renderLocation(data) {
    locationEl.innerHTML = '';
    var pathSpan = appendSpan(locationEl, '📁 ' + (data.workDir || '-'), 'status-workdir');
    pathSpan.title = data.workDir || '';
    if (data.gitBranch) {
      appendSpan(locationEl, '  ⎇ ' + data.gitBranch, 'status-branch').title = 'git 分支';
    }
  }

  function renderUsage(data) {
    usageEl.innerHTML = '';
    // ↑↓⚡：会话累计（usage），口径对齐 pi——↑=非缓存输入（prompt 含 cached，减法归一化）、↓=输出、⚡=缓存命中，三者互不重叠
    var usage = data.usage || {};
    var nonCachedPrompt = Math.max(0, (usage.promptTokens || 0) - (usage.cachedTokens || 0));
    var used = (data.contextUsedPercent === null || data.contextUsedPercent === undefined)
      ? '-' : data.contextUsedPercent + '%';
    var windowSize = data.contextWindow ? formatCompact(data.contextWindow) : '-';
    var segments = [
      '↑ ' + formatCompact(nonCachedPrompt),
      '↓ ' + formatCompact(usage.completionTokens || 0),
      '⚡ ' + formatCompact(usage.cachedTokens || 0) + ' cached',
      data.cost > 0 ? '$' + data.cost.toFixed(4) : '$-',
      used + ' / ' + windowSize
    ];
    segments.forEach(function (text, index) {
      if (index > 0) appendSpan(usageEl, ' · ', 'status-sep');
      appendSpan(usageEl, text, null);
    });
  }

  window.statusBar = {
    refresh: async function () {
      var sessionId = window.appStore.currentSessionId;
      if (!sessionId) {
        window.statusBar.hide();
        return;
      }
      try {
        var data = await window.api.getSessionStatus(sessionId);
        if (sessionId !== window.appStore.currentSessionId) return; // 竞态：已切走会话
        renderLocation(data);
        renderUsage(data);
        containerEl.classList.remove('hidden');
      } catch (ignore) {
        // 状态栏是辅助信息：失败（如会话被删）静默收起，不弹错误条
        containerEl.classList.add('hidden');
      }
    },

    hide: function () {
      containerEl.classList.add('hidden');
    }
  };
})();
