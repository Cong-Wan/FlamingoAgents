/*
Author: wilbur
Version: 1.1
Date: 2026-08-07
Description: 极简全局状态：token、会话列表、当前会话、流式缓冲（契约 §5 状态机的共享数据层）。v1.1 随包改名 localStorage key 由 flamingoWebToken 改为 webAppToken。
*/
(function () {
  'use strict';

  var TOKEN_KEY = 'webAppToken';

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
    }
  };
})();
