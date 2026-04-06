import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "install_camoufox.py"
_MODULE_SPEC = importlib.util.spec_from_file_location("install_camoufox_script", _MODULE_PATH)
install_camoufox = importlib.util.module_from_spec(_MODULE_SPEC)
assert _MODULE_SPEC and _MODULE_SPEC.loader
_MODULE_SPEC.loader.exec_module(install_camoufox)


class _FakeResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None):
        self._data = data
        self._offset = 0
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._offset
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class InstallCamoufoxTests(unittest.TestCase):
    def test_download_with_resume_appends_remaining_bytes(self):
        requests = []

        def _fake_urlopen(request, timeout=0):
            requests.append((request.full_url, dict(request.header_items()), timeout))
            headers = dict(request.header_items())
            if request.get_method() == "HEAD":
                return _FakeResponse(b"", headers={"Content-Length": "6"})
            if headers.get("Range") == "bytes=3-":
                return _FakeResponse(b"def")
            return _FakeResponse(b"abcdef")

        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "camoufox.zip"
            destination.write_bytes(b"abc")
            with patch.object(install_camoufox.urllib.request, "urlopen", side_effect=_fake_urlopen):
                install_camoufox._download_with_resume(
                    "https://example.invalid/camoufox.zip",
                    destination,
                    expected_size=6,
                    retries=1,
                    chunk_size=2,
                )

            self.assertEqual(destination.read_bytes(), b"abcdef")
            self.assertEqual(requests[0][1].get("Range"), "bytes=3-")

    def test_ensure_linux_glxtest_restores_missing_binary(self):
        with tempfile.TemporaryDirectory() as td:
            install_dir = Path(td) / "camoufox"
            install_dir.mkdir()
            source = Path(td) / "glxtest"
            source.write_bytes(b"binary")

            with patch.object(install_camoufox.os, "name", "posix"):
                with patch.object(install_camoufox, "_find_playwright_glxtest", return_value=source):
                    install_camoufox._ensure_linux_glxtest(install_dir)

            restored = install_dir / "glxtest"
            self.assertTrue(restored.exists())
            self.assertEqual(restored.read_bytes(), b"binary")

    def test_ensure_linux_glxtest_raises_when_no_fallback_exists(self):
        with tempfile.TemporaryDirectory() as td:
            install_dir = Path(td) / "camoufox"
            install_dir.mkdir()

            with patch.object(install_camoufox.os, "name", "posix"):
                with patch.object(install_camoufox, "_find_playwright_glxtest", return_value=None):
                    with self.assertRaisesRegex(RuntimeError, "missing glxtest"):
                        install_camoufox._ensure_linux_glxtest(install_dir)


if __name__ == "__main__":
    unittest.main()
