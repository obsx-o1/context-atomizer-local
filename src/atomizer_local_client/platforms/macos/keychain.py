"""Per-user macOS Keychain credential storage through Security.framework."""

from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol


_ERR_SEC_ITEM_NOT_FOUND = -25300
_GENERIC_PASSWORD_ITEM_CLASS = 0x67656E70
_SERVICE_ATTRIBUTE = 0x73766365
_ACCOUNT_ATTRIBUTE = 0x61636374
_SERVICE = "com.contextatomizer.local.credentials"


class _SecKeychainAttribute(ctypes.Structure):
    _fields_ = [
        ("tag", ctypes.c_uint32),
        ("length", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


class _SecKeychainAttributeList(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("attr", ctypes.POINTER(_SecKeychainAttribute)),
    ]


class KeychainBackend(Protocol):
    def load(self, service: str, account: str) -> bytes: ...
    def store(
        self,
        service: str,
        account: str,
        payload: bytes,
        trusted_executables: tuple[Path, ...],
    ) -> None: ...
    def remove(self, service: str, account: str) -> None: ...


class SecurityFrameworkKeychain:
    def __init__(self, keychain_path: Path | None = None) -> None:
        if sys.platform != "darwin":
            raise OSError("macOS Keychain Services are unavailable on this platform")
        self.keychain_path = Path(keychain_path).resolve() if keychain_path else None
        self.security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self.core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._configure_functions()

    def _configure_functions(self) -> None:
        pointer = ctypes.c_void_p
        length = ctypes.c_uint32
        self.security.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(pointer)]
        self.security.SecKeychainOpen.restype = ctypes.c_int32
        self.security.SecKeychainFindGenericPassword.argtypes = [
            pointer,
            length,
            ctypes.c_char_p,
            length,
            ctypes.c_char_p,
            ctypes.POINTER(length),
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
        ]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemCreateFromContent.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_SecKeychainAttributeList),
            length,
            pointer,
            pointer,
            pointer,
            ctypes.POINTER(pointer),
        ]
        self.security.SecKeychainItemCreateFromContent.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [
            pointer,
            pointer,
            length,
            pointer,
        ]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemDelete.argtypes = [pointer]
        self.security.SecKeychainItemDelete.restype = ctypes.c_int32
        self.security.SecTrustedApplicationCreateFromPath.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(pointer),
        ]
        self.security.SecTrustedApplicationCreateFromPath.restype = ctypes.c_int32
        self.security.SecAccessCreate.argtypes = [pointer, pointer, ctypes.POINTER(pointer)]
        self.security.SecAccessCreate.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.core_foundation.CFStringCreateWithCString.argtypes = [
            pointer,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.core_foundation.CFStringCreateWithCString.restype = pointer
        self.core_foundation.CFArrayCreate.argtypes = [
            pointer,
            ctypes.POINTER(pointer),
            ctypes.c_long,
            pointer,
        ]
        self.core_foundation.CFArrayCreate.restype = pointer
        self.core_foundation.CFRelease.argtypes = [pointer]
        self.core_foundation.CFRelease.restype = None

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if status != 0:
            raise OSError(status, f"macOS Keychain {operation} failed")

    @contextmanager
    def _keychain(self) -> Iterator[ctypes.c_void_p]:
        reference = ctypes.c_void_p()
        if self.keychain_path is not None:
            status = self.security.SecKeychainOpen(
                os.fsencode(self.keychain_path), ctypes.byref(reference)
            )
            self._check(status, "open")
        try:
            yield reference
        finally:
            if reference.value:
                self.core_foundation.CFRelease(reference)

    def _find(
        self, service: str, account: str
    ) -> tuple[bytes, ctypes.c_void_p] | None:
        service_bytes = service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        with self._keychain() as keychain:
            status = self.security.SecKeychainFindGenericPassword(
                keychain,
                len(service_bytes),
                service_bytes,
                len(account_bytes),
                account_bytes,
                ctypes.byref(password_length),
                ctypes.byref(password_data),
                ctypes.byref(item),
            )
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return None
        self._check(status, "lookup")
        try:
            payload = ctypes.string_at(password_data, password_length.value)
        finally:
            self.security.SecKeychainItemFreeContent(None, password_data)
        return payload, item

    @contextmanager
    def _access(
        self, description: str, trusted_executables: tuple[Path, ...]
    ) -> Iterator[ctypes.c_void_p]:
        trusted: list[ctypes.c_void_p] = []
        description_ref = ctypes.c_void_p()
        array_ref = ctypes.c_void_p()
        access_ref = ctypes.c_void_p()
        try:
            for path in trusted_executables:
                application = ctypes.c_void_p()
                status = self.security.SecTrustedApplicationCreateFromPath(
                    os.fsencode(path), ctypes.byref(application)
                )
                self._check(status, "trusted application creation")
                trusted.append(application)
            values = (ctypes.c_void_p * len(trusted))(
                *(application.value for application in trusted)
            )
            array_ref = ctypes.c_void_p(
                self.core_foundation.CFArrayCreate(
                    None, values, len(trusted), None
                )
            )
            description_ref = ctypes.c_void_p(
                self.core_foundation.CFStringCreateWithCString(
                    None, description.encode("utf-8"), 0x08000100
                )
            )
            if not array_ref or not description_ref:
                raise MemoryError("macOS Keychain access allocation failed")
            self._check(
                self.security.SecAccessCreate(
                    description_ref, array_ref, ctypes.byref(access_ref)
                ),
                "access creation",
            )
            yield access_ref
        finally:
            for reference in (access_ref, description_ref, array_ref, *trusted):
                if reference and reference.value:
                    self.core_foundation.CFRelease(reference)

    def load(self, service: str, account: str) -> bytes:
        found = self._find(service, account)
        if found is None:
            raise FileNotFoundError(account)
        payload, item = found
        self.core_foundation.CFRelease(item)
        return payload

    def store(
        self,
        service: str,
        account: str,
        payload: bytes,
        trusted_executables: tuple[Path, ...],
    ) -> None:
        found = self._find(service, account)
        data = ctypes.create_string_buffer(payload)
        if found is not None:
            _, item = found
            try:
                status = self.security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(payload), ctypes.cast(data, ctypes.c_void_p)
                )
                self._check(status, "update")
            finally:
                self.core_foundation.CFRelease(item)
            return
        service_bytes = service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        service_data = ctypes.create_string_buffer(service_bytes)
        account_data = ctypes.create_string_buffer(account_bytes)
        attributes = (_SecKeychainAttribute * 2)(
            _SecKeychainAttribute(
                _SERVICE_ATTRIBUTE,
                len(service_bytes),
                ctypes.cast(service_data, ctypes.c_void_p),
            ),
            _SecKeychainAttribute(
                _ACCOUNT_ATTRIBUTE,
                len(account_bytes),
                ctypes.cast(account_data, ctypes.c_void_p),
            ),
        )
        attribute_list = _SecKeychainAttributeList(len(attributes), attributes)
        item = ctypes.c_void_p()
        with self._keychain() as keychain, self._access(
            account, trusted_executables
        ) as access:
            status = self.security.SecKeychainItemCreateFromContent(
                _GENERIC_PASSWORD_ITEM_CLASS,
                ctypes.byref(attribute_list),
                len(payload),
                ctypes.cast(data, ctypes.c_void_p),
                keychain,
                access,
                ctypes.byref(item),
            )
            self._check(status, "create")
            if item.value:
                self.core_foundation.CFRelease(item)

    def remove(self, service: str, account: str) -> None:
        found = self._find(service, account)
        if found is None:
            return
        _, item = found
        try:
            self._check(self.security.SecKeychainItemDelete(item), "delete")
        finally:
            self.core_foundation.CFRelease(item)


class MacOSKeychainCredentialStore:
    def __init__(
        self,
        path: Path,
        *,
        description: str = "Context Atomizer Local management credential",
        backend: KeychainBackend | None = None,
    ) -> None:
        identity = str(Path(path).resolve()).encode("utf-8")
        self.account = f"{description}:{hashlib.sha256(identity).hexdigest()[:24]}"
        self.service = _SERVICE
        executable = Path(sys.executable).resolve()
        candidates = (
            executable,
            executable.parent / "atomizer-local-manager",
            executable.parent / "atomizer-local-runtime",
        )
        self.trusted_executables = tuple(
            dict.fromkeys(path for path in candidates if path.is_file())
        )
        isolated = os.environ.get("ATOMIZER_MACOS_KEYCHAIN")
        self.backend = backend or SecurityFrameworkKeychain(
            Path(isolated) if isolated else None
        )

    def load(self) -> str:
        token = self.backend.load(self.service, self.account).decode("ascii")
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
        self.backend.store(
            self.service,
            self.account,
            token.encode("ascii"),
            self.trusted_executables,
        )
        return token

    def remove(self) -> None:
        self.backend.remove(self.service, self.account)
