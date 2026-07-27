'''
Author: wilbur
Version: 1.1
Date: 2026-07-10
Description: Loads model configuration and resolves API keys without mutating process environment.
'''

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class modelConfig:
    provider: str
    model: str
    baseUrl: str
    apiType: str
    supportsToolCalling: bool = True
    thinking: dict[str, Any] | None = None
    reasoningEffort: str | None = None


@dataclass
class resolvedModelConfig:
    config: modelConfig
    apiKey: str


defaultModelConfigPath = Path(__file__).resolve().parents[2] / 'config' / 'models.yaml'


def loadModelConfig(
    providerId: str = '101',
    modelId: str | None = None,
    configPath: str | Path | None = None,
    debugConsole=None,
) -> resolvedModelConfig:
    path = Path(configPath) if configPath is not None else defaultModelConfigPath
    if path.exists():
        return loadModelConfigFromYaml(providerId=providerId, modelId=modelId, configPath=path, debugConsole=debugConsole)
    return loadModelConfigFromEnv(debugConsole=debugConsole)


def loadModelConfigFromEnv(debugConsole=None) -> resolvedModelConfig:
    model = os.getenv('FLAMINGO_AGENTS_MODEL', '').strip()
    baseUrl = os.getenv('FLAMINGO_AGENTS_BASE_URL', '').strip()
    apiKey = os.getenv('FLAMINGO_AGENTS_API_KEY', '').strip()
    apiKeyEnv = os.getenv('FLAMINGO_AGENTS_API_KEY_ENV', 'OPENAI_API_KEY').strip()
    if not apiKey and apiKeyEnv:
        apiKey = os.getenv(apiKeyEnv, '').strip()

    missingFields = []
    if not model:
        missingFields.append('FLAMINGO_AGENTS_MODEL')
    if not baseUrl:
        missingFields.append('FLAMINGO_AGENTS_BASE_URL')
    if not apiKey:
        missingFields.append(apiKeyEnv or 'FLAMINGO_AGENTS_API_KEY')
    if missingFields:
        joinedFields = ', '.join(missingFields)
        raise RuntimeError(f'模型配置缺失：{joinedFields}')

    if debugConsole:
        debugConsole.debug(f'从环境变量加载模型配置 model={model} baseUrl={baseUrl}')
    return resolvedModelConfig(
        config=modelConfig(
            provider='openaiCompatible',
            model=model,
            baseUrl=baseUrl,
            apiType='openaiCompatible',
            supportsToolCalling=True,
        ),
        apiKey=apiKey,
    )


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

    models = providerConfig.get('models')
    if not isinstance(models, list) or not models:
        raise RuntimeError(f'provider {providerId} 缺少 models。')

    selectedModel = selectModel(models, modelId, providerId)
    selectedModelId = selectedModel.get('id')
    if not isinstance(selectedModelId, str) or not selectedModelId.strip():
        raise RuntimeError(f'provider {providerId} 的模型缺少 id。')

    thinking = selectedModel.get('thinking')
    if thinking is not None and not isinstance(thinking, dict):
        raise RuntimeError(f'provider {providerId} 模型 {selectedModelId} 的 thinking 必须是对象。')

    reasoningEffort = selectedModel.get('reasoningEffort')
    if reasoningEffort is not None and not isinstance(reasoningEffort, str):
        raise RuntimeError(f'provider {providerId} 模型 {selectedModelId} 的 reasoningEffort 必须是字符串。')

    api = selectedModel.get('api') or providerConfig.get('api')
    if api != 'openai-completions':
        raise RuntimeError(f'当前仅支持 openai-completions，实际配置为：{api}')

    rawApiKey = providerConfig.get('apiKey')
    if not isinstance(rawApiKey, str) or not rawApiKey.strip():
        raise RuntimeError(f'provider {providerId} 缺少 apiKey。')
    apiKey = resolveApiKey(rawApiKey.strip(), providerId)

    if debugConsole:
        debugConsole.debug(
            f'从 YAML 加载模型配置 provider={providerId} model={selectedModelId} '
            f'baseUrl={baseUrl.strip()} thinking={thinking} reasoningEffort={reasoningEffort}'
        )
    return resolvedModelConfig(
        config=modelConfig(
            provider=providerId,
            model=selectedModelId.strip(),
            baseUrl=baseUrl.strip(),
            apiType='openaiCompatible',
            supportsToolCalling=True,
            thinking=thinking,
            reasoningEffort=reasoningEffort,
        ),
        apiKey=apiKey,
    )


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
