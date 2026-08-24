"""Native terminal-image support (detection, cell size, kitty renderer)."""

from __future__ import annotations

from .cellsize import get_cell_size, get_cell_size_ioctl
from .detect import ImageSupport, detect_image_support, query_kitty_ok
from .kitty_renderer import (
    KittyRenderer,
    compute_source_rect,
    png_size,
    prepare_hi_res,
)

__all__ = [
    "ImageSupport",
    "KittyRenderer",
    "compute_source_rect",
    "detect_image_support",
    "get_cell_size",
    "get_cell_size_ioctl",
    "png_size",
    "prepare_hi_res",
    "query_kitty_ok",
]
