'''
Author: wilbur
Version: 1.2
Date: 2026-07-09
Description: Pure-library assembly factory: resolves paths, loads model config/auth, system prompt, and schema-driven tools, then returns a ready-to-use agent.
'''

from __future__ import annotations

from pathlib import Path

from flamingoAgents.core.agent import agent
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
from flamingoAgents.models.modelAuth import createModelAuth
from flamingoAgents.models.modelConfig import loadModelConfig
from flamingoAgents.tools.builtinTools import createBuiltinTools
from flamingoAgents.tools.toolConfig import loadToolSettings
from flamingoAgents.utils.debug import debugConsole


defaultSystemPromptPath = Path(__file__).resolve().parents[1] / 'config' / 'systemPrompt.md'


def createAgent(
    workDir: str | Path,
    *,
    debug: bool = False,
    logDir: str | Path | None = None,
    modelConfigPath: str | Path | None = None,
    toolsConfigPath: str | Path | None = None,
    systemPromptPath: str | Path | None = None,
    providerId: str = '101',
    modelId: str | None = None,
) -> agent:
    workDirPath = Path(workDir).resolve()
    printer = debugConsole(debug)
    resolvedLogDir = Path(logDir).resolve() if logDir else workDirPath / '.agentLogs'
    if printer.isDebug:
        printer.debug(f'装配 Agent workDir={workDirPath} logDir={resolvedLogDir} providerId={providerId} modelId={modelId}')
    resolved = loadModelConfig(
        providerId=providerId,
        modelId=modelId,
        configPath=modelConfigPath,
        debugConsole=printer,
    )
    auth = createModelAuth(resolved.apiKey)
    adapter = chatCompletionsAdapter(resolved.config, auth, debugConsole=printer)
    settings = loadToolSettings(configPath=toolsConfigPath, debugConsole=printer)
    definitions = createBuiltinTools(settings.toolSchemas, debugConsole=printer)
    resolvedSystemPromptPath = Path(systemPromptPath).resolve() if systemPromptPath else defaultSystemPromptPath
    if printer.isDebug:
        printer.debug(f'加载系统提示词 path={resolvedSystemPromptPath}')
    if not resolvedSystemPromptPath.exists():
        raise RuntimeError(f'系统提示词文件不存在：{resolvedSystemPromptPath}')
    systemPromptText = resolvedSystemPromptPath.read_text(encoding='utf-8')
    if printer.isDebug:
        printer.debug(f'系统提示词加载完成 chars={len(systemPromptText)}')
    return agent(
        modelAdapter=adapter,
        toolDefinitions=definitions,
        workDir=workDirPath,
        logDir=resolvedLogDir,
        systemPrompt=systemPromptText,
        debugConsole=printer,
    )
