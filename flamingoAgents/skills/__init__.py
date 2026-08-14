'''
Author: wilbur
Version: 1.0
Date: 2026-08-13
Description: 导出 Skill 加载、XML 注入与默认目录等公共符号（顶层 flamingoAgents 包不转导出）。
'''

from flamingoAgents.skills.skillStore import Skill, defaultSkillsDir, formatSkillsXml, loadSkills

__all__ = [
    'Skill',
    'loadSkills',
    'formatSkillsXml',
    'defaultSkillsDir',
]
