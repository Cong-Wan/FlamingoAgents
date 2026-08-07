/*
Author: wilbur
Version: 1.0
Date: 2026-08-07
Description: fetch POST + ReadableStream 自解析 SSE 帧（空行分帧、event:/data: 行、忽略 : 注释帧；契约 §1.3）
             EventSource 不支持 POST/自定义 Header，故不可用最简实现。
*/
(function () {
  'use strict';

  // 解析单个帧文本 → { event, data }；纯注释帧返回 null
  function parseFrame(frameText) {
    var event = '';
    var dataLines = [];
    var lines = frameText.split('\n');
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (line === '' || line.charAt(0) === ':') continue; // 注释/保活帧
      if (line.indexOf('event:') === 0) {
        event = line.slice(6).trim();
      } else if (line.indexOf('data:') === 0) {
        dataLines.push(line.slice(5).replace(/^ /, ''));
      }
    }
    if (!event) return null;
    var data = null;
    if (dataLines.length > 0) {
      try {
        data = JSON.parse(dataLines.join('\n'));
      } catch (ignore) {
        data = null;
      }
    }
    return { event: event, data: data };
  }

  /**
   * 发起 SSE POST 流。
   * @param path    接口路径（/api/chat/stream | /api/chat/confirm）
   * @param body    请求 JSON
   * @param onEvent (eventName, dataObj) => void
   * @returns { done: Promise<'closed'>, abort: fn }
   *   done 在连接关闭时 resolve；REST 预检失败（4xx/5xx）时 reject（带 status/message）。
   */
  function streamPost(path, body, onEvent) {
    var controller = new AbortController();
    var headers = {
      'Content-Type': 'application/json; charset=utf-8',
      'Accept': 'text/event-stream'
    };
    if (window.appStore.token) {
      headers['Authorization'] = 'Bearer ' + window.appStore.token;
    }

    var done = (async function () {
      var resp = await fetch(path, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(body),
        signal: controller.signal
      });
      if (!resp.ok) {
        var message = '请求失败（' + resp.status + '）';
        try {
          var errBody = await resp.json();
          if (errBody && errBody.error) message = errBody.error;
        } catch (ignore) { /* 非 JSON */ }
        if (resp.status === 401) {
          window.appStore.clearToken();
          if (window.api) window.api.fireUnauthorized();
        }
        var error = new Error(message);
        error.status = resp.status;
        throw error;
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder('utf-8');
      var buffer = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        var sepIndex;
        while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
          var frameText = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          var frame = parseFrame(frameText);
          if (frame) onEvent(frame.event, frame.data);
        }
      }
      // 连接关闭 = 流结束（契约 §1.3）
      return 'closed';
    })();

    // 调用方主动 abort（如离开页面）时静默收尾
    done = done.catch(function (error) {
      if (controller.signal.aborted) return 'aborted';
      throw error;
    });

    return {
      done: done,
      abort: function () { controller.abort(); }
    };
  }

  window.sse = { streamPost: streamPost };
})();
