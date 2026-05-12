"""
Image preprocessing for handwritten math pages.

Converts a raw photo to a clean binarized PNG suitable for OCR:
greyscale → resize to A4 at 150 dpi → sharpen → denoise → deskew → binarize (Otsu).
"""

from PIL import Image, ImageFilter
import cv2
import numpy as np
import io

def preprocess_image(img_bytes: bytes) -> bytes:
    pil_img = Image.open(io.BytesIO(img_bytes))
    pil_img = pil_img.convert("L")          # greyscale
    pil_img = pil_img.resize((1240, 1754))  # a4 @ 150dpi
    pil_img = pil_img.filter(ImageFilter.SHARPEN)

    arr = np.array(pil_img)
    arr = cv2.fastNlMeansDenoising(arr, h=15)  # denoise
    arr = deskew(arr)
    _, arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)  # binarize

    _, buffer = cv2.imencode(".png", arr)
    return buffer.tobytes()


def deskew(arr: np.ndarray) -> np.ndarray:
    coords = np.column_stack(np.where(arr < 128))  # dark pixels
    if len(coords) == 0:
        return arr
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90 + angle

    (h, w) = arr.shape
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(arr, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
