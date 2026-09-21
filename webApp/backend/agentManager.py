'''
Author: wilbur
Version: 1.13
Date: 2026-09-21
Description: sessionId → agent 实例缓存、活跃流登记与泵线程结构。v1.13 Web Agent 注入 usageSource=web；状态栏费用改读 usageEvents，泵终态不再写 usageTurns。
'''

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

from flamingoAgents import createAgent
from flamingoAgents.core.types import errorEvent, reasoningDeltaEvent, terminalEventTypes, textDeltaEvent, usageUpdateEvent
from flamingoAgents.utils.logPaths import ensureSessionLogDir

from webApp.backend import sessionStore, usageStore
from webApp.backend.sseCodec import usageUpdateDto

managerLock = threading.RLock()
agentCache: dict[str, object] = {}
staleSessionIds: set[str] = set()
activeStreams: dict[str, 'streamPump'] = {}
HISTORY_MAX_EVENTS = 2000
CORE_IDLE_WAIT_SECONDS = 2.0


def getAgent(sessionId: str):
    # 懒建缓存：按索引中的 workDir/providerId/modelId 建 agent，logDir 落到 ~/.flamingo/logs/webData/<workDir路径>/。
    # draining 期间必须返回旧 pump.agent，禁止旧 worker 尚未退出时建新锁实例。
    meta = sessionStore.getSession(sessionId)
    if meta is None:
        raise RuntimeError(f'会话不存在：{sessionId}')
    with managerLock:
        pump = activeStreams.get(sessionId)
        if pump is not None:
            return pump.agent
        cached = agentCache.get(sessionId)
        if cached is not None and sessionId not in staleSessionIds:
            return cached
        newAgent = createAgent(
            workDir=meta['workDir'],
            logDir=ensureSessionLogDir('webData', Path(meta['workDir'])),
            providerId=meta['providerId'],
            modelId=meta['modelId'],
            usageSource='web',
        )
        agentCache[sessionId] = newAgent
        staleSessionIds.discard(sessionId)
        return newAgent


def getCachedAgent(sessionId: str):
    # 仅供 pending 查询：不触发建实例（避免新建 jsonl）。
    with managerLock:
        return agentCache.get(sessionId)


def dropAgent(sessionId: str) -> bool:
    with managerLock:
        if sessionId in activeStreams:
            staleSessionIds.add(sessionId)
            return False
        agentCache.pop(sessionId, None)
        staleSessionIds.discard(sessionId)
        return True


def dropAgentIfIdle(sessionId: str) -> bool:
    # /model 指令（迭代二 §3.3，评审 M7）：同一把锁内完成「有活跃流 → False / 否则丢缓存 → True」，消除竞态窗口。
    # draining 期间不替换旧 agent，但标 stale，确保 Core 完成后下次 getAgent 重建。
    with managerLock:
        if sessionId in activeStreams:
            staleSessionIds.add(sessionId)
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


def startStream(sessionId: str, agentInstance, stream, meta: dict | None = None, runEvent: threading.Event | None = None) -> 'streamPump | None':
    # claim-before-start：先复查无 active、构造并注册 pump identity，随后才启动线程。
    startError = None
    with managerLock:
        if sessionId in activeStreams:
            return None
        pump = streamPump(sessionId, agentInstance, stream, meta=meta, runEvent=runEvent)
        activeStreams[sessionId] = pump
        try:
            pump.start()
        except Exception as error:
            if activeStreams.get(sessionId) is pump:
                activeStreams.pop(sessionId, None)
            pump.coreDoneEvent.set()
            startError = error
    if startError is not None:
        try:
            stream.close()
        except Exception:
            pass
        raise startError
    return pump


def unregisterStream(sessionId: str) -> None:
    with managerLock:
        activeStreams.pop(sessionId, None)


def finishStream(sessionId: str, expectedPump: 'streamPump') -> None:
    with managerLock:
        if activeStreams.get(sessionId) is expectedPump:
            activeStreams.pop(sessionId, None)
        expectedPump.coreDoneEvent.set()


def waitForCoreIdle(sessionId: str, timeout: float = CORE_IDLE_WAIT_SECONDS) -> str:
    with managerLock:
        pump = activeStreams.get(sessionId)
        if pump is None:
            return 'idle'
        draining = (
            pump.stopFlag.is_set()
            or pump.stopClaimed
            or pump.terminalSeen
            or pump.doneEvent.is_set()
        )
        if not draining:
            return 'active'
        coreDone = pump.coreDoneEvent
    if not coreDone.wait(timeout):
        return 'timeout'
    with managerLock:
        current = activeStreams.get(sessionId)
        if current is None:
            return 'idle'
        if current is pump:
            return 'timeout'
        stillDraining = (
            current.stopFlag.is_set()
            or current.stopClaimed
            or current.terminalSeen
            or current.doneEvent.is_set()
        )
        return 'timeout' if stillDraining else 'active'


def requestStop(sessionId: str) -> bool:
    with managerLock:
        pump = activeStreams.get(sessionId)
        if pump is None:
            return False
        claimed = claimStopOwner(pump)
        alreadyTerminal = pump.terminalSeen
    if not claimed:
        if alreadyTerminal:
            try:
                pump._recordUsage()
            except Exception:
                pass
        return True
    dispatchStop(pump)
    return True


def claimStopOwner(pump: 'streamPump') -> bool:
    if pump.terminalSeen or pump.stopClaimed:
        return False
    pump.stopClaimed = True
    pump.stopDispatchDone.clear()
    pump.stopFlag.set()
    if pump.runEvent is not None:
        pump.runEvent.set()
    return True


def dispatchStop(pump: 'streamPump') -> None:
    try:
        try:
            pump.agent.interruptActiveStreams(pump.sessionId)
        except Exception as error:
            try:
                pump._logDiagEvent('stopInterruptError', error, traceback.format_exc())
            except Exception:
                pass
        try:
            pump._recordUsage()
        except Exception as error:
            try:
                pump._logDiagEvent('stopUsageError', error, traceback.format_exc())
            except Exception:
                pass
        try:
            pump._sealStopped()
        except Exception as error:
            try:
                pump._logDiagEvent('stopSealError', error, traceback.format_exc())
            except Exception:
                pass
    finally:
        pump.stopDispatchDone.set()


class streamPump:
    # 泵线程 + 广播结构（multiWindowStreamingPlan §4.1）：事件 history + 多订阅者队列。
    # subscribe/_broadcast/结束置 closed 均在 subLock 内 → 回放与实时无缝衔接，不丢不重；
    # v1.6：requestStop 主动收尾（不再只置标志等泵消费），doneEvent 供 chatStream 宽容闸等待。
    def __init__(self, sessionId: str, agentInstance, stream, meta: dict | None = None, runEvent: threading.Event | None = None):
        self.sessionId = sessionId
        self.agent = agentInstance
        self.stream = stream
        self.meta = meta or {}
        self.runEvent = runEvent if runEvent is not None else threading.Event()
        self.subLock = threading.Lock()
        self.history: list = []
        self.subscribers: list[queue.Queue] = []
        self.closed = False
        self.stopFlag = threading.Event()
        self.doneEvent = threading.Event()
        self.coreDoneEvent = threading.Event()
        self.stopDispatchDone = threading.Event()
        self.stopDispatchDone.set()
        self.stopClaimed = False
        self.terminalSeen = False
        self.usageRecorded = False
        self.usageRecordLock = threading.Lock()
        self.usageRecordDone = threading.Event()
        self.historyOverflowed = False
        config = agentInstance.modelAdapter.config
        self.pumpProviderId = str(config.configProviderId or config.provider)
        self.pumpModelId = str(config.model)
        self.liveCostState = 'pending'
        self.dbBaseCost: float | None = None
        self.pumpModelCost: dict | None = None
        self.startUsage = self._currentUsage()
        self.thread = threading.Thread(target=self._pump, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def requestStop(self) -> None:
        with managerLock:
            current = activeStreams.get(self.sessionId)
            claimed = current is self and claimStopOwner(self)
        if claimed:
            dispatchStop(self)

    def claimTerminal(self) -> bool:
        with managerLock:
            current = activeStreams.get(self.sessionId)
            if current is not None and current is not self:
                return False
            if self.stopClaimed or self.terminalSeen:
                return False
            self.terminalSeen = True
            return True

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

    def _ensureLiveCostState(self) -> None:
        if self.liveCostState != 'pending':
            return
        try:
            usageStore.querySessionCost(self.sessionId)
        except Exception as error:
            self.liveCostState = 'unavailable'
            self.dbBaseCost = None
            self.pumpModelCost = None
            try:
                self._logDiagEvent('liveCostInitError', error, traceback.format_exc())
            except Exception:
                pass
            return
        self.liveCostState = 'ready'

    def _toUsageUpdateDto(self, event: usageUpdateEvent) -> usageUpdateDto:
        try:
            sessionStore.updateUsage(
                self.sessionId,
                event.usage,
                contextTokens=event.contextTokens,
                lastUsage=None,
            )
        except Exception as error:
            try:
                self._logDiagEvent('liveUsageIndexError', error, traceback.format_exc())
            except Exception:
                pass
        self._ensureLiveCostState()
        if self.liveCostState != 'ready':
            liveCost = None
        else:
            try:
                liveCost = usageStore.querySessionCost(self.sessionId)
            except Exception as error:
                liveCost = None
                self.liveCostState = 'unavailable'
                try:
                    self._logDiagEvent('liveCostQueryError', error, traceback.format_exc())
                except Exception:
                    pass
        return usageUpdateDto(
            usage={key: int(event.usage[key]) for key in usageStore.tokenKeys},
            stepUsage={key: int(event.stepUsage.get(key, 0) or 0) for key in usageStore.tokenKeys},
            contextTokens=int(event.contextTokens),
            cost=liveCost,
        )

    def _pump(self) -> None:
        try:
            for event in self.stream:
                if self.stopFlag.is_set() or self.doneEvent.is_set():
                    break
                if isinstance(event, usageUpdateEvent):
                    event = self._toUsageUpdateDto(event)
                if self.stopFlag.is_set() or self.doneEvent.is_set():
                    break
                isTerminal = isinstance(event, terminalEventTypes)
                if isTerminal and not self.claimTerminal():
                    break
                self._broadcast(event)
                if isTerminal:
                    break
        except Exception as error:
            try:
                stack = traceback.format_exc()
                traceback.print_exc()
                self._logDiagEvent('pumpError', error, stack)
            except Exception:
                pass
            if self.claimTerminal():
                self._broadcast(errorEvent(message=str(error), errorType=type(error).__name__))
        finally:
            try:
                try:
                    self.stream.close()
                except Exception as error:
                    try:
                        self._logDiagEvent('streamCloseError', error, traceback.format_exc())
                    except Exception:
                        pass
                finally:
                    try:
                        self._recordUsage()
                    except Exception as error:
                        try:
                            self._logDiagEvent('usageRecordError', error, traceback.format_exc())
                        except Exception:
                            pass
            finally:
                if self.stopFlag.is_set() or self.stopClaimed:
                    self.stopDispatchDone.wait()
                if not self.doneEvent.is_set():
                    self._closeSubscribers()
                finishStream(self.sessionId, self)

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
        # 锁内只认领，锁外 wait/I/O/set；requestStop 与泵 finally 竞争 at-most-once。
        # 持久化异常只记诊断，不得阻断 unregister/seal。
        with self.usageRecordLock:
            if self.usageRecorded:
                isOwner = False
            else:
                self.usageRecorded = True
                isOwner = True

        if not isOwner:
            self.usageRecordDone.wait()
            return

        try:
            try:
                with self.agent.sessionLocksGuard:
                    currentConversation = self.agent.conversations.get(self.sessionId)
                if currentConversation is None:
                    return
                finalUsage = {
                    key: int(currentConversation.usageTotal.get(key, 0) or 0)
                    for key in self.startUsage
                }
                delta = {key: finalUsage[key] - self.startUsage[key] for key in finalUsage}
                sessionStore.updateUsage(
                    self.sessionId,
                    finalUsage,
                    contextTokens=int(currentConversation.lastTurnTokens or 0),
                    lastUsage=delta,
                )
            except Exception as error:
                try:
                    self._logDiagEvent('usageRecordError', error, traceback.format_exc())
                except Exception:
                    pass
        finally:
            self.usageRecordDone.set()
