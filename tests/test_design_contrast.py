"""Kontrast tokenów designu (WCAG 2.x) w obu motywach: tekst ≥ 4,5:1, ikony i akcenty ≥ 3:1, odznaki wszystkich statusów ≥ 4,5:1.

Liczymy z `src/perno/design/tokens.css`, więc zmiana koloru, której nie widać na jednym ekranie, nie przejdzie niezauważona.
"""

from __future__ import annotations

import re
import unittest

from perno.design import read_tokens
from perno.domain.statuses import STATUS_META
from perno.scraper.statuses import FIRMA_META

WHITE = (255.0, 255.0, 255.0, 1.0)


def parse_color(text: str) -> tuple[float, float, float, float]:
    text = text.strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return (float(int(digits[0:2], 16)), float(int(digits[2:4], 16)), float(int(digits[4:6], 16)), 1.0)
    parts = [float(x) for x in re.match(r"rgba?\(([^)]+)\)", text).group(1).split(",")]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def over(fg, bg):
    """Kolor z kanałem alfa położony na nieprzezroczystym tle."""
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def tint(color, alpha):
    return (color[0], color[1], color[2], alpha)


def mix(color, share, other):
    """color-mix(in srgb, color share, other) dla kolorów nieprzezroczystych."""
    return tuple(color[i] * share + other[i] * (1 - share) for i in range(3)) + (1.0,)


def luminance(color) -> float:
    def channel(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2])


def contrast(a, b) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def declarations(block: str) -> dict[str, str]:
    return {k: v.strip() for k, v in re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block) if not v.strip().startswith("url(")}


def themes() -> dict[str, dict[str, str]]:
    css = read_tokens()
    light = declarations(css[css.index(":root{"):css.index("@media")])
    dark = dict(light)
    dark.update(declarations(css[css.index("@media"):]))
    return {"jasny": light, "ciemny": dark}


class ContrastTests(unittest.TestCase):
    def assert_contrast(self, mode: str, name: str, fg, bg, minimum: float) -> None:
        ratio = contrast(fg, bg)
        self.assertGreaterEqual(ratio, minimum, f"{mode}: {name} ma kontrast {ratio:.2f}:1, wymagane {minimum}:1")

    def test_text_and_accents_meet_wcag_in_both_themes(self):
        for mode, tokens in themes().items():
            with self.subTest(theme=mode):
                c = {k: parse_color(v) for k, v in tokens.items() if re.match(r"(#|rgb)", v)}
                bg, surface, surface2 = c["--bg"], c["--surface"], c["--surface-2"]
                fill = over(c["--fill"], bg)
                tint_surface, tint_bg = over(c["--accent-tint"], surface), over(c["--accent-tint"], bg)
                for name, fg, back, need in (
                    ("label na surface", c["--label"], surface, 7),
                    ("label-2 na surface", c["--label-2"], surface, 4.5),
                    ("label-2 na bg", c["--label-2"], bg, 4.5),
                    ("label-2 na nagłówku tabeli", c["--label-2"], surface2, 4.5),
                    ("label-2 na wypełnieniu (pigułka, pole)", c["--label-2"], fill, 4.5),
                    ("placeholder (label-3) na wypełnieniu", c["--label-3"], fill, 3),
                    ("biały na akcencie (przycisk)", WHITE, c["--accent"], 4.5),
                    ("biały na akcencie po najechaniu", WHITE, c["--accent-hover"], 4.5),
                    ("biały na czerwonym przycisku", WHITE, c["--danger-fill"], 4.5),
                    ("accent-ink na surface (link)", c["--accent-ink"], surface, 4.5),
                    ("accent-ink na tincie nad surface", c["--accent-ink"], tint_surface, 4.5),
                    ("accent-ink na tincie nad bg", c["--accent-ink"], tint_bg, 4.5),
                    ("ok na surface", c["--ok"], surface, 4.5),
                    ("warn na surface", c["--warn"], surface, 4.5),
                    ("danger na surface", c["--danger"], surface, 4.5),
                ):
                    self.assert_contrast(mode, name, fg, back, need)
                for tone in ("--ok", "--warn", "--danger"):
                    callout = over(tint(c[tone], 0.12), surface)
                    self.assert_contrast(mode, f"label w callout {tone}", c["--label"], callout, 7)
                    self.assert_contrast(mode, f"ikona callouta {tone}", c[tone], callout, 3)

    def test_status_badges_and_chips_are_readable_for_every_status_color(self):
        for mode, tokens in themes().items():
            surface, bg, label = parse_color(tokens["--surface"]), parse_color(tokens["--bg"]), parse_color(tokens["--label"])
            share = float(tokens["--ink-mix"].rstrip("%")) / 100
            for table in (STATUS_META, FIRMA_META):
                for code, meta in table.items():
                    color = parse_color(meta[1])
                    text = mix(color, share, label)
                    for background, alpha in ((surface, 0.16), (bg, 0.16), (surface, 0.18), (bg, 0.18)):
                        with self.subTest(theme=mode, status=code, alpha=alpha):
                            self.assert_contrast(mode, f"odznaka {code} {meta[1]}", text, over(tint(color, alpha), background), 4.5)

    def test_rating_tokens_point_at_the_tones_and_their_badges_are_readable(self):
        """Oceny (pozytywna / neutralna / negatywna) używają tonów ok / warn / danger i muszą być czytelne jak odznaki statusów."""
        pointers = {"--rating-pos": "--ok", "--rating-neu": "--warn", "--rating-neg": "--danger"}
        css = read_tokens()
        for name, tone in pointers.items():
            self.assertIn(f"{name}:var({tone})", css.replace(" ", ""), f"{name} ma wskazywać na {tone}")
        for mode, tokens in themes().items():
            surface, bg, label = parse_color(tokens["--surface"]), parse_color(tokens["--bg"]), parse_color(tokens["--label"])
            share = float(tokens["--ink-mix"].rstrip("%")) / 100
            for name, tone in pointers.items():
                color = parse_color(tokens[tone])
                text = mix(color, share, label)
                for background in (surface, bg):
                    with self.subTest(theme=mode, rating=name):
                        self.assert_contrast(mode, f"odznaka oceny {name}", text, over(tint(color, 0.16), background), 4.5)
                        self.assert_contrast(mode, f"przycisk oceny {name} na tle odznaczonego przełącznika", text, surface, 4.5)


if __name__ == "__main__":
    unittest.main()
