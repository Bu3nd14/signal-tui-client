"""Kitty graphics renderer (DESIGN_NATIVE_IMAGES.md §3, §7).

Implements the raw kitty graphics protocol on top of an injected ``write``
callback so the class stays testable headlessly (the real callback is
``App._driver.write``).  Key corrections folded in from the POC:

- C1: ``q=2`` on **every** command, including the first ``a=t`` chunk (a ``q=0``
  would make kitty answer ``OK`` on stdin → garbage in the message input).
- R5: ``_transmitted`` (data sent once per image) is split from ``_placed``
  (active placements), so re-entering the viewport only re-emits ``a=p``.
- C2: vertical *and* horizontal clipping are computed in ``compute_source_rect``
  against the ``#chat-log`` content region and capped by ``max_w_px``.

Cell size is injected at construction time and invalidated externally on
``on_resize`` (see ``tui.app``): the renderer never re-detects it on its own.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Callable

from PIL import Image

#: A DCS transmit/place/delete sequence ends with the ST control (``ESC \\``).
_ST = "\x1b\\"
#: DCS introducer for the kitty graphics protocol.
_DCS = "\x1b_G"

#: Base64 data chunk size (a multiple of 4, per the kitty spec).
_CHUNK_SIZE = 4096

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(png_bytes: bytes) -> tuple[int, int]:
    """Return ``(width_px, height_px)`` from a PNG header (no full decode)."""
    if len(png_bytes) < 24 or png_bytes[:8] != _PNG_SIGNATURE:
        raise ValueError("not a valid PNG")
    width = int.from_bytes(png_bytes[16:20], "big")
    height = int.from_bytes(png_bytes[20:24], "big")
    return width, height


def prepare_hi_res(path, max_w_px: int, max_h_px: int) -> bytes:
    """Prepare a hi-res PNG: downscale *path* to fit ``max_w×max_h`` pixels.

    Stateless (Pillow only); used by the modal's native branch to cap the image
    at ~1600px on its long side while still fitting the terminal.
    """
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max(1, max_w_px), max(1, max_h_px)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()


def dcs_transmit(image_id: int, png_bytes: bytes) -> list[str]:
    """Build the ``a=t`` (transmit, no display) DCS sequence list.

    Each returned string is a complete DCS sequence whose base64 payload is at
    most ``_CHUNK_SIZE`` bytes (a multiple of 4).  The first chunk carries the
    full key set with ``q=2``; subsequent chunks carry only ``m``; the last one
    terminates with ``m=0`` and no extra keys (kitty spec).
    """
    payload = base64.standard_b64encode(png_bytes).decode("ascii")
    n = len(payload)
    if n == 0:
        return [f"{_DCS}a=t,i={image_id},f=100,q=2,m=0;{_ST}"]

    chunks: list[str] = []
    for offset in range(0, n, _CHUNK_SIZE):
        chunk = payload[offset : offset + _CHUNK_SIZE]
        last = offset + _CHUNK_SIZE >= n
        if offset == 0:
            header = f"a=t,i={image_id},f=100,q=2,m={'0' if last else '1'}"
        else:
            header = f"m={'0' if last else '1'}"
        chunks.append(f"{_DCS}{header};{chunk}{_ST}")
    return chunks


def transmit_chunks(image_id: int, png_bytes: bytes) -> str:
    """Build the complete chunked transmit payload for one image."""
    return "".join(dcs_transmit(image_id, png_bytes))


def dcs_place(
    image_id: int,
    placement_id: int,
    *,
    row: int,
    col: int,
    x_src: int = 0,
    y_src: int = 0,
    w_px: int = 0,
    h_px: int = 0,
) -> str:
    """Build the ``a=p`` placement DCS: cursor move + source rectangle.

    The source rectangle ``(x_src, y_src, w_px, h_px)`` is expressed in image
    pixels; horizontal clipping propagates ``x_src`` (B1, DESIGN §4.2/C2).
    ``C=1`` keeps kitty from moving the cursor, so Textual's cursor tracking is
    untouched.
    """
    move = f"\x1b[{row};{col}H"
    keys = (
        f"a=p,i={image_id},p={placement_id},x={x_src},y={y_src},"
        f"w={w_px},h={h_px},C=1,q=2"
    )
    return f"{move}{_DCS}{keys}{_ST}"


def dcs_delete(image_id: int, *, keep_data: bool) -> str:
    """Build the delete DCS: ``d=i`` (placements only) or ``d=I`` (data too)."""
    action = "i" if keep_data else "I"
    return f"{_DCS}a=d,d={action},i={image_id},q=2{_ST}"


def dcs_clear_placements() -> str:
    """Build the DCS that deletes every placement but keeps all image data."""
    return f"{_DCS}a=d,d=a,q=2{_ST}"


def dcs_clear_all() -> str:
    """Build the DCS that deletes every image and placement."""
    return f"{_DCS}a=d,d=A,q=2{_ST}"


def compute_source_rect(
    widget_region,
    container_content_region,
    cell_w: int,
    cell_h: int,
    max_w_px: int,
) -> tuple[int, int, int, int, int, int] | None:
    """Map a widget region to a kitty placement source rectangle.

    Returns ``(row, col, x_src, y_src, w_px, h_px)`` (1-based ``row``/``col``)
    or ``None`` when the widget is fully outside the container's content region.

    Vertical clipping is native via ``y_src`` (top cut) and ``h_px`` (visible
    height).  Horizontal clipping propagates ``x_src`` (left cut) and caps
    ``w_px`` (right cut + image natural width), so a wide (panorama) thumbnail
    is cropped on the correct side (B1, DESIGN §4.2/C2).
    """
    top = widget_region.y
    bottom = widget_region.bottom
    ctop = container_content_region.y
    cbottom = container_content_region.bottom
    if bottom <= ctop or top >= cbottom:
        return None

    cut_top = max(0, ctop - top)
    cut_bottom = max(0, bottom - cbottom)
    visible_h = (bottom - top) - cut_top - cut_bottom
    if visible_h <= 0:
        return None
    y_src = cut_top * cell_h
    h_px = visible_h * cell_h

    left = widget_region.x
    right = widget_region.right
    cleft = container_content_region.x
    cright = container_content_region.right
    if right <= cleft or left >= cright:
        return None

    cut_left = max(0, cleft - left)
    cut_right = max(0, right - cright)
    visible_w = (right - left) - cut_left - cut_right
    if visible_w <= 0:
        return None

    row = max(top, ctop) + 1
    col = max(left, cleft) + 1
    x_src = cut_left * cell_w
    w_px = min(visible_w * cell_w, max_w_px - x_src)
    if w_px <= 0:
        return None
    return (row, col, x_src, y_src, w_px, h_px)


class KittyRenderer:
    """Stateful kitty graphics renderer bound to an injected write callback."""

    def __init__(
        self,
        *,
        write: Callable[[str], None],
        cell_w: int,
        cell_h: int,
    ) -> None:
        self._write = write
        self.cell_w = cell_w
        self.cell_h = cell_h
        # R5 split: data transmitted once per image vs. placements currently
        # on screen.  ``d=i`` keeps data; ``d=I`` frees it.
        self._transmitted: set[int] = set()
        self._placed: set[tuple[int, int]] = set()

    @property
    def has_data(self) -> bool:
        """Whether the renderer currently owns transmitted image data."""
        return bool(self._transmitted)

    # ── Thumbnail preparation (stateless, Pillow only) ─────────────────────
    def prepare_thumbnail(self, path, max_lines: int, max_cols: int) -> bytes:
        """Resize *path* proportionally into ``max_lines``×``max_cols`` cells
        and return the encoded PNG bytes."""
        target_w = max(1, max_cols * self.cell_w)
        target_h = max(1, max_lines * self.cell_h)
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()

    # ── Protocol emission ──────────────────────────────────────────────────
    def transmit(self, image_id: int, png_bytes: bytes) -> None:
        """Send the image data once per image id (R5)."""
        if image_id in self._transmitted:
            return
        self._transmitted.add(image_id)
        for chunk in dcs_transmit(image_id, png_bytes):
            self._write(chunk)

    def transmit_prepared(self, image_id: int, payload: str) -> None:
        """Send a prebuilt complete transmit payload with one driver write."""
        if image_id in self._transmitted:
            return
        self._transmitted.add(image_id)
        self._write(payload)

    def place(
        self,
        image_id: int,
        placement_id: int,
        *,
        row: int,
        col: int,
        x_src: int = 0,
        y_src: int = 0,
        w_px: int = 0,
        h_px: int = 0,
    ) -> None:
        """Place (or replace, same ``placement_id`` → no flicker) an image."""
        self._write(
            dcs_place(
                image_id,
                placement_id,
                row=row,
                col=col,
                x_src=x_src,
                y_src=y_src,
                w_px=w_px,
                h_px=h_px,
            )
        )
        self._placed.add((image_id, placement_id))

    def delete(self, image_id: int, *, keep_data: bool) -> None:
        """Delete the placements (``keep_data=True``) or the data (``False``)."""
        self._write(dcs_delete(image_id, keep_data=keep_data))
        self._placed = {(i, p) for (i, p) in self._placed if i != image_id}
        if not keep_data:
            self._transmitted.discard(image_id)

    def clear_placements(self) -> None:
        """Remove every placement, keeping all image data (screen-stack gate)."""
        self._write(dcs_clear_placements())
        self._placed.clear()

    def clear_all(self) -> None:
        """Delete every image and placement (exit path)."""
        self._write(dcs_clear_all())
        self._placed.clear()
        self._transmitted.clear()
