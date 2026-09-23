'''
Author: wilbur
Version: 1.0
Date: 2026-09-23
Description: 版本号单一数据源后端覆盖（versionDisplayPlan §5 D1）：/api/version 免认证、与 packageVersion 一致、health 同源、userAgent 同源、dist 元数据防漂移。
'''

from __future__ import annotations

from importlib.metadata import version as distVersion

from fastapi.testclient import TestClient

import flamingoAgents
from webApp.backend import auth, server


def authHeaders(monkeypatch, token='version-api-token'):
    monkeypatch.setattr(auth, 'serverToken', token)
    return {'Authorization': f'Bearer {token}'}


def testVersionEndpointIsUnauthenticated(monkeypatch):
    headers = authHeaders(monkeypatch)
    with TestClient(server.app) as client:
        noAuth = client.get('/api/version')  # 不带 Authorization 头
        withAuth = client.get('/api/version', headers=headers)
    assert noAuth.status_code == 200
    body = noAuth.json()
    assert body['ok'] is True
    assert body['version'] == flamingoAgents.packageVersion == '0.1.0'
    assert withAuth.status_code == 200  # 带 token 访问也不拒绝


def testHealthVersionMatchesPackageVersion(monkeypatch):
    headers = authHeaders(monkeypatch)
    with TestClient(server.app) as client:
        health = client.get('/api/health', headers=headers)
        versionResp = client.get('/api/version')
    assert health.status_code == 200
    assert versionResp.status_code == 200
    assert health.json()['version'] == versionResp.json()['version'] == flamingoAgents.packageVersion


def testUserAgentFollowsPackageVersion():
    from flamingoAgents.models.responsesAdapter import userAgent

    assert userAgent == 'FlamingoAgents/' + flamingoAgents.packageVersion


def testDistMetadataMatchesPackageVersion():
    # pyproject 改 hatch dynamic version 后，dist 元数据应与 version.py 同源（versionDisplayPlan §4.2）
    assert distVersion('flamingo-agents') == flamingoAgents.packageVersion


def testVersionImportChainNoCycle():
    # R1 回归：import 链 __init__ → builder → responsesAdapter → version 不死锁，可重复导入不抛异常
    import flamingoAgents.version  # noqa: F401

    assert flamingoAgents.version.__version__ == flamingoAgents.packageVersion
