'''
Author: wilbur
Version: 1.3
Date: 2026-08-19
Description: 集中管理 session 日志根目录（~/.flamingo/logs/{webData,cliData}/<路径字符串>/）
            与 usage.db 路径；workDir 子目录为单层文件夹，名字是 ~-相对家目录 或 -绝对路径（/ 换成 -）；
            resolve* 纯计算（读侧，零副作用），ensure* 计算+mkdir（写侧）；
            newSessionId 生成 YYMMDDHHmmss-xxxxxxxx。
            v1.1 曾误做成嵌套真实路径；v1.2 用全角／做一层名；v1.3 `/` 改成 `-`。
'''

from datetime import datetime
from pathlib import Path
from uuid import uuid4

flamingoHome = Path.home() / '.flamingo'
logsRoot = flamingoHome / 'logs'
webLogsRoot = logsRoot / 'webData'
cliLogsRoot = logsRoot / 'cliData'
usageDbPath = logsRoot / 'usage.db'

_categoryRoots = {'webData': webLogsRoot, 'cliData': cliLogsRoot}


def workDirFolderName(workDir: Path) -> str:
    resolved = workDir.resolve()
    home = Path.home().resolve()
    if resolved == home or home in resolved.parents:
        relative = resolved.relative_to(home)
        text = '~' if relative == Path('.') else f'~/{relative.as_posix()}'
    else:
        parts = resolved.parts[1:] if resolved.anchor else resolved.parts
        text = '/' + '/'.join(parts) if parts else '/'
    return text.replace('/', '-')


def resolveSessionLogDir(category: str, workDir: Path) -> Path:
    # 读侧专用：纯计算，不 mkdir
    return _categoryRoots[category] / workDirFolderName(workDir)


def ensureSessionLogDir(category: str, workDir: Path) -> Path:
    # 写侧专用：计算 + mkdir；category 非法 KeyError
    folder = resolveSessionLogDir(category, workDir)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def newSessionId() -> str:
    return f'{datetime.now().strftime("%y%m%d%H%M%S")}-{uuid4().hex[:8]}'
