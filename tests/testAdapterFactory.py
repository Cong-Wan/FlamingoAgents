'''
Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: Verifies builder API dispatch keeps legacy Chat Completions unchanged and creates the dynamic Responses adapter for subscription configuration without resolving OAuth at construction time.
'''

from __future__ import annotations

from pathlib import Path

import yaml

from flamingoAgents.builder import createAgent
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
from flamingoAgents.models.responsesAdapter import responsesAdapter


def model() -> dict:
    return {
        'id': 'model-test', 'name': 'Test', 'input': ['text'],
        'contextWindow': 1000, 'maxTokens': 100, 'reasoning': True,
        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
    }


def buildConfig(tmp_path: Path) -> Path:
    path = tmp_path / 'models.yaml'
    path.write_text(yaml.safe_dump({'providers': {
        'legacy': {
            'baseUrl': 'https://relay.example/v1', 'api': 'openai-completions',
            'apiKey': 'legacy-key', 'models': [model()],
        },
        'codex': {
            'baseUrl': 'https://chatgpt.com/backend-api', 'api': 'openai-codex-responses',
            'auth': 'oauth', 'models': [model()],
        },
    }}, sort_keys=False), encoding='utf-8')
    return path


def testBuilderDispatchesAdaptersWithoutEagerOauth(tmp_path: Path) -> None:
    configPath = buildConfig(tmp_path)
    common = {
        'workDir': tmp_path,
        'logDir': tmp_path / 'logs',
        'modelConfigPath': configPath,
        'systemPrompt': 'system',
        'appendCurrentTime': False,
        'skillsDir': '',
        'toolNames': [],
    }

    legacy = createAgent(providerId='legacy', **common)
    codex = createAgent(providerId='codex', **common)

    assert isinstance(legacy.modelAdapter, chatCompletionsAdapter)
    assert isinstance(codex.modelAdapter, responsesAdapter)
