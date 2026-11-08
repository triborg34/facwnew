"""Zones: resolution-independent geometry (stage 2).

Zone points are stored **normalised to 0..1** of the image they were drawn on,
together with that image's size (``image_w``/``image_h``) for the editor.  They
are rescaled to the live frame on every use, so zones never drift when the
stream resolution changes (pitfall 6).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


@dataclass
class Zone:
    zone_uid: str
    name: str
    kind: str = "polygon"                     # polygon | rect | line
    points: List[Point] = field(default_factory=list)   # normalised 0..1
    colour: str = "#38bdf8"
    description: str = ""
    relay_channel: str = ""
    image_w: int = 0
    image_h: int = 0

    # -- conversion ----------------------------------------------------
    def to_pixels(self, width: int, height: int) -> List[Tuple[int, int]]:
        """Normalised -> pixel coordinates for a frame of ``width``x``height``."""
        return [
            (int(round(x * width)), int(round(y * height)))
            for x, y in self.points
            if math.isfinite(x) and math.isfinite(y)
        ]

    @staticmethod
    def from_pixels(
        zone_uid: str,
        name: str,
        points: Sequence[Sequence[float]],
        image_w: int,
        image_h: int,
        kind: str = "polygon",
        colour: str = "#38bdf8",
        description: str = "",
        relay_channel: str = "",
    ) -> "Zone":
        """Pixel coordinates -> normalised coordinates (editor save path)."""
        w = max(1, int(image_w))
        h = max(1, int(image_h))
        norm = [(float(p[0]) / w, float(p[1]) / h) for p in points if len(p) >= 2]
        return Zone(
            zone_uid=zone_uid,
            name=name,
            kind=kind,
            points=norm,
            colour=colour,
            description=description,
            relay_channel=relay_channel,
            image_w=int(image_w),
            image_h=int(image_h),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.zone_uid,
            "zone_uid": self.zone_uid,
            "name": self.name,
            "kind": self.kind,
            "points": [[round(x, 6), round(y, 6)] for x, y in self.points],
            "colour": self.colour,
            "description": self.description,
            "relay_channel": self.relay_channel,
            "image_w": self.image_w,
            "image_h": self.image_h,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Zone":
        return Zone(
            zone_uid=str(d.get("id") or d.get("zone_uid") or "zone"),
            name=str(d.get("name", "zone")),
            kind=str(d.get("kind", "polygon")),
            points=[(float(p[0]), float(p[1])) for p in (d.get("points") or []) if len(p) >= 2],
            colour=str(d.get("colour", "#38bdf8")),
            description=str(d.get("description", "")),
            relay_channel=str(d.get("relay_channel", "")),
            image_w=int(d.get("image_w") or d.get("img_w") or 0),
            image_h=int(d.get("image_h") or d.get("img_h") or 0),
        )


# --------------------------------------------------------------------------
# Geometry helpers - all operate on normalised coordinates
# --------------------------------------------------------------------------
def point_in_polygon(x: float, y: float, poly: Sequence[Point]) -> bool:
    """Ray casting.  Boundary points count as inside."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        # boundary check for the segment
        if _on_segment(x, y, xi, yi, xj, yj):
            return True
        if (yi > y) != (yj > y):
            x_int = (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
            if x < x_int:
                inside = not inside
        j = i
    return inside


def _on_segment(px, py, x1, y1, x2, y2, eps: float = 1e-9) -> bool:
    cross = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
    if abs(cross) > eps:
        return False
    dot = (px - x1) * (px - x2) + (py - y1) * (py - y2)
    return dot <= eps


def point_in_rect(x: float, y: float, rect: Sequence[Point]) -> bool:
    if len(rect) < 2:
        return False
    xs = [p[0] for p in rect]
    ys = [p[1] for p in rect]
    return min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys)


def distance_point_to_segment(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def point_near_line(x: float, y: float, line: Sequence[Point], tolerance: float = 0.02) -> bool:
    """A 'line' zone is a barrier: the tolerance is in normalised units."""
    if len(line) < 2:
        return False
    for i in range(len(line) - 1):
        if distance_point_to_segment(x, y, *line[i], *line[i + 1]) <= tolerance:
            return True
    return False


def zone_contains(zone: Zone, nx: float, ny: float, line_tolerance: float = 0.02) -> bool:
    if zone.kind == "rect":
        return point_in_rect(nx, ny, zone.points)
    if zone.kind == "line":
        return point_near_line(nx, ny, zone.points, line_tolerance)
    return point_in_polygon(nx, ny, zone.points)


def attribute_zone(zones: Sequence[Zone], nx: float, ny: float) -> Optional[Zone]:
    """Stage 2: first zone whose area contains the detection's centre."""
    for z in zones:
        if zone_contains(z, nx, ny):
            return z
    return None


def any_zone_contains(zones: Sequence[Zone], nx: float, ny: float) -> bool:
    return attribute_zone(zones, nx, ny) is not None


def zone_mask(
    zones: Sequence[Zone], width: int, height: int, lines_only_excluded: bool = True
):
    """Build a uint8 mask (255 inside the union of zones) for the frame size.

    Line zones are barriers, not areas, so they never contribute to the mask.
    Returns None when there is nothing to mask with, in which case the caller
    must fall back to the whole frame - never to a hard-coded rectangle
    (spec 4.3).
    """
    import numpy as np

    area_zones = [z for z in zones if z.kind != "line"] if lines_only_excluded else list(zones)
    if not area_zones:
        return None
    mask = np.zeros((height, width), dtype=np.uint8)
    for z in area_zones:
        pts = z.to_pixels(width, height)
        if len(pts) < 3:
            continue
        import cv2

        cv2.fillPoly(mask, [np.asarray(pts, dtype=np.int32)], 255)
    if not mask.any():
        return None
    return mask


def apply_zone_mask(frame, zones: Sequence[Zone]):
    """Black out everything outside the zones (only people inside are detected)."""
    mask = zone_mask(zones, frame.shape[1], frame.shape[0])
    if mask is None:
        return frame  # no zones -> whole frame
    import cv2

    return cv2.bitwise_and(frame, frame, mask=mask)
