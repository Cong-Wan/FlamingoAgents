'''
Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: Thin CLI for shared ChatGPT Codex/xAI subscription login, safe status, manual browser callback fallback, cancellation, credential persistence, and logout.
'''

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser

from flamingoAgents.models.credentialStore import defaultCredentialStore
from flamingoAgents.models.subscriptionAuth import (
    deviceCodeInfo,
    loginOpenAiDeviceCode,
    loginXaiDeviceCode,
    modelAuthError,
    startOpenAiBrowserLogin,
)


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='管理 ChatGPT Codex/xAI 订阅登录。')
    subparsers = parser.add_subparsers(dest='command', required=True)

    loginParser = subparsers.add_parser('login', help='登录订阅 Provider')
    loginParser.add_argument('provider', choices=('openai-codex', 'xai'))
    loginParser.add_argument('--method', choices=('browser', 'device-code'), default=None)

    subparsers.add_parser('status', help='查看脱敏登录状态')

    logoutParser = subparsers.add_parser('logout', help='删除订阅凭据')
    logoutParser.add_argument('provider', choices=('openai-codex', 'xai'))
    return parser.parse_args()


def printDeviceInfo(info: deviceCodeInfo) -> None:
    print(f'请打开：{info.authUrl}')
    print(f'设备码：{info.userCode}')
    print(f'授权将在约 {int(info.expiresInSeconds)} 秒后过期。')
    try:
        webbrowser.open(info.authUrl)
    except Exception:
        pass


def loginBrowser(cancelEvent: threading.Event):
    login = startOpenAiBrowserLogin()
    print(f'请打开：{login.authUrl}')
    if login.manualCodeRequired:
        print('本机 1455 回调端口不可用，请完成授权后粘贴完整回调 URL 或 code。')
    else:
        print('等待浏览器回调；远程浏览器可在下方粘贴完整回调 URL 或 code。')
    try:
        webbrowser.open(login.authUrl)
    except Exception:
        pass

    def readManualCode() -> None:
        try:
            rawCode = input('回调 URL/code（自动回调时无需输入）：').strip()
            if rawCode:
                login.submitManualCode(rawCode)
        except (EOFError, modelAuthError) as error:
            if isinstance(error, modelAuthError):
                print(str(error), file=sys.stderr)

    threading.Thread(target=readManualCode, daemon=True).start()
    try:
        return login.wait(cancelEvent=cancelEvent)
    finally:
        login.close()


def loginProvider(provider: str, method: str | None) -> None:
    cancelEvent = threading.Event()
    try:
        if provider == 'openai-codex' and (method or 'browser') == 'browser':
            credential = loginBrowser(cancelEvent)
        elif provider == 'openai-codex':
            credential = loginOpenAiDeviceCode(printDeviceInfo, cancelEvent=cancelEvent)
        else:
            if method == 'browser':
                raise RuntimeError('xAI 仅支持 device-code 登录。')
            credential = loginXaiDeviceCode(printDeviceInfo, cancelEvent=cancelEvent)
        if cancelEvent.is_set():
            raise RuntimeError('登录已取消。')
        defaultCredentialStore.writeCredential(provider, credential)
        print(f'{provider} 登录成功，凭据已安全保存。')
    except KeyboardInterrupt:
        cancelEvent.set()
        print('\n登录已取消。', file=sys.stderr)
        raise SystemExit(130)


def status() -> None:
    providers = {}
    for provider in ('openai-codex', 'xai'):
        credential = defaultCredentialStore.readCredential(provider)
        providers[provider] = {
            'loggedIn': credential is not None,
            'expiresAt': credential.expires if credential else None,
            'accountHint': maskAccountId(credential.accountId) if credential else None,
        }
    print(json.dumps({'providers': providers}, ensure_ascii=False, indent=2))


def maskAccountId(accountId: str | None) -> str | None:
    if not accountId or len(accountId) <= 8:
        return accountId
    return accountId[:4] + '…' + accountId[-4:]


def main() -> None:
    args = parseArgs()
    try:
        if args.command == 'login':
            loginProvider(args.provider, args.method)
        elif args.command == 'status':
            status()
        else:
            defaultCredentialStore.deleteCredential(args.provider)
            print(f'{args.provider} 已退出登录。')
    except (RuntimeError, modelAuthError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
