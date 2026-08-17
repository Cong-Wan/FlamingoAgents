/*
Author: wilbur
Version: 1.5
Date: 2026-08-17
Description: 侧栏视图：会话列表（今天/昨天/更早分组）、新建会话弹窗、重命名/删除、底部入口高亮。
             v1.1 迭代一：新建弹窗探建交互（契约 §3.3/§3.4 先探后建 + allowCreate 内联确认）；
             侧栏完全隐藏/悬浮展开（localStorage sidebarCollapsed）。
             v1.2（composerFocusShortcutPlan F2 改方案 A）：openModal 经 window.sidebarView 暴露，
             供 Cmd/Ctrl+K 快捷键直达「新建会话」弹窗（含 workDir 探建确认）。
             v1.3 新建会话模型下拉去掉「默认」占位项，直接列出该 provider 的 modelId。
             v1.4 新建会话弹窗支持快捷键：Enter = 创建，Esc = 取消。
             v1.5（workDirPickerPlan §2.2）：workDir 输入框 VSCode 式目录补全--splitWorkDirInput/joinWorkDir 纯函数、防抖 200ms + 请求序号丢过期响应、
             mousedown 回填下钻、改写现有 document keydown（IME 放行 + suggestVisible 闸门 + Tab/-> 仅末尾 + Enter 有高亮回填/无高亮创建 + Esc 先关下拉）、
             setWorkDirValue 显式回填（.value 赋值不触发 input）、closeModal 清下拉与防抖。
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

  // ---------- workDir 补全（workDirPickerPlan §2.2） ----------

  var suggestEl = document.getElementById('workDirSuggest');
  var suggestItems = [];      // 当前展示的目录名（前缀过滤后）
  var suggestIndex = 0;       // 高亮下标，渲染后默认 0
  var suggestSeq = 0;         // 请求序号：丢弃过期响应
  var suggestTimer = null;    // 防抖 200ms
  var suggestBrowsePath = ''; // 当前列表对应的浏览目录（回填拼接用）

  function splitWorkDirInput(raw) {
    // 目录部分与前缀切分（评审问题 1/7）：空目录部分归一为 '/'；~user 无斜杠时整段交后端 expanduser，不当根目录前缀。
    var value = String(raw || '');
    if (value === '' || value === '/') return { browsePath: '/', prefix: '' };
    if (value === '~' || value === '~/') return { browsePath: '~', prefix: '' };
    var slash = value.lastIndexOf('/');
    if (slash < 0) {
      if (value.charAt(0) === '~') return { browsePath: value, prefix: '' };
      return { browsePath: '/', prefix: value };
    }
    var browsePath = value.slice(0, slash);
    var prefix = value.slice(slash + 1);
    if (browsePath === '') browsePath = '/';
    return { browsePath: browsePath, prefix: prefix };
  }

  function joinWorkDir(browsePath, name) {
    // 回填拼接（复审 A）：browsePath 不含尾斜杠，统一拼 '/'；'/' 时拼 '/name/'，避免丢中间 '/' 或退化相对路径。
    var base = browsePath === '/' ? '' : browsePath;
    return base + '/' + name + '/';
  }

  function suggestVisible() {
    return !suggestEl.classList.contains('hidden') && suggestItems.length > 0;
  }

  function hideSuggest() {
    suggestEl.classList.add('hidden');
    suggestEl.innerHTML = '';
    suggestItems = [];
    suggestIndex = 0;
  }

  function renderSuggest() {
    suggestEl.innerHTML = '';
    suggestItems.forEach(function (name, index) {
      var item = document.createElement('div');
      item.className = 'workdir-suggest-item' + (index === suggestIndex ? ' active' : '');
      var icon = document.createElement('span');
      icon.textContent = '📁';
      item.appendChild(icon);
      var label = document.createElement('span');
      label.textContent = name;
      item.appendChild(label);
      // mousedown + preventDefault：避免 input 的 blur 先于 click 拆掉下拉导致点不中（评审问题 4）
      item.addEventListener('mousedown', function (event) {
        event.preventDefault();
        applySuggest(name);
      });
      item.addEventListener('mousemove', function () {
        if (suggestIndex !== index) { suggestIndex = index; updateSuggestActive(); }
      });
      suggestEl.appendChild(item);
    });
    suggestEl.classList.remove('hidden');
  }

  function updateSuggestActive() {
    var rows = suggestEl.children;
    for (var i = 0; i < rows.length; i++) {
      rows[i].classList.toggle('active', i === suggestIndex);
    }
  }

  function moveSuggestHighlight(delta) {
    if (!suggestVisible()) return;
    var count = suggestItems.length;
    suggestIndex = (suggestIndex + delta + count) % count;
    updateSuggestActive();
    var active = suggestEl.children[suggestIndex];
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
  }

  function applySuggest(name) {
    setWorkDirValue(joinWorkDir(suggestBrowsePath, name));
  }

  async function fetchSuggest() {
    var value = workDirInput.value;
    if (!value.trim()) { hideSuggest(); return; } // 空输入不发请求
    var parts = splitWorkDirInput(value);
    var seq = ++suggestSeq;
    try {
      var data = await window.api.listFsDir(parts.browsePath);
      if (seq !== suggestSeq) return; // 过期响应丢弃
      var lower = parts.prefix.toLowerCase();
      var names = (data.entries || []).map(function (entry) { return entry.name; })
        .filter(function (name) { return !lower || name.toLowerCase().indexOf(lower) === 0; });
      if (names.length === 0) { hideSuggest(); return; }
      suggestBrowsePath = parts.browsePath;
      suggestItems = names;
      suggestIndex = 0;
      renderSuggest();
    } catch (ignore) {
      if (seq === suggestSeq) hideSuggest(); // 浏览失败隐藏下拉，不打断输入；错误交给 probe 红字
    }
  }

  function scheduleSuggest() {
    if (suggestTimer) clearTimeout(suggestTimer);
    suggestTimer = setTimeout(function () {
      suggestTimer = null;
      fetchSuggest();
    }, 200);
  }

  function setWorkDirValue(next) {
    // 显式回填（评审问题 4）：.value 赋值不触发 input 事件，必须手动收反馈 + 触发补全。
    workDirInput.value = next;
    hideProbeFeedback();
    scheduleSuggest();
  }

  function isCaretAtEnd() {
    return workDirInput.selectionStart === workDirInput.value.length
      && workDirInput.selectionEnd === workDirInput.value.length;
  }

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
    hideSuggest();
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
      if (probe.defaultWorkDir) setWorkDirValue(probe.defaultWorkDir); // 显式函数：赋值+收反馈+触发补全（评审问题 4）
    } catch (ignore) { /* 预填失败不阻塞弹窗 */ }
    modalEl.classList.remove('hidden');
    workDirInput.focus(); // 打开即触发 focus 下的补全链路（cursor 置尾）
  }

  function closeModal() {
    modalEl.classList.add('hidden');
    hideProbeFeedback();
    hideSuggest();
    suggestSeq++; // 弹窗关后丢弃在途响应，防 200ms 后又把下拉画出来
    if (suggestTimer) { clearTimeout(suggestTimer); suggestTimer = null; }
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
  workDirInput.addEventListener('input', function () {
    hideProbeFeedback(); // 改动路径后收起确认区/红字
    scheduleSuggest();   // 用户手输触发补全
  });
  providerSelect.addEventListener('change', fillModelOptions);
  modalEl.addEventListener('click', function (event) {
    if (event.target === modalEl) closeModal();
  });
  // 快捷键：改写原有监听（评审问题 3：禁止平行再挂），先服务下拉再走弹窗原逻辑；IME 放行。
  document.addEventListener('keydown', function (event) {
    if (modalEl.classList.contains('hidden')) return; // 弹窗未打开时不处理
    if (event.isComposing || event.keyCode === 229) return; // 中文输入法回车上屏不当作创建/选中
    if (suggestVisible()) {
      if (event.key === 'ArrowDown') { event.preventDefault(); moveSuggestHighlight(1); return; }
      if (event.key === 'ArrowUp') { event.preventDefault(); moveSuggestHighlight(-1); return; }
      if (event.key === 'Tab' || (event.key === 'ArrowRight' && isCaretAtEnd())) {
        event.preventDefault();
        applySuggest(suggestItems[suggestIndex]);
        return;
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        applySuggest(suggestItems[suggestIndex]); // 下拉可见有高亮 -> 只回填不创建
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        hideSuggest(); // 先关下拉，不关弹窗
        return;
      }
    }
    if (event.key === 'Enter' && event.target.tagName !== 'TEXTAREA') {
      event.preventDefault();
      onCreate();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeModal();
    }
  });

})();
