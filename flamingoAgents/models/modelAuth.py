'''
Author: wilbur
Version: 1.1
Date: 2026-09-01
Description: Builds redaction-safe model authorization and resolves API key or refreshable OAuth credentials dynamically before every Responses request.
'''

from __future__ import annotations

from dataclasses import dataclass, field

from flamingoAgents.models.credentialStore import credentialStore
from flamingoAgents.models.modelConfig import resolvedModelConfig
from flamingoAgents.models.subscriptionAuth import modelAuthError, resolveOAuthCredential


@dataclass
class modelAuth:
    authorizationHeader: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    accessToken: str | None = field(default=None, repr=False)
    authProvider: str | None = None

    def __repr__(self) -> str:
        return f'modelAuth(authProvider={self.authProvider!r}, headers=<redacted>)'


def createModelAuth(apiKey: str) -> modelAuth:
    cleanKey = apiKey.strip()
    if not cleanKey:
        raise RuntimeError('模型 apiKey 不能为空。')
    return modelAuth(
        authorizationHeader=f'Bearer {cleanKey}',
        accessToken=cleanKey,
    )


class modelAuthResolver:
    def __init__(self, resolved: resolvedModelConfig, store: credentialStore | None = None):
        self.resolved = resolved
        self.store = store

    def resolve(self, forceRefresh: bool = False, staleAccess: str | None = None) -> modelAuth:
        config = self.resolved.config
        if config.authType == 'api-key':
            if self.resolved.apiKey is None:
                raise modelAuthError(config.authProvider or config.provider, 'resolve', errorCode='missing_api_key')
            return createModelAuth(self.resolved.apiKey)
        if config.authType != 'oauth' or not config.authProvider:
            raise modelAuthError(config.provider, 'resolve', errorCode='invalid_auth_config')
        credential = resolveOAuthCredential(
            config.authProvider,
            forceRefresh=forceRefresh,
            staleAccess=staleAccess,
            store=self.store,
        )
        headers: dict[str, str] = {}
        if config.authProvider == 'openai-codex':
            if not credential.accountId:
                raise modelAuthError('openai-codex', 'resolve', errorCode='missing_account_id')
            headers['chatgpt-account-id'] = credential.accountId
        return modelAuth(
            authorizationHeader=f'Bearer {credential.access}',
            headers=headers,
            accessToken=credential.access,
            authProvider=config.authProvider,
        )


__all__ = ['createModelAuth', 'modelAuth', 'modelAuthError', 'modelAuthResolver']
