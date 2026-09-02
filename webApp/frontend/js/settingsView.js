/*
Author: wilbur
Version: 1.9
Date: 2026-09-01
Description: Model editor with independent subscription discovery, non-overwrite guarded merge, account-epoch flights, and per-open revision commits so inverse asynchronous reloads cannot restore stale configuration.
*/
(function () {
  'use strict';

  // headers 对象 ↔ 文本域（每行 Key: Value）互转
  function headersToText(headers) {
    if (!headers) return '';
    return Object.keys(headers).map(function (key) { return key + ': ' + headers[key]; }).join('\n');
  }

  function textToHeaders(text) {
    var headers = {};
    (text || '').split('\n').forEach(function (line) {
      var index = line.indexOf(':');
      if (index <= 0) return;
      var key = line.slice(0, index).trim();
      if (key) headers[key] = line.slice(index + 1).trim();
    });
    return headers;
  }

  var tabsEl = document.getElementById('providerTabs');
  var formEl = document.getElementById('providerForm');
  var errorEl = document.getElementById('settingsError');
  var saveButton = document.getElementById('saveModelsButton');
  var resetButton = document.getElementById('resetModelsButton');
  var dirtyHintEl = document.getElementById('settingsDirtyHint');
  var importPiModelsButton = document.getElementById('importPiModelsButton');
  var piImportPanel = document.getElementById('piImportPanel');
  var piImportFile = document.getElementById('piImportFile');
  var piOverwriteModels = document.getElementById('piOverwriteModels');
  var piOverwriteProviderFields = document.getElementById('piOverwriteProviderFields');
  var piOverwriteApiKey = document.getElementById('piOverwriteApiKey');
  var piImportPreviewButton = document.getElementById('piImportPreviewButton');
  var piImportApplyButton = document.getElementById('piImportApplyButton');
  var piImportCancelButton = document.getElementById('piImportCancelButton');
  var piImportReport = document.getElementById('piImportReport');
  var modelAuthModal = document.getElementById('modelAuthModal');
  var modelAuthLoginStatus = document.getElementById('modelAuthLoginStatus');
  var modelAuthLoginUrlRow = document.getElementById('modelAuthLoginUrlRow');
  var modelAuthLoginUrl = document.getElementById('modelAuthLoginUrl');
  var modelAuthDeviceCodeRow = document.getElementById('modelAuthDeviceCodeRow');
  var modelAuthDeviceCode = document.getElementById('modelAuthDeviceCode');
  var modelAuthCountdown = document.getElementById('modelAuthCountdown');
  var modelAuthManualRow = document.getElementById('modelAuthManualRow');
  var modelAuthManualCode = document.getElementById('modelAuthManualCode');
  var modelAuthManualSubmit = document.getElementById('modelAuthManualSubmit');
  var modelAuthLoginError = document.getElementById('modelAuthLoginError');
  var modelAuthLoginCancel = document.getElementById('modelAuthLoginCancel');
  var modelAuthLoginClose = document.getElementById('modelAuthLoginClose');
  var subscriptionAccounts = document.getElementById('subscriptionAccounts');
  var subscriptionDiscoveryNotice = document.getElementById('subscriptionDiscoveryNotice');

  var modelConfig = null;       // GET /api/models 原始返回（编辑工作副本，tab 切换不重拉）
  var fetchedApiKeys = {};      // providerId → GET 返回的 apiKey 原值（判断是否为「新输入」）
  var currentProviderId = null; // 当前 tab
  var newProviderIds = {};      // 新建中的 provider（providerId 字段可编辑）
  var expandedModels = [];      // 展开的模型卡片（按对象引用）
  var dirty = false;            // 有未保存修改
  var cachedImport = null;      // {providers, report} 当前文件的转换结果
  var modelAuthStatuses = {};   // canonical provider → 脱敏状态
  var activeLogin = null;       // 当前登录任务的公开状态（不含凭据）
  var loginPollTimer = null;
  var workingRevision = 0;      // 任意编辑/reload/save 都使旧 discovery 结果失效
  var accountEpoch = { 'openai-codex': 0, xai: 0 };
  var discoveryFlights = {};    // provider + accountEpoch → Promise

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }

  function markDirty() {
    workingRevision += 1;
    dirty = true;
    dirtyHintEl.classList.remove('hidden');
  }

  function defaultModel() {
    return {
      id: '', name: '', input: ['text'], contextWindow: 128000, maxTokens: 8192,
      reasoning: false, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }
    };
  }

  /* ---------- 控件工厂（label 上控件下、全宽一行） ---------- */

  function makeField(labelText, controlEl, hintText) {
    var field = document.createElement('div');
    field.className = 'settings-field';
    var label = document.createElement('label');
    label.className = 'form-label';
    label.textContent = labelText;
    field.appendChild(label);
    field.appendChild(controlEl);
    if (hintText) {
      var hint = document.createElement('div');
      hint.className = 'field-hint';
      hint.textContent = hintText;
      field.appendChild(hint);
    }
    return field;
  }

  function makeTextInput(value, onInput, isNumber) {
    var input = document.createElement('input');
    input.className = 'form-input';
    input.type = isNumber ? 'number' : 'text';
    if (isNumber) input.min = '0';
    input.value = value;
    input.addEventListener('input', function () { onInput(input.value); markDirty(); });
    return input;
  }

  function makeSelect(pairs, value, onChange) {
    var select = document.createElement('select');
    select.className = 'form-input';
    pairs.forEach(function (pair) {
      var option = document.createElement('option');
      option.value = pair[0];
      option.textContent = pair[1];
      select.appendChild(option);
    });
    select.value = value;
    select.addEventListener('change', function () { onChange(select.value); markDirty(); });
    return select;
  }

  function canonicalProviderForApi(apiType) {
    if (apiType === 'openai-codex-responses') return 'openai-codex';
    if (apiType === 'openai-responses') return 'xai';
    return null;
  }

  function formatAuthStatus(status) {
    if (!status || !status.loggedIn) return '未登录';
    var expiresAt = Number(status.expiresAt || 0);
    var remaining = expiresAt - Date.now() / 1000;
    if (remaining <= 0) return '已过期';
    if (remaining <= 300) return '即将过期';
    return '已登录' + (status.accountHint ? '（' + status.accountHint + '）' : '');
  }

  function isDiscoveryPending(authProvider) {
    var key = window.subscriptionModels.flightKey(authProvider, accountEpoch[authProvider] || 0);
    return !!discoveryFlights[key];
  }

  function buildSubscriptionAccount(authProvider) {
    var status = modelAuthStatuses[authProvider] || {};
    var isXai = authProvider === 'xai';
    var card = document.createElement('div');
    card.className = 'subscription-account-card';
    var head = document.createElement('div');
    head.className = 'subscription-account-head';
    var name = document.createElement('div');
    name.className = 'subscription-account-name';
    name.textContent = isXai ? 'xAI SuperGrok / X Premium' : 'ChatGPT Plus / Pro';
    var statusLine = document.createElement('div');
    statusLine.className = 'subscription-account-status';
    statusLine.textContent = formatAuthStatus(status);
    head.appendChild(name);
    head.appendChild(statusLine);
    card.appendChild(head);
    var description = document.createElement('div');
    description.className = 'subscription-account-description';
    description.textContent = isXai
      ? '登录后读取 xAI 实时模型目录，并与本地 Responses 元数据取交集。'
      : 'Codex 无可靠账户枚举端点，仅提供需手工确认的内置配置候选。';
    card.appendChild(description);
    if (status.error) {
      var statusError = document.createElement('div');
      statusError.className = 'field-error';
      statusError.textContent = status.error;
      card.appendChild(statusError);
    }
    var actions = document.createElement('div');
    actions.className = 'subscription-account-actions';
    function addAction(label, handler, className) {
      var button = document.createElement('button');
      button.className = className || 'btn';
      button.type = 'button';
      button.textContent = label;
      button.addEventListener('click', handler);
      actions.appendChild(button);
      return button;
    }
    if (!status.loggedIn) {
      if (isXai) {
        addAction('订阅登录', function () { startSubscriptionLogin('xai', 'device_code'); });
      } else {
        addAction('浏览器登录', function () { startSubscriptionLogin('openai-codex', 'browser'); });
        addAction('设备码登录', function () { startSubscriptionLogin('openai-codex', 'device_code'); });
      }
    } else {
      var syncLabel = isDiscoveryPending(authProvider)
        ? '正在发现…'
        : (isXai ? '同步模型候选' : '应用内置候选');
      var syncButton = addAction(syncLabel, function () { discoverAndApplySubscription(authProvider, true); });
      syncButton.disabled = isDiscoveryPending(authProvider);
      addAction('退出登录', function () { logoutSubscription(authProvider); }, 'btn btn-danger');
    }
    card.appendChild(actions);
    return card;
  }

  function renderSubscriptionAccounts() {
    subscriptionAccounts.innerHTML = '';
    subscriptionAccounts.appendChild(buildSubscriptionAccount('openai-codex'));
    subscriptionAccounts.appendChild(buildSubscriptionAccount('xai'));
  }

  function buildProviderOauthStatus(provider) {
    var authProvider = canonicalProviderForApi(provider.api);
    var status = modelAuthStatuses[authProvider] || {};
    var wrap = document.createElement('div');
    wrap.className = 'model-auth-card';
    var statusLine = document.createElement('div');
    statusLine.className = 'model-auth-status';
    statusLine.textContent = formatAuthStatus(status);
    wrap.appendChild(statusLine);
    var note = document.createElement('div');
    note.className = 'field-hint';
    note.textContent = '登录、同步候选和退出请使用页面顶部“订阅账户”；凭据不会写入 models.yaml。';
    wrap.appendChild(note);
    return wrap;
  }

  function showDiscoveryNotice(message, kind) {
    subscriptionDiscoveryNotice.textContent = message || '';
    subscriptionDiscoveryNotice.className = 'subscription-discovery-notice' + (kind ? ' ' + kind : '');
    subscriptionDiscoveryNotice.classList.toggle('hidden', !message);
  }

  async function refreshModelAuthStatuses() {
    var response = await window.api.getModelAuth();
    modelAuthStatuses = (response && response.providers) || {};
  }

  function hasStrictSubscriptionProvider(authProvider) {
    var providers = (modelConfig && modelConfig.providers) || {};
    var expectedApi = authProvider === 'xai' ? 'openai-responses' : 'openai-codex-responses';
    var expectedUrl = authProvider === 'xai'
      ? 'https://api.x.ai/v1' : 'https://chatgpt.com/backend-api';
    return Object.keys(providers).some(function (providerId) {
      var provider = providers[providerId] || {};
      return provider.api === expectedApi && provider.auth === 'oauth' &&
        window.subscriptionModels.normalizeBaseUrl(provider.baseUrl) === expectedUrl;
    });
  }

  function formatDiscoveryReport(result, merged) {
    var report = (result && result.report) || {};
    var sourceNames = {
      'live-catalog-match': 'xAI 实时目录与本地 Responses 目录交集',
      'local-fallback': '离线本地候选（实时目录失败）',
      'local-only': '内置本地候选'
    };
    var reasonNames = {
      unsupported_output_modality: '当前不支持图像/视频生成',
      requires_openai_completions: '仅确认支持 Chat Completions',
      missing_responses_metadata: '缺少可信 Responses 元数据'
    };
    var lines = [
      '来源：' + (sourceNames[result.source] || result.source),
      '配置候选：' + ((report.includedModelIds || []).join('、') || '（无）')
    ];
    var skipped = report.skippedModels || [];
    if (skipped.length) {
      lines.push('未自动加入：');
      skipped.slice(0, 20).forEach(function (item) {
        lines.push('  - ' + item.id + '：' + (reasonNames[item.reason] || item.reason));
      });
      if (skipped.length > 20) lines.push('  - 另有 ' + (skipped.length - 20) + ' 项');
    }
    (report.warnings || []).forEach(function (warning) { lines.push('提示：' + warning); });
    if (report.liveFailureCode) lines.push('实时目录失败码：' + report.liveFailureCode);
    if (merged) {
      lines.push('已加入编辑区 Provider：' + merged.providerId);
      lines.push('新增模型：' + (merged.addedModelIds.join('、') || '（无，已有同名配置均保持不变）'));
      lines.push('尚未写入 models.yaml，请确认后点击页面底部“保存”。');
    }
    return lines.join('\n');
  }

  function discoveryGuardMatches(startRevision, authProvider, startEpoch, startGeneration, resultGeneration) {
    return window.subscriptionModels.canApplyResult(
      startRevision,
      workingRevision,
      startEpoch,
      accountEpoch[authProvider] || 0,
      startGeneration,
      resultGeneration
    );
  }

  function discoverAndApplySubscription(authProvider, manualTrigger) {
    var epoch = accountEpoch[authProvider] || 0;
    var key = window.subscriptionModels.flightKey(authProvider, epoch);
    if (discoveryFlights[key]) return discoveryFlights[key];
    var status = modelAuthStatuses[authProvider] || {};
    if (!status.loggedIn) {
      showDiscoveryNotice('请先完成 ' + authProvider + ' 订阅登录。', 'error');
      return Promise.resolve();
    }
    var startRevision = workingRevision;
    var startGeneration = Number(status.credentialGeneration || 0);
    var operation = (async function () {
      showDiscoveryNotice('正在读取 ' + authProvider + ' 模型配置候选…');
      try {
        var result = await window.api.discoverSubscriptionModels(authProvider);
        var resultGeneration = Number(result.credentialGeneration || 0);
        if (!discoveryGuardMatches(startRevision, authProvider, epoch, startGeneration, resultGeneration)) {
          showDiscoveryNotice('发现期间配置或订阅账户发生变化，旧结果已丢弃；请重新同步。', 'warning');
          return;
        }
        showDiscoveryNotice(formatDiscoveryReport(result), result.autoApplicable ? '' : 'warning');
        var candidateModels = result.providerTemplate && result.providerTemplate.models;
        if (!Array.isArray(candidateModels) || candidateModels.length === 0) return;
        if (!result.autoApplicable) {
          if (!manualTrigger) return;
          if (!window.confirm('这些只是未验证的本地配置候选，不代表账户权益。仍要加入当前编辑区吗？')) return;
        }
        if (!discoveryGuardMatches(startRevision, authProvider, epoch, startGeneration, resultGeneration)) {
          showDiscoveryNotice('确认期间配置或订阅账户发生变化，结果未应用。', 'warning');
          return;
        }
        var merged = window.subscriptionModels.mergeDiscovery(modelConfig, result, currentProviderId);
        if (!merged.ok) {
          showDiscoveryNotice(
            '存在多个匹配的订阅 Provider：' + merged.matchingProviderIds.join('、') +
            '。请先切换到目标 Provider，再点击“同步模型候选”。',
            'warning'
          );
          return;
        }
        modelConfig = merged.config;
        if (merged.createdProvider) newProviderIds[merged.providerId] = true;
        currentProviderId = merged.providerId;
        expandedModels = [];
        var targetModels = modelConfig.providers[merged.providerId].models || [];
        if (targetModels[0]) expandedModels.push(targetModels[0]);
        markDirty();
        render();
        showDiscoveryNotice(formatDiscoveryReport(result, merged), 'warning');
      } catch (error) {
        showDiscoveryNotice(error.message, 'error');
      } finally {
        if (discoveryFlights[key] === operation) delete discoveryFlights[key];
        renderSubscriptionAccounts();
      }
    })();
    discoveryFlights[key] = operation;
    renderSubscriptionAccounts();
    return operation;
  }

  async function autoDiscoverMissingXaiProvider() {
    var status = modelAuthStatuses.xai || {};
    if (status.loggedIn && !hasStrictSubscriptionProvider('xai')) {
      await discoverAndApplySubscription('xai', false);
    }
  }

  function resetLoginModal() {
    modelAuthLoginError.classList.add('hidden');
    modelAuthLoginError.textContent = '';
    modelAuthLoginUrlRow.classList.add('hidden');
    modelAuthDeviceCodeRow.classList.add('hidden');
    modelAuthManualRow.classList.add('hidden');
    modelAuthCountdown.textContent = '';
    modelAuthManualCode.value = '';
    modelAuthLoginCancel.classList.remove('hidden');
  }

  function updateLoginModal(task) {
    activeLogin = task;
    var labels = {
      pending: '等待用户完成授权…',
      completed: '登录成功。',
      error: '登录失败。',
      cancelled: '登录已取消。'
    };
    modelAuthLoginStatus.textContent = labels[task.status] || task.status;
    if (task.manualCodeRequired && task.status === 'pending') {
      modelAuthLoginStatus.textContent = '本机回调端口不可用，请使用手工回调 code 或设备码登录。';
    }
    if (task.authUrl) {
      modelAuthLoginUrl.href = task.authUrl;
      modelAuthLoginUrlRow.classList.remove('hidden');
    } else {
      modelAuthLoginUrlRow.classList.add('hidden');
    }
    if (task.deviceCode) {
      modelAuthDeviceCode.textContent = task.deviceCode;
      modelAuthDeviceCodeRow.classList.remove('hidden');
    } else {
      modelAuthDeviceCode.textContent = '';
      modelAuthDeviceCodeRow.classList.add('hidden');
    }
    modelAuthManualRow.classList.toggle('hidden', task.method !== 'browser' || task.status !== 'pending');
    var remaining = Math.max(0, Math.ceil(Number(task.expiresAt || 0) - Date.now() / 1000));
    modelAuthCountdown.textContent = task.status === 'pending' && task.expiresAt
      ? '剩余约 ' + remaining + ' 秒' : '';
    if (task.error) {
      modelAuthLoginError.textContent = task.error;
      modelAuthLoginError.classList.remove('hidden');
    } else {
      modelAuthLoginError.classList.add('hidden');
    }
    modelAuthLoginCancel.classList.toggle('hidden', task.status !== 'pending');
  }

  function scheduleLoginPoll() {
    if (loginPollTimer) window.clearTimeout(loginPollTimer);
    loginPollTimer = window.setTimeout(pollSubscriptionLogin, 1000);
  }

  async function pollSubscriptionLogin() {
    if (!activeLogin) return;
    try {
      var task = await window.api.getModelLogin(activeLogin.loginId);
      updateLoginModal(task);
      if (task.status === 'pending') {
        scheduleLoginPoll();
      } else if (task.status === 'completed') {
        await refreshModelAuthStatuses();
        render();
        await discoverAndApplySubscription(task.provider, false);
      }
    } catch (error) {
      modelAuthLoginError.textContent = error.message;
      modelAuthLoginError.classList.remove('hidden');
    }
  }

  async function startSubscriptionLogin(provider, method) {
    if (!provider) return;
    accountEpoch[provider] = (accountEpoch[provider] || 0) + 1;
    resetLoginModal();
    modelAuthLoginStatus.textContent = '正在启动登录…';
    modelAuthModal.classList.remove('hidden');
    try {
      var task = await window.api.startModelLogin(provider, method);
      updateLoginModal(task);
      if (task.method === 'browser' && task.authUrl) {
        window.open(task.authUrl, '_blank', 'noopener');
      }
      scheduleLoginPoll();
    } catch (error) {
      activeLogin = null;
      modelAuthLoginStatus.textContent = '登录启动失败。';
      modelAuthLoginError.textContent = error.message;
      modelAuthLoginError.classList.remove('hidden');
    }
  }

  async function submitManualLoginCode() {
    if (!activeLogin || !modelAuthManualCode.value.trim()) return;
    modelAuthManualSubmit.disabled = true;
    try {
      updateLoginModal(await window.api.submitModelLoginCode(activeLogin.loginId, modelAuthManualCode.value.trim()));
      modelAuthManualCode.value = '';
    } catch (error) {
      modelAuthLoginError.textContent = error.message;
      modelAuthLoginError.classList.remove('hidden');
    } finally {
      modelAuthManualSubmit.disabled = false;
    }
  }

  async function cancelSubscriptionLogin() {
    if (!activeLogin) return;
    try {
      updateLoginModal(await window.api.cancelModelLogin(activeLogin.loginId));
    } catch (error) {
      modelAuthLoginError.textContent = error.message;
      modelAuthLoginError.classList.remove('hidden');
    }
  }

  function closeLoginModal() {
    if (loginPollTimer) window.clearTimeout(loginPollTimer);
    loginPollTimer = null;
    activeLogin = null;
    modelAuthModal.classList.add('hidden');
  }

  async function logoutSubscription(provider) {
    if (!window.confirm('退出 ' + provider + ' 订阅登录？')) return;
    accountEpoch[provider] = (accountEpoch[provider] || 0) + 1;
    try {
      await window.api.logoutModelAuth(provider);
      await refreshModelAuthStatuses();
      render();
    } catch (error) {
      showError(error.message);
    }
  }

  /* ---------- 渲染 ---------- */

  function render() {
    renderSubscriptionAccounts();
    renderTabs();
    renderForm();
  }

  function renderTabs() {
    tabsEl.innerHTML = '';
    var providers = (modelConfig && modelConfig.providers) || {};
    Object.keys(providers).forEach(function (providerId) {
      var tab = document.createElement('button');
      tab.className = 'provider-tab' + (providerId === currentProviderId ? ' active' : '');
      tab.textContent = providerId || '未命名 provider';
      tab.addEventListener('click', function () { switchTab(providerId); });
      tabsEl.appendChild(tab);
    });
    var addTab = document.createElement('button');
    addTab.className = 'provider-tab provider-tab-add';
    addTab.textContent = '＋ 新增 provider';
    addTab.addEventListener('click', addProvider);
    tabsEl.appendChild(addTab);
  }

  // tab 切换不重拉（工作副本在内存，修改不丢）；脏数据时 confirm 提示（§11.3 审核低 14）
  function switchTab(providerId) {
    if (providerId === currentProviderId) return;
    if (dirty && !window.confirm('当前有未保存修改。切换 provider 不会丢失修改，但「保存」会对全部 provider 一并生效。继续切换？')) {
      return;
    }
    currentProviderId = providerId;
    render();
  }

  function renderForm() {
    formEl.innerHTML = '';
    var providers = (modelConfig && modelConfig.providers) || {};
    var provider = providers[currentProviderId];
    if (!provider) return;

    /* ----- provider 字段区（每字段独占一行全宽） ----- */

    // providerId：新建可编辑，已有只读
    var isNew = !!newProviderIds[currentProviderId];
    var idInput = makeTextInput(currentProviderId, function (v) { renameProviderId(currentProviderId, v); });
    idInput.readOnly = !isNew;
    if (!isNew) idInput.style.background = 'var(--gray-block)';
    formEl.appendChild(makeField('providerId', idInput, isNew ? '新建 provider，可修改 id' : '已有 provider 的 id 不可修改'));

    formEl.appendChild(makeField('baseUrl', makeTextInput(provider.baseUrl || '', function (v) { provider.baseUrl = v; })));

    var apiPairs = [
      ['openai-completions', 'OpenAI Chat Completions'],
      ['openai-responses', 'OpenAI Responses（xAI）'],
      ['openai-codex-responses', 'ChatGPT Codex Responses']
    ];
    if (!provider.api) provider.api = 'openai-completions';
    formEl.appendChild(makeField('api', makeSelect(apiPairs, provider.api, function (value) {
      provider.api = value;
      if (value === 'openai-completions') provider.auth = 'api-key';
      if (value === 'openai-codex-responses') provider.auth = 'oauth';
      if (!provider.auth) provider.auth = value === 'openai-responses' ? 'oauth' : 'api-key';
      renderForm();
    })));

    if (!provider.auth) provider.auth = 'api-key';
    var authPairs = provider.api === 'openai-responses'
      ? [['api-key', 'API Key'], ['oauth', '订阅 OAuth']]
      : (provider.api === 'openai-codex-responses' ? [['oauth', '订阅 OAuth']] : [['api-key', 'API Key']]);
    if (!authPairs.some(function (pair) { return pair[0] === provider.auth; })) {
      provider.auth = authPairs[0][0];
    }
    formEl.appendChild(makeField('auth', makeSelect(authPairs, provider.auth, function (value) {
      provider.auth = value;
      renderForm();
    })));

    if (provider.auth === 'api-key') {
      // apiKey：password 输入 + 眼睛切换；__KEEP__/$/空串语义不变
      var apiKeyRow = document.createElement('div');
      apiKeyRow.className = 'apikey-row';
      var apiKeyInput = document.createElement('input');
      apiKeyInput.className = 'form-input';
      apiKeyInput.type = 'password';
      apiKeyInput.value = provider.apiKey == null ? '' : provider.apiKey;
      apiKeyInput.autocomplete = 'off';
      apiKeyInput.addEventListener('input', function () { provider.apiKey = apiKeyInput.value; markDirty(); });
      var eyeButton = document.createElement('button');
      eyeButton.className = 'apikey-eye';
      eyeButton.type = 'button';
      eyeButton.textContent = '👁';
      eyeButton.title = '显示/隐藏 apiKey';
      eyeButton.addEventListener('click', function () {
        apiKeyInput.type = apiKeyInput.type === 'password' ? 'text' : 'password';
      });
      apiKeyRow.appendChild(apiKeyInput);
      apiKeyRow.appendChild(eyeButton);
      formEl.appendChild(makeField('apiKey', apiKeyRow, '__KEEP__ 表示保留原值；留空表示删除该字段；xAI 可回退服务端 XAI_API_KEY'));
    } else {
      formEl.appendChild(makeField('订阅认证', buildProviderOauthStatus(provider)));
    }

    // headers：自定义请求头（可选），随每次模型请求发送；Authorization/Content-Type 由系统设置不可覆盖
    var headersInput = document.createElement('textarea');
    headersInput.className = 'form-input headers-input';
    headersInput.rows = 3;
    headersInput.placeholder = 'User-Agent: curl/8.7.1';
    headersInput.value = headersToText(provider.headers);
    headersInput.addEventListener('input', function () { provider.headers = textToHeaders(headersInput.value); markDirty(); });
    formEl.appendChild(makeField('headers（自定义请求头，可选）', headersInput, '每行一个 Key: Value；留空 = 不携带自定义头；适用于中转站按客户端指纹拦截的场景（如伪装 curl）'));

    // 删除此 provider（危险按钮，confirm）
    var dangerZone = document.createElement('div');
    dangerZone.className = 'provider-danger-zone';
    var deleteProviderBtn = document.createElement('button');
    deleteProviderBtn.className = 'btn btn-danger';
    deleteProviderBtn.textContent = '删除此 provider';
    deleteProviderBtn.addEventListener('click', function () {
      if (!window.confirm('删除 provider「' + currentProviderId + '」及其全部模型配置？（保存后生效）')) return;
      delete modelConfig.providers[currentProviderId];
      delete newProviderIds[currentProviderId];
      var ids = Object.keys(modelConfig.providers);
      currentProviderId = ids[0] || null;
      markDirty();
      render();
    });
    dangerZone.appendChild(deleteProviderBtn);
    formEl.appendChild(dangerZone);

    /* ----- 模型列表区（每模型一张全宽折叠卡片） ----- */

    var modelsTitle = document.createElement('div');
    modelsTitle.className = 'models-section-title';
    modelsTitle.textContent = '模型列表';
    formEl.appendChild(modelsTitle);

    if (!Array.isArray(provider.models)) provider.models = [];
    provider.models.forEach(function (model) {
      formEl.appendChild(buildModelCard(provider, model));
    });

    var addModelBtn = document.createElement('button');
    addModelBtn.className = 'btn add-model-btn';
    addModelBtn.textContent = '＋ 新增模型';
    addModelBtn.addEventListener('click', function () {
      var model = defaultModel();
      provider.models.push(model);
      expandedModels.push(model); // 新模型直接展开
      markDirty();
      render();
    });
    formEl.appendChild(addModelBtn);
  }

  function buildModelCard(provider, model) {
    var card = document.createElement('div');
    card.className = 'model-card' + (expandedModels.indexOf(model) !== -1 ? ' expanded' : '');

    var header = document.createElement('div');
    header.className = 'model-card-header';
    var arrow = document.createElement('span');
    arrow.className = 'model-card-arrow';
    arrow.textContent = '▸';
    var title = document.createElement('span');
    title.className = 'model-card-title';
    title.textContent = model.id || '（未命名模型）';
    var deleteBtn = document.createElement('button');
    deleteBtn.className = 'model-card-delete';
    deleteBtn.textContent = '✕';
    deleteBtn.title = '删除该模型';
    deleteBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      var index = provider.models.indexOf(model);
      if (index !== -1) provider.models.splice(index, 1);
      var expandedIndex = expandedModels.indexOf(model);
      if (expandedIndex !== -1) expandedModels.splice(expandedIndex, 1);
      markDirty();
      render();
    });
    header.appendChild(arrow);
    header.appendChild(title);
    header.appendChild(deleteBtn);
    header.addEventListener('click', function () {
      var expandedIndex = expandedModels.indexOf(model);
      if (expandedIndex === -1) expandedModels.push(model);
      else expandedModels.splice(expandedIndex, 1);
      render();
    });
    card.appendChild(header);

    var body = document.createElement('div');
    body.className = 'model-card-body';

    body.appendChild(makeField('id', makeTextInput(model.id || '', function (v) {
      model.id = v;
      title.textContent = v || '（未命名模型）';
    })));
    body.appendChild(makeField('name', makeTextInput(model.name || '', function (v) { model.name = v; })));

    // input：text/image 多选 checkbox
    var checksWrap = document.createElement('div');
    checksWrap.className = 'model-input-checks';
    ['text', 'image'].forEach(function (kind) {
      var label = document.createElement('label');
      var checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = Array.isArray(model.input) && model.input.indexOf(kind) !== -1;
      checkbox.addEventListener('change', function () {
        if (!Array.isArray(model.input)) model.input = [];
        var index = model.input.indexOf(kind);
        if (checkbox.checked && index === -1) model.input.push(kind);
        if (!checkbox.checked && index !== -1) model.input.splice(index, 1);
        markDirty();
      });
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(kind));
      checksWrap.appendChild(label);
    });
    body.appendChild(makeField('input', checksWrap));

    body.appendChild(makeField('contextWindow', makeTextInput(model.contextWindow || 0, function (v) { model.contextWindow = Number(v); }, true)));
    body.appendChild(makeField('maxTokens', makeTextInput(model.maxTokens || 0, function (v) { model.maxTokens = Number(v); }, true)));

    // 思考强度：仅编辑 reasoningEffort；reasoning/thinking 保留工作副本原值但不暴露控件
    var effortPairs = [['', '（缺省）'], ['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['max', 'max']];
    var effortValue = model.reasoningEffort || '';
    var known = effortPairs.some(function (pair) { return pair[0] === effortValue; });
    if (effortValue && !known) effortPairs.push([effortValue, effortValue]);
    body.appendChild(makeField('思考强度', makeSelect(effortPairs, effortValue, function (v) {
      if (v) model.reasoningEffort = v;
      else delete model.reasoningEffort;
    })));

    // cost 四字段（两列网格）
    if (!model.cost) model.cost = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
    var costGrid = document.createElement('div');
    costGrid.className = 'cost-grid';
    ['input', 'output', 'cacheRead', 'cacheWrite'].forEach(function (field) {
      var input = document.createElement('input');
      input.className = 'form-input';
      input.type = 'number';
      input.min = '0';
      input.step = 'any';
      input.value = model.cost[field] == null ? 0 : model.cost[field];
      input.addEventListener('input', function () { model.cost[field] = Number(input.value); markDirty(); });
      var item = document.createElement('div');
      var label = document.createElement('label');
      label.className = 'form-label';
      label.textContent = field;
      item.appendChild(label);
      item.appendChild(input);
      costGrid.appendChild(item);
    });
    body.appendChild(makeField('cost（美元/百万 tokens）', costGrid));

    card.appendChild(body);
    return card;
  }

  // 新建 provider 改名：迁移工作副本 key（允许暂时为空；撞名时回退渲染）
  function renameProviderId(oldId, nextId) {
    nextId = (nextId || '').trim();
    if (nextId === oldId) return;
    if (Object.prototype.hasOwnProperty.call(modelConfig.providers, nextId)) {
      showError('provider「' + nextId + '」已存在。');
      render();
      return;
    }
    errorEl.classList.add('hidden');
    modelConfig.providers[nextId] = modelConfig.providers[oldId];
    delete modelConfig.providers[oldId];
    if (newProviderIds[oldId]) {
      delete newProviderIds[oldId];
      newProviderIds[nextId] = true;
    }
    currentProviderId = nextId;
    renderTabs();
  }

  /* ---------- 保存前校验（对齐契约 §2.4，失败给出中文字段名） ---------- */

  function validate(config) {
    var providers = config.providers;
    if (!providers || typeof providers !== 'object' || Array.isArray(providers)) {
      return 'providers 必须是对象。';
    }
    var ids = Object.keys(providers);
    if (ids.length === 0) return 'providers 不能为空。';
    for (var i = 0; i < ids.length; i++) {
      var providerId = ids[i];
      if (!providerId.trim()) return 'providerId 不能为空。';
      var provider = providers[providerId];
      if (!provider.baseUrl || typeof provider.baseUrl !== 'string') {
        return 'provider「' + providerId + '」的 baseUrl 不能为空。';
      }
      var allowedApis = ['openai-completions', 'openai-responses', 'openai-codex-responses'];
      if (allowedApis.indexOf(provider.api) === -1) {
        return 'provider「' + providerId + '」的 api 不受支持。';
      }
      provider.auth = provider.auth || 'api-key';
      if (['api-key', 'oauth'].indexOf(provider.auth) === -1) {
        return 'provider「' + providerId + '」的 auth 仅允许 api-key/oauth。';
      }
      if (provider.api === 'openai-completions' && provider.auth !== 'api-key') {
        return 'provider「' + providerId + '」的 Chat Completions 仅支持 API Key。';
      }
      if (provider.api === 'openai-codex-responses' && provider.auth !== 'oauth') {
        return 'provider「' + providerId + '」的 Codex Responses 仅支持订阅 OAuth。';
      }
      if (provider.auth === 'api-key' && provider.api !== 'openai-responses' &&
          (!provider.apiKey || typeof provider.apiKey !== 'string')) {
        return 'provider「' + providerId + '」的 apiKey 不能为空。';
      }
      if (!Array.isArray(provider.models) || provider.models.length === 0) {
        return 'provider「' + providerId + '」的 models 不能为空数组。';
      }
      for (var j = 0; j < provider.models.length; j++) {
        var model = provider.models[j];
        var prefix = 'provider「' + providerId + '」第 ' + (j + 1) + ' 个模型：';
        if (!model.id || typeof model.id !== 'string') return prefix + 'id 不能为空。';
        if (!Array.isArray(model.input) || model.input.length === 0) return prefix + 'input 至少选择一项。';
        for (var k = 0; k < model.input.length; k++) {
          if (model.input[k] !== 'text' && model.input[k] !== 'image') {
            return prefix + 'input 仅允许 text/image。';
          }
        }
        if (!Number.isInteger(model.contextWindow) || model.contextWindow <= 0) {
          return prefix + 'contextWindow 必须为正整数。';
        }
        if (!Number.isInteger(model.maxTokens) || model.maxTokens <= 0) {
          return prefix + 'maxTokens 必须为正整数。';
        }
        model.reasoning = !!model.reasoning;
        if (model.thinking && ['enabled', 'disabled'].indexOf(model.thinking.type) === -1) {
          return prefix + 'thinking.type 仅允许 enabled/disabled。';
        }
        var cost = model.cost || {};
        var costFields = ['input', 'output', 'cacheRead', 'cacheWrite'];
        for (var c = 0; c < costFields.length; c++) {
          var value = Number(cost[costFields[c]]);
          if (!isFinite(value) || value < 0) return prefix + 'cost.' + costFields[c] + ' 必须为 ≥0 的数值。';
          cost[costFields[c]] = value;
        }
        model.cost = cost;
      }
    }
    return null;
  }

  // $ 前缀约定（契约 §2.4-L3）：新输入的 apiKey 以 $ 开头 → 拦截提示其将被视为环境变量引用
  function checkApiKeyDollarPrefix(config) {
    var providers = config.providers || {};
    var ids = Object.keys(providers);
    for (var i = 0; i < ids.length; i++) {
      if ((providers[ids[i]].auth || 'api-key') !== 'api-key') continue;
      var apiKey = providers[ids[i]].apiKey;
      if (typeof apiKey !== 'string' || apiKey.charAt(0) !== '$') continue;
      var isNewInput = fetchedApiKeys[ids[i]] !== apiKey; // GET 原样返回的引用不算新输入
      if (isNewInput) {
        return window.confirm(
          'provider「' + ids[i] + '」的 apiKey 以 $ 开头，将被视为环境变量引用而非明文 key（明文 key 不得以 $ 开头）。确认使用？'
        );
      }
    }
    return true;
  }

  async function save() {
    if (!modelConfig) return;
    errorEl.classList.add('hidden');
    var invalid = validate(modelConfig);
    if (invalid) { showError(invalid); return; }
    if (!checkApiKeyDollarPrefix(modelConfig)) return;

    saveButton.disabled = true;
    try {
      await window.api.putModels(modelConfig);
      window.sidebarView.invalidateModelConfig();
      window.alert('已保存。config/models.yaml 已更新（原文件备份为 models.yaml.bak）。');
      await window.settingsView.open(); // 重新拉取，拿到最新脱敏状态
    } catch (error) {
      showError(error.message);
    } finally {
      saveButton.disabled = false;
    }
  }

  // 重置 = 放弃修改重新 GET（脏数据 confirm）
  async function reset() {
    if (dirty && !window.confirm('放弃全部未保存修改并重新拉取配置？')) return;
    await window.settingsView.open();
  }

  function addProvider() {
    if (!modelConfig) return;
    var providerId = '';
    if (Object.prototype.hasOwnProperty.call(modelConfig.providers, providerId)) {
      currentProviderId = providerId;
      render();
      return;
    }
    var model = defaultModel();
    modelConfig.providers[providerId] = {
      baseUrl: '',
      api: 'openai-completions',
      auth: 'api-key',
      apiKey: '',
      models: [model]
    };
    newProviderIds[providerId] = true;
    expandedModels.push(model);
    currentProviderId = providerId;
    markDirty();
    render();
  }

  /* ---------- 上传 models.json 导入（方案 §4 / D5 / D6） ---------- */

  function deepCopy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function mergePiImport(working, imported, policy, dryRun) {
    var schemaKeys = [
      'id', 'name', 'input', 'contextWindow', 'maxTokens',
      'reasoning', 'thinking', 'reasoningEffort', 'cost', 'headers'
    ];
    var addedProviders = [];
    var addedModels = [];
    var overwrittenModels = [];
    var skippedExistingModels = [];
    var keptApiKeysBecauseEmpty = [];
    var importedProviders = (imported && imported.providers) || imported || {};
    var workingProviders = (working && working.providers) || {};
    if (!dryRun) {
      if (!working.providers || typeof working.providers !== 'object') working.providers = {};
      workingProviders = working.providers;
    }
    Object.keys(importedProviders).forEach(function (providerId) {
      var importedProvider = importedProviders[providerId];
      if (!importedProvider || typeof importedProvider !== 'object') return;
      var existing = Object.prototype.hasOwnProperty.call(workingProviders, providerId)
        ? workingProviders[providerId]
        : null;
      if (!existing) {
        addedProviders.push(providerId);
        var newProviderModels = Array.isArray(importedProvider.models) ? importedProvider.models : [];
        newProviderModels.forEach(function (newModel) {
          if (newModel && newModel.id) addedModels.push({ providerId: providerId, modelId: newModel.id });
        });
        if (!dryRun) {
          workingProviders[providerId] = deepCopy(importedProvider);
          newProviderIds[providerId] = true;
        }
        return;
      }
      if (!dryRun) {
        if (policy.overwriteProviderFields) {
          existing.baseUrl = importedProvider.baseUrl;
          if (Object.prototype.hasOwnProperty.call(importedProvider, 'headers')) {
            existing.headers = deepCopy(importedProvider.headers);
          } else {
            existing.headers = {};
          }
        }
        if (policy.overwriteApiKey) {
          if (typeof importedProvider.apiKey === 'string' && importedProvider.apiKey) {
            existing.apiKey = importedProvider.apiKey;
          } else {
            keptApiKeysBecauseEmpty.push(providerId);
          }
        }
        existing.api = 'openai-completions';
        existing.auth = 'api-key';
        if (!Array.isArray(existing.models)) existing.models = [];
      } else if (policy.overwriteApiKey) {
        if (!(typeof importedProvider.apiKey === 'string' && importedProvider.apiKey)) {
          keptApiKeysBecauseEmpty.push(providerId);
        }
      }
      var existingModels = (existing && Array.isArray(existing.models)) ? existing.models : [];
      var importedModels = Array.isArray(importedProvider.models) ? importedProvider.models : [];
      importedModels.forEach(function (newModel) {
        if (!newModel || typeof newModel !== 'object' || !newModel.id) return;
        var foundIndex = -1;
        for (var i = 0; i < existingModels.length; i++) {
          if (existingModels[i] && existingModels[i].id === newModel.id) {
            foundIndex = i;
            break;
          }
        }
        if (foundIndex === -1) {
          addedModels.push({ providerId: providerId, modelId: newModel.id });
          if (!dryRun) existing.models.push(deepCopy(newModel));
          return;
        }
        if (!policy.overwriteModels) {
          skippedExistingModels.push({ providerId: providerId, modelId: newModel.id });
          return;
        }
        overwrittenModels.push({ providerId: providerId, modelId: newModel.id });
        if (dryRun) return;
        var oldModel = existing.models[foundIndex];
        var replaced = {};
        Object.keys(oldModel).forEach(function (key) {
          if (schemaKeys.indexOf(key) === -1) replaced[key] = oldModel[key];
        });
        var copy = deepCopy(newModel);
        Object.keys(copy).forEach(function (key) { replaced[key] = copy[key]; });
        if (!Object.prototype.hasOwnProperty.call(newModel, 'thinking')) delete replaced.thinking;
        if (!Object.prototype.hasOwnProperty.call(newModel, 'reasoningEffort')) delete replaced.reasoningEffort;
        if (!Object.prototype.hasOwnProperty.call(newModel, 'headers')) replaced.headers = {};
        existing.models[foundIndex] = replaced;
      });
    });
    return {
      addedProviders: addedProviders,
      addedModels: addedModels,
      overwrittenModels: overwrittenModels,
      skippedExistingModels: skippedExistingModels,
      keptApiKeysBecauseEmpty: keptApiKeysBecauseEmpty
    };
  }

  function readImportPolicy() {
    return {
      overwriteModels: !!piOverwriteModels.checked,
      overwriteProviderFields: !!piOverwriteProviderFields.checked,
      overwriteApiKey: !!piOverwriteApiKey.checked
    };
  }

  function readSelectedFileText() {
    return new Promise(function (resolve, reject) {
      var file = piImportFile.files && piImportFile.files[0];
      if (!file) {
        reject(new Error('请选择 models.json 文件。'));
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        var text = typeof reader.result === 'string' ? reader.result : '';
        if (!text.trim()) {
          reject(new Error('文件为空或全是空白字符。'));
          return;
        }
        resolve(text);
      };
      reader.onerror = function () {
        reject(new Error('读取文件失败。'));
      };
      reader.readAsText(file, 'UTF-8');
    });
  }

  function hasSelectedImportFile() {
    return !!(piImportFile.files && piImportFile.files[0]);
  }

  function syncImportButtons() {
    var hasFile = hasSelectedImportFile();
    piImportPreviewButton.disabled = !hasFile;
    if (!hasFile) {
      piImportApplyButton.disabled = true;
      return;
    }
    if (cachedImport && Object.keys(cachedImport.providers || {}).length === 0) {
      piImportApplyButton.disabled = true;
      return;
    }
    piImportApplyButton.disabled = false;
  }

  function formatIdList(items) {
    if (!items || items.length === 0) return '（无）';
    return items.join('、');
  }

  function formatPairList(items) {
    if (!items || items.length === 0) return '（无）';
    return items.map(function (item) {
      return item.providerId + '/' + item.modelId;
    }).join('、');
  }

  function formatSkippedList(items) {
    if (!items || items.length === 0) return '（无）';
    return items.map(function (item) {
      var id = item.id || item.modelId || '';
      var prefix = item.providerId ? (item.providerId + '/' + id) : id;
      return prefix + (item.reason ? '（' + item.reason + '）' : '');
    }).join('\n  ');
  }

  function formatEndpointReport(report) {
    report = report || {};
    var warnings = report.warnings || [];
    var lines = [
      '【转换报告】',
      '可导入 provider：' + formatIdList(report.importedProviders),
      '可导入模型：' + formatPairList(report.importedModels),
      '跳过 provider：' + formatSkippedList(report.skippedProviders),
      '跳过模型：' + formatSkippedList(report.skippedModels),
      '警告：' + (warnings.length ? ('\n  ' + warnings.join('\n  ')) : '（无）')
    ];
    return lines.join('\n');
  }

  function formatDryRunReport(stats) {
    var lines = [
      '【相对当前编辑区】',
      '将新增 provider：' + formatIdList(stats.addedProviders),
      '将新增模型：' + formatPairList(stats.addedModels),
      '将覆盖模型：' + formatPairList(stats.overwrittenModels),
      '因同名未开覆盖而跳过：' + formatPairList(stats.skippedExistingModels),
      '因文件密钥为空而保留现有密钥：' + formatIdList(stats.keptApiKeysBecauseEmpty)
    ];
    return lines.join('\n');
  }

  function showImportPanelError(message) {
    piImportReport.textContent = message || '导入失败。';
  }

  function openImportPanel() {
    if (!modelConfig) return;
    piImportPanel.classList.remove('hidden');
    syncImportButtons();
  }

  function closeImportPanel() {
    piImportPanel.classList.add('hidden');
    piImportFile.value = '';
    piImportReport.textContent = '';
    cachedImport = null;
    syncImportButtons();
  }

  async function ensureCachedImport() {
    if (cachedImport) return cachedImport;
    var rawText = await readSelectedFileText();
    cachedImport = await window.api.importPiModels(rawText);
    return cachedImport;
  }

  async function previewPiImport() {
    if (!modelConfig) return;
    piImportReport.textContent = '';
    try {
      var result = await ensureCachedImport();
      var providers = (result && result.providers) || {};
      syncImportButtons();
      var stats = mergePiImport(modelConfig, providers, readImportPolicy(), true);
      piImportReport.textContent = formatEndpointReport(result.report) + '\n\n' + formatDryRunReport(stats);
    } catch (error) {
      cachedImport = null;
      syncImportButtons();
      showImportPanelError(error.message);
    }
  }

  async function applyPiImport() {
    if (!modelConfig) return;
    if (dirty && !window.confirm('将在当前未保存修改上继续导入。继续？')) return;
    try {
      var result = await ensureCachedImport();
      var providers = (result && result.providers) || {};
      if (Object.keys(providers).length === 0) {
        syncImportButtons();
        showImportPanelError(formatEndpointReport(result.report) + '\n\n转换结果没有可导入的 provider，未改动编辑区。');
        return;
      }
      var hadNoTab = currentProviderId == null;
      var stats = mergePiImport(modelConfig, providers, readImportPolicy(), false);
      if (hadNoTab && stats.addedProviders.length > 0) {
        currentProviderId = stats.addedProviders[0];
      }
      markDirty();
      render();
      window.alert(
        '已应用到编辑区（尚未保存）。\n' +
        '新增 provider：' + stats.addedProviders.length + '\n' +
        '新增模型：' + stats.addedModels.length + '\n' +
        '覆盖模型：' + stats.overwrittenModels.length + '\n' +
        '跳过已有模型：' + stats.skippedExistingModels.length + '\n' +
        '因文件密钥为空而保留现有密钥：' + stats.keptApiKeysBecauseEmpty.length
      );
      closeImportPanel();
    } catch (error) {
      showImportPanelError(error.message);
    }
  }

  window.settingsView = {
    // open 时 GET 一次存工作副本；之后 tab 切换不重拉
    open: async function () {
      workingRevision += 1;
      var openRevision = workingRevision;
      errorEl.classList.add('hidden');
      showDiscoveryNotice('');
      tabsEl.innerHTML = '';
      formEl.innerHTML = '';
      dirty = false;
      dirtyHintEl.classList.add('hidden');
      newProviderIds = {};
      expandedModels = [];
      closeImportPanel();
      try {
        var responses = await Promise.all([window.api.getModels(), window.api.getModelAuth()]);
        if (!window.subscriptionModels.canCommitOpen(openRevision, workingRevision)) return;
        modelConfig = responses[0];
        modelAuthStatuses = (responses[1] && responses[1].providers) || {};
        fetchedApiKeys = {};
        Object.keys(modelConfig.providers || {}).forEach(function (providerId) {
          var provider = modelConfig.providers[providerId];
          if (!provider.auth) provider.auth = 'api-key';
          var apiKey = provider.apiKey;
          fetchedApiKeys[providerId] = apiKey == null ? '' : apiKey;
          // 工作副本规范化，避免 PUT 回传 null
          if (provider.apiKey == null) provider.apiKey = '';
          // 默认折叠、只展开每个 provider 的第一个模型
          if (Array.isArray(provider.models) && provider.models[0]) {
            expandedModels.push(provider.models[0]);
          }
        });
        var ids = Object.keys(modelConfig.providers || {});
        currentProviderId = ids[0] || null;
        render();
        if (!window.subscriptionModels.canCommitOpen(openRevision, workingRevision)) return;
        await autoDiscoverMissingXaiProvider();
      } catch (error) {
        if (!window.subscriptionModels.canCommitOpen(openRevision, workingRevision)) return;
        modelConfig = null;
        showError(error.message);
      }
    }
  };

  saveButton.addEventListener('click', save);
  resetButton.addEventListener('click', reset);
  importPiModelsButton.addEventListener('click', openImportPanel);
  piImportCancelButton.addEventListener('click', closeImportPanel);
  piImportFile.addEventListener('change', function () {
    piImportReport.textContent = '';
    cachedImport = null;
    syncImportButtons();
  });
  function refreshDryRunIfPreviewed() {
    if (!cachedImport || !modelConfig) return;
    var stats = mergePiImport(modelConfig, cachedImport.providers || {}, readImportPolicy(), true);
    piImportReport.textContent = formatEndpointReport(cachedImport.report) + '\n\n' + formatDryRunReport(stats);
  }
  piOverwriteModels.addEventListener('change', refreshDryRunIfPreviewed);
  piOverwriteProviderFields.addEventListener('change', refreshDryRunIfPreviewed);
  piOverwriteApiKey.addEventListener('change', refreshDryRunIfPreviewed);
  piImportPreviewButton.addEventListener('click', previewPiImport);
  piImportApplyButton.addEventListener('click', applyPiImport);
  modelAuthManualSubmit.addEventListener('click', submitManualLoginCode);
  modelAuthManualCode.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') submitManualLoginCode();
  });
  modelAuthLoginCancel.addEventListener('click', cancelSubscriptionLogin);
  modelAuthLoginClose.addEventListener('click', closeLoginModal);
})();
