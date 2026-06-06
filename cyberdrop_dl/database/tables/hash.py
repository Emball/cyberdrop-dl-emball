from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .definitions import add_partial_hash_column, create_files, create_hash, create_hash_index, create_partial_hash_index, create_video_fingerprint, create_video_fingerprint_index

if TYPE_CHECKING:
    import aiosqlite
    from yarl import URL

    from cyberdrop_dl.database import Database


logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True, frozen=True)
class HashTable:
    _database: Database
    cwd: Path = dataclasses.field(init=False, default_factory=lambda: Path.cwd().expanduser().resolve())

    @property
    def db_conn(self) -> aiosqlite.Connection:
        return self._database._db_conn

    async def create(self) -> None:
        for query in (
            create_files,
            create_hash,
            create_hash_index,
            create_video_fingerprint,
            create_video_fingerprint_index,
        ):
            _ = await self.db_conn.execute(query)

        await self.db_conn.commit()
        await self._migrate_partial_hash_column()

    async def _migrate_partial_hash_column(self) -> None:
        """Add partial_hash column to existing hash tables that predate this feature."""
        cursor = await self.db_conn.execute("PRAGMA table_info(hash)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "partial_hash" not in columns:
            logger.info("Migrating hash table: adding partial_hash column")
            await self.db_conn.execute(add_partial_hash_column)
            await self.db_conn.execute(create_partial_hash_index)
            await self.db_conn.commit()

    async def check_partial_hash_exists(self, partial_hash: str, hash_type: str, file_size: int | None = None) -> bool:
        """Check if a partial hash (first 16MB) matches any known file.

        Optionally also matches on file_size for a stronger signal before bytes are even downloaded.
        """
        if self._database.ignore_history:
            return False

        if file_size:
            query = """
            SELECT 1 FROM hash
            JOIN files ON hash.folder = files.folder AND hash.download_filename = files.download_filename
            WHERE hash.hash_type = ? AND hash.partial_hash = ? AND files.file_size = ?
            LIMIT 1
            """
            params = (hash_type, partial_hash, file_size)
        else:
            query = "SELECT 1 FROM hash WHERE hash_type = ? AND partial_hash = ? LIMIT 1"
            params = (hash_type, partial_hash)

        try:
            cursor = await self.db_conn.execute(query, params)
            return await cursor.fetchone() is not None
        except Exception:
            logger.exception("Error checking partial hash")
            return False

    async def check_size_has_known_hash(self, file_size: int, hash_type: str = "xxh128") -> bool:
        """Quick pre-download check: do we have any fully-hashed file of this exact size?

        This is the cheapest possible signal — no bytes downloaded yet.
        """
        if self._database.ignore_history:
            return False

        query = """
        SELECT 1 FROM hash
        JOIN files ON hash.folder = files.folder AND hash.download_filename = files.download_filename
        WHERE files.file_size = ? AND hash.hash_type = ? AND hash.hash IS NOT NULL
        LIMIT 1
        """
        try:
            cursor = await self.db_conn.execute(query, (file_size, hash_type))
            return await cursor.fetchone() is not None
        except Exception:
            logger.exception("Error checking size against known hashes")
            return False

    async def update_partial_hash(self, partial_hash: str, hash_type: str, file: Path | str) -> None:
        """Store the partial hash (first 16MB) for a file after it's been computed."""
        query = """
        UPDATE hash SET partial_hash = ?
        WHERE hash_type = ? AND folder = ? AND download_filename = ?
        """
        try:
            full_path = self.cwd / file
            folder = str(full_path.parent)
            filename = full_path.name
            await self.db_conn.execute(query, (partial_hash, hash_type, folder, filename))
            await self.db_conn.commit()
        except Exception:
            logger.exception("Error updating partial hash for '%s'", file)

    async def get_files_within_size_range(self, file_size: int, tolerance_bytes: int = 2 * 1024 * 1024) -> list[aiosqlite.Row]:
        """Returns all completed files whose size is within tolerance_bytes of file_size."""
        query = """
        SELECT files.folder, files.download_filename, files.original_filename, files.file_size
        FROM files
        JOIN media ON files.folder = media.download_path AND files.download_filename = media.download_filename
        WHERE media.completed = 1
          AND files.file_size BETWEEN ? AND ?
        """
        low = file_size - tolerance_bytes
        high = file_size + tolerance_bytes
        try:
            cursor = await self.db_conn.execute(query, (low, high))
            return cast("list[aiosqlite.Row]", await cursor.fetchall())
        except Exception:
            logger.exception("Error querying files by size range")
            return []

    async def check_fuzzy_duplicate(
        self,
        filename: str,
        file_size: int,
        size_tolerance_bytes: int = 2 * 1024 * 1024,
        name_similarity_threshold: float = 0.80,
    ) -> str | None:
        """Check if a file is a fuzzy duplicate of something already downloaded.

        Matches on file size within tolerance AND filename similarity above threshold.
        Returns the matching filename if a duplicate is found, None otherwise.
        """
        if self._database.ignore_history:
            return None

        candidates = await self.get_files_within_size_range(file_size, size_tolerance_bytes)
        if not candidates:
            return None

        from difflib import SequenceMatcher
        from pathlib import Path

        incoming_stem = _normalize_stem(Path(filename).stem)

        for row in candidates:
            candidate_name = row["original_filename"] or row["download_filename"]
            candidate_stem = _normalize_stem(Path(candidate_name).stem)
            ratio = SequenceMatcher(None, incoming_stem, candidate_stem).ratio()
            if ratio >= name_similarity_threshold:
                logger.debug(
                    f"Fuzzy duplicate: '{filename}' ~ '{candidate_name}' "
                    f"(name similarity: {ratio:.2%}, size delta: {abs(file_size - row['file_size'])} bytes)"
                )
                return candidate_name

        return None
        """gets the hash from a complete file path

        Args:
            full_path: Full path to the file to check.

        Returns:
            hash if  exists
        """
        query = "SELECT hash FROM hash WHERE folder=? AND download_filename=? AND hash_type=? AND hash IS NOT NULL"
        try:
            path = self.cwd / path
            folder = str(path.parent)
            filename = path.name

            # Check if the file exists with matching folder, filename, and size
            cursor = await self.db_conn.execute(query, (folder, filename, hash_type))
            if row := await cursor.fetchone():
                return row[0]

        except Exception:
            logger.exception("Error checking file")

    async def get_files_with_hash_matches(
        self, hash_value: str, size: int, hash_type: str | None = None
    ) -> list[aiosqlite.Row]:
        """Retrieves a list of (folder, filename) tuples based on a given hash.

        Args:
            hash_value: The hash value to search for.
            size: file size

        Returns:
            A list of (folder, filename) tuples, or an empty list if no matches found.
        """
        if hash_type:
            query = """
            SELECT files.folder, files.download_filename, files.date
            FROM hash JOIN files ON hash.folder = files.folder AND hash.download_filename = files.download_filename
            WHERE hash.hash = ? AND files.file_size = ? AND hash.hash_type = ?
            ORDER BY files.date ASC;
            """
            params = (hash_value, size, hash_type)
        else:
            query = """
            SELECT files.folder, files.download_filename, files.date
            FROM hash JOIN files ON hash.folder = files.folder AND hash.download_filename = files.download_filename
            WHERE hash.hash = ? AND files.file_size = ?
            ORDER BY files.date ASC;
            """
            params = (hash_value, size)

        try:
            cursor = await self.db_conn.execute(query, params)
            return cast("list[aiosqlite.Row]", await cursor.fetchall())

        except Exception:
            logger.exception("Error retrieving folder and filename")
            return []

    async def check_hash_exists(self, hash_type: str, hash_value: str) -> bool:
        if self._database.ignore_history:
            return False

        query = "SELECT 1 FROM hash WHERE hash.hash_type = ? AND hash.hash = ? LIMIT 1"
        cursor = await self.db_conn.execute(query, (hash_type, hash_value))
        result = await cursor.fetchone()
        return result is not None

    async def insert_or_update_hash_db(
        self, hash_value: str, hash_type: str, file: Path | str, original_filename: str | None, referer: URL | None
    ) -> bool:
        """Inserts or updates a record in the specified SQLite database.

        Args:
            hash_value: The calculated hash of the file.
            file: The file path
            original_filename: The name original name of the file.
            referer: referer URL
            hash_type: The hash type (e.g., md5, sha256)

        Returns:
            True if all the record was inserted or updated successfully, False otherwise.
        """

        hashed = await self.insert_or_update_hashes(hash_value, hash_type, file)
        existed = await self.insert_or_update_file(original_filename, referer, file)
        return existed and hashed

    async def insert_or_update_hashes(self, hash_value: str, hash_type: str, file: Path | str) -> bool:
        query = """
        INSERT INTO hash (hash, hash_type, folder, download_filename)
        VALUES (?, ?, ?, ?) ON CONFLICT(download_filename, folder, hash_type) DO UPDATE SET hash = ?;
        """

        try:
            full_path = self.cwd / file
            download_filename = full_path.name
            folder = str(full_path.parent)
            await self.db_conn.execute(query, (hash_value, hash_type, folder, download_filename, hash_value))
            await self.db_conn.commit()
        except Exception:
            logger.exception("Error inserting/updating record")
            return False
        return True

    async def insert_or_update_file(
        self, original_filename: str | None, referer: URL | str | None, file: Path | str
    ) -> bool:
        query = """
        INSERT INTO files (folder, original_filename, download_filename, file_size, referer, date)
        VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(download_filename, folder)
        DO UPDATE SET original_filename = ?, file_size = ?, referer = ?, date = ?;
        """
        referer_ = str(referer) if referer else None
        try:
            full_path = self.cwd / file
            download_filename = full_path.name
            folder = str(full_path.parent)
            stat = full_path.stat()
            file_size = stat.st_size
            file_date = int(stat.st_mtime)
            await self.db_conn.execute(
                query,
                (
                    folder,
                    original_filename,
                    download_filename,
                    file_size,
                    referer_,
                    file_date,
                    original_filename,
                    file_size,
                    referer_,
                    file_date,
                ),
            )
            await self.db_conn.commit()
        except Exception:
            logger.exception("Error inserting/updating record")
            return False
        return True

    async def get_all_unique_hashes(self, hash_type: str | None = None) -> list[str]:
        """Retrieves a list of hashes

        Args:
            hash_value: The hash value to search for.
            hash_type: The type of hash[optional]

        Returns:
            A list of (folder, filename) tuples, or an empty list if no matches found.
        """
        if hash_type:
            query, params = "SELECT DISTINCT hash FROM hash WHERE hash_type =?", (hash_type,)

        else:
            query, params = "SELECT DISTINCT hash FROM hash", ()
        try:
            cursor = await self.db_conn.execute(query, params)
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
        except Exception:
            logger.exception("Error retrieving folder and filename")
            return []


    async def insert_fingerprint_frames(self, folder: str, filename: str, frames: list[tuple[float, str]]) -> None:
        """Store perceptual hash frames for a video file.

        Args:
            folder: absolute folder path
            filename: download filename
            frames: list of (frame_pct, phash_hex) pairs
        """
        query = """
        INSERT INTO video_fingerprint (folder, download_filename, frame_pct, phash)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(folder, download_filename, frame_pct) DO UPDATE SET phash = excluded.phash;
        """
        try:
            await self.db_conn.executemany(query, [(folder, filename, pct, ph) for pct, ph in frames])
            await self.db_conn.commit()
        except Exception:
            logger.exception("Error inserting fingerprint frames for '%s/%s'", folder, filename)

    async def get_fingerprint_frames(self, folder: str, filename: str) -> list[tuple[float, str]]:
        """Retrieve stored frames for a file. Returns list of (frame_pct, phash)."""
        query = "SELECT frame_pct, phash FROM video_fingerprint WHERE folder = ? AND download_filename = ? ORDER BY frame_pct"
        try:
            cursor = await self.db_conn.execute(query, (folder, filename))
            rows = await cursor.fetchall()
            return [(row["frame_pct"], row["phash"]) for row in rows]
        except Exception:
            logger.exception("Error retrieving fingerprint for '%s/%s'", folder, filename)
            return []

    async def file_has_fingerprint(self, folder: str, filename: str) -> bool:
        """Check if we already have fingerprint frames stored for this file."""
        query = "SELECT 1 FROM video_fingerprint WHERE folder = ? AND download_filename = ? LIMIT 1"
        try:
            cursor = await self.db_conn.execute(query, (folder, filename))
            return await cursor.fetchone() is not None
        except Exception:
            logger.exception("Error checking fingerprint existence")
            return False

    async def find_fingerprint_matches(
        self,
        frames: list[tuple[float, str]],
        hamming_threshold: int = 10,
        min_matching_frames: int = 3,
    ) -> str | None:
        """Check if a set of frames matches any known file fingerprint.

        For each incoming frame, find all stored frames at the same time
        position whose pHash Hamming distance is within the threshold.
        If min_matching_frames or more positions match the same file, it's
        a duplicate.

        Returns 'folder/filename' of the matched file, or None.
        """
        if self._database.ignore_history:
            return None

        if not frames:
            return None

        # Collect per-file match counts
        from collections import Counter
        match_counts: Counter[str] = Counter()

        for frame_pct, phash_hex in frames:
            # Fetch all stored frames at this time position
            query = """
            SELECT folder, download_filename, phash
            FROM video_fingerprint
            WHERE frame_pct = ?
            """
            try:
                cursor = await self.db_conn.execute(query, (frame_pct,))
                rows = await cursor.fetchall()
            except Exception:
                logger.exception("Error querying fingerprint frames at pct=%s", frame_pct)
                continue

            incoming_hash = int(phash_hex, 16)
            for row in rows:
                stored_hash = int(row["phash"], 16)
                distance = bin(incoming_hash ^ stored_hash).count("1")
                if distance <= hamming_threshold:
                    key = f"{row['folder']}/{row['download_filename']}"
                    match_counts[key] += 1

        if not match_counts:
            return None

        best_match, count = match_counts.most_common(1)[0]
        if count >= min_matching_frames:
            logger.debug(
                "Fingerprint match: %d/%d frames matched '%s'",
                count, len(frames), best_match,
            )
            return best_match

        return None


def _normalize_stem(stem: str) -> str:
    """Normalize a filename stem for fuzzy comparison.

    Lowercases, strips punctuation/special chars, collapses whitespace.
    'TEDDY-CHAN HAS A CHRISTMAS GIFT FOR YOU¡ - INDIGO WHITE - 1080p'
    -> 'teddy chan has a christmas gift for you indigo white 1080p'
    """
    import re
    stem = stem.lower()
    # Strip resolution/quality suffixes that differ between HLS and direct
    stem = re.sub(r'\b(hls|hls_\w+|\d+p)\b', '', stem)
    # Replace punctuation and special chars with space
    stem = re.sub(r'[^\w\s]', ' ', stem)
    # Collapse whitespace
    stem = re.sub(r'\s+', ' ', stem).strip()
    return stem
