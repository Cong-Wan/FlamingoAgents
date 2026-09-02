/*
Author: wilbur
Version: 1.1
Date: 2026-09-01
Description: Pure prototype-safe subscription merge and race helpers, including strict Provider matching, non-overwrite deep-copy append, discovery guards, account-epoch flight keys, and stale-open commit rejection.
*/
(function (root) {
  'use strict';

  var safeIdPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
  var safeModelIdPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;

  function isDangerousKey(key) {
    return key === '__proto__' || key === 'prototype' || key === 'constructor';
  }

  function isPlainObject(value) {
    if (!value || Object.prototype.toString.call(value) !== '[object Object]') return false;
    var prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function assertSafeObject(value, location) {
    if (!isPlainObject(value)) throw new Error(location + ' 必须是普通对象。');
    Object.keys(value).forEach(function (key) {
      if (isDangerousKey(key)) throw new Error(location + ' 包含危险字段。');
      var child = value[key];
      if (Array.isArray(child)) {
        child.forEach(function (item, index) {
          if (isPlainObject(item)) assertSafeObject(item, location + '[' + index + ']');
        });
      } else if (isPlainObject(child)) {
        assertSafeObject(child, location + '.' + key);
      }
    });
  }

  function deepClone(value) {
    if (Array.isArray(value)) return value.map(deepClone);
    if (isPlainObject(value)) {
      assertSafeObject(value, '对象');
      var clone = {};
      Object.keys(value).forEach(function (key) { clone[key] = deepClone(value[key]); });
      return clone;
    }
    return value;
  }

  function normalizeBaseUrl(rawValue) {
    if (typeof rawValue !== 'string' || !rawValue.trim()) return null;
    try {
      var parsed = new URL(rawValue.trim());
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash) return null;
      var path = parsed.pathname.replace(/\/+$/, '');
      return 'https://' + parsed.hostname.toLowerCase() + (parsed.port ? ':' + parsed.port : '') + path;
    } catch (ignore) {
      return null;
    }
  }

  function validateDiscovery(discovery) {
    if (!isPlainObject(discovery) || !isPlainObject(discovery.providerTemplate)) {
      throw new Error('模型候选响应格式无效。');
    }
    assertSafeObject(discovery, '模型候选响应');
    var template = discovery.providerTemplate;
    if (!safeIdPattern.test(template.suggestedId || '') || isDangerousKey(template.suggestedId)) {
      throw new Error('建议 providerId 非法。');
    }
    if (template.auth !== 'oauth') throw new Error('订阅候选 auth 必须是 oauth。');
    var expectedBaseUrl = normalizeBaseUrl(template.baseUrl);
    var expected = discovery.provider === 'xai'
      ? { api: 'openai-responses', baseUrl: 'https://api.x.ai/v1' }
      : (discovery.provider === 'openai-codex'
        ? { api: 'openai-codex-responses', baseUrl: 'https://chatgpt.com/backend-api' }
        : null);
    if (!expected || template.api !== expected.api || expectedBaseUrl !== expected.baseUrl) {
      throw new Error('订阅候选 Provider 端点不可信。');
    }
    if (!Array.isArray(template.models) || template.models.length === 0) {
      throw new Error('订阅候选 models 必须是非空数组。');
    }
    var seen = Object.create(null);
    template.models.forEach(function (model) {
      if (!isPlainObject(model) || !safeModelIdPattern.test(model.id || '')) {
        throw new Error('订阅候选模型 id 非法。');
      }
      if (seen[model.id]) throw new Error('订阅候选模型 id 重复。');
      seen[model.id] = true;
    });
    return { template: template, expectedBaseUrl: expectedBaseUrl };
  }

  function matchingProviderIds(config, discovery) {
    if (!isPlainObject(config) || !isPlainObject(config.providers)) return [];
    var validated = validateDiscovery(discovery);
    assertSafeObject(config, '模型工作副本');
    return Object.keys(config.providers).filter(function (providerId) {
      if (isDangerousKey(providerId)) return false;
      var provider = config.providers[providerId];
      return isPlainObject(provider) && provider.api === validated.template.api &&
        provider.auth === 'oauth' && normalizeBaseUrl(provider.baseUrl) === validated.expectedBaseUrl;
    });
  }

  function uniqueProviderId(providers, suggestedId) {
    if (!Object.prototype.hasOwnProperty.call(providers, suggestedId)) return suggestedId;
    var suffix = 2;
    while (Object.prototype.hasOwnProperty.call(providers, suggestedId + suffix)) suffix += 1;
    return suggestedId + suffix;
  }

  function mergeDiscovery(config, discovery, preferredProviderId) {
    var matches = matchingProviderIds(config, discovery);
    var template = discovery.providerTemplate;
    var targetId = null;
    if (matches.length === 1) {
      targetId = matches[0];
    } else if (matches.length > 1) {
      if (typeof preferredProviderId === 'string' && matches.indexOf(preferredProviderId) !== -1) {
        targetId = preferredProviderId;
      } else {
        return {
          ok: false,
          code: 'ambiguous_provider',
          matchingProviderIds: matches.slice(),
        };
      }
    }

    var nextConfig = deepClone(config);
    var createdProvider = false;
    if (targetId === null) {
      targetId = uniqueProviderId(nextConfig.providers, template.suggestedId);
      nextConfig.providers[targetId] = {
        baseUrl: template.baseUrl,
        api: template.api,
        auth: 'oauth',
        headers: deepClone(template.headers || {}),
        models: [],
      };
      createdProvider = true;
    }

    var target = nextConfig.providers[targetId];
    if (!Array.isArray(target.models)) target.models = [];
    var existingIds = Object.create(null);
    target.models.forEach(function (model) {
      if (isPlainObject(model) && typeof model.id === 'string') existingIds[model.id] = true;
    });
    var addedModelIds = [];
    var keptModelIds = [];
    template.models.forEach(function (model) {
      if (existingIds[model.id]) {
        keptModelIds.push(model.id);
        return;
      }
      target.models.push(deepClone(model));
      existingIds[model.id] = true;
      addedModelIds.push(model.id);
    });
    return {
      ok: true,
      config: nextConfig,
      providerId: targetId,
      createdProvider: createdProvider,
      addedModelIds: addedModelIds,
      keptModelIds: keptModelIds,
      matchingProviderIds: matches.slice(),
    };
  }

  function canApplyResult(startRevision, currentRevision, startEpoch, currentEpoch, startGeneration, currentGeneration) {
    return startRevision === currentRevision && startEpoch === currentEpoch &&
      startGeneration === currentGeneration;
  }

  function canCommitOpen(startRevision, currentRevision) {
    return startRevision === currentRevision;
  }

  function flightKey(provider, accountEpoch) {
    return String(provider) + ':' + String(accountEpoch);
  }

  root.subscriptionModels = {
    normalizeBaseUrl: normalizeBaseUrl,
    matchingProviderIds: matchingProviderIds,
    mergeDiscovery: mergeDiscovery,
    canApplyResult: canApplyResult,
    canCommitOpen: canCommitOpen,
    flightKey: flightKey,
  };
})(typeof window !== 'undefined' ? window : globalThis);
