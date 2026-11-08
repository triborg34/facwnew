"""Download the three ONNX models FACW needs (build-time only).

    python scripts/fetch_models.py [--dest models]

Needs internet; the running service never does.  Files are fetched from the
opencv_zoo repository via its LFS media endpoint (the raw endpoint returns an
LFS pointer stub, not the model).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (filename, url, minimum plausible size in bytes)
MODELS = [
    (
        "yunet.onnx",
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        100_000,
    ),
    (
        "sface.onnx",
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        20_000_000,
    ),
    (
        "persondet.onnx",
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/person_detection_mediapipe/person_detection_mediapipe_2023mar_int8bq.onnx",
        1_000_000,
    ),
]


def _download(url: str, dest: Path, min_size: int) -> None:
    print(f"  fetching {dest.name} ...", end="", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    size = tmp.stat().st_size
    if size < min_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"{dest.name} is only {size} bytes — that is an LFS pointer, not the model"
        )
    tmp.replace(dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:12]
    print(f" ok ({size // 1024} KB, sha256 {digest}…)")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch FACW model files")
    ap.add_argument("--dest", default=str(ROOT / "models"))
    args = ap.parse_args(argv)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name, url, min_size in MODELS:
        target = dest / name
        if target.exists() and target.stat().st_size >= min_size:
            print(f"  {name} already present ({target.stat().st_size // 1024} KB)")
            continue
        try:
            _download(url, target, min_size)
        except Exception as exc:  # noqa: BLE001
            print(f" FAILED: {exc}")
            failed += 1
    if failed:
        print(f"\n{failed} file(s) could not be fetched.", file=sys.stderr)
        return 1
    print(f"\nmodels ready in {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
