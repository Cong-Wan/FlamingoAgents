'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Loads OpenAI-compatible model configuration from environment variables.
'''

from __future__ import annotations

import os

from agentTypes import modelConfig


def loadModelConfigFromEnv() -> modelConfig:
    model = os.getenv('SYSTEM_TOOL_AGENT_MODEL', '').strip()
    baseUrl = os.getenv('SYSTEM_TOOL_AGENT_BASE_URL', '').strip()
    apiKeyEnv = os.getenv('SYSTEM_TOOL_AGENT_API_KEY_ENV', 'OPENAI_API_KEY').strip()

    missingFields = []
    if not model:
        missingFields.append('SYSTEM_TOOL_AGENT_MODEL')
    if not baseUrl:
        missingFields.append('SYSTEM_TOOL_AGENT_BASE_URL')
    if not os.getenv(apiKeyEnv, '').strip():
        missingFields.append(apiKeyEnv)
    if missingFields:
        joinedFields = ', '.join(missingFields)
        raise RuntimeError(f'模型配置缺失：{joinedFields}')

    return modelConfig(
        provider='openaiCompatible',
        model=model,
        baseUrl=baseUrl,
        apiKeyEnv=apiKeyEnv,
        apiType='openaiCompatible',
        supportsToolCalling=True,
    )
