'''
Author: wilbur
Version: 1.7
Date: 2026-09-01
Description: Pure-library Agent assembly factory. v1.7 dispatches openai-completions to the unchanged static-key adapter and ChatGPT/xAI Responses APIs to the dynamic auth Responses adapter.
'''

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flamingoAgents.core.agent import agent
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
from flamingoAgents.models.modelAuth import createModelAuth, modelAuthResolver
from flamingoAgents.models.modelConfig import loadModelConfig
from flamingoAgents.models.responsesAdapter import responsesAdapter
from flamingoAgents.skills import defaultSkillsDir, formatSkillsXml, loadSkills
from flamingoAgents.tools.builtinTools import createBuiltinTools
from flamingoAgents.tools.toolConfig import loadToolSettings
from flamingoAgents.utils.debug import debugConsole
from flamingoAgents.utils.logPaths import ensureSessionLogDir


defaultSystemPromptPath = Path(__file__).resolve().parents[1] / 'config' / 'systemPrompt.md'


def createAgent(
    workDir: str | Path,
    *,
    debug: bool = False,
    logDir: str | Path | None = None,
    modelConfigPath: str | Path | None = None,
    toolsConfigPath: str | Path | None = None,
    systemPromptPath: str | Path | None = None,
    systemPrompt: str | None = None,
    appendCurrentTime: bool = True,
    toolNames: list[str] | None = None,
    providerId: str = '101',
    modelId: str | None = None,
    skillsDir: str | Path | None = None,
) -> agent:
    workDirPath = Path(workDir).resolve()
    printer = debugConsole(debug)
    resolvedLogDir = Path(logDir).resolve() if logDir else ensureSessionLogDir('cliData', workDirPath)
    if printer.isDebug:
        printer.debug(f'装配 Agent workDir={workDirPath} logDir={resolvedLogDir} providerId={providerId} modelId={modelId}')
    resolved = loadModelConfig(
        providerId=providerId,
        modelId=modelId,
        configPath=modelConfigPath,
        debugConsole=printer,
    )
    if resolved.config.apiType == 'openai-completions':
        auth = createModelAuth(resolved.apiKey or '')
        adapter = chatCompletionsAdapter(resolved.config, auth, debugConsole=printer)
    else:
        adapter = responsesAdapter(
            resolved.config,
            modelAuthResolver(resolved),
            debugConsole=printer,
        )
    settings = loadToolSettings(configPath=toolsConfigPath, debugConsole=printer)
    definitions = createBuiltinTools(settings.toolSchemas, debugConsole=printer)
    if toolNames is not None:
        availableNames = [definition.name for definition in definitions]
        unknownNames = [name for name in toolNames if name not in availableNames]
        if unknownNames:
            raise RuntimeError(
                f'toolNames 包含未知内置工具：{",".join(unknownNames)}。'
                f'可用工具：{",".join(availableNames)}'
            )
        definitions = [definition for definition in definitions if definition.name in toolNames]
        if printer.isDebug:
            printer.debug(f'内置工具白名单生效 tools={",".join(d.name for d in definitions) or "<empty>"}')
    if systemPrompt is not None and systemPrompt.strip():
        if systemPromptPath is not None and printer.isDebug:
            printer.debug('systemPrompt 直传生效，忽略 systemPromptPath。')
        systemPromptText = systemPrompt
    else:
        resolvedSystemPromptPath = Path(systemPromptPath).resolve() if systemPromptPath else defaultSystemPromptPath
        if printer.isDebug:
            printer.debug(f'加载系统提示词 path={resolvedSystemPromptPath}')
        if not resolvedSystemPromptPath.exists():
            raise RuntimeError(f'系统提示词文件不存在：{resolvedSystemPromptPath}')
        systemPromptText = resolvedSystemPromptPath.read_text(encoding='utf-8')
    if skillsDir == '':
        skills = []
    elif skillsDir is None:
        skills = loadSkills(defaultSkillsDir, debugConsole=printer)
    else:
        skills = loadSkills(Path(skillsDir), debugConsole=printer)
    skillsBlock = formatSkillsXml(skills)
    if skillsBlock:
        systemPromptText = systemPromptText.rstrip() + '\n' + skillsBlock
    if appendCurrentTime:
        currentTimeText = datetime.now().astimezone().isoformat(timespec='seconds')
        systemPromptText = (
            systemPromptText.rstrip()
            + f'\n\n## 当前时间\n\n当前日期为：{currentTimeText}。\n'
        )
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
