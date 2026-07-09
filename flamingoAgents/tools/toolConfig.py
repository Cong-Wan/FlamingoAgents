'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Loads callable tool settings and compiles runtime permission rules.
'''

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Pattern

import yaml

from flamingoAgents.tools.toolDefinition import permissionRule


@dataclass
class toolSettings:
    enabledTools: list[str]
    permissionsByTool: dict[str, list[permissionRule]] = field(default_factory=dict)


defaultToolsConfigPath = Path(__file__).resolve().parents[2] / 'config' / 'tools.yaml'


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
    if version != 2:
        raise RuntimeError(f'工具配置 version 必须是 2，实际为：{version}')

    rawEnabledTools = rawConfig.get('enabledTools')
    if not isinstance(rawEnabledTools, list) or not rawEnabledTools:
        raise RuntimeError('工具配置 enabledTools 必须是非空数组。')

    enabledTools: list[str] = []
    seenTools: set[str] = set()
    for index, rawToolName in enumerate(rawEnabledTools):
        if not isinstance(rawToolName, str) or not rawToolName.strip():
            raise RuntimeError(f'enabledTools 第 {index + 1} 项必须是非空字符串。')
        toolName = rawToolName.strip()
        if toolName in seenTools:
            raise RuntimeError(f'启用工具名称重复：{toolName}')
        seenTools.add(toolName)
        enabledTools.append(toolName)

    rawPermissionsByTool = rawConfig.get('toolPermissions', {})
    if rawPermissionsByTool is None:
        rawPermissionsByTool = {}
    if not isinstance(rawPermissionsByTool, dict):
        raise RuntimeError('工具配置 toolPermissions 必须是对象。')

    permissionsByTool: dict[str, list[permissionRule]] = {}
    for rawToolName, rawPermissions in rawPermissionsByTool.items():
        if not isinstance(rawToolName, str) or not rawToolName.strip():
            raise RuntimeError('toolPermissions 的 key 必须是非空工具名。')
        toolName = rawToolName.strip()
        if toolName not in seenTools:
            raise RuntimeError(f'工具权限配置引用了未启用工具：{toolName}')
        permissionsByTool[toolName] = parsePermissions(toolName, rawPermissions)

    for toolName in enabledTools:
        permissionsByTool.setdefault(toolName, [])

    if debugConsole:
        debugConsole.debug(
            f'工具设置加载完成 enabledTools={",".join(enabledTools)} '
            f'permissionTools={",".join(sorted(permissionsByTool.keys()))}'
        )
    return toolSettings(enabledTools=enabledTools, permissionsByTool=permissionsByTool)


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
        fieldName = readRequiredString(rawRule, 'field', f'工具 {toolName} permission {ruleId}')
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
