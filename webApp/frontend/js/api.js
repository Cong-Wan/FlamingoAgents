/*
Author: wilbur
Version: 1.3
Date: 2026-08-13
Description: fetch 封装：自动带 Bearer token；任意请求 401 → 清 token → 登录门（契约 §1.1/§3.1/§5）。
             v1.1 迭代一：新增 probeWorkDir（§3.4）与 getUsageSeries（§3.10）。
             v1.2 迭代二（方案 §4.9）：新增 getSessionStatus / updateSessionModel / listFiles / getFileContent。
             v1.3 上传 models.json 导入：新增 importPiModels → POST /api/models/importPi { rawText }。
*/
(function () {
  'use strict';

  // 由 main.js 注入：401 时跳登录门
  var onUnauthorized = function () {};

  // 统一错误对象：{ status, message }
  function buildError(status, message) {
    var error = new Error(message || ('请求失败（' + status + '）'));
    error.status = status;
    return error;
  }

  async function parseErrorBody(resp) {
    try {
      var body = await resp.json();
      if (body && body.error) return body.error;
    } catch (ignore) { /* 非 JSON 响应 */ }
    return '请求失败（' + resp.status + '）';
  }

  async function request(path, options) {
    options = options || {};
    var headers = { 'Accept': 'application/json' };
    if (window.appStore.token) {
      headers['Authorization'] = 'Bearer ' + window.appStore.token;
    }
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json; charset=utf-8';
    }
    var resp = await fetch(path, {
      method: options.method || 'GET',
      headers: headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined
    });
    if (resp.status === 401) {
      window.appStore.clearToken();
      onUnauthorized();
      throw buildError(401, '未认证或 token 已失效');
    }
    if (!resp.ok) {
      throw buildError(resp.status, await parseErrorBody(resp));
    }
    return resp.json();
  }

  window.api = {
    setUnauthorizedHandler: function (handler) { onUnauthorized = handler; },

    // 供 sse.js 在流接口收到 401 时复用同一入口
    fireUnauthorized: function () { onUnauthorized(); },

    // 登录（唯一免认证接口）：token 正确返回 true，错误抛 401
    login: async function (token) {
      var resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json' },
        body: JSON.stringify({ token: token })
      });
      if (!resp.ok) throw buildError(resp.status, await parseErrorBody(resp));
      return true;
    },

    getSessions: function () { return request('/api/sessions'); },
    probeWorkDir: function (workDir) {
      return request('/api/sessions/probeWorkDir', { method: 'POST', body: { workDir: workDir } });
    },
    createSession: function (params) { return request('/api/sessions', { method: 'POST', body: params }); },
    renameSession: function (sessionId, title) {
      return request('/api/sessions/' + encodeURIComponent(sessionId), { method: 'PATCH', body: { title: title } });
    },
    deleteSession: function (sessionId) {
      return request('/api/sessions/' + encodeURIComponent(sessionId), { method: 'DELETE' });
    },
    getMessages: function (sessionId) {
      return request('/api/sessions/' + encodeURIComponent(sessionId) + '/messages');
    },
    getPending: function (sessionId) {
      return request('/api/sessions/' + encodeURIComponent(sessionId) + '/pending');
    },
    stopChat: function (sessionId) {
      return request('/api/chat/stop', { method: 'POST', body: { sessionId: sessionId } });
    },
    getUsage: function () { return request('/api/usage'); },
    getUsageSeries: function (granularity) {
      return request('/api/usage/series?granularity=' + encodeURIComponent(granularity || 'day'));
    },
    getModels: function () { return request('/api/models'); },
    putModels: function (config) { return request('/api/models', { method: 'PUT', body: config }); },
    importPiModels: function (rawText) {
      return request('/api/models/importPi', { method: 'POST', body: { rawText: rawText } });
    },

    // 迭代二：状态栏 / /model 指令 / 文件浏览器与@附件
    getSessionStatus: function (sessionId) {
      return request('/api/sessions/' + encodeURIComponent(sessionId) + '/status');
    },
    updateSessionModel: function (sessionId, providerId, modelId) {
      return request('/api/sessions/' + encodeURIComponent(sessionId) + '/model', {
        method: 'PATCH', body: { providerId: providerId, modelId: modelId }
      });
    },
    listFiles: function (sessionId, path) {
      return request('/api/sessions/' + encodeURIComponent(sessionId) + '/files?path=' + encodeURIComponent(path || ''));
    },
    getFileContent: function (sessionId, path) {
      return request('/api/sessions/' + encodeURIComponent(sessionId) + '/fileContent?path=' + encodeURIComponent(path));
    }
  };
})();
