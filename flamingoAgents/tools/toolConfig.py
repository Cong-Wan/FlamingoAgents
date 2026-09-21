'''
Author: wilbur
Version: 1.4
Date: 2026-09-21
Description: Loads tool schemas and permission rules from YAML. v1.4 adds tools.yaml v4 parallelToolPool (opt-in allowlist + per-batch maxWorkers) while keeping v3 serial-compatible.
'''

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Pattern

import yaml

from flamingoAgents.tools.toolDefinition import permissionRule
from flamingoAgents.utils.configPaths import userToolsPath


@dataclass
class toolSchemaSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    permissions: list[permissionRule]


@dataclass(frozen=True)
class parallelToolPoolSettings:
    maxWorkers: int = 1
    toolNames: frozenset[str] = frozenset()


@dataclass
class toolSettings:
    toolSchemas: list[toolSchemaSpec]
    parallelToolPool: parallelToolPoolSettings = field(default_factory=parallelToolPoolSettings)


defaultToolsConfigPath = userToolsPath


def loadToolSettings(configPath: str | Path | None = None, debugConsole=None) -> toolSettings:
    path = Path(configPath) if configPath is not None else defaultToolsConfigPath
    if debugConsole:
        debugConsole.debug(f'加载工具设置 path={path}')
    if not path.exists():
        raise RuntimeError(f'工具配置文件不存在：{path}')
    with path.open('r', encoding='utf-8') as configFile:
        rawConfig = yaml.safe_load(configFile) or {}
    return parseToolSettings(rawConfig, source=str(path), debugConsole=debugConsole)


def parseToolSettings(rawConfig: Any, source: str = '<memory>', debugConsole=None) -> toolSettings:
    if not isinstance(rawConfig, dict):
        raise RuntimeError(f'工具配置必须是 YAML 对象：{source}')
    version = rawConfig.get('version')
    if version == 3:
        if 'parallelToolPool' in rawConfig:
            raise RuntimeError('工具配置 version 3 不支持 parallelToolPool，请升级到 version 4。')
        toolSchemas = parseToolSchemaList(rawConfig, source, debugConsole=debugConsole)
        return toolSettings(toolSchemas=toolSchemas)
    if version == 4:
        toolSchemas = parseToolSchemaList(rawConfig, source, debugConsole=debugConsole)
        pool = parseParallelToolPool(
            rawConfig.get('parallelToolPool'),
            {schema.name for schema in toolSchemas},
            source,
        )
        return toolSettings(toolSchemas=toolSchemas, parallelToolPool=pool)
    raise RuntimeError(f'工具配置 version 必须是 3 或 4，实际为：{version}')


def parseToolSchemaList(rawConfig: dict[str, Any], source: str, debugConsole=None) -> list[toolSchemaSpec]:
    rawTools = rawConfig.get('tools')
    if not isinstance(rawTools, list) or not rawTools:
        raise RuntimeError('工具配置 tools 必须是非空数组。')

    toolSchemas: list[toolSchemaSpec] = []
    seenNames: set[str] = set()
    for index, rawTool in enumerate(rawTools):
        if not isinstance(rawTool, dict):
            raise RuntimeError(f'tools 第 {index + 1} 项必须是对象。')
        schema = parseToolSchema(rawTool, source, index + 1, debugConsole=debugConsole)
        if schema.name in seenNames:
            raise RuntimeError(f'工具名称重复：{schema.name}')
        seenNames.add(schema.name)
        toolSchemas.append(schema)
        if debugConsole:
            debugConsole.debug(
                f'解析工具 schema tool={schema.name} '
                f'paramKeys={",".join((schema.parameters.get("properties") or {}).keys())} '
                f'permissionCount={len(schema.permissions)}'
            )

    if debugConsole:
        debugConsole.debug(
            f'工具设置加载完成 toolCount={len(toolSchemas)} '
            f'tools={",".join(s.name for s in toolSchemas)}'
        )
    return toolSchemas


def parseParallelToolPool(rawPool: Any, knownNames: set[str], source: str) -> parallelToolPoolSettings:
    if rawPool is None:
        return parallelToolPoolSettings()
    if not isinstance(rawPool, dict):
        raise RuntimeError(f'{source} parallelToolPool 必须是对象。')
    allowedKeys = {'maxWorkers', 'toolNames'}
    extraKeys = set(rawPool) - allowedKeys
    missingKeys = allowedKeys - set(rawPool)
    if extraKeys or missingKeys:
        parts = []
        if missingKeys:
            parts.append('缺少字段：' + ','.join(sorted(missingKeys)))
        if extraKeys:
            parts.append('含未知字段：' + ','.join(sorted(extraKeys)))
        raise RuntimeError(f'{source} parallelToolPool ' + '；'.join(parts) + '。')

    maxWorkers = rawPool.get('maxWorkers')
    if isinstance(maxWorkers, bool) or not isinstance(maxWorkers, int) or maxWorkers < 1 or maxWorkers > 32:
        raise RuntimeError(f'{source} parallelToolPool.maxWorkers 必须是整数 1..32。')

    rawNames = rawPool.get('toolNames')
    if not isinstance(rawNames, list):
        raise RuntimeError(f'{source} parallelToolPool.toolNames 必须是字符串数组。')
    toolNames: list[str] = []
    seenNames: set[str] = set()
    for index, name in enumerate(rawNames):
        if not isinstance(name, str) or not name:
            raise RuntimeError(f'{source} parallelToolPool.toolNames 第 {index + 1} 项必须是非空字符串。')
        if name in seenNames:
            raise RuntimeError(f'{source} parallelToolPool.toolNames 重复：{name}')
        if name not in knownNames:
            raise RuntimeError(f'{source} parallelToolPool.toolNames 未知工具：{name}')
        seenNames.add(name)
        toolNames.append(name)
    return parallelToolPoolSettings(maxWorkers=maxWorkers, toolNames=frozenset(toolNames))


def parseToolSchema(rawTool: dict[str, Any], source: str, position: int, debugConsole=None) -> toolSchemaSpec:
    label = f'{source} tools[{position}]'
    name = readRequiredString(rawTool, 'name', label)
    description = readRequiredString(rawTool, 'description', label)
    parameters = rawTool.get('parameters')
    if not isinstance(parameters, dict):
        raise RuntimeError(f'{label} parameters 必须是对象。')
    if parameters.get('type') != 'object':
        raise RuntimeError(f'{label} parameters.type 必须是 object。')
    permissions = parsePermissions(name, rawTool.get('permissions'), label)
    return toolSchemaSpec(
        name=name,
        description=description,
        parameters=parameters,
        permissions=permissions,
    )


def parsePermissions(toolName: str, rawPermissions: Any, label: str) -> list[permissionRule]:
    if rawPermissions is None:
        return []
    if not isinstance(rawPermissions, list):
        raise RuntimeError(f'{label} permissions 必须是数组。')
    parsedRules: list[permissionRule] = []
    for index, rawRule in enumerate(rawPermissions):
        if not isinstance(rawRule, dict):
            raise RuntimeError(f'{label} permissions 第 {index + 1} 条必须是对象。')
        ruleId = readRequiredString(rawRule, 'id', f'{label} permission {index + 1}')
        fieldName = readRequiredString(rawRule, 'field', f'{label} permission {ruleId}')
        action = readRequiredString(rawRule, 'action', f'{label} permission {ruleId}')
        if action != 'requireApproval':
            raise RuntimeError(f'{label} permission {ruleId} action 不支持：{action}')
        reason = readRequiredString(rawRule, 'reason', f'{label} permission {ruleId}')
        rawMatch = rawRule.get('match')
        if not isinstance(rawMatch, dict) or rawMatch.get('type') != 'regex':
            raise RuntimeError(f'{label} permission {ruleId} 只支持 match.type=regex。')
        rawPatterns = rawMatch.get('patterns')
        if not isinstance(rawPatterns, list) or not rawPatterns:
            raise RuntimeError(f'{label} permission {ruleId} 缺少 regex patterns。')
        patterns: list[Pattern[str]] = []
        for patternIndex, patternText in enumerate(rawPatterns):
            if not isinstance(patternText, str) or not patternText:
                raise RuntimeError(f'{label} permission {ruleId} 第 {patternIndex + 1} 个 regex 必须是非空字符串。')
            try:
                patterns.append(re.compile(patternText, re.IGNORECASE))
            except re.error as error:
                raise RuntimeError(f'{label} permission {ruleId} regex 无法编译：{patternText}') from error
        parsedRules.append(permissionRule(
            id=ruleId,
            field=fieldName,
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
