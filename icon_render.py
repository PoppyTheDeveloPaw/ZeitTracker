"""Zeichnet das Uhr-Symbol für die Taskleiste.

Bedeutung der Farben:
  * Ring (Kreis der Uhr): grün, wenn das Tagessoll unter Einrechnung des
    Überstundensaldos bereits erfüllt ist – sonst rot.
  * Zeiger: grün, wenn das Tagessoll allein durch die heute gearbeitete Zeit
    erfüllt ist – sonst rot.
  * Zifferblatt: weiß = Arbeit läuft, gelb = Pause, grau = nicht eingestempelt.
"""
from __future__ import annotations

import math
from datetime import datetime

from PIL import Image, ImageDraw

RING_OK = (46, 160, 67, 255)
RING_BAD = (218, 54, 51, 255)
HAND_OK = (22, 120, 44, 255)
HAND_BAD = (190, 30, 30, 255)
FACE = {
    "working": (255, 255, 255, 255),
    "paused": (255, 207, 64, 255),
    "idle": (190, 190, 190, 255),
}


def render_icon(ring_ok: bool, hands_ok: bool, mode: str, now: datetime, size: int = 64) -> Image.Image:
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = S / 2
    ring_w = 40
    m = 6
    ring = RING_OK if ring_ok else RING_BAD
    hand = HAND_OK if hands_ok else HAND_BAD

    d.ellipse([m, m, S - m, S - m], fill=ring)
    inner = m + ring_w
    d.ellipse([inner, inner, S - inner, S - inner], fill=FACE.get(mode, FACE["idle"]))

    # Stundenmarken bei 12/3/6/9
    r_out = c - inner - 6
    for k in range(4):
        a = math.radians(k * 90)
        x1, y1 = c + math.sin(a) * r_out, c - math.cos(a) * r_out
        x2, y2 = c + math.sin(a) * (r_out - 16), c - math.cos(a) * (r_out - 16)
        d.line([x1, y1, x2, y2], fill=(90, 90, 90, 255), width=8)

    def hand_line(angle_deg: float, length: float, width: int):
        a = math.radians(angle_deg)
        x, y = c + math.sin(a) * length, c - math.cos(a) * length
        d.line([c, c, x, y], fill=hand, width=width)
        r = width / 2
        d.ellipse([x - r, y - r, x + r, y + r], fill=hand)

    minute_angle = now.minute * 6 + now.second * 0.1
    hour_angle = (now.hour % 12) * 30 + now.minute * 0.5
    hand_line(hour_angle, (c - inner) * 0.58, 40)
    hand_line(minute_angle, (c - inner) * 0.88, 30)
    d.ellipse([c - 24, c - 24, c + 24, c + 24], fill=hand)

    if mode == "paused":  # kleines Pausensymbol unten rechts
        bx, by, bw = S - 92, S - 92, 86
        d.ellipse([bx, by, bx + bw, by + bw], fill=(40, 40, 40, 255))
        d.rectangle([bx + 25, by + 20, bx + 38, by + bw - 20], fill=(255, 207, 64, 255))
        d.rectangle([bx + bw - 38, by + 20, bx + bw - 25, by + bw - 20], fill=(255, 207, 64, 255))

    return img.resize((size, size), Image.LANCZOS)
