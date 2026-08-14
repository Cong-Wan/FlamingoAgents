'''
Author: wilbur
Version: 1.3
Date: 2026-08-14
Description: 只读扫描 config/skills/<name>/SKILL.md（大写，对齐 Agent Skills 规范），解析 frontmatter，生成注入 system prompt 的 XML 片段。
            v1.1：loadSkills 新增可选 debugConsole 参数（门控 debug 输出，不再无条件 print）。
            v1.2：目录只有大写 SKILL.md 时补 debug 提示（Linux 大小写敏感会静默加载不到）；删除残留 datetime import。
            v1.3：入口文件名由小写 skill.md 改为大写 SKILL.md（用户决定，对齐官方规范）；只对 skill.md 补 debug 提示。
'''

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Skill:
    name: str
    description: str
    filePath: str
    baseDir: str
    disabled: bool


defaultSkillsDir = Path(__file__).resolve().parents[2] / 'config' / 'skills'

skillNamePattern = re.compile(r'^[A-Za-z0-9_-]+$')


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
    block = '\n'.join(lines[1:endIndex])
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _asText(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return str(value)


def loadSkills(skillsDir, debugConsole=None) -> list[Skill]:
    def _debug(message: str) -> None:
        if debugConsole is not None and getattr(debugConsole, 'isDebug', False):
            debugConsole.debug(message)

    root = Path(skillsDir)
    if not root.is_dir():
        return []
    folders = sorted(
        [entry for entry in root.iterdir() if entry.is_dir()],
        key=lambda entry: entry.name,
    )
    seenNames: set[str] = set()
    skills: list[Skill] = []
    for folder in folders:
        skillFile = folder / 'SKILL.md'
        if not skillFile.is_file():
            if (folder / 'skill.md').is_file():
                _debug(f'跳过 skill：只找到 skill.md，需要大写 SKILL.md（{folder}）')
            continue
        try:
            text = skillFile.read_text(encoding='utf-8')
        except OSError as error:
            _debug(f'跳过 skill：无法读取 {skillFile}（{error}）')
            continue
        parsed = _parseFrontmatter(text)
        name = _asText(parsed.get('name')).strip() or folder.name
        if not skillNamePattern.fullmatch(name):
            _debug(f'跳过 skill：name 非法 {name!r}（{skillFile}）')
            continue
        description = _asText(parsed.get('description')).strip()
        if not description:
            _debug(f'跳过 skill：description 为空（{skillFile}）')
            continue
        disabled = parsed.get('disable') is True
        if name in seenNames:
            _debug(f'跳过 skill：同名 {name} 后者（{skillFile}）')
            continue
        seenNames.add(name)
        skills.append(Skill(
            name=name,
            description=description,
            filePath=str(skillFile.resolve()),
            baseDir=str(folder.resolve()),
            disabled=disabled,
        ))
    return skills


def _escapeXml(value: str) -> str:
    return html.escape(value, quote=True).replace("'", '&apos;')


def formatSkillsXml(skills) -> str:
    active = [skill for skill in skills if not skill.disabled]
    if not active:
        return ''
    lines = [
        '',
        '## 可用技能',
        '',
        '以下技能提供特定任务的专门指令。当任务与某个技能的描述匹配时，用 read 工具读取其 location 指向的文件，按其中步骤执行；技能内相对路径相对该文件所在目录解析。',
        '',
        '<available_skills>',
    ]
    for skill in active:
        lines.extend([
            '  <skill>',
            f'    <name>{_escapeXml(skill.name)}</name>',
            f'    <description>{_escapeXml(skill.description)}</description>',
            f'    <location>{_escapeXml(skill.filePath)}</location>',
            '  </skill>',
        ])
    lines.append('</available_skills>')
    return '\n'.join(lines) + '\n'
