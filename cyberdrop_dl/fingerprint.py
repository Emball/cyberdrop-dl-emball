"""Video perceptual fingerprinting.

Extracts frames at fixed time percentages from a video file, computes
a perceptual hash (pHash) on each frame, and stores/compares them against
the DB to detect near-duplicate videos regardless of re-encoding, remuxing,
or container differences (e.g. HLS vs direct MP4).

How it works:
  - Probe the file duration with ffprobe
  - Seek to 5 fixed time positions (10%, 30%, 50%, 70%, 90%)
  - Extract one frame at each position via ffmpeg → raw PNG → PIL → pHash
  - Store the 64-bit hashes in the video_fingerprint table
  - On new files: compute frames, query DB, count matches within Hamming
    distance threshold. 3+ matches = duplicate.

Works on partial files too: ffmpeg will decode whatever is there and bail
gracefully when it hits the incomplete end. Early frames are more reliable
than late ones on partials, so we check in ascending order and short-circuit
as soon as we have enough confident frames.

Supported file types: any video format ffmpeg can read (.mp4, .mkv, .webm,
.mov, .avi, .ts, .m4v, etc.)
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from cyberdrop_dl import ffmpeg

if TYPE_CHECKING:
    from cyberdrop_dl.manager import Manager

logger = logging.getLogger(__name__)

# Time positions to sample as fraction of total duration
FRAME_PERCENTAGES: tuple[float, ...] = (0.10, 0.30, 0.50, 0.70, 0.90)

# Hamming distance threshold: bits that can differ and still count as "same frame"
# 64-bit pHash: 0 = identical, 64 = completely different. 10 survives most re-encodes.
HAMMING_THRESHOLD: int = 10

# Minimum number of frame positions that must match to call it a duplicate
MIN_MATCHING_FRAMES: int = 3

# Video extensions we fingerprint (images are handled separately)
_VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v",
    ".flv", ".wmv", ".m2ts", ".mts", ".vob", ".ogv", ".3gp",
})


def _is_video(path: Path) -> bool:
    return path.suffix.lower() in _VIDEO_EXTENSIONS


def _phash_hex(png_bytes: bytes) -> str | None:
    """Compute pHash of a PNG image (as bytes) and return as zero-padded hex."""
    try:
        import imagehash
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("L")
        h = imagehash.phash(img)
        # imagehash stores as a numpy bool array; convert to int then hex
        bits = "".join("1" if b else "0" for b in h.hash.flatten())
        val = int(bits, 2)
        return f"{val:016x}"
    except Exception:
        logger.debug("pHash computation failed", exc_info=True)
        return None


async def _extract_frame_png(file: Path, seek_seconds: float) -> bytes | None:
    """Use ffmpeg to extract a single frame at seek_seconds → raw PNG bytes."""
    assert ffmpeg.which_ffmpeg(), "ffmpeg not found"
    cmd = (
        ffmpeg.which_ffmpeg(),
        "-y",
        "-loglevel", "error",
        "-ss", f"{seek_seconds:.3f}",
        "-i", str(file),
        "-frames:v", "1",
        "-f", "image2",
        "-vcodec", "png",
        "pipe:1",
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        if process.returncode == 0 and stdout:
            return stdout
    except asyncio.TimeoutError:
        logger.debug("Frame extraction timed out at %.1fs in '%s'", seek_seconds, file)
    except Exception:
        logger.debug("Frame extraction failed at %.1fs in '%s'", seek_seconds, file, exc_info=True)
    return None


async def compute_fingerprint(file: Path) -> list[tuple[float, str]]:
    """Compute perceptual hash frames for a video file.

    Returns list of (frame_pct, phash_hex) for successfully extracted frames.
    Partial files are supported — missing frames near the end are skipped silently.
    """
    if not _is_video(file):
        return []

    probe = await ffmpeg.probe(file)
    if not probe or not probe.format or not probe.format.duration:
        logger.debug("No duration found for '%s', skipping fingerprint", file)
        return []

    duration = float(probe.format.duration)
    if duration < 5.0:
        logger.debug("File too short (%.1fs) for fingerprinting: '%s'", duration, file)
        return []

    frames: list[tuple[float, str]] = []

    for pct in FRAME_PERCENTAGES:
        seek_sec = duration * pct
        png = await _extract_frame_png(file, seek_sec)
        if png is None:
            continue
        ph = _phash_hex(png)
        if ph:
            frames.append((pct, ph))

    logger.debug("Fingerprinted '%s': %d/%d frames", file.name, len(frames), len(FRAME_PERCENTAGES))
    return frames


async def fingerprint_and_store(manager: Manager, file: Path, folder: str | None = None) -> list[tuple[float, str]]:
    """Compute and persist fingerprint frames for a file.

    Skips if:
      - Not a video extension
      - ffmpeg not installed
      - Fingerprint already stored for this file

    Returns computed frames (empty list on skip or failure).
    """
    if not ffmpeg.is_installed():
        return []

    if not _is_video(file):
        return []

    resolved = file.expanduser().resolve()
    db_folder = folder or str(resolved.parent)
    db_filename = resolved.name

    # Don't re-fingerprint files we've already processed
    already = await manager.database.hash.file_has_fingerprint(db_folder, db_filename)
    if already:
        return []

    frames = await compute_fingerprint(resolved)
    if not frames:
        return []

    await manager.database.hash.insert_fingerprint_frames(db_folder, db_filename, frames)
    logger.info("Stored fingerprint for '%s': %d frames", file.name, len(frames))
    return frames


async def check_fingerprint_duplicate(
    manager: Manager,
    file: Path,
    folder: str | None = None,
) -> str | None:
    """Check if a file (complete or partial) is a near-duplicate of anything in the DB.

    Returns 'folder/filename' of the matched file if found, None otherwise.
    """
    if not ffmpeg.is_installed():
        return None

    if not _is_video(file):
        return None

    resolved = file.expanduser().resolve()
    db_folder = folder or str(resolved.parent)
    db_filename = resolved.name

    frames = await compute_fingerprint(resolved)
    if len(frames) < MIN_MATCHING_FRAMES:
        # Not enough frames extracted (file too short or too partial) — inconclusive
        logger.debug("Too few frames (%d) for fingerprint check on '%s'", len(frames), file.name)
        return None

    match = await manager.database.hash.find_fingerprint_matches(
        frames,
        hamming_threshold=HAMMING_THRESHOLD,
        min_matching_frames=MIN_MATCHING_FRAMES,
    )

    if match:
        logger.info(
            "Fingerprint duplicate detected: '%s' matches '%s'",
            file.name, match,
        )

    return match
