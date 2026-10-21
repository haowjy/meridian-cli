"""Display-width helpers shared by the browse renderers."""

from __future__ import annotations

from prompt_toolkit.utils import get_cwidth


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if get_cwidth(value) <= width:
        return value
    if width == 1:
        return "…"
    target = width - 1
    clipped: list[str] = []
    used = 0
    for character in value:
        character_width = get_cwidth(character)
        if used + character_width > target:
            break
        clipped.append(character)
        used += character_width
    return "".join(clipped).rstrip() + "…"


def cell(value: str, width: int, *, right: bool = False) -> str:
    clipped = clip(value, width)
    pad = " " * max(0, width - get_cwidth(clipped))
    return pad + clipped if right else clipped + pad
