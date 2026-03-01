"""Repair inconsistent InlineFile metadata in local .wabbajack archives."""

from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


def is_enabled() -> bool:
    """
    Enabled by default for local file installs.

    Set JACKIFY_REPAIR_INLINE_METADATA=0 to disable.
    """
    value = os.environ.get("JACKIFY_REPAIR_INLINE_METADATA", "")
    if not value.strip():
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def repair_local_wabbajack_if_needed(
    wabbajack_path: Path,
    logger: Any,
    emit_fn: Optional[Callable[[str], None]] = None,
) -> Path:
    """
    Validate and repair InlineFile {Hash, Size} metadata if mismatched.

    Returns:
        Path to the original archive (no changes) or a repaired archive copy.
    """
    path = Path(wabbajack_path)
    if not is_enabled():
        _log(logger, emit_fn, "disabled=1")
        return path
    if not path.is_file() or path.suffix.lower() != ".wabbajack":
        return path

    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "modlist" not in zf.namelist():
                _log(logger, emit_fn, f"skip no_modlist path={path}")
                return path
            modlist_raw = zf.read("modlist")
            modlist = json.loads(modlist_raw.decode("utf-8"))

            directives = modlist.get("Directives")
            if not isinstance(directives, list):
                _log(logger, emit_fn, f"skip invalid_directives path={path}")
                return path

            changed = 0
            missing = 0
            profile_changed = 0
            for d in directives:
                if not isinstance(d, dict) or d.get("$type") != "InlineFile":
                    continue
                sid = d.get("SourceDataID")
                if not sid or sid not in zf.namelist():
                    missing += 1
                    continue
                payload = zf.read(sid)
                actual_size = len(payload)
                actual_hash = _xxh64_b64_le(payload)
                old_size = int(d.get("Size", -1))
                old_hash = str(d.get("Hash", ""))
                if old_size != actual_size or old_hash != actual_hash:
                    d["Size"] = actual_size
                    d["Hash"] = actual_hash
                    changed += 1
                    to = str(d.get("To", ""))
                    if to.lower().startswith("profiles\\"):
                        profile_changed += 1

            if changed == 0:
                _log(
                    logger,
                    emit_fn,
                    f"validated path={path} inline_changed=0 missing_source_data={missing}",
                )
                return path

            new_modlist_raw = json.dumps(modlist, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            repaired_path = path.with_name(f"{path.stem}.jackify-repaired{path.suffix}")
            _rewrite_zip_with_modlist(path, repaired_path, new_modlist_raw)
            _log(
                logger,
                emit_fn,
                f"repaired src={path} dst={repaired_path} inline_changed={changed} "
                f"profile_inline_changed={profile_changed} missing_source_data={missing}",
            )
            return repaired_path
    except Exception as e:
        _log(logger, emit_fn, f"repair_failed path={path} err={e}")
        return path


def _rewrite_zip_with_modlist(src: Path, dst: Path, new_modlist_raw: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="jackify-repair-",
        suffix=".wabbajack",
        dir=str(dst.parent),
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(tmp_path, "w") as zout:
            for info in zin.infolist():
                data = new_modlist_raw if info.filename == "modlist" else zin.read(info.filename)
                new_info = _clone_zipinfo(info)
                zout.writestr(new_info, data)
        tmp_path.replace(dst)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _clone_zipinfo(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    cloned = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    cloned.compress_type = info.compress_type
    cloned.comment = info.comment
    cloned.extra = info.extra
    cloned.internal_attr = info.internal_attr
    cloned.external_attr = info.external_attr
    cloned.create_system = info.create_system
    cloned.flag_bits = info.flag_bits
    return cloned


def _xxh64_b64_le(payload: bytes) -> str:
    h = _xxh64(payload, 0)
    return base64.b64encode(struct.pack("<Q", h)).decode("ascii")


def _rotl64(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & 0xFFFFFFFFFFFFFFFF


def _round(acc: int, lane: int) -> int:
    acc = (acc + (lane * 14029467366897019727)) & 0xFFFFFFFFFFFFFFFF
    acc = _rotl64(acc, 31)
    acc = (acc * 11400714785074694791) & 0xFFFFFFFFFFFFFFFF
    return acc


def _merge_round(acc: int, val: int) -> int:
    acc ^= _round(0, val)
    acc = (acc * 11400714785074694791 + 9650029242287828579) & 0xFFFFFFFFFFFFFFFF
    return acc


def _xxh64(data: bytes, seed: int = 0) -> int:
    """Pure Python xxHash64."""
    n = len(data)
    i = 0
    if n >= 32:
        v1 = (seed + 11400714785074694791 + 14029467366897019727) & 0xFFFFFFFFFFFFFFFF
        v2 = (seed + 14029467366897019727) & 0xFFFFFFFFFFFFFFFF
        v3 = seed & 0xFFFFFFFFFFFFFFFF
        v4 = (seed - 11400714785074694791) & 0xFFFFFFFFFFFFFFFF

        limit = n - 32
        while i <= limit:
            lane1 = struct.unpack_from("<Q", data, i)[0]
            lane2 = struct.unpack_from("<Q", data, i + 8)[0]
            lane3 = struct.unpack_from("<Q", data, i + 16)[0]
            lane4 = struct.unpack_from("<Q", data, i + 24)[0]
            v1 = _round(v1, lane1)
            v2 = _round(v2, lane2)
            v3 = _round(v3, lane3)
            v4 = _round(v4, lane4)
            i += 32

        h64 = (_rotl64(v1, 1) + _rotl64(v2, 7) + _rotl64(v3, 12) + _rotl64(v4, 18)) & 0xFFFFFFFFFFFFFFFF
        h64 = _merge_round(h64, v1)
        h64 = _merge_round(h64, v2)
        h64 = _merge_round(h64, v3)
        h64 = _merge_round(h64, v4)
    else:
        h64 = (seed + 2870177450012600261) & 0xFFFFFFFFFFFFFFFF

    h64 = (h64 + n) & 0xFFFFFFFFFFFFFFFF

    while i + 8 <= n:
        lane = struct.unpack_from("<Q", data, i)[0]
        k1 = _round(0, lane)
        h64 ^= k1
        h64 = (_rotl64(h64, 27) * 11400714785074694791 + 9650029242287828579) & 0xFFFFFFFFFFFFFFFF
        i += 8

    if i + 4 <= n:
        lane = struct.unpack_from("<I", data, i)[0]
        h64 ^= (lane * 11400714785074694791) & 0xFFFFFFFFFFFFFFFF
        h64 = (_rotl64(h64, 23) * 14029467366897019727 + 1609587929392839161) & 0xFFFFFFFFFFFFFFFF
        i += 4

    while i < n:
        lane = data[i]
        h64 ^= (lane * 2870177450012600261) & 0xFFFFFFFFFFFFFFFF
        h64 = (_rotl64(h64, 11) * 11400714785074694791) & 0xFFFFFFFFFFFFFFFF
        i += 1

    h64 ^= (h64 >> 33)
    h64 = (h64 * 14029467366897019727) & 0xFFFFFFFFFFFFFFFF
    h64 ^= (h64 >> 29)
    h64 = (h64 * 1609587929392839161) & 0xFFFFFFFFFFFFFFFF
    h64 ^= (h64 >> 32)
    return h64 & 0xFFFFFFFFFFFFFFFF


def _log(logger: Any, emit_fn: Optional[Callable[[str], None]], msg: str) -> None:
    line = f"[INLINE_REPAIR] {msg}"
    if emit_fn:
        try:
            emit_fn(line)
        except Exception:
            pass
    try:
        logger.info(line)
    except Exception:
        pass
