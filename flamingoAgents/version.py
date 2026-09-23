'''
Author: wilbur
Version: 1.0
Date: 2026-09-23
Description: 包版本号唯一数据源（versionDisplayPlan §4.1）。pyproject.toml 经 hatch dynamic version、flamingoAgents.packageVersion、/api/health、/api/version、User-Agent 全部引用此处，升版本只改本文件。
'''

__version__ = '0.1.0'
