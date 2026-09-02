'''
Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: Verifies OAuth credential file permissions, strict corruption handling, unknown-provider preservation, symlink rejection, atomic concurrent writes, and secret-safe repr.
'''

from __future__ import annotations

import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from flamingoAgents.models.credentialStore import credentialStore, credentialStoreError, oauthCredential


def makeCredential(prefix: str, accountId: str | None = None) -> oauthCredential:
    return oauthCredential(
        access=f'{prefix}-access',
        refresh=f'{prefix}-refresh',
        expires=2_000_000_000,
        accountId=accountId,
    )


def writeProvider(baseDir: str, provider: str, prefix: str) -> None:
    store = credentialStore(baseDir)
    accountId = f'{prefix}-account' if provider == 'openai-codex' else None
    for _ in range(10):
        store.writeCredential(provider, makeCredential(prefix, accountId))


def testWritePermissionsRoundTripAndSafeRepr(tmp_path: Path) -> None:
    store = credentialStore(tmp_path / 'authHome')
    credential = makeCredential('secret', 'acct-1')

    store.writeCredential('openai-codex', credential)
    loaded = store.readCredential('openai-codex')

    assert loaded == credential
    assert stat.S_IMODE(os.lstat(store.baseDir).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(store.authPath).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(store.lockPath).st_mode) == 0o600
    assert 'secret-access' not in repr(loaded)
    assert 'secret-refresh' not in repr(loaded)


def testUnknownProviderIsPreservedWhenKnownCredentialChanges(tmp_path: Path) -> None:
    store = credentialStore(tmp_path / 'authHome')
    store.baseDir.mkdir(mode=0o700)
    store.authPath.write_text(json.dumps({
        'version': 1,
        'providers': {'future-provider': {'type': 'future', 'opaque': {'a': 1}}},
    }), encoding='utf-8')
    os.chmod(store.authPath, 0o600)

    store.writeCredential('xai', makeCredential('xai'))

    document = json.loads(store.authPath.read_text(encoding='utf-8'))
    assert document['providers']['future-provider'] == {'type': 'future', 'opaque': {'a': 1}}
    assert document['providers']['xai']['access'] == 'xai-access'


def testCorruptJsonIsNotOverwritten(tmp_path: Path) -> None:
    store = credentialStore(tmp_path / 'authHome')
    store.baseDir.mkdir(mode=0o700)
    store.authPath.write_text('{broken', encoding='utf-8')
    os.chmod(store.authPath, 0o600)

    with pytest.raises(credentialStoreError, match='JSON 已损坏'):
        store.writeCredential('xai', makeCredential('new'))

    assert store.authPath.read_text(encoding='utf-8') == '{broken'


def testSymlinkCredentialAndLockAreRejected(tmp_path: Path) -> None:
    baseDir = tmp_path / 'authHome'
    baseDir.mkdir(mode=0o700)
    target = tmp_path / 'target.json'
    target.write_text('{}', encoding='utf-8')
    os.symlink(target, baseDir / 'auth.json')
    store = credentialStore(baseDir)

    with pytest.raises(credentialStoreError, match='符号链接'):
        store.readCredential('xai')

    (baseDir / 'auth.json').unlink()
    (baseDir / 'auth.lock').unlink()
    os.symlink(target, baseDir / 'auth.lock')
    with pytest.raises(credentialStoreError, match='符号链接'):
        store.writeCredential('xai', makeCredential('xai'))


def testTwoProcessesDoNotLoseDifferentProviderWrites(tmp_path: Path) -> None:
    baseDir = str(tmp_path / 'authHome')
    context = multiprocessing.get_context('fork')
    first = context.Process(target=writeProvider, args=(baseDir, 'openai-codex', 'openai'))
    second = context.Process(target=writeProvider, args=(baseDir, 'xai', 'xai'))
    first.start()
    second.start()
    first.join(10)
    second.join(10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    store = credentialStore(baseDir)
    assert store.readCredential('openai-codex').access == 'openai-access'
    assert store.readCredential('xai').access == 'xai-access'
