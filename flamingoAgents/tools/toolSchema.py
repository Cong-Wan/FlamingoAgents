'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Converts callable tool definitions into model function-call schemas.
'''

from __future__ import annotations

from typing import Any

from flamingoAgents.tools.toolDefinition import toolDefinition


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
