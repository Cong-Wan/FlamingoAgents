'''
Author: wilbur
Version: 1.1
Date: 2026-07-01
Description: Registers local tools and exposes OpenAI-compatible tool schemas.
'''

from __future__ import annotations

from flamingoAgents.core.types import toolSpec
from flamingoAgents.tools.bash import executeBash
from flamingoAgents.tools.file import executeEdit, executeRead, executeWrite


class registry:
    def __init__(self):
        self.tools: dict[str, toolSpec] = {}

    def register(self, definition: toolSpec) -> None:
        self.tools[definition.name] = definition

    def get(self, name: str) -> toolSpec | None:
        return self.tools.get(name)

    def listDefinitions(self) -> list[toolSpec]:
        return list(self.tools.values())

    def listModelTools(self) -> list[dict]:
        modelTools = []
        for definition in self.listDefinitions():
            modelTools.append({
                'type': 'function',
                'function': {
                    'name': definition.name,
                    'description': definition.description,
                    'parameters': definition.parameters,
                },
            })
        return modelTools


def createDefaultRegistry() -> registry:
    catalog = registry()
    catalog.register(toolSpec(
        name='read',
        description='读取本地文本文件，可通过 offset 和 limit 控制读取的行范围。',
        parameters={
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'offset': {'type': 'integer', 'minimum': 1, 'default': 1},
                'limit': {'type': 'integer', 'minimum': 1, 'default': 200},
            },
            'required': ['path'],
            'additionalProperties': False,
        },
        execute=executeRead,
    ))
    catalog.register(toolSpec(
        name='write',
        description='创建或完整覆盖本地文本文件。',
        parameters={
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'content': {'type': 'string'},
            },
            'required': ['path', 'content'],
            'additionalProperties': False,
        },
        execute=executeWrite,
    ))
    catalog.register(toolSpec(
        name='edit',
        description='对已有文本文件进行精确文本替换。每个 oldText 必须唯一匹配。',
        parameters={
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'edits': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'oldText': {'type': 'string'},
                            'newText': {'type': 'string'},
                        },
                        'required': ['oldText', 'newText'],
                        'additionalProperties': False,
                    },
                    'minItems': 1,
                },
            },
            'required': ['path', 'edits'],
            'additionalProperties': False,
        },
        execute=executeEdit,
    ))
    catalog.register(toolSpec(
        name='bash',
        description='在工作目录中执行原生 bash 命令。curl、python、grep、open 均通过此工具执行。',
        parameters={
            'type': 'object',
            'properties': {
                'command': {'type': 'string'},
                'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 120, 'default': 30},
            },
            'required': ['command'],
            'additionalProperties': False,
        },
        execute=executeBash,
    ))
    return catalog
