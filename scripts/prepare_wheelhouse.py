"""Подготовить архив wheels для образа CPython 3.12.14 slim-bookworm на amd64."""

import argparse
import gzip
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from packaging.pylock import PackageWheel, Pylock
from packaging.tags import compatible_tags, cpython_tags


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "dist" / "coursegen-wheels-cp312-linux-amd64.tar.gz"


def linux_wheels(lock: Pylock) -> list[PackageWheel]:
    # Bookworm использует glibc 2.36. Добавляем также прежние имена manylinux.
    platforms = [f"manylinux_2_{minor}_x86_64" for minor in range(36, 4, -1)]
    platforms.extend(["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64", "linux_x86_64"])
    tags = [
        *cpython_tags((3, 12), ["cp312"], platforms),
        *compatible_tags((3, 12), "cp312", platforms),
    ]
    environment = {
        "implementation_name": "cpython", "platform_python_implementation": "CPython",
        "implementation_version": "3.12.14", "python_full_version": "3.12.14",
        "python_version": "3.12", "sys_platform": "linux", "os_name": "posix",
        "platform_machine": "x86_64", "platform_system": "Linux",
        "platform_release": "", "platform_version": "",
    }
    wheels = []
    for package, artifact in lock.select(environment=environment, tags=tags):
        if not isinstance(artifact, PackageWheel) or not artifact.url or "sha256" not in artifact.hashes:
            raise ValueError(f"Для {package.name} в lock-файле нет совместимого wheel с SHA-256.")
        wheels.append(artifact)
    return wheels


def download_verified(wheel: PackageWheel, directory: Path) -> Path:
    url = wheel.url
    if not url:
        raise ValueError("В lock-файле отсутствует URL для загрузки.")
    path = directory / Path(urlsplit(url).path).name
    with urlopen(url, timeout=60) as response, path.open("xb") as output:
        shutil.copyfileobj(response, output)
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != wheel.hashes["sha256"]:
        raise ValueError(f"SHA-256 файла {path.name} отличается от lock-файла.")
    return path


def prepare(uv: str, archive: Path) -> int:
    exported = subprocess.check_output(
        [uv, "export", "--locked", "--offline", "--no-dev", "--group", "test",
         "--no-emit-project", "--format", "pylock.toml"],
        cwd=ROOT,
        encoding="utf-8",
    )
    wheels = linux_wheels(Pylock.from_dict(tomllib.loads(exported)))
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=archive.parent) as workspace:
        directory = Path(workspace)
        with ThreadPoolExecutor(max_workers=6) as pool:
            files = sorted(pool.map(lambda wheel: download_verified(wheel, directory), wheels))
        temporary = directory / "bundle.tar.gz"
        with temporary.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|") as tar:
                for path in files:
                    info = tarfile.TarInfo(f"wheelhouse/{path.name}")
                    info.size = path.stat().st_size
                    with path.open("rb") as source:
                        tar.addfile(info, source)
        temporary.replace(archive)
    return len(wheels)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help="Путь к uv 0.13.9")
    args = parser.parse_args()
    count = prepare(args.uv, ARCHIVE)
    with ARCHIVE.open("rb") as data:
        checksum = hashlib.file_digest(data, "sha256").hexdigest()
    print(f"{ARCHIVE}\n{count} wheels\nSHA-256 {checksum}")
