# Insta360 OpenConverter

面向 Linux x86_64 的 Insta360 全景预览转换服务。通过官方 **Insta360 MediaSDK** 递归处理指定目录中的 INSP、INSV 和 LRV，在原目录生成 `_preview.jpg` / `_preview.mp4`、处理说明和文件操作审计日志，保留原始文件。

## 功能

- 递归扫描多层指定目录，支持中文、空格和大小写扩展名。
- INSP 转 JPG，INSV/LRV 转 MP4；自动配对双镜头视频文件。
- 输出 2:1 全景预览，写入 GPano / GSpherical 标记，复制目标格式支持的拍摄元数据。
- 持久化处理状态，跳过未变文件，等待上传完成并重试失败任务。
- 在原目录保存处理说明 TXT，在 `/data/log` 保存每次运行的详细审计日志。
- 通过 Docker Compose 部署，支持指定 NVIDIA GPU 索引或 UUID。

## 运行要求与兼容性

| 项目 | 要求或状态 |
| --- | --- |
| 平台 | Linux x86_64；容器基于 Ubuntu 22.04，镜像架构为 `linux/amd64` |
| 容器环境 | Docker Engine、Compose v2；GPU 部署需 NVIDIA Container Toolkit |
| SDK | 官方 Linux x86_64 MediaSDK，包含 `MediaSDKTest`、运行库和模型，独立获取并挂载 |
| GPU | 默认使用 NVIDIA GPU；驱动需兼容 SDK 包内 CUDA 运行时，具体版本以 SDK 文档为准 |
| CPU | 尚未完成兼容性验证；需要支持 CPU 的官方 SDK 和对应命令行接口 |
| 文件系统 | 档案目录可读写，支持硬链接和原子重命名；SDK 目录可读取和执行 |

官方 [Desktop 指南](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/guide.md) 要求 **MediaSDK 3.x 使用独立 GPU**，软件编解码选项不构成纯 CPU 拼接支持。离线处理不依赖 CameraSDK；当前元数据处理使用 ExifTool，尚未集成 InsMetaDataSDK。

**验证状态**：自动化测试覆盖扫描、重试、文件保护、JPG/MP4 元数据读写及容器运行。SDK 调用使用测试替身；真实 SDK 拼接、机型/LRV 兼容性与 GPU 执行尚未完成原片验证。

## 部署

### 准备 SDK 和配置

从 [Insta360 开发者平台](https://www.insta360.com/sdk/apply) 获取 Linux x86_64 MediaSDK。NVIDIA GPU 驱动安装在宿主机，通过 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 提供给容器；保留 SDK 包中完整的运行库和模型。

在本仓库根目录执行：

```bash
mkdir -p data sdk
cp config.example.toml data/config.toml
cp .env.example .env

# 在 Linux 宿主机准备 SDK；需要 Python >= 3.10 和 dpkg-deb。
python3 scripts/prepare_sdk.py \
  /path/to/MediaSDK-linux64.tar.gz \
  ./sdk/media
```

将命令中的归档路径替换为实际下载路径。脚本在临时目录中解包；对 `.deb` 使用 `dpkg-deb --extract` 提取文件，不执行安装脚本。识别到 `bin/MediaSDKTest`、`lib/`、`bin/models/` 后，将完整 SDK 整理到 `sdk/media`，不会覆盖已有目标目录。

包结构不匹配时脚本会退出，需按 SDK 自带的 README 检查布局。有官方校验值时可追加 `--sha256 <官方校验值>`；默认输出的本地 SHA256 用于记录归档指纹。

编辑 `.env`：

```dotenv
PHOTO_DIR=/srv/photo
PUID=1000
PGID=1000
NVIDIA_GPU_ID=0
```

`PHOTO_DIR` 是宿主机档案目录，挂载到容器 `/photo`。`PUID/PGID` 对应运行用户，可通过 `id -u` / `id -g` 查询。该用户需能读写档案目录与本地 `data/`，并读取和执行 `sdk/media/`。SDK 目录以只读方式挂载。

### 指定 GPU

通过 `nvidia-smi -L` 查看设备，在 `.env` 中将 `NVIDIA_GPU_ID` 设置为索引（如 `1`）或完整 UUID（如 `GPU-xxxxxxxx-…`）。推荐使用不受索引排序影响的 UUID。Compose 通过 `device_ids` 只分配选中的一张 GPU。

### 构建和启动

```bash
docker compose -f compose.yaml -f compose.gpu.yaml build
# 检查依赖、SDK 动态库和命令行接口。
docker compose -f compose.yaml -f compose.gpu.yaml run --rm converter doctor
# 扫描一遍；首次部署建议使用小规模测试档案。
docker compose -f compose.yaml -f compose.gpu.yaml run --rm converter once
docker compose -f compose.yaml -f compose.gpu.yaml up -d --force-recreate
```

| 命令 | 行为 |
| --- | --- |
| `doctor` | 检查依赖和 SDK 命令行接口，不执行素材拼接 |
| `once` | 扫描一遍后退出；存在失败或等待中的文件时返回 1，全部完成时返回 0 |
| `watch` | 持续扫描；单文件失败不影响其他任务，默认 300 秒后重试 |

依赖或配置无效时启动失败。`SIGTERM/SIGINT` 会终止当前 SDK 子进程组并清理本次临时文件，不发布半成品。首次部署需检查测试档案的画面、声音、拍摄时间与 GPS，再接入完整档案。

修改配置或 GPU 设备后，重新运行上述 `up -d --force-recreate` 命令生效；仅修改配置无需重建镜像。

### 转换配置

配置文件为 `/data/config.toml`，完整示例见 [config.example.toml](config.example.toml)。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `photo_root` | `/photo` | 容器内档案根目录，需与挂载位置一致 |
| `scan_interval` | `60` | 扫描间隔，秒 |
| `settle_seconds` | `120` | 源文件最后修改后的等待时间，秒 |
| `retry_seconds` | `300` | 失败任务重试间隔，秒 |
| `command_timeout` | `7200` | 单条外部命令超时，秒 |
| `width` / `height` | `3840` / `1920` | 预览尺寸，两边均为偶数，宽高比 2:1 |
| `bitrate` | `20000000` | 视频输出码率，bps |
| `codec` | `h264` | 视频编码，支持 `h264` / `h265` |
| `sdk.mode` | `gpu` | SDK 执行模式 |

CPU 适配需要支持 CPU 的官方 SDK，并验证其软件编解码、CUDA 开关和 CPU 渲染参数。完成适配后设置 `sdk.mode = "cpu"`、`sdk.cpu_compatible = true`，仅使用 `compose.yaml` 部署。该配置开关不提供 SDK 本身缺失的 CPU 能力。

## 目录和处理规则

```text
/photo/
  活动档案/20261102.示例事件/
    IMG_20261102_123456_00_001.insp          # 原片保留
    IMG_20261102_123456_00_001_preview.jpg
    IMG_20261102_123456_00_001.insp_preview.txt
    VID_20261102_123456_00_002.insv          # 双文件原片
    VID_20261102_123456_10_002.insv
    VID_20261102_123456_00_002_preview.mp4   # 一组只生成一份
    VID_20261102_123456_00_002.insv_preview.txt
    VID_20261102_123456_10_002.insv_preview.txt
/data/
  config.toml
  state.sqlite3                           # 持久化处理状态和生成物归属
  converter.lock
  health.json                             # 最近完整扫描的结果及时间
  metadata/<任务指纹>-<输入序号>.json         # ExifTool 可读元数据快照
  log/<启动时间带时区>-<PID>.log             # 每次启动新建，JSON Lines
  sdk/media/                              # 只读挂载的 SDK
```

- 扫描整个 `photo_root`，包括多层事件目录；`YYYYMMDD.事件名称` 用于说明文件中的事件识别，不限制更深层文件。大小写扩展名和中文/空格路径均支持。不跟随符号链接，也不扫描本工具的临时目录。
- INSP → JPG，INSV/LRV → MP4；同目录、同前缀、同序号、同扩展名的 `_00_` + `_10_` 文件成组输入 SDK。单文件/双视频轨素材直接交给 SDK 判断。孤立 `_10_` 等待配对。照片逐张处理，不擅自合并 HDR 曝光组。
- LRV 作为独立输入处理；缺少 SDK 所需拼接信息时记录失败。同一拍摄产生的 VID 和 LRV 默认各有一份预览。
- 上传时默认等待最后修改至少 120 秒；转码前记录大小、mtime、inode、SHA256，发布前再检查源文件属性。建议上传到扫描根目录外，完成后原子移入，避免上传过程中长时间暂停导致提前处理。
- 先在同目录临时子目录中生成，检查尺寸、音轨、时长和关键元数据，然后同步落盘并原子发布。底层文件系统需支持硬链接（首次无覆盖发布）和原子重命名。中断后的残余 `.insta360-preview-*` 目录不会作为原片扫描，可结合日志在服务停止后人工清理。
- 重复扫描通过 SQLite 跳过未变文件；源文件或转换配置改变后重新生成本工具拥有的预览。用户原有预览、被用户修改的预览和同名非本工具 TXT 不会被覆盖。删除预览可触发重建；**保留并备份 `/data/state.sqlite3`**，否则服务无法确认既有预览的归属，会拒绝覆盖。
- 日志包含每条外部命令及退出码、SDK stdout/stderr、原片读取/哈希、元数据快照、临时文件、发布/替换/清理、等待/跳过/失败原因。日志仅追加，不自动轮转或删除；可由宿主机配置保留策略。日志和快照可能包含拍摄时间、GPS 和原始路径，请按档案权限管理。运行中更改 GPU 后需重建服务，不能只重启原容器。

## 元数据语义

照片写入 Google **GPano**，视频写入视频轨内的 **GSpherical UUID**（Spherical Video v1）。全景画面为 2:1 单目等距柱状投影，播放效果取决于播放器对全景元数据的支持。

ExifTool 复制目标格式支持的 EXIF/XMP/IPTC 或 QuickTime 字段，再显式保留并回读核对可识别的拍摄时间（包含已有时区）、相机品牌/型号、GPS 经纬度；尺寸、方向和全景几何字段更新为预览实际值。MP4 保留可复制的 QuickTime 创建/修改日期，拍摄时间同时写到 XMP。预览文件 mtime 跟随第一原片。原片没有可识别拍摄时间时明确记日志，不从事件名称、文件名或当前时区猜测拍摄时间。

**元数据保留范围**：原始逐帧陀螺仪、防抖、深度、镜头映射、GPS 轨迹和未知二进制块等不保证嵌入 JPG/MP4；ExifTool 不可识别的数据也不在 JSON 快照中。完整原始元数据仍保留在原片中。

## GitHub 自动构建和 GHCR

工作流见 [`.github/workflows/container.yml`](.github/workflows/container.yml)。PR 运行测试和构建，不登录/推送；`main` 更新、`v*` 标签和手动触发在测试通过后构建 **linux/amd64** 并发布。测试同时覆盖开发用 Python 3.12 和镜像内 Python 3.10/系统媒体工具；镜像内测试使用非 root 用户和只读根文件系统，成功后才推送。只使用 GitHub 提供的 `GITHUB_TOKEN`，不需要将 SDK 或个人 PAT 放进 Actions secrets。

镜像地址：`ghcr.io/lonespectator/insta360-openconverter`。`main` 发布 `latest`、`main` 和 `sha-…`；`v0.1.0` 发布 `0.1.0`、`0.1` 和 `sha-…`。生产部署建议固定版本或镜像 digest。仓库需允许 GitHub Actions，包策略需允许工作流写入 GHCR。公开包支持匿名拉取，私有包需要先登录 GHCR。

自动构建完成后，在 `.env` 设置：

```dotenv
CONVERTER_IMAGE=ghcr.io/lonespectator/insta360-openconverter:latest
```

然后部署，无需本机构建：

```bash
docker compose -f compose.yaml -f compose.gpu.yaml pull converter
docker compose -f compose.yaml -f compose.gpu.yaml run --rm --no-build converter doctor
docker compose -f compose.yaml -f compose.gpu.yaml up -d --no-build
```

镜像内包含应用和公共工具，SDK 从宿主机 `sdk/media` 挂载。SDK 压缩包、动态库、模型和授权文件不纳入公开仓库或默认镜像。

## 开发与验证

Python >= 3.10（3.10 还需 `tomli`，镜像通过系统包安装），运行时不需要 PyPI 依赖。在 Linux 安装 `ffmpeg` 和 `libimage-exiftool-perl` 后：

```bash
python3 -m unittest discover -s tests -v
python3 -m converter --help
# 宿主机运行时：配置内路径需改为实际绝对路径。
python3 -m converter --data-dir /绝对路径/data doctor
python3 -m converter --data-dir /绝对路径/data once
```

测试覆盖镜头配对、孤立镜头、符号链接、输出冲突、上传等待、幂等、源文件变更、文件保护、重试、超时、中断和实例锁，以及真实媒体工具的时间、GPS、360 UUID 与音轨校验。自动化测试使用 SDK 测试替身，实际拼接质量与机型兼容性需要使用官方 SDK 和相机原片单独验证。

## 参考文档

官方参考：[MediaSDK 仓库](https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp)、[MediaSDK 接口文档](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/media.md)。命令行参数和包布局以实际安装版本自带的 README 和 `MediaSDKTest -help` 为准。

## AI 编码声明

本项目由 AI 编码。项目的应用代码、自动化测试、容器部署配置、GitHub Actions 工作流及文档等工作，主要由 **OpenAI Codex（GPT-6）** 根据项目发布者的需求生成、修改和整理。

项目发布者负责对 AI 生成的内容进行实际测试、审核、部署与后续维护。

## 许可证

项目采用 [AGPL-3.0](LICENSE) 许可证。Insta360 SDK 的使用和分发遵循其独立许可条款。
