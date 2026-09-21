'''
Author: wilbur
Version: 1.5
Date: 2026-09-21
Description: 用量统计薄入口。v1.5 页面与状态栏费用改读公共 usageEvents 账本；usageTurns 仅保留兼容表，不再作为新页面数据源。
'''

from __future__ import annotations

from flamingoAgents.utils import usageLedger

tokenKeys = ('promptTokens', 'cachedTokens', 'completionTokens')


def initUsageDb() -> None:
    usageLedger.ensureReady(reconcile=True)


def querySessionCost(sessionId: str) -> float:
    return usageLedger.querySessionCostUsd(sessionId)


def queryLastUsageTurn(sessionId: str) -> dict | None:
    return usageLedger.queryLastSessionUsage(sessionId)


def queryUsage(period: str) -> dict:
    return usageLedger.queryUsageSnapshot(period)


def writeUsageTurn(sessionId: str, providerId: str, modelId: str, delta: dict) -> None:
    return None


def loadCostMap() -> dict:
    return {}


def calcTurnCost(promptTokens: int, cachedTokens: int, completionTokens: int, cost: dict) -> float:
    from decimal import Decimal
    fields = usageLedger.extractCostFields(cost)
    if fields is None:
        return 0.0
    nano, _status = usageLedger.priceSnapshot(promptTokens, cachedTokens, completionTokens, fields)
    if nano is None:
        return 0.0
    return float(Decimal(nano) / usageLedger.NANO_PER_USD)
