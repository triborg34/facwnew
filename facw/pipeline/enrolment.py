"""Enrolment service (spec 5).

Rules, applied consistently by *every* enrolment path (console dialog, helper
app, camera still), because two paths must never disagree (pitfall 9):

* A new reference photo is **appended**.  An existing identity never loses a
  good reference to a worse one (spec 5.1).  Removal is always explicit and
  index-based (spec 5.5).
* Face selection is always explicit: a multi-face photo returns every
  candidate, and the operator's chosen face is the one registered.
* After any change the match index is rebuilt immediately - no restart and no
  manual refresh (spec 5.1 / M8).
* Biometric vectors are sealed at rest and reads are logged (spec 6.7).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from ..core.logging_setup import get_logger
from ..vision.face import FaceBox, FaceEngine, best_face, pad_box, quality_gate
from .matching import MatchIndex

log = get_logger("enrolment")

MAX_PHOTOS = 10
# upper bound on faces returned to the picker; see detect_faces()
MAX_CANDIDATES = 16


@dataclass
class FaceCandidate:
    index: int
    box: Dict[str, float]
    score: float
    width: float
    age: Optional[int]
    gender: str
    thumbnail: str            # data: URL, ready for the console
    passed_gate: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "box": self.box,
            "score": round(self.score, 4),
            "width": round(self.width, 1),
            "age": self.age,
            "gender": self.gender,
            "thumbnail": self.thumbnail,
            "passed_gate": self.passed_gate,
        }


def _data_url(img: np.ndarray, quality: int = 85, max_side: int = 220) -> str:
    import base64

    if img is None or img.size == 0:
        return ""
    h, w = img.shape[:2]
    scale = min(1.0, max_side / max(h, w, 1))
    if scale < 1.0:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


class EnrolmentService:
    def __init__(self, media, identities, face: FaceEngine, index: MatchIndex, settings_mgr, audit, bus) -> None:
        self.media = media
        self.identities = identities
        self.face = face
        self.index = index
        self.settings_mgr = settings_mgr
        self.audit = audit
        self.bus = bus

    # -- index refresh -------------------------------------------------
    def refresh_index(self) -> int:
        rows = self.identities.load_vectors(actor="enrolment")
        self.index.rebuild(rows)
        return self.index.stats()["rows"]

    # -- face detection for the picker ---------------------------------
    def _pick_candidates(self, image: np.ndarray, s) -> list:
        """The face set offered to the picker, left-to-right.

        Both the candidate list and the numbered preview overlay use this, so
        the numbers a user sees always match the chips they can choose.
        """
        faces = self.face.detect(image, min(0.3, s.face_accept_threshold))
        kept = sorted(
            faces,
            key=lambda f: (
                quality_gate(f, s.min_face_width, s.min_face_score),
                f.w * f.h,
                f.score,
            ),
            reverse=True,
        )[:MAX_CANDIDATES]
        kept.sort(key=lambda f: f.x)
        return kept

    def detect_faces(self, image: np.ndarray) -> List[FaceCandidate]:
        """Every face in the image as a selectable candidate (spec 5.3).

        Candidates are capped at ``MAX_CANDIDATES`` (best first: passes the
        quality gate, then larger, then higher score).  A crowd photo can yield
        hundreds of detections whose data-URL thumbnails add up to megabytes and
        stall the browser, so the picker only ever receives the plausible ones.
        """
        s = self.settings_mgr.get()
        faces = self._pick_candidates(image, s)
        out: List[FaceCandidate] = []
        h, w = image.shape[:2]
        for i, f in enumerate(faces):
            fx1, fy1, fx2, fy2 = pad_box(f.x, f.y, f.w, f.h, s.face_crop_padding, w, h)
            crop = image[fy1:fy2, fx1:fx2]
            from ..vision.face import estimate_attributes

            age, gender = estimate_attributes(f, float(w), float(h))
            out.append(
                FaceCandidate(
                    index=i,
                    box=f.as_dict(),
                    score=f.score,
                    width=f.w,
                    age=age,
                    gender=gender,
                    thumbnail=_data_url(crop),
                    passed_gate=quality_gate(f, s.min_face_width, s.min_face_score),
                )
            )
        return out

    def annotate_preview(self, image: np.ndarray) -> str:
        """Full-size preview with numbered boxes for the picker."""
        s = self.settings_mgr.get()
        faces = self._pick_candidates(image, s)
        canvas = image.copy()
        for i, f in enumerate(faces):
            colour = (60, 200, 90) if i == 0 else (240, 180, 60)
            cv2.rectangle(canvas, (int(f.x), int(f.y)), (int(f.x + f.w), int(f.y + f.h)), colour, 2)
            cv2.putText(
                canvas,
                f"#{i + 1} {f.score:.2f}",
                (int(f.x), max(14, int(f.y) - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                colour,
                2,
                cv2.LINE_AA,
            )
        return _data_url(canvas, quality=72, max_side=560)

    # -- enrolment -----------------------------------------------------
    def enrol(
        self,
        images: List[np.ndarray],
        selected_faces: List[int],
        metadata: Dict[str, Any],
        actor: str = "",
        identity_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Enrol one person from one or more photos.

        ``selected_faces[i]`` picks which face in ``images[i]`` is the identity.
        When ``identity_id`` is given the references are appended to that
        existing identity instead of creating a new one.
        """
        if not images:
            raise ValueError("no images supplied")
        if len(images) > MAX_PHOTOS:
            raise ValueError(f"at most {MAX_PHOTOS} photos per enrolment")
        if len(selected_faces) != len(images):
            raise ValueError("selected_faces must match images")
        s = self.settings_mgr.get()

        extracted: List[Dict[str, Any]] = []
        for img, pick in zip(images, selected_faces):
            if img is None or img.size == 0:
                continue
            # Same candidate set (and therefore the same ordering) the console
            # showed in the picker, so the chosen index is meaningful.
            faces = self._pick_candidates(img, s)
            if not faces:
                raise ValueError("no face detected in one of the photos")
            if pick < 0 or pick >= len(faces):
                raise ValueError(f"face index {pick} out of range for one photo")
            chosen = faces[pick]
            if not quality_gate(chosen, 12, 0.2):
                raise ValueError("the selected face is too small or too low quality")
            vector = self.face.embed(img, chosen)
            if vector is None:
                raise ValueError("could not compute an identity vector for the selected face")
            h, w = img.shape[:2]
            fx1, fy1, fx2, fy2 = pad_box(chosen.x, chosen.y, chosen.w, chosen.h, s.face_crop_padding, w, h)
            crop = img[fy1:fy2, fx1:fx2]
            stored = self.media.save_reference(crop if crop.size else img, s.snapshot_quality)
            extracted.append(
                {
                    "vector": vector,
                    "crop": crop,
                    "face": chosen,
                    "image_file": stored,
                    "full": img,
                }
            )
        if not extracted:
            raise ValueError("no usable photos")

        created = False
        if identity_id is None:
            first = extracted[0]["crop"]
            avatar = self.media.save_avatar(
                cv2.resize(first, (128, 128)) if first.size else first, s.snapshot_quality
            )
            identity_id = self.identities.create(
                {
                    **metadata,
                    "avatar_file": avatar,
                    "reference_file": extracted[0]["image_file"],
                    "registered_by": actor or metadata.get("registered_by", ""),
                }
            )
            created = True
        else:
            existing = self.identities.get(identity_id)
            if existing is None:
                raise ValueError("identity not found")
            if metadata:
                safe = {
                    k: v
                    for k, v in metadata.items()
                    if k in ("name", "family_name", "age", "gender", "role",
                             "national_number", "description", "registered_by")
                }
                if safe:
                    self.identities.update(identity_id, safe)

        ref_ids = []
        for item in extracted:
            ref_ids.append(
                self.identities.add_ref(
                    identity_id,
                    item["vector"],
                    item["image_file"],
                    face_width=int(item["face"].w),
                    face_score=float(item["face"].score),
                    actor=actor,
                )
            )

        self.refresh_index()
        self.audit.record(
            "identity_enrolled" if created else "reference_added",
            actor=actor,
            target=str(identity_id),
            detail=f"refs={len(ref_ids)} photos={len(images)}",
        )
        self.bus.publish("identities", {"action": "ref_added", "id": identity_id})
        return {
            "identity_id": identity_id,
            "created": created,
            "refs_added": len(ref_ids),
            "identity": self.identities.get(identity_id),
        }

    def add_reference(self, identity_id: int, image: np.ndarray, face_index: int = 0, actor: str = "") -> Dict[str, Any]:
        return self.enrol([image], [face_index], {}, actor=actor, identity_id=identity_id)

    def remove_reference(self, identity_id: int, index: int, actor: str = "") -> float:
        """Remove reference ``index`` (1-based); index refreshes immediately."""
        t0 = time.perf_counter()
        removed_file = self.identities.remove_ref(identity_id, index, actor)
        if removed_file is None:
            raise ValueError("reference index out of range")
        # Only unlink the image if no other row still references it.
        remaining = {r["image_file"] for r in self.identities.list_refs(identity_id)}
        if removed_file not in remaining:
            self.media.delete(removed_file)
        self.refresh_index()
        self.audit.record("reference_removed", actor=actor, target=str(identity_id), detail=f"index={index}")
        return (time.perf_counter() - t0) * 1000.0

    def delete_identity(self, identity_id: int, actor: str = "") -> bool:
        ident = self.identities.get(identity_id)
        if ident is None:
            return False
        refs = self.identities.list_refs(identity_id)
        ok = self.identities.delete(identity_id)
        for r in refs:
            self.media.delete(r.get("image_file", ""))
        if ident.get("avatar_file"):
            self.media.delete(ident["avatar_file"])
        # deletion removes it from matching immediately
        self.refresh_index()
        self.audit.record("identity_deleted", actor=actor, target=str(identity_id), detail=ident.get("name", ""))
        self.bus.publish("identities", {"action": "deleted", "id": identity_id})
        return ok

    # -- image search (spec 5.7) ---------------------------------------
    def search_by_image(self, image: np.ndarray, limit: int = 20) -> Dict[str, Any]:
        """Find the most similar stored *event images* and return the records.

        This path compares whole images (a perceptual thumbnail fingerprint),
        not identity vectors - it is a separate path by design.
        """
        from ..vision.fingerprint import cached, fingerprint, similarity as fp_similarity

        query = fingerprint(image)
        if query is None:
            raise ValueError("could not read the query image")
        rows = self.identities.db.query(
            "SELECT id, face_file, frame_file, person_file, identity_name, camera_source,"
            " event_date, event_time, created_ts FROM events ORDER BY created_ts DESC LIMIT 5000"
        )
        scored: List[Dict[str, Any]] = []
        rows = list(rows)
        for row in rows:
            fp = None
            for sub, rel in (
                ("faces", row["face_file"]),
                ("persons", row["person_file"]),
                ("frames", row["frame_file"]),
            ):
                if not rel:
                    continue
                name = f"{sub}/{str(rel).split('/')[-1]}"
                fp = cached(name, int(row["id"]), self.media.read_image)
                if fp is not None:
                    break
            if fp is None:
                continue
            scored.append(
                {
                    "event_id": row["id"],
                    "event": {
                        "id": row["id"],
                        "identity_name": row["identity_name"],
                        "camera_source": row["camera_source"],
                        "event_date": row["event_date"],
                        "event_time": row["event_time"],
                        "created_ts": row["created_ts"],
                        "face_file": row["face_file"],
                        "frame_file": row["frame_file"],
                        "person_file": row["person_file"],
                    },
                    "score": round(fp_similarity(query, fp, int(row["id"])), 4),
                }
            )
        scored.sort(key=lambda d: d["score"], reverse=True)
        return {"matches": scored[:limit], "scanned": len(rows)}
