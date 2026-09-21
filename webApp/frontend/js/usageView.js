/*
Author: wilbur
Version: 1.5
Date: 2026-09-21
Description: 用量统计页：一个时间范围下拉框、四张汇总卡、Provider 纵向趋势图；只请求 GET /api/usage?period=...，旧响应不得覆盖新选择。
*/
(function () {
  'use strict';

  var promptEl = document.getElementById('usagePrompt');
  var cachedEl = document.getElementById('usageCached');
  var completionEl = document.getElementById('usageCompletion');
  var costEl = document.getElementById('usageCost');
  var rangeTextEl = document.getElementById('usageRangeText');
  var timeZoneEl = document.getElementById('usageTimeZone');
  var callCountEl = document.getElementById('usageCallCount');
  var qualityEl = document.getElementById('usageQualityNotice');
  var statusEl = document.getElementById('usageStatus');
  var providerListEl = document.getElementById('providerList');
  var periodSelect = document.getElementById('periodSelect');
  var tooltip = null;
  var requestGeneration = 0;
  var abortController = null;
  var currentPeriod = 'last7Days';
  var currentSnapshot = null;
  var resizeTimer = null;
  var tokenColors = {
    input: '#a9c4ff',
    cached: '#648fdf',
    output: '#2858ad'
  };

  function formatNumber(value) {
    return Math.round(value || 0).toLocaleString('zh-CN');
  }

  function formatCompact(value) {
    var number = Number(value || 0);
    if (number >= 1000000) return (number / 1000000).toFixed(number >= 10000000 ? 0 : 1) + 'M';
    if (number >= 1000) return (number / 1000).toFixed(number >= 100000 ? 0 : 1) + 'k';
    return String(Math.round(number));
  }

  function formatCost(totals) {
    if (!totals || totals.costStatus === 'unavailable' || totals.costNanoUsd == null) return '—';
    var usd = Number(totals.costNanoUsd) / 1000000000;
    if (totals.costStatus === 'complete' && usd === 0) return '$0.0000';
    return '$' + usd.toFixed(usd >= 10 ? 2 : 4);
  }

  function formatRange(text) {
    if (!text) return '-';
    return String(text).replace('T', ' ').replace(/\+\d{2}:\d{2}$/, '');
  }

  function emptyTotals() {
    return {
      callCount: 0,
      promptTokens: 0,
      cachedTokens: 0,
      completionTokens: 0,
      totalTokens: 0,
      costNanoUsd: 0,
      costStatus: 'complete',
      unpricedCallCount: 0,
      unpricedTokens: 0
    };
  }

  function inputTokens(totals) {
    return Math.max(0, (totals.promptTokens || 0) - (totals.cachedTokens || 0));
  }

  function createElement(tagName, className, text) {
    var element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function ensureTooltip() {
    if (tooltip) return tooltip;
    tooltip = createElement('div', 'usage-tooltip');
    tooltip.setAttribute('role', 'tooltip');
    tooltip.setAttribute('aria-hidden', 'true');
    document.body.appendChild(tooltip);
    return tooltip;
  }

  function hideTooltip() {
    if (!tooltip) return;
    tooltip.classList.remove('visible');
    tooltip.setAttribute('aria-hidden', 'true');
  }

  function setStatus(kind, message, retryable) {
    statusEl.className = 'usage-status' + (kind ? ' ' + kind : '');
    statusEl.replaceChildren();
    if (!message) {
      statusEl.classList.add('hidden');
      return;
    }
    statusEl.classList.remove('hidden');
    statusEl.appendChild(document.createTextNode(message));
    if (retryable) {
      var button = createElement('button', 'btn usage-retry', '重试');
      button.type = 'button';
      button.addEventListener('click', function () { loadPeriod(currentPeriod); });
      statusEl.appendChild(button);
    }
  }

  function paintSummary(data) {
    var totals = data.totals || emptyTotals();
    promptEl.textContent = formatNumber(totals.promptTokens);
    cachedEl.textContent = formatNumber(totals.cachedTokens);
    completionEl.textContent = formatNumber(totals.completionTokens);
    costEl.textContent = formatCost(totals);
    rangeTextEl.textContent = formatRange(data.startAt) + ' ～ ' + formatRange(data.endAt);
    timeZoneEl.textContent = data.timeZone || '';
    callCountEl.textContent = '已落账 ' + formatNumber(totals.callCount) + ' 次调用';
  }

  function qualityText(quality) {
    if (!quality) return '';
    var parts = [];
    if (quality.legacyInferredRecords || quality.legacyUnverifiedRecords) {
      parts.push('含 ' + formatNumber((quality.legacyInferredRecords || 0) + (quality.legacyUnverifiedRecords || 0)) + ' 条历史迁移记录');
    }
    if (quality.migratedPriceRecords) {
      parts.push(formatNumber(quality.migratedPriceRecords) + ' 条使用迁移时价格估算');
    }
    if (quality.unknownPriceRecords) {
      parts.push(formatNumber(quality.unknownPriceRecords) + ' 条费用未知');
    }
    return parts.join('；');
  }

  function paintQuality(data) {
    var text = qualityText(data.quality);
    qualityEl.textContent = text;
    qualityEl.classList.toggle('visible', Boolean(text));
  }

  function niceMaximum(value) {
    if (value <= 0) return 1000;
    var power = Math.pow(10, Math.floor(Math.log10(value)));
    var normalized = value / power;
    var step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return step * power;
  }

  function drawChart(canvas, provider) {
    var cssWidth = Math.max(280, Math.round(canvas.getBoundingClientRect().width || canvas.parentElement.clientWidth || 800));
    var cssHeight = parseInt(getComputedStyle(canvas).height, 10) || 300;
    var deviceRatio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(cssWidth * deviceRatio);
    canvas.height = Math.round(cssHeight * deviceRatio);
    var context = canvas.getContext('2d');
    context.setTransform(deviceRatio, 0, 0, deviceRatio, 0, 0);
    context.clearRect(0, 0, cssWidth, cssHeight);
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, cssWidth, cssHeight);

    var buckets = provider.buckets || [];
    var modelIds = (provider.models || []).map(function (model) { return model.modelId; });
    var plotLeft = cssWidth < 520 ? 48 : 58;
    var plotRight = cssWidth - 12;
    var plotTop = 16;
    var plotBaseline = cssHeight - 48;
    var plotHeight = plotBaseline - plotTop;
    var plotWidth = plotRight - plotLeft;
    var maximumValue = 0;
    buckets.forEach(function (bucket) {
      (bucket.models || []).forEach(function (cell) {
        maximumValue = Math.max(maximumValue, (cell.totals && cell.totals.totalTokens) || 0);
      });
    });
    var maximum = niceMaximum(maximumValue * 1.08);
    context.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
    context.textBaseline = 'middle';
    for (var gridIndex = 0; gridIndex <= 4; gridIndex += 1) {
      var ratio = gridIndex / 4;
      var y = plotTop + plotHeight * ratio;
      context.strokeStyle = gridIndex === 4 ? '#dcdce2' : '#eeeef2';
      context.beginPath();
      context.moveTo(plotLeft, Math.round(y) + 0.5);
      context.lineTo(plotRight, Math.round(y) + 0.5);
      context.stroke();
      context.fillStyle = '#8e8e96';
      context.textAlign = 'right';
      context.fillText(formatCompact(maximum * (1 - ratio)), plotLeft - 7, y);
    }

    var groupStep = plotWidth / Math.max(1, buckets.length);
    var modelCount = Math.max(1, modelIds.length);
    var groupInnerWidth = Math.max(2, groupStep * 0.72);
    var modelGap = modelCount > 1 ? Math.min(3, groupInnerWidth * 0.1) : 0;
    var barWidth = Math.max(1, Math.min(18, (groupInnerWidth - modelGap * (modelCount - 1)) / modelCount));
    var barsWidth = barWidth * modelCount + modelGap * (modelCount - 1);
    var unit = provider.bucketUnit || 'day';
    var targetLabels = Math.max(4, Math.floor(plotWidth / (unit === 'hour' ? 52 : 58)));
    var labelStep = Math.max(1, Math.ceil(buckets.length / targetLabels));

    buckets.forEach(function (bucket, intervalIndex) {
      var centerX = plotLeft + groupStep * (intervalIndex + 0.5);
      var groupLeft = centerX - barsWidth / 2;
      var isLabel = intervalIndex % labelStep === 0 || intervalIndex === buckets.length - 1;
      if (isLabel) {
        context.strokeStyle = '#f0f1f5';
        context.beginPath();
        context.moveTo(centerX + 0.5, plotTop);
        context.lineTo(centerX + 0.5, plotBaseline);
        context.stroke();
      }
      modelIds.forEach(function (modelId, modelIndex) {
        var cell = (bucket.models || []).find(function (item) { return item.modelId === modelId; });
        var totals = (cell && cell.totals) || emptyTotals();
        var x = groupLeft + modelIndex * (barWidth + modelGap);
        var currentY = plotBaseline;
        [
          ['input', inputTokens(totals)],
          ['cached', totals.cachedTokens || 0],
          ['output', totals.completionTokens || 0]
        ].forEach(function (entry) {
          var height = entry[1] / maximum * plotHeight;
          if (height <= 0) return;
          currentY -= height;
          context.fillStyle = tokenColors[entry[0]];
          context.fillRect(x, currentY, barWidth, Math.max(1, height));
        });
      });
      if (isLabel) {
        context.fillStyle = '#85858d';
        context.textAlign = 'center';
        context.textBaseline = 'alphabetic';
        context.fillText(bucket.label || '', centerX, plotBaseline + 25);
      }
    });

    canvas._chartState = {
      provider: provider,
      modelIds: modelIds,
      buckets: buckets,
      plotLeft: plotLeft,
      plotRight: plotRight,
      plotTop: plotTop,
      plotBaseline: plotBaseline,
      groupStep: groupStep,
      barsWidth: barsWidth,
      barWidth: barWidth,
      modelGap: modelGap
    };
  }

  function tooltipRow(label, value, color) {
    var row = createElement('div', 'tooltip-row');
    var swatch = createElement('span', 'tooltip-swatch');
    swatch.style.background = color || 'transparent';
    row.appendChild(swatch);
    row.appendChild(document.createTextNode(label));
    row.appendChild(createElement('strong', '', value));
    return row;
  }

  function showTooltip(event, state, intervalIndex, modelIndex) {
    var provider = state.provider;
    var modelId = state.modelIds[modelIndex];
    var bucket = state.buckets[intervalIndex];
    var cell = (bucket.models || []).find(function (item) { return item.modelId === modelId; });
    var totals = (cell && cell.totals) || emptyTotals();
    var box = ensureTooltip();
    box.replaceChildren();
    box.appendChild(createElement('div', 'tooltip-title', provider.providerId + ' / ' + modelId));
    box.appendChild(createElement('div', 'tooltip-subtitle', bucket.label || ''));
    box.appendChild(tooltipRow('模型调用', formatNumber(totals.callCount) + ' 次', '#8292ad'));
    box.appendChild(tooltipRow('输入', formatNumber(inputTokens(totals)) + ' tokens', tokenColors.input));
    box.appendChild(tooltipRow('缓存', formatNumber(totals.cachedTokens) + ' tokens', tokenColors.cached));
    box.appendChild(tooltipRow('输出', formatNumber(totals.completionTokens) + ' tokens', tokenColors.output));
    var total = tooltipRow('合计', formatNumber(totals.totalTokens) + ' tokens', '#ffffff');
    total.classList.add('tooltip-total');
    box.appendChild(total);
    box.appendChild(tooltipRow('估算费用', formatCost(totals), '#8292ad'));
    box.classList.add('visible');
    box.setAttribute('aria-hidden', 'false');
    var left = event.clientX + 14;
    var top = event.clientY + 14;
    var width = box.offsetWidth;
    var height = box.offsetHeight;
    box.style.left = Math.max(8, Math.min(left, window.innerWidth - width - 10)) + 'px';
    box.style.top = Math.max(8, Math.min(top, window.innerHeight - height - 10)) + 'px';
  }

  function bindChartTooltip(canvas) {
    canvas.addEventListener('pointermove', function (event) {
      var state = canvas._chartState;
      if (!state || event.offsetX < state.plotLeft || event.offsetX > state.plotRight ||
          event.offsetY < state.plotTop || event.offsetY > state.plotBaseline) {
        hideTooltip();
        canvas.style.cursor = 'default';
        return;
      }
      var intervalIndex = Math.floor((event.offsetX - state.plotLeft) / state.groupStep);
      intervalIndex = Math.max(0, Math.min(state.buckets.length - 1, intervalIndex));
      var centerX = state.plotLeft + state.groupStep * (intervalIndex + 0.5);
      var groupLeft = centerX - state.barsWidth / 2;
      var localX = event.offsetX - groupLeft;
      var slotWidth = state.barWidth + state.modelGap;
      var modelIndex = Math.floor(localX / Math.max(1, slotWidth));
      if (modelIndex < 0 || modelIndex >= state.modelIds.length ||
          localX < modelIndex * slotWidth - 3 ||
          localX > modelIndex * slotWidth + state.barWidth + 3) {
        hideTooltip();
        canvas.style.cursor = 'default';
        return;
      }
      canvas.style.cursor = 'crosshair';
      showTooltip(event, state, intervalIndex, modelIndex);
    });
    canvas.addEventListener('pointerleave', hideTooltip);
  }

  function renderLegend(panel, provider) {
    var modelGroup = createElement('div', 'legend-group');
    modelGroup.appendChild(createElement('span', 'legend-title', '模型'));
    (provider.models || []).forEach(function (model, index) {
      var item = createElement('span', 'legend-item');
      item.appendChild(createElement('span', 'model-index', 'M' + (index + 1)));
      item.appendChild(document.createTextNode(model.modelId));
      modelGroup.appendChild(item);
    });
    var tokenGroup = createElement('div', 'legend-group');
    tokenGroup.appendChild(createElement('span', 'legend-title', 'Tokens'));
    [['input', '输入'], ['cached', '缓存'], ['output', '输出']].forEach(function (entry) {
      var item = createElement('span', 'legend-item');
      var swatch = createElement('span', 'token-swatch');
      swatch.style.background = tokenColors[entry[0]];
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(entry[1]));
      tokenGroup.appendChild(item);
    });
    panel.appendChild(modelGroup);
    panel.appendChild(tokenGroup);
  }

  function createProviderCard(provider) {
    var card = createElement('article', 'provider-card');
    var head = createElement('header', 'provider-head');
    var main = createElement('div', 'provider-main');
    var titleRow = createElement('div', 'provider-title-row');
    titleRow.appendChild(createElement('h2', 'provider-title', provider.providerId));
    titleRow.appendChild(createElement('span', 'provider-chip', (provider.models || []).length + ' 个模型'));
    main.appendChild(titleRow);
    main.appendChild(createElement(
      'div',
      'provider-meta',
      formatNumber(provider.totals.callCount) + ' 次调用 · ' + formatNumber(provider.totals.totalTokens) + ' tokens'
    ));
    head.appendChild(main);
    var metrics = createElement('div', 'provider-metrics');
    [
      ['Prompt', provider.totals.promptTokens],
      ['Cached', provider.totals.cachedTokens],
      ['Completion', provider.totals.completionTokens]
    ].forEach(function (entry) {
      var metric = createElement('span', '', entry[0]);
      metric.appendChild(createElement('b', '', formatNumber(entry[1])));
      metrics.appendChild(metric);
    });
    var costMetric = createElement('span', '', '估算费用');
    costMetric.appendChild(createElement('b', '', formatCost(provider.totals)));
    metrics.appendChild(costMetric);
    head.appendChild(metrics);
    card.appendChild(head);
    var legend = createElement('div', 'legend-panel');
    renderLegend(legend, provider);
    card.appendChild(legend);
    var wrap = createElement('div', 'chart-wrap');
    var canvas = createElement('canvas', 'usage-chart');
    canvas.setAttribute('aria-label', provider.providerId + ' 用量趋势');
    wrap.appendChild(canvas);
    card.appendChild(wrap);
    providerListEl.appendChild(card);
    drawChart(canvas, provider);
    bindChartTooltip(canvas);
  }

  function renderProviders(data) {
    providerListEl.replaceChildren();
    hideTooltip();
    var providers = data.providers || [];
    if (!providers.length) {
      setStatus('empty', '当前区间暂无已落账用量', false);
      return;
    }
    setStatus('', '', false);
    providers.forEach(createProviderCard);
  }

  function paint(data) {
    currentSnapshot = data;
    paintSummary(data);
    paintQuality(data);
    renderProviders(data);
  }

  function redraw() {
    if (!currentSnapshot) return;
    var canvases = providerListEl.querySelectorAll('canvas.usage-chart');
    var providers = currentSnapshot.providers || [];
    canvases.forEach(function (canvas, index) {
      if (providers[index]) drawChart(canvas, providers[index]);
    });
  }

  async function loadPeriod(period) {
    currentPeriod = period || 'last7Days';
    periodSelect.value = currentPeriod;
    requestGeneration += 1;
    var generation = requestGeneration;
    if (abortController) abortController.abort();
    abortController = typeof AbortController === 'function' ? new AbortController() : null;
    promptEl.textContent = '-';
    cachedEl.textContent = '-';
    completionEl.textContent = '-';
    costEl.textContent = '-';
    providerListEl.replaceChildren();
    qualityEl.classList.remove('visible');
    qualityEl.textContent = '';
    setStatus('loading', '正在加载用量…', false);
    try {
      var data = await window.api.getUsage(currentPeriod, abortController ? { signal: abortController.signal } : {});
      if (generation !== requestGeneration) return;
      paint(data);
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (generation !== requestGeneration) return;
      setStatus('error', '加载失败：' + (error && error.message ? error.message : error), true);
    }
  }

  periodSelect.addEventListener('change', function () {
    loadPeriod(periodSelect.value);
  });
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redraw, 80);
  });

  window.usageView = {
    open: function () {
      return loadPeriod(periodSelect.value || 'last7Days');
    }
  };
})();
