"""
Profile photo processing — validate, resize, and compress an uploaded
image into a small inline "data:image/jpeg;base64,..." URI.

Why inline (no S3): this portal has no S3 bucket accessible to the
backend anywhere in its infra (the only bucket that exists is the private
CloudFront-fronted frontend static-asset bucket — see
infra-cdk/infra_cdk/stacks/frontend_stack.py — which is unrelated and not
writable by the backend task role). Standing up a new bucket + IAM grant
just for small avatar images would be a disproportionate amount of new
infrastructure for this feature. Instead, photos are resized down to a
small square thumbnail and re-encoded as JPEG, which keeps the resulting
base64 string comfortably within DynamoDB's 400KB per-item limit (and
SQLite has no such limit locally) — stored directly on the PortalUser /
job-portal-users row (see user_store.py's photo_data_url field).
"""

from __future__ import annotations

import base64
import io

from PIL import Image, UnidentifiedImageError

# Original upload size cap — generous enough for any phone/camera photo,
# rejected before we even try to decode it (avoids wasting CPU on huge
# multi-hundred-MB files).
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB

# Final square thumbnail dimensions and JPEG quality. 256x256 at quality
# 82 typically comes out to ~15-35KB (well under DynamoDB's 400KB/item
# limit even after base64's ~33% size inflation).
THUMBNAIL_SIZE = (256, 256)
JPEG_QUALITY = 82


class ProfilePhotoError(ValueError):
    """Raised for any invalid upload — message is safe to show the user."""


def process_upload(content: bytes) -> str:
    """
    Validate `content` is a real image, then resize/crop it to a
    THUMBNAIL_SIZE square JPEG and return it as a
    "data:image/jpeg;base64,..." URI ready to store directly on a user
    row and render directly in an <img src=...>.

    Raises ProfilePhotoError for anything invalid (empty file, too large,
    not a real image, corrupt image data).
    """
    if not content:
        raise ProfilePhotoError("Uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ProfilePhotoError("Photo is too large — maximum 8 MB")

    try:
        img = Image.open(io.BytesIO(content))
        img.load()  # force full decode now, so a truncated/corrupt file raises here
    except UnidentifiedImageError:
        raise ProfilePhotoError("File is not a recognizable image")
    except Exception as e:
        raise ProfilePhotoError(f"Could not read image file: {e}")

    # Flatten to RGB (drops alpha/palette modes — JPEG has no alpha
    # channel anyway, and a plain white/neutral background reads better
    # for a small circular avatar than a transparent one composited onto
    # whatever background color a browser happens to pick).
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1])
        else:
            background.paste(img.convert("RGB"))
        img = background
    elif img.mode == "L":
        img = img.convert("RGB")

    # Center-crop to a square first (so resizing to a fixed square
    # thumbnail doesn't squash/stretch a non-square source photo), then
    # downscale to the final thumbnail size.
    width, height = img.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize(THUMBNAIL_SIZE, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
