"""Przeskalowanie/kompresja obrazu do osadzenia w treści maila (Pillow).

Import Pillow jest celowo lokalny w funkcji, nie na górze modułu — jeśli
kiedyś zabraknie go na innej maszynie, reszta programu (np. --dry-run bez
obrazu) nadal działa; błędnie tylko ta jedna funkcja, z czytelnym komunikatem.
"""

from __future__ import annotations

import io
import os


def prepare_inline_image(
    src_path: str,
    cache_path_base: str,
    max_width: int = 600,
    max_bytes: int = 500_000,
) -> tuple[bytes, str]:
    """Zwraca (bajty, subtype) gotowego do osadzenia obrazu. Wynik jest
    cache'owany pod `cache_path_base` + '.png'/'.jpg' (rozszerzenie zależy od
    tego, który format wygrał kompresję) i przeliczany tylko, gdy źródło jest
    nowsze niż cache."""
    png_cache = cache_path_base + ".png"
    jpg_cache = cache_path_base + ".jpg"
    for cache_path, subtype in ((png_cache, "png"), (jpg_cache, "jpeg")):
        if os.path.exists(cache_path) and os.path.getmtime(cache_path) > os.path.getmtime(src_path):
            with open(cache_path, "rb") as f:
                return f.read(), subtype

    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow nie jest zainstalowane. Zainstaluj: pip3 install pillow"
        ) from exc

    img = Image.open(src_path)
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, "white")
        mask = img.split()[-1] if img.mode == "RGBA" else None
        background.paste(img, mask=mask)
        img = background

    if img.width > max_width:
        new_height = round(img.height * max_width / img.width)
        img = img.resize((max_width, new_height), Image.LANCZOS)

    buf = io.BytesIO()
    img.convert("P", palette=Image.ADAPTIVE, colors=256).save(buf, format="PNG", optimize=True)
    data, subtype = buf.getvalue(), "png"

    if len(data) > max_bytes:
        buf_jpeg = io.BytesIO()
        img.convert("RGB").save(buf_jpeg, format="JPEG", quality=82, optimize=True)
        if len(buf_jpeg.getvalue()) < len(data):
            data, subtype = buf_jpeg.getvalue(), "jpeg"

    final_cache_path = jpg_cache if subtype == "jpeg" else png_cache
    stale_cache_path = png_cache if subtype == "jpeg" else jpg_cache
    os.makedirs(os.path.dirname(final_cache_path), exist_ok=True)
    with open(final_cache_path, "wb") as f:
        f.write(data)
    if os.path.exists(stale_cache_path):
        os.remove(stale_cache_path)
    return data, subtype
