import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .metadata import MetadataSDK


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
    def __init__(self, cfg, runner, audit):
        self.cfg, self.runner, self.audit = cfg, runner, audit
        self.metadata_sdk = MetadataSDK(cfg, runner, audit)

    def source_metadata(self, path, token):
        root = self.cfg.data_root / "metadata"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".insta360-metadata-", dir=root) as directory:
            temporary = Path(directory)
            sdk = self.metadata_sdk.extract(path, temporary)
            standard = self.metadata(path)
            target = root / f"{token}.sdk-{uuid.uuid4().hex}"
            for file in temporary.iterdir():
                with file.open("rb") as stream:
                    os.fsync(stream.fileno())
            self.audit.emit("file.publish_sdk_snapshot", source=temporary, destination=target)
            os.rename(temporary, target)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        sdk["snapshot_directory"] = str(target)
        standard["Insta360SDK"] = sdk
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

    def convert(self, job, output):
        cfg = self.cfg
        argv = [cfg.executable, "-inputs", *job.sources, "-output", output,
                "-output_size", f"{cfg.width}x{cfg.height}", "-stitch_type", cfg.stitch_type,
                "-model_root_dir", cfg.models.rstrip("/") + "/",
                "-camera_accessory_type", str(cfg.camera_accessory_type)]
        if cfg.flowstate:
            argv.append("-enable_flowstate")
        if output.suffix == ".mp4":
            argv.extend(["-bitrate", str(cfg.bitrate)])
            if cfg.codec == "h265":
                argv.append("-enable_h265_encoder")
        if cfg.mode == "cpu":
            argv.extend(["-disable_cuda", "-image_processing_accel", "cpu"])
            if output.suffix == ".mp4":
                argv.extend(["-enable_soft_encode", "-enable_soft_decode"])
        argv.extend(cfg.extra_args)
        env = os.environ.copy()
        libraries = cfg.library_dirs + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else [])
        env["LD_LIBRARY_PATH"] = ":".join(libraries)
        # The SDK installation is mounted read-only. Relative auxiliary output
        # must stay in this job's writable, audited temporary directory.
        self.runner.run(argv, cwd=output.parent, env=env)
        # Some official demo releases return 0 even after an SDK error callback.
        if not output.is_file() or output.is_symlink() or output.stat().st_size == 0:
            raise RuntimeError("SDK returned without a nonempty output file; inspect command.output logs")

    def validate(self, path, source_probe=None):
        info = self.probe(path)
        video = [stream for stream in info.get("streams", []) if stream["codec_type"] == "video"]
        if len(video) != 1:
            raise RuntimeError("Preview must contain exactly one stitched video/image stream")
        stream = video[0]
        if (stream["width"], stream["height"]) != (self.cfg.width, self.cfg.height):
            raise RuntimeError(f"SDK output resolution does not match configured 2:1 size: {stream}")
        if path.suffix == ".mp4":
            duration = float(info.get("format", {}).get("duration", 0))
            if duration <= 0:
                raise RuntimeError("Preview video has no positive duration")
            if source_probe:
                original = float(source_probe.get("format", {}).get("duration", 0))
                if original and abs(duration - original) > max(2, original * 0.02):
                    raise RuntimeError(f"Video duration mismatch: source={original}s, preview={duration}s")
                audio = any(s["codec_type"] == "audio" for s in source_probe.get("streams", []))
                if audio and not any(s["codec_type"] == "audio" for s in info.get("streams", [])):
                    raise RuntimeError("SDK discarded the source audio stream")
        return info

    def preserve_metadata(self, source, output, original):
        cfg = self.cfg
        argv = [cfg.exiftool, "-overwrite_original", "-TagsFromFile", source, "-XMP:all"]
        if output.suffix == ".jpg":
            argv.extend(["-EXIF:all", "-IPTC:all"])
        else:
            argv.extend(["-QuickTime:CreateDate", "-QuickTime:ModifyDate",
                         "-Keys:CreationDate", "-Keys:GPSCoordinates", "-Keys:Make", "-Keys:Model"])
        self.runner.run([*argv, output])
        fields = capture_fields(original)
        assignments = [f"-XMP-tiff:ImageWidth={cfg.width}", f"-XMP-tiff:ImageHeight={cfg.height}",
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
            assignments.extend(["-IFD0:Orientation=1", f"-ExifIFD:ExifImageWidth={cfg.width}",
                                f"-ExifIFD:ExifImageHeight={cfg.height}",
                                "-XMP-GPano:UsePanoramaViewer=True"])
        else:
            assignments.extend(["-XMP-GSpherical:Spherical=True", "-XMP-GSpherical:Stitched=True",
                                "-XMP-GSpherical:StereoMode=mono",
                                "-XMP-GSpherical:StitchingSoftware=Insta360 MediaSDK / OpenConverter"])
        assignments.extend([f"-{group}:ProjectionType=equirectangular",
                            f"-{group}:FullPanoWidthPixels={cfg.width}",
                            f"-{group}:FullPanoHeightPixels={cfg.height}",
                            f"-{group}:CroppedAreaImageWidthPixels={cfg.width}",
                            f"-{group}:CroppedAreaImageHeightPixels={cfg.height}",
                            f"-{group}:CroppedAreaLeftPixels=0", f"-{group}:CroppedAreaTopPixels=0"])
        self.runner.run([cfg.exiftool, "-overwrite_original", *assignments, output])
        actual = self.metadata(output)
        if actual.get(f"{group}:ProjectionType") != "equirectangular":
            raise RuntimeError("360° projection metadata was not written")
        boolean = "UsePanoramaViewer" if output.suffix == ".jpg" else "Spherical"
        if str(actual.get(f"{group}:{boolean}")).lower() not in ("true", "1"):
            raise RuntimeError("360° viewer metadata was not written")
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
