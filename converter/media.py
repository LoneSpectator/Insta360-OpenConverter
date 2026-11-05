import json
from fractions import Fraction
import os
import re

from .metadata import MetadataSDK
from .quality import build_plan, validate_output
from .scan import digest, snapshot
from .sidecar import MetadataSidecars
from .workspace import temporary_workspace


def pick(metadata, *names):
    for name in names:
        value = metadata.get(name)
        if value not in (None, "", "0000:00:00 00:00:00", "1904:01:01 00:00:00"):
            return value
    return None


def capture_fields(metadata):
    date = pick(metadata, "ExifIFD:DateTimeOriginal", "XMP-exif:DateTimeOriginal",
                "Keys:CreationDate", "XMP-xmp:CreateDate", "QuickTime:CreateDate")
    offset = pick(metadata, "ExifIFD:OffsetTimeOriginal")
    if date and offset and not re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", str(date)):
        date = str(date) + str(offset)
    fields = {
        "DateTimeOriginal": date,
        "Make": pick(metadata, "IFD0:Make", "Keys:Make", "XMP-tiff:Make", "Insta360:Make"),
        "Model": pick(metadata, "IFD0:Model", "Keys:Model", "XMP-tiff:Model", "Insta360:Model"),
        "GPSLatitude": pick(metadata, "Composite:GPSLatitude", "XMP-exif:GPSLatitude", "GPS:GPSLatitude"),
        "GPSLongitude": pick(metadata, "Composite:GPSLongitude", "XMP-exif:GPSLongitude", "GPS:GPSLongitude"),
        "SerialNumber": pick(metadata, "XMP-aux:SerialNumber", "ExifIFD:SerialNumber"),
        "Firmware": pick(metadata, "XMP-aux:Firmware"),
    }
    sdk = metadata.get("Insta360SDK", {})
    if sdk.get("camera_type"):
        fields.update(Make="Insta360", Model=sdk["camera_type"])
    if sdk.get("serial_number"):
        fields["SerialNumber"] = sdk["serial_number"]
    if sdk.get("firmware_version"):
        fields["Firmware"] = sdk["firmware_version"]
    gps = sdk.get("first_valid_gps")
    if gps:
        fields.update(GPSLatitude=gps["latitude"], GPSLongitude=gps["longitude"])
    # GetFirstTimeStamp is a gyro origin, never a date or a UTC epoch.
    return fields


def canonical_time(value):
    value = str(value).replace("T", " ")
    return value[:10].replace("-", ":") + value[10:]


class Media:
    def __init__(self, cfg, runner, audit, state=None):
        self.cfg, self.runner, self.audit = cfg, runner, audit
        self.metadata_sdk = MetadataSDK(cfg, runner, audit)
        self.sdk_help = None
        self.sidecars = MetadataSidecars(state, audit, runner.stop) if state is not None else None

    def source_metadata(self, path, before=None, source_sha256=None):
        if self.sidecars is None:
            raise RuntimeError("Source metadata publication requires the artifact registry")
        before = before or snapshot(path)
        if snapshot(path) != before:
            raise RuntimeError("Source changed before metadata extraction")
        source_sha256 = source_sha256 or digest(path, self.audit)
        with temporary_workspace(path.parent, self.audit, kind="metadata-read") as temporary:
            sdk = self.metadata_sdk.extract(path, temporary)
            standard = self.metadata(path)
            standard["Insta360SDK"] = sdk
            target = self.sidecars.save(path, standard, temporary, before, source_sha256)
        sdk["snapshot_file"] = str(target)
        for name, track in sdk["tracks"].items():
            track.pop("file", None)
            track["json_pointer"] = f"/Insta360SDK/tracks/{name}/records"
        return standard

    def metadata(self, path):
        result = json.loads(self.runner.run([
            self.cfg.exiftool, "-j", "-G1", "-a", "-s", "-n", "-u", path], capture=True))
        if len(result) != 1 or result[0].get("ExifTool:Error"):
            raise RuntimeError(f"Cannot read metadata for {path}: {result}")
        return result[0]

    def probe(self, path):
        return json.loads(self.runner.run([
            self.cfg.ffprobe, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", path], capture=True))

    def convert(self, job, output, plan=None):
        cfg = self.cfg
        if plan is None:
            plan = build_plan(cfg, [self.probe(path) for path in job.sources], output.suffix == ".mp4")
        argv = [cfg.executable, "-inputs", *job.sources, "-output", output,
                "-output_size", f"{plan.width}x{plan.height}", "-stitch_type", cfg.stitch_type,
                "-model_root_dir", cfg.models.rstrip("/") + "/",
                "-camera_accessory_type", str(cfg.camera_accessory_type)]
        if cfg.flowstate:
            argv.append("-enable_flowstate")
        if output.suffix == ".mp4":
            argv.extend(["-bitrate", str(plan.bitrate)])
            if plan.codec == "h265":
                argv.append("-enable_h265_encoder")
            if plan.bit_depth and plan.bit_depth > 8:
                if self.sdk_help is None:
                    self.sdk_help = self.runner.run([cfg.executable, "-help"], cwd=cfg.data_root,
                        env=self.sdk_environment(), capture=True, accepted_returncodes=(0, 1, 255))
                if "-enable_10bit" not in self.sdk_help:
                    raise RuntimeError("This MediaSDK CLI cannot confirm 10-bit export; refusing to reduce bit depth")
                argv.append("-enable_10bit")
        argv.extend(cfg.extra_args)
        self.runner.run(argv, cwd=output.parent, env=self.sdk_environment(output.parent))
        # Some official demo releases return 0 even after an SDK error callback.
        if not output.is_file() or output.is_symlink() or output.stat().st_size == 0:
            raise RuntimeError("SDK returned without a nonempty output file; inspect command.output logs")

    def sdk_environment(self, work_root=None):
        env = os.environ.copy()
        libraries = self.cfg.library_dirs + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else [])
        env["LD_LIBRARY_PATH"] = ":".join(libraries)
        if work_root is not None:
            for name in ("TMPDIR", "TMP", "TEMP"):
                env[name] = str(work_root)
        return env

    def validate(self, path, source_probe=None, plan=None):
        info = self.probe(path)
        if plan is None:
            if source_probe is None:
                raise ValueError("Output validation requires the source export plan")
            plan = build_plan(self.cfg, [source_probe], path.suffix == ".mp4")
        validate_output(plan, info, path.suffix == ".mp4")
        if path.suffix == ".mp4":
            duration = float(info.get("format", {}).get("duration", 0))
            if source_probe:
                original = float(source_probe.get("format", {}).get("duration", 0))
                fps = float(Fraction(plan.fps)) if plan.fps else 25
                if original and abs(duration - original) > max(2 / fps, original * 0.01):
                    raise RuntimeError(f"Video duration mismatch: source={original}s, preview={duration}s")
                audio = any(s["codec_type"] == "audio" for s in source_probe.get("streams", []))
                if audio and not any(s["codec_type"] == "audio" for s in info.get("streams", [])):
                    raise RuntimeError("SDK discarded the source audio stream")
        return info

    def preserve_metadata(self, source, output, original, plan):
        cfg = self.cfg
        argv = [cfg.exiftool, "-overwrite_original", "-TagsFromFile", source, "-XMP:all"]
        if output.suffix == ".jpg":
            argv.extend(["-EXIF:all", "-IPTC:all", "-ICC_Profile"])
        else:
            argv.extend(["-QuickTime:CreateDate", "-QuickTime:ModifyDate",
                         "-Keys:CreationDate", "-Keys:GPSCoordinates", "-Keys:Make", "-Keys:Model"])
        self.runner.run([*argv, output])
        fields = capture_fields(original)
        assignments = [f"-XMP-tiff:ImageWidth={plan.width}", f"-XMP-tiff:ImageHeight={plan.height}",
                       "-XMP-tiff:Orientation=1"]
        for key, group in [("DateTimeOriginal", "XMP-exif"), ("Make", "XMP-tiff"),
                           ("Model", "XMP-tiff"), ("GPSLatitude", "XMP-exif"),
                           ("GPSLongitude", "XMP-exif"), ("SerialNumber", "XMP-aux"),
                           ("Firmware", "XMP-aux")]:
            if fields[key] is not None:
                assignments.append(f"-{group}:{key}={fields[key]}")
        if fields["DateTimeOriginal"]:
            assignments.append(f"-XMP-xmp:CreateDate={fields['DateTimeOriginal']}")
        else:
            self.audit.emit("metadata.capture_time_missing", source=source,
                            message="No readable capture time; not inferred from directory or mtime")
        group = "XMP-GPano" if output.suffix == ".jpg" else "XMP-GSpherical"
        if output.suffix == ".jpg":
            assignments.extend(["-IFD0:Orientation=1", f"-ExifIFD:ExifImageWidth={plan.width}",
                                f"-ExifIFD:ExifImageHeight={plan.height}",
                                f"-IFD0:ImageWidth={plan.width}", f"-IFD0:ImageHeight={plan.height}",
                                f"-XMP-exif:ExifImageWidth={plan.width}", f"-XMP-exif:ExifImageHeight={plan.height}",
                                "-XMP-GPano:UsePanoramaViewer=True"])
        else:
            assignments.extend(["-XMP-GSpherical:Spherical=True", "-XMP-GSpherical:Stitched=True",
                                "-XMP-GSpherical:StereoMode=mono",
                                "-XMP-GSpherical:StitchingSoftware=Insta360 MediaSDK / OpenConverter"])
        assignments.extend([f"-{group}:ProjectionType=equirectangular",
                            f"-{group}:FullPanoWidthPixels={plan.width}",
                            f"-{group}:FullPanoHeightPixels={plan.height}",
                            f"-{group}:CroppedAreaImageWidthPixels={plan.width}",
                            f"-{group}:CroppedAreaImageHeightPixels={plan.height}",
                            f"-{group}:CroppedAreaLeftPixels=0", f"-{group}:CroppedAreaTopPixels=0"])
        self.runner.run([cfg.exiftool, "-overwrite_original", *assignments, output])
        actual = self.metadata(output)
        if actual.get(f"{group}:ProjectionType") != "equirectangular":
            raise RuntimeError("360° projection metadata was not written")
        boolean = "UsePanoramaViewer" if output.suffix == ".jpg" else "Spherical"
        if str(actual.get(f"{group}:{boolean}")).lower() not in ("true", "1"):
            raise RuntimeError("360° viewer metadata was not written")
        if output.suffix == ".jpg":
            for tag, expected in [("IFD0:ImageWidth", plan.width), ("IFD0:ImageHeight", plan.height),
                                  ("ExifIFD:ExifImageWidth", plan.width), ("ExifIFD:ExifImageHeight", plan.height)]:
                if actual.get(tag) != expected:
                    raise RuntimeError(f"Preview image metadata size mismatch: {tag}")
        for key, expected in fields.items():
            if expected is None:
                continue
            group_name = ("XMP-tiff" if key in ("Make", "Model") else
                          "XMP-aux" if key in ("SerialNumber", "Firmware") else "XMP-exif")
            value = actual.get(f"{group_name}:{key}")
            if key.startswith("GPS"):
                same = value is not None and abs(float(value) - float(expected)) < 1e-6
            elif key == "DateTimeOriginal":
                same = value is not None and canonical_time(value) == canonical_time(expected)
            else:
                same = value == expected
            if not same:
                raise RuntimeError(f"Metadata verification failed: {key}: {expected!r} -> {value!r}")
        self.audit.emit("metadata.verified", path=output, capture_fields=fields, projection="equirectangular")
        return fields
