# PyFLP - An FL Studio project file (.flp) parser
# Copyright (C) 2022 demberto
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. This program is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
# Public License for more details. You should have received a copy of the
# GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.

"""
PyFLP - FL Studio project file parser
=====================================

Load a project file:

    >>> import pyflp
    >>> project = pyflp.parse("/path/to/parse.flp")

Save the project:

    >>> pyflp.save(project, "/path/to/save.flp")

Full docs are available at https://pyflp.rtfd.io.
"""  # noqa

from __future__ import annotations

import io
import os
import pathlib
import struct
import sys

import construct as c

from pyflp._events import (
    DATA,
    DWORD,
    NEW_TEXT_IDS,
    TEXT,
    WORD,
    AnyEvent,
    AsciiEvent,
    EventEnum,
    EventTree,
    IndexedEvent,
    U8Event,
    U16Event,
    U32Event,
    UnicodeEvent,
    UnknownDataEvent,
)
from pyflp.exceptions import HeaderCorrupted, VersionNotDetected
from pyflp.plugin import PluginID, get_event_by_internal_name
from pyflp.project import VALID_PPQS, FileFormat, Project, ProjectID
from pyflp.arrangement import PlaylistEvent

__all__ = ["parse", "save"]

FLP_HEADER = struct.Struct("4sIh2H")

if sys.version_info < (3, 11):  # https://github.com/Bobronium/fastenum/issues/2
    import fastenum

    fastenum.enable()  # 33% faster parse()


_AC_ID = DWORD + 44
_AC_DWORD = "dword"
_AC_VARINT_LENGTH = "varint-length"
_AC_VARINT_VALUE = "varint-value"
_TEMPO_IDS = frozenset((ProjectID.Tempo.value, ProjectID._TempoCoarse.value))


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _walk(buf: bytes, ac_layout: str) -> tuple[bool, bool, int | None]:
    """Size-only pass over the event chunk.

    Returns whether the stream ends exactly at EOF with a tempo event seen,
    whether any 0xAC event occurred, and the FL major version.
    """
    pos, end = 22, len(buf)
    saw_ac = saw_tempo = False
    fl_version: int | None = None
    try:
        while pos < end:
            id = buf[pos]
            pos += 1
            if id < WORD:
                pos += 1
            elif id < DWORD:
                pos += 2
            elif id < TEXT:
                if id == _AC_ID:
                    saw_ac = True
                    if ac_layout == _AC_VARINT_LENGTH:
                        size, pos = _read_varint(buf, pos)
                        pos += size
                        continue
                    if ac_layout == _AC_VARINT_VALUE:
                        _, pos = _read_varint(buf, pos)
                        continue
                pos += 4
            else:
                size, pos = _read_varint(buf, pos)
                if id == ProjectID.FLVersion.value and fl_version is None:
                    try:
                        fl_version = int(buf[pos : pos + size].split(b".")[0])
                    except ValueError:
                        pass
                pos += size
            if id in _TEMPO_IDS:
                saw_tempo = True
    except IndexError:
        return False, saw_ac, fl_version
    return pos == end and saw_tempo, saw_ac, fl_version


def _detect_ac_layout(buf: bytes) -> str:
    """Pick how event 0xAC (DWORD+44) is encoded in this file.

    FL 2024/2025 usually store it as a fixed 4-byte DWORD and FL26 as a
    varint-length payload, but some FL 25.2.5 builds write a bare varint value
    (e.g. ``AC 01``); a fixed read there silently derails the stream and drops
    the tempo, channels and mixer. The version-based default is kept whenever
    it walks the whole chunk cleanly; the other layouts are only tried when it
    does not.
    """
    _, saw_ac, fl_version = _walk(buf, _AC_DWORD)
    default = _AC_VARINT_LENGTH if fl_version is not None and fl_version >= 26 else _AC_DWORD
    if not saw_ac:
        return default
    candidates = [default] + [
        layout
        for layout in (_AC_DWORD, _AC_VARINT_VALUE, _AC_VARINT_LENGTH)
        if layout != default
    ]
    for layout in candidates:
        if _walk(buf, layout)[0]:
            return layout
    return default


def parse(file: pathlib.Path | str) -> Project:
    """Parses an FL Studio project file and returns a parsed :class:`Project`.

    Args:
        file: Path to the FLP.

    Raises:
        HeaderCorrupted: When an invalid value is found in the file header.
        VersionNotDetected: A correct string type couldn't be determined.
    """
    with open(file, "rb") as flp:
        stream = io.BytesIO(flp.read())

    events: list[AnyEvent] = []
    header = stream.read(FLP_HEADER.size)

    try:
        hdr_magic, hdr_size, fmt, channel_count, ppq = FLP_HEADER.unpack(header)
    except struct.error as exc:
        raise HeaderCorrupted("Couldn't read the header entirely") from exc

    if hdr_magic != b"FLhd":
        raise HeaderCorrupted("Unexpected header chunk magic; expected 'FLhd'")

    if hdr_size != 6:
        raise HeaderCorrupted("Unexpected header chunk size; expected 6")

    try:
        file_format = FileFormat(fmt)
    except ValueError as exc:
        raise HeaderCorrupted("Unsupported project file format") from exc

    if ppq not in VALID_PPQS:
        raise HeaderCorrupted("Invalid PPQ")

    if stream.read(4) != b"FLdt":
        raise HeaderCorrupted("Unexpected data chunk magic; expected 'FLdt'")

    events_size = int.from_bytes(stream.read(4), "little")
    if not events_size:  # pragma: no cover
        raise HeaderCorrupted("Data chunk size couldn't be read")

    stream.seek(0, os.SEEK_END)
    file_size = stream.tell()
    if file_size != events_size + 22:
        raise HeaderCorrupted("Data chunk size corrupted")

    ac_layout = _detect_ac_layout(stream.getvalue())

    plug_name = None
    str_type: type[AsciiEvent] | type[UnicodeEvent] | None = None
    fl_version: int | None = None  # FL major version, set by the first event (FLVersion)
    stream.seek(22)  # Back to start of events
    while stream.tell() < file_size:
        event_type: type[AnyEvent] | None = None
        id = EventEnum(int.from_bytes(stream.read(1), "little"))

        if id < WORD:
            value = stream.read(1)
        elif id < DWORD:
            value = stream.read(2)
        elif id < TEXT:
            if id == _AC_ID and ac_layout == _AC_VARINT_LENGTH:
                size = c.VarInt.parse_stream(stream)
                value = stream.read(size)
            elif id == _AC_ID and ac_layout == _AC_VARINT_VALUE:
                # Like the FL26 layout this isn't 4 bytes, so the event is
                # dropped by the size check below; the stream stays aligned.
                start = stream.tell()
                c.VarInt.parse_stream(stream)
                value = stream.getvalue()[start : stream.tell()]
            else:
                value = stream.read(4)
        else:
            size = c.VarInt.parse_stream(stream)
            value = stream.read(size)

        if id == ProjectID.FLVersion:
            parts = value.decode("ascii").rstrip("\0").split(".")
            if [int(part) for part in parts][0:2] >= [11, 5]:
                str_type = UnicodeEvent
            else:
                str_type = AsciiEvent
            try:
                fl_version = int(parts[0])
            except (ValueError, IndexError):
                fl_version = None

        for enum_ in EventEnum.__subclasses__():
            if id in enum_:
                event_type = getattr(enum_(id), "type")
                break

        if event_type is None:
            if id < WORD:
                event_type = U8Event
            elif id < DWORD:
                event_type = U16Event
            elif id < TEXT:
                event_type = U32Event
            elif id < DATA or id.value in NEW_TEXT_IDS:
                if str_type is None:  # pragma: no cover
                    raise VersionNotDetected  # ! This should never happen
                event_type = str_type

                if id == PluginID.InternalName:
                    try:
                        plug_name = event_type(id, value).value
                    except Exception:
                        plug_name = None
            elif id == PluginID.Data and plug_name is not None:
                event_type = get_event_by_internal_name(plug_name)
            else:
                event_type = UnknownDataEvent

        try:
            if event_type is PlaylistEvent:
                # FLVersion is the first event, so fl_version is known here;
                # pass it so 80B (FL 2024/25) vs 88B (FL26) records are picked
                # by version instead of payload divisibility (they share
                # multiples: 17600 = 220*80 = 200*88).
                events.append(event_type(id, value, version=fl_version))
            else:
                events.append(event_type(id, value))
        except Exception:
            # FL24+/26 can carry events PyFLP misinterprets as text; degrade
            # gracefully instead of aborting the whole parse.
            try:
                events.append(UnknownDataEvent(id, value))
            except Exception:
                pass

    return Project(
        EventTree(init=(IndexedEvent(r, e) for r, e in enumerate(events))),
        channel_count=channel_count,
        format=file_format,
        ppq=ppq,
    )


def save(project: Project, file: pathlib.Path | str) -> None:
    """Save a parsed project back into a file.

    Caution:
        Always have a backup ready, just in case 😉

    Args:
        project: The object returned by :meth:`parse`.
        file: The file in which the contents of :attr:`project` are serialised back.
    """
    buf = bytearray()
    num_channels = len(project.channels)
    header = FLP_HEADER.pack(b"FLhd", 6, project.format, num_channels, project.ppq)
    buf.extend(header)
    buf.extend(b"FLdt" + (b"\0" * 4))
    total_size = 0
    for event in project.events:
        raw = bytes(event)
        total_size += len(raw)
        buf.extend(raw)
    buf[18:22] = total_size.to_bytes(4, "little")

    with open(file, "wb") as fp:
        fp.write(buf)
