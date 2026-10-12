/*
Author: wilbur
Version: 1.11
Date: 2026-10-12
Description: 会话状态栏（迭代二方案 §4.2）：composer 下方两行——workDir+git 分支、tokens/费用/窗口占用。
             刷新时机（D7）：chatView.open、流终态、/model 切换后由调用方触发 refresh()，不轮询。
             v1.1 用量行文案精简：去掉 in/out 字样，窗口改为「百分比 / 上下文长度」。
             v1.2 ↑↓⚡ 改读 lastUsage（最近一轮增量）；百分比改使用率 contextUsedPercent；$ 仍为会话累计。
             v1.3 口径对齐 pi（statusBarUsageFixPlan P2/D1-A）：↑↓⚡ 改读 usage（会话累计），↑=max(0,prompt−cached) 减法归一化，
             三者互不重叠、↑+⚡=总输入；lastUsage 字段契约保留但状态栏不再使用。
             v1.4 消费 usageUpdate：按 session/generation/requestId/usageRevision 合并；无快照 pending+单飞；权威 GET 可降价且使更早请求失效。
             v1.5 普通 refresh 复用同 session/generation 在途请求（含权威）；pending 合并保留已知费用/context 且费用单调。
             v1.6 通过支配规则的 pending（含三项相等）均 ++usageRevision，相等帧可更新 context/cost。
             v1.7（latencyMetricsDisplayPlan T2.4）：状态栏新增第三行速度行（TTFT/生成/TPS，最近一轮）；
             snapshot 透传 lastTimings/stepUsage，流式帧 timings 有效时更新，缺数据整行隐藏。
             v1.8（latencyUiFixPlan §B）：速度行 title 口径说明；timings/stepUsage 成对替换对称双清
             （缺任一即双置 null，禁止旧时长配新 tokens）；pending 缓存保留 timings。
             v1.9（thinkingPhaseMetricsPlan T3）：速度行 title 同步思考阶段新口径与 TPS v2 提示。
             v1.10（thinkingBlockMetricsPlan §B）：live TTFT——applyLiveTtfb/clearLiveTtfb；首输出即时显示，
             接受帧带有效 timings 时权威接管并清 live；拒帧不清（防真空窗）。
             v1.11（latencyDisplayImplementationPlan §3.1）：已确认 demo 口径——删除独立速度行，
             TTFT/TPS 追加到用量行末尾；废止「成对替换缺一双置 null」——仅完整同源对子才替换（否则保留上一对，
             TPS 不闪 —）；cloneStatus 保留服务端 lastStepUsage；live TTFT 仍用行内占位（…）。
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
  var liveTtfb = { active: false, ms: null, pendingPair: false }; // v1.10：流中即时 TTFT；v1.11：pendingPair=本轮待权威对子

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
    // v1.11：TTFT/TPS 追加到用量行末尾（已确认 demo）；live 首输出占位本次 TPS（…），不拿上一调用 TPS 配本次 TTFT。
    if (data.liveTtfbActive && Number.isFinite(data.liveTtfbMs)) {
      segments.push('TTFT ' + helpers.formatElapsed(data.liveTtfbMs) + ' · TPS …');
    } else if (data.pendingPair) {
      segments.push('TTFT … · TPS …');
    } else {
      var latencyText = helpers.formatSpeedLine(data.lastTimings, data.stepUsage);
      if (latencyText !== null) segments.push(latencyText);
    }
    segments.forEach(function (text, index) {
      if (index > 0) appendSpan(usageEl, ' · ', 'status-sep');
      appendSpan(usageEl, text, null);
    });
    usageEl.title = '最近一次有完整计时和用量的模型调用（非会话累计）：'
      + 'TTFT=请求发出到首个文本/思考增量；'
      + 'TPS=首输出后平均产出速率（completionTokens ÷ 首增量到收尾秒数，含思考产出；不含首包等待）；'
      + '会话切换/重开后从持久化配对恢复。';
  }

  function paintSnapshot() {
    if (!snapshot || snapshotSessionId !== window.appStore.currentSessionId) return;
    renderLocation(snapshot);
    renderUsage({
      usage: snapshot.usage,
      contextTokens: snapshot.contextTokens,
      contextWindow: snapshot.contextWindow,
      cost: snapshot.cost,
      liveTtfbActive: liveTtfb.active,
      liveTtfbMs: liveTtfb.ms,
      pendingPair: liveTtfb.pendingPair,
      lastTimings: snapshot.lastTimings,
      stepUsage: snapshot.lastStepUsage
    });
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
      cost: Number.isFinite(data && data.cost) ? data.cost : 0,
      lastTimings: helpers.readTimings(data && data.lastTimings),
      lastStepUsage: helpers.readUsage(data && data.lastStepUsage)  // v1.11：保留服务端同源对子（不再置 null）
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
      cost: nextCost,
      timings: data.timings  // v1.8：pending 保留 timings（latencyUiFixPlan §B）
    };
    pendingRevision = ++usageRevision;
    return true;
  }

  // v1.11（latencyDisplayImplementationPlan §3.1）：废止 v1.8 成对双清——
  // 仅当帧同时携带有效 timings（readTimings 非 null 且 durationMs 有效）与同源 stepUsage 且完整对子校验通过时，
  // 才替换 lastTimings/lastStepUsage；否则保留上一对（已显示 TTFT/TPS 不闪 —）；累计用量照常更新。
  function applyIncomingUsage(data) {
    if (!helpers.isValidUsage(data && data.usage)) return false;
    var incoming = helpers.normalizeUsage(data.usage);
    if (!helpers.shouldAcceptUsage(incoming, snapshot.usage)) return false;
    snapshot.usage = incoming;
    var context = helpers.toNonNegNumber(data && data.contextTokens);
    if (context !== null) snapshot.contextTokens = context;
    if (Number.isFinite(data && data.cost)) {
      snapshot.cost = helpers.liveCost(snapshot.cost, data.cost);
    }
    var incomingTimings = data && data.timings ? helpers.readTimings(data.timings) : null;
    var timingsComplete = !!(incomingTimings && incomingTimings.durationMs !== null);
    var incomingStep = data && data.stepUsage && typeof data.stepUsage === 'object' ? data.stepUsage : null;
    if (timingsComplete && incomingStep && helpers.validLatencyPair(incomingTimings, incomingStep)) {
      snapshot.lastTimings = incomingTimings;
      snapshot.lastStepUsage = incomingStep;
    }
    // v1.10→v1.11 修订：仅完整 timings（含 durationMs）才权威接管清 live；
    // 半截 timings（只有 ttfb 无 duration）不构成权威帧，不清 live 也不换配对。
    if (timingsComplete) {
      liveTtfb.active = false;
      liveTtfb.pendingPair = false;
      liveTtfb.ms = null;
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
    liveTtfb.active = false;       // v1.11：切会话即清 live 临时态（不依赖 close() 调用顺序）
    liveTtfb.pendingPair = false;
    liveTtfb.ms = null;
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
      // v1.11：权威 GET 同样仅完整对子才替换配对（不得降级回写清掉已显示 TPS）
      var responseTimings = data && data.lastTimings ? helpers.readTimings(data.lastTimings) : null;
      var responseStep = data && data.lastStepUsage && typeof data.lastStepUsage === 'object' ? data.lastStepUsage : null;
      if (responseTimings && responseTimings.durationMs !== null
        && responseStep && helpers.validLatencyPair(responseTimings, responseStep)) {
        snapshot.lastTimings = responseTimings;
        snapshot.lastStepUsage = responseStep;
      }
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

  // v1.10（thinkingBlockMetricsPlan §B）→ v1.11（latencyDisplayImplementationPlan §3.1）：
  // chatView 首输出上报的即时 TTFT；快照未建时缓存待 paint。pendingPair 标记本轮已有输出但权威对子未到
  // （行内显示 TTFT … · TPS …，不用上一调用 TPS 配本次 TTFT）。
  function applyLiveTtfb(ms) {
    liveTtfb.active = true;
    liveTtfb.pendingPair = true;
    liveTtfb.ms = Number.isFinite(ms) ? ms : null;
    if (hasCurrentSnapshot(window.appStore.currentSessionId)) {
      paintSnapshot();
    }
  }

  function clearLiveTtfb() {
    liveTtfb.active = false;
    liveTtfb.pendingPair = false;
    liveTtfb.ms = null;
    if (hasCurrentSnapshot(window.appStore.currentSessionId)) {
      paintSnapshot();
    }
  }

  window.statusBar = {
    refresh: refresh,
    applyUsageUpdate: applyUsageUpdate,
    applyLiveTtfb: applyLiveTtfb,
    clearLiveTtfb: clearLiveTtfb,
    resetForSession: resetForSession,
    hide: function () {
      resetForSession(null);
      containerEl.classList.add('hidden');
    }
  };
})();
