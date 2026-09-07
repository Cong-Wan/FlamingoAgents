/*
Author: wilbur
Version: 1.6
Date: 2026-09-07
Description: 会话状态栏（迭代二方案 §4.2）：composer 下方两行——workDir+git 分支、tokens/费用/窗口占用。
             刷新时机（D7）：chatView.open、流终态、/model 切换后由调用方触发 refresh()，不轮询。
             v1.1 用量行文案精简：去掉 in/out 字样，窗口改为「百分比 / 上下文长度」。
             v1.2 ↑↓⚡ 改读 lastUsage（最近一轮增量）；百分比改使用率 contextUsedPercent；$ 仍为会话累计。
             v1.3 口径对齐 pi（statusBarUsageFixPlan P2/D1-A）：↑↓⚡ 改读 usage（会话累计），↑=max(0,prompt−cached) 减法归一化，
             三者互不重叠、↑+⚡=总输入；lastUsage 字段契约保留但状态栏不再使用。
             v1.4 消费 usageUpdate：按 session/generation/requestId/usageRevision 合并；无快照 pending+单飞；权威 GET 可降价且使更早请求失效。
             v1.5 普通 refresh 复用同 session/generation 在途请求（含权威）；pending 合并保留已知费用/context 且费用单调。
             v1.6 通过支配规则的 pending（含三项相等）均 ++usageRevision，相等帧可更新 context/cost。
*/
(function () {
  'use strict';

  var containerEl = document.getElementById('composerStatus');
  var locationEl = document.getElementById('statusLocation');
  var usageEl = document.getElementById('statusUsage');
  var helpers = window.statusUsage;

  var snapshotSessionId = null;
  var snapshot = null;
  var pendingUsageUpdate = null;
  var pendingRevision = 0;
  var usageRevision = 0;
  var generation = 0;
  var requestId = 0;
  var inFlight = null;

  function formatCompact(num) {
    return helpers.formatCompact(num);
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
    var counts = helpers.usageDisplayCounts(data.usage);
    var used = helpers.contextUsedPercent(data.contextTokens, data.contextWindow);
    var usedText = used === null ? '-' : used + '%';
    var windowSize = data.contextWindow ? formatCompact(data.contextWindow) : '-';
    var segments = [
      '↑ ' + formatCompact(counts.prompt),
      '↓ ' + formatCompact(counts.completion),
      '⚡ ' + formatCompact(counts.cached) + ' cached',
      helpers.formatCost(data.cost),
      usedText + ' / ' + windowSize
    ];
    segments.forEach(function (text, index) {
      if (index > 0) appendSpan(usageEl, ' · ', 'status-sep');
      appendSpan(usageEl, text, null);
    });
  }

  function paintSnapshot() {
    if (!snapshot || snapshotSessionId !== window.appStore.currentSessionId) return;
    renderLocation(snapshot);
    renderUsage(snapshot);
    containerEl.classList.remove('hidden');
  }

  function cloneStatus(data) {
    var next = {
      workDir: data && data.workDir,
      gitBranch: data && data.gitBranch,
      providerId: data && data.providerId,
      modelId: data && data.modelId,
      contextWindow: data && data.contextWindow,
      usage: helpers.readUsage(data && data.usage),
      contextTokens: helpers.toNonNegNumber(data && data.contextTokens) || 0,
      cost: Number.isFinite(data && data.cost) ? data.cost : 0
    };
    return next;
  }

  function hasCurrentSnapshot(sessionId) {
    return !!(snapshot && snapshotSessionId === sessionId);
  }

  function mergeMonotonicUsage(target, data) {
    var incomingUsage = helpers.normalizeUsage(data && data.usage);
    if (!incomingUsage) return;
    if (!helpers.shouldAcceptUsage(incomingUsage, target.usage)) return;
    target.usage = incomingUsage;
    var context = helpers.toNonNegNumber(data && data.contextTokens);
    if (context !== null) target.contextTokens = context;
    if (Number.isFinite(data && data.cost)) {
      target.cost = Math.max(helpers.finiteCost(target.cost), data.cost);
    }
  }

  function replaceUsageFields(target, data) {
    var incomingUsage = helpers.normalizeUsage(data && data.usage) || helpers.readUsage(data && data.usage);
    target.usage = incomingUsage;
    var context = helpers.toNonNegNumber(data && data.contextTokens);
    if (context !== null) target.contextTokens = context;
    if (Number.isFinite(data && data.cost)) target.cost = data.cost;
    else if (data && data.cost === null) target.cost = 0;
  }

  function applyMeta(target, data) {
    helpers.copyMeta(target, data || {});
  }

  function pendingDominates(candidate, currentPending) {
    if (!currentPending) return true;
    if (helpers.shouldAcceptUsage(candidate.usage, currentPending.usage)) return true;
    return false;
  }

  function storePending(data) {
    var incomingUsage = helpers.normalizeUsage(data && data.usage);
    if (!incomingUsage) return false;
    if (!pendingDominates(data, pendingUsageUpdate)) return false;
    var previous = pendingUsageUpdate;
    var nextContext = previous ? previous.contextTokens : data.contextTokens;
    var nextCost = previous ? previous.cost : data.cost;
    var context = helpers.toNonNegNumber(data.contextTokens);
    if (context !== null) nextContext = context;
    if (Number.isFinite(data.cost)) {
      nextCost = helpers.liveCost(Number.isFinite(nextCost) ? nextCost : undefined, data.cost);
    }
    pendingUsageUpdate = {
      usage: incomingUsage,
      stepUsage: data.stepUsage,
      contextTokens: nextContext,
      cost: nextCost
    };
    pendingRevision = ++usageRevision;
    return true;
  }

  function applyIncomingUsage(data) {
    if (!helpers.isValidUsage(data && data.usage)) return false;
    var incoming = helpers.normalizeUsage(data.usage);
    if (!helpers.shouldAcceptUsage(incoming, snapshot.usage)) return false;
    snapshot.usage = incoming;
    var context = helpers.toNonNegNumber(data.contextTokens);
    if (context !== null) snapshot.contextTokens = context;
    if (Number.isFinite(data.cost)) {
      snapshot.cost = helpers.liveCost(snapshot.cost, data.cost);
    }
    usageRevision += 1;
    return true;
  }

  function consumePending(startRevision, authoritative, usageUnchanged) {
    if (!pendingUsageUpdate) return;
    if (pendingRevision > startRevision) {
      applyIncomingUsage(pendingUsageUpdate);
      pendingUsageUpdate = null;
      pendingRevision = 0;
      return;
    }
    if (authoritative && usageUnchanged) {
      pendingUsageUpdate = null;
      pendingRevision = 0;
      return;
    }
    applyIncomingUsage(pendingUsageUpdate);
    pendingUsageUpdate = null;
    pendingRevision = 0;
  }

  function identityMatches(captured) {
    return captured.sessionId === window.appStore.currentSessionId
      && captured.generation === generation
      && captured.requestId === requestId;
  }

  function resetForSession(sessionId) {
    snapshotSessionId = sessionId || null;
    snapshot = null;
    pendingUsageUpdate = null;
    pendingRevision = 0;
    usageRevision = 0;
    inFlight = null;
    generation += 1;
    if (!sessionId) {
      containerEl.classList.add('hidden');
    }
  }

  function commitStatusResponse(data, captured) {
    if (!identityMatches(captured)) return;
    var sessionId = captured.sessionId;
    var hadSnapshot = hasCurrentSnapshot(sessionId);
    var usageUnchanged = usageRevision === captured.startRevision;
    if (!hadSnapshot) {
      snapshot = cloneStatus(data);
      snapshotSessionId = sessionId;
    }
    applyMeta(snapshot, data);
    if (captured.authoritative && usageUnchanged) {
      replaceUsageFields(snapshot, data);
    } else {
      mergeMonotonicUsage(snapshot, data);
    }
    consumePending(captured.startRevision, captured.authoritative, usageUnchanged);
    paintSnapshot();
  }

  function refresh(options) {
    var sessionId = window.appStore.currentSessionId;
    if (!sessionId) {
      window.statusBar.hide();
      return Promise.resolve();
    }
    var authoritative = !!(options && options.authoritative);
    if (
      !authoritative
      && inFlight
      && inFlight.sessionId === sessionId
      && inFlight.generation === generation
    ) {
      return inFlight.promise;
    }

    requestId += 1;
    var captured = {
      sessionId: sessionId,
      generation: generation,
      requestId: requestId,
      startRevision: usageRevision,
      authoritative: authoritative
    };
    var promise = window.api.getSessionStatus(sessionId).then(function (data) {
      commitStatusResponse(data, captured);
    }).catch(function () {
      if (!identityMatches(captured)) return;
      containerEl.classList.add('hidden');
    }).finally(function () {
      if (!inFlight) return;
      if (
        inFlight.requestId === captured.requestId
        && inFlight.generation === captured.generation
        && inFlight.sessionId === captured.sessionId
        && captured.generation === generation
        && captured.sessionId === window.appStore.currentSessionId
      ) {
        inFlight = null;
      }
    });
    inFlight = {
      sessionId: sessionId,
      generation: captured.generation,
      requestId: captured.requestId,
      authoritative: authoritative,
      promise: promise
    };
    return promise;
  }

  function applyUsageUpdate(data) {
    var sessionId = window.appStore.currentSessionId;
    if (!sessionId) return;
    if (!helpers.isValidUsage(data && data.usage)) return;
    if (!hasCurrentSnapshot(sessionId)) {
      if (!storePending(data)) return;
      refresh();
      return;
    }
    if (!applyIncomingUsage(data)) return;
    paintSnapshot();
  }

  window.statusBar = {
    refresh: refresh,
    applyUsageUpdate: applyUsageUpdate,
    resetForSession: resetForSession,
    hide: function () {
      resetForSession(null);
      containerEl.classList.add('hidden');
    }
  };
})();
