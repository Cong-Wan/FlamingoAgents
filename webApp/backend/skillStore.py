'''
Author: wilbur
Version: 1.1
Date: 2026-08-14
Description: Web 侧技能薄封装：复用库 loadSkills/defaultSkillsDir，提供列表与正文读取（映射查找 + 路径拘禁）。
            v1.1：新增 _saveLock + getSkillForEdit + saveSkill（校验、文件夹对齐改名、yaml 原子写）。
'''

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import yaml

from flamingoAgents.skills import defaultSkillsDir, loadSkills

_saveLock = threading.Lock()
_namePattern = re.compile(r'^[A-Za-z0-9_-]+$')


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


def _parseFrontmatter(text: str) -> dict:
    if not text.startswith('---'):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    endIndex = None
    for index in range(1, len(lines)):
        if lines[index].strip() == '---':
            endIndex = index
            break
    if endIndex is None:
        return {}
    try:
        parsed = yaml.safe_load('\n'.join(lines[1:endIndex]))
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _skillItem(skill) -> dict:
    return {
        'name': skill.name,
        'description': skill.description,
        'filePath': skill.filePath,
        'baseDir': skill.baseDir,
        'disabled': skill.disabled,
    }


def _findSkill(name: str):
    for skill in loadSkills(defaultSkillsDir):
        if skill.name == name:
            return skill
    return None


def _assertUnderSkillsDir(resolved: Path, name: str) -> None:
    if not resolved.is_relative_to(Path(defaultSkillsDir).resolve()):
        raise RuntimeError(f'skill 路径越界：{name}')


def getSkillBody(name: str) -> dict:
    for skill in loadSkills(defaultSkillsDir):
        if skill.name == name:
            resolved = Path(skill.filePath).resolve()
            if not resolved.is_relative_to(Path(defaultSkillsDir).resolve()):
                raise RuntimeError(f'skill 路径越界：{name}')
            body = _stripFrontmatter(resolved.read_text(encoding='utf-8'))
            return {'name': skill.name, 'baseDir': skill.baseDir, 'body': body}
    raise LookupError(f'技能不存在：{name}')


def getSkillForEdit(name: str) -> dict:
    with _saveLock:
        skill = _findSkill(name)
        if skill is None:
            raise LookupError(f'技能不存在：{name}')
        resolved = Path(skill.filePath).resolve()
        _assertUnderSkillsDir(resolved, name)
        text = resolved.read_text(encoding='utf-8')
        parsed = _parseFrontmatter(text)
        if parsed:
            description = parsed.get('description')
            description = description.strip() if isinstance(description, str) else skill.description
            disabled = parsed.get('disable') is True
        else:
            description = skill.description
            disabled = skill.disabled
        return {
            'name': skill.name,
            'baseDir': skill.baseDir,
            'body': _stripFrontmatter(text),
            'description': description,
            'disabled': disabled,
        }


def _validateFields(fields) -> tuple[str, str, str, bool]:
    if not isinstance(fields, dict):
        raise RuntimeError('请求体必须是对象。')
    name = fields.get('name')
    if not isinstance(name, str) or not _namePattern.fullmatch(name):
        raise RuntimeError('技能名非法：仅允许字母、数字、下划线、连字符。')
    description = fields.get('description')
    if not isinstance(description, str) or not description.strip():
        raise RuntimeError('描述不能为空。')
    if '\n' in description or '\r' in description:
        raise RuntimeError('描述必须为单行。')
    if ': ' in description or ' #' in description:
        raise RuntimeError('描述不能包含英文冒号加空格或「 #」，请改用中文冒号。')
    body = fields.get('body')
    if not isinstance(body, str):
        raise RuntimeError('正文必须是字符串。')
    disabled = fields.get('disabled')
    if not isinstance(disabled, bool):
        raise RuntimeError('disabled 必须是布尔值。')
    return name, description.strip(), body, disabled


def _buildSkillMarkdown(name: str, description: str, body: str, disabled: bool) -> str:
    frontmatter = {'name': name, 'description': description}
    if disabled:
        frontmatter['disable'] = True
    yamlText = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    if not yamlText.endswith('\n'):
        yamlText += '\n'
    content = f'---\n{yamlText}---\n'
    if body:
        bodyOut = body if body.endswith('\n') else body + '\n'
        content += '\n' + bodyOut
    return content


def saveSkill(originalName: str, fields) -> dict:
    name, description, body, disabled = _validateFields(fields)
    with _saveLock:
        skill = _findSkill(originalName)
        if skill is None:
            raise LookupError(f'技能不存在：{originalName}')
        folder = Path(skill.baseDir).resolve()
        _assertUnderSkillsDir(folder, originalName)
        if name != folder.name:
            target = folder.parent / name
            if target.exists():
                raise RuntimeError('目标技能名已存在')
            folder.rename(target)
            folder = target.resolve()
        content = _buildSkillMarkdown(name, description, body, disabled)
        skillPath = folder / 'SKILL.md'
        tmpPath = folder / 'SKILL.md.tmp'
        try:
            tmpPath.write_text(content, encoding='utf-8')
            os.replace(tmpPath, skillPath)
        finally:
            if tmpPath.exists():
                try:
                    tmpPath.unlink()
                except OSError:
                    pass
        saved = _findSkill(name)
        if saved is None:
            raise RuntimeError(f'保存后未能加载技能：{name}')
        return _skillItem(saved)
