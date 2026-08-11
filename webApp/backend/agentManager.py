'''
Author: wilbur
Version: 1.5
Date: 2026-08-11
Description: sessionId → agent 实例缓存（懒建、模型配置变更后置失效标记惰性重建）、活跃流登记（同会话并发 409）、停止标志与泵线程结构。
            v1.1 随包改名调整 import（webApp.backend.*）。
            v1.2 迭代一（方案 §11.4）：泵线程流开始快照 usageTotal、终态算 delta 先写 usageStore.usageTurns（后回写 sessions 索引，原有回写不变）。
            v1.3 迭代二（方案 §3.3/§3.6）：新增 dropAgentIfIdle（单锁完成查活跃流+丢缓存，/model 指令用）；泵线程回写索引时附带 contextTokens（conversation.lastTurnTokens）。
            v1.4 状态栏口径：回写索引时附带 lastUsage=本轮 delta（↑↓⚡ 展示最近一轮，不再用会话累计）。
            v1.5 多窗口并行（multiWindowStreamingPlan §4.1）：streamPump 广播化——单队列改「事件 history + 多订阅者队列」（subLock 内回放/广播/关闭，不丢不重）；
            startStream 增加 meta（baseCount/userMessage，attach 首帧用）；新增 getActivePump/subscribe/unsubscribe/compactDeltas；
            stop 分支补广播 stopped 终态（其他订阅窗口静默收尾，不再误报连接中断）。
'''

from __future__ import annotations

import queue
import threading

from flamingoAgents import createAgent
from flamingoAgents.core.types import errorEvent, reasoningDeltaEvent, terminalEventTypes, textDeltaEvent

from webApp.backend import sessionStore, usageStore
from webApp.backend.sessionStore import sessionLogsDir

managerLock = threading.RLock()
agentCache: dict[str, object] = {}
staleSessionIds: set[str] = set()
activeStreams: dict[str, 'streamPump'] = {}


def getAgent(sessionId: str):
    # 懒建缓存：按索引中的 workDir/providerId/modelId 建 agent，集中 logDir 到 webData/sessionLogs。
    meta = sessionStore.getSession(sessionId)
    if meta is None:
        raise RuntimeError(f'会话不存在：{sessionId}')
    with managerLock:
        cached = agentCache.get(sessionId)
        if cached is not None and sessionId not in staleSessionIds:
            return cached
        newAgent = createAgent(
            workDir=meta['workDir'],
            logDir=sessionLogsDir,
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
    # stop 只置标志位，库生成器由泵线程自己 close（跨线程 close 会抛 ValueError）。
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
        self.thread = threading.Thread(target=self._pump, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def requestStop(self) -> None:
        self.stopFlag.set()

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

    def _broadcast(self, event) -> None:
        with self.subLock:
            self.history.append(event)
            for subscriber in self.subscribers:
                subscriber.put(event)

    def _pump(self) -> None:
        startUsage = self._currentUsage()
        try:
            for event in self.stream:
                if self.stopFlag.is_set():
                    # stop 必须广播终态（multiWindowStreamingPlan 审核修复）：否则其他订阅窗口
                    # onStreamClosed 命中 !terminalSeen 误报「连接中断」；前端按 errorType='stopped' 静默收尾。
                    self._broadcast(errorEvent(message='已停止。', errorType='stopped'))
                    break
                self._broadcast(event)
                if isinstance(event, terminalEventTypes):
                    break
        except Exception as error:
            self._broadcast(errorEvent(message=str(error), errorType=type(error).__name__))
        finally:
            self.stream.close()
            self._recordUsage(startUsage)
            unregisterStream(self.sessionId)
            with self.subLock:
                self.closed = True
                for subscriber in self.subscribers:
                    subscriber.put(None)  # 哨兵：通知各 SSE 生成器结束

    def _currentUsage(self) -> dict:
        # 从已缓存 conversation 读 usageTotal（禁止 getConversation()，避免为未发消息会话落 jsonl 的副作用）。
        with self.agent.sessionLocksGuard:
            currentConversation = self.agent.conversations.get(self.sessionId)
        if currentConversation is None:
            return {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
        usage = currentConversation.usageTotal
        return {key: int(usage.get(key, 0) or 0) for key in ('promptTokens', 'cachedTokens', 'completionTokens')}

    def _recordUsage(self, startUsage: dict) -> None:
        # 回写时机在泵线程结束（审核 L4）：客户端早断时泵仍跑到终态，回写值才完整。
        # 会话可能尚未建 conversation（如 pendingConfirmationExists 直通错误），无则跳过。
        # 顺序（方案 §11.4）：先写 usageTurns（账单，delta 任一项 >0 才写），后回写 sessions 索引（回写失败不丢账）。
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
