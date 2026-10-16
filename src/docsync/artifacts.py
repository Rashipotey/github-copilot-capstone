from __future__ import annotations

import difflib
import ctypes
import ntpath
import os
import secrets
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from docsync.models import ArtifactOutcome, ArtifactStatus, RunStatus
from docsync.paths import (
    OUTPUT_NAMES,
    PathValidationError,
    validate_output_paths,
    validate_repository_name,
)
from docsync.proposals import ProposalCandidate
from docsync.reporting import ReportData, ReportSafetyError, render_report
from docsync.screening import SecretScanner


_OUTPUT_WITHHELD = "Artifact content was unsafe or could not be screened; output was withheld."
_OUTPUT_WRITE_FAILED = "Artifact could not be staged or created safely."
_OUTPUT_EXISTS = "Output already exists; refusing to replace either artifact."
_OVERLAP = "Conflicting or overlapping proposals target the same document; patch was withheld."
_NATIVE_LINK = os.link


class ArtifactScreeningError(ValueError):
    """Artifact or proposal screening failed, so no affected output may be written."""


@dataclass(frozen=True, slots=True)
class PatchBuildResult:
    content: bytes | None
    proposals: tuple[ProposalCandidate, ...]
    withheld_proposals: int
    sanitized_detail: str


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    outcomes: tuple[ArtifactOutcome, ArtifactOutcome]
    status: RunStatus
    sanitized_detail: str
    report_content: bytes | None
    patch_content: bytes | None
    proposals: tuple[ProposalCandidate, ...]


@dataclass(frozen=True, slots=True)
class _Edit:
    start: int
    end: int
    replacement: str


def _screen_exact(scanner: SecretScanner, value: str, *, provenance: str) -> bool:
    try:
        result = scanner.screen_text(value, provenance=provenance, reject_detected=True)
    except Exception:
        return False
    return bool(
        result is not None
        and result.usable
        and result.sanitized_text == value
        and result.provenance is not None
    )


def _proposal_safe(proposal: ProposalCandidate, scanner: SecretScanner) -> bool:
    provenance = proposal.provenance
    try:
        validate_repository_name(proposal.target_path)
        validate_repository_name(provenance.source_path)
        validate_repository_name(provenance.target_path)
        for source_path in proposal.source_paths:
            validate_repository_name(source_path)
    except (PathValidationError, TypeError):
        return False

    values = (
        proposal.target_path,
        proposal.sanitized_reason,
        *proposal.source_paths,
        provenance.source_path,
        provenance.target_path,
        provenance.replacement_kind,
        provenance.old_value,
        provenance.new_value,
        provenance.diff_hunk or "",
    )
    if any(not _screen_exact(scanner, value, provenance="proposal provenance") for value in values):
        return False
    try:
        result = scanner.screen_candidate(proposal.replacement)
    except Exception:
        return False
    return bool(
        result is not None
        and result.usable
        and result.sanitized_text == proposal.replacement
    )


def _proposal_edit(original: str, replacement: str) -> _Edit | None:
    matcher = difflib.SequenceMatcher(a=original, b=replacement, autojunk=False)
    changes = [opcode for opcode in matcher.get_opcodes() if opcode[0] != "equal"]
    if len(changes) != 1:
        return None
    _tag, start, end, new_start, new_end = changes[0]
    return _Edit(start, end, replacement[new_start:new_end])


def _edits_conflict(first: _Edit, second: _Edit) -> bool:
    first_insertion = first.start == first.end
    second_insertion = second.start == second.end
    if first_insertion and second_insertion:
        return first.start == second.start
    if first_insertion:
        return second.start <= first.start <= second.end
    if second_insertion:
        return first.start <= second.start <= first.end
    return max(first.start, second.start) < min(first.end, second.end)


def build_patch(
    proposals: Sequence[ProposalCandidate],
    original_documents: Mapping[str, str],
    *,
    scanner: SecretScanner | None = None,
) -> PatchBuildResult:
    """Build a standard unified patch from screened originals without applying it."""
    scanner = scanner or SecretScanner()
    accepted: list[ProposalCandidate] = []
    grouped: dict[str, list[_Edit]] = {}
    withheld = 0

    for proposal in proposals:
        if not _proposal_safe(proposal, scanner):
            withheld += 1
            continue
        original = original_documents.get(proposal.target_path)
        if original is None or not _screen_exact(
            scanner, original, provenance=f"original Markdown:{proposal.target_path}"
        ):
            return PatchBuildResult(
                None,
                tuple(accepted),
                withheld + 1,
                "A screened original Markdown target was unavailable; patch was withheld.",
            )
        edit = _proposal_edit(original, proposal.replacement)
        if edit is None:
            return PatchBuildResult(
                None,
                tuple((*accepted, proposal)),
                withheld,
                "Proposal was not one direct replacement; patch was withheld.",
            )
        grouped.setdefault(proposal.target_path, []).append(edit)
        accepted.append(proposal)

    updated_documents: dict[str, str] = {}
    for path, edits in grouped.items():
        ordered = sorted(edits, key=lambda item: (item.start, item.end))
        if any(
            _edits_conflict(previous, current)
            for index, current in enumerate(ordered)
            for previous in ordered[:index]
        ):
            return PatchBuildResult(None, tuple(accepted), withheld, _OVERLAP)
        updated = original_documents[path]
        for edit in reversed(ordered):
            updated = updated[:edit.start] + edit.replacement + updated[edit.end:]
        updated_documents[path] = updated

    patch_lines: list[str] = []
    for path in sorted(updated_documents):
        original_lines = original_documents[path].splitlines(keepends=True)
        updated_lines = updated_documents[path].splitlines(keepends=True)
        patch_lines.extend(
            difflib.unified_diff(
                original_lines,
                updated_lines,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    content = "".join(patch_lines).encode("utf-8")
    detail = "Proposals were screened and a unified patch was generated."
    if withheld:
        detail = "Unsafe proposals were withheld; remaining safe proposals were retained."
    return PatchBuildResult(content, tuple(accepted), withheld, detail)


def _artifact_outcomes(paths: tuple[Path, Path], statuses: tuple[ArtifactStatus, ArtifactStatus], detail: str):
    return tuple(
        ArtifactOutcome(name, path, status, detail)
        for name, path, status in zip(OUTPUT_NAMES, paths, statuses)
    )


def _result(
    paths: tuple[Path, Path],
    statuses: tuple[ArtifactStatus, ArtifactStatus],
    detail: str,
    report_content: bytes | None,
    patch_content: bytes | None,
    proposals: tuple[ProposalCandidate, ...],
) -> ArtifactWriteResult:
    outcomes = _artifact_outcomes(paths, statuses, detail)
    written = sum(status is ArtifactStatus.WRITTEN for status in statuses)
    if written == 2:
        status = RunStatus.COMPLETED
    elif written:
        status = RunStatus.PARTIAL_ARTIFACTS
    else:
        status = RunStatus.FAILED
    return ArtifactWriteResult(outcomes, status, detail, report_content, patch_content, proposals)


def _screen_output(data: bytes, scanner: SecretScanner) -> bool:
    try:
        result = scanner.screen_output(data)
    except Exception:
        return False
    return bool(
        result is not None
        and result.usable
        and result.sanitized_text is not None
        and result.sanitized_text.encode("utf-8") == data
    )


def _screen_proposals(proposals: Sequence[ProposalCandidate], scanner: SecretScanner) -> bool:
    return all(_proposal_safe(proposal, scanner) for proposal in proposals)


class _DirectoryAnchor:
    """Hold a verified output directory against path-component substitution."""

    def __init__(self, directory: Path):
        self.path = Path(os.path.abspath(directory))
        self.fd: int | None = None
        self._handles: list[int] = []
        self._windows_ntdll = None

    def __enter__(self):
        if os.name == "nt":
            self._open_windows()
        else:
            self._open_posix()
        self.verify()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self._handles:
            for handle in reversed(self._handles):
                self._windows_kernel32.CloseHandle(ctypes.c_void_p(handle))
            self._handles.clear()

    def _open_posix(self) -> None:
        required = (os.open, os.stat, os.unlink, _NATIVE_LINK)
        if (
            not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_NOFOLLOW")
            or any(operation not in os.supports_dir_fd for operation in required)
            or _NATIVE_LINK not in os.supports_follow_symlinks
        ):
            raise OSError("anchored directory operations are unavailable")

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(Path(self.path.anchor), flags)
        try:
            for part in self.path.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            self.fd = descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _open_windows(self) -> None:
        from ctypes import wintypes

        absolute = self.path
        if not absolute.drive or absolute.anchor.startswith("\\\\"):
            raise OSError("anchored output is unavailable for this Windows path")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFinalPathNameByHandleW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel32.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = (
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
        kernel32.FlushFileBuffers.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        ntdll = ctypes.WinDLL("ntdll")

        class _AttributeTagInfo(ctypes.Structure):
            _fields_ = (("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD))

        def normalized(value: str) -> str:
            if value.startswith("\\\\?\\"):
                value = value[4:]
            return ntpath.normcase(ntpath.normpath(value))

        def open_directory(path: Path) -> int:
            handle = kernel32.CreateFileW(
                str(path),
                0x80,
                0x1 | 0x2 | 0x4,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            handle_value = ctypes.cast(handle, ctypes.c_void_p).value
            if handle_value in (None, ctypes.c_void_p(-1).value):
                raise OSError("output directory handle could not be opened")
            info = _AttributeTagInfo()
            if not kernel32.GetFileInformationByHandleEx(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                kernel32.CloseHandle(handle)
                raise OSError("output directory attributes could not be verified")
            if not info.attributes & 0x10 or info.attributes & 0x400:
                kernel32.CloseHandle(handle)
                raise OSError("output path is not a non-reparse directory")
            buffer = ctypes.create_unicode_buffer(32768)
            length = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer) or normalized(buffer.value) != normalized(str(path)):
                kernel32.CloseHandle(handle)
                raise OSError("output directory handle does not match its canonical path")
            return int(handle_value)

        current = Path(absolute.anchor)
        components = [current]
        for part in absolute.parts[1:]:
            current = current / part
            components.append(current)
        try:
            for component in components:
                self._handles.append(open_directory(component))
        except Exception:
            for handle in reversed(self._handles):
                kernel32.CloseHandle(ctypes.c_void_p(handle))
            self._handles.clear()
            raise
        self._windows_normalize = normalized
        self._windows_kernel32 = kernel32
        self._windows_ntdll = ntdll

    def verify(self) -> None:
        if os.name == "nt":
            if not self._handles:
                raise OSError("output directory is not anchored")
            from ctypes import wintypes

            buffer = ctypes.create_unicode_buffer(32768)
            length = self._windows_kernel32.GetFinalPathNameByHandleW(
                wintypes.HANDLE(self._handles[-1]), buffer, len(buffer), 0
            )
            if (
                not length
                or length >= len(buffer)
                or self._windows_normalize(buffer.value) != self._windows_normalize(str(self.path))
            ):
                raise OSError("output directory anchor changed")
            return

        if self.fd is None:
            raise OSError("output directory is not anchored")
        opened = os.fstat(self.fd)
        named = os.stat(self.path, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            raise OSError("output directory identity changed")
        proc_path = Path(f"/proc/self/fd/{self.fd}")
        if proc_path.exists() and proc_path.resolve(strict=True) != self.path.resolve(strict=True):
            raise OSError("output directory escaped its canonical path")

    def create_temporary(self, data: bytes) -> Path:
        self.verify()
        if os.name == "nt":
            name = f".docsync-{secrets.token_hex(12)}.tmp"
            handle = self._windows_open_relative(
                name,
                access=0x40000000 | 0x10000 | 0x100000 | 0x80,
                disposition=2,
                options=0x40 | 0x00200000 | 0x20,
            )
            temporary = self.path / name
            try:
                offset = 0
                while offset < len(data):
                    written = ctypes.c_ulong()
                    chunk = data[offset : offset + 1024 * 1024]
                    buffer = ctypes.create_string_buffer(chunk)
                    if not self._windows_kernel32.WriteFile(
                        ctypes.c_void_p(handle), buffer, len(chunk), ctypes.byref(written), None
                    ) or written.value != len(chunk):
                        raise OSError("temporary artifact could not be written")
                    offset += written.value
                if not self._windows_kernel32.FlushFileBuffers(ctypes.c_void_p(handle)):
                    raise OSError("temporary artifact could not be flushed")
            except Exception:
                self._windows_delete_handle(handle)
                self._windows_kernel32.CloseHandle(ctypes.c_void_p(handle))
                raise
            self._windows_kernel32.CloseHandle(ctypes.c_void_p(handle))
            return temporary
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            for _ in range(100):
                name = f".docsync-{secrets.token_hex(12)}.tmp"
                try:
                    descriptor = os.open(name, flags, 0o600, dir_fd=self.fd)
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("temporary artifact name could not be allocated")
            temporary = self.path / name
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            self.unlink(temporary.name)
            raise
        return temporary

    def unlink(self, name: str) -> None:
        self.verify()
        if os.name == "nt":
            handle = self._windows_open_relative(
                name,
                access=0x10000 | 0x80 | 0x100000,
                disposition=1,
                options=0x40 | 0x00200000 | 0x20,
            )
            try:
                self._windows_delete_handle(handle)
            finally:
                self._windows_kernel32.CloseHandle(ctypes.c_void_p(handle))
        else:
            os.unlink(name, dir_fd=self.fd)

    def file_identity(self, name: str) -> tuple[int, ...]:
        if os.name == "nt":
            handle = self._windows_open_relative(
                name,
                access=0x80 | 0x100000,
                disposition=1,
                options=0x40 | 0x00200000 | 0x20,
            )
            try:
                return self._windows_file_identity(handle)
            finally:
                self._windows_kernel32.CloseHandle(ctypes.c_void_p(handle))

        metadata = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("anchored artifact is not a regular file")
        return metadata.st_dev, metadata.st_ino

    def unlink_if_identity(self, name: str, identity: tuple[int, ...]) -> None:
        if os.name == "nt":
            handle = self._windows_open_relative(
                name,
                access=0x10000 | 0x80 | 0x100000,
                disposition=1,
                options=0x40 | 0x00200000 | 0x20,
            )
            try:
                if self._windows_file_identity(handle) != identity:
                    raise OSError("anchored artifact identity changed")
                self._windows_delete_handle(handle)
            finally:
                self._windows_kernel32.CloseHandle(ctypes.c_void_p(handle))
            return
        raise OSError("identity-safe anchored unlink is unavailable")

    def _windows_file_identity(self, handle: int) -> tuple[int, ...]:
        from ctypes import wintypes

        class _AttributeTagInfo(ctypes.Structure):
            _fields_ = (("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD))

        class _FileTime(ctypes.Structure):
            _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = (
                ("attributes", wintypes.DWORD),
                ("creation", _FileTime),
                ("access", _FileTime),
                ("write", _FileTime),
                ("volume", wintypes.DWORD),
                ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD),
                ("index_low", wintypes.DWORD),
            )

        attributes = _AttributeTagInfo()
        if not self._windows_kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle), 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ) or attributes.attributes & (0x10 | 0x400):
            raise OSError("anchored artifact is not a regular file")
        identity = _ByHandleFileInformation()
        if not self._windows_kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle), ctypes.byref(identity)
        ):
            raise OSError("anchored artifact identity could not be verified")
        return identity.volume, identity.index_high, identity.index_low

    def _windows_open_relative(self, name: str, *, access: int, disposition: int, options: int) -> int:
        from ctypes import wintypes

        if not name or "\\" in name or "/" in name or name in (".", ".."):
            raise OSError("unsafe artifact name")

        class _UnicodeString(ctypes.Structure):
            _fields_ = (
                ("length", wintypes.USHORT),
                ("maximum_length", wintypes.USHORT),
                ("buffer", wintypes.LPWSTR),
            )

        class _ObjectAttributes(ctypes.Structure):
            _fields_ = (
                ("length", wintypes.ULONG),
                ("root_directory", wintypes.HANDLE),
                ("object_name", ctypes.POINTER(_UnicodeString)),
                ("attributes", wintypes.ULONG),
                ("security_descriptor", wintypes.LPVOID),
                ("security_quality_of_service", wintypes.LPVOID),
            )

        class _IoStatusBlock(ctypes.Structure):
            _fields_ = (("status", wintypes.LPVOID), ("information", ctypes.c_size_t))

        encoded = name.encode("utf-16-le")
        name_buffer = ctypes.create_unicode_buffer(name)
        unicode_name = _UnicodeString(len(encoded), len(encoded), ctypes.cast(name_buffer, wintypes.LPWSTR))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            wintypes.HANDLE(self._handles[-1]),
            ctypes.pointer(unicode_name),
            0x40,
            None,
            None,
        )
        io_status = _IoStatusBlock()
        handle = wintypes.HANDLE()
        self._windows_ntdll.NtCreateFile.argtypes = (
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.ULONG,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
        )
        self._windows_ntdll.NtCreateFile.restype = ctypes.c_long
        status = self._windows_ntdll.NtCreateFile(
            ctypes.byref(handle),
            access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0x80,
            0x7,
            disposition,
            options,
            None,
            0,
        )
        if status < 0:
            raise OSError("anchored artifact file operation failed")
        return int(ctypes.cast(handle, ctypes.c_void_p).value)

    def _windows_delete_handle(self, handle: int) -> None:
        from ctypes import wintypes

        class _IoStatusBlock(ctypes.Structure):
            _fields_ = (("status", wintypes.LPVOID), ("information", ctypes.c_size_t))

        io_status = _IoStatusBlock()
        delete = ctypes.c_ubyte(1)
        self._windows_ntdll.NtSetInformationFile.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            ctypes.c_int,
        )
        self._windows_ntdll.NtSetInformationFile.restype = ctypes.c_long
        status = self._windows_ntdll.NtSetInformationFile(
            ctypes.c_void_p(handle),
            ctypes.byref(io_status),
            ctypes.byref(delete),
            ctypes.sizeof(delete),
            13,
        )
        if status < 0:
            raise OSError("temporary artifact cleanup failed")

    def _windows_link_from(self, source: Path, target_name: str, target_anchor) -> None:
        from ctypes import wintypes

        class _IoStatusBlock(ctypes.Structure):
            _fields_ = (("status", wintypes.LPVOID), ("information", ctypes.c_size_t))

        class _FileLinkInformation(ctypes.Structure):
            _fields_ = (
                ("replace_if_exists", wintypes.BOOLEAN),
                ("root_directory", wintypes.HANDLE),
                ("file_name_length", wintypes.DWORD),
                ("file_name", wintypes.WCHAR * 1),
            )

        if not target_name or "\\" in target_name or "/" in target_name:
            raise OSError("unsafe artifact target")
        source_handle = self._windows_open_relative(
            source.name,
            access=0x80000000 | 0x10000 | 0x80 | 0x100000,
            disposition=1,
            options=0x40 | 0x00200000 | 0x20,
        )
        try:
            from ctypes import wintypes

            attributes = (wintypes.DWORD * 2)()
            if not self._windows_kernel32.GetFileInformationByHandleEx(
                wintypes.HANDLE(source_handle),
                9,
                ctypes.byref(attributes),
                ctypes.sizeof(attributes),
            ) or attributes[0] & (0x10 | 0x400):
                raise OSError("staged artifact is not a regular file")
            target_bytes = target_name.encode("utf-16-le")
            buffer_size = _FileLinkInformation.file_name.offset + len(target_bytes)
            link_buffer = ctypes.create_string_buffer(buffer_size)
            link = ctypes.cast(link_buffer, ctypes.POINTER(_FileLinkInformation)).contents
            link.replace_if_exists = 0
            link.root_directory = wintypes.HANDLE(target_anchor._handles[-1])
            link.file_name_length = len(target_bytes)
            ctypes.memmove(
                ctypes.addressof(link_buffer) + _FileLinkInformation.file_name.offset,
                target_bytes,
                len(target_bytes),
            )
            io_status = _IoStatusBlock()
            self._windows_ntdll.NtSetInformationFile.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_IoStatusBlock),
                wintypes.LPVOID,
                wintypes.ULONG,
                ctypes.c_int,
            )
            self._windows_ntdll.NtSetInformationFile.restype = ctypes.c_long
            status = self._windows_ntdll.NtSetInformationFile(
                wintypes.HANDLE(source_handle),
                ctypes.byref(io_status),
                link_buffer,
                buffer_size,
                11,
            )
            if status < 0:
                raise OSError("no-replace artifact publication failed")
        finally:
            self._windows_kernel32.CloseHandle(wintypes.HANDLE(source_handle))


def _stage_bytes(data: bytes, directory: Path, scanner: SecretScanner, proposals) -> Path:
    if not _screen_proposals(proposals, scanner) or not _screen_output(data, scanner):
        raise ArtifactScreeningError("screening failed")
    with _DirectoryAnchor(directory) as anchor:
        return anchor.create_temporary(data)


def _link_anchored(source: Path, target: Path, source_anchor, target_anchor) -> None:
    source_anchor.verify()
    target_anchor.verify()
    if os.name == "nt":
        source_anchor._windows_link_from(source, target.name, target_anchor)
    else:
        os.link(
            source.name,
            target.name,
            src_dir_fd=source_anchor.fd,
            dst_dir_fd=target_anchor.fd,
            follow_symlinks=False,
        )


def _publish_staged_file(
    temporary: Path,
    target: Path,
    data: bytes,
    scanner: SecretScanner,
    proposals,
) -> None:
    if not _screen_proposals(proposals, scanner) or not _screen_output(data, scanner):
        raise ArtifactScreeningError("screening failed")
    with _DirectoryAnchor(temporary.parent) as source_anchor, _DirectoryAnchor(
        target.parent
    ) as target_anchor:
        try:
            if os.name == "nt":
                target_metadata = target.lstat()
            else:
                target_metadata = os.stat(
                    target.name,
                    dir_fd=target_anchor.fd,
                    follow_symlinks=False,
                )
        except FileNotFoundError:
            target_metadata = None
        if target_metadata is not None:
            if stat.S_ISLNK(target_metadata.st_mode) or not stat.S_ISREG(target_metadata.st_mode):
                raise ValueError("unsafe target")
            raise FileExistsError("output target already exists")
        if os.name == "nt":
            source_metadata = temporary.lstat()
            source_attributes = getattr(source_metadata, "st_file_attributes", 0)
            if stat.S_ISLNK(source_metadata.st_mode) or source_attributes & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise ValueError("unsafe staged file")
            target_device = target_anchor.path.stat().st_dev
        else:
            source_metadata = os.stat(
                temporary.name,
                dir_fd=source_anchor.fd,
                follow_symlinks=False,
            )
            target_device = os.fstat(target_anchor.fd).st_dev
        if not stat.S_ISREG(source_metadata.st_mode) or source_metadata.st_dev != target_device:
            raise OSError("staged file and output target are on different filesystems")
        staged_identity = source_anchor.file_identity(temporary.name)
        target_anchor.verify()
        _link_anchored(temporary, target, source_anchor, target_anchor)
        try:
            target_anchor.verify()
            if target_anchor.file_identity(target.name) != staged_identity:
                raise OSError("published artifact identity could not be verified")
        except OSError:
            if os.name == "nt":
                try:
                    target_anchor.unlink_if_identity(target.name, staged_identity)
                except OSError:
                    raise OSError(
                        "output directory changed during publication and rollback was not safe"
                    ) from None
                raise OSError(
                    "output directory changed during publication; artifact was rolled back"
                ) from None
            raise OSError(
                "output directory changed during publication; target was preserved"
            ) from None
        try:
            source_anchor.unlink(temporary.name)
        except OSError:
            pass


def _cleanup_temporary(temporary: Path) -> None:
    try:
        with _DirectoryAnchor(temporary.parent) as anchor:
            anchor.unlink(temporary.name)
    except OSError:
        pass


def _staging_directory(paths, repo_root: Path, repository_inputs: Sequence[str]) -> Path:
    document_directories: set[Path] = set()
    for raw_path in repository_inputs:
        if raw_path.casefold().endswith(".md"):
            try:
                parts = validate_repository_name(raw_path)
            except (PathValidationError, TypeError):
                continue
            document_directories.add(repo_root.joinpath(*parts[:-1]).resolve(strict=False))

    candidate = paths.directory.parent
    while candidate in document_directories:
        parent = candidate.parent
        if parent == candidate:
            raise OSError("safe staging location unavailable")
        candidate = parent
    while not candidate.exists():
        candidate = candidate.parent
    if not candidate.is_dir():
        raise OSError("staging location unavailable")
    if candidate.stat().st_dev != paths.directory.stat().st_dev:
        if paths.directory not in document_directories:
            candidate = paths.directory
        else:
            raise OSError("safe atomic staging location unavailable")
    if candidate in document_directories:
        raise OSError("staging location intersects source documentation")
    return candidate


def _failed_paths(repo_root: Path, output_dir: Path | str | None) -> tuple[Path, Path]:
    try:
        root = Path(repo_root).resolve(strict=True)
    except Exception:
        root = Path.cwd()
    if output_dir is None:
        directory = root / "docsync-output"
    else:
        requested = Path(output_dir)
        directory = requested if requested.is_absolute() else root / requested
    return tuple(directory / name for name in OUTPUT_NAMES)


def write_artifacts(
    repo_root: Path | str,
    report: ReportData,
    original_documents: Mapping[str, str],
    *,
    proposals: Sequence[ProposalCandidate] | None = None,
    output_dir: Path | str | None = None,
    repository_inputs: Sequence[str] = (),
    scanner: SecretScanner | None = None,
) -> ArtifactWriteResult:
    """Write only the report and patch, creating each target without replacement."""
    scanner = scanner or SecretScanner()
    requested_dir = Path(output_dir) if output_dir is not None else Path("docsync-output")
    candidates = tuple(report.proposals if proposals is None else proposals)
    fallback_paths = _failed_paths(repo_root, output_dir)
    temporaries: list[Path] = []
    report_content: bytes | None = None
    patch_content: bytes | None = None
    created_output_directory: Path | None = None
    try:
        paths = validate_output_paths(repo_root, requested_dir, repository_inputs)
        targets = (paths.report, paths.patch)
        if any(target.exists() for target in targets):
            return _result(
                targets,
                (ArtifactStatus.FAILED, ArtifactStatus.FAILED),
                _OUTPUT_EXISTS,
                None,
                None,
                (),
            )
        if not paths.directory.exists():
            created_output_directory = paths.directory
        paths.directory.mkdir(parents=True, exist_ok=True)
        paths = validate_output_paths(repo_root, requested_dir, repository_inputs)
        targets = (paths.report, paths.patch)
        if any(target.exists() for target in targets):
            return _result(
                targets,
                (ArtifactStatus.FAILED, ArtifactStatus.FAILED),
                _OUTPUT_EXISTS,
                None,
                None,
                (),
            )
        canonical_root = Path(repo_root).resolve(strict=True)
        staging_dir = _staging_directory(paths, canonical_root, repository_inputs)

        patch_build = build_patch(candidates, original_documents, scanner=scanner)
        accepted = patch_build.proposals
        updated = replace(
            report,
            proposals=accepted,
            artifact_paths=(str(paths.report), str(paths.patch)),
        )
        if patch_build.withheld_proposals:
            updated = replace(
                updated,
                warnings=(*updated.warnings, "One or more unsafe proposals were withheld."),
            )
        patch_content = patch_build.content
        if patch_content is None:
            updated = replace(
                updated,
                execution_status=RunStatus.PARTIAL_ARTIFACTS.value,
                warnings=(*updated.warnings, patch_build.sanitized_detail),
                unresolved_items=(*updated.unresolved_items, patch_build.sanitized_detail),
            )

        patch_temp: Path | None = None
        patch_status = ArtifactStatus.FAILED
        if patch_content is not None:
            try:
                patch_temp = _stage_bytes(patch_content, staging_dir, scanner, accepted)
                temporaries.append(patch_temp)
                patch_status = ArtifactStatus.SKIPPED
            except Exception:
                patch_content = None
                updated = replace(
                    updated,
                    execution_status=RunStatus.PARTIAL_ARTIFACTS.value,
                    warnings=(*updated.warnings, "Patch artifact could not be staged safely."),
                    unresolved_items=(*updated.unresolved_items, "Patch artifact could not be staged safely."),
                )

        def render_current() -> bytes:
            rendered = render_report(updated, scanner=scanner).encode("utf-8")
            if not _screen_output(rendered, scanner):
                raise ReportSafetyError(_OUTPUT_WITHHELD)
            return rendered

        def replace_target(temporary: Path, target: Path, content: bytes) -> None:
            current = validate_output_paths(repo_root, requested_dir, repository_inputs)
            if target not in (current.report, current.patch):
                raise PathValidationError("Output target changed during artifact writing.")
            _publish_staged_file(temporary, target, content, scanner, accepted)

        try:
            report_content = render_current()
            report_temp = _stage_bytes(report_content, staging_dir, scanner, accepted)
            temporaries.append(report_temp)
        except (ArtifactScreeningError, ReportSafetyError):
            report_content = None
            report_status = ArtifactStatus.FAILED
            patch_status = ArtifactStatus.FAILED
            return _result(
                targets,
                (report_status, patch_status),
                _OUTPUT_WITHHELD,
                None,
                None,
                accepted,
            )
        except Exception:
            report_content = None
            report_status = ArtifactStatus.FAILED
            if patch_temp is not None:
                try:
                    replace_target(patch_temp, paths.patch, patch_content or b"")
                    if not patch_temp.exists():
                        temporaries.remove(patch_temp)
                    patch_status = ArtifactStatus.WRITTEN
                except Exception:
                    patch_status = ArtifactStatus.FAILED
            return _result(
                targets,
                (report_status, patch_status),
                _OUTPUT_WITHHELD,
                report_content,
                patch_content if patch_status is ArtifactStatus.WRITTEN else None,
                accepted,
            )

        if patch_temp is not None:
            try:
                replace_target(patch_temp, paths.patch, patch_content or b"")
                if not patch_temp.exists():
                    temporaries.remove(patch_temp)
                patch_status = ArtifactStatus.WRITTEN
            except Exception:
                patch_status = ArtifactStatus.FAILED
                patch_content = None
                updated = replace(
                    updated,
                    execution_status=RunStatus.PARTIAL_ARTIFACTS.value,
                    warnings=(*updated.warnings, "Patch artifact creation failed; the patch was not written."),
                    unresolved_items=(*updated.unresolved_items, "Patch artifact creation failed."),
                )
                try:
                    report_temp.unlink(missing_ok=True)
                    temporaries.remove(report_temp)
                    report_content = render_current()
                    report_temp = _stage_bytes(report_content, staging_dir, scanner, accepted)
                    temporaries.append(report_temp)
                except Exception:
                    report_content = None

        report_status = ArtifactStatus.FAILED
        if report_content is not None:
            try:
                replace_target(report_temp, paths.report, report_content)
                if not report_temp.exists():
                    temporaries.remove(report_temp)
                report_status = ArtifactStatus.WRITTEN
            except Exception:
                report_status = ArtifactStatus.FAILED

        if patch_status is ArtifactStatus.WRITTEN and report_status is ArtifactStatus.WRITTEN:
            detail = "Report and patch artifacts were written."
        elif patch_status is ArtifactStatus.WRITTEN or report_status is ArtifactStatus.WRITTEN:
            detail = "Artifact generation was partial; per-file outcomes are authoritative."
        else:
            detail = _OUTPUT_WRITE_FAILED
        return _result(
            targets,
            (report_status, patch_status),
            detail,
            report_content,
            patch_content if patch_status is ArtifactStatus.WRITTEN else None,
            accepted,
        )
    except PathValidationError:
        return _result(
            fallback_paths,
            (ArtifactStatus.FAILED, ArtifactStatus.FAILED),
            "Output paths failed safety validation; no artifacts were written.",
            None,
            None,
            (),
        )
    except Exception:
        return _result(
            fallback_paths,
            (ArtifactStatus.FAILED, ArtifactStatus.FAILED),
            _OUTPUT_WRITE_FAILED,
            None,
            None,
            (),
        )
    finally:
        for temporary in temporaries:
            _cleanup_temporary(temporary)
        if created_output_directory is not None:
            try:
                created_output_directory.rmdir()
            except OSError:
                pass