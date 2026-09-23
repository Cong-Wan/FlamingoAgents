'''
Author: wilbur
Version: 1.1
Date: 2026-09-23
Description: 版本号前端静态断言（versionDisplayPlan §5 D2）：登录门与侧栏顶部容器存在、api.js 暴露 getVersion、main.js 启动填充且失败静默。v1.1：侧栏版本号从底部移到顶部「新建会话」上方，断言同步。
'''

from __future__ import annotations

from pathlib import Path


rootPath = Path(__file__).resolve().parents[1]


def testIndexHasVersionContainers():
    index = (rootPath / 'webApp/frontend/index.html').read_text(encoding='utf-8')
    assert 'id="loginVersion"' in index
    assert 'id="sidebarVersion"' in index
    # 侧栏版本号必须位于「新建会话」按钮上方（sidebar-top 之前、sidebar 内部）
    sidebarStart = index.index('<aside class="sidebar">')
    versionPos = index.index('id="sidebarVersion"')
    topPos = index.index('class="sidebar-top"')
    assert sidebarStart < versionPos < topPos
    # 静态资源版本参数已递增（破缓存）
    assert 'styles.css?v=1.27' in index
    assert 'api.js?v=1.11' in index


def testApiExposesGetVersion():
    api = (rootPath / 'webApp/frontend/js/api.js').read_text(encoding='utf-8')
    assert 'getVersion' in api
    assert '/api/version' in api
    # 免认证：getVersion 内不得带 Bearer 头（与 login 同法的裸 fetch）
    getVersionStart = api.index('getVersion: async function')
    getVersionEnd = api.index('getSessions: function')
    segment = api[getVersionStart:getVersionEnd]
    assert 'Authorization' not in segment


def testMainFillsBothContainersAndFailsSilently():
    main = (rootPath / 'webApp/frontend/js/main.js').read_text(encoding='utf-8')
    assert 'getVersion' in main
    assert "getElementById('loginVersion')" in main
    assert "getElementById('sidebarVersion')" in main
    # 失败静默：catch 存在，不阻塞启动
    assert '.catch(' in main
