'''
Author: wilbur
Version: 1.2
Date: 2026-09-01
Description: Coordinates safe Web OAuth tasks, credential generations, and strict public-error mapping so arbitrary login/status exceptions cannot expose credential canaries through browser responses.
'''

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from flamingoAgents.models.credentialStore import credentialStore, defaultCredentialStore
from flamingoAgents.models.subscriptionAuth import (
    browserLoginTimeoutSeconds,
    deviceCodeInfo,
    loginOpenAiDeviceCode,
    loginXaiDeviceCode,
    modelAuthError,
    openAiBrowserLogin,
    startOpenAiBrowserLogin,
)

terminalStatuses = frozenset({'completed', 'error', 'cancelled'})
taskRetentionSeconds = 10 * 60


class loginConflictError(RuntimeError):
    pass


class loginNotFoundError(LookupError):
    pass


@dataclass
class loginTask:
    loginId: str
    provider: str
    method: str
    status: str = 'pending'
    authUrl: str | None = None
    deviceCode: str | None = None
    manualCodeRequired: bool = False
    expiresAt: float | None = None
    accountHint: str | None = None
    error: str | None = None
    credentialGeneration: int = 0
    createdAt: float = field(default_factory=time.time)
    updatedAt: float = field(default_factory=time.time)
    terminalAt: float | None = None
    cancelEvent: threading.Event = field(default_factory=threading.Event, repr=False)
    workerExited: threading.Event = field(default_factory=threading.Event, repr=False)
    browserLogin: openAiBrowserLogin | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def toPublic(self) -> dict[str, Any]:
        return {
            'loginId': self.loginId,
            'provider': self.provider,
            'method': self.method,
            'status': self.status,
            'authUrl': self.authUrl,
            'deviceCode': self.deviceCode,
            'manualCodeRequired': self.manualCodeRequired,
            'expiresAt': self.expiresAt,
            'accountHint': self.accountHint,
            'error': self.error,
            'credentialGeneration': self.credentialGeneration,
        }


class modelAuthManager:
    def __init__(
        self,
        store: credentialStore | None = None,
        invalidateCallback: Callable[[], None] | None = None,
    ):
        self.store = store or defaultCredentialStore
        self.invalidateCallback = invalidateCallback or (lambda: None)
        self.lock = threading.RLock()
        self.tasks: dict[str, loginTask] = {}
        self.activeProviderTasks: dict[str, str] = {}
        self.credentialGenerations = {'openai-codex': 0, 'xai': 0}

    def getAuthStatus(self) -> dict[str, Any]:
        providers = {}
        for provider in ('openai-codex', 'xai'):
            try:
                credential = self.store.readCredential(provider)
                providers[provider] = {
                    'loggedIn': credential is not None,
                    'expiresAt': credential.expires if credential else None,
                    'accountHint': accountHint(credential.accountId) if credential else None,
                    'error': None,
                    'credentialGeneration': self.getCredentialGeneration(provider),
                }
            except RuntimeError as error:
                providers[provider] = {
                    'loggedIn': False,
                    'expiresAt': None,
                    'accountHint': None,
                    'error': safePublicAuthError(error),
                    'credentialGeneration': self.getCredentialGeneration(provider),
                }
        return {'providers': providers}

    def startLogin(self, provider: str, method: str | None = None) -> dict[str, Any]:
        normalizedMethod = normalizeLoginMethod(provider, method)
        with self.lock:
            self._cleanupLocked()
            activeId = self.activeProviderTasks.get(provider)
            active = self.tasks.get(activeId) if activeId else None
            if active is not None and active.status == 'pending':
                raise loginConflictError(f'Provider {provider} 已有登录任务。')
            task = loginTask(
                loginId='login_' + secrets.token_urlsafe(18),
                provider=provider,
                method=normalizedMethod,
                credentialGeneration=self.credentialGenerations[provider],
            )
            if provider == 'openai-codex' and normalizedMethod == 'browser':
                task.browserLogin = startOpenAiBrowserLogin(store=self.store)
                task.authUrl = task.browserLogin.authUrl
                task.manualCodeRequired = task.browserLogin.manualCodeRequired
                task.expiresAt = time.time() + browserLoginTimeoutSeconds
            self.tasks[task.loginId] = task
            self.activeProviderTasks[provider] = task.loginId
            task.thread = threading.Thread(target=self._runTask, args=(task.loginId,), daemon=True)
            task.thread.start()
            return task.toPublic()

    def getLogin(self, loginId: str) -> dict[str, Any]:
        with self.lock:
            self._cleanupLocked()
            return self._requireTaskLocked(loginId).toPublic()

    def submitManualCode(self, loginId: str, rawCode: str) -> dict[str, Any]:
        if not isinstance(rawCode, str) or not rawCode.strip():
            raise RuntimeError('code 必须是非空字符串。')
        with self.lock:
            task = self._requireTaskLocked(loginId)
            if task.status != 'pending' or task.method != 'browser' or task.browserLogin is None:
                raise RuntimeError('该登录任务不接受手工回调 code。')
            browserLogin = task.browserLogin
        browserLogin.submitManualCode(rawCode)
        return self.getLogin(loginId)

    def cancelLogin(self, loginId: str) -> dict[str, Any]:
        browserLogin = None
        with self.lock:
            task = self._requireTaskLocked(loginId)
            if task.status == 'pending':
                task.cancelEvent.set()
                task.status = 'cancelled'
                task.error = None
                task.terminalAt = time.time()
                task.updatedAt = task.terminalAt
                if self.activeProviderTasks.get(task.provider) == loginId:
                    self.activeProviderTasks.pop(task.provider, None)
                browserLogin = task.browserLogin
            result = task.toPublic()
        if browserLogin is not None:
            browserLogin.close()
        return result

    def logout(self, provider: str) -> None:
        if provider not in ('openai-codex', 'xai'):
            raise RuntimeError(f'不支持的 OAuth Provider：{provider}')
        self.store.deleteCredential(provider)
        with self.lock:
            self.credentialGenerations[provider] += 1
        self.invalidateCallback()

    def getCredentialGeneration(self, provider: str) -> int:
        if provider not in ('openai-codex', 'xai'):
            raise RuntimeError(f'不支持的 OAuth Provider：{provider}')
        with self.lock:
            return self.credentialGenerations[provider]

    def _runTask(self, loginId: str) -> None:
        with self.lock:
            task = self.tasks.get(loginId)
        if task is None:
            return
        try:
            if task.provider == 'openai-codex' and task.method == 'browser':
                if task.browserLogin is None:
                    raise RuntimeError('浏览器登录初始化失败。')
                credential = task.browserLogin.wait(cancelEvent=task.cancelEvent)
            elif task.provider == 'openai-codex':
                credential = loginOpenAiDeviceCode(
                    lambda info: self._notifyDeviceCode(loginId, info),
                    cancelEvent=task.cancelEvent,
                )
            else:
                credential = loginXaiDeviceCode(
                    lambda info: self._notifyDeviceCode(loginId, info),
                    cancelEvent=task.cancelEvent,
                )
            with self.lock:
                current = self.tasks.get(loginId)
                if current is None or current.status != 'pending' or current.cancelEvent.is_set():
                    return
                self.store.writeCredential(current.provider, credential)
                self.credentialGenerations[current.provider] += 1
                current.credentialGeneration = self.credentialGenerations[current.provider]
                current.status = 'completed'
                current.expiresAt = credential.expires
                current.accountHint = accountHint(credential.accountId)
                current.error = None
                current.terminalAt = time.time()
                current.updatedAt = current.terminalAt
                if self.activeProviderTasks.get(current.provider) == loginId:
                    self.activeProviderTasks.pop(current.provider, None)
                self.invalidateCallback()
        except Exception as error:
            with self.lock:
                current = self.tasks.get(loginId)
                if current is not None and current.status == 'pending':
                    if current.cancelEvent.is_set():
                        current.status = 'cancelled'
                        current.error = None
                    else:
                        current.status = 'error'
                        current.error = safePublicAuthError(error)
                    current.terminalAt = time.time()
                    current.updatedAt = current.terminalAt
                    if self.activeProviderTasks.get(current.provider) == loginId:
                        self.activeProviderTasks.pop(current.provider, None)
        finally:
            if task.browserLogin is not None:
                task.browserLogin.close()
            task.workerExited.set()

    def _notifyDeviceCode(self, loginId: str, info: deviceCodeInfo) -> None:
        with self.lock:
            task = self.tasks.get(loginId)
            if task is None or task.status != 'pending' or task.cancelEvent.is_set():
                return
            task.authUrl = info.authUrl
            task.deviceCode = info.userCode
            task.expiresAt = time.time() + info.expiresInSeconds
            task.updatedAt = time.time()

    def _requireTaskLocked(self, loginId: str) -> loginTask:
        task = self.tasks.get(loginId)
        if task is None:
            raise loginNotFoundError(f'登录任务不存在：{loginId}')
        return task

    def _cleanupLocked(self) -> None:
        now = time.time()
        expiredIds = [
            loginId
            for loginId, task in self.tasks.items()
            if task.status in terminalStatuses
            and task.workerExited.is_set()
            and task.terminalAt is not None
            and now - task.terminalAt >= taskRetentionSeconds
        ]
        for loginId in expiredIds:
            task = self.tasks.pop(loginId)
            if self.activeProviderTasks.get(task.provider) == loginId:
                self.activeProviderTasks.pop(task.provider, None)


def normalizeLoginMethod(provider: str, method: str | None) -> str:
    if provider == 'openai-codex':
        normalized = (method or 'browser').replace('-', '_')
        if normalized not in ('browser', 'device_code'):
            raise RuntimeError('OpenAI 登录 method 仅允许 browser/device_code。')
        return normalized
    if provider == 'xai':
        normalized = (method or 'device_code').replace('-', '_')
        if normalized != 'device_code':
            raise RuntimeError('xAI 仅支持 device_code 登录。')
        return normalized
    raise RuntimeError(f'不支持的 OAuth Provider：{provider}')


def safePublicAuthError(error: Exception) -> str:
    if isinstance(error, modelAuthError):
        return str(error)
    return '订阅认证失败，请重试或重新登录。'


def accountHint(accountId: str | None) -> str | None:
    if not accountId:
        return None
    if len(accountId) <= 8:
        return accountId
    return accountId[:4] + '…' + accountId[-4:]


from webApp.backend import agentManager

defaultModelAuthManager = modelAuthManager(invalidateCallback=agentManager.invalidateAllAgents)
