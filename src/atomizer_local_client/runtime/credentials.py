"""Per-user protected credential storage backed by Windows DPAPI."""

from __future__ import annotations

import ctypes
import os
import secrets
from ctypes import wintypes
from pathlib import Path


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(payload: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(payload)
    return (
        _DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))),
        buffer,
    )


def _dpapi_transform(
    payload: bytes, *, protect: bool, description: str = "Context Atomizer Local credential"
) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is required for production credential storage")
    input_blob, input_buffer = _blob(payload)
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        arguments = (
            ctypes.byref(input_blob),
            description,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        arguments = (
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    succeeded = function(*arguments)
    del input_buffer
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


class CredentialStore:
    def __init__(
        self,
        path: Path,
        *,
        description: str = "Context Atomizer Local management credential",
    ) -> None:
        self.path = Path(path)
        self.description = description

    def load(self) -> str:
        token = _dpapi_transform(
            self.path.read_bytes(), protect=False, description=self.description
        ).decode("ascii")
        if len(token) < 32:
            raise ValueError("stored credential is invalid")
        return token

    def load_or_create(self) -> str:
        try:
            return self.load()
        except FileNotFoundError:
            return self.rotate()

    def rotate(self) -> str:
        token = secrets.token_urlsafe(48)
        protected = _dpapi_transform(
            token.encode("ascii"), protect=True, description=self.description
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_bytes(protected)
        os.replace(temporary, self.path)
        return token

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)
