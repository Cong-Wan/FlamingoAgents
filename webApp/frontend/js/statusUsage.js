/*
Author: wilbur
Version: 1.1
Date: 2026-09-07
Description: DOM-free status-bar usage helpers: token normalization, three-field dominance, live cost merge, Python-aligned context percent, and display formatting.
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
    copyMeta: copyMeta
  };
})(typeof window !== 'undefined' ? window : globalThis);
