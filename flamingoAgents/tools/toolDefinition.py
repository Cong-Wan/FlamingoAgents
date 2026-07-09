'''
Author: wilbur
Version: 1.0
Date: 2026-07-08
Description: Defines callable tool metadata, permission rule types, execution signatures, and a lightweight defineTool helper.
'''

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Pattern

from flamingoAgents.core.types import toolContext, toolOutput

permissionAction = Literal['requireApproval']


@dataclass
class permissionRule:
    id: str
    field: str
    action: permissionAction
    reason: str
    patterns: list[Pattern[str]]


toolExecuteFunction = Callable[[dict[str, Any], toolContext], toolOutput]
toolPrepareFunction = Callable[[dict[str, Any]], dict[str, Any]]
toolPreviewFunction = Callable[[dict[str, Any]], str]


@dataclass
class toolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: toolExecuteFunction
    permissions: list[permissionRule] = field(default_factory=list)
    prepareArguments: toolPrepareFunction | None = None
    preview: toolPreviewFunction | None = None


def defineTool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    execute: toolExecuteFunction,
    permissions: list[permissionRule] | None = None,
    prepareArguments: toolPrepareFunction | None = None,
    preview: toolPreviewFunction | None = None,
) -> toolDefinition:
    return toolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        execute=execute,
        permissions=list(permissions or []),
        prepareArguments=prepareArguments,
        preview=preview,
    )
