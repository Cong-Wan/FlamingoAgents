/*
Author: wilbur
Version: 1.16
Date: 2026-08-17
Description: 聊天视图：历史渲染、流式增量、思维链折叠、工具卡片（含 dangling 归位/孤儿 End）、
             确认框、停止；完整落实契约 §5 前端状态机。v1.1：契约引用编号修正（pending 接口 §3.7→§3.8）。
             v1.2 迭代二（方案 §4.5/§4.6）：头像换 flamingo2.png；send 支持 attachments（纯附件可发，气泡显示 chip 行）；
             历史 user 消息的 attachment 块渲染为折叠 chip；流终态刷新状态栏；open/showEmpty 挂载 statusBar/fileExplorer/fileMention；
             新增 discardPendingConfirm（/model 切换放弃待确认）。
             v1.3（fixPlan Phase1）：智能贴底--stickToBottom 状态 + scroll 监听；流式增量路径改 maybeScrollToBottom（不再抢视口）；
             离底时显示「↓ 回到底部」按钮；发送/重载/切会话/终态重置贴底。
             v1.4（fixPlan Phase3）：流式 thinking 交互--首包展开「思考中…」，转 text/tool/封口自动折叠「已思考」（尊重 userToggledThinking）；
             appendAssistantHistory 渲染 msg.reasoning（content 之前，默认折叠）；无 reasoning 不渲染空壳。
             v1.5（fixPlan Phase4+Phase5）：live 按模型 step 拆块（D1/D6 隐式边界推断）；step 持有独立 live/textBuf/reasoningBuf/sawToolEnd；
             toolCallStart/End 仅本块新建卡片置 sawToolEnd（ownedByLiveBlock 标记，dangling 重放/pending 恢复命中注册表不置位、不 newStep，审核 S1）；
             pending 恢复复用历史 thinking 壳避免双壳（审核 M1），restored step 初始 sawToolEnd=true 使 confirm 后续模型输出落新块；
             工具 running 态加呼吸/pulse 动画（Phase5 T5.1）。
             v1.6 多窗口并行（multiWindowStreamingPlan §5）：reloadSession 乐观 attach（历史先渲染，attach 成功按 baseCount 截断重渲染+回放续播）；
             attaching 占位流态（close 可 abort、send 拦截、composer 禁用）+ sessionId/占位身份双重守卫；
             onStreamEvent 四处 currentStep 空守卫；handleStreamError 新增 stopped 静默分支（跨窗口停止）。
             v1.7（streamingLatencyFixPlan Phase2/D4）：live paint rAF 合并——textDelta/reasoningDelta 只进 buffer + scheduleLivePaint，
             DOM 写在 rAF/flushLivePaint（每帧最多一次 renderMarkdown，长正文不再每 token 全量 parse 卡主线程）；
             强制 flush 清单：step 切换前、completed/error/confirmationRequired 入口、stop 进 stopping、goIdle 双保险、collapseThinking 前双 buffer 上屏；
             工具卡/确认事件保持即时 DOM，不进文本 paint 队列。
             v1.8 聊天页 topbar 穿梭灯条：syncStreamIndicator 在 streaming/attaching 亮、其余灭；挂 updateComposer 末尾统一驱动。
             v1.9 重试提示块（retryNotice 实时更新/终态清除）+ 模型错误内联块（emptyMessage/无 step 回退顶部 errorBar）。
             v1.10（composerFocusShortcutPlan T1）：回答结束后光标自动回落输入框——新增 focusComposerIfReady（三守卫：
             聊天页可见/输入框可用/无 .modal-mask 弹层），挂载点：onStreamClosed（stopping 早退分支+末尾）、
             reloadSession 无 pending 分支（confirmationMismatch 重载路径）、onStreamFailed（REST 预检失败路径）。
             v1.11（grok 验收返工）：修复 F1 主路径不生效——completed/error/stopped 先 goIdle 置空 stream，
             onStreamClosed 的 !stream 早退改为补 focus 后再 return；C1 挂载点从 reloadSession 移除
             （attachStream 同步禁用 composer，focus 必被守卫拦截，属死代码），改挂 resetToHistoryState
             （attach 落空/失败、composer 恢复可用后）；顺带修复 open() 裸 focus 打在 attaching 禁用输入框上的既有竞态。
             v1.12（grok 复核建议项）：focusComposerIfReady 守卫 +1——#app 隐藏（登录门态）不 focus，
             堵「completed → goIdle 启用输入框 → 401 跳登录门 → onStreamClosed 补 focus」窄窗口抢登录框焦点。
             v1.13（stopResponsivenessPlan L1）：stop() 改 fire-and-forget + 立即 abort（保持 stopping 至 onStreamClosed）；
             send() 的 onStreamFailed 对 409「活跃流」静默重试一次。
             v1.14 /skill: chip 发送：同步定界后取正文拼 wireText，气泡只显示 /skill:名+补充；open/showEmpty 清 skillChip。
             v1.15（fileMentionFixPlan）：气泡 chip 按 attachment.type 出图标（📄 文件 / 📁 目录）。
             v1.16（toolCardStopUiFixPlan）：stopped 时新增 settleRunningCardsOnStop——泵在 stopFlag 后置位吞掉 toolCallEnd，
             把仍 running 的卡片定格为失败态（文案锚定后端 closeUnfinishedToolCalls userStopped），不再永远「执行中」。
             挂载两处：handleStreamError stopped 分支（其他窗口收后端广播）+ stop()（本窗口点停止，abort 后收不到 stopped 广播）。
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
  var streamIndicatorEl = document.getElementById('streamIndicator');

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

  /* ---------- 智能贴底（fixPlan Phase1） ---------- */
  var NEAR_BOTTOM_PX = 80;
  var stickToBottom = true;
  var jumpToBottomBtn = null;
  var jumpToBottomVisible = false;

  messageListEl.addEventListener('scroll', function () {
    stickToBottom = (messageListEl.scrollHeight - messageListEl.scrollTop - messageListEl.clientHeight) <= NEAR_BOTTOM_PX;
    if (stickToBottom) hideJumpToBottom();
  });

  function maybeScrollToBottom() {
    if (stickToBottom) scrollToBottom();
    else showJumpToBottom();
  }

  function ensureJumpBtn() {
    if (jumpToBottomBtn) return;
    var btn = document.createElement('button');
    btn.className = 'jump-to-bottom hidden';
    btn.type = 'button';
    btn.textContent = '↓ 回到底部';
    btn.addEventListener('click', function () {
      stickToBottom = true;
      scrollToBottom();
      hideJumpToBottom();
    });
    document.querySelector('.chat-center').appendChild(btn);
    jumpToBottomBtn = btn;
  }

  function showJumpToBottom() {
    ensureJumpBtn();
    if (jumpToBottomVisible) return;
    var composer = document.querySelector('.composer');
    var offset = (composer ? composer.offsetHeight : 0) + 12;
    jumpToBottomBtn.style.bottom = offset + 'px';
    jumpToBottomBtn.classList.remove('hidden');
    jumpToBottomVisible = true;
  }

  function hideJumpToBottom() {
    if (!jumpToBottomBtn) return;
    jumpToBottomBtn.classList.add('hidden');
    jumpToBottomVisible = false;
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
    card.el.classList.toggle('tool-card-running', status === 'running'); // Phase5：running 态呼吸动画
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
    var card = { status: status, toolName: toolCall.toolName, args: toolCall.arguments, ownedByLiveBlock: false };

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
      return existing; // 注册表命中（dangling 重放/pending 恢复）：不参与本块 step 归属
    }
    var card = buildToolCard(toolCall, 'running', preview);
    card.ownedByLiveBlock = true; // 本块新建：参与 step 归属判定（fixPlan Phase4 T4.5/审核 S1）
    toolCards[toolCall.id] = card;
    liveBodyEl().appendChild(card.el);
    maybeScrollToBottom();
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
      card.ownedByLiveBlock = true; // 孤儿 End 在本块新建，参与 step 归属
      toolCards[toolResult.toolCallId] = card;
      liveBodyEl().appendChild(card.el);
    }
    setCardStatus(card, statusFromResult(toolResult));
    card.resultSection.classList.remove('hidden');
    setCollapsibleText(card.resultPre, toolResult.content || '', card.resultSection);
    maybeScrollToBottom();
    return card;
  }

  // 历史渲染规则（契约 §2.2）：details.reason == 'userRejectedApproval' → 被拒绝；其余 isError → 失败
  function statusFromResult(toolResult) {
    var details = toolResult.details;
    if (details && details.reason === 'userRejectedApproval') return 'rejected';
    if (toolResult.isError) return 'error';
    return 'done';
  }

  // 停止收尾（toolCardStopUiFixPlan）：stopped 时泵已吞掉 toolCallEnd，把仍 running 的卡片定格为失败态
  function settleRunningCardsOnStop() {
    Object.keys(toolCards).forEach(function (id) {
      var card = toolCards[id];
      if (card.status !== 'running') return;
      setCardStatus(card, 'error');
      card.resultSection.classList.remove('hidden');
      // 文案锚点：与 flamingoAgents/core/agent.py closeUnfinishedToolCalls contents['userStopped'] 保持一致，改文案需同步
      setCollapsibleText(card.resultPre, '该工具调用因用户停止未完成；停止前可能已产生文件或命令副作用。', card.resultSection);
    });
    maybeScrollToBottom();
  }

  /* ---------- 消息块 ---------- */

  // 附件块标记（与后端 fileBrowser.buildAttachmentMessage 约定一致；path 字符集校验防手输伪造，评审 M5）
  var ATTACHMENT_RE = /<attachment path="([A-Za-z0-9_\-\.\/]+)">\n([\s\S]*?)\n<\/attachment>/g;

  function appendTextSegment(bubble, text) {
    if (!text) return;
    var span = document.createElement('span');
    span.textContent = text;
    bubble.appendChild(span);
  }

  function buildAttachmentBlock(path, content) {
    // 历史回放：附件块折叠为 chip（details），内容纯 textContent
    var details = document.createElement('details');
    details.className = 'attachment-block';
    var summary = document.createElement('summary');
    summary.textContent = '📄 ' + path;
    summary.title = path;
    var pre = document.createElement('pre');
    pre.className = 'attachment-block-content';
    pre.textContent = content;
    details.appendChild(summary);
    details.appendChild(pre);
    return details;
  }

  // sentAttachments：本次发送的 chip 列表（仅显示路径）；为空则按历史消息解析 attachment 块
  function appendUserMessage(content, sentAttachments) {
    var row = document.createElement('div');
    row.className = 'msg msg-user';
    var bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    if (sentAttachments && sentAttachments.length > 0) {
      appendTextSegment(bubble, content);
      var chipRow = document.createElement('div');
      chipRow.className = 'attachment-chip-row';
      sentAttachments.forEach(function (attachment) {
        var chip = document.createElement('span');
        chip.className = 'attachment-chip static';
        chip.textContent = (attachment.type === 'dir' ? '📁 ' : '📄 ') + attachment.path;
        chip.title = attachment.path;
        chipRow.appendChild(chip);
      });
      bubble.appendChild(chipRow);
    } else {
      ATTACHMENT_RE.lastIndex = 0;
      var last = 0;
      var match;
      while ((match = ATTACHMENT_RE.exec(content)) !== null) {
        appendTextSegment(bubble, content.slice(last, match.index).trim());
        bubble.appendChild(buildAttachmentBlock(match[1], match[2]));
        last = match.index + match[0].length;
      }
      appendTextSegment(bubble, last > 0 ? content.slice(last).trim() : content);
    }
    row.appendChild(bubble);
    messageListEl.appendChild(row);
    scrollToBottom();
  }

  function buildAssistantShell() {
    var row = document.createElement('div');
    row.className = 'msg msg-assistant';
    var avatar = document.createElement('img');
    avatar.className = 'msg-avatar';
    avatar.src = '/static/flamingo2.png';
    avatar.alt = '🦩';
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
    return { el: details, summaryEl: summary, contentEl: content };
  }

  // 设置 thinking 块的 summary 文案与 open 状态；open 仅在用户未手动 toggle 时生效（fixPlan Phase3 D4）
  function setThinkingState(live, summaryText, open) {
    if (!live || !live.thinkingSummaryEl) return;
    live.thinkingSummaryEl.textContent = summaryText;
    if (live.userToggledThinking) return; // 用户手动 toggle 过，不再自动改 open
    live.thinkingOpen = open;
    if (open) live.thinkingEl.setAttribute('open', '');
    else live.thinkingEl.removeAttribute('open');
  }

  // 本 step 转 text/tool/封口时折叠 thinking（D4）；已折叠或无 reasoning 则跳过
  function collapseThinkingIfOpen(live) {
    if (!live || !live.reasoningSeen || !live.thinkingOpen) return;
    setThinkingState(live, '已思考', false);
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
    maybeScrollToBottom();
    var live = {
      bodyEl: shell.body,
      rowEl: shell.row,
      thinkingEl: thinking.el,
      thinkingSummaryEl: thinking.summaryEl,
      thinkingContentEl: thinking.contentEl,
      thinkingOpen: false,
      userToggledThinking: false,
      reasoningSeen: false,
      contentEl: contentEl,
      interrupted: false
    };
    thinking.summaryEl.addEventListener('click', function () { live.userToggledThinking = true; });
    return live;
  }

  // 创建一个模型 step：内含 live 块 + 本 step 自有 buffer 与工具阶段标记（fixPlan Phase4 D1/D6）
  // paintScheduled：rAF paint 合并标记（streamingLatencyFixPlan Phase2 D4）
  function createStep() {
    return {
      live: createLiveAssistantBlock(),
      textBuf: '',
      reasoningBuf: '',
      sawToolEnd: false,
      paintScheduled: false
    };
  }

  // rAF paint 合并（D4）：把本 step 的 reasoningBuf/textBuf 一次性写入 DOM 并清除调度标记。
  // step 切换/终态/stop/goIdle/collapse 前必须调用（漏则丢尾）。
  function flushLivePaint(step) {
    if (!step || !step.live) return;
    step.paintScheduled = false;
    if (step.reasoningBuf && step.live.thinkingContentEl) {
      step.live.thinkingContentEl.textContent = step.reasoningBuf;
    }
    if (step.textBuf) {
      renderMarkdown(step.live.contentEl, step.textBuf);
    }
  }

  // delta 只进 buffer，每帧最多一次 DOM 写；scroll 合入 paint 回调末尾
  function scheduleLivePaint(step) {
    if (step.paintScheduled) return;
    step.paintScheduled = true;
    requestAnimationFrame(function () {
      if (!step.paintScheduled) return; // 已被 flushLivePaint 清掉
      flushLivePaint(step);
      maybeScrollToBottom();
    });
  }

  // 折叠 thinking 前强制双 buffer 上屏（D4 清单 5：避免「已思考」少字/正文丢尾）；仅在实际将折叠时 flush，保住 rAF 合并收益
  function flushAndCollapseThinking(step) {
    if (!step || !step.live) return;
    flushLivePaint(step);
    collapseThinkingIfOpen(step.live);
  }

  // D6 隐式 step 边界：当前 step 已走过工具阶段（sawToolEnd=true）后又有模型输出（textDelta/reasoningDelta）
  // -> 封口当前块、新建下一块。dangling 重放/pending 恢复命中注册表不置 sawToolEnd，故不触发 newStep。
  function beginNewStepIfNeeded(eventKind) {
    var stream = window.appStore.stream;
    if (!stream) return;
    var step = stream.currentStep;
    if (!step) {
      stream.currentStep = createStep();
      stream.steps = [stream.currentStep];
      return;
    }
    if (step.sawToolEnd && (eventKind === 'textDelta' || eventKind === 'reasoningDelta')) {
      flushLivePaint(step); // 旧 step 封眼前强制上屏（D4 清单 1）
      stream.currentStep = createStep();
      stream.steps.push(stream.currentStep);
    }
  }

  // 当前流式渲染挂载点；无 currentStep 时（兜底）新建
  function liveBodyEl() {
    var stream = window.appStore.stream;
    if (stream && stream.currentStep && stream.currentStep.live) return stream.currentStep.live.bodyEl;
    var step = createStep();
    if (stream) {
      stream.currentStep = step;
      stream.steps = [step];
    }
    return step.live.bodyEl;
  }

  function markInterrupted() {
    var stream = window.appStore.stream;
    if (!stream || !stream.currentStep || !stream.currentStep.live || stream.currentStep.live.interrupted) return;
    var live = stream.currentStep.live;
    live.interrupted = true;
    var badge = document.createElement('span');
    badge.className = 'msg-interrupted';
    badge.textContent = '已中断';
    live.bodyEl.appendChild(badge);
  }

  // 重试提示块：在当前 step.live.bodyEl 内 upsert .msg-retry-notice（modelUxImprovePlan D6/T9）
  function upsertRetryNotice(step, data) {
    if (!step || !step.live || !step.live.bodyEl) return;
    data = data || {};
    var text = '模型请求失败，第 ' + (data.attempt || 1) + ' 次重试';
    if (data.retryAfterMs) text += '，' + Math.ceil(data.retryAfterMs / 1000) + '秒后';
    text += '…';
    var el = step.live.retryNoticeEl;
    if (!el || !el.parentNode) {
      el = document.createElement('div');
      el.className = 'msg-retry-notice';
      step.live.bodyEl.appendChild(el);
      step.live.retryNoticeEl = el;
    }
    el.textContent = text;
    maybeScrollToBottom();
  }

  // 清除本 step 的重试提示块（后续 textDelta/reasoningDelta/completed/error 时调用）
  function clearRetryNotice(step) {
    if (!step || !step.live) return;
    var el = step.live.retryNoticeEl;
    if (el && el.parentNode) el.parentNode.removeChild(el);
    step.live.retryNoticeEl = null;
  }

  // 模型错误内联块：挂在 bodyEl 下方，含文案 + ✕ 关闭（modelUxImprovePlan D5/T10）
  function appendInlineErrorBlock(bodyEl, message) {
    if (!bodyEl) return;
    var block = document.createElement('div');
    block.className = 'msg-error-block';
    var span = document.createElement('span');
    span.textContent = message || '模型调用失败';
    var closeBtn = document.createElement('button');
    closeBtn.className = 'msg-error-close';
    closeBtn.textContent = '✕';
    closeBtn.addEventListener('click', function () {
      if (block.parentNode) block.parentNode.removeChild(block);
    });
    block.appendChild(span);
    block.appendChild(closeBtn);
    bodyEl.appendChild(block);
    maybeScrollToBottom();
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
    var thinking = null;
    if (msg.reasoning) {
      thinking = buildThinkingBlock();
      thinking.contentEl.textContent = msg.reasoning;
      shell.body.appendChild(thinking.el);
    }
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
    // 返回 thinking 壳引用（供 buildLiveFromHistory 复用，避免双壳--审核 M1）；无 reasoning 时为 null
    return {
      bodyEl: shell.body,
      contentEl: contentEl,
      content: msg.content || '',
      thinkingEl: thinking ? thinking.el : null,
      thinkingSummaryEl: thinking ? thinking.summaryEl : null,
      thinkingContentEl: thinking ? thinking.contentEl : null
    };
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

  function syncStreamIndicator() {
    var stream = window.appStore.stream;
    var active = !!(stream && (stream.phase === 'streaming' || stream.phase === 'attaching'));
    streamIndicatorEl.classList.toggle('hidden', !active);
  }

  function updateComposer() {
    var stream = window.appStore.stream;
    var hasSession = !!window.appStore.currentSessionId;
    if (!hasSession) {
      composerInput.disabled = true;
      sendButton.disabled = true;
      sendButton.textContent = '发送';
      sendButton.classList.remove('stop');
    } else if (!stream) { // 空闲
      composerInput.disabled = false;
      sendButton.disabled = false;
      sendButton.textContent = '发送';
      sendButton.classList.remove('stop');
    } else if (stream.phase === 'streaming') {
      composerInput.disabled = true;
      sendButton.disabled = false;
      sendButton.textContent = '停止';
      sendButton.classList.add('stop');
    } else if (stream.phase === 'attaching') {
      // attach 在途（multiWindowStreamingPlan §5.1）：禁用输入与按钮，防止初始化前发送撞 409
      composerInput.disabled = true;
      sendButton.disabled = true;
      sendButton.textContent = '发送';
      sendButton.classList.remove('stop');
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
    syncStreamIndicator();
  }

  // 回答结束后光标回落输入框（composerFocusShortcutPlan §3.2）：仅聊天页可见、输入框可用、无弹层时聚焦
  function focusComposerIfReady() {
    var chatPageEl = document.getElementById('chatPage');
    if (chatPageEl.classList.contains('hidden')) return;
    if (document.getElementById('app').classList.contains('hidden')) return; // v1.12：登录门态不抢焦点（窄窗口：completed 后 401 跳登录门）
    if (composerInput.disabled) return;
    if (document.querySelector('.modal-mask:not(.hidden)')) return; // 任一弹层可见不抢焦点
    composerInput.focus();
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
        beginNewStepIfNeeded('textDelta');
        clearRetryNotice(stream.currentStep);
        stream.currentStep.textBuf += data.text || '';
        // 即将折叠 thinking 时先 flush 双 buffer（collapse 只发生一次，不破坏 rAF 合并）
        if (stream.currentStep.live.reasoningSeen && stream.currentStep.live.thinkingOpen) {
          flushAndCollapseThinking(stream.currentStep);
        }
        scheduleLivePaint(stream.currentStep);
        break;

      case 'reasoningDelta':
        beginNewStepIfNeeded('reasoningDelta');
        clearRetryNotice(stream.currentStep);
        stream.currentStep.reasoningBuf += data.text || '';
        stream.currentStep.live.thinkingEl.classList.remove('hidden');
        stream.currentStep.live.reasoningSeen = true;
        setThinkingState(stream.currentStep.live, '思考中…', true);
        scheduleLivePaint(stream.currentStep);
        break;

      case 'retryNotice': // 非终态：消息下方「重试中」提示块（modelUxImprovePlan D6/T9）
        if (stream.currentStep && stream.currentStep.live) {
          upsertRetryNotice(stream.currentStep, data);
        }
        break;

      case 'toolCallStart':
        if (data.toolCall) {
          if (stream.currentStep) flushAndCollapseThinking(stream.currentStep); // attach 首事件可能无 step（§5.2）
          upsertToolCardOnStart(data.toolCall, data.preview || '');
        }
        break;

      case 'toolCallEnd':
        if (data.toolResult) {
          // 仅本块新建卡片（非注册表归位）参与 step 归属判定（fixPlan Phase4 T4.5/审核 S1）
          var resolved = resolveToolCardOnEnd(data.toolResult);
          if (resolved.ownedByLiveBlock) stream.currentStep.sawToolEnd = true;
        }
        break;

      case 'confirmationRequired': // 终态：弹框 + 用帧内 toolCall 建「待确认」卡片
        stream.terminalSeen = true;
        if (stream.currentStep) flushAndCollapseThinking(stream.currentStep); // 终态入口强制 flush（D4 清单 2）；attach 回放可能无 step（§5.2）
        if (data.toolCall) {
          var card = toolCards[data.toolCall.id];
          if (card) {
            setCardStatus(card, 'pending');
          } else {
            card = buildToolCard(data.toolCall, 'pending', data.commandPreview || '');
            card.ownedByLiveBlock = true; // 本块新建待确认卡：confirm 后 End 触发 sawToolEnd
            toolCards[data.toolCall.id] = card;
            liveBodyEl().appendChild(card.el);
          }
          if (data.commandPreview) card.previewEl.textContent = data.commandPreview;
        }
        stream.phase = 'waitingConfirm';
        stream.pending = data;
        showConfirmModal(data);
        updateComposer();
        maybeScrollToBottom();
        break;

      case 'completed': // 终态：刷新侧栏（title/usage/updatedAt 已变）；状态栏在 onStreamClosed 统一刷新（泵线程回写时序）
        stream.terminalSeen = true;
        if (stream.currentStep) {
          clearRetryNotice(stream.currentStep);
          flushAndCollapseThinking(stream.currentStep); // 终态入口强制 flush（D4 清单 2）
        }
        goIdle();
        window.sidebarView.refresh().then(function () { window.chatView.syncTopbar(); });
        break;

      case 'error': // 终态
        stream.terminalSeen = true;
        if (stream.currentStep) {
          clearRetryNotice(stream.currentStep);
          flushAndCollapseThinking(stream.currentStep); // 终态入口强制 flush（D4 清单 2）
        }
        handleStreamError(data);
        break;
    }
  }

  function handleStreamError(data) {
    var sessionId = window.appStore.currentSessionId;
    if (data.errorType === 'stopped') {
      // 其他窗口点了停止（后端广播的 stopped 终态，multiWindowStreamingPlan §5.3）：半截消息加「已中断」，静默回空闲
      settleRunningCardsOnStop(); // 定格残留 running 卡片（泵已吞 toolCallEnd，审核根因）
      markInterrupted();
      goIdle();
      return;
    }
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
    // 其它 errorType：优先内联到当前 step 消息体；emptyMessage / 无 step 回退顶部 errorBar（D5/T10）
    var stream = window.appStore.stream;
    var canInline = data.errorType !== 'emptyMessage'
      && stream && stream.currentStep && stream.currentStep.live && stream.currentStep.live.bodyEl;
    if (canInline) {
      appendInlineErrorBlock(stream.currentStep.live.bodyEl, data.message || '模型调用失败');
    } else {
      showError(data.message || '模型调用失败');
    }
    goIdle();
  }

  // 进入 [待确认] 态：pending 卡片 + 弹框（供 pending 恢复 / pendingConfirmationExists 共用）
  function enterWaitingConfirm(pending, restoredLive) {
    var stream = window.appStore.stream;
    if (!stream) {
      // restored 块复用历史 thinking 壳；sawToolEnd=true 表示该 step 已有工具阶段，
      // confirm 后续模型输出将触发 newStep 落到新块（D6 continueConfirmation 边界）
      var live = restoredLive ? buildLiveFromHistory(restoredLive) : null;
      var step = live ? { live: live, textBuf: '', reasoningBuf: '', sawToolEnd: true, paintScheduled: false } : null;
      stream = {
        phase: 'waitingConfirm',
        abort: null,
        currentStep: step,
        steps: step ? [step] : [],
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

  // pending 恢复时，续流需渲染进同一 assistant 块：把历史末块包装为 live 结构。
  // fixPlan Phase4 T4.7（审核 M1 修复）：复用历史已渲染的 thinking 壳，不再插入新空壳（避免双 thinking）；
  // 历史块无 reasoning 时 thinkingEl 为 null--续流 thinking 由新 step 块承担（confirm 后 End->sawToolEnd->delta 触发 newStep）。
  function buildLiveFromHistory(lastAssistant) {
    var live = {
      bodyEl: lastAssistant.bodyEl,
      rowEl: lastAssistant.bodyEl.parentNode,
      thinkingEl: lastAssistant.thinkingEl || null,
      thinkingSummaryEl: lastAssistant.thinkingSummaryEl || null,
      thinkingContentEl: lastAssistant.thinkingContentEl || null,
      thinkingOpen: false,
      userToggledThinking: false,
      reasoningSeen: false,
      contentEl: lastAssistant.contentEl,
      interrupted: false
    };
    if (live.thinkingSummaryEl) {
      live.thinkingSummaryEl.addEventListener('click', function () { live.userToggledThinking = true; });
    }
    return live;
  }

  function onStreamClosed() {
    var stream = window.appStore.stream;
    // 连接关闭 = 泵线程已回写用量（先回写后哨兵，D7）；completed 已 goIdle 置空 stream 也需刷新，statusBar 内部防会话竞态
    window.statusBar.refresh();
    if (!stream) { focusComposerIfReady(); return; } // v1.11：completed/error/stopped 已 goIdle 置空 stream，早退前必须补 focus（F1 主路径）；切页/登录门由三守卫拦截
    if (stream.phase === 'waitingConfirm') return; // 等用户确认，保持该态，不抢焦点
    if (stream.phase === 'stopping') {
      goIdle();
      focusComposerIfReady(); // 本窗口点停止：早退分支单独补 focus（v1.10）
      return;
    }
    if (!stream.terminalSeen) {
      // 未收到任何终态事件连接断开 → 按「中断」处理（契约 §1.3）
      markInterrupted();
      showError('连接中断：未收到终态事件，刷新页面可恢复最新状态。');
    }
    goIdle();
    focusComposerIfReady(); // 主收口：终态/中断/跨窗口 stopped 后的连接关闭（v1.10）
  }

  var lastUserSend = null; // { text, attachments }：409 静默重试用，避免 composer 已清空导致 send() 空转

  function onStreamFailed(error, meta) {
    // REST 预检失败（400/404/409，未开流）：回空闲态；待确认场景可重进会话经 GET pending 自愈
    // G4：send 撞 409「活跃流」时 goIdle 后静默重试一次（仅当无新流抢占；confirm 不走此分支）。
    meta = meta || {};
    if (meta.fromSend && !meta.isRetry && error && error.status === 409 && String(error.message || '').indexOf('活跃流') !== -1) {
      var failedStream = window.appStore.stream;
      if (failedStream && failedStream.currentStep && failedStream.currentStep.live && failedStream.currentStep.live.rowEl) {
        failedStream.currentStep.live.rowEl.remove();
      }
      goIdle();
      if (window.appStore.stream === null && lastUserSend) {
        var retryPayload = lastUserSend;
        setTimeout(function () { send({ retry: true, payload: retryPayload }); }, 600);
        return;
      }
    }
    lastUserSend = null;
    showError(error.message);
    goIdle();
    focusComposerIfReady(); // 预检失败不经 onStreamClosed，单独补 focus（v1.10）
  }

  function goIdle() {
    var stream = window.appStore.stream;
    if (stream && stream.currentStep) flushLivePaint(stream.currentStep); // 双保险（D4 清单 4）
    window.appStore.stream = null;
    stickToBottom = true;
    hideJumpToBottom();
    updateComposer();
  }

  /* ---------- 发消息 / 确认 / 停止 ---------- */

  async function send(options) {
    var sessionId = window.appStore.currentSessionId;
    var stream = window.appStore.stream;
    if (!sessionId || stream) return;
    stickToBottom = true;
    options = options || {};
    var isRetry = !!options.retry && options.payload;
    var text;
    var attachments;
    if (isRetry) {
      if (window.appStore.stream !== null) return;
      text = options.payload.text;
      attachments = options.payload.attachments || [];
    } else {
      if (sendButton.disabled) return; // await 期间挡双击/连按 Enter
      var chip = window.skillChip && window.skillChip.get();
      var userText = composerInput.value.trim();
      attachments = window.fileMention.getAttachments();
      if (!chip && !userText && attachments.length === 0) return; // D8：纯附件可发；chip 单独也可发
      if (chip) window.skillChip.clear(); // 同步定界：先摘 chip，防双击重复取
      composerInput.value = '';
      autoResize();
      sendButton.disabled = true; // await 期间 stream 仍 null，靠这个挡第二次进入
      var wireText = userText;
      var displayText = userText;
      if (chip) {
        var skillResult;
        try {
          skillResult = await window.api.getSkillBody(chip.name);
        } catch (error) {
          window.skillChip.pin(chip.name);
          composerInput.value = userText;
          autoResize();
          sendButton.disabled = false;
          if (window.toast) window.toast('加载技能失败：' + ((error && error.message) || '未知错误'));
          return;
        }
        if (sessionId !== window.appStore.currentSessionId || window.appStore.stream) {
          window.skillChip.pin(chip.name);
          composerInput.value = userText;
          autoResize();
          sendButton.disabled = false;
          if (window.toast) window.toast('会话已切换，未发送');
          return;
        }
        var bodyText = (skillResult && skillResult.body) || '';
        wireText = bodyText ? (userText ? bodyText + '\n\n' + userText : bodyText) : userText;
        displayText = '/skill:' + chip.name + (userText ? '\n' + userText : '');
      }
      if (!wireText && attachments.length === 0) {
        sendButton.disabled = false;
        return;
      }
      appendUserMessage(displayText, attachments);
      window.fileMention.clearChips();
      lastUserSend = { text: wireText, attachments: attachments };
      text = wireText;
    }

    var step = createStep();
    window.appStore.stream = {
      phase: 'streaming',
      abort: null,
      currentStep: step,
      steps: [step],
      terminalSeen: false,
      pending: null
    };
    updateComposer();

    var body = { sessionId: sessionId, message: text };
    if (attachments.length > 0) body.attachments = attachments;
    var handle = window.sse.streamPost('/api/chat/stream', body, onStreamEvent);
    window.appStore.stream.abort = handle.abort;
    handle.done.then(onStreamClosed).catch(function (error) {
      onStreamFailed(error, { fromSend: true, isRetry: isRetry });
    });
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
    if (!stream.currentStep) {
      var step = createStep();
      stream.currentStep = step;
      stream.steps = [step];
    }
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
    if (stream.currentStep) flushLivePaint(stream.currentStep); // 进 stopping 前 buffer 强制上屏（D4 清单 3）
    markInterrupted(); // 立即停渲染 + 半截消息加「已中断」标记
    settleRunningCardsOnStop(); // 本窗口点停止：定格残留 running 卡片（本窗口 abort 后收不到后端 stopped 广播，走不到 handleStreamError）
    updateComposer();
    // fire-and-forget：先发出 stop POST，再同步 abort 本窗口 SSE；保持 stopping 到 onStreamClosed。
    var stopDone = window.api.stopChat(sessionId).catch(function () { return null; });
    if (stream.abort) stream.abort();
    await stopDone;
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
    } else {
      // 乐观 attach（multiWindowStreamingPlan §5.1）：历史已先渲染；有活跃流则截断重渲染 + 回放续播，404 静默保持历史态。
      // pending 态必无活跃流（泵在 confirmationRequired 已终态），跳过 attach 以免占位态覆盖 waitingConfirm。
      attachStream(sessionId, messages);
      // 此处不补 focus：attachStream 同步置 attaching 禁用 composer，focus 必被守卫 2 拦截（v1.11 验收发现 v1.10 死代码）。
      // attach 落空/失败由 resetToHistoryState 补，attach 成功的终态由 onStreamClosed(!stream) 补。
    }
    stickToBottom = true;
    scrollToBottom();
  }

  /* ---------- attach 回放式重连（multiWindowStreamingPlan §5.1） ---------- */

  function attachStream(sessionId, messages) {
    var preInitBuf = []; // streamResume 之前到达的事件缓冲（meta 必为首帧，此为防御性缓冲）
    var initialized = false;
    // attaching 占位流态：close() 能 abort 在途 attach；send() 被 stream 非空拦截；updateComposer 禁用输入与按钮
    var placeholder = { phase: 'attaching', abort: null, currentStep: null, steps: [], terminalSeen: false, pending: null };
    window.appStore.stream = placeholder;
    updateComposer();
    var handle = window.sse.streamPost('/api/chat/attach', { sessionId: sessionId }, function (event, data) {
      if (!initialized) {
        if (event !== 'streamResume') { preInitBuf.push({ event: event, data: data }); return; }
        if (sessionId !== window.appStore.currentSessionId) return; // 已切走，丢弃迟到初始化
        if (window.appStore.stream !== placeholder) return; // 已被新 attach 替换（A→B→A 快速重进）
        initAttachedStream(messages, data || {});
        initialized = true;
        preInitBuf.forEach(function (item) { onStreamEvent(item.event, item.data); });
        preInitBuf = null;
        return;
      }
      onStreamEvent(event, data);
    });
    placeholder.abort = handle.abort;
    handle.done.then(function () {
      if (!initialized) { resetToHistoryState(false); return; } // 未初始化即结束（极端竞态）
      onStreamClosed();
    }).catch(function (error) {
      if (!initialized) { resetToHistoryState(error.status !== 404, error); return; } // 404 静默；其余提示
      onStreamFailed(error);
    });

    // 历史已在 attach 前渲染（含 pending），兜底只需复位 composer；404=无活跃流/竞态结束属常态，静默
    function resetToHistoryState(withHint, error) {
      if (sessionId !== window.appStore.currentSessionId) return; // 已切走，不污染新视图
      if (window.appStore.stream !== placeholder) return; // 别清掉新 attach 的占位
      window.appStore.stream = null;
      updateComposer();
      focusComposerIfReady(); // v1.11：attach 落空/失败后 composer 恢复可用，回落焦点（C1 真正生效点）
      if (withHint) showError('流恢复失败（' + error.message + '）；页面为静态历史，流可能仍在后台运行，刷新重试。');
    }
  }

  function initAttachedStream(messages, meta) {
    // baseCount 水位线截断：本次流已落盘的尾巴由事件回放重建，不丢不重（§3.1）
    var baseCount = Math.min(meta.baseCount || 0, messages.length);
    renderHistory(messages.slice(0, baseCount), null);
    if (meta.userMessage) {
      appendUserMessage(meta.userMessage); // 历史渲染同构（ATTACHMENT_RE 解析附件块）
    } else {
      // confirm 流（userMessage=null）可能中途落盘 queued 用户消息（agent.py driveConfirmation）：
      // baseCount 之后的 user 消息不在回放事件里，需补渲染（位置近似，§6-E13 已声明）
      messages.slice(baseCount).forEach(function (msg) {
        if (msg.kind === 'user') appendUserMessage(msg.content);
      });
    }
    var stream = window.appStore.stream; // 复用占位态，迁移为 streaming
    stream.phase = 'streaming';
    stream.currentStep = null; // 懒建：首个 delta/工具事件时建块，避免全归位历史卡片时留空气泡
    stream.steps = [];
    stickToBottom = true;
    updateComposer();
    scrollToBottom();
  }

  function autoResize() {
    composerInput.style.height = 'auto';
    composerInput.style.height = Math.min(composerInput.scrollHeight, 200) + 'px';
  }

  /* ---------- 对外接口 ---------- */

  window.chatView = {
    open: async function (sessionId) {
      window.appStore.currentSessionId = sessionId;
      stickToBottom = true;
      hideJumpToBottom();
      chatEmptyEl.classList.add('hidden');
      messageListEl.classList.remove('hidden');
      errorBarEl.classList.add('hidden');
      window.chatView.syncTopbar();
      updateComposer();
      window.fileMention.resetForSession();
      if (window.skillChip) window.skillChip.clear();
      window.fileExplorer.open();
      try {
        await reloadSession(sessionId);
        if (!window.appStore.stream) updateComposer();
        window.statusBar.refresh();
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
      stickToBottom = true;
      hideJumpToBottom();
      topbarTitleEl.textContent = 'FlamingoAgents';
      topbarModelEl.textContent = '';
      messageListEl.innerHTML = '';
      messageListEl.classList.add('hidden');
      chatEmptyEl.classList.remove('hidden');
      errorBarEl.classList.add('hidden');
      window.statusBar.hide();
      window.fileExplorer.hide();
      window.fileMention.resetForSession();
      if (window.skillChip) window.skillChip.clear();
      updateComposer();
    },

    // 路由切走时：主动 abort 前端流（后端泵线程继续跑到终态，重进自愈，契约 §5）
    close: function () {
      var stream = window.appStore.stream;
      if (stream && stream.abort) stream.abort();
      window.appStore.stream = null;
      hideConfirmModal();
      stickToBottom = true;
      hideJumpToBottom();
    },

    // /model 切换放弃待确认（§3.3）：关确认框、回空闲态
    discardPendingConfirm: function () {
      hideConfirmModal();
      window.appStore.stream = null;
      updateComposer();
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
