/*
Author: wilbur
Version: 1.4
Date: 2026-08-14
Description: 侧栏视图：会话列表（今天/昨天/更早分组）、新建会话弹窗、重命名/删除、底部入口高亮。
             v1.1 迭代一：新建弹窗探建交互（契约 §3.3/§3.4 先探后建 + allowCreate 内联确认）；
             侧栏完全隐藏/悬浮展开（localStorage sidebarCollapsed）。
             v1.2（composerFocusShortcutPlan F2 改方案 A）：openModal 经 window.sidebarView 暴露，
             供 Cmd/Ctrl+K 快捷键直达「新建会话」弹窗（含 workDir 探建确认）。
             v1.3 新建会话模型下拉去掉「默认」占位项，直接列出该 provider 的 modelId。
             v1.4 新建会话弹窗支持快捷键：Enter = 创建，Esc = 取消。
*/
(function () {
  'use strict';

  var listEl = document.getElementById('sessionList');
  var modalEl = document.getElementById('newSessionModal');
  var workDirInput = document.getElementById('newSessionWorkDir');
  var workDirErrorEl = document.getElementById('workDirError');
  var confirmAreaEl = document.getElementById('workDirCreateConfirm');
  var confirmPathEl = document.getElementById('workDirCreatePath');
  var confirmCreateButton = document.getElementById('workDirCreateAllow');
  var providerSelect = document.getElementById('newSessionProvider');
  var modelSelect = document.getElementById('newSessionModel');
  var errorEl = document.getElementById('newSessionError');
  var createButton = document.getElementById('newSessionCreate');

  var cachedModelConfig = null; // 新建弹窗的 provider/model 下拉数据源（GET /api/models）
  var pendingCreatePath = null; // probe 确认的 resolvedPath（确认创建时提交它，而非用户原始输入）

  /* ---------- 侧栏完全隐藏/悬浮展开（§11.2，状态存 localStorage） ---------- */

  var sidebarEl = document.querySelector('.sidebar');
  var collapseButton = document.getElementById('sidebarCollapseButton');
  var expandButton = document.getElementById('sidebarExpandButton');

  function applySidebarCollapsed(collapsed) {
    sidebarEl.classList.toggle('hidden', collapsed); // display:none 完全隐藏，展开为 flex 推挤式
    expandButton.classList.toggle('hidden', !collapsed);
    document.getElementById('app').classList.toggle('sidebar-collapsed', collapsed);
    try { localStorage.setItem('sidebarCollapsed', collapsed ? '1' : '0'); } catch (ignore) { /* 隐私模式 */ }
  }

  collapseButton.addEventListener('click', function () { applySidebarCollapsed(true); });
  expandButton.addEventListener('click', function () { applySidebarCollapsed(false); });
  applySidebarCollapsed((function () {
    try { return localStorage.getItem('sidebarCollapsed') === '1'; } catch (ignore) { return false; }
  })());

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
    if (provider && Array.isArray(provider.models)) {
      provider.models.forEach(function (model) {
        var option = document.createElement('option');
        option.value = model.id;
        option.textContent = model.id;
        modelSelect.appendChild(option);
      });
    }
  }

  function hideProbeFeedback() {
    workDirErrorEl.classList.add('hidden');
    confirmAreaEl.classList.add('hidden');
    pendingCreatePath = null;
  }

  function showWorkDirError(message) {
    workDirErrorEl.textContent = message;
    workDirErrorEl.classList.remove('hidden');
  }

  async function openModal() {
    errorEl.classList.add('hidden');
    hideProbeFeedback();
    workDirInput.value = '';
    createButton.disabled = false;
    try {
      if (!cachedModelConfig) cachedModelConfig = await window.api.getModels();
      fillProviderOptions(cachedModelConfig);
    } catch (error) {
      errorEl.textContent = '加载模型配置失败：' + error.message;
      errorEl.classList.remove('hidden');
    }
    // 预填 defaultWorkDir（契约 §3.4 空 workDir 会 400，以 '/' 占位调 probe 仅取 defaultWorkDir）
    try {
      var probe = await window.api.probeWorkDir('/');
      if (probe.defaultWorkDir) workDirInput.value = probe.defaultWorkDir;
    } catch (ignore) { /* 预填失败不阻塞弹窗 */ }
    modalEl.classList.remove('hidden');
  }

  function closeModal() {
    modalEl.classList.add('hidden');
    hideProbeFeedback();
  }

  // 提交 create（契约 §3.3 非幂等 L4：按钮禁用至响应返回）；400 错误在弹窗内展示
  async function submitCreate(workDir, allowCreate) {
    var params = { providerId: providerSelect.value, workDir: workDir, allowCreate: allowCreate };
    if (modelSelect.value) params.modelId = modelSelect.value;
    createButton.disabled = true;
    confirmCreateButton.disabled = true;
    errorEl.classList.add('hidden');
    try {
      var session = await window.api.createSession(params);
      closeModal();
      await window.sidebarView.refresh();
      location.hash = '#/chat/' + session.sessionId;
    } catch (error) {
      errorEl.textContent = error.message;
      errorEl.classList.remove('hidden');
    } finally {
      createButton.disabled = false;
      confirmCreateButton.disabled = false;
    }
  }

  // 点「创建」：先 probe（契约 §3.4，判定只用 creatable/willCreate 两个布尔）
  async function onCreate() {
    errorEl.classList.add('hidden');
    hideProbeFeedback();
    if (!providerSelect.value) {
      errorEl.textContent = '请选择 provider。';
      errorEl.classList.remove('hidden');
      return;
    }
    var workDir = workDirInput.value.trim();
    if (!workDir) {
      showWorkDirError('workDir 必填。');
      return;
    }
    createButton.disabled = true;
    var probe;
    try {
      probe = await window.api.probeWorkDir(workDir);
    } catch (error) {
      showWorkDirError(error.message);
      createButton.disabled = false;
      return;
    }
    createButton.disabled = false;
    if (probe.creatable && !probe.willCreate) {
      await submitCreate(probe.resolvedPath || workDir, false); // 目录已存在且可写 → 直接建
    } else if (probe.creatable && probe.willCreate) {
      pendingCreatePath = probe.resolvedPath || workDir; // 需确认后自动创建
      confirmPathEl.textContent = pendingCreatePath;
      confirmAreaEl.classList.remove('hidden');
    } else {
      showWorkDirError(probe.message || '该目录不可用。'); // 不可建 → 红字拦截
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
    invalidateModelConfig: function () { cachedModelConfig = null; },

    // Cmd/Ctrl+K 快捷键直达「新建会话」弹窗（方案 A，composerFocusShortcutPlan）；私有 openModal 的对外口
    openNewSessionModal: function () { openModal(); }
  };

  document.getElementById('newSessionButton').addEventListener('click', openModal);
  document.getElementById('newSessionCancel').addEventListener('click', closeModal);
  createButton.addEventListener('click', onCreate);
  confirmCreateButton.addEventListener('click', function () {
    if (pendingCreatePath) submitCreate(pendingCreatePath, true); // 确认创建 → allowCreate:true
  });
  workDirInput.addEventListener('input', hideProbeFeedback); // 改动路径后收起确认区/红字
  providerSelect.addEventListener('change', fillModelOptions);
  modalEl.addEventListener('click', function (event) {
    if (event.target === modalEl) closeModal();
  });
  // 快捷键：Enter = 创建 / Esc = 取消（挂 document，只判断弹窗可见，不受焦点位置影响）
  document.addEventListener('keydown', function (event) {
    if (modalEl.classList.contains('hidden')) return; // 弹窗未打开时不处理
    if (event.key === 'Enter' && event.target.tagName !== 'TEXTAREA') {
      event.preventDefault();
      onCreate();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeModal();
    }
  });

})();
