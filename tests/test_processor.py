import io
import numpy as np
import pytest
from PIL import Image
from processor import preprocess_image, deskew


def make_image_bytes(w=200, h=200, color=128):
    """make a simple grey png in memory"""
    img = Image.fromarray(np.full((h, w), color, dtype=np.uint8), mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_preprocess_returns_bytes():
    result = preprocess_image(make_image_bytes())
    assert isinstance(result, bytes) and len(result) > 0


def test_preprocess_output_is_valid_png():
    result = preprocess_image(make_image_bytes())
    img = Image.open(io.BytesIO(result))
    assert img.format == "PNG"


def test_preprocess_output_size():
    result = preprocess_image(make_image_bytes())
    img = Image.open(io.BytesIO(result))
    assert img.size == (1240, 1754)


def test_deskew_blank_image():
    # all-white — no dark pixels, should return unchanged
    arr = np.full((100, 100), 255, dtype=np.uint8)
    result = deskew(arr)
    assert result.shape == arr.shape


def test_deskew_normal_image():
    arr = np.random.randint(0, 256, (100, 100), dtype=np.uint8)
    result = deskew(arr)
    assert result.shape == arr.shape
