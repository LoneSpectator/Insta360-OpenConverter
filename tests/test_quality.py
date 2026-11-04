"""Export-policy tests using synthetic ffprobe data, without running the SDK."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import unittest

from converter.config import Config
from converter.quality import ExportPlan, build_plan, summarize, validate_output


def video_stream(width=5760, height=2880, **overrides):
    stream = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": width,
        "height": height,
        "avg_frame_rate": "30000/1001",
        "r_frame_rate": "30000/1001",
        "pix_fmt": "yuv420p",
        "bits_per_raw_sample": "8",
        "bit_rate": "100000000",
        "disposition": {"attached_pic": 0},
    }
    stream.update(overrides)
    return stream


def media_probe(*streams, bitrate="100000000", duration="10.0"):
    return {
        "streams": list(streams) or [video_stream()],
        "format": {"bit_rate": bitrate, "duration": duration},
    }


def source_config(**overrides):
    values = {"width": 0, "height": 0, "bitrate": 0, "codec": "source", "flowstate": False}
    values.update(overrides)
    return Config(**values)


class ExportPlanTests(unittest.TestCase):
    def test_default_configuration_uses_source_properties(self):
        cfg = Config()
        self.assertEqual((cfg.width, cfg.height, cfg.bitrate), (0, 0, 0))
        self.assertEqual(cfg.codec, "source")
        self.assertFalse(cfg.flowstate)

    def test_single_equirectangular_source_preserves_dimensions_and_rate(self):
        plan = build_plan(source_config(), [media_probe()])
        self.assertIsInstance(plan, ExportPlan)
        self.assertEqual((plan.width, plan.height), (5760, 2880))
        self.assertEqual(plan.bitrate, 100000000)
        self.assertEqual(plan.codec, "h264")
        self.assertEqual(plan.fps, "30000/1001")
        self.assertEqual(plan.bit_depth, 8)
        self.assertEqual(plan.chroma, "420")
        self.assertIsInstance(plan.geometry, str)
        self.assertTrue(plan.geometry)
        self.assertIsInstance(plan.bitrate_basis, str)
        self.assertTrue(plan.bitrate_basis)
        with self.assertRaises(FrozenInstanceError):
            plan.width = 3840

    def test_two_square_lens_files_preserve_lens_height(self):
        probes = [media_probe(video_stream(2880, 2880)) for _ in range(2)]
        plan = build_plan(source_config(), probes)
        self.assertEqual((plan.width, plan.height), (5760, 2880))
        self.assertEqual(plan.bitrate, 200000000)

    def test_two_square_tracks_in_one_file_are_both_used(self):
        probe = media_probe(video_stream(2880, 2880), video_stream(2880, 2880))
        plan = build_plan(source_config(), [probe])
        self.assertEqual((plan.width, plan.height), (5760, 2880))
        self.assertEqual(plan.bitrate, 200000000)

    def test_smaller_proxy_audio_and_attached_picture_are_not_primary_video(self):
        probe = media_probe(
            video_stream(1280, 640, bit_rate="1000000"),
            {"codec_type": "audio", "codec_name": "aac", "bit_rate": "128000"},
            video_stream(12000, 6000, disposition={"attached_pic": 1}),
            video_stream(),
        )
        plan = build_plan(source_config(), [probe])
        self.assertEqual((plan.width, plan.height), (5760, 2880))
        self.assertEqual(plan.bitrate, 100000000)
        details = summarize(probe)
        self.assertEqual(len(details["streams"]), 1)
        primary = details["streams"][0]
        self.assertEqual((primary["width"], primary["height"]), (5760, 2880))
        self.assertEqual(primary["codec"], "h264")
        self.assertEqual(primary["fps"], "30000/1001")
        self.assertEqual(primary["bit_depth"], 8)
        self.assertEqual(primary["chroma"], "420")
        self.assertEqual(primary["pix_fmt"], "yuv420p")
        self.assertEqual(primary["bitrate"], 100000000)
        self.assertEqual(float(details["duration"]), 10.0)
        self.assertEqual(details["container_bitrate"], 100000000)

    def test_ambiguous_geometry_needs_explicit_resolution(self):
        for dimensions in ((2880, 2880), (4000, 3000)):
            with self.subTest(dimensions=dimensions):
                with self.assertRaises(ValueError):
                    build_plan(source_config(), [media_probe(video_stream(*dimensions))])

    def test_explicit_resolution_and_bitrate_override_source_values(self):
        plan = build_plan(
            source_config(width=3840, height=1920, bitrate=42000000),
            [media_probe(video_stream(4000, 3000))],
        )
        self.assertEqual((plan.width, plan.height), (3840, 1920))
        self.assertEqual(plan.bitrate, 42000000)

    def test_explicit_resolution_can_handle_two_equirectangular_inputs(self):
        plan = build_plan(
            source_config(width=5760, height=2880),
            [media_probe(), media_probe()],
        )
        self.assertEqual((plan.width, plan.height), (5760, 2880))

    def test_different_lens_dimensions_are_rejected(self):
        probes = [
            media_probe(video_stream(2880, 2880)),
            media_probe(video_stream(2688, 2688)),
        ]
        with self.assertRaises(ValueError):
            build_plan(source_config(), probes)

    def test_different_lens_frame_rates_are_rejected(self):
        probes = [
            media_probe(video_stream(2880, 2880)),
            media_probe(video_stream(2880, 2880, avg_frame_rate="24/1", r_frame_rate="24/1")),
        ]
        with self.assertRaises(ValueError):
            build_plan(source_config(), probes)

    def test_different_lens_durations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "durations"):
            build_plan(source_config(), [media_probe(video_stream(2880, 2880), duration="10"),
                                         media_probe(video_stream(2880, 2880), duration="1")])

    def test_equivalent_frame_rate_fractions_are_normalized(self):
        probes = [
            media_probe(video_stream(2880, 2880, avg_frame_rate="60000/2002")),
            media_probe(video_stream(2880, 2880)),
        ]
        self.assertEqual(build_plan(source_config(), probes).fps, "30000/1001")
        self.assertEqual(
            build_plan(source_config(), [media_probe(video_stream(avg_frame_rate="30/1"))]).fps,
            "30",
        )

    def test_missing_track_bitrate_uses_container_rate_once(self):
        missing_rate = video_stream(2880, 2880)
        missing_rate.pop("bit_rate")
        probe = media_probe(
            video_stream(2880, 2880, bit_rate="30000000"),
            missing_rate,
            bitrate="80000000",
        )
        self.assertEqual(build_plan(source_config(), [probe]).bitrate, 80000000)

    def test_bitrate_fallback_is_per_input_file(self):
        missing_rate = video_stream(2880, 2880)
        missing_rate.pop("bit_rate")
        probes = [
            media_probe(video_stream(2880, 2880, bit_rate="30000000")),
            media_probe(missing_rate, bitrate="80000000"),
        ]
        self.assertEqual(build_plan(source_config(), probes).bitrate, 110000000)

    def test_unknown_source_bitrate_uses_sdk_default(self):
        probe = media_probe()
        probe["streams"][0].pop("bit_rate")
        probe["format"].pop("bit_rate")
        self.assertEqual(build_plan(source_config(), [probe]).bitrate, 0)

    def test_hevc_source_uses_h265(self):
        plan = build_plan(source_config(), [media_probe(video_stream(codec_name="hevc"))])
        self.assertEqual(plan.codec, "h265")

    def test_ten_bit_source_retains_depth_and_color_tags(self):
        color = {
            "color_range": "tv",
            "color_space": "bt2020nc",
            "color_transfer": "smpte2084",
            "color_primaries": "bt2020",
        }
        stream = video_stream(pix_fmt="yuv420p10le", bits_per_raw_sample="10", **color)
        plan = build_plan(source_config(), [media_probe(stream)])
        self.assertEqual(plan.codec, "h265")
        self.assertEqual(plan.bit_depth, 10)
        self.assertEqual(plan.color, color)

    def test_manual_h264_is_rejected_for_ten_bit_source(self):
        stream = video_stream(codec_name="hevc", pix_fmt="yuv420p10le", bits_per_raw_sample="10")
        with self.assertRaises(ValueError):
            build_plan(source_config(codec="h264"), [media_probe(stream)])

    def test_manual_h265_is_respected_for_eight_bit_source(self):
        plan = build_plan(source_config(codec="h265"), [media_probe()])
        self.assertEqual(plan.codec, "h265")

    def test_unknown_source_video_codec_requires_explicit_choice(self):
        probe = media_probe(video_stream(codec_name="unrecognized_codec"))
        with self.assertRaises(ValueError):
            build_plan(source_config(), [probe])

    def test_jpeg_plan_preserves_photo_dimensions_and_ignores_video_bitrate(self):
        probe = media_probe(video_stream(11904, 5952, codec_name="mjpeg", pix_fmt="yuvj420p"))
        plan = build_plan(source_config(bitrate=42000000), [probe], is_video=False)
        self.assertEqual((plan.width, plan.height), (11904, 5952))
        self.assertEqual(plan.codec, "jpeg")
        self.assertEqual(plan.bitrate, 0)

    def test_photo_auto_geometry_requires_equirectangular_source(self):
        probe = media_probe(video_stream(4000, 3000, codec_name="mjpeg", pix_fmt="yuvj420p"))
        with self.assertRaises(ValueError):
            build_plan(source_config(), [probe], is_video=False)


class OutputValidationTests(unittest.TestCase):
    def setUp(self):
        self.probe = media_probe()
        self.plan = build_plan(source_config(), [self.probe])

    def test_output_matching_plan_is_accepted(self):
        validate_output(self.plan, deepcopy(self.probe), is_video=True)

    def test_equivalent_output_frame_rate_fraction_is_accepted(self):
        output = deepcopy(self.probe)
        output["streams"][0]["avg_frame_rate"] = "60000/2002"
        validate_output(self.plan, output, is_video=True)

    def test_output_size_frame_rate_and_codec_must_match(self):
        for field, value in (("width", 3840), ("height", 1920),
                             ("avg_frame_rate", "24/1"), ("codec_name", "hevc")):
            with self.subTest(field=field):
                output = deepcopy(self.probe)
                output["streams"][0][field] = value
                with self.assertRaises(RuntimeError):
                    validate_output(self.plan, output, is_video=True)

    def test_output_bit_depth_must_not_be_reduced(self):
        source = media_probe(video_stream(
            codec_name="hevc", pix_fmt="yuv420p10le", bits_per_raw_sample="10"))
        plan = build_plan(source_config(), [source])
        output = media_probe(video_stream(codec_name="hevc"))
        with self.assertRaises(RuntimeError):
            validate_output(plan, output, is_video=True)

    def test_known_color_tags_must_not_be_lost_or_changed(self):
        color = {
            "color_range": "tv",
            "color_space": "bt2020nc",
            "color_transfer": "smpte2084",
            "color_primaries": "bt2020",
        }
        source = media_probe(video_stream(**color))
        plan = build_plan(source_config(), [source])
        validate_output(plan, deepcopy(source), is_video=True)
        for field in color:
            for changed in (False, True):
                with self.subTest(field=field, changed=changed):
                    output = deepcopy(source)
                    if changed:
                        output["streams"][0][field] = "unknown"
                    else:
                        output["streams"][0].pop(field)
                    with self.assertRaises(RuntimeError):
                        validate_output(plan, output, is_video=True)

    def test_chroma_subsampling_must_not_be_reduced(self):
        source = media_probe(video_stream(pix_fmt="yuv444p"))
        plan = build_plan(source_config(), [source])
        self.assertEqual(plan.chroma, "444")
        for pixel_format in ("yuv422p", "yuv420p"):
            with self.subTest(pixel_format=pixel_format):
                output = media_probe(video_stream(pix_fmt=pixel_format))
                with self.assertRaises(RuntimeError):
                    validate_output(plan, output, is_video=True)

    def test_chroma_detail_is_checked_on_both_axes(self):
        source = media_probe(video_stream(pix_fmt="yuv440p"))
        plan = build_plan(source_config(), [source])
        with self.assertRaises(RuntimeError):
            validate_output(plan, media_probe(video_stream(pix_fmt="yuv422p")))

    def test_hdr_side_data_must_not_be_lost(self):
        hdr = [{"side_data_type": "Mastering display metadata", "red_x": "34000/50000"}]
        source = media_probe(video_stream(side_data_list=hdr))
        plan = build_plan(source_config(), [source])
        validate_output(plan, deepcopy(source))
        with self.assertRaisesRegex(RuntimeError, "HDR"):
            validate_output(plan, media_probe())

    def test_audio_from_secondary_lens_must_not_be_discarded(self):
        left = media_probe(video_stream(2880, 2880))
        right = media_probe(video_stream(2880, 2880), {"codec_type": "audio", "codec_name": "aac"})
        plan = build_plan(source_config(), [left, right])
        self.assertTrue(plan.audio_required)
        with self.assertRaisesRegex(RuntimeError, "audio"):
            validate_output(plan, media_probe())

    def test_output_video_requires_positive_duration(self):
        for duration in ("0", "-1", "N/A", None):
            with self.subTest(duration=duration):
                output = media_probe(duration=duration)
                with self.assertRaises(RuntimeError):
                    validate_output(self.plan, output, is_video=True)

    def test_output_video_requires_one_main_video_stream(self):
        for streams in (
            [video_stream(), video_stream(1280, 640)],
            [video_stream(disposition={"attached_pic": 1})],
            [{"codec_type": "audio", "codec_name": "aac"}],
        ):
            with self.subTest(streams=streams):
                with self.assertRaises(RuntimeError):
                    validate_output(self.plan, media_probe(*streams), is_video=True)

    def test_cover_art_does_not_count_as_an_additional_video_stream(self):
        output = media_probe(
            video_stream(),
            video_stream(8000, 4000, disposition={"attached_pic": 1}),
        )
        validate_output(self.plan, output, is_video=True)

    def test_jpeg_output_is_validated_without_a_video_duration(self):
        source = media_probe(video_stream(codec_name="mjpeg", pix_fmt="yuvj420p"))
        plan = build_plan(source_config(), [source], is_video=False)
        output = deepcopy(source)
        output["format"].pop("duration")
        validate_output(plan, output, is_video=False)
        output["streams"][0]["codec_name"] = "png"
        with self.assertRaises(RuntimeError):
            validate_output(plan, output, is_video=False)


if __name__ == "__main__":
    unittest.main()
