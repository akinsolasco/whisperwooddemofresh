from datetime import datetime
from typing import Any, Optional


def local_now() -> datetime:
    return datetime.now().astimezone()


def _local_timezone():
    return local_now().tzinfo


def _friendly_timezone_label(label: str) -> str:
    label = str(label or "").strip()
    if not label:
        return ""
    upper = label.upper()
    if upper in {"UTC", "GMT"}:
        return upper
    words = [word for word in label.replace("_", " ").split() if word]
    if len(words) >= 2 and words[-1].lower() == "time":
        acronym = "".join(word[0].upper() for word in words if word[0].isalpha())
        return acronym if 2 <= len(acronym) <= 5 else label
    return label


def _clean_iso_value(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "T" not in text and " " in text:
        first, rest = text.split(" ", 1)
        if first.count("-") == 2:
            text = f"{first}T{rest}"
    if "." in text:
        head, tail = text.split(".", 1)
        offset = ""
        for marker in ("+", "-"):
            if marker in tail:
                fraction, offset_tail = tail.split(marker, 1)
                offset = marker + offset_tail
                break
        else:
            fraction = tail
        fraction = "".join(ch for ch in fraction if ch.isdigit())[:6]
        text = f"{head}.{fraction}{offset}" if fraction else f"{head}{offset}"
    return text


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value

    raw = str(value).strip()
    if not raw:
        return None

    candidates = [_clean_iso_value(raw), raw]
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            pass

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            pass
    return None


def format_readable_datetime(value: Any, fallback: str = "") -> str:
    dt = parse_datetime(value)
    if not dt:
        return fallback if fallback else str(value or "")

    if dt.tzinfo is not None:
        dt = dt.astimezone()
        tz_label = _friendly_timezone_label(dt.tzname() or "")
    else:
        tz_label = _friendly_timezone_label(_local_timezone().tzname(dt) or "")

    hour = dt.strftime("%I").lstrip("0") or "12"
    minute = dt.strftime("%M")
    am_pm = dt.strftime("%p")
    date_part = f"{dt.strftime('%a')}, {dt.strftime('%b')} {dt.day}, {dt.year}"
    time_part = f"{hour}:{minute} {am_pm}"
    return f"{date_part} {time_part} {tz_label}".strip()


def format_local_now() -> str:
    return format_readable_datetime(local_now())


def format_elapsed_seconds(value: Any) -> str:
    try:
        seconds = int(float(value))
    except Exception:
        return "Not reported"

    if seconds < 0:
        return "Just now"
    if seconds <= 5:
        return "Just now"
    if seconds < 60:
        return f"{seconds} sec ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"
