'''
Author: wilbur
Version: 1.3
Date: 2026-09-01
Description: Reads and atomically merges config/models.yaml for Web settings. v1.3 supports three model APIs and api-key/oauth auth, keeps credentials separate, validates canonical subscription combinations, and preserves legacy auth defaults and apiKey masking.
'''

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

from flamingoAgents.models.modelConfig import validateApiAuth

modelsYamlPath = Path(__file__).resolve().parents[2] / 'config' / 'models.yaml'
backupPath = modelsYamlPath.with_suffix('.yaml.bak')

keepPlaceholder = '__KEEP__'
allowedApis = {'openai-completions', 'openai-responses', 'openai-codex-responses'}
allowedAuthTypes = {'api-key', 'oauth'}
allowedInputTypes = {'text', 'image'}
allowedThinkingTypes = {'enabled', 'disabled'}


def readRawYaml() -> dict:
    if not modelsYamlPath.exists():
        raise RuntimeError('config/models.yaml 不存在。')
    try:
        raw = yaml.safe_load(modelsYamlPath.read_text(encoding='utf-8'))
    except yaml.YAMLError as error:
        raise RuntimeError(f'模型配置 yaml 解析失败：{error}')
    return raw if isinstance(raw, dict) else {}


def maskApiKey(rawValue) -> str:
    # 脱敏规则（契约 §2.4）：明文 → __KEEP__；$ 开头环境变量引用原样返回；缺失/空 → ""。
    if not isinstance(rawValue, str) or not rawValue.strip():
        return ''
    value = rawValue.strip()
    if value.startswith('$'):
        return value
    return keepPlaceholder


def normalizeHeadersForRead(headers) -> dict:
    # 宽松读取：仅保留 字符串→字符串 的键值对；无配置返回空对象（UI 以空文本域呈现）。
    if not isinstance(headers, dict):
        return {}
    return {str(key): value for key, value in headers.items() if isinstance(value, str)}


def normalizeModelForRead(model) -> dict:
    modelDict = model if isinstance(model, dict) else {}
    result = {
        'id': modelDict.get('id') if isinstance(modelDict.get('id'), str) else '',
        'name': modelDict.get('name') if isinstance(modelDict.get('name'), str) else '',
        'input': [item for item in (modelDict.get('input') or []) if isinstance(item, str)],
        'contextWindow': modelDict.get('contextWindow') if isinstance(modelDict.get('contextWindow'), int) else 0,
        'maxTokens': modelDict.get('maxTokens') if isinstance(modelDict.get('maxTokens'), int) else 0,
        'reasoning': bool(modelDict.get('reasoning')),
        'cost': normalizeCostForRead(modelDict.get('cost')),
        'headers': normalizeHeadersForRead(modelDict.get('headers')),
    }
    if isinstance(modelDict.get('thinking'), dict):
        result['thinking'] = modelDict['thinking']
    if isinstance(modelDict.get('reasoningEffort'), str):
        result['reasoningEffort'] = modelDict['reasoningEffort']
    return result


def normalizeCostForRead(cost) -> dict:
    costDict = cost if isinstance(cost, dict) else {}
    return {
        key: costDict[key] if isinstance(costDict.get(key), (int, float)) and not isinstance(costDict.get(key), bool) else 0
        for key in ('input', 'output', 'cacheRead', 'cacheWrite')
    }


def readModelsConfig() -> dict:
    # GET 口径（审核 M3）：原始 yaml + 宽松校验，不用库解析器（apiKey 允许为空、不强制单 provider）。
    raw = readRawYaml()
    rawProviders = raw.get('providers')
    providers: dict = {}
    if isinstance(rawProviders, dict):
        for providerId, provider in rawProviders.items():
            providerDict = provider if isinstance(provider, dict) else {}
            rawModels = providerDict.get('models')
            providers[str(providerId)] = {
                'baseUrl': providerDict.get('baseUrl') if isinstance(providerDict.get('baseUrl'), str) else '',
                'api': providerDict.get('api') if isinstance(providerDict.get('api'), str) else '',
                'auth': providerDict.get('auth') if providerDict.get('auth') in allowedAuthTypes else 'api-key',
                'apiKey': maskApiKey(providerDict.get('apiKey')),
                'headers': normalizeHeadersForRead(providerDict.get('headers')),
                'models': [normalizeModelForRead(item) for item in rawModels] if isinstance(rawModels, list) else [],
            }
    return {'providers': providers}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def isPositiveInt(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def isNonNegativeNumber(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def validateHeaders(headers, location: str) -> None:
    if headers is None:
        return
    require(isinstance(headers, dict), f'{location} 的 headers 必须是对象。')
    for key, value in headers.items():
        require(isinstance(key, str) and isinstance(value, str), f'{location} 的 headers 键值必须都是字符串。')


def validateModel(model, location: str) -> None:
    # 契约 §2.4 PUT 校验规则，消息中文指明具体字段。
    require(isinstance(model, dict), f'{location} 必须是对象。')
    require(isinstance(model.get('id'), str) and bool(model['id'].strip()), f'{location} 的 id 必须是非空字符串。')
    if 'name' in model:
        require(isinstance(model['name'], str), f'{location} 的 name 必须是字符串。')
    inputValue = model.get('input')
    require(isinstance(inputValue, list) and all(isinstance(item, str) for item in inputValue), f'{location} 的 input 必须是字符串数组。')
    invalidInput = [item for item in inputValue if item not in allowedInputTypes]
    require(not invalidInput, f'{location} 的 input 仅允许 text/image：{",".join(invalidInput)}')
    require(isPositiveInt(model.get('contextWindow')), f'{location} 的 contextWindow 必须是正整数。')
    require(isPositiveInt(model.get('maxTokens')), f'{location} 的 maxTokens 必须是正整数。')
    require(isinstance(model.get('reasoning'), bool), f'{location} 的 reasoning 必须是布尔值。')
    thinking = model.get('thinking')
    if thinking is not None:
        require(isinstance(thinking, dict) and thinking.get('type') in allowedThinkingTypes,
                f'{location} 的 thinking.type 仅允许 enabled/disabled。')
    reasoningEffort = model.get('reasoningEffort')
    if reasoningEffort is not None:
        require(isinstance(reasoningEffort, str), f'{location} 的 reasoningEffort 必须是字符串。')
    cost = model.get('cost')
    require(isinstance(cost, dict), f'{location} 的 cost 必须是对象。')
    for key in ('input', 'output', 'cacheRead', 'cacheWrite'):
        require(isNonNegativeNumber(cost.get(key)), f'{location} 的 cost.{key} 必须是不小于 0 的数值。')
    validateHeaders(model.get('headers'), location)


def validateProvider(providerId: str, provider) -> None:
    require(isinstance(provider, dict), f'provider {providerId} 必须是对象。')
    require(isinstance(provider.get('baseUrl'), str) and bool(provider['baseUrl'].strip()),
            f'provider {providerId} 的 baseUrl 必须是非空字符串。')
    apiType = provider.get('api')
    require(apiType in allowedApis, f'provider {providerId} 的 api 仅允许 {"/".join(sorted(allowedApis))}。')
    authType = provider.get('auth', 'api-key')
    require(authType in allowedAuthTypes, f'provider {providerId} 的 auth 仅允许 api-key/oauth。')
    validateApiAuth(providerId, apiType, authType, provider['baseUrl'].strip())
    apiKey = provider.get('apiKey')
    if apiKey is not None:
        require(isinstance(apiKey, str), f'provider {providerId} 的 apiKey 必须是字符串。')
    if authType == 'api-key':
        hasConfiguredKey = isinstance(apiKey, str) and bool(apiKey.strip())
        hasXaiFallback = apiType == 'openai-responses' and bool(os.getenv('XAI_API_KEY', '').strip())
        require(hasConfiguredKey or hasXaiFallback, f'provider {providerId} 的 auth=api-key 时必须配置 apiKey。')
    models = provider.get('models')
    require(isinstance(models, list) and bool(models), f'provider {providerId} 的 models 必须是非空数组。')
    validateHeaders(provider.get('headers'), f'provider {providerId}')
    for index, model in enumerate(models):
        validateModel(model, f'provider {providerId} 的 models[{index}]')


def validateBody(body) -> None:
    require(isinstance(body, dict), '请求体必须是对象。')
    providers = body.get('providers')
    require(isinstance(providers, dict) and bool(providers), 'providers 必须是非空对象。')
    for providerId, provider in providers.items():
        require(isinstance(providerId, str) and bool(providerId.strip()), 'providerId 必须是非空字符串。')
        validateProvider(providerId, provider)


def mergeModel(requestModel: dict, existingModels: list) -> dict:
    # 合并式更新（审核 M2）：schema 内字段覆盖，同 id 旧模型的 schema 外字段（如 stream）保留。
    existing = next(
        (item for item in existingModels if isinstance(item, dict) and item.get('id') == requestModel['id']),
        None,
    )
    merged = dict(existing) if existing is not None else {}
    merged['id'] = requestModel['id'].strip()
    merged['name'] = requestModel.get('name', '')
    merged['input'] = list(requestModel['input'])
    merged['contextWindow'] = requestModel['contextWindow']
    merged['maxTokens'] = requestModel['maxTokens']
    merged['reasoning'] = requestModel['reasoning']
    if requestModel.get('thinking') is not None:
        merged['thinking'] = requestModel['thinking']
    else:
        merged.pop('thinking', None)
    if requestModel.get('reasoningEffort') is not None:
        merged['reasoningEffort'] = requestModel['reasoningEffort']
    else:
        merged.pop('reasoningEffort', None)
    merged['cost'] = {key: requestModel['cost'][key] for key in ('input', 'output', 'cacheRead', 'cacheWrite')}
    if 'headers' in requestModel:
        if requestModel['headers']:
            merged['headers'] = dict(requestModel['headers'])
        else:
            merged.pop('headers', None)  # 空对象 = 删除该字段
    return merged


def mergeProvider(requestProvider: dict, existingProvider) -> dict:
    existing = dict(existingProvider) if isinstance(existingProvider, dict) else {}
    existing['baseUrl'] = requestProvider['baseUrl'].strip()
    existing['api'] = requestProvider['api']
    existing['auth'] = requestProvider.get('auth', 'api-key')
    apiKey = requestProvider.get('apiKey')
    if existing['auth'] == 'oauth':
        existing.pop('apiKey', None)
    elif apiKey == keepPlaceholder:
        # 保留 yaml 原值：无原值则视为缺省（不落字段）。
        if not isinstance(existing.get('apiKey'), str) or not existing['apiKey'].strip():
            existing.pop('apiKey', None)
    elif apiKey is None or apiKey == '':
        existing.pop('apiKey', None)
    else:
        existing['apiKey'] = apiKey
    if 'headers' in requestProvider:
        if requestProvider['headers']:
            existing['headers'] = dict(requestProvider['headers'])
        else:
            existing.pop('headers', None)  # 空对象 = 删除该字段
    existingModels = existing.get('models')
    existingModels = existingModels if isinstance(existingModels, list) else []
    existing['models'] = [mergeModel(model, existingModels) for model in requestProvider['models']]
    return existing


def writeModelsConfig(body: dict) -> None:
    validateBody(body)
    # yaml 缺失时以空文档为基底创建（审核 L5）；语法错误沿用 GET 的 400 口径。
    raw = readRawYaml() if modelsYamlPath.exists() else {}
    rawProviders = raw.get('providers')
    existingProviders = rawProviders if isinstance(rawProviders, dict) else {}
    mergedProviders = {
        providerId: mergeProvider(provider, existingProviders.get(providerId))
        for providerId, provider in body['providers'].items()
    }
    mergedDocument = dict(raw)
    mergedDocument['providers'] = mergedProviders
    modelsYamlPath.parent.mkdir(parents=True, exist_ok=True)
    if modelsYamlPath.exists():
        shutil.copy2(modelsYamlPath, backupPath)
    tempPath = modelsYamlPath.with_suffix('.yaml.tmp')
    tempPath.write_text(yaml.safe_dump(mergedDocument, allow_unicode=True, sort_keys=False), encoding='utf-8')
    os.replace(tempPath, modelsYamlPath)
