'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Enforces config-driven tool permission rules before runtime execution.
'''

from __future__ import annotations

from dataclasses import dataclass

from flamingoAgents.core.types import toolCall
from flamingoAgents.tools.toolConfig import toolDefinition


@dataclass
class policyDecision:
    requiresApproval: bool
    reason: str = ''
    permissionId: str = ''


def evaluateToolCall(definition: toolDefinition, call: toolCall, debugConsole=None) -> policyDecision:
    if debugConsole:
        debugConsole.debug(f'评估工具权限 tool={definition.name} callId={call.id} permissionCount={len(definition.permissions)}')
    for rule in definition.permissions:
        fieldValue = call.arguments.get(rule.field, '') if isinstance(call.arguments, dict) else ''
        textValue = str(fieldValue)
        for pattern in rule.patterns:
            if pattern.search(textValue):
                if debugConsole:
                    debugConsole.debug(f'工具权限命中 tool={definition.name} callId={call.id} permissionId={rule.id}')
                return policyDecision(
                    requiresApproval=True,
                    reason=rule.reason,
                    permissionId=rule.id,
                )
    return policyDecision(requiresApproval=False)
