'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Pure-library assembly factory: resolves paths, loads model config/auth and callable tools, and returns a ready-to-use agent.
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


def createAgent(
    workDir: str | Path,
    *,
    debug: bool = False,
    logDir: str | Path | None = None,
    modelConfigPath: str | Path | None = None,
    toolsConfigPath: str | Path | None = None,
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
    definitions = createBuiltinTools(settings.enabledTools, settings.permissionsByTool, debugConsole=printer)
    return agent(
        modelAdapter=adapter,
        toolDefinitions=definitions,
        workDir=workDirPath,
        logDir=resolvedLogDir,
        debugConsole=printer,
    )
