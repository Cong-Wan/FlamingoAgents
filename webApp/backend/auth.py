'''
Author: wilbur
Version: 1.0
Date: 2026-08-05
Description: 静态 Bearer Token 认证：token 来自环境变量 FLAMINGO_WEB_TOKEN，secrets.compare_digest 比对，提供 FastAPI 依赖与登录校验。
'''

import os
import secrets

from fastapi import Depends, HTTPException, Request

tokenEnvName = 'FLAMINGO_WEB_TOKEN'
# 启动入口 __main__ 保证该变量已设置（未设置则启动报错退出），此处直接读取。
serverToken = os.environ.get(tokenEnvName, '')


def checkToken(token: str) -> bool:
    # 登录接口与 Bearer 头共用的比对逻辑，compare_digest 防时序侧信道。
    if not serverToken or not isinstance(token, str) or not token:
        return False
    return secrets.compare_digest(token, serverToken)


def requireToken(request: Request) -> None:
    # 挂在除 login 外全部 /api/* 路由上的认证依赖。
    authHeader = request.headers.get('authorization', '')
    if not authHeader.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='缺少认证头，请先登录。')
    if not checkToken(authHeader[len('Bearer '):].strip()):
        raise HTTPException(status_code=401, detail='未认证或 token 不正确。')


authDependency = Depends(requireToken)
