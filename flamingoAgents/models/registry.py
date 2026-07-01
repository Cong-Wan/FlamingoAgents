'''
Author: wilbur
Version: 1.5
Date: 2026-07-01
Description: Loads Flamingo Agents model configuration and runs direct endpoint validation.
'''

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from flamingoAgents.core.types import modelConfig


defaultModelConfigPath = Path(__file__).resolve().parents[2] / 'config' / 'models.yaml'


def loadModelConfig() -> modelConfig:
    if defaultModelConfigPath.exists():
        return loadModelConfigFromYaml()
    return loadModelConfigFromEnv()


def testModelConfig(prompt: str = '请只回复 pong', maxTokens: int = 16, timeout: int = 30) -> str:
    config = loadModelConfig()
    apiKey = os.getenv(config.apiKeyEnv, '').strip()
    if not apiKey:
        raise RuntimeError(f'环境变量缺失：{config.apiKeyEnv}')

    requestPayload = {
        'model': config.model,
        'messages': [
            {'role': 'user', 'content': prompt},
        ],
        'max_tokens': maxTokens,
    }
    requestUrl = config.baseUrl.rstrip('/') + '/chat/completions'
    requestBytes = json.dumps(requestPayload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        requestUrl,
        data=requestBytes,
        method='POST',
        headers={
            'Authorization': f'Bearer {apiKey}',
            'Content-Type': 'application/json',
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            responseText = response.read().decode('utf-8')
    except urllib.error.HTTPError as error:
        errorText = error.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'模型配置测试失败：status={error.code} body={errorText[:1000]}') from error
    except urllib.error.URLError as error:
        raise RuntimeError(f'模型配置测试失败：{error.reason}') from error

    try:
        payload = json.loads(responseText)
    except json.JSONDecodeError as error:
        raise RuntimeError(f'模型配置测试失败：响应不是合法 JSON：{responseText[:1000]}') from error

    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices:
        raise RuntimeError('模型配置测试失败：响应缺少 choices。')
    rawMessage = choices[0].get('message')
    if not isinstance(rawMessage, dict):
        raise RuntimeError('模型配置测试失败：响应缺少 message。')
    content = rawMessage.get('content')
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError('模型配置测试失败：响应缺少 assistant 内容。')
    return content


def loadModelConfigFromEnv() -> modelConfig:
    model = os.getenv('FLAMINGO_AGENTS_MODEL', '').strip()
    baseUrl = os.getenv('FLAMINGO_AGENTS_BASE_URL', '').strip()
    apiKeyEnv = os.getenv('FLAMINGO_AGENTS_API_KEY_ENV', 'OPENAI_API_KEY').strip()

    missingFields = []
    if not model:
        missingFields.append('FLAMINGO_AGENTS_MODEL')
    if not baseUrl:
        missingFields.append('FLAMINGO_AGENTS_BASE_URL')
    if not os.getenv(apiKeyEnv, '').strip():
        missingFields.append(apiKeyEnv)
    if missingFields:
        joinedFields = ', '.join(missingFields)
        raise RuntimeError(f'模型配置缺失：{joinedFields}')

    return modelConfig(
        provider='openaiCompatible',
        model=model,
        baseUrl=baseUrl,
        apiKeyEnv=apiKeyEnv,
        apiType='openaiCompatible',
        supportsToolCalling=True,
    )


def loadModelConfigFromYaml(
    providerId: str = '101',
    modelId: str | None = None,
    configPath: str | Path | None = None,
) -> modelConfig:
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

    selectedModel: dict[str, Any] | None = None
    if modelId is None:
        firstModel = models[0]
        if isinstance(firstModel, dict):
            selectedModel = firstModel
    else:
        for modelItem in models:
            if isinstance(modelItem, dict) and modelItem.get('id') == modelId:
                selectedModel = modelItem
                break
    if selectedModel is None:
        raise RuntimeError(f'provider {providerId} 缺少可用模型：{modelId or "<first>"}')

    selectedModelId = selectedModel.get('id')
    if not isinstance(selectedModelId, str) or not selectedModelId.strip():
        raise RuntimeError(f'provider {providerId} 的模型缺少 id。')

    api = selectedModel.get('api') or providerConfig.get('api')
    if api != 'openai-completions':
        raise RuntimeError(f'当前仅支持 openai-completions，实际配置为：{api}')

    rawApiKey = providerConfig.get('apiKey')
    if not isinstance(rawApiKey, str) or not rawApiKey.strip():
        raise RuntimeError(f'provider {providerId} 缺少 apiKey。')
    apiKey = rawApiKey.strip()

    if apiKey.startswith('${') and apiKey.endswith('}'):
        apiKeyEnv = apiKey[2:-1].strip()
    elif apiKey.startswith('$'):
        apiKeyEnv = apiKey[1:].strip()
    else:
        safeProviderId = ''.join(char if char.isalnum() else '_' for char in providerId).upper()
        apiKeyEnv = f'FLAMINGO_AGENTS_{safeProviderId}_API_KEY'
        os.environ[apiKeyEnv] = apiKey

    if not apiKeyEnv:
        raise RuntimeError(f'provider {providerId} 的 apiKey 环境变量名为空。')
    if not os.getenv(apiKeyEnv, '').strip():
        raise RuntimeError(f'模型配置缺失：{apiKeyEnv}')

    return modelConfig(
        provider=providerId,
        model=selectedModelId,
        baseUrl=baseUrl.strip(),
        apiKeyEnv=apiKeyEnv,
        apiType='openaiCompatible',
        supportsToolCalling=True,
    )


def main() -> None:
    reply = testModelConfig()
    print(f'reply= {reply}')


if __name__ == '__main__':
    main()
