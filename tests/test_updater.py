import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import updater as up  # noqa: E402

EXE = b"MZ-neue-version" * 1000
SHA = hashlib.sha256(EXE).hexdigest()


class R:
    def __init__(self, status=200, js=None, body=b"", headers=None):
        self.status_code, self._js, self.content = status, js, body
        self.text = body.decode("utf-8", "replace")
        self.headers = headers or {"Content-Length": str(len(body))}

    def json(self):
        return self._js

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    def __init__(self, tag="v1.2.0", exe=EXE, sha=SHA, status=200):
        self.tag, self.exe, self.sha, self.status = tag, exe, sha, status

    def get(self, url, headers=None, timeout=None, stream=False):
        assert headers["User-Agent"]
        if url == up.API_LATEST:
            if self.status != 200:
                return R(self.status, js={})
            return R(js={"tag_name": self.tag, "body": "- Neue Funktion", "html_url": "https://example/rel",
                         "assets": [{"name": f"ZeitTracker-{self.tag}.exe", "browser_download_url": "https://dl/exe"},
                                    {"name": f"ZeitTracker-{self.tag}.exe.sha256", "browser_download_url": "https://dl/sha"}]})
        if url == "https://dl/sha":
            return R(body=f"{self.sha}  ZeitTracker-{self.tag}.exe\r\n".encode())
        if url == "https://dl/exe":
            return R(body=self.exe)
        raise AssertionError(url)


def test_versions():
    assert up.is_newer("v1.2.0", "1.1.0")
    assert up.is_newer("v1.10.0", "1.9.3")
    assert not up.is_newer("v1.1.0", "1.1.0")
    assert not up.is_newer("v1.0.0", "1.1.0")
    assert not up.is_newer("v9.9.9", "dev")          # Entwicklerversion bietet keine Updates an
    assert not up.is_newer("nightly", "1.0.0")


def test_check(monkeypatch):
    monkeypatch.setattr(up, "__version__", "1.1.0")
    info = up.check(session=FakeGitHub())
    assert info["version"] == "1.2.0" and info["notes"] == "- Neue Funktion"
    assert info["exe_url"] == "https://dl/exe" and info["sha_url"] == "https://dl/sha"
    # is_newer nutzt den Standardwert -> direkt prüfen
    assert up.is_newer(info["tag"], "1.1.0")
    with pytest.raises(up.UpdateError, match="noch kein Release"):
        up.check(session=FakeGitHub(status=404))


def test_download_verifies_checksum(tmp_path):
    info = up.check(session=FakeGitHub())
    seen = []
    path = up.download(info, tmp_path, session=FakeGitHub(), progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == EXE and seen[-1] == (len(EXE), len(EXE))
    with pytest.raises(up.UpdateError, match="Prüfsumme stimmt nicht"):
        up.download(info, tmp_path, session=FakeGitHub(exe=b"manipuliert"))
    assert not list(tmp_path.glob("*.part"))


def test_install_swaps_exe_and_cleanup(tmp_path):
    cur = tmp_path / "ZeitTracker.exe"
    cur.write_bytes(b"alt")
    new = tmp_path / "ZeitTracker.new-1.2.0.exe"
    new.write_bytes(b"neu")
    old = up.install(new, current_exe=cur, launch=False)
    assert cur.read_bytes() == b"neu" and old.read_bytes() == b"alt" and not new.exists()
    up.cleanup_old(cur)
    assert not old.exists() and cur.exists()
