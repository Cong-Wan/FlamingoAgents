'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Builds model authorization data from resolved model credentials.
'''

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class modelAuth:
    authorizationHeader: str


def createModelAuth(apiKey: str) -> modelAuth:
    cleanKey = apiKey.strip()
    if not cleanKey:
        raise RuntimeError('模型 apiKey 不能为空。')
    return modelAuth(authorizationHeader=f'Bearer {cleanKey}')
