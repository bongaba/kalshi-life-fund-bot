from pathlib import Path
import shutil
import os

from loguru import logger

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
_CONFIGURED_LOG_FILES: set[Path] = set()


def _get_log_family_dir(filename: str) -> Path:
    return LOGS_DIR / Path(filename).stem


def _iter_related_legacy_logs(filename: str) -> list[Path]:
    log_name = Path(filename)
    stem = log_name.stem
    suffix = log_name.suffix

    candidates: list[Path] = []
    exact_match = BASE_DIR / log_name.name
    if exact_match.exists():
        candidates.append(exact_match)

    for rotated_path in sorted(BASE_DIR.glob(f"{stem}.*{suffix}")):
        if rotated_path not in candidates:
            candidates.append(rotated_path)

    flat_logs_dir_match = LOGS_DIR / log_name.name
    if flat_logs_dir_match.exists() and flat_logs_dir_match not in candidates:
        candidates.append(flat_logs_dir_match)

    for rotated_path in sorted(LOGS_DIR.glob(f"{stem}.*{suffix}")):
        if rotated_path not in candidates:
            candidates.append(rotated_path)

    return candidates


def _move_legacy_root_logs(filename: str) -> tuple[list[Path], list[Path]]:
    LOGS_DIR.mkdir(exist_ok=True)
    family_dir = _get_log_family_dir(filename)
    family_dir.mkdir(exist_ok=True)
    moved_files: list[Path] = []
    skipped_files: list[Path] = []

    for source_path in _iter_related_legacy_logs(filename):
        if not source_path.is_file():
            continue

        if source_path.parent == family_dir:
            continue

        target_path = family_dir / source_path.name
        if target_path.exists():
            suffix_index = 1
            while True:
                candidate = family_dir / f"{source_path.stem}.{suffix_index}{source_path.suffix}"
                if not candidate.exists():
                    target_path = candidate
                    break
                suffix_index += 1

        try:
            shutil.move(str(source_path), str(target_path))
            moved_files.append(target_path)
        except PermissionError:
            skipped_files.append(source_path)

    return moved_files, skipped_files


def setup_log_file(filename: str, rotation: str = "1 day") -> Path:
    archived_files, skipped_files = _move_legacy_root_logs(filename)
    family_dir = _get_log_family_dir(filename)
    base_name = Path(filename)
    process_safe_name = f"{base_name.stem}.{os.getpid()}{base_name.suffix}"
    log_path = family_dir / process_safe_name

    if log_path not in _CONFIGURED_LOG_FILES:
        # enqueue=True makes sink writes asynchronous and safer across process boundaries.
        logger.add(str(log_path), rotation=rotation, enqueue=True)
        _CONFIGURED_LOG_FILES.add(log_path)

    if archived_files:
        logger.info(
            "[LOGGING] Archived {} legacy log files to {}",
            len(archived_files),
            family_dir,
        )

    if skipped_files:
        logger.warning(
            "[LOGGING] Skipped {} locked legacy log files for {}",
            len(skipped_files),
            filename,
        )

    return log_path
