/*
Author: wilbur
Version: 1.1
Date: 2026-08-07
Description: 聊天视图：历史渲染、流式增量、思维链折叠、工具卡片（含 dangling 归位/孤儿 End）、
             确认框、停止；完整落实契约 §5 前端状态机。v1.1：契约引用编号修正（pending 接口 §3.7→§3.8）。
*/
(function () {
  'use strict';

  var messageListEl = document.getElementById('messageList');
  var chatEmptyEl = document.getElementById('chatEmpty');
  var topbarTitleEl = document.getElementById('topbarTitle');
  var topbarModelEl = document.getElementById('topbarModel');
  var errorBarEl = document.getElementById('errorBar');
  var composerInput = document.getElementById('composerInput');
  var sendButton = document.getElementById('sendButton');

  var confirmModalEl = document.getElementById('confirmModal');
  var confirmToolNameEl = document.getElementById('confirmToolName');
  var confirmReasonEl = document.getElementById('confirmReason');
  var confirmPreviewRowEl = document.getElementById('confirmPreviewRow');
  var confirmPreviewEl = document.getElementById('confirmPreview');
  var confirmArgsEl = document.getElementById('confirmArgs');

  // 工具卡片注册表：toolCallId → 卡片对象。历史卡片与新流事件按 id 命中更新（dangling 归位，契约 §5-M5）
  var toolCards = {};

  var STATUS_MAP = {
    running: { label: '执行中', cls: 'status-running' },
    done: { label: '完成', cls: 'status-done' },
    error: { label: '失败', cls: 'status-error' },
    rejected: { label: '被拒绝', cls: 'status-rejected' },
    pending: { label: '待确认', cls: 'status-pending' },
    dangling: { label: '中断未完成', cls: 'status-dangling' }
  };

  if (window.marked) {
    window.marked.setOptions({ gfm: true, breaks: true });
  }

  /* ---------- 基础渲染工具 ---------- */

  // XSS 红线：不可信文本必须 marked → DOMPurify 后才允许 innerHTML
  function renderMarkdown(el, text) {
    var html = window.marked ? window.marked.parse(text || '') : '';
    el.innerHTML = window.DOMPurify ? window.DOMPurify.sanitize(html) : '';
  }

  function scrollToBottom() {
    messageListEl.scrollTop = messageListEl.scrollHeight;
  }

  function showError(message) {
    errorBarEl.innerHTML = '';
    var span = document.createElement('span');
    span.textContent = message;
    var closeBtn = document.createElement('button');
    closeBtn.className = 'error-bar-close';
    closeBtn.textContent = '✕';
    closeBtn.addEventListener('click', function () { errorBarEl.classList.add('hidden'); });
    errorBarEl.appendChild(span);
    errorBarEl.appendChild(closeBtn);
    errorBarEl.classList.remove('hidden');
  }

  /* ---------- 工具卡片 ---------- */

  function setCardStatus(card, status) {
    var meta = STATUS_MAP[status] || STATUS_MAP.dangling;
    card.status = status;
    card.statusEl.textContent = meta.label;
    card.statusEl.className = 'tool-status ' + meta.cls;
    card.el.classList.toggle('dangling', status === 'dangling');
  }

  function setCollapsibleText(preEl, text, containerEl) {
    preEl.textContent = text || '';
    var old = containerEl.querySelector('.tool-expand-btn');
    if (old) old.remove();
    if ((text || '').length > 300 || (text || '').split('\n').length > 8) {
      preEl.classList.add('collapsed');
      var btn = document.createElement('button');
      btn.className = 'tool-expand-btn';
      btn.textContent = '展开全部';
      btn.addEventListener('click', function (event) {
        event.stopPropagation();
        var collapsed = preEl.classList.toggle('collapsed');
        btn.textContent = collapsed ? '展开全部' : '收起';
      });
      containerEl.appendChild(btn);
    } else {
      preEl.classList.remove('collapsed');
    }
  }

  function buildToolCard(toolCall, status, preview) {
    var card = { status: status, toolName: toolCall.toolName, args: toolCall.arguments };

    var el = document.createElement('div');
    el.className = 'tool-card';
    card.el = el;

    var header = document.createElement('div');
    header.className = 'tool-card-header';

    var icon = document.createElement('span');
    icon.className = 'tool-icon';
    icon.textContent = '🔧';

    var name = document.createElement('span');
    name.className = 'tool-name';
    name.textContent = toolCall.toolName || 'tool';

    var previewEl = document.createElement('span');
    previewEl.className = 'tool-preview';
    previewEl.textContent = preview || '';
    card.previewEl = previewEl;

    var statusEl = document.createElement('span');
    card.statusEl = statusEl;

    var toggle = document.createElement('span');
    toggle.className = 'tool-toggle';
    toggle.textContent = '▸';

    header.appendChild(icon);
    header.appendChild(name);
    header.appendChild(previewEl);
    header.appendChild(statusEl);
    header.appendChild(toggle);
    el.appendChild(header);

    var detail = document.createElement('div');
    detail.className = 'tool-card-detail hidden';

    var argsSection = document.createElement('div');
    argsSection.className = 'tool-section';
    var argsTitle = document.createElement('h4');
    argsTitle.textContent = '入参';
    var argsPre = document.createElement('pre');
    argsPre.className = 'tool-pre';
    // 纯文本节点：JSON 不经过 innerHTML
    argsPre.textContent = safeJson(toolCall.arguments);
    argsSection.appendChild(argsTitle);
    argsSection.appendChild(argsPre);
    detail.appendChild(argsSection);

    var resultSection = document.createElement('div');
    resultSection.className = 'tool-section hidden';
    var resultTitle = document.createElement('h4');
    resultTitle.textContent = '结果';
    var resultPre = document.createElement('pre');
    resultPre.className = 'tool-pre collapsed';
    resultSection.appendChild(resultTitle);
    resultSection.appendChild(resultPre);
    detail.appendChild(resultSection);
    card.resultSection = resultSection;
    card.resultPre = resultPre;

    header.addEventListener('click', function () {
      var expanded = el.classList.toggle('expanded');
      detail.classList.toggle('hidden', !expanded);
    });

    el.appendChild(detail);
    setCardStatus(card, status);
    return card;
  }

  function safeJson(value) {
    try {
      return JSON.stringify(value === undefined ? {} : value, null, 2);
    } catch (ignore) {
      return String(value);
    }
  }

  // toolCallStart：命中同 id 卡片（dangling 灰卡/待确认卡）→ 更新；否则在 live 块内新建（契约 §5-M5）
  function upsertToolCardOnStart(toolCall, preview) {
    var existing = toolCall && toolCards[toolCall.id];
    if (existing) {
      setCardStatus(existing, 'running');
      if (preview) existing.previewEl.textContent = preview;
      return existing;
    }
    var card = buildToolCard(toolCall, 'running', preview);
    toolCards[toolCall.id] = card;
    liveBodyEl().appendChild(card.el);
    scrollToBottom();
    return card;
  }

  // toolCallEnd：含孤儿 End（拒绝路径无配对 Start，命中「待确认」卡片置「被拒绝」）
  function resolveToolCardOnEnd(toolResult) {
    var card = toolCards[toolResult.toolCallId];
    if (!card) {
      // 极端兜底：无任何先验卡片（如跨页恢复后服务端重放仅发 End）
      card = buildToolCard(
        { id: toolResult.toolCallId, toolName: toolResult.toolName, arguments: {} },
        'running', ''
      );
      toolCards[toolResult.toolCallId] = card;
      liveBodyEl().appendChild(card.el);
    }
    setCardStatus(card, statusFromResult(toolResult));
    card.resultSection.classList.remove('hidden');
    setCollapsibleText(card.resultPre, toolResult.content || '', card.resultSection);
    scrollToBottom();
  }

  // 历史渲染规则（契约 §2.2）：details.reason == 'userRejectedApproval' → 被拒绝；其余 isError → 失败
  function statusFromResult(toolResult) {
    var details = toolResult.details;
    if (details && details.reason === 'userRejectedApproval') return 'rejected';
    if (toolResult.isError) return 'error';
    return 'done';
  }

  /* ---------- 消息块 ---------- */

  function appendUserMessage(content) {
    var row = document.createElement('div');
    row.className = 'msg msg-user';
    var bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.textContent = content;
    row.appendChild(bubble);
    messageListEl.appendChild(row);
    scrollToBottom();
  }

  function buildAssistantShell() {
    var row = document.createElement('div');
    row.className = 'msg msg-assistant';
    var avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.textContent = '🦩';
    var body = document.createElement('div');
    body.className = 'msg-body';
    row.appendChild(avatar);
    row.appendChild(body);
    return { row: row, body: body };
  }

  function buildThinkingBlock() {
    var details = document.createElement('details');
    details.className = 'thinking-block';
    var summary = document.createElement('summary');
    summary.textContent = '已思考';
    var content = document.createElement('div');
    content.className = 'thinking-content';
    details.appendChild(summary);
    details.appendChild(content);
    return { el: details, contentEl: content };
  }

  // 新建一个流式 assistant 块（思维链容器 + 正文容器），作为增量渲染目标
  function createLiveAssistantBlock() {
    var shell = buildAssistantShell();
    var thinking = buildThinkingBlock();
    thinking.el.classList.add('hidden');
    var contentEl = document.createElement('div');
    contentEl.className = 'markdown-content';
    shell.body.appendChild(thinking.el);
    shell.body.appendChild(contentEl);
    messageListEl.appendChild(shell.row);
    scrollToBottom();
    return {
      bodyEl: shell.body,
      rowEl: shell.row,
      thinkingEl: thinking.el,
      thinkingContentEl: thinking.contentEl,
      contentEl: contentEl,
      interrupted: false
    };
  }

  // 当前流式渲染挂载点；无 live 块时（兜底）新建
  function liveBodyEl() {
    var stream = window.appStore.stream;
    if (stream && stream.live) return stream.live.bodyEl;
    var live = createLiveAssistantBlock();
    if (stream) stream.live = live;
    return live.bodyEl;
  }

  function markInterrupted() {
    var stream = window.appStore.stream;
    if (!stream || !stream.live || stream.live.interrupted) return;
    stream.live.interrupted = true;
    var badge = document.createElement('span');
    badge.className = 'msg-interrupted';
    badge.textContent = '已中断';
    stream.live.bodyEl.appendChild(badge);
  }

  /* ---------- 历史回放（契约 §2.2） ---------- */

  function renderHistory(messages, pending) {
    messageListEl.innerHTML = '';
    toolCards = {};

    var toolResults = {};
    messages.forEach(function (msg) {
      if (msg.kind === 'tool') toolResults[msg.toolCallId] = msg;
    });

    var lastAssistant = null;
    messages.forEach(function (msg) {
      if (msg.kind === 'user') {
        appendUserMessage(msg.content);
      } else if (msg.kind === 'assistant') {
        lastAssistant = appendAssistantHistory(msg, toolResults, pending);
      }
      // kind === 'tool' 已通过配对消费，不单独渲染
    });
    scrollToBottom();
    return lastAssistant;
  }

  function appendAssistantHistory(msg, toolResults, pending) {
    var shell = buildAssistantShell();
    var contentEl = document.createElement('div');
    contentEl.className = 'markdown-content';
    shell.body.appendChild(contentEl);
    renderMarkdown(contentEl, msg.content || '');

    (msg.toolCalls || []).forEach(function (toolCall) {
      var result = toolResults[toolCall.id];
      var card;
      if (result) {
        card = buildToolCard(toolCall, statusFromResult(result), '');
        card.resultSection.classList.remove('hidden');
        setCollapsibleText(card.resultPre, result.content || '', card.resultSection);
      } else if (pending && pending.toolCall && pending.toolCall.id === toolCall.id) {
        // pending 中的 toolCall 不按 dangling 渲染，而是「待确认」卡片（契约 §3.8/§5）
        card = buildToolCard(toolCall, 'pending', pending.commandPreview || '');
      } else {
        // 末尾未配对 toolCalls = dangling（中断未完成），渲染置灰卡片
        card = buildToolCard(toolCall, 'dangling', '');
      }
      toolCards[toolCall.id] = card;
      shell.body.appendChild(card.el);
    });

    messageListEl.appendChild(shell.row);
    return { bodyEl: shell.body, contentEl: contentEl, content: msg.content || '' };
  }

  /* ---------- 确认框 ---------- */

  function showConfirmModal(pending) {
    confirmToolNameEl.textContent = (pending.toolCall && pending.toolCall.toolName) || '';
    confirmReasonEl.textContent = pending.reason || '';
    if (pending.commandPreview) {
      confirmPreviewRowEl.classList.remove('hidden');
      confirmPreviewEl.textContent = pending.commandPreview;
    } else {
      confirmPreviewRowEl.classList.add('hidden');
    }
    confirmArgsEl.textContent = safeJson(pending.toolCall ? pending.toolCall.arguments : {});
    confirmModalEl.classList.remove('hidden');
  }

  function hideConfirmModal() {
    confirmModalEl.classList.add('hidden');
  }

  /* ---------- composer 状态（契约 §5 各态） ---------- */

  function updateComposer() {
    var stream = window.appStore.stream;
    var hasSession = !!window.appStore.currentSessionId;
    if (!hasSession) {
      composerInput.disabled = true;
      sendButton.disabled = true;
      sendButton.textContent = '发送';
      sendButton.classList.remove('stop');
      return;
    }
    if (!stream) { // 空闲
      composerInput.disabled = false;
      sendButton.disabled = false;
      sendButton.textContent = '发送';
      sendButton.classList.remove('stop');
    } else if (stream.phase === 'streaming') {
      composerInput.disabled = true;
      sendButton.disabled = false;
      sendButton.textContent = '停止';
      sendButton.classList.add('stop');
    } else if (stream.phase === 'stopping') {
      composerInput.disabled = true;
      sendButton.disabled = true;
      sendButton.textContent = '停止中';
      sendButton.classList.add('stop');
    } else { // waitingConfirm：输入框禁用
      composerInput.disabled = true;
      sendButton.disabled = true;
      sendButton.textContent = '发送';
      sendButton.classList.remove('stop');
    }
  }

  /* ---------- 流式事件处理（契约 §4.3 事件集） ---------- */

  function onStreamEvent(event, data) {
    var stream = window.appStore.stream;
    if (!stream) return;
    data = data || {};

    if (stream.phase === 'stopping') {
      // 已点停止：立即停渲染，仅记录终态到达
      if (event === 'completed' || event === 'error' || event === 'confirmationRequired') {
        stream.terminalSeen = true;
      }
      return;
    }
    if (stream.phase !== 'streaming') return;

    switch (event) {
      case 'textDelta':
        stream.textBuf += data.text || '';
        if (stream.live) renderMarkdown(stream.live.contentEl, stream.textBuf);
        scrollToBottom();
        break;

      case 'reasoningDelta':
        stream.reasoningBuf += data.text || '';
        if (stream.live) {
          stream.live.thinkingEl.classList.remove('hidden');
          stream.live.thinkingContentEl.textContent = stream.reasoningBuf;
        }
        scrollToBottom();
        break;

      case 'toolCallStart':
        if (data.toolCall) upsertToolCardOnStart(data.toolCall, data.preview || '');
        break;

      case 'toolCallEnd':
        if (data.toolResult) resolveToolCardOnEnd(data.toolResult);
        break;

      case 'confirmationRequired': // 终态：弹框 + 用帧内 toolCall 建「待确认」卡片
        stream.terminalSeen = true;
        if (data.toolCall) {
          var card = toolCards[data.toolCall.id];
          if (card) {
            setCardStatus(card, 'pending');
          } else {
            card = buildToolCard(data.toolCall, 'pending', data.commandPreview || '');
            toolCards[data.toolCall.id] = card;
            liveBodyEl().appendChild(card.el);
          }
          if (data.commandPreview) card.previewEl.textContent = data.commandPreview;
        }
        stream.phase = 'waitingConfirm';
        stream.pending = data;
        showConfirmModal(data);
        updateComposer();
        scrollToBottom();
        break;

      case 'completed': // 终态：刷新侧栏（title/usage/updatedAt 已变）
        stream.terminalSeen = true;
        goIdle();
        window.sidebarView.refresh().then(function () { window.chatView.syncTopbar(); });
        break;

      case 'error': // 终态
        stream.terminalSeen = true;
        handleStreamError(data);
        break;
    }
  }

  function handleStreamError(data) {
    var sessionId = window.appStore.currentSessionId;
    if (data.errorType === 'pendingConfirmationExists') {
      // 契约 §5：GET pending → 重弹确认框
      window.api.getPending(sessionId).then(function (res) {
        if (res && res.pending) {
          enterWaitingConfirm(res.pending);
        } else {
          goIdle();
        }
      }).catch(function (error) {
        showError(error.message);
        goIdle();
      });
      return;
    }
    if (data.errorType === 'confirmationMismatch') {
      // 契约 §4.2/§5：重新拉 messages 刷新（挂起确认可能已失效，按 dangling 呈现）
      hideConfirmModal();
      goIdle();
      reloadSession(sessionId).catch(function (error) { showError(error.message); });
      showError('确认已失效（' + (data.message || 'confirmationMismatch') + '），已刷新会话。');
      return;
    }
    // 其它 errorType：空闲 + 错误提示条
    showError(data.message || '模型调用失败');
    goIdle();
  }

  // 进入 [待确认] 态：pending 卡片 + 弹框（供 pending 恢复 / pendingConfirmationExists 共用）
  function enterWaitingConfirm(pending, restoredLive) {
    var stream = window.appStore.stream;
    if (!stream) {
      stream = {
        phase: 'waitingConfirm',
        abort: null,
        textBuf: restoredLive ? restoredLive.content : '',
        reasoningBuf: '',
        live: restoredLive ? buildLiveFromHistory(restoredLive) : null,
        terminalSeen: true,
        pending: pending
      };
      window.appStore.stream = stream;
    } else {
      stream.phase = 'waitingConfirm';
      stream.pending = pending;
    }
    if (pending.toolCall) {
      var card = toolCards[pending.toolCall.id];
      if (card) {
        setCardStatus(card, 'pending');
        if (pending.commandPreview) card.previewEl.textContent = pending.commandPreview;
      }
    }
    showConfirmModal(pending);
    updateComposer();
  }

  // pending 恢复时，续流需渲染进同一 assistant 块：把历史末块包装为 live 结构
  function buildLiveFromHistory(lastAssistant) {
    var thinking = buildThinkingBlock();
    thinking.el.classList.add('hidden');
    lastAssistant.bodyEl.insertBefore(thinking.el, lastAssistant.bodyEl.firstChild);
    return {
      bodyEl: lastAssistant.bodyEl,
      rowEl: lastAssistant.bodyEl.parentNode,
      thinkingEl: thinking.el,
      thinkingContentEl: thinking.contentEl,
      contentEl: lastAssistant.contentEl,
      interrupted: false
    };
  }

  function onStreamClosed() {
    var stream = window.appStore.stream;
    if (!stream) return; // 已复位（离开页面/completed 已处理）
    if (stream.phase === 'waitingConfirm') return; // 等用户确认，保持该态
    if (stream.phase === 'stopping') {
      goIdle();
      return;
    }
    if (!stream.terminalSeen) {
      // 未收到任何终态事件连接断开 → 按「中断」处理（契约 §1.3）
      markInterrupted();
      showError('连接中断：未收到终态事件，刷新页面可恢复最新状态。');
    }
    goIdle();
  }

  function onStreamFailed(error) {
    // REST 预检失败（400/404/409，未开流）：回空闲态；待确认场景可重进会话经 GET pending 自愈
    showError(error.message);
    goIdle();
  }

  function goIdle() {
    window.appStore.stream = null;
    updateComposer();
  }

  /* ---------- 发消息 / 确认 / 停止 ---------- */

  async function send() {
    var sessionId = window.appStore.currentSessionId;
    var stream = window.appStore.stream;
    if (!sessionId || stream) return;
    var text = composerInput.value.trim();
    if (!text) return;

    composerInput.value = '';
    autoResize();
    appendUserMessage(text);

    window.appStore.stream = {
      phase: 'streaming',
      abort: null,
      textBuf: '',
      reasoningBuf: '',
      live: createLiveAssistantBlock(),
      terminalSeen: false,
      pending: null
    };
    updateComposer();

    var handle = window.sse.streamPost(
      '/api/chat/stream',
      { sessionId: sessionId, message: text },
      onStreamEvent
    );
    window.appStore.stream.abort = handle.abort;
    handle.done.then(onStreamClosed).catch(onStreamFailed);
  }

  function confirm(approved) {
    var sessionId = window.appStore.currentSessionId;
    var stream = window.appStore.stream;
    if (!sessionId || !stream || stream.phase !== 'waitingConfirm' || !stream.pending) return;
    var pending = stream.pending;
    hideConfirmModal();

    // [待确认] --批准/拒绝 POST confirm--> [流式中]（续流渲染进同一 assistant 块）
    stream.phase = 'streaming';
    stream.terminalSeen = false;
    if (!stream.live) stream.live = createLiveAssistantBlock();
    updateComposer();

    var handle = window.sse.streamPost(
      '/api/chat/confirm',
      { sessionId: sessionId, confirmationId: pending.confirmationId, approved: approved },
      onStreamEvent
    );
    stream.abort = handle.abort;
    handle.done.then(onStreamClosed).catch(onStreamFailed);
  }

  async function stop() {
    var sessionId = window.appStore.currentSessionId;
    var stream = window.appStore.stream;
    if (!sessionId || !stream || stream.phase !== 'streaming') return;
    stream.phase = 'stopping';
    markInterrupted(); // 立即停渲染 + 半截消息加「已中断」标记
    updateComposer();
    try {
      await window.api.stopChat(sessionId);
    } catch (ignore) { /* stopped:false 幂等不报错；网络错误等连接关闭后自复位 */ }
    // 等连接关闭后由 onStreamClosed 回空闲态
  }

  /* ---------- 会话装载 ---------- */

  async function reloadSession(sessionId) {
    // 进入/刷新会话页：GET messages + GET pending 并行（契约 §5）
    var results = await Promise.all([
      window.api.getMessages(sessionId),
      window.api.getPending(sessionId)
    ]);
    var messages = results[0].messages || [];
    var pending = results[1] ? results[1].pending : null;
    var lastAssistant = renderHistory(messages, pending);
    if (pending) {
      enterWaitingConfirm(pending, lastAssistant);
    }
  }

  function autoResize() {
    composerInput.style.height = 'auto';
    composerInput.style.height = Math.min(composerInput.scrollHeight, 200) + 'px';
  }

  /* ---------- 对外接口 ---------- */

  window.chatView = {
    open: async function (sessionId) {
      window.appStore.currentSessionId = sessionId;
      chatEmptyEl.classList.add('hidden');
      messageListEl.classList.remove('hidden');
      errorBarEl.classList.add('hidden');
      window.chatView.syncTopbar();
      updateComposer();
      try {
        await reloadSession(sessionId);
        if (!window.appStore.stream) updateComposer();
      } catch (error) {
        if (error.status === 404) {
          showError('会话不存在。');
        } else {
          showError(error.message);
        }
      }
      composerInput.focus();
    },

    showEmpty: function () {
      window.appStore.currentSessionId = null;
      topbarTitleEl.textContent = 'FlamingoAgents';
      topbarModelEl.textContent = '';
      messageListEl.innerHTML = '';
      messageListEl.classList.add('hidden');
      chatEmptyEl.classList.remove('hidden');
      errorBarEl.classList.add('hidden');
      updateComposer();
    },

    // 路由切走时：主动 abort 前端流（后端泵线程继续跑到终态，重进自愈，契约 §5）
    close: function () {
      var stream = window.appStore.stream;
      if (stream && stream.abort) stream.abort();
      window.appStore.stream = null;
      hideConfirmModal();
    },

    syncTopbar: function () {
      var session = window.appStore.findSession(window.appStore.currentSessionId);
      if (!session) return;
      topbarTitleEl.textContent = session.title || '新会话';
      topbarModelEl.textContent = session.modelId
        ? session.providerId + ' / ' + session.modelId
        : (session.providerId || '');
    }
  };

  /* ---------- DOM 事件绑定 ---------- */

  sendButton.addEventListener('click', function () {
    var stream = window.appStore.stream;
    if (stream && stream.phase === 'streaming') {
      stop();
    } else {
      send();
    }
  });

  composerInput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  composerInput.addEventListener('input', autoResize);

  document.getElementById('confirmApprove').addEventListener('click', function () { confirm(true); });
  document.getElementById('confirmReject').addEventListener('click', function () { confirm(false); });
})();
