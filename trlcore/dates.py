"""日付の解釈と鮮度計算。基準日(as_of)は必ず引数で受け取る(内部で today を使わない)。"""

import calendar
import re
from datetime import date


_RE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def parse_date(s, end=False):
    """YYYY / YYYY-MM / YYYY-MM-DD を date に。欠けた部分は end=False で期間の最初、True で最後。"""
    if isinstance(s, date):
        return s
    if not isinstance(s, str):
        return None
    m = _RE.fullmatch(s.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    try:
        if mo is None:
            return date(y, 12, 31) if end else date(y, 1, 1)
        mo = int(mo)
        last = calendar.monthrange(y, mo)[1]
        if d is None:
            return date(y, mo, last if end else 1)
        return date(y, mo, int(d))
    except ValueError:
        return None


def days_since(published, as_of):
    p = parse_date(published)
    return None if p is None else (as_of - p).days


def freshness_info(published, as_of):
    n = days_since(published, as_of)
    if n is None:
        bucket = "unknown"
    elif n < 0:
        bucket = "future"
    elif n <= 30:
        bucket = "fresh_30d"
    elif n <= 180:
        bucket = "recent_180d"
    else:
        bucket = "older"
    return {"published": published, "as_of": as_of.isoformat(), "days_since_published": n, "bucket": bucket}
