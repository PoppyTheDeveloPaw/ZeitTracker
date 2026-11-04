import sys
import types

import pytest

import winutil


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def reg(monkeypatch):
    """Ersetzt winreg durch ein Dict und simuliert die gebaute EXE."""
    store: dict = {}
    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(), KEY_READ=1, KEY_SET_VALUE=2, REG_SZ=1,
        OpenKey=lambda *a: FakeKey(),
        SetValueEx=lambda key, name, _r, _t, value: store.__setitem__(name, value),
        DeleteValue=lambda key, name: store.pop(name),
    )

    def query(key, name):
        if name not in store:
            raise FileNotFoundError(name)
        return store[name], 1
    fake.QueryValueEx = query

    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(winutil, "IS_WINDOWS", True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Tools\ZeitTracker-v1.2.0.exe")
    return store


def test_sync_updates_entry_after_rename(reg):
    reg[winutil.VALUE_NAME] = r'"C:\Tools\ZeitTracker-v1.0.0.exe"'
    assert winutil.sync_autostart(True) is True
    assert reg[winutil.VALUE_NAME] == r'"C:\Tools\ZeitTracker-v1.2.0.exe"'


def test_sync_leaves_matching_entry_alone(reg):
    reg[winutil.VALUE_NAME] = r'"c:\tools\zeittracker-v1.2.0.exe"'
    assert winutil.sync_autostart(True) is False


def test_sync_recreates_missing_entry(reg):
    assert winutil.sync_autostart(True) is True
    assert reg[winutil.VALUE_NAME] == r'"C:\Tools\ZeitTracker-v1.2.0.exe"'


def test_sync_does_nothing_when_disabled(reg):
    reg[winutil.VALUE_NAME] = r'"C:\Tools\ZeitTracker-v1.0.0.exe"'
    assert winutil.sync_autostart(False) is False
    assert reg[winutil.VALUE_NAME] == r'"C:\Tools\ZeitTracker-v1.0.0.exe"'


def test_sync_skipped_outside_frozen_exe(reg, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False)
    assert winutil.sync_autostart(True) is False
    assert reg == {}
