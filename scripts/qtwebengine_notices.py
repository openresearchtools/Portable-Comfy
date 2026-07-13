#!/usr/bin/env python3
"""Mirror and verify Qt WebEngine's versioned third-party attribution pages."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path


LINK = re.compile(r"^[A-Za-z0-9._-]+(?:-attribution-|-3rdparty-)[A-Za-z0-9._-]+\.html$")
HEX = re.compile(r"^[0-9a-f]{64}$")
MAX_PAGE_BYTES = 4 * 1024 * 1024
MODULE_LICENSE_NAMES = (
    "Apache-2.0.txt",
    "BSD-3-Clause.txt",
    "CC0-1.0.txt",
    "GFDL-1.3-no-invariants-only.txt",
    "GPL-2.0-only.txt",
    "GPL-3.0-only.txt",
    "LGPL-2.0-or-later.txt",
    "LGPL-3.0-only.txt",
    "LicenseRef-Qt-Commercial.txt",
    "LicenseRef-Tango-Icons-Public-Domain.txt",
    "MIT.txt",
    "Qt-GPL-exception-1.0.txt",
)


def fail(message: str) -> None:
    raise SystemExit(f"invalid Qt WebEngine notice mirror: {message}")


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = next((value for key, value in attrs if key.lower() == "href"), None)
        if href is not None and LINK.fullmatch(href):
            self.values.add(href)


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Portable-Comfy/1"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = response.read(MAX_PAGE_BYTES + 1)
            if not value or len(value) > MAX_PAGE_BYTES:
                fail(f"download is empty or too large: {url}")
            return value
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    fail(f"could not download {url}: {last_error}")


def checksum_manifest(pages: dict[str, bytes]) -> bytes:
    return "".join(
        f"{hashlib.sha256(value).hexdigest()}  {name}\n"
        for name, value in sorted(pages.items())
    ).encode()


def verify(
    destination: Path,
    *,
    source_url: str,
    webengine_url: str,
    main_sha256: str,
    webengine_sha256: str,
    manifest_sha256: str,
    module_license_manifest_sha256: str,
    expected_linked_pages: int,
    qt_version: str,
    qtwebengine_commit: str,
    chromium_commit: str,
) -> None:
    checksums = destination / "SHA256SUMS"
    provenance = destination / "provenance.json"
    try:
        checksum_bytes = checksums.read_bytes()
        metadata = json.loads(provenance.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(str(error))
    if hashlib.sha256(checksum_bytes).hexdigest() != manifest_sha256:
        fail("SHA256SUMS does not match the pinned mirror")
    wanted_metadata = {
        "schema_version": 1,
        "qt_version": qt_version,
        "qtwebengine_commit": qtwebengine_commit,
        "chromium_commit": chromium_commit,
        "source_url": source_url,
        "webengine_source_url": webengine_url,
        "main_sha256": main_sha256,
        "webengine_sha256": webengine_sha256,
        "html_manifest_sha256": manifest_sha256,
        "module_license_manifest_sha256": module_license_manifest_sha256,
        "html_pages": expected_linked_pages + 2,
    }
    if metadata != wanted_metadata:
        fail("provenance disagrees with the pinned Qt WebEngine source")
    expected: dict[str, str] = {}
    for line in checksum_bytes.decode("ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            separator != "  "
            or not HEX.fullmatch(digest)
            or (
                name not in {"licenses-used-in-qt.html", "qtwebengine-licensing.html"}
                and not LINK.fullmatch(name)
            )
            or name in expected
        ):
            fail("SHA256SUMS contains a malformed entry")
        expected[name] = digest
    if len(expected) != expected_linked_pages + 2:
        fail("mirror contains the wrong number of attribution pages")
    actual = {path.name for path in destination.glob("*.html") if path.is_file()}
    if actual != set(expected):
        fail("HTML page set disagrees with SHA256SUMS")
    for name, digest in expected.items():
        if hashlib.sha256((destination / name).read_bytes()).hexdigest() != digest:
            fail(f"page checksum mismatch: {name}")

    module_root = destination / "MODULE-LICENSES"
    module_checksums = module_root / "SHA256SUMS"
    try:
        module_checksum_bytes = module_checksums.read_bytes()
    except OSError as error:
        fail(str(error))
    if (
        hashlib.sha256(module_checksum_bytes).hexdigest()
        != module_license_manifest_sha256
    ):
        fail("module license SHA256SUMS does not match the pinned source")
    module_expected: dict[str, str] = {}
    for line in module_checksum_bytes.decode("ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            separator != "  "
            or not HEX.fullmatch(digest)
            or name not in MODULE_LICENSE_NAMES
            or name in module_expected
        ):
            fail("module license SHA256SUMS contains a malformed entry")
        module_expected[name] = digest
    if set(module_expected) != set(MODULE_LICENSE_NAMES):
        fail("Qt WebEngine module license set is incomplete")
    actual_module_files = {
        path.name for path in module_root.glob("*.txt") if path.is_file()
    }
    if actual_module_files != set(module_expected):
        fail("Qt WebEngine module license files disagree with SHA256SUMS")
    for name, digest in module_expected.items():
        if hashlib.sha256((module_root / name).read_bytes()).hexdigest() != digest:
            fail(f"module license checksum mismatch: {name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--webengine-url", required=True)
    parser.add_argument("--main-sha256", required=True)
    parser.add_argument("--webengine-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--module-license-manifest-sha256", required=True)
    parser.add_argument("--expected-linked-pages", type=int, required=True)
    parser.add_argument("--qt-version", required=True)
    parser.add_argument("--qtwebengine-commit", required=True)
    parser.add_argument("--chromium-commit", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if not all(
        HEX.fullmatch(value)
        for value in (
            args.main_sha256,
            args.webengine_sha256,
            args.manifest_sha256,
            args.module_license_manifest_sha256,
        )
    ):
        parser.error("checksums must be lowercase SHA-256 values")
    if args.expected_linked_pages <= 0:
        parser.error("expected page count must be positive")

    destination = args.destination.resolve()
    if not args.verify_only:
        main_name = "licenses-used-in-qt.html"
        main_page = download(args.url)
        if hashlib.sha256(main_page).hexdigest() != args.main_sha256:
            fail("main licensing page checksum changed")
        webengine_name = "qtwebengine-licensing.html"
        webengine_page = download(args.webengine_url)
        if hashlib.sha256(webengine_page).hexdigest() != args.webengine_sha256:
            fail("Qt WebEngine licensing page checksum changed")
        links = Links()
        links.feed(main_page.decode("utf-8"))
        links.feed(webengine_page.decode("utf-8"))
        if len(links.values) != args.expected_linked_pages:
            fail("main page contains an unexpected attribution link set")

        base = args.url.rsplit("/", 1)[0] + "/"
        with ThreadPoolExecutor(max_workers=16) as executor:
            values = executor.map(
                download,
                [urllib.parse.urljoin(base, name) for name in sorted(links.values)],
            )
        pages = {
            main_name: main_page,
            webengine_name: webengine_page,
            **dict(zip(sorted(links.values), values)),
        }
        checksum_bytes = checksum_manifest(pages)
        if hashlib.sha256(checksum_bytes).hexdigest() != args.manifest_sha256:
            fail("downloaded attribution pages changed from the pinned mirror")

        license_base = (
            "https://raw.githubusercontent.com/qt/qtwebengine/"
            f"{args.qtwebengine_commit}/LICENSES/"
        )
        with ThreadPoolExecutor(max_workers=12) as executor:
            license_values = executor.map(
                download,
                [
                    urllib.parse.urljoin(license_base, name)
                    for name in MODULE_LICENSE_NAMES
                ],
            )
        module_licenses = dict(zip(MODULE_LICENSE_NAMES, license_values))
        module_checksum_bytes = checksum_manifest(module_licenses)
        if (
            hashlib.sha256(module_checksum_bytes).hexdigest()
            != args.module_license_manifest_sha256
        ):
            fail("Qt WebEngine module licenses changed from the pinned source")

        temporary = destination.with_name(f".{destination.name}.tmp")
        shutil.rmtree(temporary, ignore_errors=True)
        temporary.mkdir(parents=True)
        try:
            for name, value in pages.items():
                (temporary / name).write_bytes(value)
            (temporary / "SHA256SUMS").write_bytes(checksum_bytes)
            module_root = temporary / "MODULE-LICENSES"
            module_root.mkdir()
            for name, value in module_licenses.items():
                (module_root / name).write_bytes(value)
            (module_root / "SHA256SUMS").write_bytes(module_checksum_bytes)
            (temporary / "provenance.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "qt_version": args.qt_version,
                        "qtwebengine_commit": args.qtwebengine_commit,
                        "chromium_commit": args.chromium_commit,
                        "source_url": args.url,
                        "webengine_source_url": args.webengine_url,
                        "main_sha256": args.main_sha256,
                        "webengine_sha256": args.webengine_sha256,
                        "html_manifest_sha256": args.manifest_sha256,
                        "module_license_manifest_sha256": (
                            args.module_license_manifest_sha256
                        ),
                        "html_pages": len(pages),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            shutil.rmtree(destination, ignore_errors=True)
            temporary.replace(destination)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    verify(
        destination,
        source_url=args.url,
        webengine_url=args.webengine_url,
        main_sha256=args.main_sha256,
        webengine_sha256=args.webengine_sha256,
        manifest_sha256=args.manifest_sha256,
        module_license_manifest_sha256=args.module_license_manifest_sha256,
        expected_linked_pages=args.expected_linked_pages,
        qt_version=args.qt_version,
        qtwebengine_commit=args.qtwebengine_commit,
        chromium_commit=args.chromium_commit,
    )
    print(
        f"verified Qt {args.qt_version} notice mirror: "
        f"{args.expected_linked_pages + 2} HTML pages"
    )


if __name__ == "__main__":
    main()
