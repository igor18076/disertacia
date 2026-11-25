"""Подготовить локальную модель поиска на машине с доступом к Hugging Face."""

import gzip
import hashlib
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from coursegen.retrieval.model_spec import FILES, REPOSITORY, REVISION


def prepare() -> Path:
    model = ROOT / "models" / "rubert-tiny2-int8"
    archive = ROOT / "dist" / "coursegen-search-model.tar.gz"
    model.mkdir(parents=True, exist_ok=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=archive.parent) as tmp:
        for name, expected in FILES.items():
            path = model / Path(name).name
            if path.is_file() and checksum(path) == expected:
                continue
            staged = Path(tmp) / path.name
            url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}"
            with urlopen(url, timeout=90) as response, staged.open("wb") as output:
                shutil.copyfileobj(response, output)
            if checksum(staged) != expected:
                raise ValueError(f"Ошибка SHA-256 для {path.name}")
            staged.replace(path)
        staged = Path(tmp) / "model.tar.gz"
        with staged.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|") as tar:
                for name in sorted(FILES):
                    path = model / Path(name).name
                    info = tarfile.TarInfo(path.relative_to(ROOT).as_posix())
                    info.size = path.stat().st_size
                    with path.open("rb") as stream:
                        tar.addfile(info, stream)
        staged.replace(archive)
    return archive


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


if __name__ == "__main__":
    bundle = prepare()
    print(f"{bundle}\nSHA-256 {checksum(bundle)}")
