'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Converts internal config-driven tool definitions into model function-call schemas.
'''

from __future__ import annotations

from typing import Any

from flamingoAgents.tools.toolConfig import toolDefinition


def buildModelTool(definition: toolDefinition) -> dict[str, Any]:
    return {
        'type': 'function',
        'function': {
            'name': definition.name,
            'description': definition.description,
            'parameters': definition.parameters,
        },
    }


def buildModelTools(definitions: list[toolDefinition]) -> list[dict[str, Any]]:
    return [buildModelTool(definition) for definition in definitions]
