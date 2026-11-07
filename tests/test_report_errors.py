import unittest

from converter.report_errors import explain_error, explain_reason


class ReportErrorTests(unittest.TestCase):
    def test_legacy_workspace_cleanup_error_is_not_described_as_bad_camera_media(self):
        for raw in ("[Errno 39] Directory not empty: 'report-0123456789abcdef'",
                    "[Errno 39] Directory not empty: '/photo/event/.insta360-openconverter/metadata-read-0123456789abcdef'"):
            with self.subTest(raw=raw):
                result = explain_error(raw)
                self.assertEqual(result["error_code"], "workspace_cleanup_failed")
                self.assertIn("临时工作目录", result["error"])
                self.assertIn("后续清理", result["error"])
                self.assertEqual(result["error_raw"], raw)
        unrelated = "[Errno 39] Directory not empty: '/photo/original-event'"
        self.assertEqual(explain_error(unrelated)["error_code"], "processing_failed")

    def test_filesystem_errors_preserve_original_context(self):
        cases = (
            ("[Errno 13] Permission denied: '/photo/事件/file'", "permission_denied", "权限"),
            ("[Errno 30] Read-only file system: '/photo/file'", "read_only_filesystem", "只读"),
            ("[Errno 28] No space left on device: '/photo/file'", "no_space", "空间不足"),
            ("[Errno 2] No such file or directory: '/sdk/reader'", "path_missing", "不存在"),
            ("[Errno 17] File exists: 'temporary' -> 'preview'", "destination_exists", "停止覆盖"),
        )
        for raw, code, chinese in cases:
            with self.subTest(raw=raw):
                self.assertEqual(explain_error(raw)["error_code"], code)
                self.assertIn(chinese, explain_error(raw)["error"])
                self.assertEqual(explain_error(raw)["error_raw"], raw)

    def test_runtime_libraries_distinguish_driver_injection_from_image_dependencies(self):
        prefix = "MediaSDKTest exited 127: error while loading shared libraries: "
        for library, code in (("libgtk-3.so.0", "runtime_library_missing"),
                              ("libcuda.so.1", "gpu_library_missing"),
                              ("libnvidia-encode.so.1", "gpu_library_missing")):
            raw = prefix + library + ": cannot open shared object file: No such file or directory"
            with self.subTest(library=library):
                result = explain_error(raw)
                self.assertEqual(result["error_code"], code)
                self.assertIn(library, result["error"])
                self.assertEqual(result["error_raw"], raw)
        wrapped = "media SDK preparation failed: the container image/runtime environment is missing shared library libgtk-3.so.0. Original loader error: unavailable"
        self.assertIn("libgtk-3.so.0。", explain_error(wrapped)["error"])

    def test_native_parse_error_is_more_specific_than_exit_code(self):
        raw = "/data/sdk/metadata/bin/insta360-metadata exited 3: InsMetaDataSDK Parse failed"
        result = explain_error(raw)
        self.assertEqual(result["error_code"], "metadata_sdk_parse_failed")
        self.assertIn("官方元数据 SDK 无法解析", result["error"])
        self.assertEqual(result["error_raw"], raw)

    def test_native_argument_check_is_distinguished_from_media_parse_failure(self):
        raw = "/data/sdk/metadata/bin/insta360-metadata exited 1: Expected a regular input file and an empty output directory\n"
        result = explain_error(raw)
        self.assertEqual(result["error_code"], "metadata_adapter_arguments_invalid")
        self.assertIn("空输出目录", result["error"])
        self.assertIn("尚未开始解析", result["error"])
        self.assertEqual(result["error_raw"], raw)
        self.assertNotEqual(result["error_code"], "metadata_sdk_parse_failed")

    def test_photo_orientation_errors_have_specific_chinese_diagnostics(self):
        for raw, code in (
            ("SDK JPEG orientation metadata conflicts: {'IFD0:Orientation': 3, 'XMP-tiff:Orientation': 1}",
             "photo_orientation_invalid"),
            ("SDK JPEG orientation metadata is invalid: IFD0:Orientation=0", "photo_orientation_invalid"),
            ("JPEG orientation verification failed: missing or invalid IFD0:Orientation",
             "photo_orientation_verification_failed"),
        ):
            with self.subTest(raw=raw):
                result = explain_error(raw)
                self.assertEqual(result["error_code"], code)
                self.assertIn("方向标记", result["error"])
                self.assertEqual(result["error_raw"], raw)

    def test_large_video_metadata_error_is_not_reported_as_generic_command_failure(self):
        for prefix in ("Error", "Warning"):
            raw = ("exiftool exited 1: Warning: [minor] No media data - /photo/大视频.mp4\n"
                   f"{prefix}: End of processing at large atom (LargeFileSupport not enabled) - /photo/大视频.mp4\n")
            with self.subTest(prefix=prefix):
                result = explain_error(raw)
                self.assertEqual(result["error_code"], "metadata_large_file_support_disabled")
                self.assertIn("大文件支持", result["error"])
                self.assertEqual(result["error_raw"], raw)
        filename = "exiftool exited 1: unrelated - /photo/End of processing at large atom (LargeFileSupport not enabled).mp4"
        self.assertEqual(explain_error(filename)["error_code"], "command_failed")

    def test_command_timeout_and_failure_are_actionable_without_guessing_sdk_cause(self):
        raw = "Command exceeded 7200s: /data/sdk/media/bin/MediaSDKTest"
        self.assertEqual(explain_error(raw)["error_code"], "command_timeout")
        self.assertIn("7200 秒", explain_error(raw)["error"])
        raw = "/path with spaces/MediaSDKTest exited -9: diagnostic"
        result = explain_error(raw)
        self.assertEqual(result["error_code"], "command_failed")
        self.assertIn("-9", result["error"])
        self.assertNotIn("内存不足", result["error"])

    def test_gpu_processing_error_does_not_automatically_blame_initialization(self):
        raw = "MediaSDKTest exited 1: CUDA_ERROR_INVALID_VALUE while converting a frame"
        result = explain_error(raw)
        self.assertEqual(result["error_code"], "gpu_processing_failed")
        self.assertNotIn("初始化", result["error"])
        raw = "MediaSDKTest exited 1: CUDA driver version is insufficient for CUDA runtime version"
        self.assertEqual(explain_error(raw)["error_code"], "gpu_initialization_failed")

    def test_existing_metadata_header_error_does_not_blame_sdk_output(self):
        raw = "Invalid or oversized metadata ownership header: /photo/VID_metadata.json"
        result = explain_error(raw)
        self.assertEqual(result["error_code"], "metadata_ownership_invalid")
        self.assertIn("已有元数据", result["error"])
        self.assertNotIn("适配器返回", result["error"])

    def test_original_mutation_unknown_geometry_and_false_sdk_success(self):
        cases = (
            ("Source changed during conversion; staged preview discarded", "source_changed", "上传"),
            ("Source changed while repairing metadata", "source_changed", "停止发布"),
            ("Cannot infer a source-preserving 2:1 size from this lens layout; configure width/height explicitly",
             "source_layout_unsupported", "width"),
            ("SDK returned without a nonempty output file; inspect command.output logs", "sdk_no_output", "未生成有效"),
        )
        for raw, code, phrase in cases:
            with self.subTest(raw=raw):
                result = explain_error(raw)
                self.assertEqual(result["error_code"], code)
                self.assertIn(phrase, result["error"])

    def test_output_validation_explains_which_property_failed(self):
        cases = (
            ("Output resolution differs from the export plan", "output_dimensions_mismatch", "尺寸"),
            ("Output codec differs from the export plan", "output_codec_mismatch", "编码"),
            ("SDK output changed or omitted the source frame rate", "output_fps_changed", "帧率"),
            ("SDK output reduced or omitted the source bit depth", "output_bit_depth_reduced", "位深"),
            ("SDK output reduced or omitted source chroma sampling", "output_chroma_reduced", "色度"),
            ("SDK output changed or omitted source color metadata: color_transfer", "output_color_changed", "色彩"),
            ("SDK output changed or omitted source HDR metadata", "output_hdr_changed", "HDR"),
            ("SDK output discarded a source audio stream", "output_audio_missing", "音轨"),
            ("SDK output duration differs from the source recording", "output_duration_mismatch", "时长"),
            ("360° projection metadata was not written", "panorama_metadata_missing", "全景标记"),
        )
        for raw, code, phrase in cases:
            with self.subTest(raw=raw):
                result = explain_error(raw)
                self.assertEqual(result["error_code"], code)
                self.assertIn(phrase, result["error"])
                self.assertEqual(result["error_raw"], raw)

    def test_protected_file_conflicts_are_not_described_as_sdk_corruption(self):
        cases = (
            ("Refusing to overwrite an unowned or externally changed preview: /photo/preview.jpg", "preview_conflict"),
            ("Refusing to overwrite unowned or externally changed metadata: /photo/file_metadata.json", "metadata_conflict"),
            ("Refusing to overwrite metadata belonging to another source: /photo/file_metadata.json", "metadata_source_conflict"),
            ("Metadata directory path already exists; refusing to create both metadata forms: /photo/file_metadata", "metadata_layout_conflict"),
            ("Metadata path is a symlink: /photo/file_metadata.json", "symlink_path"),
            ("Refusing to overwrite an unrelated text file: /photo/report.txt", "report_conflict"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                result = explain_error(raw)
                self.assertEqual(result["error_code"], code)
                self.assertNotIn("SDK 损坏", result["error"])
                self.assertEqual(result["error_raw"], raw)

    def test_english_message_with_chinese_filename_is_still_translated(self):
        result = explain_error("Refusing to overwrite unowned or externally changed metadata: /photo/事件_metadata.json")
        self.assertEqual(result["error_code"], "metadata_conflict")
        self.assertIn("已有元数据", result["error"])

    def test_words_in_a_filename_do_not_trigger_a_made_up_diagnosis(self):
        for raw in ("Unexpected failure for /photo/permission denied.insv",
                    "Unexpected failure for /photo/No space left on device.insv",
                    "Could not process /photo/gpu.insv",
                    "/photo/Source changed during conversion.insv"):
            with self.subTest(raw=raw):
                result = explain_error(raw)
                self.assertEqual(result["error_code"], "processing_failed")
                self.assertEqual(result["error_raw"], raw)

    def test_unknown_and_already_chinese_messages_retain_original_evidence(self):
        raw = "Unexpected SDK diagnostic: code 0x9876"
        result = explain_error(raw)
        self.assertEqual(result["error_code"], "processing_failed")
        self.assertIn("原始错误", result["error"])
        self.assertEqual(result["error_raw"], raw)
        raw = "原文件尚未准备完成，请稍后重试。"
        result = explain_error(raw)
        self.assertEqual(result["error"], raw)
        self.assertEqual(result["error_raw"], raw)

    def test_deferred_reasons_distinguish_upload_pairing_and_name_conflicts(self):
        cases = (
            ("Source is empty or still settling after upload", "上传"),
            ("Missing _00_ lens file; waiting for the matching source", "_00_"),
            ("Multiple sources would write the same preview: /photo/same_preview.mp4", "同名预览"),
            ("Multiple sources would write the same metadata: /photo/same_metadata.json", "同名元数据"),
        )
        for raw, phrase in cases:
            with self.subTest(raw=raw):
                result = explain_reason(raw)
                self.assertIn(phrase, result["reason"])
                self.assertEqual(result["reason_raw"], raw)
                self.assertNotIn("error", result)
        raw = "其他暂缓原因"
        self.assertEqual(explain_reason(raw)["reason"], raw)
        self.assertIn("原始说明", explain_reason("Unrecognized deferred state")["reason"])


if __name__ == "__main__":
    unittest.main()
