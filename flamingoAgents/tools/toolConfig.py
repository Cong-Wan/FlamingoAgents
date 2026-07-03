'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Loads config-driven tool definitions and compiles runtime permission rules.
'''

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Pattern

import yaml

permissionAction = Literal['requireApproval']


@dataclass
class permissionRule:
    id: str
    field: str
    action: permissionAction
    reason: str
    patterns: list[Pattern[str]]


@dataclass
class toolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    runtime: dict[str, Any]
    permissions: list[permissionRule]


defaultToolsConfigPath = Path(__file__).resolve().parents[2] / 'config' / 'tools.yaml'


def loadToolConfig(configPath: str | Path | None = None, debugConsole=None) -> list[toolDefinition]:
    path = Path(configPath) if configPath is not None else defaultToolsConfigPath
    if debugConsole:
        debugConsole.debug(f'加载工具配置 path={path}')
    if not path.exists():
        raise RuntimeError(f'工具配置文件不存在：{path}')
    with path.open('r', encoding='utf-8') as configFile:
        rawConfig = yaml.safe_load(configFile) or {}
    return parseToolConfig(rawConfig, source=str(path), debugConsole=debugConsole)


def parseToolConfig(rawConfig: Any, source: str = '<memory>', debugConsole=None) -> list[toolDefinition]:
    if not isinstance(rawConfig, dict):
        raise RuntimeError(f'工具配置必须是 YAML 对象：{source}')
    version = rawConfig.get('version')
    if version != 1:
        raise RuntimeError(f'工具配置 version 必须是 1，实际为：{version}')
    rawTools = rawConfig.get('tools')
    if not isinstance(rawTools, list) or not rawTools:
        raise RuntimeError('工具配置 tools 必须是非空数组。')

    seenNames: set[str] = set()
    definitions: list[toolDefinition] = []
    for index, rawTool in enumerate(rawTools):
        definition = parseTool(index, rawTool)
        if definition.name in seenNames:
            raise RuntimeError(f'工具名称重复：{definition.name}')
        seenNames.add(definition.name)
        definitions.append(definition)

    if debugConsole:
        debugConsole.debug(f'工具配置加载完成 count={len(definitions)} names={",".join(sorted(seenNames))}')
    return definitions


def parseTool(index: int, rawTool: Any) -> toolDefinition:
    if not isinstance(rawTool, dict):
        raise RuntimeError(f'第 {index + 1} 个工具必须是对象。')

    name = readRequiredString(rawTool, 'name', f'第 {index + 1} 个工具')
    description = readRequiredString(rawTool, 'description', f'工具 {name}')
    permissionSummary = rawTool.get('modelPermissionSummary')
    if isinstance(permissionSummary, str) and permissionSummary.strip():
        description = f'{description}\n\n权限提示：{permissionSummary.strip()}'

    parameters = rawTool.get('parameters')
    if not isinstance(parameters, dict) or parameters.get('type') != 'object':
        raise RuntimeError(f'工具 {name} 的 parameters 必须是 type=object 的对象。')

    runtime = rawTool.get('runtime')
    if not isinstance(runtime, dict):
        raise RuntimeError(f'工具 {name} 缺少 runtime 对象。')
    runtimeType = runtime.get('type')
    if runtimeType not in {'file', 'shell'}:
        raise RuntimeError(f'工具 {name} 的 runtime.type 不支持：{runtimeType}')
    if runtimeType == 'file' and runtime.get('operation') not in {'read', 'write', 'edit'}:
        raise RuntimeError(f'工具 {name} 的 file operation 不支持：{runtime.get("operation")}')

    permissions = parsePermissions(name, rawTool.get('permissions', []))
    return toolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        runtime=runtime,
        permissions=permissions,
    )


def parsePermissions(toolName: str, rawPermissions: Any) -> list[permissionRule]:
    if rawPermissions is None:
        return []
    if not isinstance(rawPermissions, list):
        raise RuntimeError(f'工具 {toolName} 的 permissions 必须是数组。')
    parsedRules: list[permissionRule] = []
    for index, rawRule in enumerate(rawPermissions):
        if not isinstance(rawRule, dict):
            raise RuntimeError(f'工具 {toolName} 的第 {index + 1} 条 permission 必须是对象。')
        ruleId = readRequiredString(rawRule, 'id', f'工具 {toolName} permission {index + 1}')
        field = readRequiredString(rawRule, 'field', f'工具 {toolName} permission {ruleId}')
        action = readRequiredString(rawRule, 'action', f'工具 {toolName} permission {ruleId}')
        if action != 'requireApproval':
            raise RuntimeError(f'工具 {toolName} permission {ruleId} action 不支持：{action}')
        reason = readRequiredString(rawRule, 'reason', f'工具 {toolName} permission {ruleId}')
        rawMatch = rawRule.get('match')
        if not isinstance(rawMatch, dict) or rawMatch.get('type') != 'regex':
            raise RuntimeError(f'工具 {toolName} permission {ruleId} 只支持 match.type=regex。')
        rawPatterns = rawMatch.get('patterns')
        if not isinstance(rawPatterns, list) or not rawPatterns:
            raise RuntimeError(f'工具 {toolName} permission {ruleId} 缺少 regex patterns。')
        patterns: list[Pattern[str]] = []
        for patternIndex, patternText in enumerate(rawPatterns):
            if not isinstance(patternText, str) or not patternText:
                raise RuntimeError(f'工具 {toolName} permission {ruleId} 第 {patternIndex + 1} 个 regex 必须是非空字符串。')
            try:
                patterns.append(re.compile(patternText, re.IGNORECASE))
            except re.error as error:
                raise RuntimeError(f'工具 {toolName} permission {ruleId} regex 无法编译：{patternText}') from error
        parsedRules.append(permissionRule(
            id=ruleId,
            field=field,
            action='requireApproval',
            reason=reason,
            patterns=patterns,
        ))
    return parsedRules


def readRequiredString(rawData: dict[str, Any], key: str, label: str) -> str:
    value = rawData.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f'{label} 缺少非空字符串字段：{key}')
    return value.strip()
