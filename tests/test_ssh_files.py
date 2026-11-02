"""SSH SFTP 文件操作的解析层测试（假 SFTP 客户端，不依赖真实服务器）。

运行：.venv/bin/python tests/test_ssh_files.py
"""
import base64
import io
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import ssh_session  # noqa: E402


class _Attr:
    def __init__(self, filename, mode, size=0, mtime=0):
        self.filename = filename
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


class _Handle(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _FakeSFTP:
    def __init__(self):
        self.files = {
            "/home/u/readme.txt": b"hello world\n",
            "/home/u/binary.bin": b"\x00\x01\x02",
            "/home/u/photo.png": b"\x89PNG\r\n\x1a\n",
        }
        self.closed = False

    def get_channel(self):
        return None

    def normalize(self, path):
        if path in ("", ".", "~"):
            return "/home/u"
        return path

    def listdir_attr(self, path):
        if path != "/home/u":
            raise IOError("No such directory")
        return [
            _Attr("zeta.txt", stat.S_IFREG | 0o644, 12, 100),
            _Attr("alpha", stat.S_IFDIR | 0o755, 0, 200),
            _Attr("readme.txt", stat.S_IFREG | 0o644, 12, 300),
        ]

    def stat(self, path):
        if path == "/home/u/alpha":
            return _Attr("alpha", stat.S_IFDIR | 0o755)
        data = self.files.get(path)
        if data is None:
            raise IOError("No such file")
        return _Attr(Path(path).name, stat.S_IFREG | 0o644, len(data), 400)

    def lstat(self, path):
        return self.stat(path)

    def remove(self, path):
        if path not in self.files:
            raise IOError("No such file")
        del self.files[path]

    def rmdir(self, path):
        raise IOError("Directory not empty")

    def open(self, path, mode):
        if mode == "rb":
            data = self.files.get(path)
            if data is None:
                raise IOError("No such file")
            return _Handle(data)
        return _Handle()

    def close(self):
        self.closed = True


class _FakeClient:
    def __init__(self):
        self.sftp = _FakeSFTP()
        self.closed = False

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


def _session():
    client = _FakeClient()
    session = ssh_session._Session("test", client, None, {"username": "u", "host": "h"})
    session.mode = "files"
    return session, client


def test_list_dir_sorts_and_reports_parent():
    session, _ = _session()
    result = session.list_dir("")
    assert result["ok"] is True
    assert result["path"] == "/home/u"
    assert result["home"] == "/home/u"
    assert result["parent"] == "/home"
    names = [entry["name"] for entry in result["entries"]]
    assert names == ["alpha", "readme.txt", "zeta.txt"]
    assert result["entries"][0]["is_dir"] is True


def test_read_text_and_binary_and_truncation():
    session, _ = _session()
    text = session.read_file("/home/u/readme.txt")
    assert text["kind"] == "text"
    assert base64.b64decode(text["content_b64"]) == b"hello world\n"
    assert text["truncated"] is False

    truncated = session.read_file("/home/u/readme.txt", max_bytes=5)
    assert truncated["truncated"] is True
    assert base64.b64decode(truncated["content_b64"]) == b"hello"

    binary = session.read_file("/home/u/binary.bin")
    assert binary["kind"] == "binary"

    image = session.read_file("/home/u/photo.png")
    assert image["kind"] == "image"


def test_write_file_and_validation():
    session, _ = _session()
    content = base64.b64encode(b"uploaded").decode()
    result = session.write_file("/home/u", "new.txt", content)
    assert result == {"ok": True, "path": "/home/u/new.txt", "size": 8}

    try:
        session.write_file("/home/u", "../evil", content)
    except ssh_session.SshError as exc:
        assert "文件名不合法" in str(exc)
    else:
        raise AssertionError("非法文件名未被拒绝")

    try:
        session.write_file("/home/u", "bad.txt", "not-base64!!")
    except ssh_session.SshError as exc:
        assert "base64" in str(exc)
    else:
        raise AssertionError("非法 base64 未被拒绝")


def test_download_to_local_file():
    import tempfile

    session, _ = _session()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "saved.txt"
        result = session.download_to("/home/u/readme.txt", str(local))
        assert result["ok"] is True
        assert result["size"] == 12
        assert local.read_bytes() == b"hello world\n"


def test_delete_file_and_guard_root():
    session, client = _session()
    result = session.delete_path("/home/u/readme.txt")
    assert result["ok"] is True and result["deleted"] == 1
    assert "/home/u/readme.txt" not in client.sftp.files

    for path in ("", "/"):
        try:
            session.delete_path(path)
        except ssh_session.SshError as exc:
            assert ("根目录" in str(exc)) or ("有效路径" in str(exc))
        else:
            raise AssertionError("根目录 / 空路径未被拒绝")

    try:
        session.delete_path("/home/u/alpha")
    except ssh_session.SshError as exc:
        assert "递归" in str(exc)
    else:
        raise AssertionError("非空目录未被拒绝")


def test_close_closes_sftp():
    session, client = _session()
    session.list_dir("")
    assert session.sftp is client.sftp
    session.close()
    assert session.sftp.closed is True
    assert client.closed is True


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
