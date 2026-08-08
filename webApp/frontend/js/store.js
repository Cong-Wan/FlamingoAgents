/*
Author: wilbur
Version: 1.2
Date: 2026-08-07
Description: 极简全局状态：token、会话列表、当前会话、流式缓冲（契约 §5 状态机的共享数据层）。v1.1 随包改名 localStorage key 由 flamingoWebToken 改为 webAppToken。v1.2 迭代二：新增 modalStack 弹层 Esc 栈（全局唯一 Esc 分发，只关栈顶一层，方案 §4.3）。
*/
(function () {
  'use strict';

  var TOKEN_KEY = 'webAppToken';

  // 弹层 Esc 栈（迭代二 §4.3）：各弹层 open 时 push 关闭回调，Esc 只关栈顶
  var modalStack = [];
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape' || modalStack.length === 0) return;
    var closeTop = modalStack[modalStack.length - 1];
    event.preventDefault();
    closeTop();
  });

  window.appStore = {
    // 认证 token（仅存 localStorage，仅经 Authorization 头发送）
    token: localStorage.getItem(TOKEN_KEY) || '',

    // 会话列表（GET /api/sessions 全量，updatedAt 倒序）
    sessions: [],

    // 当前打开的会话 id（无则 null）
    currentSessionId: null,

    // 流式状态：null = 空闲；否则 { phase, abort, textBuf, reasoningBuf, block, terminalSeen }
    // phase ∈ 'streaming' | 'waitingConfirm' | 'stopping'
    stream: null,

    setToken: function (token) {
      this.token = token;
      localStorage.setItem(TOKEN_KEY, token);
    },

    clearToken: function () {
      this.token = '';
      localStorage.removeItem(TOKEN_KEY);
    },

    isStreaming: function () {
      return this.stream !== null;
    },

    findSession: function (sessionId) {
      for (var i = 0; i < this.sessions.length; i++) {
        if (this.sessions[i].sessionId === sessionId) return this.sessions[i];
      }
      return null;
    },

    // 弹层 Esc 栈操作：open 时 push，close 时必须调 removeModalClose 出栈（防止重复关闭已关弹层）
    pushModalClose: function (closeFn) { modalStack.push(closeFn); },
    removeModalClose: function (closeFn) {
      var index = modalStack.lastIndexOf(closeFn);
      if (index >= 0) modalStack.splice(index, 1);
    }
  };
})();
