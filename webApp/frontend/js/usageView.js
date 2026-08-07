/*
Author: wilbur
Version: 1.0
Date: 2026-08-07
Description: 用量统计页：顶部三卡片（总 prompt/cached/completion）+ 会话用量表格（契约 §2.3/§3.8）
*/
(function () {
  'use strict';

  var promptEl = document.getElementById('usagePrompt');
  var cachedEl = document.getElementById('usageCached');
  var completionEl = document.getElementById('usageCompletion');
  var tableBodyEl = document.getElementById('usageTableBody');

  function formatNumber(num) {
    return (num || 0).toLocaleString('zh-CN');
  }

  function formatTime(isoTime) {
    if (!isoTime) return '';
    var date = new Date(isoTime);
    if (isNaN(date.getTime())) return isoTime;
    function pad(n) { return n < 10 ? '0' + n : '' + n; }
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
      + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  window.usageView = {
    open: async function () {
      promptEl.textContent = '-';
      cachedEl.textContent = '-';
      completionEl.textContent = '-';
      tableBodyEl.innerHTML = '';
      try {
        var data = await window.api.getUsage();
        var total = data.total || {};
        promptEl.textContent = formatNumber(total.promptTokens);
        cachedEl.textContent = formatNumber(total.cachedTokens);
        completionEl.textContent = formatNumber(total.completionTokens);

        (data.sessions || []).forEach(function (session) {
          var row = document.createElement('tr');

          var titleTd = document.createElement('td');
          var link = document.createElement('a');
          link.href = '#/chat/' + session.sessionId;
          link.textContent = session.title || '新会话';
          titleTd.appendChild(link);
          row.appendChild(titleTd);

          var modelTd = document.createElement('td');
          modelTd.textContent = session.modelId
            ? session.providerId + ' / ' + session.modelId
            : (session.providerId || '');
          row.appendChild(modelTd);

          var usage = session.usage || {};
          [usage.promptTokens, usage.cachedTokens, usage.completionTokens].forEach(function (value) {
            var td = document.createElement('td');
            td.textContent = formatNumber(value);
            row.appendChild(td);
          });

          var timeTd = document.createElement('td');
          timeTd.textContent = formatTime(session.updatedAt);
          row.appendChild(timeTd);

          tableBodyEl.appendChild(row);
        });
      } catch (error) {
        var row = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 6;
        td.textContent = '加载失败：' + error.message;
        row.appendChild(td);
        tableBodyEl.appendChild(row);
      }
    }
  };
})();
