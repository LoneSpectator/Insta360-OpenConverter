# Insta360 OpenConverter

面向 Linux x86_64 的 Insta360 全景预览转换服务。通过官方 **Insta360 MediaSDK** 递归处理指定目录中的 INSP、INSV 和 LRV，在原目录生成 `_preview.jpg` / `_preview.mp4`、元数据 JSON 和处理说明，在 `/data/log` 保存文件操作审计日志，保留原始文件。

## 功能

- 递归扫描多层指定目录，支持中文、空格和大小写扩展名。
- INSP 转 JPG，INSV/LRV 转 MP4；自动配对双镜头视频文件。
- 输出 2:1 全景预览，写入 GPano / GSpherical 标记，复制目标格式支持的拍摄元数据。
- 在每个源文件旁保存 `<源文件主名>_metadata.json`，合并标准元数据、官方 SDK 字段和完整传感器轨迹。
- 默认按源数据确定输出尺寸、视频编码和码率预算，校验输出帧率、位深及已知色彩信息。
- 持久化处理状态，跳过未变文件，等待上传完成并重试失败任务。
- 每个源文件所在目录共用一份 `insta360-preview-report.txt`，在 `/data/log` 保存每次运行的详细审计日志。
- 通过 Docker Compose 部署，支持指定 NVIDIA GPU 索引或 UUID。
- 启动时自动准备本地官方 SDK，校验缓存并在需要时重建。

## 运行要求与兼容性

| 项目 | 要求或状态 |
| --- | --- |
| 平台 | Linux x86_64；容器基于 Ubuntu 22.04，镜像架构为 `linux/amd64` |
| 容器环境 | Docker Engine、Compose v2、NVIDIA Container Toolkit |
| SDK | 官方 Linux x86_64 MediaSDK（拼接）和 InsMetaDataSDK（元数据读取）压缩包，放在宿主机 `sdk/` |
| GPU | NVIDIA GPU；驱动需兼容 SDK 包内 CUDA 运行时，具体版本以 SDK 文档为准 |
| 文件系统 | 档案目录可读写，支持硬链接和原子重命名；`data/` 可读写并允许执行 SDK 缓存，`sdk/` 归档可读 |

官方 [Desktop 指南](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/guide.md) 要求 **MediaSDK 3.x 使用独立 GPU**，本项目统一使用 NVIDIA GPU 部署。InsMetaDataSDK 元数据读取本身不调用 MediaSDK，容器仍使用同一 GPU 配置。离线文件处理不依赖 CameraSDK。

**验证状态**：自动化测试覆盖扫描、文件保护、SDK 字段映射、轨迹保存和 JPG/MP4 元数据回读。官方 InsMetaDataSDK 2.0.2 用于本项目适配器编译、动态库加载和错误路径验证；公开 CI 的 SDK 调用使用测试替身。真实相机数据和 MediaSDK 拼接、机型/LRV 兼容性仍需原片验证。

## 部署

从 [Insta360 开发者平台](https://www.insta360.com/sdk/apply) 获取 Linux x86_64 MediaSDK 和 InsMetaDataSDK。NVIDIA GPU 驱动安装在宿主机，通过 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 提供给容器。部署时宿主机无需安装 Python 或 GCC。

### 1. 初始化配置

在本仓库根目录执行：

```bash
mkdir -p data sdk
cp config.example.toml data/config.toml
cp .env.example .env
```

### 2. 放置官方 SDK 压缩包

将 MediaSDK 和 InsMetaDataSDK 官方压缩包放到仓库根目录的 `sdk/` 中，保留下载时的原始文件名即可。以下是文件名匹配示例，兼容 `.tar.gz` 和 `.tar_时间戳.gz`：

```text
sdk/
    MediaSDK*.tar*.gz
    InsMetaDataSDK*.tar*.gz
```

程序从官方文件名前缀识别 SDK 类型，并在准备时检查包内容，不依赖固定版本号或下载时间戳。同类存在多个版本时，选择可解析的最高语义版本；最高版本有多个候选包，或无法明确判断版本时，报告冲突并提示整理归档。

Compose 将 `./sdk` 只读挂载到容器 `/sdk`。解包、目录整理和本项目元数据适配器的编译均由容器自动完成，结果保存在 `/data/sdk`。

### 3. 配置档案目录和 GPU

编辑第 1 步创建的 `.env`：

```dotenv
PHOTO_DIR=/srv/photo
PUID=1000
PGID=1000
NVIDIA_GPU_ID=0
```

`PHOTO_DIR` 是宿主机档案目录，挂载到容器 `/photo`。`PUID/PGID` 对应运行用户，可通过 `id -u` / `id -g` 查询。该用户需能读写档案目录与本地 `data/`，并读取 `sdk/` 中的官方压缩包。

通过 `nvidia-smi -L` 查看设备，在 `.env` 中将 `NVIDIA_GPU_ID` 设置为索引（如 `1`）或完整 UUID（如 `GPU-xxxxxxxx-…`）。推荐使用不受索引排序影响的 UUID。Compose 通过 `device_ids` 只分配选中的一张 GPU。

### 4. 拉取镜像和启动

默认使用 GitHub Actions 自动构建并发布的 `ghcr.io/lonespectator/insta360-openconverter:latest` 镜像。所有部署命令使用仓库中的 `docker-compose.yml`：

```bash
docker compose pull converter
docker compose up -d --force-recreate
```

首次启动会自动解包 MediaSDK 的程序、运行库和模型，并使用官方 InsMetaDataSDK 头文件及预编译动态库，编译本项目输出 JSON 的 C++ 元数据适配器。镜像已包含所需 Python、`dpkg-deb` 和 GCC。

每次启动重新计算 SDK 归档、构建源码和缓存文件的 SHA256，并与构建清单核对；归档更新或本工具管理的缓存缺失、损坏时自动重建。缓存、编译临时文件和准备状态均保存在 `/data/sdk`。官方压缩包本身损坏时，替换为完整官方包后重新启动。

`doctor`、`once`、`watch` 和 `metadata` 都会先完成所需 SDK 的自动准备与校验；`metadata` 仅准备元数据 SDK。可使用 `docker compose run --rm converter doctor` 检查依赖和 SDK 接口，使用 `docker compose run --rm converter once` 执行一次扫描。

一次性命令与后台服务共用 `/data` 时，先执行 `docker compose stop converter`，完成后用 `docker compose up -d` 恢复服务。同一数据目录只允许一个实例运行。

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

结果保存在源文件同目录的 `VID_sample_metadata.json` 中，与自动转换使用相同命名规则。完成日志中的 `snapshot` 指向该文件。源文件解析失败时返回错误，不会静默使用 ExifTool 代替官方 SDK。

### 转换配置

配置文件为 `/data/config.toml`，完整示例见 [config.example.toml](config.example.toml)。

升级时，请从已有配置中删除已移除的 `sdk.mode` 和 `sdk.cpu_compatible`，避免加载未知配置项失败。

默认优先保留源画质，不统一缩放到固定分辨率或限制为固定码率。已有配置若仍指定 `3840` / `1920`、`20000000`、`h264` 和 `flowstate = true`，需分别改为 `0` / `0`、`0`、`source` 和 `false` 才能采用新默认。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `photo_root` | `/photo` | 容器内档案根目录，需与挂载位置一致 |
| `scan_interval` | `60` | 扫描间隔，秒 |
| `settle_seconds` | `120` | 源文件最后修改后的等待时间，秒 |
| `retry_seconds` | `300` | 失败任务重试间隔，秒 |
| `command_timeout` | `7200` | 单条外部命令超时，秒 |
| `width` / `height` | `0` / `0` | 自动推导源尺寸；也可同时指定偶数尺寸，宽高比须为 2:1 |
| `bitrate` | `0` | 按源主视频流合计码率确定编码预算；也可指定 bps |
| `codec` | `source` | 跟随源 H.264 / H.265；可显式选择 `h264` / `h265` |
| `flowstate` | `false` | 默认不额外启用 FlowState 防抖 |
| `sdk.source_root` | `/sdk` | 官方 SDK 压缩包所在目录，默认只读挂载宿主机 `sdk/` |
| `metadata.reader` | `/data/sdk/metadata/bin/insta360-metadata` | 官方元数据 SDK 的本地适配器 |
| `metadata.library_dirs` | `["/data/sdk/metadata/lib"]` | 元数据 SDK 运行库目录 |

自动尺寸识别支持单个 2:1 主图像/视频流，以及双路同尺寸方形视频流；后者输出为单路宽度的两倍、高度不变。其他镜头布局需显式配置 `width` / `height`，无法确定时记录失败。源视频流缺少码率时，每个输入文件最多使用一次容器总码率估算；仍不可用时交由 SDK 使用源码率。相同码率只代表编码预算，不保证相同画质。

转换后核对尺寸、视频编码、帧率、位深、色度采样和源文件中已知的色彩描述；不符合保留要求的产物不会发布。10-bit 视频使用 H.265，须由 SDK 明确提供 `-enable_10bit`，并通过实际输出检查。源探测结果、导出计划和实际输出参数写入目录处理说明中对应文件条目的 `quality` 字段及审计日志。

全景拼接需要重投影和重新编码，MP4/JPG 输出不属于无损封装转换。程序默认不追加滤镜、降噪、增强或第二次 JPEG 编码；JPEG 质量由 SDK 内部控制，公开 CLI 未提供质量参数。SDK 可能仍执行其内部默认处理，例如部分演示版本默认启用且无法通过 CLI 关闭的 StitchFusion。实际画质和各机型兼容性需使用官方 SDK 与原片验证。

## 目录和处理规则

```text
/photo/
    活动档案/20261102.示例事件/
        insta360-preview-report.txt           # 本目录所有原片的最新处理情况
        IMG_20261102_123456_00_001.insp          # 原片保留
        IMG_20261102_123456_00_001_metadata.json
        IMG_20261102_123456_00_001_preview.jpg
        VID_20261102_123456_00_002.insv          # 双文件原片
        VID_20261102_123456_00_002_metadata.json
        VID_20261102_123456_10_002.insv
        VID_20261102_123456_10_002_metadata.json
        VID_20261102_123456_00_002_preview.mp4   # 一组只生成一份
/sdk/                                       # 宿主机 sdk/，只读挂载
    MediaSDK*.tar*.gz                        # 官方包名匹配示例
    InsMetaDataSDK*.tar*.gz
/data/
    config.toml
    state.sqlite3                           # 持久化处理状态和生成物归属
    converter.lock
    health.json                             # 最近完整扫描的结果及时间
    log/<启动时间带时区>-<PID>.log             # 每次启动新建，JSON Lines
    sdk/                                    # 自动准备缓存、构建清单及临时文件
        media/                              # 解包整理后的 MediaSDK
        metadata/                           # 官方元数据库与本项目适配器
```

- 扫描整个 `photo_root`，包括多层事件目录；`YYYYMMDD.事件名称` 用于说明文件中的事件识别，不限制更深层文件。大小写扩展名和中文/空格路径均支持。不跟随符号链接，也不扫描本工具的临时目录。
- 每个实际包含原片的目录只生成一份 `insta360-preview-report.txt`，按原文件名汇总该目录直属原片的最新状态；再次处理时更新对应条目，保留其他文件的记录。双镜头配对的两个原片各有条目。更深层子目录使用各自的说明文件，不汇入上层目录。
- INSP → JPG，INSV/LRV → MP4；同目录、同前缀、同序号、同扩展名的 `_00_` + `_10_` 文件成组输入 SDK。单文件/双视频轨素材直接交给 SDK 判断。孤立 `_10_` 等待配对。照片逐张处理，不擅自合并 HDR 曝光组。
- 每个原片各有一个 `<源文件主名>_metadata.json`，主名不含原扩展名，例如 `VID_001.insv` 对应 `VID_001_metadata.json`。双文件视频的两个原片分别保存元数据。标准字段、SDK 汇总和四类轨迹合并为一个 JSON，不再另外保留轨迹目录。同目录存在主名相同、扩展名不同的原片时，元数据命名会冲突，程序报告冲突并保护已有文件。
- LRV 作为独立输入处理；缺少 SDK 所需拼接信息时记录失败。同一拍摄产生的 VID 和 LRV 默认各有一份预览。
- 上传时默认等待最后修改至少 120 秒；转码前记录大小、mtime、inode、SHA256，发布前再检查源文件属性。建议上传到扫描根目录外，完成后原子移入，避免上传过程中长时间暂停导致提前处理。
- 先在同目录临时子目录中生成，检查导出画质参数、音轨、时长和关键元数据，然后同步落盘并原子发布。底层文件系统需支持硬链接（首次无覆盖发布）和原子重命名。中断后的残余 `.insta360-preview-*` 目录不会作为原片扫描，可结合日志在服务停止后人工清理。
- 重复扫描通过 SQLite 跳过未变文件；源文件或转换配置改变后重新生成本工具拥有的产物。用户原有文件、被用户修改的预览或元数据 JSON，以及同名非本工具 TXT 不会被覆盖。删除预览可触发重建，缺失的元数据 JSON 会在后续扫描补回；**保留并备份 `/data/state.sqlite3`**，否则服务无法确认既有产物的归属，会拒绝覆盖。
- 日志包含每条外部命令及退出码、SDK stdout/stderr、原片读取/哈希、元数据快照、临时文件、发布/替换/清理、等待/跳过/失败原因。日志仅追加，不自动轮转或删除；可由宿主机配置保留策略。日志和快照可能包含拍摄时间、GPS 和原始路径，请按档案权限管理。运行中更改 GPU 后需重建服务，不能只重启原容器。

## 元数据语义

照片写入 Google **GPano**，视频写入视频轨内的 **GSpherical UUID**（Spherical Video v1）。全景画面为 2:1 单目等距柱状投影，播放效果取决于播放器对全景元数据的支持。

源文件的相机型号、固件、序列号和传感器轨迹通过 **官方 InsMetaDataSDK** 读取。预览优先使用 SDK 相机字段和首个有效 GPS 点，按南北/东西标记处理坐标符号。

每个源文件旁的 `_metadata.json` 同时包含 ExifTool 标准字段、`Insta360SDK` 汇总，以及 GPS、陀螺仪与加速度、曝光、延时摄影轨迹。完整记录保存在 `Insta360SDK.tracks.<轨迹名>.records` 数组中，并保留数量、SDK 可用状态和原始轨迹的 SHA256 等信息。JSON 使用 4 空格缩进，轨迹逐条合并写入，长视频无需一次将全部记录加载到内存。SDK 中间文件仅存于源文件同目录的临时子目录，合并后清理；运行日志不输出逐帧轨迹。

元数据名称均使用不含扩展名的源文件主名。同一原片的元数据采用一种存放形式：单文件为 `<源文件主名>_metadata.json`；需要多个文件时则全部放入 `<源文件主名>_metadata/`，目录外不再另留同名 JSON。当前支持的数据均可合并，因此只生成单个 JSON。若源文件旁已存在对应的 `_metadata` 目录，程序保留该目录并报告冲突，避免两种形式同时出现。

ExifTool 补充 SDK 未公开的标准拍摄时间等字段，复制目标格式支持的 EXIF/XMP/IPTC 或 QuickTime 字段，并写入与回读核对预览的拍摄时间、相机信息、GPS 和全景标记。尺寸、方向与全景几何字段更新为预览实际值。MP4 保留可复制的 QuickTime 日期，拍摄时间同时写到 XMP；文件 mtime 跟随第一原片。

InsMetaDataSDK 的 `GetFirstTimeStamp()` 是陀螺仪时间基准，不能作为拍摄日期。SDK 2.0.2 的 GPS `timestampMs` 字段名与头文件的秒单位注释存在歧义，因此轨迹保留原值，不猜测时间单位或转换成 UTC 日期。缺失的拍摄时间会记入日志，不从文件名或目录名推断。

**元数据保留范围**：SDK 暴露的 GPS、陀螺仪、曝光、延时轨迹完整保存在源文件旁的 JSON 中，不保证嵌入 JPG/MP4。SDK 与 ExifTool 均未公开的防抖、深度、镜头映射和未知二进制数据仍保留在原片中。SDK 解析失败与单个轨迹不存在分别记录，不会把缺失数据写成有效的零值。

旧版本生成的 `/data/metadata` 历史文件和逐文件 `*_preview.txt` 说明不会自动删除。升级后，后续扫描会按新规则在源文件旁生成元数据 JSON，并按实际目录汇总处理说明；确认归档和备份后可自行整理历史文件。

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
docker compose up -d
```

镜像内包含应用、公共工具和适配器源码。宿主机 `sdk/` 中的官方压缩包只读挂载到 `/sdk`，由容器在 `/data/sdk` 自动准备运行文件。SDK 压缩包、动态库、模型和授权文件不纳入公开仓库或默认镜像。

MediaSDK 的命令行程序也会链接图形运行库，镜像包含 GTK 3 和 FreeGLUT，无需启动桌面环境。启动出现 `error while loading shared libraries`（例如缺少 `libgtk-3.so.0`）时，先拉取最新镜像并用 `docker compose up -d --force-recreate` 重建容器。此类报错表示运行环境缺少动态库，需检查镜像依赖或库搜索路径；仅在官方包本身不完整时才需重新获取 SDK。

## 开发与验证

格式约定见 [.editorconfig](.editorconfig)，统一使用 4 空格缩进。Compose 配置统一命名为 `docker-compose.yml`；JSON Lines 审计日志保持一行一条记录。

宿主机直接开发需 Python >= 3.10（3.10 还需 `tomli`），运行时不需要 PyPI 依赖。SDK 自动准备还需 `dpkg-deb` 和兼容运行环境的 C++17 编译器；部署镜像已包含这些工具。在 Linux 安装 `ffmpeg` 和 `libimage-exiftool-perl` 后：

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
