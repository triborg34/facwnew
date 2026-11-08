"""Printable report generation (spec 7.3).

Multi-page, portrait, right-to-left, with an index, the captured frame, name,
date, time, gender, approval state, age and national number.

RTL is handled by laying out the table right-to-left explicitly (the first
column of each row is the image on the right) rather than hoping the PDF engine
mirrors it, so the layout is identical in both directions.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Dict, List, Optional

import cv2
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, portrait
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..core.logging_setup import get_logger

log = get_logger("reports")

ROLE_LABEL = {
    "approve": "مجاز",
    "deny": "غیرمجاز",
    "visitor": "مهمان",
    "colleague": "همکار",
    "unknown": "نامشخص",
}
GENDER_LABEL = {
    "male": "مرد",
    "female": "زن",
    "unknown": "نامشخص",
}

_FONT = "Helvetica"


def rtl(text: str) -> str:
    """Shape and reorder Persian/Arabic text so reportlab can draw it.

    reportlab writes glyphs left-to-right with no contextual joining, so a
    Persian label would otherwise appear as isolated, reversed letters.
    """
    text = "" if text is None else str(text)
    if not text or not any("؀" <= ch <= "ۿ" or "ﭐ" <= ch <= "﷿" for ch in text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001 - libraries optional at runtime
        return text


def register_font(font_path: Optional[str] = None) -> str:
    """Register a Unicode font so names in any script render correctly."""
    global _FONT
    candidates = []
    if font_path:
        candidates.append(font_path)
    candidates += [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in candidates:
        try:
            import os

            if path and os.path.exists(path):
                pdfmetrics.registerFont(TTFont("FacwSans", path))
                _FONT = "FacwSans"
                return _FONT
        except Exception:  # noqa: BLE001 - fall back to Helvetica
            continue
    return _FONT


def _thumb(media, rel: str, max_w: float = 44 * mm, max_h: float = 33 * mm) -> Optional[RLImage]:
    if not rel:
        return None
    img = media.read_image(rel)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        return None
    data = io.BytesIO(buf.tobytes())
    return RLImage(data, width=w * scale, height=h * scale)


def build_report(
    rows: List[Dict[str, Any]],
    filters: Dict[str, Any],
    media,
    meta: Optional[Dict[str, Any]] = None,
) -> bytes:
    font = register_font()
    meta = meta or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=portrait(A4),
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=rtl("گزارش شناسایی چهره"),
        author="FACW",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=styles["Title"], fontName=font, fontSize=17, alignment=2)
    sub_style = ParagraphStyle("s", parent=styles["Normal"], fontName=font, fontSize=9, alignment=2)
    head_style = ParagraphStyle("h", parent=styles["Normal"], fontName=font, fontSize=8.5, alignment=1)
    cell_style = ParagraphStyle("c", parent=styles["Normal"], fontName=font, fontSize=8, alignment=2,
                                wordWrap="CJK")

    story: List[Any] = []
    story.append(Paragraph(rtl("گزارش شناسایی چهره — FACW"), title_style))
    generated = meta.get("generated") or datetime.now()
    story.append(
        Paragraph(
            rtl(f"تاریخ تولید: {generated.strftime('%Y-%m-%d %H:%M')}  ·  تعداد ردیف‌ها: {len(rows)}"),
            sub_style,
        )
    )
    active = {k: v for k, v in (filters or {}).items() if v not in (None, "", [])}
    if active:
        story.append(Paragraph(rtl("فیلترها: " + ", ".join(f"{k}={v}" for k, v in active.items())), sub_style))
    story.append(Spacer(1, 5 * mm))

    # --- index page: newest first, compact ---------------------------------
    story.append(Paragraph(rtl("فهرست"), sub_style))
    index_rows = [[rtl(h) for h in ["#", "نام", "تاریخ", "ساعت", "وضعیت", "دوربین"]]]
    for i, row in enumerate(rows, start=1):
        index_rows.append(
            [
                str(i),
                Paragraph(str(row.get("identity_name") or "unknown"), cell_style),
                str(row.get("event_date", "")),
                str(row.get("event_time", "")),
                ROLE_LABEL.get(str(row.get("role")), str(row.get("role") or "")),
                Paragraph(str(row.get("camera_source") or ""), cell_style),
            ]
        )
    if len(index_rows) > 1:
        t = Table(index_rows, colWidths=[10 * mm, 45 * mm, 26 * mm, 20 * mm, 24 * mm, 45 * mm], repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#94a3b8")),
                    ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        story.append(t)
    else:
        story.append(Paragraph(rtl("رکوردی یافت نشد"), sub_style))

    # --- detail pages ------------------------------------------------------
    story.append(PageBreak())
    for i, row in enumerate(rows, start=1):
        story.append(Paragraph(f"#{i} &nbsp; {row.get('identity_name') or 'unknown'}", head_style))
        thumb = _thumb(media, row.get("frame_file") or row.get("person_file") or "")
        face = _thumb(media, row.get("face_file") or "", max_w=24 * mm, max_h=24 * mm)
        details = [
            [rtl("نام"), str(row.get("identity_name") or "نامشخص")],
            [rtl("تاریخ"), str(row.get("event_date", ""))],
            [rtl("ساعت"), str(row.get("event_time", ""))],
            [rtl("جنسیت"), rtl(GENDER_LABEL.get(str(row.get("gender")), str(row.get("gender") or "")))],
            [rtl("وضعیت"), rtl(ROLE_LABEL.get(str(row.get("role")), str(row.get("role") or "")))],
            [rtl("سن"), str(row.get("age") if row.get("age") is not None else "-")],
            ["national no.", str(row.get("national_number") or "-")],
            ["similarity", f"{float(row.get('score') or 0):.3f}"],
            ["camera", str(row.get("camera_source") or "")],
        ]
        detail_table = Table(
            [[Paragraph(f"<b>{k}</b>", cell_style), Paragraph(v, cell_style)] for k, v in details],
            colWidths=[24 * mm, 52 * mm],
        )
        detail_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
                    ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ]
            )
        )
        right_cell = [thumb] if thumb is not None else [Paragraph("(image unavailable)", cell_style)]
        if face is not None:
            right_cell += [Spacer(1, 2 * mm), face]
        # First column is the image so the whole row reads right-to-left.
        row_table = Table([[[*right_cell], detail_table]], colWidths=[50 * mm, 85 * mm])
        row_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                       ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
        story.append(row_table)
        story.append(Spacer(1, 5 * mm))

    if not rows:
        story.append(Paragraph("no records matched the filters", sub_style))

    doc.build(story)
    return buf.getvalue()
