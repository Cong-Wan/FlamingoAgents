'''
Author: wilbur
Version: 1.0
Date: 2026-07-08
Description: Provides a unique-name registry for callable tool definitions.
'''

from __future__ import annotations

from flamingoAgents.tools.toolDefinition import toolDefinition


class toolRegistry:
    def __init__(self, definitions: list[toolDefinition], debugConsole=None):
        self.definitions: dict[str, toolDefinition] = {}
        self.debugConsole = debugConsole
        for definition in definitions:
            self.register(definition)
        if self.debugConsole:
            self.debugConsole.debug(f'工具 registry 初始化完成 count={len(self.definitions)}')

    def register(self, definition: toolDefinition) -> None:
        if not definition.name.strip():
            raise RuntimeError('工具名称不能为空。')
        if definition.name in self.definitions:
            raise RuntimeError(f'工具名称重复：{definition.name}')
        self.definitions[definition.name] = definition
        if self.debugConsole:
            self.debugConsole.debug(f'注册工具 tool={definition.name}')

    def get(self, name: str) -> toolDefinition | None:
        return self.definitions.get(name)

    def list(self) -> list[toolDefinition]:
        return list(self.definitions.values())
