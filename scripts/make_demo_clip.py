"""Generate a synthetic walk-through clip for demos and tests.

Renders a neutral hallway scene (no red blocks, no stock photography) with a
face composited from a fixture image moving toward the camera, so the pipeline
has something realistic to detect, track and match.

    python scripts/make_demo_clip.py [--seconds 12] [--out data/temp/livecam.avi]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent


def _face_square(img: np.ndarray) -> np.ndarray:
    """Return a square crop centred on the largest detected face."""
    try:
        from facw.vision.face import FaceEngine, best_face

        eng = FaceEngine(ROOT / "models")
        faces = eng.detect(img, 0.3)
        if faces:
            f = best_face(faces)
            cx, cy = int(f.x + f.w / 2), int(f.y + f.h / 2)
            side = int(max(f.w, f.h) * 1.45)      # include hair/chin, keep it square
            side = max(32, min(side, min(img.shape[0], img.shape[1])))
            x0 = max(0, min(cx - side // 2, img.shape[1] - side))
            y0 = max(0, min(cy - side // 2, img.shape[0] - side))
            return img[y0 : y0 + side, x0 : x0 + side]
    except Exception:
        pass
    h, w = img.shape[:2]
    side = min(h, w)
    return img[(h - side) // 2 : (h + side) // 2, (w - side) // 2 : (w + side) // 2]


def build(out: Path, seconds: float, fps: float, face_path: Path) -> None:
    w, h = 960, 540
    face_img = cv2.imread(str(face_path))
    if face_img is None:
        raise SystemExit(f"cannot read {face_path}")
    # Crop tightly around the detected face (plus a small margin).  Using the
    # whole photo would paste a full portrait into the head ellipse, leaving the
    # actual face only a few dozen pixels wide - below the quality gate, so the
    # demo would never identify anybody.
    face_img = _face_square(face_img)

    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    vw = cv2.VideoWriter(str(out), fourcc, fps, (w, h))
    if not vw.isOpened():
        raise SystemExit(f"cannot open writer for {out}")

    total = int(seconds * fps)
    for i in range(total):
        # ------- background: neutral hallway with perspective floor
        frame = np.full((h, w, 3), 214, dtype=np.uint8)
        # ceiling band
        cv2.rectangle(frame, (0, 0), (w, 70), (198, 200, 204), -1)
        # back wall
        cv2.rectangle(frame, (0, 70), (w, h - 150), (226, 228, 232), -1)
        # floor with subtle perspective lines
        cv2.rectangle(frame, (0, h - 150), (w, h), (176, 180, 188), -1)
        for k in range(-4, 14):
            x_top = int(w / 2 + k * 26)
            x_bot = int(w / 2 + k * 120)
            cv2.line(frame, (x_top, h - 150), (x_bot, h), (166, 170, 178), 1)
        # wall trim + a door frame for context
        cv2.line(frame, (0, h - 152), (w, h - 152), (150, 154, 162), 2)
        cv2.rectangle(frame, (60, 110), (250, h - 152), (150, 152, 158), 3)
        cv2.rectangle(frame, (66, 116), (244, h - 158), (205, 208, 214), -1)
        # light fixtures
        for lx in (w // 4, (3 * w) // 4):
            cv2.ellipse(frame, (lx, 40), (70, 16), 0, 0, 360, (236, 238, 240), -1)

        # ------- walking person: grows as they approach
        t = i / max(1, total - 1)
        scale = 0.72 + 0.34 * t                      # 72% -> 106% of frame height
        body_h = int(h * scale)
        cx = int(w * (0.34 + 0.34 * t))              # left -> right
        top = int((h - 40) - body_h)
        # The pasted crop includes hair/shoulders, so the detected face box ends
        # up roughly a third of the head diameter; scale generously so it clears
        # the default 52 px quality gate throughout the walk.
        head_r = max(48, int(body_h * 0.26))
        head_c = (cx, top + head_r + int(body_h * 0.02))

        # torso + legs in muted clothing colours
        shoulder_y = head_c[1] + head_r + int(body_h * 0.03)
        body_w = int(body_h * 0.26)
        cv2.rectangle(
            frame,
            (cx - body_w // 2, shoulder_y),
            (cx + body_w // 2, shoulder_y + int(body_h * 0.32)),
            (92, 104, 126),  # slate jacket
            -1,
        )
        leg_y = shoulder_y + int(body_h * 0.32)
        stride = int(body_h * 0.035) * (1 if (i // 6) % 2 == 0 else -1)
        cv2.line(frame, (cx - body_w // 4, leg_y), (cx - body_w // 3 + stride, h - 40), (58, 62, 74), max(3, body_w // 6))
        cv2.line(frame, (cx + body_w // 4, leg_y), (cx + body_w // 3 - stride, h - 40), (58, 62, 74), max(3, body_w // 6))
        # arms
        cv2.line(frame, (cx - body_w // 2, shoulder_y + 6), (cx - body_w // 2 - int(body_w * 0.3), leg_y), (92, 104, 126), max(3, body_w // 7))
        cv2.line(frame, (cx + body_w // 2, shoulder_y + 6), (cx + body_w // 2 + int(body_w * 0.3), leg_y), (92, 104, 126), max(3, body_w // 7))

        # ------- face: real photo, scaled and blended into an ellipse
        face_size = head_r * 2
        if face_size > 8:
            patch = cv2.resize(face_img, (face_size, face_size), interpolation=cv2.INTER_AREA)
            mask = np.zeros((face_size, face_size), dtype=np.uint8)
            cv2.ellipse(mask, (face_size // 2, face_size // 2), (face_size // 2 - 1, face_size // 2 - 1), 0, 0, 360, 255, -1)
            x0 = head_c[0] - face_size // 2
            y0 = head_c[1] - face_size // 2
            x0c, y0c = max(0, x0), max(0, y0)
            x1c, y1c = min(w, x0 + face_size), min(h, y0 + face_size)
            if x1c > x0c and y1c > y0c:
                sub = patch[y0c - y0 : y1c - y0, x0c - x0 : x1c - x0]
                smask = mask[y0c - y0 : y1c - y0, x0c - x0 : x1c - x0]
                roi = frame[y0c:y1c, x0c:x1c]
                roi[smask > 0] = sub[smask > 0]

        # timestamp burn-in (helps verify frame ordering)
        cv2.putText(frame, f"DEMO {i + 1:03d}/{total:03d}", (14, h - 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (70, 74, 84), 1, cv2.LINE_AA)
        vw.write(frame)

    vw.release()
    print(f"wrote {out} ({total} frames, {w}x{h}, {fps} fps)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "temp" / "livecam.avi"))
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--fps", type=float, default=12.0)
    ap.add_argument("--face", default=str(ROOT / "tests" / "fixtures" / "lena.jpg"))
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    build(out, a.seconds, a.fps, Path(a.face))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
