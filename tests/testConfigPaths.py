'''
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: Tests ensureUserConfig 幂等初始化（不拷 models.yaml、不覆盖已有文件）与 loadModelConfig 缺失时报无可用模型。
'''

from __future__ import annotations

from pathlib import Path

import pytest

from flamingoAgents.models.modelConfig import loadModelConfig
from flamingoAgents.utils import configPaths


def _pointUserConfig(monkeypatch, root: Path) -> None:
    configDir = root / 'config'
    monkeypatch.setattr(configPaths, 'userConfigDir', configDir)
    monkeypatch.setattr(configPaths, 'userModelsPath', configDir / 'models.yaml')
    monkeypatch.setattr(configPaths, 'userToolsPath', configDir / 'tools.yaml')
    monkeypatch.setattr(configPaths, 'userSystemPromptPath', configDir / 'systemPrompt.md')
    monkeypatch.setattr(configPaths, 'userSkillsDir', configDir / 'skills')


def testEnsureUserConfigCopiesDefaultsWithoutModels(tmp_path: Path, monkeypatch) -> None:
    _pointUserConfig(monkeypatch, tmp_path)
    configPaths.ensureUserConfig()
    assert configPaths.userToolsPath.is_file()
    assert configPaths.userSystemPromptPath.is_file()
    assert configPaths.userSkillsDir.is_dir()
    assert not configPaths.userModelsPath.exists()
    assert not (configPaths.userConfigDir / 'models.yaml').exists()


def testEnsureUserConfigDoesNotOverwrite(tmp_path: Path, monkeypatch) -> None:
    _pointUserConfig(monkeypatch, tmp_path)
    configPaths.ensureUserConfig()
    configPaths.userToolsPath.write_text('user-modified', encoding='utf-8')
    configPaths.userSystemPromptPath.write_text('user-prompt', encoding='utf-8')
    configPaths.ensureUserConfig()
    assert configPaths.userToolsPath.read_text(encoding='utf-8') == 'user-modified'
    assert configPaths.userSystemPromptPath.read_text(encoding='utf-8') == 'user-prompt'


def testEnsureUserConfigMissingTemplateRaises(tmp_path: Path, monkeypatch) -> None:
    _pointUserConfig(monkeypatch, tmp_path)
    emptyTemplates = tmp_path / 'emptyTemplates'
    emptyTemplates.mkdir()
    monkeypatch.setattr(configPaths, 'projectConfigDir', emptyTemplates)
    with pytest.raises(RuntimeError, match='模板文件不存在'):
        configPaths.ensureUserConfig()


def testLoadModelConfigMissingFileRaises(tmp_path: Path) -> None:
    missing = tmp_path / 'missing-models.yaml'
    with pytest.raises(RuntimeError, match='无可用模型'):
        loadModelConfig(configPath=missing)
