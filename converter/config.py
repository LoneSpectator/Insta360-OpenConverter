from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re

try:
    import tomllib
except ImportError:  # Ubuntu 22.04 / Python 3.10
    import tomli as tomllib


@dataclass
class Config:
    photo_root: Path = Path("/photo")
    data_root: Path = Path("/data")
    scan_interval_minutes: int = 60
    max_concurrent_jobs: int = 3
    settle_seconds: int = 120
    command_timeout: int = 7200
    width: int = 0
    height: int = 0
    bitrate: int = 0
    codec: str = "source"
    flowstate: bool = False
    photo_flowstate: bool = True
    stitch_type: str = "optflow"
    camera_accessory_type: int = 0
    executable: str = "/data/sdk/media/bin/MediaSDKTest"
    models: str = "/data/sdk/media/bin/models"
    library_dirs: list[str] = field(default_factory=lambda: [
        "/data/sdk/media/lib", "/data/sdk/media/bin"])
    source_root: Path = Path("/sdk")
    extra_args: list[str] = field(default_factory=list)
    exiftool: str = "exiftool"
    ffprobe: str = "ffprobe"
    metadata_reader: str = "/data/sdk/metadata/bin/insta360-metadata"
    metadata_library_dirs: list[str] = field(default_factory=lambda: ["/data/sdk/metadata/lib"])

    @classmethod
    def load(cls, path: Path, data_root: Path):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if "scan_interval" in raw:
            if "scan_interval_minutes" in raw:
                raise ValueError("Configure scan_interval_minutes only; scan_interval is its legacy alias")
            # Scan intervals are now expressed in minutes, including the old
            # key, so an existing default of 60 becomes the hourly schedule.
            raw["scan_interval_minutes"] = raw.pop("scan_interval")
        # Failed files are retried by every scan; the former per-file delay
        # remains accepted in old configuration files but has no effect.
        raw.pop("retry_seconds", None)
        sdk = raw.pop("sdk", {})
        tools = raw.pop("tools", {})
        metadata = raw.pop("metadata", {})
        top_keys = set(cls.__dataclass_fields__) - {
            "data_root", "executable", "models", "library_dirs", "source_root",
            "extra_args", "exiftool", "ffprobe", "metadata_reader", "metadata_library_dirs"}
        for obj, allowed in [(raw, top_keys), (sdk, {
            "executable", "models", "library_dirs", "source_root", "extra_args"
        }), (tools, {"exiftool", "ffprobe"}), (metadata, {"reader", "library_dirs"})]:
            unknown = set(obj) - allowed
            if unknown:
                raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        metadata_fields = {"metadata_" + key: value for key, value in metadata.items()}
        cfg = cls(**raw, **sdk, **tools, **metadata_fields, data_root=data_root)
        cfg.photo_root = Path(cfg.photo_root)
        cfg.source_root = Path(cfg.source_root)
        cfg.validate()
        return cfg

    def validate(self):
        for key in ("scan_interval_minutes", "max_concurrent_jobs", "command_timeout"):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if type(self.settle_seconds) is not int or self.settle_seconds < 0:
            raise ValueError("settle_seconds must be a nonnegative integer")
        for key in ("width", "height", "bitrate"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
        if (self.width, self.height) != (0, 0) and (
                not self.width or not self.height or self.width != 2 * self.height
                or self.width % 2 or self.height % 2):
            raise ValueError("Use width=height=0 for source size, or an even 2:1 resolution")
        if self.codec not in ("source", "h264", "h265") or self.stitch_type not in (
                "optflow", "dynamicstitch", "aistitch"):
            raise ValueError("Invalid codec or stitch_type")
        for key in ("flowstate", "photo_flowstate"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} must be a boolean")
        if type(self.camera_accessory_type) is not int or not 0 <= self.camera_accessory_type <= 20:
            raise ValueError("camera_accessory_type must be an SDK accessory enum value (0..20)")
        if not self.photo_root.is_absolute() or not self.data_root.is_absolute():
            raise ValueError("photo_root and data directory must be absolute")
        if not isinstance(self.source_root, Path) or not self.source_root.is_absolute():
            raise ValueError("sdk.source_root must be an absolute path")
        photo, data = self.photo_root.resolve(), self.data_root.resolve()
        if photo == data or photo in data.parents or data in photo.parents:
            raise ValueError("Photo and data directories must not overlap")
        for key in ("library_dirs", "extra_args", "metadata_library_dirs"):
            value = getattr(self, key)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"sdk.{key} must be an array of strings")
        for key in ("executable", "models", "exiftool", "ffprobe", "metadata_reader"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key):
                raise ValueError(f"{key} must be a nonempty string")
        if not Path(self.executable).is_absolute() or not Path(self.models).is_absolute():
            raise ValueError("sdk.executable and sdk.models must be absolute paths")
        if not all(Path(path).is_absolute() for path in self.library_dirs):
            raise ValueError("sdk.library_dirs must contain absolute paths")
        if not Path(self.metadata_reader).is_absolute() or not all(
                Path(path).is_absolute() for path in self.metadata_library_dirs):
            raise ValueError("Metadata reader and library directories must be absolute paths")
        reserved = {"inputs", "output", "image_sequence_dir", "export_frame_index", "output_size"}
        if any(arg.split("=", 1)[0].lstrip("-") in reserved for arg in self.extra_args):
            raise ValueError("sdk.extra_args must not override input/output paths or output geometry")
        for index, arg in enumerate(self.extra_args):
            option, separator, value = arg.partition("=")
            option = option.lstrip("-").lower()
            if option == "disable_cuda":
                raise ValueError("GPU rendering is required; disabling CUDA is not supported")
            if option == "image_processing_accel":
                if not separator and index + 1 < len(self.extra_args):
                    value = self.extra_args[index + 1]
                if value.strip().lower() == "cpu":
                    raise ValueError("GPU rendering is required; CPU rendering is not supported")

    def managed_runtime_identity(self):
        """Return SDK content identities only for the complete managed layout."""
        base = self.data_root / "sdk"
        expected = (
            str(base / "media/bin/MediaSDKTest"), str(base / "media/bin/models"),
            [str(base / "media/lib"), str(base / "media/bin")],
            str(base / "metadata/bin/insta360-metadata"), [str(base / "metadata/lib")],
        )
        if (self.executable, self.models, self.library_dirs,
                self.metadata_reader, self.metadata_library_dirs) != expected:
            return None
        identities = []
        for component in ("media", "metadata"):
            manifest = base / component / ".insta360-bootstrap.json"
            try:
                if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 65536:
                    return None
                with manifest.open("rb") as file:
                    content = file.read(65537)
                if len(content) > 65536:
                    return None
                data = json.loads(content)
                identity = data.get("runtime_sha256")
                if (data.get("project") != "Insta360-OpenConverter"
                        or type(data.get("schema_version")) is not int
                        or data["schema_version"] != 1 or data.get("component") != component
                        or not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None):
                    return None
                identities.append(identity)
            except (OSError, ValueError, TypeError, AttributeError):
                return None
        return tuple(identities)

    def fingerprint(self):
        values = asdict(self)
        # Scheduling does not change rendered output; photo-only settings are
        # included in each photo job's identity.
        for key in ("scan_interval_minutes", "max_concurrent_jobs", "settle_seconds",
                    "command_timeout", "data_root", "photo_flowstate"):
            values.pop(key)
        values["pipeline_version"] = 3
        # Replacing the local SDK also invalidates successful jobs.
        binary = Path(self.executable)
        if binary.is_file():
            stat = binary.stat()
            values["sdk_binary"] = [stat.st_size, stat.st_mtime_ns]
        # Bootstrap manifests include all runtime files, including libraries
        # and models, so an SDK update also invalidates completed previews.
        for component in ("media", "metadata"):
            manifest = self.data_root / "sdk" / component / ".insta360-bootstrap.json"
            if manifest.is_file():
                values[f"{component}_runtime"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        for index, path in enumerate([Path(self.metadata_reader)] + [
                Path(directory) / "libInsMetaDataSDK.so" for directory in self.metadata_library_dirs]):
            if path.is_file():
                stat = path.stat()
                values[f"metadata_binary_{index}"] = [stat.st_size, stat.st_mtime_ns]
        return hashlib.sha256(json.dumps(values, default=str, sort_keys=True).encode()).hexdigest()
