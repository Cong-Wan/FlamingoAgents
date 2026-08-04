'''
Author: wilbur
Version: 1.3
Date: 2026-07-26
Description: Exposes the pure-library public API for Flamingo Agents. v1.3 exports the 7 agent event classes for event-stream consumers (docs/streamOutputPlan.md §6.2).
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

packageVersion = '0.1.0'

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
