"""Проверки утилиты Windows/Debian. Запуск: python tests/scripts/check_wheelhouse.py."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from packaging.pylock import PackageWheel, Pylock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from prepare_wheelhouse import download_verified, linux_wheels


class WheelhouseTest(unittest.TestCase):
    def test_target_selects_linux_wheel_and_evaluates_markers(self):
        names = [
            "native-1-cp312-cp312-win_amd64.whl",
            "native-1-cp313-cp313-manylinux_2_28_x86_64.whl",
            "native-1-cp310-abi3-manylinux2014_x86_64.whl",
        ]
        wheels = [{"url": "https://example.org/" + name, "hashes": {"sha256": "0" * 64}} for name in names]
        lock = Pylock.from_dict({
            "lock-version": "1.0", "created-by": "test",
            "packages": [
                {"name": "native", "version": "1", "wheels": wheels},
                {"name": "win", "version": "1", "marker": "sys_platform == 'win32'",
                 "wheels": [{"url": "https://example.org/win-1-py3-none-any.whl",
                             "hashes": {"sha256": "0" * 64}}]},
            ],
        })
        self.assertEqual([w.url for w in linux_wheels(lock)], [wheels[-1]["url"]])

    def test_rejects_changed_download(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.whl"
            source.write_bytes(b"wheel bytes")
            downloads = Path(directory) / "downloads"
            downloads.mkdir()
            wheel = PackageWheel(url=source.as_uri(), hashes={"sha256": hashlib.sha256(b"old").hexdigest()})
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                download_verified(wheel, downloads)


if __name__ == "__main__":
    unittest.main()
