"""Birth dates and the age derived from them.

The console asks for a *birth date*, never an age: an age entered by hand
goes stale the day after it is typed, while a birth date stays correct for
ever.  The age that is shown and stored is always derived from that date.

Dates are Gregorian ``YYYY-MM-DD`` strings - the same format the events
table uses for ``event_date`` - so they sort and compare as text.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


def parse_birth_date(value: object) -> Optional[date]:
    """Parse a ``YYYY-MM-DD`` birth date; ``None`` when it is unusable."""
    if value in (None, "", "null"):
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def age_from_birth_date(birth_date: object, today: Optional[date] = None) -> Optional[int]:
    """Whole years between the birth date and today.

    ``None`` when no usable date was given or when the date lies in the
    future - a person cannot be younger than zero.
    """
    born = parse_birth_date(birth_date)
    if born is None:
        return None
    now = today or date.today()
    if born > now:
        return None
    years = now.year - born.year
    if (now.month, now.day) < (born.month, born.day):
        years -= 1
    return max(0, years)
