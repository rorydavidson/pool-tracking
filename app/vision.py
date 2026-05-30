"""Read a pool test-strip photo with Claude vision.

The user photographs a dipped test strip next to its colour key (the printed
chart on the bottle, as in the bundled example). Claude compares each pad to
the key *in the same image* and returns numeric values, which we use to
pre-fill the reading form for the user to confirm.

Only the parameters our readings store are returned; anything Claude can't read
confidently is left null rather than guessed.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

from pydantic import BaseModel, Field

from .config import get_settings

logger = logging.getLogger("pool_tracking.vision")

# Accepted upload types → the media_type Claude expects.
SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/heic": "image/heic",
    "image/heif": "image/heif",
}

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB

VISION_SYSTEM_PROMPT = """\
You read residential pool/spa test strips from a photo. The photo contains a \
dipped test strip and, usually, the manufacturer's colour key (a printed chart \
with labelled value columns). Compare each pad on the strip to the colour key \
shown in the same image and estimate the value for each parameter.

Rules:
- Use ONLY the key visible in the image to interpret colours. If no key is \
visible, interpret using standard pool test-strip conventions but lower your \
confidence.
- Match each pad to the nearest column; interpolate between columns when the \
colour clearly falls between two.
- Return numbers in the units the strip's key uses (ppm for chlorine, \
alkalinity, hardness, stabiliser; pH is unitless).
- If a pad is unreadable, missing, or you are not reasonably sure, return null \
for that parameter. Do not guess.
- free_chlorine: read the "Free Chlorine"/"FCl" pad (ppm). \
total_alkalinity: the "Alkalinity"/"Alk" pad. ph: the "pH" pad. \
total_chlorine, cyanuric_acid (stabiliser/CYA) and calcium_hardness \
(total hardness) only if present on the strip."""


class StripReading(BaseModel):
    ph: Optional[float] = Field(default=None, description="pH pad value, or null.")
    free_chlorine: Optional[float] = Field(default=None, description="Free chlorine ppm, or null.")
    total_chlorine: Optional[float] = Field(default=None, description="Total chlorine ppm, or null.")
    total_alkalinity: Optional[float] = Field(default=None, description="Total alkalinity ppm, or null.")
    cyanuric_acid: Optional[float] = Field(default=None, description="Cyanuric acid / stabiliser ppm, or null.")
    calcium_hardness: Optional[float] = Field(default=None, description="Total/calcium hardness ppm, or null.")
    confidence: str = Field(description="Overall confidence: high, medium, or low.")
    notes: Optional[str] = Field(
        default=None,
        description="Brief note on anything ambiguous (e.g. 'no key visible', 'pH pad overexposed').",
    )


class VisionUnavailable(RuntimeError):
    """Raised when strip reading can't run (no API key) or the call fails."""


def read_test_strip(image_bytes: bytes, media_type: str) -> StripReading:
    """Interpret a test-strip photo, returning per-parameter estimates.

    Raises :class:`VisionUnavailable` if no Anthropic key is configured or the
    vision call fails, so the caller can fall back to manual entry.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise VisionUnavailable("Photo reading needs an ANTHROPIC_API_KEY; enter values manually.")

    try:
        return _read_with_claude(image_bytes, media_type, settings)
    except VisionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Vision strip reading failed")
        raise VisionUnavailable(f"Could not read the photo: {exc}") from exc


def _read_with_claude(image_bytes: bytes, media_type: str, settings) -> StripReading:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")

    response = client.messages.parse(
        model=settings.advice_model,
        max_tokens=1024,
        system=VISION_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Read this pool test strip against its colour key and return the values.",
                    },
                ],
            }
        ],
        output_format=StripReading,
    )
    return response.parsed_output
