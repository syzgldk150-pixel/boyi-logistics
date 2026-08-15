"""DPAPI machine-bound secret storage for Worker signing and pipe keys."""

from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import wintypes
from pathlib import Path

from agent.automation_plugins.package import Ed25519PackageSigner


_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4
_MAX_PROTECTED_BYTES = 64 * 1024


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(value: bytes) -> tuple[_DataBlob, object]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("DPAPI Worker key storage requires Windows")


def protect_machine_secret(value: bytes, *, entropy: bytes) -> bytes:
    _require_windows()
    if not value or len(value) > _MAX_PROTECTED_BYTES or not entropy:
        raise ValueError("Worker secret or entropy size is invalid")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_blob, input_buffer = _blob(bytes(value))
    entropy_blob, entropy_buffer = _blob(bytes(entropy))
    output_blob = _DataBlob()
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "Boyi Windows Worker",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN | _CRYPTPROTECT_LOCAL_MACHINE,
        ctypes.byref(output_blob),
    ):
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
        del input_buffer, entropy_buffer


def unprotect_machine_secret(value: bytes, *, entropy: bytes) -> bytes:
    _require_windows()
    if not value or len(value) > _MAX_PROTECTED_BYTES or not entropy:
        raise ValueError("Worker protected secret or entropy size is invalid")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_blob, input_buffer = _blob(bytes(value))
    entropy_blob, entropy_buffer = _blob(bytes(entropy))
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        result = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        if not result or len(result) > _MAX_PROTECTED_BYTES:
            raise ValueError("DPAPI returned an invalid Worker secret")
        return result
    finally:
        kernel32.LocalFree(output_blob.pbData)
        if description:
            kernel32.LocalFree(description)
        del input_buffer, entropy_buffer


def write_protected_secret(path: Path | str, value: bytes, *, entropy: bytes) -> Path:
    _require_windows()
    target = Path(path)
    if not target.is_absolute() or target.is_symlink() or target.exists():
        raise ValueError("Worker secret destination must be a new absolute regular path")
    parent = target.parent.resolve()
    if parent == parent.parent or not parent.is_dir() or parent.is_symlink():
        raise ValueError("Worker secret parent is unsafe")
    protected = protect_machine_secret(value, entropy=entropy)
    temporary = parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(protected)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def read_protected_secret(path: Path | str, *, entropy: bytes) -> bytes:
    _require_windows()
    target = Path(path)
    if not target.is_absolute() or target.is_symlink() or not target.is_file():
        raise ValueError("Worker secret path is missing or unsafe")
    size = target.stat().st_size
    if size <= 0 or size > _MAX_PROTECTED_BYTES:
        raise ValueError("Worker protected secret size is invalid")
    return unprotect_machine_secret(target.read_bytes(), entropy=entropy)


def load_ed25519_device_signer(
    path: Path | str,
    *,
    entropy: bytes,
    key_id: str,
) -> Ed25519PackageSigner:
    private_material = bytearray(read_protected_secret(path, entropy=entropy))
    try:
        from Crypto.PublicKey import ECC
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("pycryptodome is required for the Windows Worker") from exc
    try:
        private_key = ECC.import_key(bytes(private_material))
    except (ValueError, TypeError, IndexError) as exc:
        raise ValueError("protected Worker signing key is invalid") from exc
    finally:
        for index in range(len(private_material)):
            private_material[index] = 0
    return Ed25519PackageSigner(key_id=key_id, private_key=private_key)
