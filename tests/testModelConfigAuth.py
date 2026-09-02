'''
Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: Verifies legacy API-key defaults, canonical subscription auth mapping, optional OAuth keys, xAI environment fallback, and invalid API/auth/baseUrl rejection.
'''

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from flamingoAgents.models.modelConfig import loadModelConfigFromYaml


def writeConfig(tmp_path: Path, provider: dict) -> Path:
    path = tmp_path / 'models.yaml'
    path.write_text(yaml.safe_dump({'providers': {'test': provider}}, sort_keys=False), encoding='utf-8')
    return path


def baseModel() -> dict:
    return {
        'id': 'model-1',
        'name': 'Model 1',
        'input': ['text'],
        'contextWindow': 1000,
        'maxTokens': 100,
        'reasoning': True,
        'reasoningEffort': 'high',
        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
    }


def testLegacyConfigDefaultsToApiKeyAndKeepsRealApiType(tmp_path: Path) -> None:
    path = writeConfig(tmp_path, {
        'baseUrl': 'https://relay.example/v1',
        'api': 'openai-completions',
        'apiKey': 'legacy-key',
        'models': [baseModel()],
    })

    resolved = loadModelConfigFromYaml('test', configPath=path)

    assert resolved.apiKey == 'legacy-key'
    assert resolved.config.apiType == 'openai-completions'
    assert resolved.config.authType == 'api-key'
    assert resolved.config.authProvider is None
    assert resolved.config.configProviderId == 'test'


def testCodexOauthDoesNotRequireApiKey(tmp_path: Path) -> None:
    path = writeConfig(tmp_path, {
        'baseUrl': 'https://chatgpt.com/backend-api',
        'api': 'openai-codex-responses',
        'auth': 'oauth',
        'models': [baseModel()],
    })

    resolved = loadModelConfigFromYaml('test', configPath=path)

    assert resolved.apiKey is None
    assert resolved.config.authProvider == 'openai-codex'
    assert resolved.config.reasoning is True


def testXaiOauthAndApiKeyFallback(tmp_path: Path, monkeypatch) -> None:
    oauthPath = writeConfig(tmp_path, {
        'baseUrl': 'https://api.x.ai/v1',
        'api': 'openai-responses',
        'auth': 'oauth',
        'models': [baseModel()],
    })
    oauth = loadModelConfigFromYaml('test', configPath=oauthPath)
    assert oauth.apiKey is None
    assert oauth.config.authProvider == 'xai'

    apiKeyPath = tmp_path / 'apiKeyModels.yaml'
    apiKeyPath.write_text(yaml.safe_dump({'providers': {'test': {
        'baseUrl': 'https://api.x.ai/v1',
        'api': 'openai-responses',
        'auth': 'api-key',
        'models': [baseModel()],
    }}}), encoding='utf-8')
    monkeypatch.setenv('XAI_API_KEY', 'xai-env-key')
    apiKey = loadModelConfigFromYaml('test', configPath=apiKeyPath)
    assert apiKey.apiKey == 'xai-env-key'


def testInvalidApiAuthCombinationsAreRejected(tmp_path: Path) -> None:
    codexKey = writeConfig(tmp_path, {
        'baseUrl': 'https://chatgpt.com/backend-api',
        'api': 'openai-codex-responses',
        'auth': 'api-key',
        'apiKey': 'wrong',
        'models': [baseModel()],
    })
    with pytest.raises(RuntimeError, match='仅支持 auth=oauth'):
        loadModelConfigFromYaml('test', configPath=codexKey)

    xaiWrongHost = tmp_path / 'wrongHost.yaml'
    xaiWrongHost.write_text(yaml.safe_dump({'providers': {'test': {
        'baseUrl': 'https://relay.example/v1',
        'api': 'openai-responses',
        'auth': 'oauth',
        'models': [baseModel()],
    }}}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='必须使用 api.x.ai'):
        loadModelConfigFromYaml('test', configPath=xaiWrongHost)

    completionsOauth = tmp_path / 'completionsOauth.yaml'
    completionsOauth.write_text(yaml.safe_dump({'providers': {'test': {
        'baseUrl': 'https://relay.example/v1',
        'api': 'openai-completions',
        'auth': 'oauth',
        'models': [baseModel()],
    }}}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='仅支持 auth=api-key'):
        loadModelConfigFromYaml('test', configPath=completionsOauth)
