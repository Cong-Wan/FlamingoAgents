/*
Author: wilbur
Version: 1.2
Date: 2026-08-13
Description: 「/」快捷指令面板（迭代二方案 §4.4）：指令注册表（/model 切换当前会话模型、/new 同目录新开会话），
             capture 阶段键盘拦截（§4.3，先于 chatView 的 Enter→send）；不命中指令按普通文本发送。
             另暴露 window.toast 轻提示（供 fileMention/fileExplorer 复用）。
             v1.1：修复 IME 组合态按 Enter 误执行指令（/new 执行后残留 new）——组合中放行让输入法先提交候选词，提交后再按 Enter 才执行。
             v1.2：方向键切换 /model 列表高亮时，用面板自身 scrollTop 把当前项拉进 260px 视口，不带动外层滚动。
*/
(function () {
  'use strict';

  var composerInput = document.getElementById('composerInput');
  var panelEl = document.getElementById('slashPanel');

  var panelOpen = false;
  var items = [];        // 当前面板条目 [{ label, desc, run }]
  var activeIndex = 0;

  /* ---------- 轻提示（全局复用） ---------- */

  var toastEl = null;
  var toastTimer = null;
  window.toast = function (message) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'toast';
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = message;
    toastEl.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove('show'); }, 2500);
  };

  /* ---------- 面板渲染 ---------- */

  function renderPanel() {
    panelEl.innerHTML = '';
    items.forEach(function (item, index) {
      var row = document.createElement('div');
      row.className = 'command-item' + (index === activeIndex ? ' active' : '');
      var label = document.createElement('span');
      label.className = 'command-label';
      label.textContent = item.label;
      row.appendChild(label);
      if (item.desc) {
        var desc = document.createElement('span');
        desc.className = 'command-desc';
        desc.textContent = item.desc;
        row.appendChild(desc);
      }
      row.addEventListener('mousedown', function (event) {
        event.preventDefault(); // 防 blur 抢焦点导致面板先行关闭
        runItem(item);
      });
      panelEl.appendChild(row);
    });
    panelEl.classList.remove('hidden');
    panelOpen = true;
    scrollActiveIntoView();
  }

  // 只改面板自身的 scrollTop，避免 scrollIntoView 沿祖先链带动整页
  function scrollActiveIntoView() {
    var active = panelEl.querySelector('.command-item.active');
    if (!active) return;
    var panelRect = panelEl.getBoundingClientRect();
    var itemRect = active.getBoundingClientRect();
    if (itemRect.top < panelRect.top) {
      panelEl.scrollTop -= panelRect.top - itemRect.top;
    } else if (itemRect.bottom > panelRect.bottom) {
      panelEl.scrollTop += itemRect.bottom - panelRect.bottom;
    }
  }

  function closePanel() {
    panelEl.classList.add('hidden');
    panelEl.innerHTML = '';
    panelOpen = false;
    items = [];
    activeIndex = 0;
  }

  function runItem(item) {
    closePanel();
    composerInput.value = '';
    composerInput.dispatchEvent(new Event('input')); // 触发 autoResize 与 @ 检测复位
    item.run();
  }

  /* ---------- 指令实现 ---------- */

  function openModelPicker() {
    var sessionId = window.appStore.currentSessionId;
    if (!sessionId) return;
    window.api.getModels().then(function (config) {
      var session = window.appStore.findSession(sessionId);
      var modelItems = [];
      Object.keys(config.providers || {}).forEach(function (providerId) {
        var models = config.providers[providerId].models || [];
        models.forEach(function (model) {
          modelItems.push({
            label: providerId + ' / ' + model.id,
            desc: session && session.providerId === providerId && session.modelId === model.id ? '当前模型' : '',
            run: function () { switchModel(providerId, model.id); }
          });
        });
      });
      if (modelItems.length === 0) {
        window.toast('无可用模型（models.yaml 为空）');
        return;
      }
      items = modelItems;
      activeIndex = 0;
      renderPanel();
    }).catch(function (error) {
      window.toast('加载模型失败：' + error.message);
    });
  }

  async function switchModel(providerId, modelId) {
    var sessionId = window.appStore.currentSessionId;
    var session = window.appStore.findSession(sessionId);
    if (session && session.providerId === providerId && session.modelId === modelId) {
      window.toast('已是当前模型');
      return;
    }
    try {
      await window.api.updateSessionModel(sessionId, providerId, modelId);
      if (window.appStore.stream && window.appStore.stream.phase === 'waitingConfirm') {
        window.chatView.discardPendingConfirm(); // 切换模型放弃待确认（§3.3 已声明）
        window.toast('已切换到 ' + providerId + ' / ' + modelId + '（已放弃当前待确认）');
      } else {
        window.toast('已切换到 ' + providerId + ' / ' + modelId);
      }
      await window.sidebarView.refresh(); // 同步 appStore.sessions，syncTopbar 才读得到新 modelId
      window.chatView.syncTopbar();
      window.statusBar.refresh();
    } catch (error) {
      if (error.status === 409) {
        // 索引已是新模型，本轮仍跑旧模型（§3.3 语义）
        window.toast(error.message);
        await window.sidebarView.refresh();
        window.chatView.syncTopbar();
        window.statusBar.refresh();
      } else {
        window.toast('切换失败：' + error.message);
      }
    }
  }

  async function newSessionHere() {
    var sessionId = window.appStore.currentSessionId;
    if (!sessionId) return;
    var session = window.appStore.findSession(sessionId);
    if (!session) {
      // 兜底：本地列表未包含当前会话（如会话由他端创建）时先刷新再查，仍无则提示而非静默无反应
      await window.sidebarView.refresh().catch(function () {});
      session = window.appStore.findSession(sessionId);
    }
    if (!session) {
      window.toast('会话数据未加载，请刷新页面');
      return;
    }
    try {
      var created = await window.api.createSession({
        providerId: session.providerId,
        modelId: session.modelId,
        workDir: session.workDir,
        allowCreate: false
      });
      await window.sidebarView.refresh();
      location.hash = '#/chat/' + created.sessionId;
      window.toast('已在当前目录新开一个会话');
    } catch (error) {
      window.toast('新建失败：' + error.message);
    }
  }

  // 指令注册表（方案 §4.4）：后续加指令只需在这里追加 { name, desc, run }
  var commandRegistry = [
    { name: '/model', desc: '切换当前会话模型', run: openModelPicker },
    { name: '/new', desc: '在当前目录新开一个会话', run: newSessionHere }
  ];

  /* ---------- 触发与键盘 ---------- */

  function onInput() {
    var value = composerInput.value;
    if (value.indexOf('/') === 0 && !/\s/.test(value) && window.appStore.currentSessionId) {
      var keyword = value.slice(1).toLowerCase();
      items = commandRegistry
        .filter(function (command) { return command.name.slice(1).indexOf(keyword) === 0; })
        .map(function (command) {
          return { label: command.name, desc: command.desc, run: command.run };
        });
      if (items.length > 0) {
        activeIndex = Math.min(activeIndex, items.length - 1);
        renderPanel();
        return;
      }
    }
    // 模型选择子面板打开时输入变化不关（面板与输入脱钩）；其余情况输入不符即关
    if (panelOpen && items.length > 0 && items[0].label.indexOf('/') !== 0) return;
    closePanel();
  }

  composerInput.addEventListener('input', onInput);

  // capture 阶段（§4.3）：面板打开时拦截 Enter/Tab/Esc/↑↓，先于 chatView 的 Enter→send。
  // 注意：textarea 是事件目标本身，同节点监听器按注册顺序都执行，必须 stopImmediatePropagation。
  composerInput.addEventListener('keydown', function (event) {
    if (!panelOpen) return;
    // IME 组合态放行：此时 Enter 是提交候选词而非执行指令，否则 runItem 清空输入后 compositionend 会把残留字符写回
    if (event.isComposing || event.keyCode === 229) return;
    if (['Enter', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown'].indexOf(event.key) < 0) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    if (event.key === 'Escape') {
      closePanel();
    } else if (event.key === 'ArrowUp') {
      activeIndex = (activeIndex - 1 + items.length) % items.length;
      renderPanel();
    } else if (event.key === 'ArrowDown') {
      activeIndex = (activeIndex + 1) % items.length;
      renderPanel();
    } else if (items[activeIndex]) { // Enter / Tab
      runItem(items[activeIndex]);
    }
  }, true);

  window.slashCommand = {
    isOpen: function () { return panelOpen; }
  };
})();
