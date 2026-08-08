'''
Author: wilbur
Version: 1.4
Date: 2026-08-08
Description: sessionId → agent 实例缓存（懒建、模型配置变更后置失效标记惰性重建）、活跃流登记（同会话并发 409）、停止标志与泵线程结构。
            v1.1 随包改名调整 import（webApp.backend.*）。
            v1.2 迭代一（方案 §11.4）：泵线程流开始快照 usageTotal、终态算 delta 先写 usageStore.usageTurns（后回写 sessions 索引，原有回写不变）。
            v1.3 迭代二（方案 §3.3/§3.6）：新增 dropAgentIfIdle（单锁完成查活跃流+丢缓存，/model 指令用）；泵线程回写索引时附带 contextTokens（conversation.lastTurnTokens）。
            v1.4 状态栏口径：回写索引时附带 lastUsage=本轮 delta（↑↓⚡ 展示最近一轮，不再用会话累计）。
'''

from __future__ import annotations

import queue
import threading

from flamingoAgents import createAgent
from flamingoAgents.core.types import errorEvent, terminalEventTypes

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


def startStream(sessionId: str, agentInstance, stream) -> 'streamPump | None':
    # 同会话已有活跃流时返回 None（路由层映射 409）；登记与启动在同一把锁内完成。
    with managerLock:
        if sessionId in activeStreams:
            return None
        pump = streamPump(sessionId, agentInstance, stream)
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
    # 泵线程 + 队列 + 停止标志（webAppPlan §4.3-H1）：SSE 生成器只从队列取事件，
    # stop 只置标志位，库生成器由泵线程自己 close（跨线程 close 会抛 ValueError）。
    def __init__(self, sessionId: str, agentInstance, stream):
        self.sessionId = sessionId
        self.agent = agentInstance
        self.stream = stream
        self.eventQueue: queue.Queue = queue.Queue()
        self.stopFlag = threading.Event()
        self.thread = threading.Thread(target=self._pump, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def requestStop(self) -> None:
        self.stopFlag.set()

    def _pump(self) -> None:
        startUsage = self._currentUsage()
        try:
            for event in self.stream:
                if self.stopFlag.is_set():
                    break
                self.eventQueue.put(event)
                if isinstance(event, terminalEventTypes):
                    break
        except Exception as error:
            self.eventQueue.put(errorEvent(message=str(error), errorType=type(error).__name__))
        finally:
            self.stream.close()
            self._recordUsage(startUsage)
            unregisterStream(self.sessionId)
            self.eventQueue.put(None)  # 哨兵：通知 SSE 生成器结束

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
