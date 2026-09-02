'''
Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: Verifies per-request OAuth resolution, current Codex account header pairing, API-key behavior, and redaction-safe authorization repr.
'''

from __future__ import annotations

import time
from pathlib import Path

from flamingoAgents.models.credentialStore import credentialStore, oauthCredential
from flamingoAgents.models.modelAuth import modelAuthResolver
from flamingoAgents.models.modelConfig import modelConfig, resolvedModelConfig


def testCodexResolverReadsCurrentCredentialEachRequestAndHidesToken(tmp_path: Path) -> None:
    store = credentialStore(tmp_path / 'auth')
    store.writeCredential('openai-codex', oauthCredential(
        access='first-access-secret', refresh='first-refresh-secret',
        expires=time.time() + 3600, accountId='account-first',
    ))
    config = modelConfig(
        provider='alias', model='gpt', baseUrl='https://chatgpt.com/backend-api',
        apiType='openai-codex-responses', authType='oauth', authProvider='openai-codex',
    )
    resolver = modelAuthResolver(resolvedModelConfig(config=config, apiKey=None), store=store)

    first = resolver.resolve()
    store.writeCredential('openai-codex', oauthCredential(
        access='second-access-secret', refresh='second-refresh-secret',
        expires=time.time() + 3600, accountId='account-second',
    ))
    second = resolver.resolve()

    assert first.headers['chatgpt-account-id'] == 'account-first'
    assert second.headers['chatgpt-account-id'] == 'account-second'
    assert second.authorizationHeader == 'Bearer second-access-secret'
    assert 'second-access-secret' not in repr(second)
    assert 'Bearer' not in repr(second)


def testApiKeyResolverDoesNotExposeKeyInRepr() -> None:
    config = modelConfig(
        provider='legacy', model='model', baseUrl='https://relay.example/v1',
        apiType='openai-completions', authType='api-key',
    )
    auth = modelAuthResolver(resolvedModelConfig(config=config, apiKey='api-key-secret')).resolve()

    assert auth.authorizationHeader == 'Bearer api-key-secret'
    assert 'api-key-secret' not in repr(auth)
