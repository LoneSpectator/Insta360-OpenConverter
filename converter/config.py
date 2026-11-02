from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path

try:
    import tomllib
except ImportError:  # Ubuntu 22.04 / Python 3.10
    import tomli as tomllib


@dataclass
class Config:
    photo_root: Path = Path("/photo")
    data_root: Path = Path("/data")
    scan_interval: int = 60
    settle_seconds: int = 120
    retry_seconds: int = 300
    command_timeout: int = 7200
    width: int = 3840
    height: int = 1920
    bitrate: int = 20_000_000
    codec: str = "h264"
    flowstate: bool = True
    stitch_type: str = "optflow"
    camera_accessory_type: int = 0
    executable: str = "/data/sdk/media/bin/MediaSDKTest"
    models: str = "/data/sdk/media/bin/models"
    library_dirs: list[str] = field(default_factory=lambda: [
        "/data/sdk/media/lib", "/data/sdk/media/bin"])
    mode: str = "gpu"
    cpu_compatible: bool = False
    extra_args: list[str] = field(default_factory=list)
    exiftool: str = "exiftool"
    ffprobe: str = "ffprobe"
    metadata_reader: str = "/data/sdk/metadata/bin/insta360-metadata"
    metadata_library_dirs: list[str] = field(default_factory=lambda: ["/data/sdk/metadata/lib"])

    @classmethod
    def load(cls, path: Path, data_root: Path):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        sdk = raw.pop("sdk", {})
        tools = raw.pop("tools", {})
        metadata = raw.pop("metadata", {})
        top_keys = set(cls.__dataclass_fields__) - {
            "data_root", "executable", "models", "library_dirs", "mode",
            "cpu_compatible", "extra_args", "exiftool", "ffprobe", "metadata_reader", "metadata_library_dirs"}
        for obj, allowed in [(raw, top_keys), (sdk, {
            "executable", "models", "library_dirs", "mode", "cpu_compatible", "extra_args"
        }), (tools, {"exiftool", "ffprobe"}), (metadata, {"reader", "library_dirs"})]:
            unknown = set(obj) - allowed
            if unknown:
                raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        metadata_fields = {"metadata_" + key: value for key, value in metadata.items()}
        cfg = cls(**raw, **sdk, **tools, **metadata_fields, data_root=data_root)
        cfg.photo_root = Path(cfg.photo_root)
        cfg.validate()
        return cfg

    def validate(self):
        for key in ("scan_interval", "retry_seconds", "command_timeout", "width", "height", "bitrate"):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if type(self.settle_seconds) is not int or self.settle_seconds < 0:
            raise ValueError("settle_seconds must be a nonnegative integer")
        if self.width != 2 * self.height or self.width % 2 or self.height % 2:
            raise ValueError("Output must have an even, 2:1 resolution")
        if self.codec not in ("h264", "h265") or self.stitch_type not in (
                "optflow", "dynamicstitch", "aistitch"):
            raise ValueError("Invalid codec or stitch_type")
        if type(self.flowstate) is not bool or type(self.cpu_compatible) is not bool:
            raise ValueError("flowstate and sdk.cpu_compatible must be booleans")
        if type(self.camera_accessory_type) is not int or not 0 <= self.camera_accessory_type <= 20:
            raise ValueError("camera_accessory_type must be an SDK accessory enum value (0..20)")
        if self.mode not in ("gpu", "cpu"):
            raise ValueError("sdk.mode must be gpu or cpu")
        if self.mode == "cpu" and not self.cpu_compatible:
            raise ValueError("MediaSDK 3.x requires GPU. Obtain an official CPU-compatible SDK first.")
        if not self.photo_root.is_absolute() or not self.data_root.is_absolute():
            raise ValueError("photo_root and data directory must be absolute")
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

    def fingerprint(self):
        values = asdict(self)
        for key in ("scan_interval", "settle_seconds", "retry_seconds", "command_timeout", "data_root"):
            values.pop(key)
        values["pipeline_version"] = 2
        # Replacing the local SDK also invalidates successful jobs.
        binary = Path(self.executable)
        if binary.is_file():
            stat = binary.stat()
            values["sdk_binary"] = [stat.st_size, stat.st_mtime_ns]
        for index, path in enumerate([Path(self.metadata_reader)] + [
                Path(directory) / "libInsMetaDataSDK.so" for directory in self.metadata_library_dirs]):
            if path.is_file():
                stat = path.stat()
                values[f"metadata_binary_{index}"] = [stat.st_size, stat.st_mtime_ns]
        return hashlib.sha256(json.dumps(values, default=str, sort_keys=True).encode()).hexdigest()
