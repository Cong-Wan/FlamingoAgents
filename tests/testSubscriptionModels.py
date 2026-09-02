'''
Author: wilbur
Version: 1.1
Date: 2026-09-01
Description: Tests proxy-aware fixed-URL/no-redirect discovery, bounded HTTPError handling, live/local semantics, filtering, stale-token refresh, structured failures, and canary non-disclosure.
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


def makeStore(tmp_path: Path, access: str = accessCanary) -> credentialStore:
    store = credentialStore(tmp_path / 'auth')
    store.writeCredential('xai', oauthCredential(
        access=access,
        refresh=refreshCanary,
        expires=time.time() + 3600,
    ))
    return store


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

    opener = subscriptionModels.buildXaiModelsOpener()
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

    monkeypatch.setattr(subscriptionModels, 'buildXaiModelsOpener', lambda: fakeOpener())
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


def testLocalCodexCandidatesRequireLoginAndExplicitApplication(tmp_path: Path) -> None:
    store = credentialStore(tmp_path / 'auth')
    with pytest.raises(subscriptionModels.modelDiscoveryError) as missing:
        subscriptionModels.discoverSubscriptionModels('openai-codex', store=store)
    assert missing.value.code == 'not_logged_in'

    store.writeCredential('openai-codex', oauthCredential(
        access='codex-access', refresh='codex-refresh', expires=time.time() + 3600,
        accountId='account-id',
    ))
    result = subscriptionModels.discoverSubscriptionModels('openai-codex', store=store)

    assert result['source'] == 'local-only'
    assert result['autoApplicable'] is False
    assert result['providerTemplate']['api'] == 'openai-codex-responses'
    assert len(result['providerTemplate']['models']) >= 1
