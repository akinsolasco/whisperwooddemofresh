from dataclasses import dataclass
from typing import Optional, Dict, Any

PALETTE = ["WHITE", "BLACK", "RED", "YELLOW", "BLUE", "GREEN"]
SECTIONS = ["NAME", "ROOM", "DIET", "TEXTURE", "FLUIDS", "NOTE", "DRINKS"]


def auto_fg_for_bg(bg: str) -> str:
    bg = (bg or "").upper()
    if bg in ("RED", "BLUE", "GREEN", "BLACK"):
        return "WHITE"
    return "BLACK"


@dataclass
class Device:
    id: str
    ip: str
    port: int
    fw: Optional[str]
    pending_seq: Optional[int]
    pending_img_seq: Optional[int]
    last_seen_s: int
    battery_level: Optional[int] = None

    @property
    def is_online(self) -> bool:
        return self.last_seen_s <= 10


@dataclass
class HighlightRule:
    type: str
    section: str
    value: Optional[str]
    bg: str
    fg: str

    def to_json(self) -> Dict[str, Any]:
        data = {
            "type": self.type,
            "section": self.section,
            "bg": self.bg,
            "fg": self.fg,
        }
        if self.type == "value":
            data["value"] = self.value or ""
        return data

    def label(self) -> str:
        if self.type == "section":
            return f"SECTION {self.section} BG={self.bg} FG={self.fg}"
        return f"VALUE {self.section} '{self.value}' BG={self.bg} FG={self.fg}"
