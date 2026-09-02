'''
Author: wilbur
Version: 1.2
Date: 2026-09-01
Description: Tests Web OAuth CAS/generations, arbitrary login-error canary redaction, safe status/logout, authenticated no-store discovery routes, generation race rejection, and credential-secret-free browser responses.
'''

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from flamingoAgents.models.credentialStore import credentialStore, oauthCredential
from webApp.backend import auth, modelAuthManager, server
from webApp.backend.server import app


def waitFor(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('等待条件超时。')


def testDuplicateTaskAndCancelledLateSuccessCannotWrite(tmp_path: Path, monkeypatch) -> None:
    store = credentialStore(tmp_path / 'auth')
    started = threading.Event()
    release = threading.Event()

    def lateSuccess(notify, cancelEvent=None):
        notify(modelAuthManager.deviceCodeInfo(
            authUrl='https://auth.x.ai/device', userCode='SAFE-CODE',
            intervalSeconds=1, expiresInSeconds=60,
        ))
        started.set()
        release.wait(2)
        return oauthCredential(access='late-access-secret', refresh='late-refresh-secret', expires=time.time() + 3600)

    monkeypatch.setattr(modelAuthManager, 'loginXaiDeviceCode', lateSuccess)
    manager = modelAuthManager.modelAuthManager(store=store)
    task = manager.startLogin('xai', 'device_code')
    assert started.wait(1)

    try:
        manager.startLogin('xai', 'device_code')
        raise AssertionError('重复任务应被拒绝。')
    except modelAuthManager.loginConflictError:
        pass

    cancelled = manager.cancelLogin(task['loginId'])
    release.set()
    waitFor(lambda: manager.tasks[task['loginId']].workerExited.is_set())

    assert cancelled['status'] == 'cancelled'
    assert manager.getLogin(task['loginId'])['status'] == 'cancelled'
    assert store.readCredential('xai') is None
    assert 'late-access-secret' not in json.dumps(manager.getLogin(task['loginId']))


def testSuccessfulTaskStatusAndLogoutAreSafe(tmp_path: Path, monkeypatch) -> None:
    store = credentialStore(tmp_path / 'auth')
    invalidations = []

    def success(notify, cancelEvent=None):
        notify(modelAuthManager.deviceCodeInfo(
            authUrl='https://auth.x.ai/device', userCode='SAFE-CODE',
            intervalSeconds=1, expiresInSeconds=60,
        ))
        return oauthCredential(access='access-secret', refresh='refresh-secret', expires=time.time() + 3600)

    monkeypatch.setattr(modelAuthManager, 'loginXaiDeviceCode', success)
    manager = modelAuthManager.modelAuthManager(store=store, invalidateCallback=lambda: invalidations.append(True))
    task = manager.startLogin('xai')
    waitFor(lambda: manager.getLogin(task['loginId'])['status'] != 'pending')

    publicTask = manager.getLogin(task['loginId'])
    publicStatus = manager.getAuthStatus()
    serialized = json.dumps({'task': publicTask, 'status': publicStatus})
    assert publicTask['status'] == 'completed'
    assert publicTask['credentialGeneration'] == 1
    assert publicStatus['providers']['xai']['loggedIn'] is True
    assert publicStatus['providers']['xai']['credentialGeneration'] == 1
    assert 'access-secret' not in serialized
    assert 'refresh-secret' not in serialized
    assert invalidations == [True]

    manager.logout('xai')
    assert store.readCredential('xai') is None
    assert manager.getCredentialGeneration('xai') == 2
    assert invalidations == [True, True]


def testArbitraryLoginFailureCannotExposeCredentialCanaries(tmp_path: Path, monkeypatch, capsys) -> None:
    store = credentialStore(tmp_path / 'auth')
    accessCanary = 'LOGIN-ACCESS-CANARY'
    refreshCanary = 'LOGIN-REFRESH-CANARY'

    def unsafeFailure(notify, cancelEvent=None):
        raise RuntimeError(
            f'Authorization: Bearer {accessCanary} access_token={accessCanary} refresh_token={refreshCanary}'
        )

    monkeypatch.setattr(modelAuthManager, 'loginXaiDeviceCode', unsafeFailure)
    manager = modelAuthManager.modelAuthManager(store=store)
    task = manager.startLogin('xai')
    waitFor(lambda: manager.getLogin(task['loginId'])['status'] != 'pending')

    serialized = json.dumps(manager.getLogin(task['loginId'])) + json.dumps(manager.getAuthStatus())
    captured = capsys.readouterr()
    for forbidden in (accessCanary, refreshCanary, 'authorization', 'access_token', 'refresh_token'):
        assert forbidden.lower() not in serialized.lower()
        assert forbidden.lower() not in (captured.out + captured.err).lower()
    assert manager.getLogin(task['loginId'])['error'] == '订阅认证失败，请重试或重新登录。'


def testModelAuthRoutesRequireWebTokenAndNeverReturnSecrets(tmp_path: Path, monkeypatch) -> None:
    store = credentialStore(tmp_path / 'auth')

    def success(notify, cancelEvent=None):
        return oauthCredential(access='route-access-secret', refresh='route-refresh-secret', expires=time.time() + 3600)

    monkeypatch.setattr(modelAuthManager, 'loginXaiDeviceCode', success)
    manager = modelAuthManager.modelAuthManager(store=store)
    monkeypatch.setattr(modelAuthManager, 'defaultModelAuthManager', manager)
    monkeypatch.setattr(auth, 'serverToken', 'web-test-token')
    client = TestClient(app)

    unauthorized = client.get('/api/modelAuth')
    assert unauthorized.status_code == 401

    headers = {'Authorization': 'Bearer web-test-token'}
    started = client.post('/api/modelAuth/xai/login', json={'method': 'device_code'}, headers=headers)
    assert started.status_code == 200
    loginId = started.json()['loginId']
    waitFor(lambda: manager.getLogin(loginId)['status'] != 'pending')

    taskResponse = client.get('/api/modelAuth/logins/' + loginId, headers=headers)
    statusResponse = client.get('/api/modelAuth', headers=headers)
    assert taskResponse.status_code == 200
    assert statusResponse.status_code == 200
    serialized = taskResponse.text + statusResponse.text
    for forbidden in ('route-access-secret', 'route-refresh-secret', 'authorization', 'code_verifier'):
        assert forbidden not in serialized.lower()

    loggedOut = client.delete('/api/modelAuth/xai', headers=headers)
    assert loggedOut.status_code == 200
    assert store.readCredential('xai') is None


def testDiscoveryRouteIsAuthenticatedNoStoreAndRejectsGenerationRace(tmp_path: Path, monkeypatch) -> None:
    store = credentialStore(tmp_path / 'auth')
    store.writeCredential('xai', oauthCredential(
        access='discover-access-secret', refresh='discover-refresh-secret', expires=time.time() + 3600,
    ))
    manager = modelAuthManager.modelAuthManager(store=store)
    monkeypatch.setattr(modelAuthManager, 'defaultModelAuthManager', manager)
    monkeypatch.setattr(auth, 'serverToken', 'web-test-token')
    safeResult = {
        'provider': 'xai', 'source': 'live-catalog-match', 'autoApplicable': True,
        'providerTemplate': {
            'suggestedId': 'xaiSubscription', 'baseUrl': 'https://api.x.ai/v1',
            'api': 'openai-responses', 'auth': 'oauth', 'headers': {}, 'models': [],
        },
        'report': {
            'discoveredModelIds': [], 'includedModelIds': [], 'skippedModels': [],
            'warnings': [], 'liveFailureCode': None,
        },
    }
    monkeypatch.setattr(server, 'discoverSubscriptionModels', lambda provider, store: safeResult)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer web-test-token'}

    assert client.post('/api/modelAuth/xai/discover').status_code == 401
    unsupported = client.post('/api/modelAuth/unknown/discover', headers=headers)
    assert unsupported.status_code == 400
    assert unsupported.headers['cache-control'] == 'no-store'
    assert unsupported.json()['code'] == 'unsupported_provider'
    response = client.post('/api/modelAuth/xai/discover', headers=headers)

    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['credentialGeneration'] == 0
    serialized = response.text.lower()
    for forbidden in ('discover-access-secret', 'discover-refresh-secret', 'authorization', 'code_verifier'):
        assert forbidden not in serialized

    def changeAccountDuringDiscovery(provider, store):
        manager.logout('xai')
        return safeResult

    monkeypatch.setattr(server, 'discoverSubscriptionModels', changeAccountDuringDiscovery)
    staleResponse = client.post('/api/modelAuth/xai/discover', headers=headers)
    assert staleResponse.status_code == 409
    assert staleResponse.json()['code'] == 'account_changed'
    assert staleResponse.headers['cache-control'] == 'no-store'
