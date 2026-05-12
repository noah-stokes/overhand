"""
Splits a preprocessed handwritten-math page into one crop per line.

Pipeline: extract ink blobs → cluster into lines → share tall symbols (integrals,
brackets) across the lines they overlap → absorb orphan dots/punctuation into the
nearest real line → recompute bounding boxes → trim whitespace → flag suspicious crops.

Each LineCrop that comes out is ready to pass straight to the OCR model.
"""

from __future__ import annotations

__all__ = ["detect_lines", "LineCrop", "CropFlag"]

import io
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import NamedTuple

import cv2
import numpy as np
from PIL import Image
from sklearn.cluster import AgglomerativeClustering


# Public types

class CropFlag(Flag):
    """Warning flags on a crop. CLEAN means nothing looked suspicious."""
    CLEAN = 0
    TALL = auto()        # taller than twice the median — might be two merged lines
    NARROW = auto()      # very short horizontally — might be a lone symbol or fragment
    LOW_INK = auto()     # barely any ink — might be noise or a blank region
    SINGLE_COMP = auto() # only one tiny blob — worth double-checking


class LineCrop(NamedTuple):
    """One detected line, ready for the OCR model.

    `image` is the cropped PIL image to pass to the model.
    `y0/y1/x0/x1` are the pixel coordinates in the original page image.
    `flags` tells you if anything looked off about this crop.
    """
    image: Image.Image
    y0: int
    y1: int
    x0: int
    x1: int
    flags: CropFlag


@dataclass
class _Component:
    idx: int
    x0: int
    y0: int
    x1: int
    y1: int
    mid_x: float
    mid_y: float
    area: int

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def width(self) -> int:
        return self.x1 - self.x0


@dataclass
class _Line:
    y0: int
    y1: int
    x0: int
    x1: int
    comp_indices: list[int] = field(default_factory=list)

    @property
    def mid_y(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


# Internal helpers

def _extract_components(
    binary: np.ndarray,
    min_area_ratio: float,
    max_area_ratio: float,
) -> list[_Component]:
    """Find all ink blobs and drop anything implausibly small or large.

    The area threshold scales with the image so it works across different
    resolutions without manual tuning.
    """
    n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        binary, connectivity=8,
    )
    img_area = binary.shape[0] * binary.shape[1]
    min_area = max(int(img_area * min_area_ratio), 4)

    components = []
    for i in range(1, n_labels):  # skip background (label 0)
        area = stats[i, cv2.CC_STAT_AREA]
        if area < min_area or area > img_area * max_area_ratio:
            continue
        x = stats[i, cv2.CC_STAT_LEFT]
        y = stats[i, cv2.CC_STAT_TOP]
        w = stats[i, cv2.CC_STAT_WIDTH]
        h = stats[i, cv2.CC_STAT_HEIGHT]
        components.append(_Component(
            idx=i,
            x0=x, y0=y, x1=x + w, y1=y + h,
            mid_x=centroids[i][0], mid_y=centroids[i][1],
            area=area,
        ))
    return components


def _build_distance_matrix(
    components: list[_Component],
    median_height: float,
    horizontal_penalty_weight: float,
) -> np.ndarray:
    """Pairwise distances for clustering.

    Base distance is vertical midpoint separation. When two blobs don't
    overlap horizontally at all, we add a penalty proportional to the gap —
    this keeps a superscript on line 1 from accidentally merging with a
    subscript on line 2 just because their midpoints happen to be close.
    """
    n = len(components)
    dist = np.zeros((n, n), dtype=float)
    for i in range(n):
        ci = components[i]
        for j in range(i + 1, n):
            cj = components[j]
            vert = abs(ci.mid_y - cj.mid_y)

            # horizontal overlap: 0 when fully disjoint, positive when overlapping
            overlap = min(ci.x1, cj.x1) - max(ci.x0, cj.x0)
            if overlap < 0:
                # gap as fraction of median height — scaled penalty
                gap = max(ci.x0, cj.x0) - min(ci.x1, cj.x1)
                penalty = horizontal_penalty_weight * median_height * (
                    gap / max(ci.width, cj.width, 1)
                )
            else:
                penalty = 0.0

            d = vert + penalty
            dist[i, j] = d
            dist[j, i] = d
    return dist


def _cluster_into_lines(
    components: list[_Component],
    distance_factor: float,
    horizontal_penalty_weight: float,
) -> list[_Line]:
    """Group blobs into lines.

    The clustering cutoff is set relative to the median blob height, so it
    automatically adapts to the handwriting scale rather than needing a
    hardcoded pixel value.
    """
    if not components:
        return []
    if len(components) == 1:
        c = components[0]
        return [_Line(c.y0, c.y1, c.x0, c.x1, [0])]

    heights = np.array([c.height for c in components], dtype=float)
    median_h = float(np.median(heights))
    threshold = max(median_h * distance_factor, 1.0)

    dist_matrix = _build_distance_matrix(
        components, median_h, horizontal_penalty_weight,
    )

    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=threshold,
        metric="precomputed",
        linkage="average",
    )
    labels = clustering.fit_predict(dist_matrix)

    lines: dict[int, _Line] = {}
    for i, label in enumerate(labels):
        c = components[i]
        if label not in lines:
            lines[label] = _Line(c.y0, c.y1, c.x0, c.x1, [i])
        else:
            seg = lines[label]
            seg.y0 = min(seg.y0, c.y0)
            seg.y1 = max(seg.y1, c.y1)
            seg.x0 = min(seg.x0, c.x0)
            seg.x1 = max(seg.x1, c.x1)
            seg.comp_indices.append(i)

    return list(lines.values())


def _detect_single_line(components: list[_Component], median_h: float) -> bool:
    """Quick check: if everything fits within about two blob-heights, it's one line.

    Skips the full clustering pipeline for single-line inputs, which is the
    common case when the image is already a tightly cropped equation.
    """
    if not components:
        return True
    global_y0 = min(c.y0 for c in components)
    global_y1 = max(c.y1 for c in components)
    return (global_y1 - global_y0) < 2.0 * median_h


def _duplicate_tall_components(
    lines: list[_Line],
    components: list[_Component],
    tall_threshold: float,
) -> list[_Line]:
    """Handle tall symbols like integrals, summations, and big brackets.

    These span multiple lines visually, so clustering always puts them in
    just one. We copy them into every line they overlap so each crop includes
    the full symbol that belongs to it.
    """
    if len(lines) <= 1:
        return lines

    cluster_heights = [ln.height for ln in lines]
    median_cluster_h = float(np.median(cluster_heights))
    cutoff = median_cluster_h * tall_threshold

    tall_comp_indices: set[int] = set()
    for ln in lines:
        for ci in ln.comp_indices:
            if components[ci].height > cutoff:
                tall_comp_indices.add(ci)

    if not tall_comp_indices:
        return lines

    duplicated: set[int] = set()

    for ln in lines:
        for ci in tall_comp_indices:
            if ci in ln.comp_indices:
                continue
            comp = components[ci]
            inter_top = max(comp.y0, ln.y0)
            inter_bot = min(comp.y1, ln.y1)
            if inter_bot > inter_top:
                ln.comp_indices.append(ci)
                duplicated.add(ci)

    return [
        ln for ln in lines
        if not all(
            ci in tall_comp_indices and ci in duplicated
            for ci in ln.comp_indices
        )
    ]


def _reassign_orphans(
    lines: list[_Line],
    components: list[_Component],
    orphan_height_ratio: float,
) -> list[_Line]:
    """Absorb stray clusters (dots, punctuation, small accents) into real lines.

    When clustering produces a line made entirely of tiny blobs, we fold it
    into whichever real line is closest — using both vertical distance and
    horizontal proximity, so a dot above a letter goes to the right line
    rather than the one with the nearest midpoint.
    """
    if len(lines) <= 1:
        return lines

    median_h = float(np.median([c.height for c in components]))
    threshold = median_h * orphan_height_ratio

    real_lines: list[_Line] = []
    orphan_lines: list[_Line] = []
    for ln in lines:
        tallest = max(components[i].height for i in ln.comp_indices)
        (orphan_lines if tallest < threshold else real_lines).append(ln)

    if not real_lines:
        return lines

    for orphan in orphan_lines:
        o_mid_y = orphan.mid_y
        o_mid_x = (orphan.x0 + orphan.x1) / 2.0

        def _affinity(rl: _Line) -> float:
            vert = abs(rl.mid_y - o_mid_y)
            h_dists = []
            for ci in rl.comp_indices:
                c = components[ci]
                c_mid_x = (c.x0 + c.x1) / 2.0
                h_dists.append(abs(c_mid_x - o_mid_x))
            h_dist = min(h_dists) if h_dists else 0.0
            return vert * 10.0 + h_dist

        best = min(real_lines, key=_affinity)
        best.comp_indices.extend(orphan.comp_indices)

    return real_lines


def _recompute_bounds(lines: list[_Line], components: list[_Component]) -> None:
    """Recalculate each line's bounding box after blobs have been moved around."""
    for ln in lines:
        if not ln.comp_indices:
            continue
        ln.y0 = min(components[i].y0 for i in ln.comp_indices)
        ln.y1 = max(components[i].y1 for i in ln.comp_indices)
        ln.x0 = min(components[i].x0 for i in ln.comp_indices)
        ln.x1 = max(components[i].x1 for i in ln.comp_indices)


def _trim_whitespace(crop: np.ndarray, padding: int) -> tuple[np.ndarray, int, int]:
    """Cut blank columns off the left and right, keeping `padding` px of breathing room."""
    ink_cols = np.where(np.any(crop == 0, axis=0))[0]
    if len(ink_cols) == 0:
        return crop, 0, crop.shape[1]
    left = max(0, ink_cols[0] - padding)
    right = min(crop.shape[1], ink_cols[-1] + 1 + padding)
    return crop[:, left:right], left, right


def _flag_crop(
    line: _Line,
    components: list[_Component],
    median_line_h: float,
    median_line_w: float,
    ink_arr: np.ndarray,
) -> CropFlag:
    """Check a crop for anything that looks suspicious and return the appropriate flags."""
    flags = CropFlag.CLEAN
    if line.height > median_line_h * 2.0:
        flags |= CropFlag.TALL
    width = line.x1 - line.x0
    if width < median_line_w * 0.15:
        flags |= CropFlag.NARROW
    ink_ratio = np.sum(ink_arr == 0) / max(ink_arr.size, 1)
    if ink_ratio < 0.005:
        flags |= CropFlag.LOW_INK
    if len(line.comp_indices) == 1:
        comp = components[line.comp_indices[0]]
        if comp.area < median_line_h * median_line_w * 0.01:
            flags |= CropFlag.SINGLE_COMP
    return flags


def _save_debug_image(
    binary: np.ndarray,
    components: list[_Component],
    lines: list[_Line],
    path: str,
) -> None:
    """Write a colour-coded debug image to disk.

    Each detected line gets a distinct colour. Individual blob bounding boxes
    are drawn thinly; the full line crop boundary is drawn thick. Useful for
    visually verifying that segmentation is working correctly.
    """
    vis = cv2.cvtColor(
        np.where(binary, 255, 0).astype(np.uint8), cv2.COLOR_GRAY2BGR,
    )
    rng = np.random.RandomState(42)
    colours = [
        tuple(int(x) for x in rng.randint(60, 255, size=3))
        for _ in range(len(lines))
    ]

    for li, ln in enumerate(lines):
        col = colours[li % len(colours)]
        for ci in ln.comp_indices:
            c = components[ci]
            cv2.rectangle(vis, (c.x0, c.y0), (c.x1, c.y1), col, 1)
        cv2.rectangle(vis, (ln.x0, ln.y0), (ln.x1, ln.y1), col, 2)
        cv2.putText(
            vis, f"L{li}", (ln.x0, max(ln.y0 - 4, 12)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1,
        )

    cv2.imwrite(path, vis)


# Public API

def detect_lines(
    preprocessed_bytes: bytes,
    *,
    morph_close_width: int = 0,
    min_area_ratio: float = 0.0005,
    max_area_ratio: float = 0.4,
    distance_factor: float = 0.7,
    horizontal_penalty_weight: float = 0.3,
    tall_threshold: float = 1.8,
    orphan_height_ratio: float = 0.3,
    min_components: int = 1,
    min_line_height: int = 8,
    padding: int = 6,
    debug: bool = False,
    debug_path: str = "/tmp/line_detect_debug.png",
) -> list[LineCrop]:
    """Segment a preprocessed page image into one crop per math line.

    Pass in the bytes from `preprocess_image`. Get back a list of `LineCrop`
    objects sorted top-to-bottom — each one has a PIL image ready for the OCR
    model, the coordinates on the original page, and any quality flags.

    Parameters
    ----------
    preprocessed_bytes:
        PNG/JPEG bytes from a binarized grayscale image (black ink, white background).
    morph_close_width:
        If > 0, applies a horizontal morphological close before blob extraction
        to bridge gaps in broken strokes. Usually not needed if preprocessing is clean.
    min_area_ratio:
        Smallest blob to keep, as a fraction of total image area. Filters out dust.
    max_area_ratio:
        Largest blob to keep. Filters out page borders or accidental full-page blobs.
    distance_factor:
        Controls how aggressively lines are split. Higher values merge more blobs
        together; lower values split more. Threshold = median_blob_height × this.
    horizontal_penalty_weight:
        How much to penalise horizontally distant blobs when computing clustering
        distance. Increase if blobs from different columns are getting merged.
    tall_threshold:
        Blobs taller than median_line_height × this are treated as spanning
        multiple lines (e.g. integrals) and duplicated into each overlapping line.
    orphan_height_ratio:
        Lines whose tallest blob is below median_height × this are considered
        orphans and merged into the nearest real line.
    min_components:
        Minimum number of blobs a line must have to survive. Mostly useful for
        filtering empty lines that slip through.
    min_line_height:
        Minimum pixel height for a line crop. Shorter crops are discarded.
    padding:
        Pixels of whitespace to add around each crop on all sides.
    debug:
        If True, saves a colour-coded visualisation of the segmentation to disk.
    debug_path:
        Where to write the debug image.
    """
    img = Image.open(io.BytesIO(preprocessed_bytes))
    arr = np.array(img)

    if arr.ndim == 3:
        arr = arr[:, :, 0]

    if morph_close_width > 0:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (morph_close_width, 1),
        )
        inv = cv2.bitwise_not(arr)
        inv = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel)
        work = cv2.bitwise_not(inv)
    else:
        work = arr

    binary = cv2.bitwise_not(work)

    h, w = binary.shape

    components = _extract_components(binary, min_area_ratio, max_area_ratio)
    if not components:
        return []

    median_h = float(np.median([c.height for c in components]))

    if _detect_single_line(components, median_h):
        global_y0 = min(c.y0 for c in components)
        global_y1 = max(c.y1 for c in components)
        if global_y1 - global_y0 < min_line_height:
            return []
        global_x0 = min(c.x0 for c in components)
        global_x1 = max(c.x1 for c in components)
        y0p = max(0, global_y0 - padding)
        y1p = min(h, global_y1 + padding)
        x0p = max(0, global_x0 - padding)
        x1p = min(w, global_x1 + padding)
        crop = arr[y0p:y1p, x0p:x1p]
        crop, _, _ = _trim_whitespace(crop, padding)
        return [LineCrop(
            image=Image.fromarray(crop),
            y0=y0p, y1=y1p, x0=x0p, x1=x1p,
            flags=CropFlag.CLEAN,
        )]

    lines = _cluster_into_lines(
        components, distance_factor, horizontal_penalty_weight,
    )

    lines = _duplicate_tall_components(lines, components, tall_threshold)

    lines = _reassign_orphans(lines, components, orphan_height_ratio)

    _recompute_bounds(lines, components)

    median_area = float(np.median([c.area for c in components]))
    filtered = []
    for ln in lines:
        if ln.height < min_line_height:
            continue
        if len(ln.comp_indices) < min_components:
            if len(ln.comp_indices) == 1:
                comp = components[ln.comp_indices[0]]
                if comp.area < median_area * 0.5:
                    continue
            else:
                continue
        filtered.append(ln)
    lines = filtered

    lines.sort(key=lambda ln: ln.mid_y)

    if debug:
        _save_debug_image(binary, components, lines, debug_path)

    median_line_h = float(np.median([ln.height for ln in lines])) if lines else 1.0
    median_line_w = float(np.median([ln.x1 - ln.x0 for ln in lines])) if lines else 1.0

    results: list[LineCrop] = []
    for ln in lines:
        y0 = max(0, ln.y0 - padding)
        y1 = min(h, ln.y1 + padding)
        x0 = max(0, ln.x0 - padding)
        x1 = min(w, ln.x1 + padding)

        crop = arr[y0:y1, x0:x1]
        crop, trim_left, trim_right = _trim_whitespace(crop, padding)

        final_x0 = x0 + trim_left
        final_x1 = x0 + trim_right

        flags = _flag_crop(ln, components, median_line_h, median_line_w, crop)

        results.append(LineCrop(
            image=Image.fromarray(crop),
            y0=y0, y1=y1, x0=final_x0, x1=final_x1,
            flags=flags,
        ))

    return results
