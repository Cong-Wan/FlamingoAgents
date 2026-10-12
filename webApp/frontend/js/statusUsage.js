/*
Author: wilbur
Version: 1.7
Date: 2026-10-12
Description: DOM-free status-bar usage helpers: token normalization, three-field dominance, live cost merge, Python-aligned context percent, and display formatting.
             v1.2（latencyMetricsDisplayPlan T2.1）：新增 formatElapsed/readTimings/formatSpeedLine（TTFT/生成/TPS 速度行纯函数，供状态栏与块脚标共用）。
             v1.3（latencyUiFixPlan §B）：formatSpeedLine 改两形态互斥——TTFT 有效时 TTFT/Output/TPS，
             TTFT 缺失或越界时 TTFT—/Total/TPS—（不用 Total 冒充 Output）；TPS 补 tok/s 单位。
             v1.4（thinkingPhaseMetricsPlan §B）：拆思考阶段——readTimings 读 reasoningStart/EndMs，
             D5 边界防御（无效/越界回退无思考）；速度行有思考时 TTFT/Thinking/Output/TPS（Output=正文阶段，
             TPS 分母同步 v2）。
             v1.5（thinkingBlockMetricsPlan）：删除 formatStepElapsedLine（块脚标回退 Elapsed-only，思考耗时落思考块）；thinkingMs 正式导出供历史回放。
             v1.6（latencyDisplayImplementationPlan §3.1）：formatSpeedLine 收窄为 TTFT/TPS-only（已确认 demo 口径）；新增 validLatencyPair（完整同源对子才可替换 TPS 显示）。
             v1.7：TPS 口径 v3 修复分子分母不对称——分母改为 duration−ttfb（首输出之后的平均产出速率，
             分子 completionTokens 含思考 token，两侧对称）；旧口径（思考结束后才计时）在思考长时 TPS 虚高。
*/
(function (root) {
  'use strict';

  var tokenKeys = ['promptTokens', 'cachedTokens', 'completionTokens'];
  var metaKeys = ['workDir', 'gitBranch', 'providerId', 'modelId', 'contextWindow'];

  function toFiniteNumber(value) {
    if (typeof value === 'boolean' || value === null || value === undefined || value === '') return null;
    var num = typeof value === 'number' ? value : Number(value);
    if (!Number.isFinite(num)) return null;
    return num;
  }

  function toNonNegInt(value) {
    var num = toFiniteNumber(value);
    if (num === null || num < 0) return null;
    return Math.round(num);
  }

  function toNonNegNumber(value) {
    var num = toFiniteNumber(value);
    if (num === null || num < 0) return null;
    return num;
  }

  function isValidUsage(usage) {
    if (!usage || typeof usage !== 'object' || Array.isArray(usage)) return false;
    for (var i = 0; i < tokenKeys.length; i++) {
      var key = tokenKeys[i];
      if (!Object.prototype.hasOwnProperty.call(usage, key)) return false;
      if (toNonNegInt(usage[key]) === null) return false;
    }
    return true;
  }

  function normalizeUsage(usage) {
    if (!isValidUsage(usage)) return null;
    return {
      promptTokens: toNonNegInt(usage.promptTokens),
      cachedTokens: toNonNegInt(usage.cachedTokens),
      completionTokens: toNonNegInt(usage.completionTokens)
    };
  }

  function readUsage(usage) {
    var source = usage && typeof usage === 'object' && !Array.isArray(usage) ? usage : {};
    return {
      promptTokens: toNonNegInt(source.promptTokens) || 0,
      cachedTokens: toNonNegInt(source.cachedTokens) || 0,
      completionTokens: toNonNegInt(source.completionTokens) || 0
    };
  }

  function usageDominates(candidate, current) {
    var left = normalizeUsage(candidate);
    var right = readUsage(current);
    if (!left) return false;
    return left.promptTokens >= right.promptTokens
      && left.cachedTokens >= right.cachedTokens
      && left.completionTokens >= right.completionTokens;
  }

  function shouldAcceptUsage(candidate, current) {
    return usageDominates(candidate, current);
  }

  function usageDisplayCounts(usage) {
    var normalized = readUsage(usage);
    return {
      prompt: Math.max(0, normalized.promptTokens - normalized.cachedTokens),
      completion: normalized.completionTokens,
      cached: normalized.cachedTokens
    };
  }

  function contextUsedPercent(contextTokens, contextWindow) {
    var tokens = toNonNegNumber(contextTokens);
    var windowSize = toFiniteNumber(contextWindow);
    if (tokens === null || windowSize === null || windowSize <= 0) return null;
    var raw = (tokens / windowSize) * 100;
    if (raw < 0) raw = 0;
    if (raw > 100) raw = 100;
    // Python round-half-even: binary-exact *.25 goes down; *.75 and toFixed already match.
    if (Number.isInteger(raw * 4) && !Number.isInteger(raw * 2)) {
      var tenths = Math.floor(raw * 10 + 1e-12);
      if (tenths % 2 === 0) return tenths / 10;
    }
    return Number(raw.toFixed(1));
  }

  function formatCompact(num) {
    var value = toFiniteNumber(num);
    if (value === null) return '0';
    if (value >= 1000000) return (value / 1000000).toFixed(1) + 'M';
    if (value >= 1000) return (value / 1000).toFixed(1) + 'k';
    return String(Math.round(value));
  }

  function formatCost(cost) {
    if (!Number.isFinite(cost) || cost <= 0) return '$-';
    return '$' + cost.toFixed(4);
  }

  function finiteCost(cost) {
    return Number.isFinite(cost) ? cost : 0;
  }

  function liveCost(currentCost, incomingCost) {
    if (!Number.isFinite(incomingCost)) return Number.isFinite(currentCost) ? currentCost : 0;
    return Math.max(finiteCost(currentCost), incomingCost);
  }

  function copyMeta(target, source) {
    if (!target || !source || typeof source !== 'object') return target;
    metaKeys.forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(source, key)) target[key] = source[key];
    });
    return target;
  }

  function formatElapsed(ms) {
    var value = toFiniteNumber(ms);
    if (value === null || value < 0) return null;
    var seconds = value / 1000;
    if (seconds < 10) return seconds.toFixed(1) + 's';
    if (seconds < 60) return Math.round(seconds) + 's';
    var minutes = Math.floor(seconds / 60);
    var rest = Math.round(seconds - minutes * 60);
    if (rest === 60) { minutes += 1; rest = 0; }
    return minutes + 'm ' + rest + 's';
  }

  function readTimings(timings) {
    if (!timings || typeof timings !== 'object') return null;
    var ttfb = toNonNegNumber(timings.ttfbMs);
    var duration = toNonNegNumber(timings.durationMs);
    if (ttfb === null && duration === null) return null;
    return {
      ttfbMs: ttfb,
      durationMs: duration,
      reasoningStartMs: toNonNegNumber(timings.reasoningStartMs),
      reasoningEndMs: toNonNegNumber(timings.reasoningEndMs)
    };
  }

  // v1.4（thinkingPhaseMetricsPlan D5）：思考段有效性——四字段齐全、start<end、end<=duration 才启用，否则回退无思考口径
  function thinkingMs(timings) {
    if (
      timings.durationMs === null
      || timings.reasoningStartMs === null || timings.reasoningEndMs === null
      || timings.reasoningStartMs >= timings.reasoningEndMs
      || timings.reasoningEndMs > timings.durationMs
    ) return null;
    return timings.reasoningEndMs - timings.reasoningStartMs;
  }

  // v1.7：TPS 口径 v3——分母 = duration − ttfb（首个增量到流收尾，含思考输出时段），
  //   分子 = 本次 completionTokens（含思考 token）：两侧对称，修复思考长时 TPS 虚高（旧口径分母只量正文）。
  //   语义：首输出之后的平均产出速率（对齐 vLLM TPOT 排除 TTFT 的业界做法）；ttfb 无效时退回全时长。
  function formatSpeedLine(lastTimings, stepUsage) {
    var timings = readTimings(lastTimings);
    if (!timings || timings.durationMs === null) return null;
    var tokens = toNonNegNumber(stepUsage && stepUsage.completionTokens);
    var ttfbValid = timings.ttfbMs !== null && timings.ttfbMs >= 0 && timings.ttfbMs <= timings.durationMs;
    var outputMs = ttfbValid ? Math.max(0, timings.durationMs - timings.ttfbMs) : timings.durationMs;
    var tpsText = '—';
    if (tokens !== null && tokens > 0 && outputMs >= 100) {
      var tps = tokens / (outputMs / 1000);
      tpsText = (tps >= 100 ? tps.toFixed(0) : tps.toFixed(1)) + ' tok/s';
    }
    return 'TTFT ' + (ttfbValid ? formatElapsed(timings.ttfbMs) : '—') + ' · TPS ' + tpsText;
  }

  // v1.6（latencyDisplayImplementationPlan §3.1）：完整同源对子校验——仅两者同时有效才可替换 TPS 显示；
  //   半截 timings（无 durationMs）或非正 completionTokens 不算完整对子（累计用量更新不受此限）。
  function validLatencyPair(lastTimings, stepUsage) {
    var timings = readTimings(lastTimings);
    if (!timings || timings.durationMs === null) return false;
    var tokens = toNonNegNumber(stepUsage && stepUsage.completionTokens);
    return tokens !== null && tokens > 0;
  }

  root.statusUsage = {
    toNonNegNumber: toNonNegNumber,
    isValidUsage: isValidUsage,
    normalizeUsage: normalizeUsage,
    readUsage: readUsage,
    shouldAcceptUsage: shouldAcceptUsage,
    usageDisplayCounts: usageDisplayCounts,
    contextUsedPercent: contextUsedPercent,
    formatCompact: formatCompact,
    formatCost: formatCost,
    finiteCost: finiteCost,
    liveCost: liveCost,
    copyMeta: copyMeta,
    formatElapsed: formatElapsed,
    readTimings: readTimings,
    thinkingMs: thinkingMs,
    formatSpeedLine: formatSpeedLine,
    validLatencyPair: validLatencyPair
  };
})(typeof window !== 'undefined' ? window : globalThis);
