'''
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: 集中管理用户配置目录（~/.flamingo/config/）与项目模板源（config/）路径。
             ensureUserConfig 幂等初始化：mkdir config 与 skills/，缺失的 tools.yaml / systemPrompt.md
             从项目模板 copy2 拷贝（已存在一律不覆盖）；models.yaml 不自动创建（用户参照
             models.example.yaml 手动配置，缺失时由 modelConfig 报无可用模型）。
             显式约束：本模块保持零库内依赖（仅标准库），防止反向导入成环。
'''

from __future__ import annotations

import shutil
from pathlib import Path

flamingoHome = Path.home() / '.flamingo'
userConfigDir = flamingoHome / 'config'
userModelsPath = userConfigDir / 'models.yaml'
userToolsPath = userConfigDir / 'tools.yaml'
userSystemPromptPath = userConfigDir / 'systemPrompt.md'
userSkillsDir = userConfigDir / 'skills'

projectConfigDir = Path(__file__).resolve().parents[2] / 'config'
modelsExamplePath = projectConfigDir / 'models.example.yaml'


def _copyIfMissing(source: Path, target: Path) -> None:
    if target.exists():
        return
    if not source.is_file():
        raise RuntimeError(f'模板文件不存在：{source}（项目 config/ 目录不完整）')
    shutil.copy2(source, target)


def ensureUserConfig() -> None:
    # 幂等初始化：只补缺失项，已存在一律不覆盖；models.yaml 不在此列（configHomePlan D1/D3）。
    userConfigDir.mkdir(parents=True, exist_ok=True)
    userSkillsDir.mkdir(parents=True, exist_ok=True)
    _copyIfMissing(projectConfigDir / 'tools.yaml', userToolsPath)
    _copyIfMissing(projectConfigDir / 'systemPrompt.md', userSystemPromptPath)
