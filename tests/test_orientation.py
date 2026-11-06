import copy
import unittest

from converter.orientation import (ORIENTATION_TAGS, normalized_orientation_args,
                                   sdk_orientation, verify_orientation)


class JpegOrientationTests(unittest.TestCase):
    def test_all_standard_rotations_and_mirrors_are_preserved(self):
        for value in range(1, 9):
            with self.subTest(value=value):
                metadata = {"IFD0:Orientation": value, "XMP-tiff:Orientation": str(value)}
                self.assertEqual(sdk_orientation(metadata), value)
                self.assertEqual(normalized_orientation_args(value),
                                 [f"-{tag}#={value}" for tag in ORIENTATION_TAGS])
                verify_orientation(value, metadata)

    def test_either_sdk_tag_can_supply_the_display_transform(self):
        for tag in ORIENTATION_TAGS:
            with self.subTest(tag=tag):
                self.assertEqual(sdk_orientation({tag: " 3 "}), 3)

    def test_absent_sdk_orientation_does_not_invent_or_inherit_a_transform(self):
        self.assertIsNone(sdk_orientation({"SourceOrientation": 3, "IFD1:Orientation": 8}))
        self.assertEqual(normalized_orientation_args(None),
                         ["-IFD0:Orientation=", "-XMP-tiff:Orientation="])
        verify_orientation(None, {"IFD1:Orientation": 8})

    def test_conflicting_sdk_tags_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "SDK JPEG orientation metadata conflicts:"):
            sdk_orientation({"IFD0:Orientation": 3, "XMP-tiff:Orientation": 1})

    def test_invalid_sdk_values_are_rejected_instead_of_guessed(self):
        for tag in ORIENTATION_TAGS:
            for value in (True, False, 0, 9, -1, 3.0, None, "", "03", "3.0", "Rotate 180", [], {}):
                with self.subTest(tag=tag, value=value):
                    with self.assertRaisesRegex(ValueError, "SDK JPEG orientation metadata is invalid:"):
                        sdk_orientation({tag: value})

    def test_metadata_snapshot_is_not_modified(self):
        metadata = {"IFD0:Orientation": 3, "XMP-tiff:Orientation": "3", "other": {"value": 1}}
        previous = copy.deepcopy(metadata)
        self.assertEqual(sdk_orientation(metadata), 3)
        self.assertEqual(metadata, previous)

    def test_verification_rejects_missing_changed_invalid_and_conflicting_tags(self):
        for actual in ({}, {"IFD0:Orientation": 3},
                       {"IFD0:Orientation": 1, "XMP-tiff:Orientation": 1},
                       {"IFD0:Orientation": True, "XMP-tiff:Orientation": 3},
                       {"IFD0:Orientation": 3, "XMP-tiff:Orientation": 8}):
            with self.subTest(actual=actual):
                with self.assertRaisesRegex(RuntimeError, "JPEG orientation verification failed:"):
                    verify_orientation(3, actual)

    def test_absent_sdk_transform_requires_both_source_fields_to_be_cleared(self):
        for tag in ORIENTATION_TAGS:
            for value in (1, 3, None):
                with self.subTest(tag=tag, value=value):
                    with self.assertRaisesRegex(RuntimeError, "JPEG orientation verification failed:"):
                        verify_orientation(None, {tag: value})

    def test_argument_generation_rejects_invalid_transform(self):
        for value in (True, 0, 9, 3.0, "Rotate 180"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalized_orientation_args(value)


if __name__ == "__main__":
    unittest.main()
