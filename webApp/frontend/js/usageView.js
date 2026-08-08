/*
Author: wilbur
Version: 1.2
Date: 2026-08-08
Description: 用量统计页：顶部三卡片（总 prompt/cached/completion）+ 会话用量表格（契约 §2.3/§3.9）。
             v1.1 迭代一（§11.4/契约 §3.10）：Chart.js 组合图（每模型哈希固定色堆叠柱状 + 总量折线）、
             时/天/月粒度切换、任一模型 cost 非零时出「总费用」卡（month 全量求和口径）、双口径标注。
             v1.2 tokensOf 去掉重复计入的 cachedTokens（其为 promptTokens 子集，OpenAI 原生语义），图表总量不再双计。
*/
(function () {
  'use strict';

  var promptEl = document.getElementById('usagePrompt');
  var cachedEl = document.getElementById('usageCached');
  var completionEl = document.getElementById('usageCompletion');
  var costCardEl = document.getElementById('usageCostCard');
  var costEl = document.getElementById('usageCost');
  var tableBodyEl = document.getElementById('usageTableBody');
  var chartCanvas = document.getElementById('usageChart');
  var chartEmptyEl = document.getElementById('usageChartEmpty');
  var granularitySwitchEl = document.getElementById('granularitySwitch');

  var granularity = 'day'; // 当前粒度（默认天）
  var chart = null;        // Chart.js 实例

  // 每模型固定一色：按 providerId/modelId 字符串 djb2 哈希到预设调色板
  var palette = ['#3b6ef6', '#30a46c', '#f5a524', '#e5484d', '#8e4ec6',
                 '#12a594', '#e93d82', '#6d7ff2', '#ad5700', '#5b5bd6'];

  function colorFor(modelKey) {
    var hash = 5381;
    for (var i = 0; i < modelKey.length; i++) {
      hash = ((hash << 5) + hash + modelKey.charCodeAt(i)) >>> 0;
    }
    return palette[hash % palette.length];
  }

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

  function tokensOf(entry) {
    // cachedTokens 是 promptTokens 的子集（OpenAI 原生语义），不再单加，避免双计（statusBarUsageFixPlan M3）
    return (entry.promptTokens || 0) + (entry.completionTokens || 0);
  }

  /* ---------- 卡片区（三 token 卡原口径；费用卡 = month 全量求和） ---------- */

  async function loadSummary() {
    var data = await window.api.getUsage();
    var total = data.total || {};
    promptEl.textContent = formatNumber(total.promptTokens);
    cachedEl.textContent = formatNumber(total.cachedTokens);
    completionEl.textContent = formatNumber(total.completionTokens);
    renderTable(data.sessions || []);
  }

  // 总费用卡：granularity=month 全量 buckets cost 求和（hour/day 有范围限制，不能作总费用口径）
  async function loadCostCard() {
    costCardEl.classList.add('hidden');
    try {
      var data = await window.api.getUsageSeries('month');
      var totalCost = 0;
      (data.buckets || []).forEach(function (bucket) { totalCost += bucket.cost || 0; });
      if (totalCost > 0) { // 全部模型 cost 为 0 时恒 0，不展示费用卡
        costEl.textContent = '$' + totalCost.toFixed(4);
        costCardEl.classList.remove('hidden');
      }
    } catch (ignore) { /* 费用卡加载失败不影响其它区块 */ }
  }

  function renderTable(sessions) {
    tableBodyEl.innerHTML = '';
    sessions.forEach(function (session) {
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
  }

  /* ---------- 图表区（契约 §3.10：堆叠柱状 + 总量折线，byModel key 为 providerId/modelId） ---------- */

  async function loadChart() {
    if (chart) { chart.destroy(); chart = null; }
    chartEmptyEl.classList.add('hidden');
    if (!window.Chart) {
      chartEmptyEl.textContent = '图表组件（Chart.js）加载失败。';
      chartEmptyEl.classList.remove('hidden');
      return;
    }
    var data;
    try {
      data = await window.api.getUsageSeries(granularity);
    } catch (error) {
      chartEmptyEl.textContent = '图表数据加载失败：' + error.message;
      chartEmptyEl.classList.remove('hidden');
      return;
    }
    var models = data.models || [];
    var buckets = data.buckets || [];
    if (buckets.length === 0) {
      chartEmptyEl.textContent = '暂无用量数据';
      chartEmptyEl.classList.remove('hidden');
      return;
    }

    var hasCost = buckets.some(function (bucket) { return (bucket.cost || 0) > 0; });

    // 堆叠柱状：每模型一根柱（值 = 该桶该模型三 token 之和），固定色
    var datasets = models.map(function (modelKey) {
      return {
        type: 'bar',
        label: modelKey,
        backgroundColor: colorFor(modelKey),
        stack: 'tokens',
        data: buckets.map(function (bucket) {
          var byModel = bucket.byModel && bucket.byModel[modelKey];
          return byModel ? tokensOf(byModel) : 0;
        })
      };
    });
    // 总量折线叠加（所有模型三 token 之和）
    datasets.push({
      type: 'line',
      label: '总量',
      borderColor: '#1c1c1e',
      backgroundColor: '#1c1c1e',
      borderWidth: 2,
      pointRadius: 2,
      tension: 0.2,
      data: buckets.map(tokensOf)
    });

    chart = new window.Chart(chartCanvas, {
      data: { labels: buckets.map(function (bucket) { return bucket.label; }), datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { stacked: true },
          y: { stacked: true, beginAtZero: true }
        },
        plugins: {
          tooltip: {
            callbacks: {
              label: function (item) {
                return ' ' + item.dataset.label + '：' + formatNumber(item.parsed.y) + ' tokens';
              },
              // 该桶各模型明细与 cost（cost 恒 0 时不显示费用行）
              afterBody: function (items) {
                if (!items.length) return [];
                var bucket = buckets[items[0].dataIndex];
                if (!bucket) return [];
                var lines = [];
                models.forEach(function (modelKey) {
                  var byModel = bucket.byModel && bucket.byModel[modelKey];
                  if (!byModel) return;
                  var line = modelKey + '：' + formatNumber(tokensOf(byModel)) + ' tokens';
                  if (hasCost) line += '，$' + (byModel.cost || 0).toFixed(4);
                  lines.push(line);
                });
                if (hasCost) lines.push('桶费用合计：$' + (bucket.cost || 0).toFixed(4));
                return lines;
              }
            }
          }
        }
      }
    });
  }

  granularitySwitchEl.addEventListener('click', function (event) {
    var button = event.target.closest('.granularity-btn');
    if (!button || button.dataset.granularity === granularity) return;
    granularity = button.dataset.granularity;
    granularitySwitchEl.querySelectorAll('.granularity-btn').forEach(function (btn) {
      btn.classList.toggle('active', btn === button);
    });
    loadChart();
  });

  window.usageView = {
    open: async function () {
      promptEl.textContent = '-';
      cachedEl.textContent = '-';
      completionEl.textContent = '-';
      costCardEl.classList.add('hidden');
      tableBodyEl.innerHTML = '';
      try {
        await loadSummary();
      } catch (error) {
        var row = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 6;
        td.textContent = '加载失败：' + error.message;
        row.appendChild(td);
        tableBodyEl.appendChild(row);
      }
      loadCostCard();
      loadChart();
    }
  };
})();
