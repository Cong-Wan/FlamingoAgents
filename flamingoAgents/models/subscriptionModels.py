'''
Author: wilbur
Version: 1.1
Date: 2026-09-01
Description: Safely discovers subscription model candidates with a proxy-aware fixed-URL urllib opener that rejects every redirect, bounded normal/error bodies, stale-token 401 refresh, local metadata, and secret-free reports.
'''

from __future__ import annotations

import copy
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from flamingoAgents.models.credentialStore import credentialStore, defaultCredentialStore
from flamingoAgents.models.subscriptionAuth import modelAuthError, resolveOAuthCredential

xaiModelsUrl = 'https://api.x.ai/v1/models'
modelsHttpTimeoutSeconds = 20
maximumModelsResponseBytes = 1024 * 1024
maximumDiscoveredModels = 200
modelIdPattern = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')
forbiddenTemplateKeys = frozenset({'__proto__', 'prototype', 'constructor'})


class modelDiscoveryError(RuntimeError):
    def __init__(
        self,
        provider: str,
        code: str,
        *,
        statusCode: int | None = None,
        retryAfter: float | None = None,
    ):
        self.provider = provider
        self.code = code
        self.statusCode = statusCode
        self.retryAfter = retryAfter
        self.retryable = code in {'rate_limited'}
        message = discoveryErrorMessage(code)
        statusPart = f' status={statusCode}' if statusCode is not None else ''
        super().__init__(f'{provider} 模型候选发现失败：code={code}{statusPart} {message}')

    def toPublic(self) -> dict[str, Any]:
        result: dict[str, Any] = {'error': str(self), 'code': self.code}
        if self.retryAfter is not None:
            result['retryAfter'] = self.retryAfter
        return result


@dataclass(frozen=True)
class modelListHttpResponse:
    statusCode: int
    body: bytes
    headers: dict[str, str]


def subscriptionModel(
    modelId: str,
    name: str,
    contextWindow: int,
    maxTokens: int,
    *,
    inputTypes: tuple[str, ...] = ('text', 'image'),
    reasoning: bool = True,
    reasoningEffort: str | None = 'high',
) -> dict[str, Any]:
    model = {
        'id': modelId,
        'name': name,
        'input': list(inputTypes),
        'contextWindow': contextWindow,
        'maxTokens': maxTokens,
        'reasoning': reasoning,
        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
    }
    if reasoning and reasoningEffort:
        model['reasoningEffort'] = reasoningEffort
    return model


xaiResponsesCatalog = {
    'grok-4.5': subscriptionModel('grok-4.5', 'Grok 4.5 Subscription', 500000, 500000),
    'grok-4.6': subscriptionModel('grok-4.6', 'Grok 4.6 Subscription', 500000, 500000),
}

xaiCompletionsOnlyModels = frozenset({'grok-4.3', 'grok-build-0.1'})

openAiCodexCatalog = {
    'gpt-5.3-codex-spark': subscriptionModel(
        'gpt-5.3-codex-spark', 'GPT-5.3 Codex Spark', 128000, 128000, inputTypes=('text',),
    ),
    'gpt-5.4': subscriptionModel('gpt-5.4', 'GPT-5.4', 272000, 128000),
    'gpt-5.4-mini': subscriptionModel('gpt-5.4-mini', 'GPT-5.4 mini', 272000, 128000),
    'gpt-5.5': subscriptionModel('gpt-5.5', 'GPT-5.5', 272000, 128000),
    'gpt-5.6-luna': subscriptionModel('gpt-5.6-luna', 'GPT-5.6 Luna', 272000, 128000),
    'gpt-5.6-sol': subscriptionModel('gpt-5.6-sol', 'GPT-5.6 Sol', 272000, 128000),
    'gpt-5.6-terra': subscriptionModel('gpt-5.6-terra', 'GPT-5.6 Terra', 272000, 128000),
}


def discoverSubscriptionModels(
    provider: str,
    *,
    store: credentialStore | None = None,
    requestFn: Callable[[str], modelListHttpResponse] | None = None,
) -> dict[str, Any]:
    if provider == 'openai-codex':
        activeStore = store or defaultCredentialStore
        try:
            resolveOAuthCredential(provider, store=activeStore)
        except modelAuthError as error:
            raise mapAuthError(provider, error) from None
        except Exception:
            raise modelDiscoveryError(provider, 'credential_error') from None
        return localDiscovery(
            provider,
            source='local-only',
            failureCode=None,
            warnings=[
                'ChatGPT 暂无可靠的账户模型枚举端点；以下仅为内置 Codex 配置候选，不代表账户权益。',
                'cost=0 仅表示不做订阅按 Token 成本估算，不代表订阅没有成本。',
            ],
        )
    if provider != 'xai':
        raise modelDiscoveryError(provider, 'unsupported_provider')

    activeStore = store or defaultCredentialStore
    activeRequest = requestFn or requestXaiModels
    try:
        credential = resolveOAuthCredential('xai', store=activeStore)
    except modelAuthError as error:
        raise mapAuthError('xai', error) from None
    except Exception:
        raise modelDiscoveryError('xai', 'credential_error') from None

    usedAccess = credential.access
    firstResponse = safeModelListRequest(activeRequest, usedAccess)
    if firstResponse is None:
        return localDiscovery('xai', source='local-fallback', failureCode='network_error')
    if firstResponse.statusCode == 401:
        try:
            credential = resolveOAuthCredential(
                'xai', forceRefresh=True, staleAccess=usedAccess, store=activeStore,
            )
        except modelAuthError:
            raise modelDiscoveryError('xai', 'reauth_required', statusCode=401) from None
        except Exception:
            raise modelDiscoveryError('xai', 'credential_error') from None
        secondResponse = safeModelListRequest(activeRequest, credential.access)
        if secondResponse is None:
            return localDiscovery('xai', source='local-fallback', failureCode='network_error')
        if secondResponse.statusCode in (401, 403):
            raise modelDiscoveryError('xai', 'reauth_required', statusCode=secondResponse.statusCode)
        return discoveryFromXaiResponse(secondResponse)
    if firstResponse.statusCode == 403:
        raise modelDiscoveryError('xai', 'reauth_required', statusCode=403)
    return discoveryFromXaiResponse(firstResponse)


class noRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, filePointer, code, message, headers, newUrl):
        return None


def buildXaiModelsOpener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(),
        noRedirectHandler(),
    )


def requestXaiModels(accessToken: str) -> modelListHttpResponse:
    request = urllib.request.Request(
        xaiModelsUrl,
        headers={
            'Authorization': f'Bearer {accessToken}',
            'Accept': 'application/json',
            'User-Agent': 'FlamingoAgents/model-discovery',
        },
        method='GET',
    )
    opener = buildXaiModelsOpener()
    try:
        response = opener.open(request, timeout=modelsHttpTimeoutSeconds)
    except urllib.error.HTTPError as error:
        try:
            return modelListHttpResponse(
                statusCode=error.code,
                body=error.read(maximumModelsResponseBytes + 1),
                headers=safeHttpHeaders(error.headers),
            )
        finally:
            error.close()
    try:
        return modelListHttpResponse(
            statusCode=response.getcode(),
            body=response.read(maximumModelsResponseBytes + 1),
            headers=safeHttpHeaders(response.headers),
        )
    finally:
        response.close()


def safeHttpHeaders(rawHeaders) -> dict[str, str]:
    if rawHeaders is None:
        return {}
    return {
        str(key).lower(): str(value)[:200]
        for key, value in rawHeaders.items()
    }


def safeModelListRequest(
    requestFn: Callable[[str], modelListHttpResponse],
    accessToken: str,
) -> modelListHttpResponse | None:
    try:
        response = requestFn(accessToken)
    except Exception:
        return None
    if (
        not isinstance(response, modelListHttpResponse)
        or not isinstance(response.statusCode, int)
        or not 100 <= response.statusCode <= 599
        or not isinstance(response.body, bytes)
        or not isinstance(response.headers, dict)
    ):
        raise modelDiscoveryError('xai', 'invalid_upstream_response')
    return response


def discoveryFromXaiResponse(response: modelListHttpResponse) -> dict[str, Any]:
    statusCode = response.statusCode
    if 300 <= statusCode < 400:
        raise modelDiscoveryError('xai', 'redirect_forbidden', statusCode=statusCode)
    if statusCode == 429:
        raise modelDiscoveryError(
            'xai', 'rate_limited', statusCode=429,
            retryAfter=parseRetryAfter(response.headers.get('retry-after')),
        )
    if statusCode >= 500:
        return localDiscovery('xai', source='local-fallback', failureCode=f'upstream_{statusCode}')
    if statusCode in (401, 403):
        raise modelDiscoveryError('xai', 'reauth_required', statusCode=statusCode)
    if statusCode < 200 or statusCode >= 300:
        raise modelDiscoveryError('xai', 'upstream_rejected', statusCode=statusCode)
    if len(response.body) > maximumModelsResponseBytes:
        raise modelDiscoveryError('xai', 'invalid_upstream_response', statusCode=statusCode)

    try:
        document = json.loads(response.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise modelDiscoveryError('xai', 'invalid_upstream_response', statusCode=statusCode) from None
    if not isinstance(document, dict) or not isinstance(document.get('data'), list):
        raise modelDiscoveryError('xai', 'invalid_upstream_response', statusCode=statusCode)
    rawModels = document['data']
    if len(rawModels) > maximumDiscoveredModels:
        raise modelDiscoveryError('xai', 'invalid_upstream_response', statusCode=statusCode)

    modelIds = []
    seenIds = set()
    invalidCount = 0
    for rawModel in rawModels:
        modelId = rawModel.get('id') if isinstance(rawModel, dict) else None
        if not isinstance(modelId, str) or not modelIdPattern.fullmatch(modelId):
            invalidCount += 1
            continue
        if modelId not in seenIds:
            seenIds.add(modelId)
            modelIds.append(modelId)

    includedModels = []
    includedIds = []
    skippedModels = []
    for modelId in modelIds:
        knownModel = xaiResponsesCatalog.get(modelId)
        if knownModel is not None:
            includedModels.append(copy.deepcopy(knownModel))
            includedIds.append(modelId)
        elif modelId.startswith('grok-imagine-'):
            skippedModels.append({'id': modelId, 'reason': 'unsupported_output_modality'})
        elif modelId in xaiCompletionsOnlyModels:
            skippedModels.append({'id': modelId, 'reason': 'requires_openai_completions'})
        else:
            skippedModels.append({'id': modelId, 'reason': 'missing_responses_metadata'})

    warnings = [
        '实时目录只证明上游返回该 ID，不保证最终账户权益或每次调用成功。',
        'cost=0 仅表示不做订阅按 Token 成本估算，不代表订阅没有成本。',
    ]
    if invalidCount:
        warnings.append(f'已忽略 {invalidCount} 个格式非法的模型条目。')
    if not includedModels:
        warnings.append('实时目录没有命中本地已知 Responses 元数据，未自动创建模型配置。')
    return buildDiscovery(
        'xai',
        source='live-catalog-match',
        autoApplicable=bool(includedModels),
        models=includedModels,
        discoveredIds=modelIds,
        includedIds=includedIds,
        skippedModels=skippedModels,
        warnings=warnings,
        failureCode=None,
    )


def localDiscovery(
    provider: str,
    *,
    source: str,
    failureCode: str | None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    catalog = xaiResponsesCatalog if provider == 'xai' else openAiCodexCatalog
    baseWarnings = list(warnings or [])
    if provider == 'xai':
        baseWarnings.extend([
            '实时 xAI 目录暂不可用；以下为离线配置候选，未验证当前账户权益。',
            '必须显式确认后才会加入编辑区，不会自动保存。',
            'cost=0 仅表示不做订阅按 Token 成本估算，不代表订阅没有成本。',
        ])
    modelIds = list(catalog)
    return buildDiscovery(
        provider,
        source=source,
        autoApplicable=False,
        models=[copy.deepcopy(catalog[modelId]) for modelId in modelIds],
        discoveredIds=[],
        includedIds=modelIds,
        skippedModels=[],
        warnings=baseWarnings,
        failureCode=failureCode,
    )


def buildDiscovery(
    provider: str,
    *,
    source: str,
    autoApplicable: bool,
    models: list[dict[str, Any]],
    discoveredIds: list[str],
    includedIds: list[str],
    skippedModels: list[dict[str, str]],
    warnings: list[str],
    failureCode: str | None,
) -> dict[str, Any]:
    if provider == 'xai':
        suggestedId = 'xaiSubscription'
        baseUrl = 'https://api.x.ai/v1'
        api = 'openai-responses'
    else:
        suggestedId = 'openaiCodex'
        baseUrl = 'https://chatgpt.com/backend-api'
        api = 'openai-codex-responses'
    if suggestedId in forbiddenTemplateKeys:
        raise modelDiscoveryError(provider, 'invalid_local_catalog')
    return {
        'provider': provider,
        'source': source,
        'autoApplicable': autoApplicable,
        'providerTemplate': {
            'suggestedId': suggestedId,
            'baseUrl': baseUrl,
            'api': api,
            'auth': 'oauth',
            'headers': {},
            'models': models,
        },
        'report': {
            'discoveredModelIds': discoveredIds,
            'includedModelIds': includedIds,
            'skippedModels': skippedModels,
            'warnings': warnings,
            'liveFailureCode': failureCode,
        },
    }


def mapAuthError(provider: str, error: modelAuthError) -> modelDiscoveryError:
    if error.errorCode == 'not_logged_in':
        return modelDiscoveryError(provider, 'not_logged_in')
    return modelDiscoveryError(provider, 'reauth_required', statusCode=error.statusCode)


def parseRetryAfter(rawValue: str | None) -> float | None:
    if not isinstance(rawValue, str):
        return None
    try:
        return max(0.0, min(float(rawValue), 3600.0))
    except ValueError:
        return None


def discoveryErrorMessage(code: str) -> str:
    messages = {
        'unsupported_provider': '不支持该订阅 Provider。',
        'not_logged_in': '请先完成订阅登录。',
        'reauth_required': '凭据已失效，请退出后重新登录。',
        'rate_limited': '上游模型目录限流，请稍后重试。',
        'redirect_forbidden': '上游模型目录返回了禁止的重定向。',
        'invalid_upstream_response': '上游模型目录响应格式异常。',
        'upstream_rejected': '上游拒绝了模型目录请求。',
        'invalid_local_catalog': '本地模型目录配置异常。',
        'credential_error': '凭据存储不可用，请检查本机 auth.json 权限和格式。',
        'account_changed': '订阅账户在发现期间发生变化，请重新同步。',
    }
    return messages.get(code, '模型候选发现失败。')
