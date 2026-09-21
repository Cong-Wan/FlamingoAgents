'''
Author: wilbur
Version: 1.0
Date: 2026-09-21
Description: Isolate usage ledger SQLite/JSONL roots per test so Core 落账不会写入真实 ~/.flamingo/logs。
'''

from __future__ import annotations

import pytest

from flamingoAgents.utils import usageLedger


@pytest.fixture(autouse=True)
def isolateUsageLedger(tmp_path, monkeypatch):
    monkeypatch.setattr(usageLedger, 'dbPath', tmp_path / 'usage-ledger-test.db')
    monkeypatch.setattr(usageLedger, 'logsRoot', tmp_path / 'usage-ledger-logs')
    monkeypatch.setattr(usageLedger, 'enableBackgroundRetry', False)
    usageLedger.resetState()
    yield
    usageLedger.resetState()
