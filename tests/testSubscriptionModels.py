'''
Author: wilbur
Version: 1.3
Date: 2026-09-07
Description: Tests proxy-aware fixed-URL/no-redirect discovery, bounded HTTPError handling, live/local semantics, filtering, stale-token refresh, structured failures, and canary non-disclosure. v1.2 adds ChatGPT Codex live catalog, GPT-6 mapping, concurrency, boundary, transport, and fallback coverage. v1.3 verifies OpenAI 403 is not mislabeled as expired credentials.
'''

from __future__ import annotations

import http.server
import io
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from flamingoAgents.models import subscriptionAuth, subscriptionModels
from flamingoAgents.models.credentialStore import credentialStore, oauthCredential

accessCanary = 'ACCESS-CANARY-NEVER-RETURN'
refreshCanary = 'REFRESH-CANARY-NEVER-RETURN'
accountCanary = 'ACCOUNT-CANARY-NEVER-RETURN'


def makeStore(tmp_path: Path, access: str = accessCanary) -> credentialStore:
    store = credentialStore(tmp_path / 'auth')
    store.writeCredential('xai', oauthCredential(
        access=access,
        refresh=refreshCanary,
        expires=time.time() + 3600,
    ))
    return store


def makeOpenAiStore(tmp_path: Path, access: str = accessCanary) -> credentialStore:
    store = credentialStore(tmp_path / 'auth')
    store.writeCredential('openai-codex', oauthCredential(
        access=access,
        refresh=refreshCanary,
        expires=time.time() + 3600,
        accountId=accountCanary,
    ))
    return store


def openAiModel(
    slug: str,
    *,
    displayName: str | None = None,
    visibility: str = 'list',
    contextWindow: int = 272000,
    priority: int | None = 1,
    inputModalities: list[str] | None = None,
    defaultReasoning: str | None = 'low',
) -> dict:
    model = {
        'slug': slug,
        'display_name': displayName if displayName is not None else slug,
        'visibility': visibility,
        'context_window': contextWindow,
        'input_modalities': inputModalities if inputModalities is not None else ['text', 'image'],
        'default_reasoning_level': defaultReasoning,
        'supported_reasoning_levels': (
            [{'effort': defaultReasoning, 'description': 'reasoning'}] if defaultReasoning else []
        ),
    }
    if priority is not None:
        model['priority'] = priority
    return model


def httpResponse(status: int, body=None, headers=None) -> subscriptionModels.modelListHttpResponse:
    if isinstance(body, bytes):
        payload = body
    else:
        payload = json.dumps(body if body is not None else {}).encode('utf-8')
    return subscriptionModels.modelListHttpResponse(
        statusCode=status,
        body=payload,
        headers=headers or {},
    )


def modelDocument(ids: list[str]) -> dict:
    return {'object': 'list', 'data': [{'id': modelId, 'object': 'model'} for modelId in ids]}


def testLiveXaiDiscoveryOnlyAutoAppliesKnownResponsesModels(tmp_path: Path) -> None:
    store = makeStore(tmp_path)
    response = modelDocument([
        'grok-4.6', 'grok-4.5', 'grok-4.6',
        'grok-4.3', 'grok-build-0.1', 'grok-imagine-video',
        'grok-future-unknown', 'bad/model',
    ])
    response['data'][0]['access_token'] = accessCanary

    result = subscriptionModels.discoverSubscriptionModels(
        'xai', store=store, requestFn=lambda access: httpResponse(200, response),
    )

    assert result['source'] == 'live-catalog-match'
    assert result['autoApplicable'] is True
    assert result['report']['includedModelIds'] == ['grok-4.6', 'grok-4.5']
    assert [item['id'] for item in result['providerTemplate']['models']] == ['grok-4.6', 'grok-4.5']
    reasons = {item['id']: item['reason'] for item in result['report']['skippedModels']}
    assert reasons['grok-4.3'] == 'requires_openai_completions'
    assert reasons['grok-imagine-video'] == 'unsupported_output_modality'
    assert reasons['grok-future-unknown'] == 'missing_responses_metadata'
    serialized = json.dumps(result)
    assert accessCanary not in serialized
    assert refreshCanary not in serialized
    assert 'authorization' not in serialized.lower()


def testConcurrent401DiscoveryUsesStaleAccessAndRefreshesOnce(tmp_path: Path, monkeypatch) -> None:
    store = makeStore(tmp_path, access='stale-access')
    requestBarrier = threading.Barrier(2)
    refreshCount = 0
    refreshLock = threading.Lock()

    def refresh(provider: str, current: oauthCredential, nowFn=time.time) -> oauthCredential:
        nonlocal refreshCount
        with refreshLock:
            refreshCount += 1
        return oauthCredential(
            access='fresh-access', refresh='fresh-refresh', expires=time.time() + 3600,
        )

    def request(access: str) -> subscriptionModels.modelListHttpResponse:
        if access == 'stale-access':
            requestBarrier.wait(2)
            return httpResponse(401)
        assert access == 'fresh-access'
        return httpResponse(200, modelDocument(['grok-4.6']))

    monkeypatch.setattr(subscriptionAuth, 'refreshOAuthCredential', refresh)
    results = []
    errors = []

    def run() -> None:
        try:
            results.append(subscriptionModels.discoverSubscriptionModels('xai', store=store, requestFn=request))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert not errors
    assert len(results) == 2
    assert refreshCount == 1
    assert store.readCredential('xai').access == 'fresh-access'


def testAuthenticationAndRateLimitFailuresNeverFallback(tmp_path: Path) -> None:
    store = makeStore(tmp_path)

    with pytest.raises(subscriptionModels.modelDiscoveryError) as forbidden:
        subscriptionModels.discoverSubscriptionModels('xai', store=store, requestFn=lambda access: httpResponse(403))
    assert forbidden.value.code == 'reauth_required'

    with pytest.raises(subscriptionModels.modelDiscoveryError) as limited:
        subscriptionModels.discoverSubscriptionModels(
            'xai', store=store,
            requestFn=lambda access: httpResponse(429, headers={'retry-after': '17'}),
        )
    assert limited.value.code == 'rate_limited'
    assert limited.value.retryAfter == 17


def testSecond401StopsAfterOneRefresh(tmp_path: Path, monkeypatch) -> None:
    store = makeStore(tmp_path)
    requestCount = 0

    def refresh(provider: str, current: oauthCredential, nowFn=time.time) -> oauthCredential:
        return oauthCredential(access='new-access', refresh='new-refresh', expires=time.time() + 3600)

    def request(access: str) -> subscriptionModels.modelListHttpResponse:
        nonlocal requestCount
        requestCount += 1
        return httpResponse(401)

    monkeypatch.setattr(subscriptionAuth, 'refreshOAuthCredential', refresh)
    with pytest.raises(subscriptionModels.modelDiscoveryError) as failure:
        subscriptionModels.discoverSubscriptionModels('xai', store=store, requestFn=request)

    assert failure.value.code == 'reauth_required'
    assert requestCount == 2


def testNetworkAndServerFailuresReturnExplicitNonAutomaticFallback(tmp_path: Path) -> None:
    store = makeStore(tmp_path)

    def secretException(access: str):
        raise RuntimeError(f'Authorization: Bearer {access} refresh_token={refreshCanary}')

    network = subscriptionModels.discoverSubscriptionModels('xai', store=store, requestFn=secretException)
    server = subscriptionModels.discoverSubscriptionModels(
        'xai', store=store, requestFn=lambda access: httpResponse(503),
    )

    for result, code in ((network, 'network_error'), (server, 'upstream_503')):
        assert result['source'] == 'local-fallback'
        assert result['autoApplicable'] is False
        assert result['report']['liveFailureCode'] == code
        serialized = json.dumps(result)
        assert accessCanary not in serialized
        assert refreshCanary not in serialized


def testRedirectAndMalformedResponsesAreRejectedWithoutFallback(tmp_path: Path) -> None:
    store = makeStore(tmp_path)
    with pytest.raises(subscriptionModels.modelDiscoveryError) as invalidTransport:
        subscriptionModels.discoverSubscriptionModels('xai', store=store, requestFn=lambda access: object())
    assert invalidTransport.value.code == 'invalid_upstream_response'
    cases = [
        *[
            (httpResponse(status, headers={'location': 'https://attacker.example/models'}), 'redirect_forbidden')
            for status in (301, 302, 303, 307, 308)
        ],
        (httpResponse(200, b'not-json'), 'invalid_upstream_response'),
        (httpResponse(200, {'data': {}}), 'invalid_upstream_response'),
        (httpResponse(200, b'x' * (subscriptionModels.maximumModelsResponseBytes + 1)), 'invalid_upstream_response'),
    ]
    for response, code in cases:
        with pytest.raises(subscriptionModels.modelDiscoveryError) as failure:
            subscriptionModels.discoverSubscriptionModels('xai', store=store, requestFn=lambda access, value=response: value)
        assert failure.value.code == code
        assert accessCanary not in str(failure.value)
        assert refreshCanary not in str(failure.value)


def testProductionOpenerUsesHttpsProxyAndCustomNoRedirect(monkeypatch) -> None:
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:17890')
    monkeypatch.setenv('https_proxy', 'http://127.0.0.1:17890')

    opener = subscriptionModels.buildModelsOpener()
    proxyHandlers = [handler for handler in opener.handlers if type(handler) is urllib.request.ProxyHandler]
    redirectHandlers = [
        handler for handler in opener.handlers
        if type(handler) is subscriptionModels.noRedirectHandler
    ]

    assert len(proxyHandlers) == 1
    assert proxyHandlers[0].proxies['https'] == 'http://127.0.0.1:17890'
    assert len(redirectHandlers) == 1
    handler = redirectHandlers[0]
    request = urllib.request.Request(subscriptionModels.xaiModelsUrl)
    for status in (301, 302, 303, 307, 308):
        assert handler.redirect_request(
            request, None, status, 'redirect', {}, 'https://attacker.example/steal',
        ) is None


def testNoRedirectOpenerNeverContactsLocationTarget() -> None:
    targetHits = []

    class targetHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            targetHits.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            return

    class redirectHandler(http.server.BaseHTTPRequestHandler):
        statusCode = 302
        targetUrl = ''

        def do_GET(self):
            self.send_response(self.statusCode)
            self.send_header('Location', self.targetUrl)
            self.end_headers()

        def log_message(self, format, *args):
            return

    targetServer = http.server.HTTPServer(('127.0.0.1', 0), targetHandler)
    redirectServer = http.server.HTTPServer(('127.0.0.1', 0), redirectHandler)
    redirectHandler.targetUrl = f'http://127.0.0.1:{targetServer.server_port}/steal'
    threads = [
        threading.Thread(target=targetServer.serve_forever, daemon=True),
        threading.Thread(target=redirectServer.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), subscriptionModels.noRedirectHandler(),
    )
    try:
        for status in (301, 302, 303, 307, 308):
            redirectHandler.statusCode = status
            request = urllib.request.Request(
                f'http://127.0.0.1:{redirectServer.server_port}/models',
                headers={'Authorization': f'Bearer {accessCanary}'},
            )
            with pytest.raises(urllib.error.HTTPError) as failure:
                opener.open(request, timeout=2)
            assert failure.value.code == status
            failure.value.close()
        assert targetHits == []
    finally:
        redirectServer.shutdown()
        targetServer.shutdown()
        redirectServer.server_close()
        targetServer.server_close()
        for thread in threads:
            thread.join(2)


def testProductionRequestConvertsBoundedHttpErrorWithoutChangingFixedUrl(monkeypatch) -> None:
    requests = []
    errorBody = io.BytesIO(b'{"error":"unauthorized"}')

    class fakeOpener:
        def open(self, request, timeout):
            requests.append({'url': request.full_url, 'timeout': timeout})
            raise urllib.error.HTTPError(
                request.full_url, 401, 'Authorization Bearer hidden',
                {'Retry-After': '3'}, errorBody,
            )

    monkeypatch.setattr(subscriptionModels, 'buildModelsOpener', lambda: fakeOpener())
    response = subscriptionModels.requestXaiModels(accessCanary)

    assert requests == [{
        'url': 'https://api.x.ai/v1/models',
        'timeout': subscriptionModels.modelsHttpTimeoutSeconds,
    }]
    assert response.statusCode == 401
    assert response.body == b'{"error":"unauthorized"}'
    assert response.headers == {'retry-after': '3'}


def testCredentialStoreExceptionsAreMappedWithoutLeakingCanaries() -> None:
    class brokenStore:
        def readCredential(self, provider):
            raise RuntimeError(f'Authorization Bearer {accessCanary} refresh_token={refreshCanary}')

    with pytest.raises(subscriptionModels.modelDiscoveryError) as failure:
        subscriptionModels.discoverSubscriptionModels('xai', store=brokenStore())

    assert failure.value.code == 'credential_error'
    assert accessCanary not in str(failure.value)
    assert refreshCanary not in str(failure.value)


def testOpenAiCandidatesRequireLoginAndUseExplicitFallback(tmp_path: Path) -> None:
    store = credentialStore(tmp_path / 'auth')
    with pytest.raises(subscriptionModels.modelDiscoveryError) as missing:
        subscriptionModels.discoverSubscriptionModels('openai-codex', store=store)
    assert missing.value.code == 'not_logged_in'

    store = makeOpenAiStore(tmp_path)

    def failRequest(access: str, accountId: str):
        raise TimeoutError(f'Authorization Bearer {access} account={accountId}')

    result = subscriptionModels.discoverSubscriptionModels(
        'openai-codex', store=store, requestFn=failRequest,
    )

    assert result['source'] == 'local-fallback'
    assert result['autoApplicable'] is False
    assert result['report']['liveFailureCode'] == 'network_error'
    assert 'gpt-6-astra' in result['report']['includedModelIds']
    serialized = json.dumps(result)
    assert accessCanary not in serialized
    assert refreshCanary not in serialized
    assert accountCanary not in serialized
    assert result['providerTemplate']['api'] == 'openai-codex-responses'


def testLiveOpenAiDiscoveryMapsVisibleModelsWithoutIdAllowlist(tmp_path: Path) -> None:
    store = makeOpenAiStore(tmp_path)
    gpt6 = openAiModel(
        'gpt-6-astra', displayName='GPT-6-Astra', priority=1,
        inputModalities=['text', 'image'], defaultReasoning='low',
    )
    gpt6['base_instructions'] = accessCanary
    future = openAiModel('gpt-future', displayName='', priority=None, defaultReasoning='ultra')
    hidden = openAiModel('gpt-reserve', visibility='hide', priority=2)
    zeroContext = openAiModel('gpt-zero', contextWindow=0, priority=3)
    unsupportedInput = openAiModel('gpt-audio', inputModalities=['audio'], priority=4)
    missingVisibility = openAiModel('gpt-missing-visibility', priority=5)
    missingVisibility.pop('visibility')
    response = {
        'models': [future, hidden, gpt6, gpt6, zeroContext, unsupportedInput, missingVisibility, {'slug': 'bad/model'}],
        'access_token': accessCanary,
    }
    requestCredentials = []

    def request(access: str, accountId: str) -> subscriptionModels.modelListHttpResponse:
        requestCredentials.append((access, accountId))
        return httpResponse(200, response)

    result = subscriptionModels.discoverSubscriptionModels(
        'openai-codex', store=store, requestFn=request,
    )

    assert requestCredentials == [(accessCanary, accountCanary)]
    assert result['source'] == 'live-account-catalog'
    assert result['autoApplicable'] is True
    assert result['report']['discoveredModelIds'] == [
        'gpt-future', 'gpt-reserve', 'gpt-6-astra', 'gpt-zero',
        'gpt-audio', 'gpt-missing-visibility',
    ]
    assert result['report']['includedModelIds'] == ['gpt-6-astra', 'gpt-future']
    models = result['providerTemplate']['models']
    assert [model['id'] for model in models] == ['gpt-6-astra', 'gpt-future']
    assert models[0] == {
        'id': 'gpt-6-astra',
        'name': 'GPT-6-Astra',
        'input': ['text', 'image'],
        'contextWindow': 272000,
        'maxTokens': 128000,
        'reasoning': True,
        'reasoningEffort': 'low',
        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
    }
    assert models[1]['name'] == 'gpt-future'
    assert models[1]['reasoningEffort'] == 'ultra'
    reasons = {item['id']: item['reason'] for item in result['report']['skippedModels']}
    assert reasons == {
        'gpt-reserve': 'hidden_by_provider',
        'gpt-zero': 'missing_model_metadata',
        'gpt-audio': 'unsupported_input_modality',
        'gpt-missing-visibility': 'missing_model_metadata',
    }
    serialized = json.dumps(result)
    for canary in (accessCanary, refreshCanary, accountCanary, 'base_instructions'):
        assert canary not in serialized


def testOpenAi401RefreshesOnceAndRotatesAccount(tmp_path: Path, monkeypatch) -> None:
    store = makeOpenAiStore(tmp_path, access='stale-access')
    refreshCount = 0
    requests = []

    def refresh(provider: str, current: oauthCredential, nowFn=time.time) -> oauthCredential:
        nonlocal refreshCount
        refreshCount += 1
        assert provider == 'openai-codex'
        assert current.access == 'stale-access'
        return oauthCredential(
            access='fresh-access', refresh='fresh-refresh', expires=time.time() + 3600,
            accountId='fresh-account',
        )

    def request(access: str, accountId: str) -> subscriptionModels.modelListHttpResponse:
        requests.append((access, accountId))
        if access == 'stale-access':
            return httpResponse(401)
        return httpResponse(200, {'models': [openAiModel('gpt-6-astra')]})

    monkeypatch.setattr(subscriptionAuth, 'refreshOAuthCredential', refresh)
    result = subscriptionModels.discoverSubscriptionModels(
        'openai-codex', store=store, requestFn=request,
    )

    assert result['report']['includedModelIds'] == ['gpt-6-astra']
    assert requests == [('stale-access', accountCanary), ('fresh-access', 'fresh-account')]
    assert refreshCount == 1


def testConcurrentOpenAi401RefreshesOnlyOnce(tmp_path: Path, monkeypatch) -> None:
    store = makeOpenAiStore(tmp_path, access='stale-access')
    requestBarrier = threading.Barrier(2)
    refreshCount = 0
    refreshLock = threading.Lock()
    replayCount = 0

    def refresh(provider: str, current: oauthCredential, nowFn=time.time) -> oauthCredential:
        nonlocal refreshCount
        with refreshLock:
            refreshCount += 1
        return oauthCredential(
            access='fresh-access', refresh='fresh-refresh', expires=time.time() + 3600,
            accountId='fresh-account',
        )

    def request(access: str, accountId: str) -> subscriptionModels.modelListHttpResponse:
        nonlocal replayCount
        if access == 'stale-access':
            requestBarrier.wait(2)
            return httpResponse(401)
        assert (access, accountId) == ('fresh-access', 'fresh-account')
        with refreshLock:
            replayCount += 1
        return httpResponse(200, {'models': [openAiModel('gpt-6-astra')]})

    monkeypatch.setattr(subscriptionAuth, 'refreshOAuthCredential', refresh)
    results = []
    errors = []

    def run() -> None:
        try:
            results.append(subscriptionModels.discoverSubscriptionModels(
                'openai-codex', store=store, requestFn=request,
            ))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert not errors
    assert len(results) == 2
    assert refreshCount == 1
    assert replayCount == 2
    assert all(result['report']['includedModelIds'] == ['gpt-6-astra'] for result in results)


def testOpenAiFailuresAndBoundariesAreExplicit(tmp_path: Path, monkeypatch) -> None:
    store = makeOpenAiStore(tmp_path)
    for status, code in (
        (302, 'redirect_forbidden'),
        (400, 'upstream_rejected'),
        (403, 'upstream_rejected'),
        (429, 'rate_limited'),
    ):
        with pytest.raises(subscriptionModels.modelDiscoveryError) as failure:
            subscriptionModels.discoverSubscriptionModels(
                'openai-codex', store=store,
                requestFn=lambda access, accountId, value=status: httpResponse(
                    value, headers={'retry-after': '7'},
                ),
            )
        assert failure.value.code == code
        if status == 429:
            assert failure.value.retryAfter == 7

    for body in (b'not-json', {'models': {}}, {'models': [openAiModel(f'gpt-{index}') for index in range(201)]}):
        with pytest.raises(subscriptionModels.modelDiscoveryError) as failure:
            subscriptionModels.discoverSubscriptionModels(
                'openai-codex', store=store,
                requestFn=lambda access, accountId, value=body: httpResponse(200, value),
            )
        assert failure.value.code == 'invalid_upstream_response'

    oversized = httpResponse(200, b'x' * (subscriptionModels.maximumModelsResponseBytes + 1))
    parseCalled = False

    def forbiddenParse(value):
        nonlocal parseCalled
        parseCalled = True
        raise AssertionError('oversized response must not be parsed')

    monkeypatch.setattr(subscriptionModels.json, 'loads', forbiddenParse)
    with pytest.raises(subscriptionModels.modelDiscoveryError) as tooLarge:
        subscriptionModels.discoveryFromOpenAiResponse(oversized)
    assert tooLarge.value.code == 'invalid_upstream_response'
    assert parseCalled is False


def testOpenAiEmptyAndMaximumCatalogBoundaries(tmp_path: Path) -> None:
    store = makeOpenAiStore(tmp_path)
    emptyPayload = json.dumps({'models': []}).encode()
    exactLimitPayload = emptyPayload + b' ' * (subscriptionModels.maximumModelsResponseBytes - len(emptyPayload))
    exact = subscriptionModels.discoverSubscriptionModels(
        'openai-codex', store=store,
        requestFn=lambda access, accountId: httpResponse(200, exactLimitPayload),
    )
    assert exact['source'] == 'live-account-catalog'
    assert exact['autoApplicable'] is False
    assert exact['providerTemplate']['models'] == []

    maximumModels = [openAiModel(f'gpt-boundary-{index}', visibility='hide') for index in range(200)]
    maximum = subscriptionModels.discoverSubscriptionModels(
        'openai-codex', store=store,
        requestFn=lambda access, accountId: httpResponse(200, {'models': maximumModels}),
    )
    assert len(maximum['report']['discoveredModelIds']) == 200
    assert maximum['autoApplicable'] is False


def testOpenAiNetworkAndServerFallbackAreNeverAutomatic(tmp_path: Path) -> None:
    store = makeOpenAiStore(tmp_path)

    def secretFailure(access: str, accountId: str):
        raise TimeoutError(f'Bearer {access} account={accountId} refresh={refreshCanary}')

    network = subscriptionModels.discoverSubscriptionModels(
        'openai-codex', store=store, requestFn=secretFailure,
    )
    server = subscriptionModels.discoverSubscriptionModels(
        'openai-codex', store=store,
        requestFn=lambda access, accountId: httpResponse(503),
    )
    for result, code in ((network, 'network_error'), (server, 'upstream_503')):
        assert result['source'] == 'local-fallback'
        assert result['autoApplicable'] is False
        assert result['report']['liveFailureCode'] == code
        assert 'gpt-6-astra' in result['report']['includedModelIds']
        serialized = json.dumps(result)
        for canary in (accessCanary, refreshCanary, accountCanary):
            assert canary not in serialized


def testOpenAiSecond401StopsAfterOneRefresh(tmp_path: Path, monkeypatch) -> None:
    store = makeOpenAiStore(tmp_path)
    requestCount = 0
    refreshCount = 0

    def refresh(provider: str, current: oauthCredential, nowFn=time.time) -> oauthCredential:
        nonlocal refreshCount
        refreshCount += 1
        return oauthCredential(
            access='fresh-access', refresh='fresh-refresh', expires=time.time() + 3600,
            accountId='fresh-account',
        )

    def request(access: str, accountId: str) -> subscriptionModels.modelListHttpResponse:
        nonlocal requestCount
        requestCount += 1
        return httpResponse(401)

    monkeypatch.setattr(subscriptionAuth, 'refreshOAuthCredential', refresh)
    with pytest.raises(subscriptionModels.modelDiscoveryError) as failure:
        subscriptionModels.discoverSubscriptionModels(
            'openai-codex', store=store, requestFn=request,
        )

    assert failure.value.code == 'reauth_required'
    assert refreshCount == 1
    assert requestCount == 2


def testProductionOpenAiRequestUsesFixedUrlAndHeaders(monkeypatch) -> None:
    captured = {}
    errorBody = io.BytesIO(b'{}')

    class fakeOpener:
        def open(self, request, timeout):
            captured['url'] = request.full_url
            captured['method'] = request.get_method()
            captured['timeout'] = timeout
            captured['headers'] = {key.lower(): value for key, value in request.header_items()}
            raise urllib.error.HTTPError(request.full_url, 401, 'hidden', {}, errorBody)

    monkeypatch.setattr(subscriptionModels, 'buildModelsOpener', lambda: fakeOpener())
    response = subscriptionModels.requestOpenAiCodexModels(accessCanary, accountCanary)

    assert captured['url'] == subscriptionModels.openAiCodexModelsUrl
    assert captured['url'] == (
        'https://chatgpt.com/backend-api/codex/models?client_version=' +
        subscriptionModels.openAiCodexModelsClientVersion
    )
    assert captured['method'] == 'GET'
    assert captured['timeout'] == subscriptionModels.modelsHttpTimeoutSeconds
    assert captured['headers']['authorization'] == f'Bearer {accessCanary}'
    assert captured['headers']['chatgpt-account-id'] == accountCanary
    assert captured['headers']['accept'] == 'application/json'
    assert response.statusCode == 401
