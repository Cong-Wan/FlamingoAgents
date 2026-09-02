'''
Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: Implements native Python OAuth login, device polling, refresh, cancellation, and concurrency-safe credential resolution for ChatGPT Codex and xAI subscriptions.
'''

from __future__ import annotations

import base64
import binascii
import errno
import fcntl
import hashlib
import json
import os
import queue
import re
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

from flamingoAgents.models.credentialStore import credentialStore, defaultCredentialStore, oauthCredential

openAiClientId = 'app_EMoamEEZ73f0CkXaXp7hrann'
openAiAuthBaseUrl = 'https://auth.openai.com'
openAiAuthorizeUrl = f'{openAiAuthBaseUrl}/oauth/authorize'
openAiTokenUrl = f'{openAiAuthBaseUrl}/oauth/token'
openAiRedirectUri = 'http://localhost:1455/auth/callback'
openAiDeviceUserCodeUrl = f'{openAiAuthBaseUrl}/api/accounts/deviceauth/usercode'
openAiDeviceTokenUrl = f'{openAiAuthBaseUrl}/api/accounts/deviceauth/token'
openAiDeviceVerificationUri = f'{openAiAuthBaseUrl}/codex/device'
openAiDeviceRedirectUri = f'{openAiAuthBaseUrl}/deviceauth/callback'
openAiScope = 'openid profile email offline_access'
openAiJwtClaimPath = 'https://api.openai.com/auth'
openAiOriginator = 'pi'

xaiClientId = 'b1a00492-073a-47ea-816f-4c329264a828'
xaiScope = 'openid profile email offline_access grok-cli:access api:access'
xaiDeviceCodeUrl = 'https://auth.x.ai/oauth2/device/code'
xaiTokenUrl = 'https://auth.x.ai/oauth2/token'
xaiReferrer = 'pi'

minimumValiditySeconds = 5 * 60
xaiRefreshSkewSeconds = 5 * 60
defaultTokenLifetimeSeconds = 3600
defaultPollIntervalSeconds = 5.0
minimumPollIntervalSeconds = 1.0
slowDownIncrementSeconds = 5.0
openAiDeviceTimeoutSeconds = 15 * 60
oauthHttpTimeoutSeconds = 30
browserLoginTimeoutSeconds = 15 * 60

sensitivePattern = re.compile(
    r'(?i)(access[_ -]?token|refresh[_ -]?token|authorization|code[_ -]?verifier|device[_ -]?code)'
    r'\s*[:=]\s*([^\s,;&]+)'
)


class modelAuthError(RuntimeError):
    def __init__(
        self,
        provider: str,
        action: str,
        statusCode: int | None = None,
        errorCode: str | None = None,
        detail: str | None = None,
    ):
        self.provider = provider
        self.action = action
        self.statusCode = statusCode
        self.retryable = False
        self.errorCode = errorCode
        self.detail = sanitizeErrorText(detail or '')
        parts = [f'{provider} 认证失败', f'action={action}']
        if statusCode is not None:
            parts.append(f'status={statusCode}')
        if errorCode:
            parts.append(f'code={sanitizeErrorText(errorCode)}')
        if self.detail:
            parts.append(self.detail)
        super().__init__('：'.join((parts[0], ' '.join(parts[1:]))))


@dataclass
class httpResult:
    statusCode: int
    body: dict[str, Any]


@dataclass
class openAiBrowserAuthorization:
    authUrl: str
    state: str = field(repr=False)
    verifier: str = field(repr=False)


@dataclass
class deviceCodeInfo:
    authUrl: str
    userCode: str
    intervalSeconds: float
    expiresInSeconds: float


class loginCancelledError(modelAuthError):
    def __init__(self, provider: str, action: str = 'login'):
        super().__init__(provider, action, errorCode='cancelled', detail='登录已取消。')


def sanitizeErrorText(value: str) -> str:
    text = re.sub(r'(?i)Bearer\s+[A-Za-z0-9._~+\-/]+=*', 'Bearer_<redacted>', str(value))
    text = sensitivePattern.sub(lambda match: f'{match.group(1)}=<redacted>', text)
    return text[:500]


def ensureNotCancelled(cancelEvent: threading.Event | None, provider: str, action: str = 'login') -> None:
    if cancelEvent is not None and cancelEvent.is_set():
        raise loginCancelledError(provider, action)


def base64UrlEncode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def generatePkce() -> tuple[str, str]:
    verifier = base64UrlEncode(secrets.token_bytes(32))
    challenge = base64UrlEncode(hashlib.sha256(verifier.encode('ascii')).digest())
    return verifier, challenge


def decodeJwtPayload(token: str) -> dict[str, Any] | None:
    parts = token.split('.')
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
        value = json.loads(decoded.decode('utf-8'))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def extractOpenAiAccountId(accessToken: str) -> str:
    payload = decodeJwtPayload(accessToken)
    authClaim = payload.get(openAiJwtClaimPath) if payload else None
    accountId = authClaim.get('chatgpt_account_id') if isinstance(authClaim, dict) else None
    if not isinstance(accountId, str) or not accountId:
        raise modelAuthError('openai-codex', 'extract-account', errorCode='missing_account_id')
    return accountId


def parseAuthorizationInput(rawInput: str) -> tuple[str | None, str | None]:
    value = rawInput.strip()
    if not value:
        return None, None
    try:
        parsedUrl = urllib.parse.urlsplit(value)
        if parsedUrl.scheme and parsedUrl.netloc:
            query = urllib.parse.parse_qs(parsedUrl.query)
            return _firstQueryValue(query, 'code'), _firstQueryValue(query, 'state')
    except ValueError:
        pass
    if '#' in value:
        code, state = value.split('#', 1)
        return code or None, state or None
    if 'code=' in value:
        query = urllib.parse.parse_qs(value, keep_blank_values=True)
        return _firstQueryValue(query, 'code'), _firstQueryValue(query, 'state')
    return value, None


def _firstQueryValue(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values and values[0] else None


def createOpenAiBrowserAuthorization() -> openAiBrowserAuthorization:
    verifier, challenge = generatePkce()
    state = secrets.token_hex(16)
    query = urllib.parse.urlencode({
        'response_type': 'code',
        'client_id': openAiClientId,
        'redirect_uri': openAiRedirectUri,
        'scope': openAiScope,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': state,
        'id_token_add_organizations': 'true',
        'codex_cli_simplified_flow': 'true',
        'originator': openAiOriginator,
    })
    return openAiBrowserAuthorization(
        authUrl=f'{openAiAuthorizeUrl}?{query}',
        state=state,
        verifier=verifier,
    )


browserCallbackThreadLock = threading.Lock()


class callbackLease:
    def __init__(self, store: credentialStore):
        self.store = store
        self.lockFd: int | None = None
        self.threadAcquired = False

    def acquire(self) -> bool:
        self.threadAcquired = browserCallbackThreadLock.acquire(blocking=False)
        if not self.threadAcquired:
            return False
        try:
            self.store._ensureBaseDir()
            lockPath = self.store.baseDir / 'oauthCallback.lock'
            lockFd = os.open(
                lockPath,
                os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0),
                0o600,
            )
            lockStat = os.fstat(lockFd)
            if not stat.S_ISREG(lockStat.st_mode) or lockStat.st_uid != os.getuid():
                os.close(lockFd)
                self.release()
                return False
            os.fchmod(lockFd, 0o600)
            try:
                fcntl.flock(lockFd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(lockFd)
                self.release()
                return False
            self.lockFd = lockFd
            return True
        except OSError:
            self.release()
            return False

    def release(self) -> None:
        if self.lockFd is not None:
            try:
                fcntl.flock(self.lockFd, fcntl.LOCK_UN)
            finally:
                os.close(self.lockFd)
                self.lockFd = None
        if self.threadAcquired:
            self.threadAcquired = False
            browserCallbackThreadLock.release()


class quietCallbackServer(HTTPServer):
    allow_reuse_address = False

    def handle_error(self, request, clientAddress) -> None:
        return


class openAiBrowserLogin:
    def __init__(
        self,
        store: credentialStore | None = None,
        host: str = '127.0.0.1',
        port: int = 1455,
    ):
        self.store = store or defaultCredentialStore
        self.authorization = createOpenAiBrowserAuthorization()
        self.resultQueue: queue.Queue[str] = queue.Queue(maxsize=1)
        self.lease = callbackLease(self.store)
        self.server: quietCallbackServer | None = None
        self.serverThread: threading.Thread | None = None
        self.closed = False
        self.closeLock = threading.Lock()
        self.manualCodeRequired = True
        if self.lease.acquire():
            try:
                handler = self._buildHandler()
                self.server = quietCallbackServer((host, port), handler)
                self.serverThread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.serverThread.start()
                self.manualCodeRequired = False
            except OSError:
                if self.server is not None:
                    self.server.server_close()
                    self.server = None
                self.lease.release()

    @property
    def authUrl(self) -> str:
        return self.authorization.authUrl

    def _buildHandler(self):
        login = self

        class callbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    parsed = urllib.parse.urlsplit(self.path)
                    if parsed.path != '/auth/callback':
                        self._reply(404, 'Callback route not found.')
                        return
                    query = urllib.parse.parse_qs(parsed.query)
                    state = _firstQueryValue(query, 'state')
                    if state != login.authorization.state:
                        self._reply(400, 'State mismatch.')
                        return
                    code = _firstQueryValue(query, 'code')
                    if not code:
                        self._reply(400, 'Missing authorization code.')
                        return
                    login._offerCode(code)
                    self._reply(200, 'OpenAI authentication completed. You can close this window.')
                except Exception:
                    self._reply(500, 'Internal error while processing OAuth callback.')

            def _reply(self, statusCode: int, message: str) -> None:
                body = (
                    '<!doctype html><html><head><meta charset="utf-8"></head>'
                    f'<body><p>{message}</p></body></html>'
                ).encode('utf-8')
                self.send_response(statusCode)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, formatValue: str, *args) -> None:
                return

        return callbackHandler

    def _offerCode(self, code: str) -> None:
        try:
            self.resultQueue.put_nowait(code)
        except queue.Full:
            pass

    def submitManualCode(self, rawInput: str) -> None:
        code, state = parseAuthorizationInput(rawInput)
        if state is not None and state != self.authorization.state:
            raise modelAuthError('openai-codex', 'callback', errorCode='state_mismatch')
        if not code:
            raise modelAuthError('openai-codex', 'callback', errorCode='missing_code')
        self._offerCode(code)

    def wait(
        self,
        cancelEvent: threading.Event | None = None,
        timeoutSeconds: float = browserLoginTimeoutSeconds,
    ) -> oauthCredential:
        deadline = time.monotonic() + timeoutSeconds
        try:
            while time.monotonic() < deadline:
                ensureNotCancelled(cancelEvent, 'openai-codex')
                try:
                    code = self.resultQueue.get(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
                    break
                except queue.Empty:
                    continue
            else:
                raise modelAuthError('openai-codex', 'callback', errorCode='timeout')
            ensureNotCancelled(cancelEvent, 'openai-codex')
            return exchangeOpenAiAuthorizationCode(
                code,
                self.authorization.verifier,
                redirectUri=openAiRedirectUri,
                cancelEvent=cancelEvent,
            )
        finally:
            self.close()

    def close(self) -> None:
        with self.closeLock:
            if self.closed:
                return
            self.closed = True
            if self.server is not None:
                try:
                    self.server.shutdown()
                finally:
                    self.server.server_close()
                self.server = None
            self.lease.release()


def startOpenAiBrowserLogin(store: credentialStore | None = None) -> openAiBrowserLogin:
    return openAiBrowserLogin(store=store)


def _requestJson(
    url: str,
    *,
    provider: str,
    action: str,
    form: dict[str, str] | None = None,
    jsonBody: dict[str, Any] | None = None,
    cancelEvent: threading.Event | None = None,
    timeoutSeconds: float = oauthHttpTimeoutSeconds,
    allowInvalidErrorBody: bool = False,
) -> httpResult:
    ensureNotCancelled(cancelEvent, provider, action)
    if (form is None) == (jsonBody is None):
        raise ValueError('form 与 jsonBody 必须且只能提供一个。')
    if form is not None:
        requestBody = urllib.parse.urlencode(form).encode('utf-8')
        contentType = 'application/x-www-form-urlencoded'
    else:
        requestBody = json.dumps(jsonBody, separators=(',', ':')).encode('utf-8')
        contentType = 'application/json'
    request = urllib.request.Request(
        url,
        data=requestBody,
        method='POST',
        headers={'Accept': 'application/json', 'Content-Type': contentType},
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeoutSeconds)
        with response:
            statusCode = int(getattr(response, 'status', response.getcode()))
            responseBytes = response.read(65536)
    except urllib.error.HTTPError as error:
        statusCode = error.code
        responseBytes = error.read(65536)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        ensureNotCancelled(cancelEvent, provider, action)
        reason = getattr(error, 'reason', None)
        detail = type(reason or error).__name__
        raise modelAuthError(provider, action, errorCode='network_error', detail=detail) from error
    ensureNotCancelled(cancelEvent, provider, action)
    try:
        parsed = json.loads(responseBytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        if allowInvalidErrorBody and not 200 <= statusCode < 300:
            parsed = {}
        else:
            raise modelAuthError(provider, action, statusCode=statusCode, errorCode='invalid_json') from error
    if not isinstance(parsed, dict):
        raise modelAuthError(provider, action, statusCode=statusCode, errorCode='invalid_response')
    return httpResult(statusCode=statusCode, body=parsed)


def _oauthFailure(provider: str, action: str, result: httpResult) -> modelAuthError:
    rawError = result.body.get('error')
    if isinstance(rawError, dict):
        errorCode = rawError.get('code') or rawError.get('type')
        detail = rawError.get('message')
    else:
        errorCode = rawError
        detail = result.body.get('error_description')
    return modelAuthError(
        provider,
        action,
        statusCode=result.statusCode,
        errorCode=errorCode if isinstance(errorCode, str) else None,
        detail=detail if isinstance(detail, str) else None,
    )


def _requiredString(body: dict[str, Any], fieldName: str, provider: str, action: str) -> str:
    value = body.get(fieldName)
    if not isinstance(value, str) or not value:
        raise modelAuthError(provider, action, errorCode=f'missing_{fieldName}')
    return value


def _positiveNumber(body: dict[str, Any], fieldName: str, provider: str, action: str) -> float:
    value = body.get(fieldName)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise modelAuthError(provider, action, errorCode=f'invalid_{fieldName}')
    return float(value)


def _openAiCredentialFromTokenBody(body: dict[str, Any], action: str, nowFn: Callable[[], float]) -> oauthCredential:
    access = _requiredString(body, 'access_token', 'openai-codex', action)
    refresh = _requiredString(body, 'refresh_token', 'openai-codex', action)
    expiresIn = _positiveNumber(body, 'expires_in', 'openai-codex', action)
    accountId = extractOpenAiAccountId(access)
    return oauthCredential(
        access=access,
        refresh=refresh,
        expires=nowFn() + expiresIn,
        accountId=accountId,
    )


def exchangeOpenAiAuthorizationCode(
    code: str,
    verifier: str,
    *,
    redirectUri: str,
    cancelEvent: threading.Event | None = None,
    nowFn: Callable[[], float] = time.time,
) -> oauthCredential:
    ensureNotCancelled(cancelEvent, 'openai-codex', 'token-exchange')
    result = _requestJson(
        openAiTokenUrl,
        provider='openai-codex',
        action='token-exchange',
        form={
            'grant_type': 'authorization_code',
            'client_id': openAiClientId,
            'code': code,
            'code_verifier': verifier,
            'redirect_uri': redirectUri,
        },
        cancelEvent=cancelEvent,
    )
    if not 200 <= result.statusCode < 300:
        raise _oauthFailure('openai-codex', 'token-exchange', result)
    ensureNotCancelled(cancelEvent, 'openai-codex', 'token-exchange')
    return _openAiCredentialFromTokenBody(result.body, 'token-exchange', nowFn)


def refreshOpenAiCredential(
    current: oauthCredential,
    *,
    cancelEvent: threading.Event | None = None,
    nowFn: Callable[[], float] = time.time,
) -> oauthCredential:
    result = _requestJson(
        openAiTokenUrl,
        provider='openai-codex',
        action='refresh',
        form={
            'grant_type': 'refresh_token',
            'refresh_token': current.refresh,
            'client_id': openAiClientId,
        },
        cancelEvent=cancelEvent,
    )
    if not 200 <= result.statusCode < 300:
        raise _oauthFailure('openai-codex', 'refresh', result)
    ensureNotCancelled(cancelEvent, 'openai-codex', 'refresh')
    return _openAiCredentialFromTokenBody(result.body, 'refresh', nowFn)


def _sleepWithCancel(
    seconds: float,
    cancelEvent: threading.Event | None,
    provider: str,
    sleepFn: Callable[[float], None],
) -> None:
    if seconds <= 0:
        ensureNotCancelled(cancelEvent, provider)
        return
    if cancelEvent is not None and sleepFn is time.sleep:
        if cancelEvent.wait(seconds):
            raise loginCancelledError(provider)
    else:
        sleepFn(seconds)
        ensureNotCancelled(cancelEvent, provider)


def pollDeviceCodeFlow(
    *,
    provider: str,
    poll: Callable[[], dict[str, Any]],
    intervalSeconds: float | None,
    expiresInSeconds: float,
    waitBeforeFirstPoll: bool,
    cancelEvent: threading.Event | None = None,
    sleepFn: Callable[[float], None] = time.sleep,
    nowFn: Callable[[], float] = time.monotonic,
) -> Any:
    rawInterval = defaultPollIntervalSeconds if intervalSeconds is None else float(intervalSeconds)
    interval = max(minimumPollIntervalSeconds, rawInterval)
    deadline = nowFn() + expiresInSeconds
    slowDownSeen = False
    if waitBeforeFirstPoll:
        _sleepWithCancel(min(interval, max(0.0, deadline - nowFn())), cancelEvent, provider, sleepFn)
    while nowFn() < deadline:
        ensureNotCancelled(cancelEvent, provider)
        result = poll()
        status = result.get('status')
        if status == 'complete':
            return result.get('value')
        if status == 'failed':
            message = result.get('message') if isinstance(result.get('message'), str) else None
            code = result.get('errorCode') if isinstance(result.get('errorCode'), str) else 'device_flow_failed'
            raise modelAuthError(provider, 'device-poll', errorCode=code, detail=message)
        if status == 'slow_down':
            slowDownSeen = True
            serverInterval = result.get('intervalSeconds')
            if isinstance(serverInterval, (int, float)) and not isinstance(serverInterval, bool) and serverInterval > 0:
                interval = max(minimumPollIntervalSeconds, float(serverInterval))
            else:
                interval += slowDownIncrementSeconds
        elif status != 'pending':
            raise modelAuthError(provider, 'device-poll', errorCode='invalid_poll_state')
        remaining = deadline - nowFn()
        if remaining <= 0:
            break
        _sleepWithCancel(min(interval, remaining), cancelEvent, provider, sleepFn)
    timeoutCode = 'slow_down_timeout' if slowDownSeen else 'timeout'
    raise modelAuthError(provider, 'device-poll', errorCode=timeoutCode)


def loginOpenAiDeviceCode(
    notify: Callable[[deviceCodeInfo], None],
    *,
    cancelEvent: threading.Event | None = None,
    sleepFn: Callable[[float], None] = time.sleep,
    nowFn: Callable[[], float] = time.monotonic,
    wallClockFn: Callable[[], float] = time.time,
) -> oauthCredential:
    result = _requestJson(
        openAiDeviceUserCodeUrl,
        provider='openai-codex',
        action='device-authorization',
        jsonBody={'client_id': openAiClientId},
        cancelEvent=cancelEvent,
    )
    if not 200 <= result.statusCode < 300:
        raise _oauthFailure('openai-codex', 'device-authorization', result)
    deviceAuthId = _requiredString(result.body, 'device_auth_id', 'openai-codex', 'device-authorization')
    userCode = _requiredString(result.body, 'user_code', 'openai-codex', 'device-authorization')
    rawInterval = result.body.get('interval')
    if isinstance(rawInterval, str):
        try:
            rawInterval = float(rawInterval.strip())
        except ValueError:
            rawInterval = None
    if isinstance(rawInterval, bool) or not isinstance(rawInterval, (int, float)) or rawInterval < 0:
        raise modelAuthError('openai-codex', 'device-authorization', errorCode='invalid_interval')
    interval = max(minimumPollIntervalSeconds, float(rawInterval))
    notify(deviceCodeInfo(
        authUrl=openAiDeviceVerificationUri,
        userCode=userCode,
        intervalSeconds=interval,
        expiresInSeconds=openAiDeviceTimeoutSeconds,
    ))

    def poll() -> dict[str, Any]:
        pollResult = _requestJson(
            openAiDeviceTokenUrl,
            provider='openai-codex',
            action='device-poll',
            jsonBody={'device_auth_id': deviceAuthId, 'user_code': userCode},
            cancelEvent=cancelEvent,
            allowInvalidErrorBody=True,
        )
        if 200 <= pollResult.statusCode < 300:
            authorizationCode = pollResult.body.get('authorization_code')
            codeVerifier = pollResult.body.get('code_verifier')
            if not isinstance(authorizationCode, str) or not authorizationCode or not isinstance(codeVerifier, str) or not codeVerifier:
                return {'status': 'failed', 'errorCode': 'invalid_device_token'}
            return {'status': 'complete', 'value': (authorizationCode, codeVerifier)}
        if pollResult.statusCode in (403, 404):
            return {'status': 'pending'}
        rawError = pollResult.body.get('error')
        errorCode = rawError.get('code') if isinstance(rawError, dict) else rawError
        if errorCode == 'deviceauth_authorization_pending':
            return {'status': 'pending'}
        if errorCode == 'slow_down':
            intervalValue = pollResult.body.get('interval')
            return {'status': 'slow_down', 'intervalSeconds': intervalValue}
        return {
            'status': 'failed',
            'errorCode': errorCode if isinstance(errorCode, str) else 'device_poll_failed',
            'message': f'HTTP {pollResult.statusCode}',
        }

    authorizationCode, codeVerifier = pollDeviceCodeFlow(
        provider='openai-codex',
        poll=poll,
        intervalSeconds=interval,
        expiresInSeconds=openAiDeviceTimeoutSeconds,
        waitBeforeFirstPoll=False,
        cancelEvent=cancelEvent,
        sleepFn=sleepFn,
        nowFn=nowFn,
    )
    ensureNotCancelled(cancelEvent, 'openai-codex')
    return exchangeOpenAiAuthorizationCode(
        authorizationCode,
        codeVerifier,
        redirectUri=openAiDeviceRedirectUri,
        cancelEvent=cancelEvent,
        nowFn=wallClockFn,
    )


def validateVerificationUri(rawUri: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(rawUri)
    except ValueError as error:
        raise modelAuthError('xai', 'device-authorization', errorCode='untrusted_verification_uri') from error
    if parsed.scheme != 'https' or not parsed.netloc:
        raise modelAuthError('xai', 'device-authorization', errorCode='untrusted_verification_uri')
    return urllib.parse.urlunsplit(parsed)


def _xaiCredentialFromTokenBody(
    body: dict[str, Any],
    action: str,
    previousRefresh: str | None,
    nowFn: Callable[[], float],
) -> oauthCredential:
    access = _requiredString(body, 'access_token', 'xai', action)
    rawRefresh = body.get('refresh_token')
    if rawRefresh is None and previousRefresh:
        refresh = previousRefresh
    else:
        refresh = _requiredString(body, 'refresh_token', 'xai', action)
    if 'expires_in' in body:
        expiresIn = _positiveNumber(body, 'expires_in', 'xai', action)
    else:
        expiresIn = defaultTokenLifetimeSeconds
    return oauthCredential(
        access=access,
        refresh=refresh,
        expires=nowFn() + expiresIn - xaiRefreshSkewSeconds,
    )


def loginXaiDeviceCode(
    notify: Callable[[deviceCodeInfo], None],
    *,
    cancelEvent: threading.Event | None = None,
    sleepFn: Callable[[float], None] = time.sleep,
    nowFn: Callable[[], float] = time.monotonic,
    wallClockFn: Callable[[], float] = time.time,
) -> oauthCredential:
    result = _requestJson(
        xaiDeviceCodeUrl,
        provider='xai',
        action='device-authorization',
        form={'client_id': xaiClientId, 'scope': xaiScope, 'referrer': xaiReferrer},
        cancelEvent=cancelEvent,
    )
    if not 200 <= result.statusCode < 300:
        raise _oauthFailure('xai', 'device-authorization', result)
    deviceCode = _requiredString(result.body, 'device_code', 'xai', 'device-authorization')
    userCode = _requiredString(result.body, 'user_code', 'xai', 'device-authorization')
    verificationUri = validateVerificationUri(_requiredString(
        result.body, 'verification_uri', 'xai', 'device-authorization',
    ))
    completeUri = result.body.get('verification_uri_complete')
    authUrl = validateVerificationUri(completeUri) if isinstance(completeUri, str) and completeUri else verificationUri
    expiresIn = _positiveNumber(result.body, 'expires_in', 'xai', 'device-authorization')
    rawInterval = result.body.get('interval')
    interval = float(rawInterval) if isinstance(rawInterval, (int, float)) and not isinstance(rawInterval, bool) and rawInterval > 0 else defaultPollIntervalSeconds
    interval = max(minimumPollIntervalSeconds, interval)
    notify(deviceCodeInfo(
        authUrl=authUrl,
        userCode=userCode,
        intervalSeconds=interval,
        expiresInSeconds=expiresIn,
    ))

    def poll() -> dict[str, Any]:
        pollResult = _requestJson(
            xaiTokenUrl,
            provider='xai',
            action='device-poll',
            form={
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                'client_id': xaiClientId,
                'device_code': deviceCode,
            },
            cancelEvent=cancelEvent,
        )
        if 200 <= pollResult.statusCode < 300:
            return {
                'status': 'complete',
                'value': _xaiCredentialFromTokenBody(pollResult.body, 'device-poll', None, wallClockFn),
            }
        errorCode = pollResult.body.get('error')
        if errorCode == 'authorization_pending':
            return {'status': 'pending'}
        if errorCode == 'slow_down':
            return {'status': 'slow_down', 'intervalSeconds': pollResult.body.get('interval')}
        if errorCode in ('access_denied', 'authorization_denied'):
            return {'status': 'failed', 'errorCode': 'access_denied', 'message': '设备授权被拒绝。'}
        if errorCode == 'expired_token':
            return {'status': 'failed', 'errorCode': 'expired_token', 'message': '设备码已过期，请重新登录。'}
        return {
            'status': 'failed',
            'errorCode': errorCode if isinstance(errorCode, str) else 'device_poll_failed',
            'message': f'HTTP {pollResult.statusCode}',
        }

    credential = pollDeviceCodeFlow(
        provider='xai',
        poll=poll,
        intervalSeconds=interval,
        expiresInSeconds=expiresIn,
        waitBeforeFirstPoll=True,
        cancelEvent=cancelEvent,
        sleepFn=sleepFn,
        nowFn=nowFn,
    )
    ensureNotCancelled(cancelEvent, 'xai')
    return credential


def refreshXaiCredential(
    current: oauthCredential,
    *,
    cancelEvent: threading.Event | None = None,
    nowFn: Callable[[], float] = time.time,
) -> oauthCredential:
    result = _requestJson(
        xaiTokenUrl,
        provider='xai',
        action='refresh',
        form={
            'grant_type': 'refresh_token',
            'client_id': xaiClientId,
            'refresh_token': current.refresh,
        },
        cancelEvent=cancelEvent,
    )
    if not 200 <= result.statusCode < 300:
        raise _oauthFailure('xai', 'refresh', result)
    ensureNotCancelled(cancelEvent, 'xai', 'refresh')
    return _xaiCredentialFromTokenBody(result.body, 'refresh', current.refresh, nowFn)


def refreshOAuthCredential(
    provider: str,
    current: oauthCredential,
    *,
    nowFn: Callable[[], float] = time.time,
) -> oauthCredential:
    if provider == 'openai-codex':
        return refreshOpenAiCredential(current, nowFn=nowFn)
    if provider == 'xai':
        return refreshXaiCredential(current, nowFn=nowFn)
    raise modelAuthError(provider, 'refresh', errorCode='unsupported_provider')


def resolveOAuthCredential(
    provider: str,
    forceRefresh: bool = False,
    staleAccess: str | None = None,
    *,
    store: credentialStore | None = None,
    nowFn: Callable[[], float] = time.time,
) -> oauthCredential:
    activeStore = store or defaultCredentialStore
    current = activeStore.readCredential(provider)
    if current is None:
        raise modelAuthError(provider, 'resolve', errorCode='not_logged_in', detail='请先登录。')
    if not forceRefresh and nowFn() + minimumValiditySeconds < current.expires:
        return current

    def refreshLocked(lockedCurrent: oauthCredential | None) -> oauthCredential | None:
        if lockedCurrent is None:
            raise modelAuthError(provider, 'resolve', errorCode='not_logged_in', detail='请先登录。')
        if forceRefresh:
            if staleAccess is not None and lockedCurrent.access != staleAccess:
                return None
        elif nowFn() + minimumValiditySeconds < lockedCurrent.expires:
            return None
        return refreshOAuthCredential(provider, lockedCurrent, nowFn=nowFn)

    resolved = activeStore.modifyCredential(provider, refreshLocked)
    if resolved is None:
        raise modelAuthError(provider, 'resolve', errorCode='not_logged_in', detail='请先登录。')
    return resolved
