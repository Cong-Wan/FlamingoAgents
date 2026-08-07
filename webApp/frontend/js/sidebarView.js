/*
Author: wilbur
Version: 1.0
Date: 2026-08-07
Description: 侧栏视图：会话列表（今天/昨天/更早分组）、新建会话弹窗、重命名/删除、底部入口高亮
*/
(function () {
  'use strict';

  var listEl = document.getElementById('sessionList');
  var modalEl = document.getElementById('newSessionModal');
  var workDirInput = document.getElementById('newSessionWorkDir');
  var providerSelect = document.getElementById('newSessionProvider');
  var modelSelect = document.getElementById('newSessionModel');
  var errorEl = document.getElementById('newSessionError');
  var createButton = document.getElementById('newSessionCreate');

  var cachedModelConfig = null; // 新建弹窗的 provider/model 下拉数据源（GET /api/models）

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  function groupLabelOf(isoTime) {
    var date = new Date(isoTime);
    var now = new Date();
    if (sameDay(date, now)) return '今天';
    var yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (sameDay(date, yesterday)) return '昨天';
    return '更早';
  }

  function buildSessionItem(session) {
    var item = document.createElement('div');
    item.className = 'session-item' + (session.sessionId === window.appStore.currentSessionId ? ' active' : '');
    item.dataset.sessionId = session.sessionId;

    var title = document.createElement('span');
    title.className = 'session-item-title';
    title.textContent = session.title || '新会话';
    item.appendChild(title);

    var usage = document.createElement('span');
    usage.className = 'session-item-usage';
    var total = session.usage
      ? (session.usage.promptTokens || 0) + (session.usage.completionTokens || 0)
      : 0;
    usage.textContent = total > 0 ? formatCompact(total) : '';
    item.appendChild(usage);

    var actions = document.createElement('span');
    actions.className = 'session-item-actions';

    var renameBtn = document.createElement('button');
    renameBtn.className = 'session-action-btn';
    renameBtn.title = '重命名';
    renameBtn.textContent = '✎';
    renameBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      onRename(session);
    });

    var deleteBtn = document.createElement('button');
    deleteBtn.className = 'session-action-btn';
    deleteBtn.title = '删除';
    deleteBtn.textContent = '🗑';
    deleteBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      onDelete(session);
    });

    actions.appendChild(renameBtn);
    actions.appendChild(deleteBtn);
    item.appendChild(actions);

    item.addEventListener('click', function () {
      location.hash = '#/chat/' + session.sessionId;
    });
    return item;
  }

  function formatCompact(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
    return String(num);
  }

  async function onRename(session) {
    var next = window.prompt('重命名会话', session.title || '');
    if (next === null) return;
    next = next.trim();
    if (!next || next === session.title) return;
    try {
      await window.api.renameSession(session.sessionId, next);
      await window.sidebarView.refresh();
      if (session.sessionId === window.appStore.currentSessionId && window.chatView) {
        window.chatView.syncTopbar();
      }
    } catch (error) {
      window.alert(error.message);
    }
  }

  async function onDelete(session) {
    if (!window.confirm('删除会话「' + (session.title || '新会话') + '」？历史记录将一并删除。')) return;
    try {
      await window.api.deleteSession(session.sessionId);
      if (session.sessionId === window.appStore.currentSessionId) {
        location.hash = '#/chat';
      }
      await window.sidebarView.refresh();
    } catch (error) {
      window.alert(error.message);
    }
  }

  function fillProviderOptions(config) {
    providerSelect.innerHTML = '';
    var ids = Object.keys(config.providers || {});
    ids.forEach(function (id) {
      var option = document.createElement('option');
      option.value = id;
      option.textContent = id;
      providerSelect.appendChild(option);
    });
    fillModelOptions();
  }

  function fillModelOptions() {
    modelSelect.innerHTML = '';
    var providerId = providerSelect.value;
    var provider = cachedModelConfig && cachedModelConfig.providers
      ? cachedModelConfig.providers[providerId] : null;
    var defaultOption = document.createElement('option');
    defaultOption.value = '';
    defaultOption.textContent = '默认（该 provider 首个模型）';
    modelSelect.appendChild(defaultOption);
    if (provider && Array.isArray(provider.models)) {
      provider.models.forEach(function (model) {
        var option = document.createElement('option');
        option.value = model.id;
        option.textContent = model.id + (model.name ? '（' + model.name + '）' : '');
        modelSelect.appendChild(option);
      });
    }
  }

  async function openModal() {
    errorEl.classList.add('hidden');
    workDirInput.value = '';
    createButton.disabled = false;
    try {
      if (!cachedModelConfig) cachedModelConfig = await window.api.getModels();
      fillProviderOptions(cachedModelConfig);
    } catch (error) {
      errorEl.textContent = '加载模型配置失败：' + error.message;
      errorEl.classList.remove('hidden');
    }
    modalEl.classList.remove('hidden');
  }

  function closeModal() {
    modalEl.classList.add('hidden');
  }

  async function onCreate() {
    var providerId = providerSelect.value;
    if (!providerId) {
      errorEl.textContent = '请选择 provider。';
      errorEl.classList.remove('hidden');
      return;
    }
    var params = { providerId: providerId };
    var workDir = workDirInput.value.trim();
    if (workDir) params.workDir = workDir;
    if (modelSelect.value) params.modelId = modelSelect.value;

    // 契约 §3.3 非幂等（L4）：创建按钮防重，禁用至响应返回
    createButton.disabled = true;
    errorEl.classList.add('hidden');
    try {
      var session = await window.api.createSession(params);
      closeModal();
      await window.sidebarView.refresh();
      location.hash = '#/chat/' + session.sessionId;
    } catch (error) {
      errorEl.textContent = error.message;
      errorEl.classList.remove('hidden');
      createButton.disabled = false;
    }
  }

  window.sidebarView = {
    // 拉取会话列表并重绘（启动、completed 后、增删改后调用）
    refresh: async function () {
      var data = await window.api.getSessions();
      window.appStore.sessions = data.sessions || [];
      window.sidebarView.render();
    },

    render: function () {
      listEl.innerHTML = '';
      var groups = [
        { label: '今天', items: [] },
        { label: '昨天', items: [] },
        { label: '更早', items: [] }
      ];
      window.appStore.sessions.forEach(function (session) {
        var label = groupLabelOf(session.updatedAt || session.createdAt);
        for (var i = 0; i < groups.length; i++) {
          if (groups[i].label === label) { groups[i].items.push(session); break; }
        }
      });
      groups.forEach(function (group) {
        if (group.items.length === 0) return;
        var labelEl = document.createElement('div');
        labelEl.className = 'session-group-label';
        labelEl.textContent = group.label;
        listEl.appendChild(labelEl);
        group.items.forEach(function (session) {
          listEl.appendChild(buildSessionItem(session));
        });
      });
    },

    // 新建会话成功后模型配置可能变化，使缓存失效
    invalidateModelConfig: function () { cachedModelConfig = null; }
  };

  document.getElementById('newSessionButton').addEventListener('click', openModal);
  document.getElementById('newSessionCancel').addEventListener('click', closeModal);
  createButton.addEventListener('click', onCreate);
  providerSelect.addEventListener('change', fillModelOptions);
  modalEl.addEventListener('click', function (event) {
    if (event.target === modalEl) closeModal();
  });
})();
