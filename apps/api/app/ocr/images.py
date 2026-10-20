"""アップロード画像の検証と正規化(API 側、Pillow のみ)。

- 画像として読めないもの・対応外形式・大きすぎるものは受け付けない。
- EXIF(撮影位置などの個人情報を含みうる)を除去し、向きだけ反映して保存し直す。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import get_settings
from app.core.errors import ApiError

ALLOWED = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass
class NormalizedImage:
    data: bytes
    content_type: str
    width: int
    height: int
    original_sha256: str


@dataclass
class ProbedImage:
    format: str
    width: int
    height: int


def check_size(n: int) -> None:
    s = get_settings()
    if n == 0:
        raise ApiError(422, "IMAGE_EMPTY", "画像ファイルが空です")
    if n > s.receipt_max_bytes:
        raise ApiError(413, "IMAGE_TOO_LARGE", f"画像は{s.receipt_max_bytes // (1024 * 1024)}MB以下にしてください")


def probe(raw: bytes) -> ProbedImage:
    """ヘッダーだけを読み、形式と寸法を確かめる(画素のデコードはしない)。重い処理の前に呼ぶ。"""
    s = get_settings()
    check_size(len(raw))
    try:
        with Image.open(io.BytesIO(raw)) as im:  # 遅延読み込み: ここではヘッダーのみ
            fmt, (w, h) = im.format or "", im.size
    except Image.DecompressionBombError:
        # Pillow は寸法がその上限の 2 倍を超えるとヘッダーの時点で拒否する
        raise ApiError(413, "IMAGE_TOO_MANY_PIXELS", "画像の解像度が大きすぎます。縮小してから選んでください") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ApiError(415, "IMAGE_UNREADABLE", "画像を読み込めませんでした。別の画像を選んでください") from None
    if fmt not in ALLOWED:
        raise ApiError(415, "IMAGE_TYPE_UNSUPPORTED", "JPEG・PNG・WebP の画像を選んでください(HEIC は未対応です)")
    if w <= 0 or h <= 0:
        raise ApiError(415, "IMAGE_UNREADABLE", "画像を読み込めませんでした。別の画像を選んでください")
    # Pillow の MAX_IMAGE_PIXELS は上限の 2 倍まで警告だけなので、ここで明示的に拒否する
    if w * h > s.receipt_max_pixels or max(w, h) > s.receipt_max_side:
        raise ApiError(413, "IMAGE_TOO_MANY_PIXELS", "画像の解像度が大きすぎます。縮小してから選んでください")
    return ProbedImage(fmt, w, h)


def normalize_upload(raw: bytes) -> NormalizedImage:
    s = get_settings()
    probe(raw)
    Image.MAX_IMAGE_PIXELS = s.receipt_max_pixels
    try:
        with Image.open(io.BytesIO(raw)) as probe_img:
            probe_img.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            out = io.BytesIO()
            # EXIF・ICC などのメタデータは付けずに保存し直す
            img.save(out, format="JPEG", quality=92, optimize=True)
            w, h = img.size
    except ApiError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise ApiError(415, "IMAGE_UNREADABLE", "画像を読み込めませんでした。別の画像を選んでください") from None
    return NormalizedImage(out.getvalue(), "image/jpeg", w, h, hashlib.sha256(raw).hexdigest())


def storage_root() -> Path:
    return Path(get_settings().receipt_storage_dir)


def save_image(receipt_id: uuid.UUID, data: bytes) -> str:
    key = f"{receipt_id}.jpg"
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{key}.tmp"
    tmp.write_bytes(data)
    tmp.replace(root / key)  # 途中で落ちても壊れたファイルを残さない
    return key


def image_path(key: str) -> Path:
    p = (storage_root() / key).resolve()
    if p.parent != storage_root().resolve():  # パス操作の防止
        raise ValueError("invalid storage key")
    return p


def delete_image(key: str) -> None:
    image_path(key).unlink(missing_ok=True)
