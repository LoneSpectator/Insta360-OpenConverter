"""Expose failed unittest output in Actions summaries and annotations."""

import html
import os
from pathlib import Path
import sys


def failure_details(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if len(line) >= 10 and set(line) == {"="}:
            return "\n".join(lines[index:])
    return "\n".join(lines[-100:])


def annotation_escape(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main():
    log = Path(sys.argv[1])
    if not log.is_file():
        print(f"Test log was not created: {log}")
        return
    details = failure_details(log.read_text(encoding="utf-8", errors="replace"))
    if not details.strip():
        details = "测试进程没有输出错误详情，请查看对应步骤与完整日志。"
    # Keep annotations small; the artifact retains the complete output.
    excerpt = details[:16000]
    if len(details) > len(excerpt):
        excerpt += "\n\n（其余内容见完整测试日志。）"
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("## 测试失败详情\n\n")
            stream.write("完整日志保存在本次运行的测试日志附件中。\n\n")
            stream.write("<pre>" + html.escape(excerpt) + "</pre>\n")
    print("::error title=测试失败详情::" + annotation_escape(excerpt))


if __name__ == "__main__":
    main()
