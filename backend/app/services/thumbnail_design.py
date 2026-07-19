"""Will-approved thumbnail design system (the "Will See" brand look).

Every AI-art thumbnail/cover is built in two layers:

1. **Scene** — a fal-generated warm-desert psychedelic illustration with ONE
   dramatic genre-driven focal subject working in an EYE motif, filling the
   right two-thirds of the frame (wide) with the left third kept darker for
   text. The prompt pins the palette to the six brand colors and explicitly
   forbids any text in the art (the overlay owns all typography).
2. **Deterministic PIL overlay** (:func:`compose_thumbnail`) — a warm dark
   scrim with a gaussian-blurred edge, a <=3-word hook in the Press Start 2P
   pixel font (huge, cream, hard black shadow + dark-brown stroke), an
   accent-color bar under the text, and a "WILL SEE" pixel tag on a dark-brown
   chip.

The design was validated visually by Will against five prototypes (dnb coyote,
dubstep sandstone totem, house sunflower-eye, trance planet-eye oasis, edm
ember phoenix); those five motifs are ported verbatim and the remaining genre
buckets are authored in the same voice.
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.config import settings

logger = logging.getLogger(__name__)

# Brand palette (kept in the scene prompt verbatim)
CREAM_TEXT = (248, 240, 210)
DARK_BROWN = (62, 44, 35)
SCRIM_BROWN = (33, 22, 16)

ACCENT_AMBER = (217, 160, 91)      # #D9A05B
ACCENT_ORANGE = (180, 85, 45)      # #B4552D
ACCENT_SAGE = (122, 158, 126)      # #7A9E7E
ACCENT_ROSE = (232, 180, 160)      # #E8B4A0

# The /data fallback kept for operators who mount their own font.
DATA_FONT_PATH = "/data/fonts/PressStart2P-Regular.ttf"

# Repo-bundled font (backend/assets/fonts) — resolves both in the Docker image
# (/app/backend/assets/fonts) and in local test runs.
_BUNDLED_FONT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "assets", "fonts",
        "PressStart2P-Regular.ttf",
    )
)

# ---------------------------------------------------------------------------
# Genre motifs
# ---------------------------------------------------------------------------
# One dramatic desert-psychedelic focal subject per genre bucket, each with an
# eye motif, a distinct <=3-word hook, and an accent from the brand palette.
# The first five are Will's approved prototypes, ported verbatim.

GENRE_MOTIFS: Dict[str, Dict[str, object]] = {
    "drum and bass": {
        "scene": (
            "a sleek cybernetic desert coyote mid-leap between towering "
            "saguaro cacti at dusk, one large glowing amber eye staring at "
            "the viewer, circuit patterns in burnt-orange and copper on its "
            "body, dusty rose sky, red rock mesas"
        ),
        "accent": ACCENT_ORANGE,
        "hook": "DNB\nOVERDRIVE",
    },
    "dubstep": {
        "scene": (
            "a massive carved sandstone totem skull with one huge hypnotic "
            "spiral eye, cracks glowing with molten copper light, desert "
            "night in deep brown and burnt orange, warm dust haze"
        ),
        "accent": ACCENT_ROSE,
        "hook": "BASS\nRITUAL",
    },
    "house": {
        "scene": (
            "a giant blooming desert sunflower whose center is a serene "
            "human eye, melting golden-hour sun behind it, saguaros and "
            "wildflowers, warm amber and cream with sage green leaves"
        ),
        "accent": ACCENT_AMBER,
        "hook": "HOUSE\nTHERAPY",
    },
    "trance": {
        "scene": (
            "an enormous crystalline planet-eye floating over a mirror-still "
            "desert oasis at twilight, sage-green and teal aurora ribbons "
            "over amber horizon glow, red rock silhouettes, stars"
        ),
        "accent": ACCENT_SAGE,
        "hook": "3AM\nTRANCE",
    },
    "edm": {
        "scene": (
            "a titanic phoenix made of golden ember fireworks erupting over "
            "a desert festival crowd silhouette, one blazing amber eye in "
            "the burst, burnt orange and cream sparks against a deep "
            "brown-violet dusk sky"
        ),
        "accent": ACCENT_ORANGE,
        "hook": "FESTIVAL\nFUEL",
    },
    # --- authored in the same voice ---
    "trap": {
        "scene": (
            "a colossal desert rattlesnake coiled around a cracked adobe "
            "speaker stack, its single hypnotic amber eye wide open, heat "
            "shimmer and dusty rose smoke curling into a burnt-orange dusk, "
            "saguaro silhouettes"
        ),
        "accent": ACCENT_ROSE,
        "hook": "DESERT\nTRAP",
    },
    "deep house": {
        "scene": (
            "a sunken desert canyon pool at dusk with one enormous calm eye "
            "gazing up from beneath the glassy water, sage reeds and smooth "
            "red stones ringing the edge, soft amber light rays bending "
            "underwater"
        ),
        "accent": ACCENT_SAGE,
        "hook": "DEEP\nDIVE",
    },
    "tech house": {
        "scene": (
            "a monumental geometric adobe machine-temple in the open desert, "
            "interlocking terracotta gears and one rotating mechanical eye "
            "at its center, clean warm shadows, cream sky, cacti standing "
            "in rows like a crowd"
        ),
        "accent": ACCENT_AMBER,
        "hook": "GROOVE\nMACHINE",
    },
    "techno": {
        "scene": (
            "a towering monolithic basalt obelisk standing alone on cracked "
            "desert flats at night, a single stark geometric eye carved into "
            "it pulsing rings of molten copper light, long hard shadows, "
            "deep brown sky"
        ),
        "accent": ACCENT_ORANGE,
        "hook": "TECHNO\nPULSE",
    },
    "garage": {
        "scene": (
            "a chrome-feathered desert roadrunner sprinting through rippling "
            "waves of dust, one wide amber eye locked forward, shuffled "
            "footprints glowing dusty rose behind it, burnt-orange mesas and "
            "a cream sunset sky"
        ),
        "accent": ACCENT_ROSE,
        "hook": "GARAGE\nHEAT",
    },
    "organic": {
        "scene": (
            "a giant night-blooming cactus flower opening under a huge desert "
            "moon, a gentle luminous eye at the flower's heart, sage vines "
            "and soft pollen motes drifting, warm amber moonglow over quiet "
            "dunes"
        ),
        "accent": ACCENT_SAGE,
        "hook": "DESERT\nBLOOM",
    },
    "open format": {
        "scene": (
            "a towering totem stacked from carved desert idols each holding a "
            "different watching eye, one great amber eye crowning the top, "
            "prayer-flag ribbons in cream and dusty rose, saguaro valley at "
            "golden hour"
        ),
        "accent": ACCENT_AMBER,
        "hook": "ALL\nVIBES",
    },
}

# genre-string aliases -> motif key. Checked in order (most specific first) so
# "deep house" never falls through to "house", etc.
_GENRE_ALIASES: List[Tuple[str, str]] = [
    ("deep house", "deep house"),
    ("tech house", "tech house"),
    ("drum and bass", "drum and bass"),
    ("drum & bass", "drum and bass"),
    ("dnb", "drum and bass"),
    ("d&b", "drum and bass"),
    ("jungle", "drum and bass"),
    ("liquid", "drum and bass"),
    ("neurofunk", "drum and bass"),
    ("dubstep", "dubstep"),
    ("riddim", "dubstep"),
    ("brostep", "dubstep"),
    ("bass music", "dubstep"),
    ("trap", "trap"),
    ("big room", "edm"),
    ("edm", "edm"),
    ("electro house", "edm"),
    ("garage", "garage"),
    ("ukg", "garage"),
    ("2-step", "garage"),
    ("2step", "garage"),
    ("breakbeat", "garage"),
    ("breaks", "garage"),
    ("melodic", "organic"),
    ("progressive", "organic"),
    ("organic", "organic"),
    ("ambient", "organic"),
    ("downtempo", "organic"),
    ("chill", "organic"),
    ("trance", "trance"),
    ("techno", "techno"),
    ("house", "house"),
    ("open format", "open format"),
]

FALLBACK_GENRE_KEY = "open format"


def resolve_genre_key(genres: Optional[List[str]]) -> str:
    """Map a mix's ordered genre list to the best GENRE_MOTIFS key.

    Genres are resolved in mix order (``genres[0]`` is the dominant genre):
    an exact motif-key match wins for that genre, then alias substring
    matching (most specific aliases first). Falls back to ``open format``
    when nothing matches ("electronic", empty, etc.).
    """
    for genre in genres or []:
        g = (genre or "").strip().lower()
        if not g:
            continue
        if g in GENRE_MOTIFS:
            return g
        for alias, key in _GENRE_ALIASES:
            if alias in g:
                return key
    return FALLBACK_GENRE_KEY


# ---------------------------------------------------------------------------
# Scene prompt
# ---------------------------------------------------------------------------

BASE_SCENE_PROMPT_WIDE = (
    "Warm psychedelic desert art in a cozy hand-illustrated style, "
    "bold shapes, clean composition, single "
    "dramatic focal subject filling the right two-thirds of the frame, "
    "left third darker and simpler for text, dark vignette corners, "
    "color palette strictly: warm amber #D9A05B, burnt orange #B4552D, "
    "sage #7A9E7E, cream #F2E3C6, dark brown #3E2C23, dusty rose #E8B4A0. "
    "{scene}. Absolutely no text, letters or numbers anywhere."
)

BASE_SCENE_PROMPT_SQUARE = (
    "Warm psychedelic desert art in a cozy hand-illustrated style, "
    "bold shapes, clean composition, single "
    "dramatic focal subject filling the lower two-thirds of the frame, "
    "upper third darker and simpler for text, dark vignette corners, "
    "color palette strictly: warm amber #D9A05B, burnt orange #B4552D, "
    "sage #7A9E7E, cream #F2E3C6, dark brown #3E2C23, dusty rose #E8B4A0. "
    "{scene}. Absolutely no text, letters or numbers anywhere."
)


def build_scene_prompt(
    genre_key: str, brand_settings=None, aspect: str = "wide"
) -> str:
    """The full fal prompt for a genre motif's scene.

    ``brand_settings.genre_visual_modifiers`` may override a motif's scene by
    genre key (the rest of the design — palette, composition, no-text — is
    fixed brand identity and not overridable here).
    """
    motif = GENRE_MOTIFS.get(genre_key) or GENRE_MOTIFS[FALLBACK_GENRE_KEY]
    scene = str(motif["scene"])
    overrides = getattr(brand_settings, "genre_visual_modifiers", None) or {}
    if isinstance(overrides, dict) and overrides.get(genre_key):
        scene = str(overrides[genre_key])
    base = BASE_SCENE_PROMPT_SQUARE if aspect == "square" else BASE_SCENE_PROMPT_WIDE
    return base.format(scene=scene)


# ---------------------------------------------------------------------------
# Deterministic PIL overlay
# ---------------------------------------------------------------------------

def _font_path() -> Optional[str]:
    """Resolve the pixel font: configured path -> /data fallback -> bundled."""
    candidates = [
        settings.PIXEL_FONT_PATH,
        DATA_FONT_PATH,
        _BUNDLED_FONT_PATH,
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _load_font(size: int):
    path = _font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # pragma: no cover - corrupt font file
            logger.warning("Could not load pixel font at %s", path, exc_info=True)
    try:  # pragma: no cover - Pillow always bundles a default
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _fit_font(draw, text: str, start_size: int, max_w: int, max_h: int, spacing: int):
    """Largest pixel-font size (stepping down by 6) whose text block fits."""
    size = start_size
    font = _load_font(size)
    while size > 16:
        box = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
        if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
            break
        size -= 6
        font = _load_font(size)
    return font


def compose_thumbnail(
    art_path: str,
    hook_text: str,
    accent: Tuple[int, int, int],
    out_path: str,
    size: Tuple[int, int] = (1280, 720),
) -> str:
    """Compose the deterministic brand overlay onto a generated scene.

    Wide (16:9) layout: warm dark scrim over the left half (gaussian-blurred
    edge), the hook huge in Press Start 2P on the left, accent bar under it,
    "WILL SEE" pixel tag bottom-left on a dark-brown chip.

    Square layout (w == h, SoundCloud cover art): the scrim becomes a top
    band and the hook is centered in the upper third; tag stays bottom-left.

    Writes JPEG to ``out_path`` (may equal ``art_path``) and returns it.
    """
    w, h = size
    square = w == h
    img = Image.open(art_path).convert("RGB")
    if img.size != (w, h):
        img = img.resize((w, h), Image.LANCZOS)
    d = ImageDraw.Draw(img)

    # Scrim so text always reads: left half (wide) or top band (square),
    # warm dark brown with a blurred edge.
    scrim = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(scrim)
    if square:
        sd.rectangle([0, 0, w, int(h * 0.40)], fill=110)
    else:
        sd.rectangle([0, 0, int(w * 0.52), h], fill=110)
    scrim = scrim.filter(ImageFilter.GaussianBlur(60))
    img.paste(Image.new("RGB", (w, h), SCRIM_BROWN), (0, 0), scrim)
    d = ImageDraw.Draw(img)

    # Hook text: pixel font, huge, cream with hard shadow + dark-brown stroke.
    spacing = 26
    if square:
        start_size = int(h * 0.09)
        font = _fit_font(d, hook_text, start_size, int(w * 0.86), int(h * 0.30), spacing)
        box = d.multiline_textbbox((0, 0), hook_text, font=font, spacing=spacing)
        x = (w - (box[2] - box[0])) // 2
        y = int(h * 0.09)
        align = "center"
    else:
        font = _fit_font(d, hook_text, 92, int(w * 0.46), int(h * 0.5), spacing)
        x, y = int(w * 0.045), int(h * 0.30)
        align = "left"

    d.multiline_text(
        (x + 6, y + 8), hook_text, font=font, fill=(0, 0, 0),
        spacing=spacing, align=align,
    )  # hard shadow
    d.multiline_text(
        (x, y), hook_text, font=font, fill=CREAM_TEXT,
        spacing=spacing, align=align, stroke_width=6, stroke_fill=DARK_BROWN,
    )

    # Accent bar under the text.
    box = d.multiline_textbbox((x, y), hook_text, font=font, spacing=spacing)
    if square:
        bar_w = int((box[2] - box[0]) * 0.72)
        bar_x = (w - bar_w) // 2
        d.rectangle([bar_x, box[3] + 26, bar_x + bar_w, box[3] + 40], fill=accent)
    else:
        d.rectangle(
            [x, box[3] + 26, x + int((box[2] - x) * 0.72), box[3] + 40], fill=accent
        )

    # WILL SEE brand tag, bottom-left, consistent placement.
    tag_scale = 30 if not square else max(30, int(h * 0.024))
    tag_font = _load_font(tag_scale)
    tx = int(w * 0.045)
    ty = h - 74 if not square else h - int(h * 0.075)
    tb = d.textbbox((0, 0), "WILL SEE", font=tag_font)
    d.rectangle([tx - 14, ty - 12, tx + tb[2] + 14, ty + tb[3] + 16], fill=DARK_BROWN)
    d.text((tx, ty), "WILL SEE", font=tag_font, fill=accent)

    img.save(out_path, "JPEG", quality=93)
    return out_path
