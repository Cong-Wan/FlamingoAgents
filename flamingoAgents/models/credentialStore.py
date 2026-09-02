'''
Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: Stores ChatGPT/xAI OAuth credentials in ~/.flamingo/auth.json with strict validation, stable cross-process locking, ownership/type checks, and atomic 0600 writes.
'''

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

canonicalProviders = frozenset({'openai-codex', 'xai'})


class credentialStoreError(RuntimeError):
    pass


@dataclass
class oauthCredential:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    expires: float
    accountId: str | None = None
    type: str = 'oauth'

    def __repr__(self) -> str:
        accountText = ', accountId=<set>' if self.accountId else ''
        return f'oauthCredential(type={self.type!r}, expires={self.expires!r}{accountText})'


threadLocksGuard = threading.Lock()
threadLocks: dict[tuple[str, str], threading.RLock] = {}
fileThreadLocks: dict[str, threading.RLock] = {}


def getThreadLock(lockPath: Path, provider: str) -> tuple[threading.RLock, threading.RLock]:
    pathKey = str(lockPath)
    with threadLocksGuard:
        fileLock = fileThreadLocks.setdefault(pathKey, threading.RLock())
        providerLock = threadLocks.setdefault((pathKey, provider), threading.RLock())
    return fileLock, providerLock


class credentialStore:
    def __init__(self, baseDir: str | Path | None = None):
        self.baseDir = Path(baseDir).expanduser() if baseDir is not None else Path.home() / '.flamingo'
        self.authPath = self.baseDir / 'auth.json'
        self.lockPath = self.baseDir / 'auth.lock'

    def readCredential(self, provider: str) -> oauthCredential | None:
        self._validateProvider(provider)
        with self._locked(provider):
            document = self._readDocumentUnlocked()
            rawCredential = document['providers'].get(provider)
            if rawCredential is None:
                return None
            return self._parseCredential(provider, rawCredential)

    def writeCredential(self, provider: str, credential: oauthCredential) -> None:
        self._validateProvider(provider)
        self._validateCredential(provider, credential)
        with self._locked(provider):
            document = self._readDocumentUnlocked()
            document['providers'][provider] = self._credentialToJson(credential)
            self._writeDocumentUnlocked(document)

    def deleteCredential(self, provider: str) -> None:
        self._validateProvider(provider)
        with self._locked(provider):
            document = self._readDocumentUnlocked()
            if provider not in document['providers']:
                return
            del document['providers'][provider]
            self._writeDocumentUnlocked(document)

    def modifyCredential(
        self,
        provider: str,
        update: Callable[[oauthCredential | None], oauthCredential | None],
    ) -> oauthCredential | None:
        """Run a provider read/modify/write while holding both thread and file locks.

        Returning None means no write and returns the current value. Deletion uses
        deleteCredential() so refresh callbacks cannot accidentally erase credentials.
        """
        self._validateProvider(provider)
        with self._locked(provider):
            document = self._readDocumentUnlocked()
            rawCurrent = document['providers'].get(provider)
            current = self._parseCredential(provider, rawCurrent) if rawCurrent is not None else None
            nextCredential = update(current)
            if nextCredential is None:
                return current
            self._validateCredential(provider, nextCredential)
            document['providers'][provider] = self._credentialToJson(nextCredential)
            self._writeDocumentUnlocked(document)
            return nextCredential

    def _validateProvider(self, provider: str) -> None:
        if provider not in canonicalProviders:
            raise credentialStoreError(f'不支持的 OAuth Provider：{provider}')

    def _ensureBaseDir(self) -> None:
        try:
            pathStat = os.lstat(self.baseDir)
        except FileNotFoundError:
            try:
                self.baseDir.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                pass
            pathStat = os.lstat(self.baseDir)
        self._validateOwnedType(self.baseDir, pathStat, expected='directory')
        try:
            os.chmod(self.baseDir, 0o700, follow_symlinks=False)
        except (NotImplementedError, TypeError):
            os.chmod(self.baseDir, 0o700)

    def _validateOwnedType(self, path: Path, pathStat: os.stat_result, expected: str) -> None:
        if stat.S_ISLNK(pathStat.st_mode):
            raise credentialStoreError(f'凭据路径不能是符号链接：{path}')
        typeMatches = stat.S_ISDIR(pathStat.st_mode) if expected == 'directory' else stat.S_ISREG(pathStat.st_mode)
        if not typeMatches:
            raise credentialStoreError(f'凭据路径类型非法（期望 {expected}）：{path}')
        if pathStat.st_uid != os.getuid():
            raise credentialStoreError(f'凭据路径不属于当前用户：{path}')

    def _openFlags(self, baseFlags: int) -> int:
        return baseFlags | getattr(os, 'O_NOFOLLOW', 0)

    @contextmanager
    def _locked(self, provider: str) -> Iterator[None]:
        fileThreadLock, providerThreadLock = getThreadLock(self.lockPath, provider)
        with fileThreadLock, providerThreadLock:
            self._ensureBaseDir()
            try:
                lockFd = os.open(self.lockPath, self._openFlags(os.O_RDWR | os.O_CREAT), 0o600)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.EMLINK):
                    raise credentialStoreError(f'凭据锁文件不能是符号链接：{self.lockPath}') from error
                raise credentialStoreError(f'无法打开凭据锁文件：{error}') from error
            try:
                lockStat = os.fstat(lockFd)
                self._validateOwnedType(self.lockPath, lockStat, expected='file')
                os.fchmod(lockFd, 0o600)
                fcntl.flock(lockFd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(lockFd, fcntl.LOCK_UN)
                finally:
                    os.close(lockFd)

    def _readDocumentUnlocked(self) -> dict[str, Any]:
        try:
            pathStat = os.lstat(self.authPath)
        except FileNotFoundError:
            return {'version': 1, 'providers': {}}
        self._validateOwnedType(self.authPath, pathStat, expected='file')
        try:
            authFd = os.open(self.authPath, self._openFlags(os.O_RDONLY))
        except OSError as error:
            raise credentialStoreError(f'无法读取凭据文件：{error}') from error
        try:
            openedStat = os.fstat(authFd)
            self._validateOwnedType(self.authPath, openedStat, expected='file')
            if (openedStat.st_dev, openedStat.st_ino) != (pathStat.st_dev, pathStat.st_ino):
                raise credentialStoreError('凭据文件在读取期间被替换，请重试。')
            with os.fdopen(authFd, 'r', encoding='utf-8') as authFile:
                authFd = -1
                try:
                    document = json.load(authFile)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise credentialStoreError(f'凭据文件 JSON 已损坏，原文件已保留：{self.authPath}') from error
        finally:
            if authFd >= 0:
                os.close(authFd)
        if not isinstance(document, dict) or document.get('version') != 1:
            raise credentialStoreError('凭据文件必须是 version=1 的 JSON 对象。')
        providers = document.get('providers')
        if not isinstance(providers, dict):
            raise credentialStoreError('凭据文件 providers 必须是 JSON 对象。')
        return document

    def _writeDocumentUnlocked(self, document: dict[str, Any]) -> None:
        try:
            existingStat = os.lstat(self.authPath)
        except FileNotFoundError:
            existingStat = None
        if existingStat is not None:
            self._validateOwnedType(self.authPath, existingStat, expected='file')

        payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8') + b'\n'
        tempPath = self.baseDir / f'.auth.{os.getpid()}.{secrets.token_hex(8)}.tmp'
        tempFd = -1
        try:
            tempFd = os.open(
                tempPath,
                self._openFlags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                0o600,
            )
            os.fchmod(tempFd, 0o600)
            with os.fdopen(tempFd, 'wb') as tempFile:
                tempFd = -1
                tempFile.write(payload)
                tempFile.flush()
                os.fsync(tempFile.fileno())
            os.replace(tempPath, self.authPath)
            authStat = os.lstat(self.authPath)
            self._validateOwnedType(self.authPath, authStat, expected='file')
            os.chmod(self.authPath, 0o600, follow_symlinks=False)
            directoryFd = os.open(self.baseDir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directoryFd)
            finally:
                os.close(directoryFd)
        except OSError as error:
            raise credentialStoreError(f'写入凭据文件失败：{error}') from error
        finally:
            if tempFd >= 0:
                os.close(tempFd)
            try:
                tempPath.unlink()
            except FileNotFoundError:
                pass

    def _parseCredential(self, provider: str, value: Any) -> oauthCredential:
        if not isinstance(value, dict):
            raise credentialStoreError(f'Provider {provider} 的凭据必须是 JSON 对象。')
        expectedKeys = {'type', 'access', 'refresh', 'expires', 'accountId'}
        unknownKeys = set(value) - expectedKeys
        if unknownKeys:
            raise credentialStoreError(f'Provider {provider} 的凭据包含未知字段：{",".join(sorted(unknownKeys))}')
        access = value.get('access')
        refresh = value.get('refresh')
        expires = value.get('expires')
        accountId = value.get('accountId')
        if value.get('type') != 'oauth':
            raise credentialStoreError(f'Provider {provider} 的凭据 type 必须是 oauth。')
        if not isinstance(access, str) or not access:
            raise credentialStoreError(f'Provider {provider} 的 access 无效。')
        if not isinstance(refresh, str) or not refresh:
            raise credentialStoreError(f'Provider {provider} 的 refresh 无效。')
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires < 0:
            raise credentialStoreError(f'Provider {provider} 的 expires 无效。')
        if accountId is not None and (not isinstance(accountId, str) or not accountId):
            raise credentialStoreError(f'Provider {provider} 的 accountId 无效。')
        credential = oauthCredential(access=access, refresh=refresh, expires=float(expires), accountId=accountId)
        self._validateCredential(provider, credential)
        return credential

    def _validateCredential(self, provider: str, credential: oauthCredential) -> None:
        if not isinstance(credential, oauthCredential) or credential.type != 'oauth':
            raise credentialStoreError(f'Provider {provider} 的凭据类型无效。')
        if not credential.access or not credential.refresh:
            raise credentialStoreError(f'Provider {provider} 的 OAuth Token 不能为空。')
        if isinstance(credential.expires, bool) or not isinstance(credential.expires, (int, float)) or credential.expires < 0:
            raise credentialStoreError(f'Provider {provider} 的 expires 无效。')
        if provider == 'openai-codex' and not credential.accountId:
            raise credentialStoreError('OpenAI Codex 凭据缺少 accountId。')
        if credential.accountId is not None and not isinstance(credential.accountId, str):
            raise credentialStoreError(f'Provider {provider} 的 accountId 无效。')

    def _credentialToJson(self, credential: oauthCredential) -> dict[str, Any]:
        result: dict[str, Any] = {
            'type': 'oauth',
            'access': credential.access,
            'refresh': credential.refresh,
            'expires': credential.expires,
        }
        if credential.accountId:
            result['accountId'] = credential.accountId
        return result


defaultCredentialStore = credentialStore()


def readCredential(provider: str) -> oauthCredential | None:
    return defaultCredentialStore.readCredential(provider)


def writeCredential(provider: str, credential: oauthCredential) -> None:
    defaultCredentialStore.writeCredential(provider, credential)


def deleteCredential(provider: str) -> None:
    defaultCredentialStore.deleteCredential(provider)
