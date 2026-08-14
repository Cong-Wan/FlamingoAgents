/*
Author: wilbur
Version: 1.5
Date: 2026-08-14
Description: 启动引导 + 登录门 + hash 路由（#/chat、#/chat/{id}、#/settings/models、#/settings/skills、#/usage）。
             v1.1（composerFocusShortcutPlan T2）：全局快捷键 Cmd+N（mac）/ Ctrl+N（win）新建应用窗口
             （浏览器保留键拦不住时走原生行为；登录门态/弹层打开时不响应）。
             v1.2：快捷键 N → K（Cmd+K / Ctrl+K）——N 是浏览器保留键普通标签页拦不到，
             K 可派发可 preventDefault（GitHub/Linear 同款），拦截后不再走浏览器地址栏搜索。
             v1.3（F2 改方案 A）：快捷键语义由「新开浏览器窗口」改为「直达新建会话弹窗」——
             用户反馈新建窗口无 workDir 确认流程，真正诉求是快速新建会话；调 sidebarView.openNewSessionModal()。
             v1.4（F2 discoverability）：启动时按平台渲染「新建会话」按钮的快捷键提示
             （navigator.platform 判 mac 显 ⌘K，其余显 Ctrl+K；含 kbd 文本与 title 悬停）。
             v1.5：新增与模型配置平级的「技能」只读页路由 #/settings/skills（skillsView）。
*/
(function () {
  'use strict';

  var loginGateEl = document.getElementById('loginGate');
  var appEl = document.getElementById('app');
  var loginTokenInput = document.getElementById('loginTokenInput');
  var loginErrorEl = document.getElementById('loginError');
  var loginButton = document.getElementById('loginButton');

  var chatPageEl = document.getElementById('chatPage');
  var settingsPageEl = document.getElementById('settingsPage');
  var skillsPageEl = document.getElementById('skillsPage');
  var usagePageEl = document.getElementById('usagePage');

  /* ---------- 登录门 ---------- */

  function showLoginGate(message) {
    window.chatView.close();
    appEl.classList.add('hidden');
    loginGateEl.classList.remove('hidden');
    if (message) {
      loginErrorEl.textContent = message;
      loginErrorEl.classList.remove('hidden');
    } else {
      loginErrorEl.classList.add('hidden');
    }
    loginTokenInput.value = '';
    loginTokenInput.focus();
  }

  function hideLoginGate() {
    loginGateEl.classList.add('hidden');
    appEl.classList.remove('hidden');
  }

  async function onLogin() {
    var token = loginTokenInput.value.trim();
    if (!token) return;
    loginButton.disabled = true;
    loginErrorEl.classList.add('hidden');
    try {
      await window.api.login(token);
      window.appStore.setToken(token); // 验证通过后存 localStorage，作为后续 Bearer 头
      hideLoginGate();
      await boot();
    } catch (error) {
      loginErrorEl.textContent = error.status === 401 ? 'token 不正确。' : error.message;
      loginErrorEl.classList.remove('hidden');
    } finally {
      loginButton.disabled = false;
    }
  }

  /* ---------- hash 路由 ---------- */

  function showPage(pageEl) {
    [chatPageEl, settingsPageEl, skillsPageEl, usagePageEl].forEach(function (el) {
      el.classList.toggle('hidden', el !== pageEl);
    });
  }

  async function route() {
    if (!window.appStore.token) return; // 登录门态不路由
    var hash = location.hash || '#/chat';

    if (hash.indexOf('#/chat/') === 0) {
      var sessionId = decodeURIComponent(hash.slice('#/chat/'.length));
      if (sessionId === window.appStore.currentSessionId && chatPageEl.classList.contains('hidden') === false) {
        return; // 已在该会话页
      }
      window.chatView.close();
      showPage(chatPageEl);
      await window.chatView.open(sessionId);
      window.sidebarView.render(); // open 后更新 active 高亮
      return;
    }

    window.chatView.close();
    window.appStore.currentSessionId = null;
    window.sidebarView.render();

    if (hash === '#/settings/models') {
      showPage(settingsPageEl);
      await window.settingsView.open();
    } else if (hash === '#/settings/skills') {
      showPage(skillsPageEl);
      window.skillsView.open();
    } else if (hash === '#/usage') {
      showPage(usagePageEl);
      await window.usageView.open();
    } else { // '#/chat' 及其它 → 聊天首页
      showPage(chatPageEl);
      window.chatView.showEmpty();
    }
  }

  /* ---------- 启动引导 ---------- */

  async function boot() {
    try {
      await window.sidebarView.refresh();
    } catch (error) {
      if (error.status === 401) return; // 401 已由 api 层跳登录门
    }
    await route();
  }

  window.addEventListener('hashchange', route);

  // 全局快捷键（composerFocusShortcutPlan F2 方案 A）：Cmd+K（mac）/ Ctrl+K（win）直达「新建会话」弹窗
  // 按钮快捷键提示按平台渲染（mac 显示 ⌘K，win 显示 Ctrl+K）
  (function renderShortcutHint() {
    var isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    var hint = isMac ? '⌘K' : 'Ctrl+K';
    var kbdEl = document.querySelector('#newSessionButton .kbd-hint');
    if (kbdEl) kbdEl.textContent = hint;
    var btn = document.getElementById('newSessionButton');
    if (btn) btn.title = '新建会话（' + hint + '）';
  })();

  document.addEventListener('keydown', function (event) {
    if (!(event.metaKey || event.ctrlKey)) return;
    if (event.altKey || event.shiftKey) return; // 不抢 Cmd+Option+K / Cmd+Shift+K 等其它组合
    if (event.key.toLowerCase() !== 'k') return;
    if (appEl.classList.contains('hidden')) return; // 登录门态不响应
    if (document.querySelector('.modal-mask:not(.hidden)')) return; // 弹层打开时不响应（含新建会话弹窗自身）
    event.preventDefault(); // 拦截浏览器默认行为（地址栏搜索）
    window.sidebarView.openNewSessionModal(); // 方案 A：打开新建会话弹窗（含 workDir 探建确认）
  });

  loginButton.addEventListener('click', onLogin);
  loginTokenInput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') onLogin();
  });

  // 任意请求 401 → 清 token → 登录门（契约 §5）
  window.api.setUnauthorizedHandler(function () {
    showLoginGate('登录已失效，请重新输入 token。');
  });

  if (window.appStore.token) {
    hideLoginGate();
    boot();
  } else {
    showLoginGate();
  }
})();
