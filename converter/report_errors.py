"""Chinese report messages with stable categories and untouched audit details."""
import re


def _text(raw):
    return "" if raw is None else str(raw)


def _chinese_message(text):
    # A Chinese filename inside an English error must not bypass translation.
    return bool(re.match(r"^[\u3400-\u9fff]", text.lstrip()))


# These prefixes are emitted by this application. Match the message, rather
# than isolated words that can also appear in an original file's path.
_MESSAGES = (
    (("Source changed ",), "source_changed",
     "原文件在处理期间发生变化，已停止发布结果。请等待上传或修改完成后重试。"),
    (("SDK input changed while preparing",), "sdk_input_changed",
     "SDK 文件在准备期间发生变化，请等待文件复制完成后重试。"),
    (("Cannot infer a source-preserving 2:1 size",), "source_layout_unsupported",
     "无法从原片的镜头布局确定保留原画质的全景尺寸，请核对素材并配置 width、height。"),
    (("Source has no readable main image/video stream",), "source_stream_unreadable",
     "无法读取原文件的主要图像或视频流，请检查文件是否完整及格式是否受支持。"),
    (("Source lens dimensions differ",), "source_dimensions_mismatch", "配对镜头的原始分辨率不一致，无法合并处理。"),
    (("Source lens frame rates differ",), "source_fps_mismatch", "配对镜头的原始帧率不一致，无法合并处理。"),
    (("Source lens durations differ",), "source_duration_mismatch", "配对镜头的录制时长不一致，请核对配对文件是否完整。"),
    (("Source lens bit depths differ",), "source_bit_depth_mismatch", "配对镜头的原始位深不一致，无法合并处理。"),
    (("Source lens color descriptions differ",), "source_color_mismatch", "配对镜头的色彩描述不一致，无法合并处理。"),
    (("Source lens chroma sampling differs",), "source_chroma_mismatch", "配对镜头的色度采样不一致，无法合并处理。"),
    (("Source lens HDR metadata differs",), "source_hdr_mismatch", "配对镜头的 HDR 元数据不一致，无法合并处理。"),
    (("Cannot preserve the source codec",), "source_codec_unsupported",
     "无法沿用原视频编码，请核对 SDK 支持范围或显式配置支持的编码。"),
    (("Resolved source size must be even and 2:1",), "export_dimensions_invalid",
     "导出尺寸须为偶数且宽高比为 2:1，请检查 width、height 配置。"),
    (("SDK 10-bit export cannot preserve",), "source_bit_depth_unsupported",
     "原视频位深超过当前 SDK 的 10-bit 导出能力，已停止导出以避免降低位深。"),
    (("H.264 SDK export cannot preserve 10-bit source",), "export_10bit_codec_invalid",
     "当前 H.264 导出不能保留 10-bit 原片，请将 codec 配置为 source 或 h265。"),
    (("This MediaSDK CLI cannot confirm 10-bit export",), "export_10bit_unsupported",
     "当前 MediaSDK 命令行接口未确认支持 10-bit 导出，已停止导出以避免降低位深。"),
    (("SDK returned without a nonempty output file",), "sdk_no_output",
     "SDK 结束后未生成有效输出文件，请查看本次运行日志中的 SDK 输出。"),
    (("Output must contain exactly one stitched main stream",), "output_stream_invalid",
     "输出未包含唯一的已拼接主图像或视频流，结果未发布。"),
    (("Output resolution differs from the export plan", "Preview image metadata size mismatch"),
     "output_dimensions_mismatch", "输出尺寸与导出计划不符，结果未发布。"),
    (("Output codec differs from the export plan",), "output_codec_mismatch", "输出编码与导出计划不符，结果未发布。"),
    (("SDK output reduced or omitted the source bit depth",), "output_bit_depth_reduced",
     "SDK 输出降低了原片位深，或未提供可核对的位深信息，结果未发布。"),
    (("SDK output reduced or omitted source chroma sampling",), "output_chroma_reduced",
     "SDK 输出降低了原片色度采样，或未提供可核对的信息，结果未发布。"),
    (("SDK output changed or omitted source color metadata",), "output_color_changed",
     "SDK 输出改变或遗漏了原片色彩描述，结果未发布。"),
    (("SDK output changed or omitted source HDR metadata",), "output_hdr_changed",
     "SDK 输出改变或遗漏了原片 HDR 元数据，结果未发布。"),
    (("SDK output changed or omitted the source frame rate",), "output_fps_changed",
     "SDK 输出帧率与原片不一致或无法核对，结果未发布。"),
    (("Output video has no positive duration",), "output_duration_invalid", "输出视频没有有效时长，结果未发布。"),
    (("SDK output duration differs from the source recording", "Video duration mismatch:"),
     "output_duration_mismatch", "输出视频时长与原片不符，结果未发布。"),
    (("SDK output discarded a source audio stream", "SDK discarded the source audio stream"),
     "output_audio_missing", "SDK 输出遗漏了原片音轨，结果未发布。"),
    (("360° projection metadata was not written", "360° viewer metadata was not written"),
     "panorama_metadata_missing", "输出文件未成功写入 360° 全景标记，结果未发布。"),
    (("Metadata verification failed:",), "metadata_verification_failed",
     "输出元数据与原片中需保留的字段不一致，结果未发布。请查看原始错误中的字段名。"),
    (("Cannot read metadata for ",), "source_metadata_unreadable",
     "无法读取原文件的标准元数据，请检查文件完整性及本次运行日志。"),
    (("Refusing to overwrite an unowned or externally changed preview:",), "preview_conflict",
     "已有预览不属于本工具，或已被外部修改。为保护文件，已停止覆盖。"),
    (("Refusing to overwrite unowned or externally changed metadata:",), "metadata_conflict",
     "已有元数据不属于本工具，或已被外部修改。为保护文件，已停止覆盖。"),
    (("Refusing to overwrite metadata belonging to another source:",), "metadata_source_conflict",
     "同名元数据属于另一个原文件，已停止覆盖。请整理原文件主名冲突。"),
    (("Metadata directory path already exists;",), "metadata_layout_conflict",
     "已存在同主名的 _metadata 路径。为避免 JSON 与目录两种形式并存，已停止写入。"),
    (("Invalid or oversized metadata ownership header:",), "metadata_ownership_invalid",
     "已有元数据文件的归属标记无效或过大，已停止覆盖。请检查原始错误和现有文件。"),
    (("Metadata changed while checking ownership:", "Metadata changed before publication;"),
     "metadata_changed", "元数据文件在读写期间发生变化，为保护已有文件已停止覆盖。"),
    (("Preview changed during conversion;",), "preview_changed",
     "预览文件在处理期间发生变化，为保护已有文件已停止覆盖。"),
    (("Directory report changed ",), "report_changed",
     "目录报告在读写期间发生变化，为保护已有记录已停止覆盖。"),
    (("Temporary workspace root is not a real directory:",), "temporary_directory_conflict",
     "临时工作目录路径已被文件或符号链接占用，请检查 .insta360-openconverter 路径。"),
    (("Temporary workspace root changed during creation",), "temporary_directory_changed",
     "临时工作目录在创建期间发生变化，已停止当前任务以保护文件。"),
    (("Refusing to overwrite an unrelated text file:",), "report_conflict",
     "同名报告不是本工具生成的文件，已保留。请处理命名冲突后重试。"),
    (("Directory report contains invalid JSON:", "Directory report has an unsupported format:",
      "Refusing to overwrite an unrelated text file or invalid JSON report:"),
     "report_invalid", "同名文件不是有效的本工具报告，已保留。请检查命名冲突或报告格式。"),
    (("Metadata path is a symlink:", "Preview path is a symlink:", "SDK destination is a symlink:",
      "photo_root must not be a symlink", "SDK cache directory must not be a symlink:"),
     "symlink_path", "目标路径是符号链接，程序已停止读写该路径以保护原有文件。"),
    (("Not a regular file:", "Refusing to replace a non-regular directory report:"),
     "non_regular_file", "目标路径不是普通文件，程序已停止覆盖。请核对原始错误中的路径。"),
    (("Missing SDK track:", "Truncated SDK track:", "Invalid SDK track descriptor:",
      "SDK track record count mismatch:", "SDK track changed while merging metadata:",
      "SDK track record is not an object:"), "metadata_track_invalid",
     "SDK 传感器轨迹缺失、不完整或校验不一致，元数据文件未发布。请查看原始错误中的轨迹名。"),
    (("Metadata SDK did not report all track statuses", "Metadata SDK did not produce a valid summary file",
      "Metadata SDK summary has an unsupported schema", "Metadata adapter has an unsupported protocol",
      "Invalid SDK field:", "Invalid SDK gyro time origin"),
     "metadata_format_invalid", "元数据适配器返回的数据格式不符合预期，请核对适配器与 SDK 版本。"),
    (("Required executable is missing or not executable:", "Original MediaSDK executable is missing or not executable;"),
     "executable_missing", "处理程序不存在或没有执行权限，请检查容器镜像、SDK 文件和执行权限。"),
    (("SDK models directory does not exist:",), "sdk_models_missing", "SDK 模型目录不存在，请核对官方包和配置路径。"),
    (("Original MediaSDK did not report its expected command interface;",
      "SDK command line did not report the expected -inputs / -output interface"),
     "sdk_interface_invalid", "MediaSDK 未提供预期命令行接口，请核对 SDK 版本和运行依赖。"),
    (("Archive mount is missing:", "Archive directory does not exist:"),
     "archive_missing", "原片目录不存在或未正确挂载，请检查档案目录配置和容器挂载。"),
    (("Another converter is using this /data directory",), "instance_conflict",
     "另一个程序实例正在使用同一 data 目录，请先停止已有实例。"),
)


def explain_error(raw):
    """Translate a known failure without changing its original audit evidence."""
    original = _text(raw)
    text = original.strip()

    def result(code, message):
        return {"error": message, "error_code": code, "error_raw": original}

    if _chinese_message(text):
        return result("reported_error", text)
    if re.fullmatch(r"\[Errno 39\] Directory not empty: ['\"](?:[^'\"]*/\.insta360-openconverter/)?"
                    r"(?:report|preview|metadata|metadata-read|metadata-json|atomic|sdk-media|sdk-metadata)-[0-9a-f]{16}['\"]", text):
        return result("workspace_cleanup_failed",
                      "临时工作目录未能清空，目录已保留待后续清理。请查看对应运行日志。")
    for pattern, code, message in (
            (r"(?:\[Errno 13\]\s+Permission denied|(?:^|:\s|\n)Permission denied(?:[:\s]|$))",
             "permission_denied", "文件或目录权限不足，请检查运行用户对相关路径的读写、执行权限。"),
            (r"(?:\[Errno 30\]\s+Read-only file system|(?:^|:\s|\n)Read-only file system(?:[:\s]|$))",
             "read_only_filesystem", "目标文件系统为只读，请检查原片目录和 data 目录的写入权限及挂载方式。"),
            (r"(?:\[Errno 28\]\s+No space left on device|(?:^|:\s|\n)No space left on device(?:[:\s]|$))",
             "no_space", "目标设备空间不足，无法继续写入。请检查磁盘容量、配额和可用 inode。"),
            (r"^\[Errno 2\]\s+No such file or directory", "path_missing",
             "处理所需的文件或目录不存在，请核对原片、SDK 和容器挂载路径。"),
            (r"^\[Errno 17\]\s+File exists", "destination_exists",
             "目标位置已存在文件，程序已停止覆盖。请检查同名文件或并发写入。")):
        if re.search(pattern, text):
            return result(code, message)
    library = re.search(r"(?:error while loading shared libraries:\s*|is missing shared library\s+)"
                        r"([^\s:]+)", text)
    if library:
        name = library.group(1).rstrip(".")
        if name.startswith(("libcuda.", "libnvidia-", "libnvcuvid.")):
            return result("gpu_library_missing",
                          f"运行环境缺少 NVIDIA 驱动库 {name}。请检查宿主机驱动、NVIDIA Container Toolkit 和 GPU 分配。")
        return result("runtime_library_missing",
                      f"运行环境缺少动态库 {name}。请更新容器镜像或检查动态库搜索路径。")
    if "InsMetaDataSDK Parse failed" in text:
        return result("metadata_sdk_parse_failed",
                      "官方元数据 SDK 无法解析原文件，请检查文件是否上传完整及该机型、格式是否受 SDK 支持。")
    if re.search(r"no CUDA-capable device|CUDA driver version is insufficient|"
                 r"No CUDA devices|CUDA initialization error", text):
        return result("gpu_initialization_failed",
                      "GPU 初始化失败，请检查选中的 NVIDIA 设备、驱动和容器 GPU 配置。")
    if re.search(r"\bCUDA_ERROR_[A-Z_]+\b", text):
        return result("gpu_processing_failed",
                      "GPU 处理发生错误，请查看原始 CUDA 错误码及对应运行日志。")
    for prefixes, code, message in _MESSAGES:
        if text.startswith(prefixes):
            return result(code, message)
    timeout = re.match(r"Command exceeded (\d+)s:", text)
    if timeout:
        return result("command_timeout", f"外部处理程序超过 {timeout.group(1)} 秒未完成，已终止。请检查运行日志及超时配置。")
    if text == "Timed out waiting for another SDK preparation":
        return result("sdk_lock_timeout", "等待其他实例完成 SDK 准备超时，请检查正在运行的实例和相关日志。")
    if re.search(r"(?:^|\n)\S[^\n]* exited (-?\d+):", text):
        code = re.search(r" exited (-?\d+):", text).group(1)
        return result("command_failed", f"外部处理程序异常退出（退出码 {code}），请查看原始错误及本次运行日志。")
    if text.startswith(("media SDK preparation failed:", "metadata SDK preparation failed:")):
        return result("sdk_preparation_failed", "官方 SDK 准备失败，请根据原始错误检查 SDK 文件、编译工具和容器运行依赖。")
    return result("processing_failed", "文件处理失败，暂未识别具体错误类型。请查看原始错误及对应运行日志。")


def explain_reason(raw):
    """Explain deferred work separately from a processing failure."""
    original = _text(raw)
    text = original.strip()
    if _chinese_message(text):
        message = text
    elif text == "Source is empty or still settling after upload":
        message = "原文件为空或仍在上传稳定等待期，暂缓处理。"
    elif text.startswith("Missing _00_ lens file;"):
        message = "缺少配对的 _00_ 镜头文件，等待原片上传完整。"
    elif text.startswith("Multiple sources would write the same preview:"):
        message = "多个原文件会生成同名预览，请整理命名冲突后重试。"
    elif text.startswith("Multiple sources would write the same metadata:"):
        message = "多个原文件会生成同名元数据，请整理原文件主名冲突后重试。"
    else:
        message = "当前文件暂缓处理，具体原因请查看原始说明。"
    return {"reason": message, "reason_raw": original}
