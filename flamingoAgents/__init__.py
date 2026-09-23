'''
Author: wilbur
Version: 1.4
Date: 2026-09-23
Description: Exposes the pure-library public API for Flamingo Agents. v1.3 exports the 7 agent event classes for event-stream consumers (docs/streamOutputPlan.md §6.2).
            v1.4（versionDisplayPlan §4.1）：packageVersion 改由 flamingoAgents/version.py 唯一数据源引用，不再硬编码。
'''

from flamingoAgents.builder import createAgent
from flamingoAgents.core.types import (
    completedEvent,
    confirmationRequiredEvent,
    errorEvent,
    reasoningDeltaEvent,
    textDeltaEvent,
    toolCallEndEvent,
    toolCallStartEvent,
)
from flamingoAgents.version import __version__ as packageVersion

__all__ = [
    'createAgent',
    'packageVersion',
    'textDeltaEvent',
    'reasoningDeltaEvent',
    'toolCallStartEvent',
    'toolCallEndEvent',
    'confirmationRequiredEvent',
    'completedEvent',
    'errorEvent',
]
