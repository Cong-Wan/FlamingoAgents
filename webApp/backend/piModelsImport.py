'''
Author: wilbur
Version: 1.0
Date: 2026-08-13
Description: 上传 models.json 导入：纯函数 convertPiDocument，把已解析的 pi providers 转成 flamingo 形状并生成中文报告。不读盘、不写盘。
'''

from __future__ import annotations

THINKING_LEVEL_ORDER = ('max', 'xhigh', 'high', 'medium', 'low', 'minimal')
ALLOWED_INPUT_TYPES = ('text', 'image')
COST_FIELDS = ('input', 'output', 'cacheRead', 'cacheWrite')
DEFAULT_CONTEXT_WINDOW = 128000
DEFAULT_MAX_TOKENS = 16384
SUPPORTED_API = 'openai-completions'


def convertPiDocument(raw: dict) -> tuple[dict, dict]:
    providersOut: dict = {}
    report = {
        'importedProviders': [],
        'importedModels': [],
        'skippedProviders': [],
        'skippedModels': [],
        'warnings': [],
    }
    providersIn = raw.get('providers') if isinstance(raw, dict) else None
    if not isinstance(providersIn, dict):
        return providersOut, report

    for providerId, provider in providersIn.items():
        _convertProvider(providerId, provider, providersOut, report)

    return providersOut, report


def _convertProvider(providerId, provider, providersOut: dict, report: dict) -> None:
    if not isinstance(providerId, str) or not providerId.strip():
        report['skippedProviders'].append({'id': providerId if isinstance(providerId, str) else '', 'reason': 'providerId 为空'})
        return
    if not isinstance(provider, dict):
        report['skippedProviders'].append({'id': providerId, 'reason': '不是对象。'})
        return

    if 'modelOverrides' in provider:
        report['warnings'].append(f'provider「{providerId}」flamingo 无内置目录，modelOverrides 已忽略。')
    if 'compat' in provider:
        report['warnings'].append(f'provider「{providerId}」的 compat 已忽略。')

    baseUrl = provider.get('baseUrl')
    if not isinstance(baseUrl, str) or not baseUrl.strip():
        report['skippedProviders'].append({'id': providerId, 'reason': '缺少 baseUrl'})
        return

    modelsList = provider.get('models') if isinstance(provider.get('models'), list) else []
    anyModelApiOverride = any(isinstance(model, dict) and 'api' in model for model in modelsList)
    providerApi = provider.get('api')
    if _isNonEmpty(providerApi) and providerApi != SUPPORTED_API and not anyModelApiOverride:
        report['skippedProviders'].append({
            'id': providerId,
            'reason': f'api 为 {providerApi}，当前仅支持 {SUPPORTED_API}。',
        })
        return

    apiKey = _normalizeApiKey(provider.get('apiKey'), providerId, report['warnings'])
    headers = _normalizeHeaders(provider.get('headers'), f'provider「{providerId}」', report['warnings'])

    outModels = []
    seenIds: dict = {}
    for model in modelsList:
        converted = _convertModel(providerId, providerApi, model, report)
        if converted is None:
            continue
        modelId = converted['id']
        if modelId in seenIds:
            report['warnings'].append(f'provider「{providerId}」模型「{modelId}」重复 id，后者覆盖。')
            outModels[seenIds[modelId]] = converted
        else:
            seenIds[modelId] = len(outModels)
            outModels.append(converted)

    if not outModels:
        report['skippedProviders'].append({'id': providerId, 'reason': '没有可导入的 openai-completions 模型'})
        return

    result = {
        'baseUrl': baseUrl.strip() if isinstance(baseUrl, str) else baseUrl,
        'api': SUPPORTED_API,
        'apiKey': apiKey,
        'models': outModels,
    }
    if headers is not None:
        result['headers'] = headers
    providersOut[providerId] = result
    report['importedProviders'].append(providerId)
    for model in outModels:
        report['importedModels'].append({'providerId': providerId, 'modelId': model['id']})


def _convertModel(providerId, providerApi, model, report: dict):
    if not isinstance(model, dict):
        report['skippedModels'].append({'providerId': providerId, 'modelId': '', 'reason': '模型不是对象或 id 为空'})
        return None
    modelId = model.get('id')
    if not isinstance(modelId, str) or not modelId.strip():
        report['skippedModels'].append({
            'providerId': providerId,
            'modelId': modelId if isinstance(modelId, str) else '',
            'reason': '模型不是对象或 id 为空',
        })
        return None
    modelId = modelId.strip()

    effectiveApi = model['api'] if _isNonEmpty(model.get('api')) else providerApi
    if effectiveApi != SUPPORTED_API:
        displayApi = effectiveApi if _isNonEmpty(effectiveApi) else '缺失'
        report['skippedModels'].append({
            'providerId': providerId,
            'modelId': modelId,
            'reason': f'api 为 {displayApi}',
        })
        return None

    if 'baseUrl' in model:
        report['warnings'].append(f'provider「{providerId}」模型「{modelId}」的模型级 baseUrl 已忽略。')
    if 'compat' in model:
        report['warnings'].append(f'provider「{providerId}」模型「{modelId}」的模型级 compat 已忽略。')

    cost = model.get('cost')
    if isinstance(cost, dict) and 'tiers' in cost:
        report['warnings'].append(
            f'provider「{providerId}」模型「{modelId}」的 cost.tiers 已忽略（flamingo 不支持分档计价）。'
        )

    name = model.get('name')
    if not isinstance(name, str) or name == '':
        name = modelId

    converted = {
        'id': modelId,
        'name': name,
        'input': _normalizeInput(model.get('input')),
        'contextWindow': _normalizePositiveInt(
            model, 'contextWindow', DEFAULT_CONTEXT_WINDOW, providerId, modelId, report['warnings'],
        ),
        'maxTokens': _normalizePositiveInt(
            model, 'maxTokens', DEFAULT_MAX_TOKENS, providerId, modelId, report['warnings'],
        ),
        'reasoning': False,
        'cost': _normalizeCost(cost, providerId, modelId, report['warnings']),
    }

    reasoning, thinking, effort = _deriveThinking(model)
    converted['reasoning'] = reasoning
    if thinking is not None:
        converted['thinking'] = thinking
    if effort is not None:
        converted['reasoningEffort'] = effort

    headers = _normalizeHeaders(
        model.get('headers'),
        f'provider「{providerId}」模型「{modelId}」',
        report['warnings'],
    )
    if headers is not None:
        converted['headers'] = headers
    return converted


def _deriveThinking(model: dict) -> tuple[bool, dict | None, str | None]:
    reasoning = bool(model.get('reasoning'))
    effort = None
    for key in ('reasoningEffort', 'reasoning_effort'):
        value = model.get(key)
        if isinstance(value, str) and value:
            effort = value
            break

    levelMap = model.get('thinkingLevelMap')
    if isinstance(levelMap, dict):
        for key in THINKING_LEVEL_ORDER:
            value = levelMap.get(key)
            if isinstance(value, str) and value:
                effort = value
                reasoning = True
                break

    thinking = {'type': 'enabled'} if reasoning else None
    if not (isinstance(effort, str) and effort):
        effort = None
    return reasoning, thinking, effort


def _normalizeHeaders(source, location: str, warnings: list):
    if not isinstance(source, dict):
        return None
    result = {}
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped.startswith('!') or '$' in stripped:
            warnings.append(f'{location} 的 header「{key}」使用了 pi 取值语法，flamingo 不解析，已跳过')
            continue
        result[key] = value
    return result if result else None


def _normalizeApiKey(value, providerId: str, warnings: list) -> str:
    if not isinstance(value, str) or not value.strip():
        warnings.append(
            f'provider「{providerId}」未配置 apiKey（pi 可能走 auth.json/oauth，flamingo 不支持），保存后需手动补 key'
        )
        return ''
    stripped = value.strip()
    if stripped.startswith('!'):
        warnings.append(f'provider「{providerId}」的 apiKey 为 !command，不执行，apiKey 置空')
        return ''
    return stripped


def _normalizeInput(value) -> list:
    result = []
    if isinstance(value, list):
        for item in value:
            if item in ALLOWED_INPUT_TYPES and item not in result:
                result.append(item)
    return result or ['text']


def _normalizePositiveInt(model: dict, field: str, default: int, providerId: str, modelId: str, warnings: list) -> int:
    if field not in model:
        return default
    value = model.get(field)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    warnings.append(
        f'provider「{providerId}」模型「{modelId}」的 {field} 不是正整数，已用缺省 {default}。'
    )
    return default


def _normalizeCost(cost, providerId: str, modelId: str, warnings: list) -> dict:
    costDict = cost if isinstance(cost, dict) else {}
    result = {}
    for field in COST_FIELDS:
        value = costDict.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            result[field] = 0
            continue
        if value < 0:
            warnings.append(
                f'provider「{providerId}」模型「{modelId}」的 cost.{field} 为负数，已置 0。'
            )
            result[field] = 0
        else:
            result[field] = value
    return result


def _isNonEmpty(value) -> bool:
    if isinstance(value, str):
        return bool(value)
    return value is not None
