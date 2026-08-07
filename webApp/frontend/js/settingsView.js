/*
Author: wilbur
Version: 1.0
Date: 2026-08-07
Description: 模型配置编辑页：provider 卡片（新增/删除/baseUrl/apiKey）+ 模型表格行编辑；
             保存 = 全量 PUT（契约 §2.4/§3.9/§3.10）。apiKey 遵循 __KEEP__ / $ 引用 / 空串删除规则。
*/
(function () {
  'use strict';

  var providerListEl = document.getElementById('providerList');
  var errorEl = document.getElementById('settingsError');
  var saveButton = document.getElementById('saveModelsButton');

  var modelConfig = null;       // GET /api/models 原始返回（编辑工作副本）
  var fetchedApiKeys = {};      // providerId → GET 返回的 apiKey 原值（判断是否为「新输入」）

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }

  /* ---------- 渲染 ---------- */

  function render() {
    providerListEl.innerHTML = '';
    var providers = (modelConfig && modelConfig.providers) || {};
    Object.keys(providers).forEach(function (providerId) {
      providerListEl.appendChild(buildProviderCard(providerId, providers[providerId]));
    });
  }

  function buildProviderCard(providerId, provider) {
    var card = document.createElement('div');
    card.className = 'provider-card';
    card.dataset.providerId = providerId;

    var header = document.createElement('div');
    header.className = 'provider-card-header';
    var title = document.createElement('span');
    title.className = 'provider-card-title';
    title.textContent = providerId;
    var deleteBtn = document.createElement('button');
    deleteBtn.className = 'btn btn-danger';
    deleteBtn.textContent = '删除 Provider';
    deleteBtn.addEventListener('click', function () {
      if (!window.confirm('删除 provider「' + providerId + '」及其全部模型配置？')) return;
      delete modelConfig.providers[providerId];
      render();
    });
    header.appendChild(title);
    header.appendChild(deleteBtn);
    card.appendChild(header);

    var fields = document.createElement('div');
    fields.className = 'provider-fields';

    var baseUrlField = document.createElement('div');
    baseUrlField.className = 'provider-field';
    var baseUrlLabel = document.createElement('label');
    baseUrlLabel.className = 'form-label';
    baseUrlLabel.textContent = 'baseUrl';
    var baseUrlInput = document.createElement('input');
    baseUrlInput.className = 'form-input';
    baseUrlInput.type = 'text';
    baseUrlInput.value = provider.baseUrl || '';
    baseUrlInput.addEventListener('input', function () { provider.baseUrl = baseUrlInput.value; });
    baseUrlField.appendChild(baseUrlLabel);
    baseUrlField.appendChild(baseUrlInput);
    fields.appendChild(baseUrlField);

    var apiKeyField = document.createElement('div');
    apiKeyField.className = 'provider-field';
    var apiKeyLabel = document.createElement('label');
    apiKeyLabel.className = 'form-label';
    apiKeyLabel.textContent = 'apiKey';
    var apiKeyInput = document.createElement('input');
    apiKeyInput.className = 'form-input';
    apiKeyInput.type = 'text';
    apiKeyInput.value = provider.apiKey == null ? '' : provider.apiKey;
    apiKeyInput.autocomplete = 'off';
    apiKeyInput.addEventListener('input', function () { provider.apiKey = apiKeyInput.value; });
    var apiKeyHint = document.createElement('div');
    apiKeyHint.className = 'field-hint';
    apiKeyHint.textContent = '__KEEP__ 表示保留原值；留空表示删除该字段；$ 开头视为环境变量引用';
    apiKeyField.appendChild(apiKeyLabel);
    apiKeyField.appendChild(apiKeyInput);
    apiKeyField.appendChild(apiKeyHint);
    fields.appendChild(apiKeyField);

    card.appendChild(fields);
    card.appendChild(buildModelTable(provider));
    return card;
  }

  function buildModelTable(provider) {
    if (!Array.isArray(provider.models)) provider.models = [];

    var wrap = document.createElement('div');
    var table = document.createElement('table');
    table.className = 'model-table';
    var thead = document.createElement('thead');
    var headRow = document.createElement('tr');
    ['id', 'name', 'input', 'contextWindow', 'maxTokens', 'reasoning', 'thinking', 'effort',
     'cost 入/出/缓存读/缓存写', ''].forEach(function (text) {
      var th = document.createElement('th');
      th.textContent = text;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    provider.models.forEach(function (model) {
      tbody.appendChild(buildModelRow(provider, model, tbody));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    var addBtn = document.createElement('button');
    addBtn.className = 'btn add-model-btn';
    addBtn.textContent = '＋ 新增模型';
    addBtn.addEventListener('click', function () {
      provider.models.push({
        id: '', name: '', input: ['text'], contextWindow: 128000, maxTokens: 8192,
        reasoning: false, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }
      });
      render();
    });
    wrap.appendChild(addBtn);
    return wrap;
  }

  function buildModelRow(provider, model, tbody) {
    var row = document.createElement('tr');

    function textCell(value, onChange, isNumber) {
      var td = document.createElement('td');
      var input = document.createElement('input');
      input.type = isNumber ? 'number' : 'text';
      input.value = value;
      if (isNumber) input.min = '0';
      input.addEventListener('input', function () { onChange(input.value); });
      td.appendChild(input);
      return td;
    }

    row.appendChild(textCell(model.id || '', function (v) { model.id = v; }));
    row.appendChild(textCell(model.name || '', function (v) { model.name = v; }));

    // input：text/image 多选
    var inputTd = document.createElement('td');
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
      });
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(kind));
      checksWrap.appendChild(label);
    });
    inputTd.appendChild(checksWrap);
    row.appendChild(inputTd);

    row.appendChild(textCell(model.contextWindow || 0, function (v) { model.contextWindow = Number(v); }, true));
    row.appendChild(textCell(model.maxTokens || 0, function (v) { model.maxTokens = Number(v); }, true));

    // reasoning 布尔
    var reasoningTd = document.createElement('td');
    var reasoningCheckbox = document.createElement('input');
    reasoningCheckbox.type = 'checkbox';
    reasoningCheckbox.checked = !!model.reasoning;
    reasoningCheckbox.addEventListener('change', function () { model.reasoning = reasoningCheckbox.checked; });
    reasoningTd.appendChild(reasoningCheckbox);
    row.appendChild(reasoningTd);

    // thinking.type：缺省 / enabled / disabled
    var thinkingTd = document.createElement('td');
    var thinkingSelect = document.createElement('select');
    [['', '（缺省）'], ['enabled', 'enabled'], ['disabled', 'disabled']].forEach(function (pair) {
      var option = document.createElement('option');
      option.value = pair[0];
      option.textContent = pair[1];
      thinkingSelect.appendChild(option);
    });
    thinkingSelect.value = model.thinking && model.thinking.type ? model.thinking.type : '';
    thinkingSelect.addEventListener('change', function () {
      if (thinkingSelect.value) {
        model.thinking = { type: thinkingSelect.value };
      } else {
        delete model.thinking;
      }
    });
    thinkingTd.appendChild(thinkingSelect);
    row.appendChild(thinkingTd);

    row.appendChild(textCell(model.reasoningEffort || '', function (v) {
      if (v) model.reasoningEffort = v; else delete model.reasoningEffort;
    }));

    // cost 四字段
    var costTd = document.createElement('td');
    var costWrap = document.createElement('div');
    costWrap.className = 'model-input-checks';
    if (!model.cost) model.cost = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
    ['input', 'output', 'cacheRead', 'cacheWrite'].forEach(function (field) {
      var input = document.createElement('input');
      input.type = 'number';
      input.min = '0';
      input.step = 'any';
      input.style.width = '64px';
      input.value = model.cost[field] == null ? 0 : model.cost[field];
      input.title = field;
      input.addEventListener('input', function () { model.cost[field] = Number(input.value); });
      costWrap.appendChild(input);
    });
    costTd.appendChild(costWrap);
    row.appendChild(costTd);

    var opsTd = document.createElement('td');
    var deleteBtn = document.createElement('button');
    deleteBtn.className = 'row-delete-btn';
    deleteBtn.textContent = '✕';
    deleteBtn.title = '删除该模型';
    deleteBtn.addEventListener('click', function () {
      var index = provider.models.indexOf(model);
      if (index !== -1) provider.models.splice(index, 1);
      render();
    });
    opsTd.appendChild(deleteBtn);
    row.appendChild(opsTd);

    return row;
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
      var provider = providers[providerId];
      if (!provider.baseUrl || typeof provider.baseUrl !== 'string') {
        return 'provider「' + providerId + '」的 baseUrl 不能为空。';
      }
      provider.api = 'openai-completions';
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

  function addProvider() {
    var providerId = window.prompt('新 provider 的 id（如 volcano）：');
    if (providerId === null) return;
    providerId = providerId.trim();
    if (!providerId) return;
    if (modelConfig.providers[providerId]) {
      showError('provider「' + providerId + '」已存在。');
      return;
    }
    modelConfig.providers[providerId] = {
      baseUrl: '',
      api: 'openai-completions',
      apiKey: '',
      models: [{
        id: '', name: '', input: ['text'], contextWindow: 128000, maxTokens: 8192,
        reasoning: false, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }
      }]
    };
    render();
  }

  window.settingsView = {
    open: async function () {
      errorEl.classList.add('hidden');
      providerListEl.innerHTML = '';
      try {
        modelConfig = await window.api.getModels();
        fetchedApiKeys = {};
        Object.keys(modelConfig.providers || {}).forEach(function (providerId) {
          var apiKey = modelConfig.providers[providerId].apiKey;
          fetchedApiKeys[providerId] = apiKey == null ? '' : apiKey;
          // 工作副本规范化，避免 PUT 回传 null
          if (modelConfig.providers[providerId].apiKey == null) {
            modelConfig.providers[providerId].apiKey = '';
          }
        });
        render();
      } catch (error) {
        modelConfig = null;
        showError(error.message);
      }
    }
  };

  saveButton.addEventListener('click', save);
  document.getElementById('addProviderButton').addEventListener('click', addProvider);
})();
