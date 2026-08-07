/*
Author: wilbur
Version: 1.0
Date: 2026-08-07
Description: 启动引导 + 登录门 + hash 路由（#/chat、#/chat/{id}、#/settings/models、#/usage）
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
    [chatPageEl, settingsPageEl, usagePageEl].forEach(function (el) {
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
