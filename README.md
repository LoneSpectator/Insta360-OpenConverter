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
| 容器环境 | Docker Engine、Compose v2、NVIDIA Container Toolkit |
| SDK | 官方 Linux x86_64 MediaSDK（拼接）和 InsMetaDataSDK（元数据读取），分别获取并只读挂载 |
| GPU | NVIDIA GPU；驱动需兼容 SDK 包内 CUDA 运行时，具体版本以 SDK 文档为准 |
| 文件系统 | 档案目录可读写，支持硬链接和原子重命名；SDK 目录可读取和执行 |

官方 [Desktop 指南](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/guide.md) 要求 **MediaSDK 3.x 使用独立 GPU**，本项目统一使用 NVIDIA GPU 部署。InsMetaDataSDK 元数据读取本身不调用 MediaSDK，容器仍使用同一 GPU 配置。离线文件处理不依赖 CameraSDK。

**验证状态**：自动化测试覆盖扫描、文件保护、SDK 字段映射、轨迹保存和 JPG/MP4 元数据回读。官方 InsMetaDataSDK 2.0.2 用于本地编译、加载和错误路径验证；公开 CI 的 SDK 调用使用测试替身。真实相机数据和 MediaSDK 拼接、机型/LRV 兼容性仍需原片验证。

## 部署

### 准备 SDK 和配置

从 [Insta360 开发者平台](https://www.insta360.com/sdk/apply) 获取 Linux x86_64 MediaSDK 和 InsMetaDataSDK。NVIDIA GPU 驱动安装在宿主机，通过 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 提供给容器；保留 MediaSDK 包中完整的运行库和模型。

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

元数据适配器使用项目内的 C++ 源码与官方 InsMetaDataSDK 头文件、动态库编译。可在 Ubuntu 22.04、GCC 11 或更新的兼容环境中执行：

```bash
python3 scripts/prepare_metadata_sdk.py \
    /path/to/InsMetaDataSDK-linux64.tar.gz \
    ./sdk/metadata
```

建议使用 GHCR 镜像内的编译环境，以匹配运行镜像。拉取镜像后，挂载归档所在目录与 SDK 输出目录：

```bash
docker pull ghcr.io/lonespectator/insta360-openconverter:latest
docker run --rm --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$PWD/sdk,dst=/sdk" \
    --mount "type=bind,src=/path/to/sdk-archives,dst=/archives,readonly" \
    --entrypoint python3 ghcr.io/lonespectator/insta360-openconverter:latest \
    /app/scripts/prepare_metadata_sdk.py \
    /archives/InsMetaDataSDK-linux64.tar.gz /sdk/metadata
```

归档路径替换为实际位置。准备结果包含 `bin/insta360-metadata`、官方头文件和 `lib/libInsMetaDataSDK.so`，并用 `build.json` 记录归档、适配器源码和二进制的 SHA256。SDK 及编译结果留在本地 `sdk/metadata`，由 Compose 只读挂载，不参与公开镜像构建。

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

### 拉取镜像和启动

默认使用 GitHub Actions 自动构建并发布的 `ghcr.io/lonespectator/insta360-openconverter:latest` 镜像。所有部署命令使用仓库中的 `docker-compose.yml`：

```bash
docker compose pull converter
# 检查依赖、SDK 动态库和命令行接口。
docker compose run --rm converter doctor
# 扫描一遍；首次部署建议使用小规模测试档案。
docker compose run --rm converter once
docker compose up -d --force-recreate
```

| 命令 | 行为 |
| --- | --- |
| `doctor` | 检查依赖和 SDK 命令行接口，不执行素材拼接 |
| `once` | 扫描一遍后退出；存在失败或等待中的文件时返回 1，全部完成时返回 0 |
| `watch` | 持续扫描；单文件失败不影响其他任务，默认 300 秒后重试 |
| `metadata <文件路径>` | 只提取元数据和传感器轨迹，不调用 MediaSDK，不生成预览 |

依赖或配置无效时启动失败。`SIGTERM/SIGINT` 会终止当前 SDK 子进程组并清理本次临时文件，不发布半成品。首次部署需检查测试档案的画面、声音、拍摄时间与 GPS，再接入完整档案。

修改配置或 GPU 设备后，重新运行上述 `up -d --force-recreate` 命令生效；仅修改配置无需重建镜像。

独立元数据读取示例（使用同一容器配置，不调用 MediaSDK）：

```bash
docker compose run --rm converter metadata /photo/VID_sample.insv
```

结果保存在 `/data/metadata`，完成日志中的 `snapshot` 和 `sdk_directory` 分别指向汇总 JSON 与原始轨迹目录。源文件解析失败时返回错误，不会静默使用 ExifTool 代替官方 SDK。

### 转换配置

配置文件为 `/data/config.toml`，完整示例见 [config.example.toml](config.example.toml)。

升级时，请从已有配置中删除已移除的 `sdk.mode` 和 `sdk.cpu_compatible`，避免加载未知配置项失败。

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
| `metadata.reader` | `/data/sdk/metadata/bin/insta360-metadata` | 官方元数据 SDK 的本地适配器 |
| `metadata.library_dirs` | `["/data/sdk/metadata/lib"]` | 元数据 SDK 运行库目录 |

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
    metadata/<任务指纹>-<输入序号>.json         # SDK 与标准元数据汇总
    metadata/<任务标识>.sdk-<唯一ID>/
        sdk.json                              # 官方 SDK 原始字段、轨迹数量及可用状态
        gps.jsonl                             # GPS 原始轨迹
        gyro.jsonl                            # 陀螺仪与加速度
        exposure.jsonl                        # 曝光与快门速度
        timelapse.jsonl                       # 延时摄影时间戳
    log/<启动时间带时区>-<PID>.log             # 每次启动新建，JSON Lines
    sdk/media/                              # 只读挂载的 SDK
    sdk/metadata/                           # 只读挂载的元数据 SDK 与适配器
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

源文件的相机型号、固件、序列号和传感器轨迹通过 **官方 InsMetaDataSDK** 读取。预览优先使用 SDK 相机字段和首个有效 GPS 点，按南北/东西标记处理坐标符号；完整原始轨迹单独保存为 JSON Lines，记录 SHA256、数量和 SDK 可用状态，避免日志输出逐帧数据。

ExifTool 补充 SDK 未公开的标准拍摄时间等字段，复制目标格式支持的 EXIF/XMP/IPTC 或 QuickTime 字段，并写入与回读核对预览的拍摄时间、相机信息、GPS 和全景标记。尺寸、方向与全景几何字段更新为预览实际值。MP4 保留可复制的 QuickTime 日期，拍摄时间同时写到 XMP；文件 mtime 跟随第一原片。

InsMetaDataSDK 的 `GetFirstTimeStamp()` 是陀螺仪时间基准，不能作为拍摄日期。SDK 2.0.2 的 GPS `timestampMs` 字段名与头文件的秒单位注释存在歧义，因此轨迹保留原值，不猜测时间单位或转换成 UTC 日期。缺失的拍摄时间会记入日志，不从文件名或目录名推断。

**元数据保留范围**：SDK 暴露的 GPS、陀螺仪、曝光、延时轨迹完整保存在单独的快照中，不保证嵌入 JPG/MP4。SDK 与 ExifTool 均未公开的防抖、深度、镜头映射和未知二进制数据仍保留在原片中。SDK 解析失败与单个轨迹不存在分别记录，不会把缺失数据写成有效的零值。

## GitHub 自动构建和 GHCR

工作流见 [`.github/workflows/container.yml`](.github/workflows/container.yml)。PR 运行测试和构建，不登录/推送；`main` 更新、`v*` 标签和手动触发在测试通过后构建 **linux/amd64** 并发布。测试同时覆盖开发用 Python 3.12 和镜像内 Python 3.10/系统媒体工具；镜像内测试使用非 root 用户和只读根文件系统，成功后才推送。只使用 GitHub 提供的 `GITHUB_TOKEN`，不需要将 SDK 或个人 PAT 放进 Actions secrets。

镜像地址：`ghcr.io/lonespectator/insta360-openconverter`。`main` 发布 `latest`、`main` 和 `sha-…`；`v0.1.0` 发布 `0.1.0`、`0.1` 和 `sha-…`。生产部署建议固定版本或镜像 digest。仓库需允许 GitHub Actions，包策略需允许工作流写入 GHCR。公开包支持匿名拉取，私有包需要先登录 GHCR。

Compose 默认使用 GHCR 的 `latest` 镜像，也可在 `.env` 中通过 `CONVERTER_IMAGE` 指定版本或镜像 digest：

```dotenv
CONVERTER_IMAGE=ghcr.io/lonespectator/insta360-openconverter:latest
```

然后部署，无需本机构建：

```bash
docker compose pull converter
docker compose run --rm converter doctor
docker compose up -d
```

镜像内包含应用和公共工具，SDK 从宿主机 `sdk/media` 挂载。SDK 压缩包、动态库、模型和授权文件不纳入公开仓库或默认镜像。

## 开发与验证

格式约定见 [.editorconfig](.editorconfig)，统一使用 4 空格缩进。Compose 配置统一命名为 `docker-compose.yml`；JSON Lines 日志与轨迹保持一行一条记录。

Python >= 3.10（3.10 还需 `tomli`，镜像通过系统包安装），运行时不需要 PyPI 依赖。编译元数据适配器需 C++17 编译器；容器已包含 GCC。在 Linux 安装 `ffmpeg` 和 `libimage-exiftool-perl` 后：

```bash
python3 -m unittest discover -s tests -v
python3 -m converter --help
# 宿主机运行时：配置内路径需改为实际绝对路径。
python3 -m converter --data-dir /绝对路径/data doctor
python3 -m converter --data-dir /绝对路径/data once
```

测试覆盖镜头配对、孤立镜头、符号链接、输出冲突、上传等待、幂等、源文件变更、文件保护、重试、超时、中断和实例锁，以及真实媒体工具的时间、GPS、360 UUID 与音轨校验。自动化测试使用 SDK 测试替身，实际拼接质量与机型兼容性需要使用官方 SDK 和相机原片单独验证。

本地提供官方元数据适配器时，可通过 `INSTA360_METADATA_READER=/绝对路径/bin/insta360-metadata` 启用真实 SDK 加载与非法输入测试。公开 CI 不保存 SDK；未提供该变量时，这项私有依赖测试明确跳过。

## 参考文档

官方参考：[MediaSDK 仓库](https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp)、[MediaSDK 接口文档](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/media.md)。命令行参数和包布局以实际安装版本自带的 README 和 `MediaSDKTest -help` 为准。

## AI 编码声明

本项目由 AI 编码。项目的应用代码、自动化测试、容器部署配置、GitHub Actions 工作流及文档等工作，主要由 **OpenAI Codex（GPT-6）** 根据项目发布者的需求生成、修改和整理。

项目发布者负责对 AI 生成的内容进行实际测试、审核、部署与后续维护。

## 许可证

项目采用 [AGPL-3.0](LICENSE) 许可证。Insta360 SDK 的使用和分发遵循其独立许可条款。
