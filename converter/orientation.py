"""Preserve the SDK JPEG's display transform without changing its pixels."""


ORIENTATION_TAGS = ("IFD0:Orientation", "XMP-tiff:Orientation")


def _orientation(value):
    if type(value) is int and 1 <= value <= 8:
        return value
    if isinstance(value, str) and value.strip() in tuple("12345678"):
        return int(value.strip())
    raise ValueError(f"Invalid JPEG orientation: {value!r}")


def sdk_orientation(metadata):
    """Read the rendered SDK image, independently of original-file tags."""
    values = {}
    for tag in ORIENTATION_TAGS:
        if tag not in metadata:
            continue
        try:
            values[tag] = _orientation(metadata[tag])
        except ValueError as error:
            raise ValueError(f"SDK JPEG orientation metadata is invalid: {tag}={metadata[tag]!r}") from error
    if not values:
        return None
    if len(set(values.values())) != 1:
        raise ValueError(f"SDK JPEG orientation metadata conflicts: {values}")
    return next(iter(values.values()))


def normalized_orientation_args(orientation):
    """Set consistent EXIF/XMP transforms, or clear unknown SDK transforms."""
    if orientation is None:
        return [f"-{tag}=" for tag in ORIENTATION_TAGS]
    value = _orientation(orientation)
    # ExifTool's # suffix writes the numeric TIFF enum without PrintConv.
    return [f"-{tag}#={value}" for tag in ORIENTATION_TAGS]


def verify_orientation(expected, actual):
    """Require the copied metadata to retain the SDK's transform semantics."""
    if expected is not None:
        expected = _orientation(expected)
    for tag in ORIENTATION_TAGS:
        if expected is None:
            if tag in actual:
                raise RuntimeError(f"JPEG orientation verification failed: unexpected {tag}={actual[tag]!r}")
            continue
        try:
            value = _orientation(actual[tag])
        except (KeyError, ValueError) as error:
            raise RuntimeError(f"JPEG orientation verification failed: missing or invalid {tag}") from error
        if value != expected:
            raise RuntimeError(f"JPEG orientation verification failed: {tag}: {expected} -> {value}")
