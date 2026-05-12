import io
import pathlib
import numpy as np
import pytest
from PIL import Image
from detector import detect_lines, LineCrop, CropFlag
from processor import preprocess_image

IMAGE_DIR = pathlib.Path(__file__).parent / "images"


def make_preprocessed_bytes(height=200, width=100, bands: list[tuple[int, int]] | None = None) -> bytes:
    """White image (255) with optional black pixel bands at specified row ranges."""
    arr = np.full((height, width), 255, dtype=np.uint8)
    for y0, y1 in (bands or []):
        arr[y0:y1, :] = 0
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def test_blank_returns_no_lines():
    result = detect_lines(make_preprocessed_bytes())
    assert result == []


def test_single_band_returns_one_line():
    result = detect_lines(make_preprocessed_bytes(bands=[(20, 40)]))
    assert len(result) == 1


def test_multiple_bands_returns_correct_count():
    result = detect_lines(make_preprocessed_bytes(height=300, bands=[(20, 40), (100, 120), (200, 220)]))
    assert len(result) == 3


def test_crop_width_matches_image_width():
    # full-width band → no columns to trim, crop width equals image width
    width = 150
    result = detect_lines(make_preprocessed_bytes(width=width, bands=[(20, 40)]))
    assert result[0].image.width == width


def test_crop_height_includes_padding():
    band_h = 40 - 20  # 20
    padding = 4
    result = detect_lines(make_preprocessed_bytes(bands=[(20, 40)]), padding=padding)
    assert result[0].image.height >= band_h + padding


def test_thin_band_filtered_out():
    # band only 5 rows tall, min_line_height default is 8 — should be filtered
    result = detect_lines(make_preprocessed_bytes(bands=[(20, 25)]))
    assert len(result) == 0


def test_far_bands_are_not_merged():
    # gap of 80 rows — well beyond any clustering threshold
    result = detect_lines(make_preprocessed_bytes(height=300, bands=[(20, 40), (140, 160)]))
    assert len(result) == 2


def test_returns_linecrop_objects():
    result = detect_lines(make_preprocessed_bytes(bands=[(20, 40)]))
    for lc in result:
        assert isinstance(lc, LineCrop)


def test_linecrop_image_is_pil():
    result = detect_lines(make_preprocessed_bytes(bands=[(20, 40)]))
    assert isinstance(result[0].image, Image.Image)


def test_linecrop_flags_is_cropflag():
    result = detect_lines(make_preprocessed_bytes(bands=[(20, 40)]))
    assert isinstance(result[0].flags, CropFlag)


def test_linecrop_has_valid_coordinates():
    result = detect_lines(make_preprocessed_bytes(bands=[(20, 40)]))
    lc = result[0]
    assert lc.y0 < lc.y1
    assert lc.x0 < lc.x1


def test_results_sorted_top_to_bottom():
    result = detect_lines(make_preprocessed_bytes(height=300, bands=[(20, 40), (100, 120), (200, 220)]))
    assert len(result) == 3
    y0_values = [lc.y0 for lc in result]
    assert y0_values == sorted(y0_values)


# --- Real image harness ---

def _real_images() -> list[pathlib.Path]:
    return list(IMAGE_DIR.glob("*.png")) + list(IMAGE_DIR.glob("*.jpg"))


@pytest.mark.parametrize("path", _real_images())
def test_real_image_detects_lines(path):
    raw = path.read_bytes()
    clean = preprocess_image(raw)
    lines = detect_lines(clean)
    assert isinstance(lines, list)
    assert len(lines) > 0, f"No lines detected in {path.name}"
    for lc in lines:
        assert isinstance(lc, LineCrop)
        assert isinstance(lc.image, Image.Image)
        assert lc.image.width > 0 and lc.image.height > 0
        assert isinstance(lc.flags, CropFlag)
