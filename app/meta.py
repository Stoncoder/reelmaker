"""Metadata removal and independent verification for every video the app writes."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .config import tool

EXIFTOOL = shutil.which("exiftool") or "/opt/homebrew/bin/exiftool"

# Structural fields every MP4 needs to play. Anything else is treated as metadata and must not be there.
STRUCTURAL = {
    "MajorBrand", "MinorVersion", "CompatibleBrands", "MediaDataSize", "MediaDataOffset", "MovieHeaderVersion",
    "TimeScale", "Duration", "PreferredRate", "PreferredVolume", "PreviewTime", "PreviewDuration", "PosterTime",
    "SelectionTime", "SelectionDuration", "CurrentTime", "NextTrackID", "TrackHeaderVersion", "TrackID",
    "TrackDuration", "TrackLayer", "TrackVolume", "ImageWidth", "ImageHeight", "GraphicsMode", "OpColor",
    "CompressorID", "SourceImageWidth", "SourceImageHeight", "XResolution", "YResolution", "BitDepth",
    "VideoFrameRate", "MatrixStructure", "MediaHeaderVersion", "MediaTimeScale", "MediaDuration",
    "HandlerType", "HandlerDescription", "Balance", "AudioFormat", "AudioChannels", "AudioBitsPerSample",
    "AudioSampleRate", "PixelAspectRatio", "ColorRepresentation", "ColorPrimaries", "TransferCharacteristics",
    "MatrixCoefficients", "VideoFullRangeFlag", "AvgBitrate", "Rotation", "ImageSize", "Megapixels",
    "MediaLanguageCode", "CleanApertureDimensions", "ProductionApertureDimensions", "EncodedPixelsDimensions",
    "TrackCreateDate", "TrackModifyDate", "MediaCreateDate", "MediaModifyDate", "CreateDate", "ModifyDate",
    "Encoder",  # checked separately below: must be empty
}
ZERO_DATES = {"0000:00:00 00:00:00", "", None}
HANDLER_OK = {"VideoHandler", "SoundHandler", "Video Track", "Sound Track", ""}


def _run(cmd):
    from .system import NO_WINDOW
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    except FileNotFoundError:   # the tool isn't installed on this computer
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")


def blank_user_data(path: Path) -> int:
    """Overwrite every 'udta' box (user data: iTunes-style tag lists, the empty 'mdir/appl' container, etc.)
    with an equally sized zero-filled 'free' box. Same size, so the player's offsets stay valid."""
    import struct
    data = bytearray(path.read_bytes())
    count = 0

    def walk(start, end, inside):
        nonlocal count
        i = start
        while i + 8 <= end:
            size, typ = struct.unpack(">I4s", data[i:i + 8])
            hdr = 8
            if size == 1:
                size = struct.unpack(">Q", data[i + 8:i + 16])[0]; hdr = 16
            elif size == 0:
                size = end - i
            if size < hdr or i + size > end:
                break
            if typ == b"udta" or (typ == b"meta" and inside == b"moov"):
                data[i + 4:i + 8] = b"free"
                data[i + hdr:i + size] = bytes(size - hdr)
                count += 1
            elif typ in (b"moov", b"trak", b"mdia", b"minf"):
                walk(i + hdr, i + size, typ)
            i += size

    walk(0, len(data), b"")
    if count:
        path.write_bytes(bytes(data))
    return count


def clean(src: Path, dst: Path | None = None) -> Path:
    """Strip all metadata from a video. Re-muxes (no quality loss). Writes to dst, or replaces src."""
    out = dst or src.with_name(src.stem + ".clean-tmp.mp4")
    probe = json.loads(_run([tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams", str(src)]).stdout or "{}")
    vcodec = next((s.get("codec_name") for s in probe.get("streams", []) if s.get("codec_type") == "video"), "")
    # video is copied untouched; audio is re-encoded only if an encoder hid its name inside the AAC data
    hidden = b"Lavc" in src.read_bytes()
    audio = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"] if hidden else ["-c:a", "copy"]
    cmd = [tool("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "copy",
           *audio,
           "-map_metadata", "-1", "-map_metadata:s:v", "-1", "-map_metadata:s:a", "-1", "-map_chapters", "-1",
           "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
           "-metadata", "creation_time=", "-metadata:s:v", "encoder=", "-metadata:s:a", "encoder=",
           "-metadata:s:v", "handler_name=VideoHandler", "-metadata:s:a", "handler_name=SoundHandler"]
    if vcodec == "h264":
        cmd += ["-bsf:v", "filter_units=remove_types=6"]   # x264's settings/version block
    elif vcodec == "hevc":
        cmd += ["-bsf:v", "filter_units=remove_types=39|40"]
    cmd += ["-movflags", "+faststart", str(out)]
    r = _run(cmd)
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError("Metadata clean failed: " + r.stderr[-500:])
    # second pass: anything exiftool can still write-delete (XMP, user data, etc.)
    if Path(EXIFTOOL).exists():
        _run([EXIFTOOL, "-q", "-q", "-all=", "-overwrite_original", str(out)])
    # ffmpeg always writes an (empty) Apple-style tag container; blank it out
    blank_user_data(out)
    # macOS file attributes (e.g. "downloaded from" URLs, quarantine); nothing to do on Windows
    from .system import clear_file_attributes
    clear_file_attributes(out)
    if dst is None:
        out.replace(src)
        clear_file_attributes(src)
        return src
    return out


import re

# What counts as metadata: anything that describes who/what/where/when/with-what, rather than how to play the file.
META_NAME = re.compile(r"(title|artist|author|album|genre|comment|description|copyright|keyword|software|encoder|"
                       r"encodedby|compressorname|make|model|gps|location|creator|producer|director|publisher|"
                       r"rating|lyrics|composer|show|episode|category|account|purchase|owner|user|url|"
                       r"camera|lens|serial|device|firmware|contentidentifier|com\.apple)", re.I)
META_GROUPS = ("XMP", "ItemList", "Keys", "UserData", "Microsoft", "Nikon", "Canon", "Sony", "Apple", "GPS")


def check(path: Path) -> dict:
    """Independent check with exiftool. Returns {ok, leftover: {tag: value}, read_by}."""
    leftover = {}
    read_by = "ffprobe"
    if Path(EXIFTOOL).exists():
        r = _run([EXIFTOOL, "-j", "-a", "-G1", "-s", str(path)])
        data = (json.loads(r.stdout or "[{}]") or [{}])[0]
        for k, v in data.items():
            grp, _, name = k.partition(":")
            if not name or grp in ("ExifTool", "System", "File", "Composite"):
                continue
            if name.endswith("Date"):
                if v not in ZERO_DATES and not str(v).startswith("0000"):
                    leftover[k] = v          # a real recording/creation date
                continue
            if name in ("HandlerDescription",) and str(v) in HANDLER_OK:
                continue
            if grp.startswith(META_GROUPS) or META_NAME.search(name):
                if str(v).strip() not in ("", "0"):
                    leftover[k] = v
        read_by = "exiftool " + _run([EXIFTOOL, "-ver"]).stdout.strip()
    # encoder fingerprints anywhere in the file (also inside the audio/video data)
    data = path.read_bytes()
    for sig in (b"x264 - core", b"Lavf", b"Lavc", b"HandBrake", b"Adobe Premiere", b"CapCut", b"com.apple.quicktime",
                b"mdirappl"):
        if sig in data:
            leftover[f"hidden text: {sig.decode()}"] = "found inside the file"
    from .system import file_attributes
    xa = file_attributes(path)
    if xa:  # com.apple.provenance is a macOS-local security tag; it's never part of the uploaded file
        leftover["macOS file attributes"] = ", ".join(xa)
    return {"ok": not leftover, "leftover": leftover, "read_by": read_by}
