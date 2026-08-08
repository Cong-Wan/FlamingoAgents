/*
Author: wilbur
Version: 1.2
Date: 2026-08-08
Description: 模型配置编辑页：整页表单化（§11.3）——顶部 provider tab 条 + 全宽纵向字段 + 模型折叠卡片 +
             底部固定保存/重置栏。内存工作副本：open 时 GET 一次，tab 切换不重拉（脏数据 confirm 提示），
             重置 = 放弃修改重拉，保存 = 工作副本全量 PUT（契约 §2.4/§3.11/§3.12）。
             apiKey 遵循 __KEEP__ / $ 引用 / 空串删除规则。
             v1.2：provider 级新增 headers 自定义请求头编辑（每行 Key: Value；空=删除该字段；可用于伪装 UA 绕过中转 CF 拦截）。
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

  var modelConfig = null;       // GET /api/models 原始返回（编辑工作副本，tab 切换不重拉）
  var fetchedApiKeys = {};      // providerId → GET 返回的 apiKey 原值（判断是否为「新输入」）
  var currentProviderId = null; // 当前 tab
  var newProviderIds = {};      // 新建中的 provider（providerId 字段可编辑）
  var expandedModels = [];      // 展开的模型卡片（按对象引用）
  var dirty = false;            // 有未保存修改
  var newProviderSeq = 0;

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }

  function markDirty() {
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

  /* ---------- 渲染 ---------- */

  function render() {
    renderTabs();
    renderForm();
  }

  function renderTabs() {
    tabsEl.innerHTML = '';
    var providers = (modelConfig && modelConfig.providers) || {};
    Object.keys(providers).forEach(function (providerId) {
      var tab = document.createElement('button');
      tab.className = 'provider-tab' + (providerId === currentProviderId ? ' active' : '');
      tab.textContent = providerId;
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

    // api：只读 openai-completions（契约 §2.4 唯一允许值）
    var apiInput = makeTextInput('openai-completions', function () {});
    apiInput.readOnly = true;
    apiInput.style.background = 'var(--gray-block)';
    formEl.appendChild(makeField('api', apiInput));

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
    formEl.appendChild(makeField('apiKey', apiKeyRow, '__KEEP__ 表示保留原值；留空表示删除该字段；$ 开头视为环境变量引用'));

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

    // reasoning 开关
    var reasoningLabel = document.createElement('label');
    reasoningLabel.className = 'model-input-checks';
    var reasoningCheckbox = document.createElement('input');
    reasoningCheckbox.type = 'checkbox';
    reasoningCheckbox.checked = !!model.reasoning;
    reasoningCheckbox.addEventListener('change', function () { model.reasoning = reasoningCheckbox.checked; markDirty(); });
    reasoningLabel.appendChild(reasoningCheckbox);
    body.appendChild(makeField('reasoning', reasoningLabel));

    // thinking.type 下拉：缺省 / enabled / disabled
    body.appendChild(makeField('thinking.type', makeSelect(
      [['', '（缺省）'], ['enabled', 'enabled'], ['disabled', 'disabled']],
      model.thinking && model.thinking.type ? model.thinking.type : '',
      function (v) {
        if (v) model.thinking = { type: v };
        else delete model.thinking;
      }
    )));

    // reasoningEffort 下拉：缺省 / low / medium / high / max；yaml 现值不在列则动态补选项
    var effortPairs = [['', '（缺省）'], ['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['max', 'max']];
    var effortValue = model.reasoningEffort || '';
    var known = effortPairs.some(function (pair) { return pair[0] === effortValue; });
    if (effortValue && !known) effortPairs.push([effortValue, effortValue]);
    body.appendChild(makeField('reasoningEffort', makeSelect(effortPairs, effortValue, function (v) {
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

  // 新建 provider 改名：迁移工作副本 key（撞名/空值拦截并回退渲染）
  function renameProviderId(oldId, nextId) {
    nextId = (nextId || '').trim();
    if (!nextId || nextId === oldId) return;
    if (modelConfig.providers[nextId]) {
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
    render();
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

  // 重置 = 放弃修改重新 GET（脏数据 confirm）
  async function reset() {
    if (dirty && !window.confirm('放弃全部未保存修改并重新拉取配置？')) return;
    await window.settingsView.open();
  }

  function addProvider() {
    if (!modelConfig) return;
    newProviderSeq += 1;
    var providerId = 'newProvider' + newProviderSeq;
    while (modelConfig.providers[providerId]) {
      newProviderSeq += 1;
      providerId = 'newProvider' + newProviderSeq;
    }
    var model = defaultModel();
    modelConfig.providers[providerId] = {
      baseUrl: '',
      api: 'openai-completions',
      apiKey: '',
      models: [model]
    };
    newProviderIds[providerId] = true;
    expandedModels.push(model);
    currentProviderId = providerId;
    markDirty();
    render();
  }

  window.settingsView = {
    // open 时 GET 一次存工作副本；之后 tab 切换不重拉
    open: async function () {
      errorEl.classList.add('hidden');
      tabsEl.innerHTML = '';
      formEl.innerHTML = '';
      dirty = false;
      dirtyHintEl.classList.add('hidden');
      newProviderIds = {};
      expandedModels = [];
      try {
        modelConfig = await window.api.getModels();
        fetchedApiKeys = {};
        Object.keys(modelConfig.providers || {}).forEach(function (providerId) {
          var provider = modelConfig.providers[providerId];
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
      } catch (error) {
        modelConfig = null;
        showError(error.message);
      }
    }
  };

  saveButton.addEventListener('click', save);
  resetButton.addEventListener('click', reset);
})();
