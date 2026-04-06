import json
import os
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from platformdirs import user_cache_dir


def _probe_remote_size(url: str) -> int | None:
    request = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(request, timeout=60) as response:
        size = response.headers.get("Content-Length")
        return int(size) if size else None


def _download_with_resume(
    url: str,
    destination: Path,
    *,
    expected_size: int | None = None,
    retries: int = 5,
    chunk_size: int = 1024 * 1024,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if expected_size is None:
        expected_size = _probe_remote_size(url)

    for attempt in range(1, retries + 1):
        downloaded = destination.stat().st_size if destination.exists() else 0
        if expected_size and downloaded >= expected_size:
            break

        request = urllib.request.Request(url)
        if downloaded:
            request.add_header("Range", f"bytes={downloaded}-")

        mode = "ab" if downloaded else "wb"
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with destination.open(mode) as file_obj:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        file_obj.write(chunk)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(min(attempt * 3, 15))

    final_size = destination.stat().st_size if destination.exists() else 0
    if expected_size and final_size != expected_size:
        raise RuntimeError(
            f"download incomplete: expected {expected_size} bytes, got {final_size}"
        )


def _find_playwright_glxtest() -> Path | None:
    playwright_dir = Path(user_cache_dir("ms-playwright"))
    patterns = (
        "firefox-*/firefox/glxtest",
        "firefox*/firefox/glxtest",
        "firefox-*/glxtest",
        "firefox*/glxtest",
    )
    for pattern in patterns:
        for candidate in sorted(playwright_dir.glob(pattern), reverse=True):
            if candidate.is_file():
                return candidate
    return None


def _ensure_linux_glxtest(install_dir: Path) -> None:
    if os.name == "nt":
        return

    glxtest_path = install_dir / "glxtest"
    if glxtest_path.exists():
        return

    source = _find_playwright_glxtest()
    if source is None:
        raise RuntimeError(
            "Camoufox install is missing glxtest and no fallback source was found "
            "in the Playwright Firefox cache"
        )

    print(f"Restoring missing glxtest from Playwright Firefox cache: {source}")
    shutil.copy2(source, glxtest_path)
    glxtest_path.chmod(0o755)


def main() -> None:
    version = os.environ["CAMOUFOX_VERSION"]
    release = os.environ["CAMOUFOX_RELEASE"]
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "i686",
        "i686": "i686",
        "x86": "i686",
    }
    machine = os.uname().machine.lower()
    arch = arch_map.get(machine)
    if not arch:
        raise SystemExit(f"Unsupported Camoufox arch: {machine}")

    tag = f"v{version}-{release}"
    asset_name = f"camoufox-{version}-{release}-lin.{arch}.zip"
    asset_url = f"https://github.com/daijro/camoufox/releases/download/{tag}/{asset_name}"
    addon_url = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"
    install_dir = Path(user_cache_dir("camoufox"))
    temp_dir = Path(tempfile.mkdtemp(prefix="camoufox-install-"))

    try:
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)

        archive_path = temp_dir / asset_name
        print(f"Downloading Camoufox package: {asset_url}")
        _download_with_resume(asset_url, archive_path)
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(install_dir)

        version_path = install_dir / "version.json"
        version_path.write_text(
            json.dumps({"version": version, "release": release}),
            encoding="utf-8",
        )
        _ensure_linux_glxtest(install_dir)

        addon_dir = install_dir / "addons" / "UBO"
        addon_dir.mkdir(parents=True, exist_ok=True)
        addon_path = temp_dir / "ublock-origin.xpi"
        print(f"Downloading default addon UBO: {addon_url}")
        _download_with_resume(addon_url, addon_path, retries=3)
        with zipfile.ZipFile(addon_path) as zf:
            zf.extractall(addon_dir)

        for path in install_dir.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o644)

        binary = install_dir / "camoufox-bin"
        if binary.exists():
            binary.chmod(0o755)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
