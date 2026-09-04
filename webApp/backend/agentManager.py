'''
Author: wilbur
Version: 1.9
Date: 2026-09-02
Description: sessionId → agent 实例缓存（懒建、模型配置变更后置失效标记惰性重建）、活跃流登记（同会话并发 409）、停止标志与泵线程结构。
            v1.1 随包改名调整 import（webApp.backend.*）。
            v1.2 迭代一（方案 §11.4）：泵线程流开始快照 usageTotal、终态算 delta 先写 usageStore.usageTurns（后回写 sessions 索引，原有回写不变）。
            v1.3 迭代二（方案 §3.3/§3.6）：新增 dropAgentIfIdle（单锁完成查活跃流+丢缓存，/model 指令用）；泵线程回写索引时附带 contextTokens（conversation.lastTurnTokens）。
            v1.4 状态栏口径：回写索引时附带 lastUsage=本轮 delta（↑↓⚡ 展示最近一轮，不再用会话累计）。
            v1.5 多窗口并行（multiWindowStreamingPlan §4.1）：streamPump 广播化——单队列改「事件 history + 多订阅者队列」（subLock 内回放/广播/关闭，不丢不重）；
            startStream 增加 meta（baseCount/userMessage，attach 首帧用）；新增 getActivePump/subscribe/unsubscribe/compactDeltas；
            stop 分支补广播 stopped 终态（其他订阅窗口静默收尾，不再误报连接中断）。
            v1.6（stopResponsivenessPlan L2）：requestStop 改主动收尾（interrupt + 广播 stopped + 幂等 usage + 注销 + 关订阅）；
            doneEvent/usageRecorded/historyOverflowed；_broadcast 拦截已终态事件；history 2000 截尾。
            v1.7 logDir 按会话 workDir 注入 ~/.flamingo/logs/webData/<workDir路径>/，不再用扁平 sessionLogsDir。
            v1.8 泵异常与 sseGen 意外异常落 jsonl（pumpError/sseGenError），只用 conversations.get，禁止 getConversation。
            v1.9 泵/sseGen 诊断落盘失败不得盖掉真正的流异常。
'''

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

from flamingoAgents import createAgent
from flamingoAgents.core.types import errorEvent, reasoningDeltaEvent, terminalEventTypes, textDeltaEvent
from flamingoAgents.utils.logPaths import ensureSessionLogDir

from webApp.backend import sessionStore, usageStore

managerLock = threading.RLock()
agentCache: dict[str, object] = {}
staleSessionIds: set[str] = set()
activeStreams: dict[str, 'streamPump'] = {}
HISTORY_MAX_EVENTS = 2000


def getAgent(sessionId: str):
    # 懒建缓存：按索引中的 workDir/providerId/modelId 建 agent，logDir 落到 ~/.flamingo/logs/webData/<workDir路径>/。
    meta = sessionStore.getSession(sessionId)
    if meta is None:
        raise RuntimeError(f'会话不存在：{sessionId}')
    with managerLock:
        cached = agentCache.get(sessionId)
        if cached is not None and sessionId not in staleSessionIds:
            return cached
        newAgent = createAgent(
            workDir=meta['workDir'],
            logDir=ensureSessionLogDir('webData', Path(meta['workDir'])),
            providerId=meta['providerId'],
            modelId=meta['modelId'],
        )
        agentCache[sessionId] = newAgent
        staleSessionIds.discard(sessionId)
        return newAgent


def getCachedAgent(sessionId: str):
    # 仅供 pending 查询：不触发建实例（避免新建 jsonl）。
    with managerLock:
        return agentCache.get(sessionId)


def dropAgent(sessionId: str) -> None:
    with managerLock:
        agentCache.pop(sessionId, None)
        staleSessionIds.discard(sessionId)


def dropAgentIfIdle(sessionId: str) -> bool:
    # /model 指令（迭代二 §3.3，评审 M7）：同一把锁内完成「有活跃流 → False / 否则丢缓存 → True」，消除竞态窗口。
    with managerLock:
        if sessionId in activeStreams:
            return False
        agentCache.pop(sessionId, None)
        staleSessionIds.discard(sessionId)
        return True


def invalidateAllAgents() -> None:
    # 模型配置变更：置失效标记而非立即销毁，下次 getAgent 惰性重建（进行中的流不受影响）。
    with managerLock:
        staleSessionIds.update(agentCache.keys())


def hasActiveStream(sessionId: str) -> bool:
    with managerLock:
        return sessionId in activeStreams


def getActivePump(sessionId: str) -> 'streamPump | None':
    # attach 路由用（multiWindowStreamingPlan §4.3）：无活跃流 → None（映射 404）。
    with managerLock:
        return activeStreams.get(sessionId)


def compactDeltas(events: list) -> list:
    # 合并相邻同型 textDelta/reasoningDelta（text 拼接），其余事件原样保序；
    # 仅作用于 subscribe 回放副本（避免前端回放 O(n²) 重渲染），不改 history 本体。
    compacted: list = []
    for event in events:
        if compacted:
            previous = compacted[-1]
            if isinstance(event, textDeltaEvent) and isinstance(previous, textDeltaEvent):
                compacted[-1] = textDeltaEvent(text=previous.text + event.text)
                continue
            if isinstance(event, reasoningDeltaEvent) and isinstance(previous, reasoningDeltaEvent):
                compacted[-1] = reasoningDeltaEvent(text=previous.text + event.text)
                continue
        compacted.append(event)
    return compacted


def startStream(sessionId: str, agentInstance, stream, meta: dict | None = None) -> 'streamPump | None':
    # 同会话已有活跃流时返回 None（路由层映射 409）；登记与启动在同一把锁内完成。
    # meta = {'baseCount': int, 'userMessage': str|None}：attach 首帧 streamResume 用（multiWindowStreamingPlan §4.3）。
    with managerLock:
        if sessionId in activeStreams:
            return None
        pump = streamPump(sessionId, agentInstance, stream, meta=meta)
        activeStreams[sessionId] = pump
        pump.start()
        return pump


def unregisterStream(sessionId: str) -> None:
    with managerLock:
        activeStreams.pop(sessionId, None)


def requestStop(sessionId: str) -> bool:
    with managerLock:
        pump = activeStreams.get(sessionId)
        if pump is None:
            return False
        pump.requestStop()
        return True


class streamPump:
    # 泵线程 + 广播结构（multiWindowStreamingPlan §4.1）：事件 history + 多订阅者队列。
    # subscribe/_broadcast/结束置 closed 均在 subLock 内 → 回放与实时无缝衔接，不丢不重；
    # v1.6：requestStop 主动收尾（不再只置标志等泵消费），doneEvent 供 chatStream 宽容闸等待。
    def __init__(self, sessionId: str, agentInstance, stream, meta: dict | None = None):
        self.sessionId = sessionId
        self.agent = agentInstance
        self.stream = stream
        self.meta = meta or {}
        self.subLock = threading.Lock()
        self.history: list = []
        self.subscribers: list[queue.Queue] = []
        self.closed = False
        self.stopFlag = threading.Event()
        self.doneEvent = threading.Event()
        self.usageRecorded = False
        self.historyOverflowed = False
        self.startUsage = self._currentUsage()
        self.thread = threading.Thread(target=self._pump, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def requestStop(self) -> None:
        # 主动收尾（stopResponsivenessPlan L2）：幂等早退 → 置标志 → 叫醒库内阻塞
        # → 记 usage → 注销泵 → 同锁广播 stopped + 关订阅 + 置 doneEvent（竞态红线）。
        # doneEvent 必须在 unregister 之后置位，否则宽容闸 wait 成功后 startStream 仍撞旧泵。
        if self.doneEvent.is_set():
            return
        self.stopFlag.set()
        try:
            self.agent.interruptActiveStreams(self.sessionId)
        except Exception:
            pass
        self._recordUsage()
        unregisterStream(self.sessionId)
        self._sealStopped()

    def _sealStopped(self) -> None:
        # 同锁写入 stopped + 关订阅 + 置 doneEvent，保证 stopped 是 history 尾事件。
        with self.subLock:
            if self.doneEvent.is_set():
                return
            event = errorEvent(message='已停止。', errorType='stopped')
            self.history.append(event)
            self._trimHistoryIfNeeded()
            for subscriber in self.subscribers:
                subscriber.put(event)
            self._closeSubscribersLocked()
            self.doneEvent.set()

    def subscribe(self) -> queue.Queue:
        # 新订阅者：先回放 history（压缩连续 delta），泵已关则直接补哨兵，否则登记跟实时。
        subscriber: queue.Queue = queue.Queue()
        with self.subLock:
            for event in compactDeltas(self.history):
                subscriber.put(event)
            if self.closed:
                subscriber.put(None)
            else:
                self.subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        # 订阅者断连反注册（sseGen finally 调用），防止死订阅队列继续堆积事件。
        with self.subLock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)

    def _trimHistoryIfNeeded(self) -> None:
        # 超长丢最旧 delta 段（保留终态尾部），stopped 必须仍是 history 最后一个事件。
        while len(self.history) > HISTORY_MAX_EVENTS:
            startIndex = 0
            while startIndex < len(self.history) and not isinstance(
                self.history[startIndex], (textDeltaEvent, reasoningDeltaEvent)
            ):
                startIndex += 1
            if startIndex >= len(self.history):
                return
            endIndex = startIndex
            while endIndex < len(self.history) and isinstance(
                self.history[endIndex], (textDeltaEvent, reasoningDeltaEvent)
            ):
                endIndex += 1
            del self.history[startIndex:endIndex]
            self.historyOverflowed = True

    def _broadcast(self, event) -> None:
        with self.subLock:
            if self.doneEvent.is_set():
                return
            self.history.append(event)
            self._trimHistoryIfNeeded()
            for subscriber in self.subscribers:
                subscriber.put(event)

    def _closeSubscribersLocked(self) -> None:
        # 调用方必须已持有 subLock。closed + 各订阅队列放哨兵。
        self.closed = True
        for subscriber in self.subscribers:
            subscriber.put(None)

    def _closeSubscribers(self) -> None:
        # subLock 内关订阅并置 doneEvent（竞态红线：哨兵与 doneEvent 同临界区）。
        with self.subLock:
            if self.doneEvent.is_set():
                return
            self._closeSubscribersLocked()
            self.doneEvent.set()

    def _pump(self) -> None:
        try:
            for event in self.stream:
                if self.stopFlag.is_set():
                    # requestStop 已广播 stopped；此处只跳出，避免泵再追加事件。
                    break
                self._broadcast(event)
                if isinstance(event, terminalEventTypes):
                    break
        except Exception as error:
            try:
                stack = traceback.format_exc()
                traceback.print_exc()
                self._logDiagEvent('pumpError', error, stack)
            except Exception:
                pass
            self._broadcast(errorEvent(message=str(error), errorType=type(error).__name__))
        finally:
            self.stream.close()
            self._recordUsage()
            if self.stopFlag.is_set():
                # 中断路径由 requestStop 负责广播 stopped / 关订阅 / 置 doneEvent，
                # 避免泵先关连接导致其他窗口收不到 stopped（G3）。
                return
            if not self.doneEvent.is_set():
                unregisterStream(self.sessionId)
                self._closeSubscribers()

    def logSseGenError(self, error) -> None:
        try:
            stack = traceback.format_exc()
            traceback.print_exc()
            self._logDiagEvent('sseGenError', error, stack)
        except Exception:
            pass

    def _logDiagEvent(self, eventType: str, error, stack: str) -> None:
        try:
            with self.agent.sessionLocksGuard:
                currentConversation = self.agent.conversations.get(self.sessionId)
            if currentConversation is None:
                return
            currentConversation.logger.logEvent({
                'type': eventType,
                'sessionId': self.sessionId,
                'errorType': type(error).__name__,
                'message': str(error),
                'traceback': stack,
            })
        except Exception:
            pass

    def _currentUsage(self) -> dict:
        # 从已缓存 conversation 读 usageTotal（禁止 getConversation()，避免为未发消息会话落 jsonl 的副作用）。
        with self.agent.sessionLocksGuard:
            currentConversation = self.agent.conversations.get(self.sessionId)
        if currentConversation is None:
            return {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
        usage = currentConversation.usageTotal
        return {key: int(usage.get(key, 0) or 0) for key in ('promptTokens', 'cachedTokens', 'completionTokens')}

    def _recordUsage(self) -> None:
        # 回写时机在泵线程结束（审核 L4）：客户端早断时泵仍跑到终态，回写值才完整。
        # 会话可能尚未建 conversation（如 pendingConfirmationExists 直通错误），无则跳过。
        # 顺序（方案 §11.4）：先写 usageTurns（账单，delta 任一项 >0 才写），后回写 sessions 索引（回写失败不丢账）。
        # v1.6 幂等：requestStop 与泵 finally 都可能调用，首行守卫消除双记。
        if self.usageRecorded:
            return
        self.usageRecorded = True
        startUsage = self.startUsage
        with self.agent.sessionLocksGuard:
            currentConversation = self.agent.conversations.get(self.sessionId)
        if currentConversation is None:
            return
        finalUsage = {key: int(currentConversation.usageTotal.get(key, 0) or 0) for key in startUsage}
        delta = {key: finalUsage[key] - startUsage[key] for key in finalUsage}
        meta = sessionStore.getSession(self.sessionId) or {}
        usageStore.writeUsageTurn(self.sessionId, meta.get('providerId', 'unknown'), meta.get('modelId', ''), delta)
        sessionStore.updateUsage(
            self.sessionId,
            finalUsage,
            contextTokens=int(currentConversation.lastTurnTokens or 0),
            lastUsage=delta,
        )
