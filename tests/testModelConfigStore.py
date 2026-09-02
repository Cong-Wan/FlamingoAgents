'''
Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: Tests Web models.yaml auth/API read and merge behavior, legacy defaults, OAuth apiKey removal, backup creation, and credential-state separation.
'''

from __future__ import annotations

from pathlib import Path

import yaml

from webApp.backend import modelConfigStore


def model(modelId: str) -> dict:
    return {
        'id': modelId,
        'name': modelId,
        'input': ['text'],
        'contextWindow': 1000,
        'maxTokens': 100,
        'reasoning': True,
        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
        'headers': {},
    }


def testWebStorePreservesLegacyAndWritesOauthWithoutApiKey(tmp_path: Path, monkeypatch) -> None:
    modelsPath = tmp_path / 'models.yaml'
    backupPath = tmp_path / 'models.yaml.bak'
    modelsPath.write_text(yaml.safe_dump({'providers': {
        'legacy': {
            'baseUrl': 'https://relay.example/v1',
            'api': 'openai-completions',
            'apiKey': 'existing-secret',
            'stream': False,
            'models': [{**model('legacy-model'), 'stream': False}],
        },
    }}, sort_keys=False), encoding='utf-8')
    monkeypatch.setattr(modelConfigStore, 'modelsYamlPath', modelsPath)
    monkeypatch.setattr(modelConfigStore, 'backupPath', backupPath)

    readBefore = modelConfigStore.readModelsConfig()
    assert readBefore['providers']['legacy']['auth'] == 'api-key'
    assert readBefore['providers']['legacy']['apiKey'] == '__KEEP__'
    assert 'loggedIn' not in readBefore['providers']['legacy']

    modelConfigStore.writeModelsConfig({'providers': {
        'legacy': {
            'baseUrl': 'https://relay.example/v1',
            'api': 'openai-completions',
            'auth': 'api-key',
            'apiKey': '__KEEP__',
            'headers': {},
            'models': [model('legacy-model')],
        },
        'codex': {
            'baseUrl': 'https://chatgpt.com/backend-api',
            'api': 'openai-codex-responses',
            'auth': 'oauth',
            'apiKey': '__KEEP__',
            'headers': {},
            'models': [model('gpt-test')],
        },
    }})

    written = yaml.safe_load(modelsPath.read_text(encoding='utf-8'))['providers']
    assert written['legacy']['apiKey'] == 'existing-secret'
    assert written['legacy']['stream'] is False
    assert written['legacy']['models'][0]['stream'] is False
    assert written['codex']['auth'] == 'oauth'
    assert 'apiKey' not in written['codex']
    assert backupPath.exists()
