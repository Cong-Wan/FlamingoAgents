'''
Author: wilbur
Version: 1.6
Date: 2026-09-08
Description: Loads model configuration without mutating process environment. v1.4 adds ChatGPT Codex/xAI Responses APIs, api-key/oauth auth types, canonical auth providers, optional OAuth apiKey, reasoning metadata, and xAI API key fallback while preserving old openai-completions defaults. v1.5（imageInputPlan §3.1）新增 inputTypes 透传与派生 supportsImageInput；缺省 ['text']，YAML 显式 input 时校验列表与枚举。v1.6（configHomePlan P2）：默认路径切到 ~/.flamingo/config/models.yaml；文件缺失时报无可用模型（含模板指引），删除环境变量回退（loadModelConfigFromEnv，方案 D3）。
'''

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from flamingoAgents.utils.configPaths import modelsExamplePath, userModelsPath

allowedApis = frozenset({'openai-completions', 'openai-responses', 'openai-codex-responses'})
allowedAuthTypes = frozenset({'api-key', 'oauth'})


@dataclass
class modelConfig:
    provider: str
    model: str
    baseUrl: str
    apiType: str
    supportsToolCalling: bool = True
    thinking: dict[str, Any] | None = None
    reasoningEffort: str | None = None
    stream: bool = True
    headers: dict[str, str] | None = None
    authType: str = 'api-key'
    configProviderId: str | None = None
    authProvider: str | None = None
    reasoning: bool = False
    inputTypes: list[str] = field(default_factory=lambda: ['text'])

    def __post_init__(self) -> None:
        if self.configProviderId is None:
            self.configProviderId = self.provider

    @property
    def supportsImageInput(self) -> bool:
        return 'image' in (self.inputTypes or [])


@dataclass
class resolvedModelConfig:
    config: modelConfig
    apiKey: str | None


defaultModelConfigPath = userModelsPath


def loadModelConfig(
    providerId: str = '101',
    modelId: str | None = None,
    configPath: str | Path | None = None,
    debugConsole=None,
) -> resolvedModelConfig:
    path = Path(configPath) if configPath is not None else defaultModelConfigPath
    if not path.exists():
        # configHomePlan D3：未配置 models 直接报无可用模型，不静默回退、不自动拷模板。
        raise RuntimeError(
            f'无可用模型：{path} 不存在。请参照模板 {modelsExamplePath} 手动创建，'
            f'或在 Web 设置页配置后保存。'
        )
    return loadModelConfigFromYaml(providerId=providerId, modelId=modelId, configPath=path, debugConsole=debugConsole)


def loadModelConfigFromYaml(
    providerId: str = '101',
    modelId: str | None = None,
    configPath: str | Path | None = None,
    debugConsole=None,
) -> resolvedModelConfig:
    path = Path(configPath) if configPath is not None else defaultModelConfigPath
    if not path.exists():
        raise RuntimeError(f'模型配置文件不存在：{path}')

    with path.open('r', encoding='utf-8') as configFile:
        rawConfig = yaml.safe_load(configFile) or {}
    if not isinstance(rawConfig, dict):
        raise RuntimeError('模型配置文件必须是 YAML 对象。')

    providers = rawConfig.get('providers')
    if not isinstance(providers, dict):
        raise RuntimeError('模型配置缺少 providers 对象。')

    providerConfig = providers.get(providerId)
    if not isinstance(providerConfig, dict):
        raise RuntimeError(f'模型配置缺少 provider：{providerId}')

    baseUrl = providerConfig.get('baseUrl')
    if not isinstance(baseUrl, str) or not baseUrl.strip():
        raise RuntimeError(f'provider {providerId} 缺少 baseUrl。')
    baseUrl = baseUrl.strip()

    models = providerConfig.get('models')
    if not isinstance(models, list) or not models:
        raise RuntimeError(f'provider {providerId} 缺少 models。')

    selectedModel = selectModel(models, modelId, providerId)
    selectedModelId = selectedModel.get('id')
    if not isinstance(selectedModelId, str) or not selectedModelId.strip():
        raise RuntimeError(f'provider {providerId} 的模型缺少 id。')
    selectedModelId = selectedModelId.strip()

    thinking = selectedModel.get('thinking')
    if thinking is not None and not isinstance(thinking, dict):
        raise RuntimeError(f'provider {providerId} 模型 {selectedModelId} 的 thinking 必须是对象。')

    reasoningEffort = selectedModel.get('reasoningEffort')
    if reasoningEffort is not None and not isinstance(reasoningEffort, str):
        raise RuntimeError(f'provider {providerId} 模型 {selectedModelId} 的 reasoningEffort 必须是字符串。')

    reasoning = selectedModel.get('reasoning', False)
    if not isinstance(reasoning, bool):
        raise RuntimeError(f'provider {providerId} 模型 {selectedModelId} 的 reasoning 必须是布尔值。')

    streamValue = selectedModel.get('stream', providerConfig.get('stream'))
    if streamValue is not None and not isinstance(streamValue, bool):
        raise RuntimeError(f'provider {providerId} 模型 {selectedModelId} 的 stream 必须是布尔值。')

    inputTypes = selectedModel.get('input')
    if inputTypes is not None:
        if not isinstance(inputTypes, list) or not all(isinstance(item, str) for item in inputTypes):
            raise RuntimeError(f'provider {providerId} 模型 {selectedModelId} 的 input 必须是字符串数组。')
        invalidInputs = [item for item in inputTypes if item not in ('text', 'image')]
        if invalidInputs:
            raise RuntimeError(f'provider {providerId} 模型 {selectedModelId} 的 input 仅允许 text/image：{invalidInputs}')

    headers = parseHeaders(providerConfig.get('headers'), selectedModel.get('headers'), providerId, selectedModelId)

    api = selectedModel.get('api') or providerConfig.get('api')
    if api not in allowedApis:
        raise RuntimeError(f'provider {providerId} 的 api 不受支持：{api}')
    authType = providerConfig.get('auth', 'api-key')
    if authType not in allowedAuthTypes:
        raise RuntimeError(f'provider {providerId} 的 auth 仅允许 api-key/oauth，实际为：{authType}')
    authProvider = resolveAuthProvider(api)
    validateApiAuth(providerId, api, authType, baseUrl)

    apiKey = None
    if authType == 'api-key':
        rawApiKey = providerConfig.get('apiKey')
        if isinstance(rawApiKey, str) and rawApiKey.strip():
            apiKey = resolveApiKey(rawApiKey.strip(), providerId)
        elif api == 'openai-responses' and authProvider == 'xai':
            apiKey = os.getenv('XAI_API_KEY', '').strip()
            if not apiKey:
                raise RuntimeError(f'provider {providerId} 缺少 apiKey 或环境变量 XAI_API_KEY。')
        else:
            raise RuntimeError(f'provider {providerId} 缺少 apiKey。')

    if debugConsole:
        debugConsole.debug(
            f'从 YAML 加载模型配置 provider={providerId} model={selectedModelId} '
            f'baseUrl={baseUrl} api={api} auth={authType} '
            f'thinking={thinking} reasoningEffort={reasoningEffort} stream={streamValue} input={inputTypes}'
        )
    return resolvedModelConfig(
        config=modelConfig(
            provider=providerId,
            configProviderId=providerId,
            model=selectedModelId,
            baseUrl=baseUrl,
            apiType=api,
            authType=authType,
            authProvider=authProvider,
            supportsToolCalling=True,
            thinking=thinking,
            reasoningEffort=reasoningEffort,
            reasoning=reasoning,
            stream=streamValue if streamValue is not None else True,
            headers=headers,
            inputTypes=list(inputTypes) if isinstance(inputTypes, list) else ['text'],
        ),
        apiKey=apiKey,
    )


def resolveAuthProvider(apiType: str) -> str | None:
    if apiType == 'openai-codex-responses':
        return 'openai-codex'
    if apiType == 'openai-responses':
        return 'xai'
    return None


def validateApiAuth(providerId: str, apiType: str, authType: str, baseUrl: str) -> None:
    if apiType == 'openai-completions' and authType != 'api-key':
        raise RuntimeError(f'provider {providerId} 的 openai-completions 仅支持 auth=api-key。')
    if apiType == 'openai-codex-responses':
        if authType != 'oauth':
            raise RuntimeError(f'provider {providerId} 的 openai-codex-responses 仅支持 auth=oauth。')
        if not hasExpectedHost(baseUrl, 'chatgpt.com'):
            raise RuntimeError(f'provider {providerId} 的 Codex OAuth baseUrl 必须使用 chatgpt.com。')
    if apiType == 'openai-responses' and authType == 'oauth' and not hasExpectedHost(baseUrl, 'api.x.ai'):
        raise RuntimeError(f'provider {providerId} 的 Responses OAuth baseUrl 必须使用 api.x.ai。')


def hasExpectedHost(baseUrl: str, expectedHost: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(baseUrl)
    except ValueError:
        return False
    return parsed.scheme == 'https' and parsed.hostname == expectedHost


def parseHeaders(providerHeaders, modelHeaders, providerId: str, modelId: str) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for source, location in (
        (providerHeaders, f'provider {providerId}'),
        (modelHeaders, f'provider {providerId} 模型 {modelId}'),
    ):
        if source is None:
            continue
        if not isinstance(source, dict):
            raise RuntimeError(f'{location} 的 headers 必须是对象（键值均为字符串）。')
        for key, value in source.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise RuntimeError(f'{location} 的 headers 键值必须都是字符串。')
            merged[key] = value
    return merged or None


def selectModel(models: list[Any], modelId: str | None, providerId: str) -> dict[str, Any]:
    if modelId is None:
        firstModel = models[0]
        if isinstance(firstModel, dict):
            return firstModel
    else:
        for modelItem in models:
            if isinstance(modelItem, dict) and modelItem.get('id') == modelId:
                return modelItem
    raise RuntimeError(f'provider {providerId} 缺少可用模型：{modelId or "<first>"}')


def resolveApiKey(rawApiKey: str, providerId: str) -> str:
    if rawApiKey.startswith('${') and rawApiKey.endswith('}'):
        envName = rawApiKey[2:-1].strip()
        if not envName:
            raise RuntimeError(f'provider {providerId} 的 apiKey 环境变量名为空。')
        value = os.getenv(envName, '').strip()
        if not value:
            raise RuntimeError(f'模型配置缺失：{envName}')
        return value
    if rawApiKey.startswith('$'):
        envName = rawApiKey[1:].strip()
        if not envName:
            raise RuntimeError(f'provider {providerId} 的 apiKey 环境变量名为空。')
        value = os.getenv(envName, '').strip()
        if not value:
            raise RuntimeError(f'模型配置缺失：{envName}')
        return value
    return rawApiKey
