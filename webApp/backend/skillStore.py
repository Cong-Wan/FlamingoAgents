'''
Author: wilbur
Version: 1.0
Date: 2026-08-13
Description: Web 侧技能薄封装：复用库 loadSkills/defaultSkillsDir，提供列表与正文读取（映射查找 + 路径拘禁）。
'''

from __future__ import annotations

from pathlib import Path

from flamingoAgents.skills import defaultSkillsDir, loadSkills


def listSkills() -> dict:
    skills = loadSkills(defaultSkillsDir)
    return {'skills': [
        {
            'name': skill.name,
            'description': skill.description,
            'filePath': skill.filePath,
            'baseDir': skill.baseDir,
            'disabled': skill.disabled,
        }
        for skill in skills
    ]}


def _stripFrontmatter(text: str) -> str:
    if not text.startswith('---'):
        return text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        return text
    endIndex = None
    for index in range(1, len(lines)):
        if lines[index].strip() == '---':
            endIndex = index
            break
    if endIndex is None:
        return text
    return ''.join(lines[endIndex + 1:]).lstrip('\n')


def getSkillBody(name: str) -> dict:
    for skill in loadSkills(defaultSkillsDir):
        if skill.name == name:
            resolved = Path(skill.filePath).resolve()
            if not resolved.is_relative_to(Path(defaultSkillsDir).resolve()):
                raise RuntimeError(f'skill 路径越界：{name}')
            body = _stripFrontmatter(resolved.read_text(encoding='utf-8'))
            return {'name': skill.name, 'baseDir': skill.baseDir, 'body': body}
    raise LookupError(f'技能不存在：{name}')
