"""Resolve a per-recording export plan without inventing a fixed preview size."""
from dataclasses import asdict, dataclass
from fractions import Fraction
import math
import re


COLORS = ("color_primaries", "color_transfer", "color_space", "color_range")
UNKNOWN = (None, "", "unknown", "unspecified", "reserved", "N/A")


def positive_int(value):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (ValueError, TypeError):
        return None


def frame_rate(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = Fraction(str(stream.get(key, "0/0")))
            if rate > 0:
                return str(rate)
        except (ValueError, ZeroDivisionError):
            pass
    return None


def bit_depth(stream):
    explicit = positive_int(stream.get("bits_per_raw_sample")) or positive_int(stream.get("bits_per_sample"))
    if explicit:
        return explicit
    pixel = stream.get("pix_fmt", "")
    match = re.search(r"(?:p|gray)(9|10|12|14|16)(?:le|be)?$", pixel)
    if not match:
        match = re.match(r"p0(10|12|16)(?:le|be)$", pixel)
    if match:
        return int(match[1])
    if pixel in ("rgb24", "bgr24", "rgba", "bgra", "gray", "gray8", "nv12", "nv21") or re.fullmatch(
            r"yuvj?(?:420|422|444|440|411|410)p", pixel):
        return 8
    return None


def chroma(stream):
    pixel = stream.get("pix_fmt", "")
    for sampling in ("420", "422", "444", "440", "411", "410"):
        if sampling in pixel:
            return sampling
    if pixel in ("nv12", "nv21") or re.match(r"p0(?:10|12|16)(?:le|be)$", pixel):
        return "420"
    if pixel.startswith(("rgb", "bgr", "gbr")):
        return "444"
    return None


def codec_name(value):
    return {"hevc": "h265", "h265": "h265", "h264": "h264", "mjpeg": "jpeg", "jpeg": "jpeg"}.get(value, value)


def duration(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def hdr_metadata(stream):
    return tuple(item for item in stream.get("side_data_list", []) if any(
        name in item.get("side_data_type", "").lower()
        for name in ("mastering display", "content light", "hdr", "dovi")))


def main_streams(probe):
    streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"
               and not s.get("disposition", {}).get("attached_pic", 0)]
    streams = [s for s in streams if positive_int(s.get("width")) and positive_int(s.get("height"))]
    if not streams:
        raise ValueError("Source has no readable main image/video stream")
    largest = max(s["width"] * s["height"] for s in streams)
    return [s for s in streams if s["width"] * s["height"] == largest]


def summarize(probe):
    return {"streams": [{
        "index": s.get("index"), "width": s["width"], "height": s["height"],
        "codec": codec_name(s.get("codec_name")), "fps": frame_rate(s),
        "bitrate": positive_int(s.get("bit_rate")), "pix_fmt": s.get("pix_fmt"),
        "bit_depth": bit_depth(s), "chroma": chroma(s),
        "color": {key: s[key] for key in COLORS if s.get(key) not in UNKNOWN},
        "hdr_metadata": hdr_metadata(s),
    } for s in main_streams(probe)], "duration": probe.get("format", {}).get("duration"),
        "container_bitrate": positive_int(probe.get("format", {}).get("bit_rate"))}


@dataclass(frozen=True)
class ExportPlan:
    width: int
    height: int
    codec: str
    bitrate: int
    fps: str | None
    bit_depth: int | None
    color: dict
    chroma: str | None
    geometry: str
    bitrate_basis: str
    duration: float | None = None
    audio_required: bool = False
    hdr_metadata: tuple = ()

    def record(self):
        return asdict(self)


def build_plan(cfg, probes, is_video=True):
    groups = [main_streams(probe) for probe in probes]
    streams = [s for group in groups for s in group]
    if len({(s["width"], s["height"]) for s in streams}) > 1:
        raise ValueError("Source lens dimensions differ")
    if cfg.width and cfg.height:
        width, height, geometry = cfg.width, cfg.height, "explicit_size"
    elif len(streams) == 1 and streams[0]["width"] == 2 * streams[0]["height"]:
        width, height, geometry = streams[0]["width"], streams[0]["height"], "source_2_to_1"
    elif (is_video and len(streams) == 2 and all(s["width"] == s["height"] for s in streams)
          and streams[0]["width"] == streams[1]["width"]):
        width, height, geometry = streams[0]["width"] * 2, streams[0]["height"], "dual_square_lenses"
    else:
        raise ValueError("Cannot infer a source-preserving 2:1 size from this lens layout; configure width/height explicitly")
    if width % 2 or height % 2 or width != 2 * height:
        raise ValueError("Resolved source size must be even and 2:1")

    known_rates = [frame_rate(s) for s in streams if frame_rate(s)] if is_video else []
    if known_rates and any(abs(float(Fraction(rate) - Fraction(known_rates[0]))) > 0.001 for rate in known_rates):
        raise ValueError("Source lens frame rates differ")
    known_durations = [duration(s.get("duration")) or duration(probe.get("format", {}).get("duration"))
                       for probe, group in zip(probes, groups) for s in group] if is_video else []
    known_durations = [value for value in known_durations if value is not None]
    fps = float(Fraction(known_rates[0])) if known_rates else 25
    if known_durations and max(known_durations) - min(known_durations) > max(2 / fps, max(known_durations) * 0.01):
        raise ValueError("Source lens durations differ")
    depths = {bit_depth(s) for s in streams if bit_depth(s) is not None}
    if len(depths) > 1:
        raise ValueError("Source lens bit depths differ")
    depth = next(iter(depths), None)
    color = {}
    for key in COLORS:
        values = {s[key] for s in streams if s.get(key) not in UNKNOWN}
        if len(values) > 1:
            raise ValueError(f"Source lens color descriptions differ: {key}")
        if values:
            color[key] = values.pop()
    samplings = {chroma(s) for s in streams if chroma(s) is not None}
    if len(samplings) > 1:
        raise ValueError("Source lens chroma sampling differs")

    target_codec = "jpeg"
    if is_video:
        codecs = {codec_name(s.get("codec_name")) for s in streams}
        if cfg.codec == "source":
            if len(codecs) != 1 or not codecs <= {"h264", "h265"}:
                raise ValueError("Cannot preserve the source codec; configure a supported codec explicitly")
            target_codec = codecs.pop()
        else:
            target_codec = cfg.codec
        if depth and depth > 8:
            if depth > 10:
                raise ValueError("SDK 10-bit export cannot preserve a source deeper than 10 bits")
            if cfg.codec == "h264":
                raise ValueError("H.264 SDK export cannot preserve 10-bit source; use source or h265")
            target_codec = "h265"

    bitrate, basis = 0, "image_sdk_default"
    if is_video:
        bitrate, basis = cfg.bitrate, "explicit_bitrate"
        if not bitrate:
            values, fallback = [], False
            for probe, group in zip(probes, groups):
                rates = [positive_int(s.get("bit_rate")) for s in group]
                if all(rates):
                    values.append(sum(rates))
                else:
                    values.append(positive_int(probe.get("format", {}).get("bit_rate")))
                    fallback = True
            if all(values):
                bitrate = sum(values)
                basis = "source_container_estimate" if fallback else "source_video_streams"
            else:
                bitrate, basis = 0, "sdk_source_bitrate"
    hdr = [hdr_metadata(s) for s in streams if hdr_metadata(s)]
    if hdr and any(value != hdr[0] for value in hdr):
        raise ValueError("Source lens HDR metadata differs")
    return ExportPlan(width, height, target_codec, bitrate, known_rates[0] if known_rates else None,
                      depth, color, next(iter(samplings), None), geometry, basis,
                      known_durations[0] if known_durations else None,
                      is_video and any(s.get("codec_type") == "audio" for probe in probes for s in probe.get("streams", [])),
                      hdr[0] if hdr else ())


def validate_output(plan, probe, is_video=True):
    video = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"
             and not s.get("disposition", {}).get("attached_pic", 0)]
    if len(video) != 1:
        raise RuntimeError("Output must contain exactly one stitched main stream")
    stream = video[0]
    if (stream.get("width"), stream.get("height")) != (plan.width, plan.height):
        raise RuntimeError("Output resolution differs from the export plan")
    if codec_name(stream.get("codec_name")) != plan.codec:
        raise RuntimeError("Output codec differs from the export plan")
    actual_depth = bit_depth(stream)
    if plan.bit_depth and (actual_depth is None or actual_depth < plan.bit_depth):
        raise RuntimeError("SDK output reduced or omitted the source bit depth")
    if plan.chroma:
        factors = {"410": (4, 2), "411": (4, 1), "420": (2, 2), "440": (1, 2), "422": (2, 1), "444": (1, 1)}
        actual_chroma = chroma(stream)
        if actual_chroma not in factors or any(
                actual > expected for actual, expected in zip(factors[actual_chroma], factors[plan.chroma])):
            raise RuntimeError("SDK output reduced or omitted source chroma sampling")
    for key, value in plan.color.items():
        if stream.get(key) != value:
            raise RuntimeError(f"SDK output changed or omitted source color metadata: {key}")
    if plan.hdr_metadata and hdr_metadata(stream) != plan.hdr_metadata:
        raise RuntimeError("SDK output changed or omitted source HDR metadata")
    if is_video:
        try:
            duration = float(probe.get("format", {}).get("duration", 0))
        except (ValueError, TypeError):
            duration = 0
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("Output video has no positive duration")
        actual_fps = frame_rate(stream)
        if plan.fps and (not actual_fps or abs(float(Fraction(actual_fps) - Fraction(plan.fps))) > 0.001):
            raise RuntimeError("SDK output changed or omitted the source frame rate")
        if plan.duration and abs(duration - plan.duration) > max(2 / float(Fraction(plan.fps or "25")), plan.duration * 0.01):
            raise RuntimeError("SDK output duration differs from the source recording")
        if plan.audio_required and not any(s.get("codec_type") == "audio" for s in probe.get("streams", [])):
            raise RuntimeError("SDK output discarded a source audio stream")
