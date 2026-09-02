'''
Author: wilbur
Version: 1.1
Date: 2026-09-01
Description: Tests PKCE/JWT helpers, browser callback safety/fallback, RFC device polling timing, refresh semantics, HTTP timeout, Bearer/token redaction, minimum validity, and concurrent stale-token refresh collapse.
'''

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from flamingoAgents.models.credentialStore import credentialStore, oauthCredential
from flamingoAgents.models import subscriptionAuth


def makeJwt(accountId: str) -> str:
    header = base64.urlsafe_b64encode(b'{}').rstrip(b'=').decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        subscriptionAuth.openAiJwtClaimPath: {'chatgpt_account_id': accountId},
    }).encode()).rstrip(b'=').decode()
    return f'{header}.{payload}.signature'


class fakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def testPkceAndJwtPadding() -> None:
    verifier, challenge = subscriptionAuth.generatePkce()

    assert len(verifier) == 43
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    assert challenge == expected
    assert subscriptionAuth.extractOpenAiAccountId(makeJwt('acct-padding')) == 'acct-padding'


def testManualCallbackRejectsMismatchedState(tmp_path: Path) -> None:
    login = subscriptionAuth.openAiBrowserLogin(store=credentialStore(tmp_path / 'auth'), port=0)
    try:
        with pytest.raises(subscriptionAuth.modelAuthError, match='state_mismatch'):
            login.submitManualCode('authorization-code#wrong-state')
    finally:
        login.close()


def testOccupiedCallbackPortFallsBackToManualCode(tmp_path: Path) -> None:
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    login = subscriptionAuth.openAiBrowserLogin(store=credentialStore(tmp_path / 'auth'), port=port)
    try:
        assert login.manualCodeRequired is True
        assert login.authUrl.startswith(subscriptionAuth.openAiAuthorizeUrl)
    finally:
        login.close()
        occupied.close()


def testDevicePollingImmediateSlowDownAndWaitBeforeFirst() -> None:
    immediateClock = fakeClock()
    statuses = iter([
        {'status': 'pending'},
        {'status': 'slow_down'},
        {'status': 'complete', 'value': 'done'},
    ])
    pollTimes = []

    result = subscriptionAuth.pollDeviceCodeFlow(
        provider='openai-codex',
        poll=lambda: (pollTimes.append(immediateClock.now()) or next(statuses)),
        intervalSeconds=0,
        expiresInSeconds=30,
        waitBeforeFirstPoll=False,
        sleepFn=immediateClock.sleep,
        nowFn=immediateClock.now,
    )

    assert result == 'done'
    assert pollTimes[0] == 0
    assert immediateClock.sleeps == [1.0, 6.0]

    xaiClock = fakeClock()
    xaiPollTimes = []
    assert subscriptionAuth.pollDeviceCodeFlow(
        provider='xai',
        poll=lambda: (xaiPollTimes.append(xaiClock.now()) or {'status': 'complete', 'value': 'xai'}),
        intervalSeconds=None,
        expiresInSeconds=30,
        waitBeforeFirstPoll=True,
        sleepFn=xaiClock.sleep,
        nowFn=xaiClock.now,
    ) == 'xai'
    assert xaiPollTimes == [5.0]


def testXaiRefreshPreservesRefreshToken(monkeypatch) -> None:
    current = oauthCredential(access='old-access', refresh='old-refresh', expires=1)
    monkeypatch.setattr(subscriptionAuth, '_requestJson', lambda *args, **kwargs: subscriptionAuth.httpResult(
        statusCode=200,
        body={'access_token': 'new-access', 'expires_in': 3600},
    ))

    refreshed = subscriptionAuth.refreshXaiCredential(current, nowFn=lambda: 1000)

    assert refreshed.access == 'new-access'
    assert refreshed.refresh == 'old-refresh'
    assert refreshed.expires == 1000 + 3600 - 300


def testOpenAiRefreshAtomicallyGetsNewAccount(monkeypatch) -> None:
    current = oauthCredential(
        access=makeJwt('old-account'),
        refresh='old-refresh',
        expires=1,
        accountId='old-account',
    )
    monkeypatch.setattr(subscriptionAuth, '_requestJson', lambda *args, **kwargs: subscriptionAuth.httpResult(
        statusCode=200,
        body={
            'access_token': makeJwt('new-account'),
            'refresh_token': 'new-refresh',
            'expires_in': 1000,
        },
    ))

    refreshed = subscriptionAuth.refreshOpenAiCredential(current, nowFn=lambda: 50)

    assert refreshed.accountId == 'new-account'
    assert refreshed.refresh == 'new-refresh'
    assert refreshed.expires == 1050


def testMinimumValidityAndConcurrent401RefreshOnlyOnce(tmp_path: Path, monkeypatch) -> None:
    store = credentialStore(tmp_path / 'auth')
    stale = oauthCredential(access='stale', refresh='refresh', expires=time.time() + 60)
    store.writeCredential('xai', stale)
    refreshCalls = 0
    refreshLock = threading.Lock()

    def fakeRefresh(provider, current, nowFn=time.time):
        nonlocal refreshCalls
        with refreshLock:
            refreshCalls += 1
        time.sleep(0.05)
        return oauthCredential(access='fresh', refresh='rotated', expires=time.time() + 3600)

    monkeypatch.setattr(subscriptionAuth, 'refreshOAuthCredential', fakeRefresh)
    first = subscriptionAuth.resolveOAuthCredential('xai', store=store)
    assert first.access == 'fresh'
    assert refreshCalls == 1

    store.writeCredential('xai', stale)
    refreshCalls = 0
    barrier = threading.Barrier(2)

    def resolve401():
        barrier.wait()
        return subscriptionAuth.resolveOAuthCredential(
            'xai', forceRefresh=True, staleAccess='stale', store=store,
        ).access

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resolve401(), range(2)))

    assert results == ['fresh', 'fresh']
    assert refreshCalls == 1


def testOAuthHttpUsesFiniteTimeoutAndRedactsSecrets(monkeypatch) -> None:
    captured = {}

    class fakeResponse:
        status = 200

        def getcode(self):
            return 200

        def read(self, limit):
            return b'{}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fakeUrlopen(request, timeout):
        captured['timeout'] = timeout
        return fakeResponse()

    monkeypatch.setattr(subscriptionAuth.urllib.request, 'urlopen', fakeUrlopen)
    subscriptionAuth._requestJson(
        'https://example.test/token',
        provider='xai',
        action='test',
        form={'client_id': 'public'},
    )

    error = subscriptionAuth.modelAuthError(
        'xai', 'refresh', detail='access_token=secret-access refresh_token=secret-refresh',
    )
    assert captured['timeout'] == subscriptionAuth.oauthHttpTimeoutSeconds
    assert 'secret-access' not in str(error)
    assert 'secret-refresh' not in str(error)
    assert '<redacted>' in str(error)
    assert 'bearer-secret' not in subscriptionAuth.sanitizeErrorText('authorization=Bearer bearer-secret')
