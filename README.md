# Insta360 OpenConverter

面向 Linux x86_64 的 Insta360 全景预览转换服务。通过官方 **Insta360 MediaSDK** 递归处理指定目录中的 INSP、INSV 和 LRV，在原目录生成 `_preview.jpg` / `_preview.mp4`、元数据 JSON 和目录处理报告，并保留原始文件。

[最简部署](#最简部署) · [升级](#升级) · [完整配置](#完整配置) · [手动命令](#手动命令)

## 最简部署

适用于 **Linux x86_64 + NVIDIA GPU**。宿主机需安装 Git、Docker Engine、Compose v2、NVIDIA 驱动和 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。从 [Insta360 开发者平台](https://www.insta360.com/sdk/apply) 获取 Linux x86_64 MediaSDK 和 InsMetaDataSDK 官方压缩包。

以下命令均在**宿主机终端（包括 SSH 终端）**执行。所用账号需能运行 Docker，并对档案目录拥有读写、删除权限。

### 1. 获取部署文件

```bash
git clone https://github.com/LoneSpectator/Insta360-OpenConverter.git
cd Insta360-OpenConverter
mkdir -p data sdk
cp config.example.toml data/config.toml
cp .env.example .env
```

`data/config.toml` 保持默认内容即可。后续命令都在这个包含 `docker-compose.yml` 的目录中执行。

### 2. 放入 SDK 压缩包

将两个官方压缩包放入刚创建的 `sdk/` 目录，保留下载时的文件名：

```text
sdk/
    MediaSDK*.tar*.gz
    InsMetaDataSDK*.tar*.gz
```

容器会自动识别版本、解包 SDK 并编译元数据适配器。宿主机无需安装 Python 或编译器。

### 3. 设置档案路径和运行账号

编辑 `.env`：

```dotenv
PHOTO_DIR=/srv/photo
PUID=1000
PGID=1000
NVIDIA_GPU_ID=0
```

将 `PHOTO_DIR` 改为宿主机实际档案目录。`PUID`、`PGID` 填写上述运行账号的 UID、GID，可分别用 `id -u`、`id -g` 查询；该账号也需能写入本地 `data/`、读取 `sdk/`。默认使用 GPU `0`，其他设备的选择方式见[完整配置](#完整配置)。其余设置保持默认。

### 4. 拉取镜像并启动

```bash
docker compose pull
docker compose up -d
```

默认使用 GitHub Actions 发布的 `ghcr.io/lonespectator/insta360-openconverter:latest` 镜像。容器自动运行后台扫描服务：准备 SDK 后立即扫描 `/photo`，随后每 **60 分钟**扫描一次，默认最多同时处理 **3 个任务**。档案中的新文件、失败任务和被删除的预览会在后续扫描中重新检查。

查看运行日志：

```bash
docker compose logs -f --tail=100
```

按 `Ctrl+C` 结束日志跟随，后台服务继续运行。预览文件、`*_metadata.json` 和 `Insta360_OpenConverter_convert_report.txt` 保存在原片目录；详细日志位于宿主机 `data/log/`。首次使用可先用少量素材检查画面、声音、拍摄时间和 GPS。

## 升级

在原部署目录执行：

```bash
docker compose pull
docker compose up -d
```

拉取新镜像后，第二条命令会更新运行中的容器。使用 `latest` 时，可先在 [GitHub Actions](https://github.com/LoneSpectator/Insta360-OpenConverter/actions) 确认目标版本已经发布成功。

升级继续使用现有 `.env`、`data/config.toml`、`data/state.sqlite3` 和 `sdk/`。已完成且校验通过的产物继续复用，失败或未完成的任务会再次处理。**保留并备份 `data/state.sqlite3`**，程序依靠它确认已有产物的归属。使用固定版本或 digest 时，先在 `.env` 中将 `CONVERTER_IMAGE` 改为目标版本。

跨旧版本升级前，请查看[旧配置迁移](#旧配置迁移)中的兼容说明。

<details>
<summary>可选：备份状态和更新部署文件</summary>

需要备份或同步仓库中的部署文件时，在原部署目录执行：

```bash
docker compose stop
backup_dir="data/upgrade-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
cp -a .env data/config.toml data/state.sqlite3* "$backup_dir/"
git pull --ff-only
docker compose pull
docker compose up -d
```

停止服务后备份，可保持 SQLite 状态及附属文件一致。已有配置继续使用，新选项省略时采用默认值；跨旧版本升级的配置迁移见[旧配置迁移](#旧配置迁移)。

</details>

## 完整配置

默认部署完成后，按需调整本节配置。`.env` 控制宿主机挂载、运行账号、GPU 和镜像；`data/config.toml` 控制扫描与转换，挂载为容器内的 `/data/config.toml`。完整 TOML 示例见 [config.example.toml](config.example.toml)。

### 宿主机设置：.env

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `PHOTO_DIR` | 必填 | 宿主机档案路径，挂载到容器 `/photo` |
| `PUID` | `1000` | 容器运行账号 UID，需能读写和删除档案中的生成物，并写入本地 `data/` |
| `PGID` | `1000` | 容器运行账号 GID |
| `NVIDIA_GPU_ID` | `0` | 分配给容器的一张 NVIDIA GPU，填写宿主机索引或完整 UUID |
| `CONVERTER_IMAGE` | `ghcr.io/lonespectator/insta360-openconverter:latest` | 镜像标签或 digest |

在宿主机执行 `nvidia-smi -L` 查看 GPU。`NVIDIA_GPU_ID` 可设为索引（如 `1`）或完整 UUID（如 `GPU-xxxxxxxx-…`）；UUID 不受索引排序变化影响。Compose 通过 `device_ids` 只分配选中的一张 GPU。

### 扫描与转换：data/config.toml

以下为全部 TOML 配置项。表中的 `sdk.*`、`tools.*` 和 `metadata.*` 分别写在 `[sdk]`、`[tools]`、`[metadata]` 段中；所有路径均为**容器内路径**。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `photo_root` | `"/photo"` | 容器内档案根目录；更改时同步调整 Compose 挂载目标 |
| `scan_interval_minutes` | `60` | 固定扫描周期，分钟，正整数；启动时立即扫描 |
| `max_concurrent_jobs` | `3` | 同一批扫描最多同时处理的任务数，正整数；`1` 为逐个处理，双镜头配对文件算一个任务 |
| `settle_seconds` | `120` | 原片最后修改后的等待时间，秒，可为 `0` |
| `command_timeout` | `7200` | 单条外部命令超时，秒，正整数 |
| `width` / `height` | `0` / `0` | 同时为 `0` 时推导源尺寸；显式设置须均为正偶数且宽高比为 2:1 |
| `bitrate` | `0` | 视频编码预算，bps；`0` 按源主视频流合计码率确定 |
| `codec` | `"source"` | 跟随源编码；可选 `source`、`h264`、`h265` |
| `flowstate` | `false` | 视频 FlowState 防抖开关 |
| `photo_flowstate` | `true` | 照片 SDK 拼接中的 FlowState 开关，独立于视频设置 |
| `stitch_type` | `"optflow"` | 拼接方式：`optflow`、`dynamicstitch`、`aistitch`，需 SDK 支持 |
| `camera_accessory_type` | `0` | 官方 SDK 配件类型枚举，范围 `0..20`，具体含义以 SDK 文档为准 |
| `sdk.source_root` | `"/sdk"` | 官方 SDK 包所在的容器内目录，默认只读挂载宿主机 `sdk/` |
| `sdk.executable` | `"/data/sdk/media/bin/MediaSDKTest"` | 准备后的 MediaSDK 命令行程序 |
| `sdk.models` | `"/data/sdk/media/bin/models"` | MediaSDK 模型目录 |
| `sdk.library_dirs` | `["/data/sdk/media/lib", "/data/sdk/media/bin"]` | MediaSDK 运行库搜索目录 |
| `sdk.extra_args` | `[]` | 追加 SDK 参数；禁止覆盖保留的输入、输出、尺寸和帧选择参数，或关闭 GPU 渲染 |
| `tools.exiftool` | `"exiftool"` | 标准元数据工具的命令名或路径 |
| `tools.ffprobe` | `"ffprobe"` | 媒体探测工具的命令名或路径 |
| `metadata.reader` | `"/data/sdk/metadata/bin/insta360-metadata"` | 官方元数据 SDK 的本地适配器 |
| `metadata.library_dirs` | `["/data/sdk/metadata/lib"]` | 元数据 SDK 运行库搜索目录 |

例如将扫描周期改为 30 分钟，并设置最多同时处理 3 个任务，编辑 `data/config.toml` 中的对应配置：

```toml
scan_interval_minutes = 30
max_concurrent_jobs = 3
```

并发任务共用选中的 GPU，各任务使用独立的 SDK 进程和工作目录。实际并发能力受 GPU 显存、编解码会话数量和存储带宽影响，增加并发数不保证按比例提高速度；资源不足时可将 `max_concurrent_jobs` 调整为 `1`。修改该调度项不会使已成功且未变的预览重新转码。

官方[接口文档](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/media.md)说明视频导出为异步调用，但未明确承诺同一 GPU 上多路拼接的兼容性或并发上限。三路并发需结合实际 SDK 版本、GPU 和素材验证。

修改 `.env` 或 `data/config.toml` 后，在部署目录重建容器使配置生效：

```bash
docker compose up -d --force-recreate
```

### 画质设置

默认优先保留源画质：`width = 0`、`height = 0`、`bitrate = 0`、`codec = "source"`，不统一缩放到固定尺寸或限制为固定码率。

`flowstate` 控制视频，照片使用独立的 `photo_flowstate`，默认值为 `true`。照片的 FlowState 在同一次 SDK 拼接导出中启用，不追加 JPEG 重编码或缩放；其姿态校正效果仍需在目标 GPU 上用对应机型的原片验证。

自动尺寸识别支持单个 2:1 主图像/视频流，以及双路同尺寸方形视频流；后者输出为单路宽度的两倍、高度不变。其他镜头布局需显式配置 `width` / `height`，无法确定时记录失败。源视频流缺少码率时，每个输入文件最多使用一次容器总码率估算；仍不可用时交由 SDK 使用源码率。相同码率只代表编码预算，不保证相同画质。

转换后核对尺寸、视频编码、帧率、位深、色度采样和源文件中已知的色彩描述；不符合保留要求的产物不会发布。10-bit 视频使用 H.265，须由 SDK 明确提供 `-enable_10bit`，并通过实际输出检查。源探测结果、导出计划和实际输出参数写入目录处理报告中对应文件条目的 `quality` 字段及审计日志。

全景拼接需要重投影和重新编码，MP4/JPG 输出不属于无损封装转换。程序默认不追加滤镜、降噪、增强或第二次 JPEG 编码；JPEG 质量由 SDK 内部控制，公开 CLI 未提供质量参数。SDK 可能仍执行其内部默认处理，例如部分演示版本默认启用且无法通过 CLI 关闭的 StitchFusion。实际画质和各机型兼容性需使用官方 SDK 与原片验证。

### 旧配置迁移

- 旧版 `sdk.mode` 和 `sdk.cpu_compatible` 已移除，需从已有配置中删除。
- 扫描周期统一使用分钟，推荐 `scan_interval_minutes`。旧键 `scan_interval` 仍可读取，但也按分钟解释，且不能与新键同时存在。
- 旧 `retry_seconds` 兼容读取时被忽略，可删除；失败任务由下一批扫描重新检查。
- 已有画质参数保持原值。采用当前默认时，将宽高改为 `0` / `0`、码率改为 `0`、编码改为 `"source"`、视频 `flowstate` 改为 `false`。

照片方向规则和 `photo_flowstate` 有独立的状态标识：规则或设置改变后，本工具拥有的旧 JPG 预览会从原片重新导出，视频不会因此重新转码。手动修改过的预览继续受到保护；需要重新生成时，先备份再移除对应 JPG，随后扫描会重新处理。

## 手动命令

本节用于依赖检查、单次扫描、单独提取元数据或前台调试。正常后台运行使用最简部署中的 `docker compose up -d`，它已自动选择 `watch`。

`doctor`、`once`、`watch`、`metadata` 是**容器内程序的子命令**。下面每组命令都在宿主机 SSH 终端的**部署目录**执行，由 `docker compose run` 启动临时容器。同一个 `data/` 只允许一个实例运行，因此每组示例先停后台服务，手动命令结束后再恢复。一次选择一组即可。

### 检查运行环境：doctor

```bash
docker compose stop
docker compose run --rm converter doctor
docker compose up -d
```

检查依赖、SDK 加载和命令行接口，成功返回 `0`，出错返回 `1`。首次使用时也会准备 SDK，检查过程不拼接素材。

### 扫描一遍后退出：once

```bash
docker compose stop
docker compose run --rm converter once
docker compose up -d
```

扫描并处理当前档案后退出。全部完成返回 `0`，存在失败或等待中的文件时返回 `1`。

### 单独提取一个文件的元数据：metadata

```bash
docker compose stop
docker compose run --rm converter metadata "/photo/事件目录/VID_001.insv"
docker compose up -d
```

将示例路径替换为实际**容器内路径**：例如 `PHOTO_DIR=/srv/photo` 时，宿主机 `/srv/photo/事件目录/VID_001.insv` 对应容器 `/photo/事件目录/VID_001.insv`。

只准备并调用官方 InsMetaDataSDK，提取元数据和传感器轨迹，结果保存在源文件旁的 `VID_001_metadata.json`。不生成预览。完成日志中的 `snapshot` 指向该文件；解析失败时返回错误。

### 前台持续扫描：watch

```bash
docker compose stop
docker compose run --rm converter watch
# 持续运行；按 Ctrl+C 停止前台任务后，再执行下一行。
docker compose up -d
```

扫描规则与后台服务相同。单文件失败会记录到报告，不影响其他文件处理。

## SDK 准备与运行机制

首次启动自动解包 MediaSDK 的程序、运行库和模型，并使用官方 InsMetaDataSDK 头文件及预编译动态库，编译本项目输出 JSON 的 C++ 元数据适配器。镜像包含所需 Python、`dpkg-deb` 和 GCC。

程序按官方文件名前缀识别 SDK 类型，兼容 `.tar.gz` 和 `.tar_时间戳.gz`，不依赖固定版本号或时间戳。同类存在多个版本时，选择可解析的最高语义版本；最高版本存在多个候选包，或版本无法明确判断时，报告冲突并提示整理归档。Compose 将宿主机 `sdk/` 只读挂载到 `/sdk`。

每次启动校验 SDK 归档、构建源码和缓存文件的 SHA256；归档更新或本工具管理的缓存缺失、损坏时自动重建。缓存和准备状态在 `/data/sdk`，解包、编译等临时内容在 `/data/sdk/.insta360-openconverter/`。官方压缩包本身损坏时，替换为完整官方包后重新启动。

四种程序命令都会先完成所需 SDK 的准备与校验，其中 `metadata` 仅准备元数据 SDK。依赖或配置无效时启动失败。

MediaSDK 的命令行程序也会链接图形运行库，镜像包含 GTK 3 和 FreeGLUT，无需启动桌面环境。启动出现 `error while loading shared libraries`（例如缺少 `libgtk-3.so.0`）时，先拉取最新镜像并用 `docker compose up -d --force-recreate` 重建容器。此类报错表示运行环境缺少动态库，需检查镜像依赖或库搜索路径；仅在官方包本身不完整时才需重新获取 SDK。

后台 `watch` 按固定周期触发扫描；上一批仍在处理时跳过到期批次，批次之间不会并行。同一批内部按 `max_concurrent_jobs` 限制并发任务数，默认最多同时处理 3 个任务；双镜头配对的两个原片共用一个转换任务。当前批次完成后等待下一个未来计划点，不补跑错过的批次。每批重新检查失败任务。

重启后依据持久化状态按文件恢复：成功且未变的产物跳过，未完成文件从原片重新处理，原片始终保留。中断时的 `processing` 记录在恢复扫描时回到待处理状态；SDK 未提供视频帧级续转能力。收到 `SIGTERM/SIGINT` 时会终止当前 SDK 子进程组并尝试清理临时文件，不发布半成品。

## 运行要求与兼容性

| 项目 | 要求或状态 |
| --- | --- |
| 平台 | Linux x86_64；容器基于 Ubuntu 22.04，镜像架构为 `linux/amd64` |
| 部署工具 | Git、Docker Engine、Compose v2、NVIDIA Container Toolkit |
| SDK | 官方 Linux x86_64 MediaSDK（拼接）和 InsMetaDataSDK（元数据读取）压缩包，放在宿主机 `sdk/` |
| GPU | NVIDIA GPU；驱动需兼容 SDK 包内 CUDA 运行时，具体版本以 SDK 文档为准 |
| 文件系统 | 档案目录可读写，支持硬链接和原子重命名；`data/` 可读写并允许执行 SDK 缓存，`sdk/` 归档可读 |

官方 [Desktop 指南](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/guide.md) 要求 **MediaSDK 3.x 使用独立 GPU**，本项目统一使用 NVIDIA GPU 部署。InsMetaDataSDK 元数据读取本身不调用 MediaSDK，容器仍使用同一 GPU 配置。离线文件处理不依赖 CameraSDK。

**验证状态**：自动化测试覆盖扫描、文件保护、SDK 字段映射、轨迹保存和 JPG/MP4 元数据回读。官方 InsMetaDataSDK 2.0.2 用于本项目适配器编译、动态库加载和错误路径验证；公开 CI 的 SDK 调用使用测试替身。真实相机数据和 MediaSDK 拼接、机型/LRV 兼容性仍需原片验证。

## 功能

- 递归扫描多层指定目录，支持中文、空格和大小写扩展名。
- INSP 转 JPG，INSV/LRV 转 MP4；自动配对双镜头视频文件。
- 输出 2:1 全景预览，写入 GPano / GSpherical 标记，复制目标格式支持的拍摄元数据。
- 在每个源文件旁保存 `<源文件主名>_metadata.json`，合并标准元数据、官方 SDK 字段和完整传感器轨迹。
- 默认按源数据确定输出尺寸、视频编码和码率预算，校验输出帧率、位深及已知色彩信息。
- 持久化处理状态，跳过未变文件，等待上传完成并重试失败任务。
- 每个源文件所在目录共用一份 `Insta360_OpenConverter_convert_report.txt`，优先列出失败文件及中文错误说明，在 `/data/log` 保存每次运行的详细审计日志。
- 通过 Docker Compose 部署，支持指定 NVIDIA GPU 索引或 UUID。
- 启动时自动准备本地官方 SDK，校验缓存并在需要时重建。

## 目录和处理规则

```text
/photo/
    活动档案/20261102.示例事件/
        .insta360-openconverter/             # 任务执行期间的临时工作目录
        Insta360_OpenConverter_convert_report.txt  # 本目录进度与失败记录
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
        .insta360-openconverter/             # SDK 准备期间的临时工作目录
        media/                              # 解包整理后的 MediaSDK
        metadata/                           # 官方元数据库与本项目适配器
```

- 扫描整个 `photo_root`，包括多层事件目录；`YYYYMMDD.事件名称` 用于说明文件中的事件识别，不限制更深层文件。大小写扩展名和中文/空格路径均支持。不跟随符号链接，也不扫描 `.insta360-openconverter/` 等本工具临时目录。
- 每个实际包含原片的目录只生成一份 `Insta360_OpenConverter_convert_report.txt`，按原文件名汇总该目录直属原片的最新状态；再次处理时更新对应条目，保留其他文件的记录。双镜头配对的两个原片各有条目。更深层子目录使用各自的报告，不汇入上层目录。
- INSP → JPG，INSV/LRV → MP4；同目录、同前缀、同序号、同扩展名的 `_00_` + `_10_` 文件成组输入 SDK。单文件/双视频轨素材直接交给 SDK 判断。孤立 `_10_` 等待配对。照片逐张处理，不擅自合并 HDR 曝光组。
- 每个原片各有一个 `<源文件主名>_metadata.json`，主名不含原扩展名，例如 `VID_001.insv` 对应 `VID_001_metadata.json`。双文件视频的两个原片分别保存元数据。标准字段、SDK 汇总和四类轨迹合并为一个 JSON，不再另外保留轨迹目录。同目录存在主名相同、扩展名不同的原片时，元数据命名会冲突，程序报告冲突并保护已有文件。
- LRV 作为独立输入处理；缺少 SDK 所需拼接信息时记录失败。同一拍摄产生的 VID 和 LRV 默认各有一份预览。
- 上传时默认等待最后修改至少 120 秒；转码前记录大小、mtime、inode、SHA256，发布前再检查源文件属性。建议上传到扫描根目录外，完成后原子移入，避免上传过程中长时间暂停导致提前处理。
- 预览、元数据和报告的临时文件统一放入源文件所在目录的 `.insta360-openconverter/` 下；任务完成、失败或取消时尝试清理本次临时内容，工作目录空闲且无其他内容时一并移除。文件系统暂时阻止清理时记录警告，后续扫描再尝试回收。检查导出画质参数、音轨、时长和关键元数据后，同步落盘并原子发布；底层文件系统需支持硬链接（首次无覆盖发布）和原子重命名。
- 进程异常退出后，后续启动或扫描会回收可确认由本工具创建且已无任务使用的残留。可识别的旧版空工作子目录也会自动移除；缺少归属标记的非空目录、来源不明的目录、符号链接和仍在使用的目录受到保护。旧版随机名称的临时目录继续排除在扫描之外。
- 重复扫描通过 SQLite 跳过未变文件；源文件或转换配置改变后重新生成本工具拥有的产物。用户原有文件、被用户修改的预览或元数据 JSON，以及同名非本工具 TXT 不会被覆盖。删除预览后会从原片重新生成，缺失的元数据 JSON 会在后续扫描补回并复用已有预览；**保留并备份 `/data/state.sqlite3`**，否则服务无法确认既有产物的归属，会拒绝覆盖。
- 日志包含每条外部命令及退出码、SDK stdout/stderr、原片读取/哈希、元数据快照、临时文件、发布/替换/清理、等待/跳过/失败原因。日志仅追加，不自动轮转或删除；可由宿主机配置保留策略。日志和快照可能包含拍摄时间、GPS 和原始路径，请按档案权限管理。运行中更改 GPU 后需重建服务，不能只重启原容器。

共享文件系统可能延迟回收仍被打开的已删除文件。临时目录清理未完成时，程序单独记录维护警告并在后续扫描重试，已成功发布的预览和报告保持成功状态。更新容器后，可识别的旧版空工作子目录会自动移除；来源不明、非空且缺少归属标记或仍在使用的内容会保留。

目录报告使用 4 空格缩进的标准 JSON，没有头部说明或注释，可直接由 JSON 解析器读取。扫描开始时登记目录内发现的原片，随后更新待处理、处理中和处理结果。报告先展示失败数量和 `failures` 清单，再展示逐文件记录；失败项使用中文错误说明和 `error_code`，`files` 中保留 `error_raw` 原始错误，便于定位原因和核对程序行为。

| 字段 | 含义 |
| --- | --- |
| `format` / `schema_version` | 报告标识与结构版本 |
| `updated_at` | 报告最后更新时间 |
| `summary` | 当前目录的总数及失败、处理中、待处理、等待、成功数量 |
| `failures` | 当前失败文件及中文错误说明，优先查看此项 |
| `files` | 各原片的最新状态、运行标识、时间、产物路径、校验值与处理详情 |

`status` 保留 `pending`、`processing`、`deferred`、`failed`、`success` 等机器可读值，`status_text` 提供中文状态。后续扫描中已不在目录内的原片记录保留为 `present = false`，供审计查询，不计入当前进度汇总。报告记录最新结果，完整操作过程通过 `run_id` 关联 `/data/log` 中的运行日志。

## 元数据语义

照片写入 Google **GPano**，视频写入视频轨内的 **GSpherical UUID**（Spherical Video v1）。全景画面为 2:1 单目等距柱状投影，播放效果取决于播放器对全景元数据的支持。

源文件的相机型号、固件、序列号和传感器轨迹通过 **官方 InsMetaDataSDK** 读取。预览优先使用 SDK 相机字段和首个有效 GPS 点，按南北/东西标记处理坐标符号。

每个源文件旁的 `_metadata.json` 同时包含 ExifTool 标准字段、`Insta360SDK` 汇总，以及 GPS、陀螺仪与加速度、曝光、延时摄影轨迹。完整记录保存在 `Insta360SDK.tracks.<轨迹名>.records` 数组中，并保留数量、SDK 可用状态和原始轨迹的 SHA256 等信息。JSON 使用 4 空格缩进，轨迹逐条合并写入，长视频无需一次将全部记录加载到内存。SDK 中间文件仅存于源文件所在目录的 `.insta360-openconverter/` 内，合并后清理；运行日志不输出逐帧轨迹。

元数据名称均使用不含扩展名的源文件主名。同一原片的元数据采用一种存放形式：单文件为 `<源文件主名>_metadata.json`；需要多个文件时则全部放入 `<源文件主名>_metadata/`，目录外不再另留同名 JSON。当前支持的数据均可合并，因此只生成单个 JSON。若源文件旁已存在对应的 `_metadata` 目录，程序保留该目录并报告冲突，避免两种形式同时出现。

ExifTool 补充 SDK 未公开的标准拍摄时间等字段，复制目标格式支持的 EXIF/XMP/IPTC 或 QuickTime 字段，并写入与回读核对预览的拍摄时间、相机信息、GPS 和全景标记。尺寸与全景几何字段更新为预览实际值。MP4 保留可复制的 QuickTime 日期，拍摄时间同时写到 XMP；文件 mtime 跟随第一原片。

ExifTool 的读取、复制、写入和回读均启用大文件支持，以读取大 INSV 的拍摄字段并写入大 MP4 的全景标记，无需额外配置。

照片先记录 SDK 导出的 JPG 方向标记，再复制原片拍摄字段。最终 EXIF/XMP 方向保持 SDK 渲染后的显示语义，不复制原片的 `Orientation` 或 IFD1 缩略图；SDK 未提供方向标记时不从原片补出。元数据写入不旋转图像像素、不追加 JPEG 编码。方向标签一致性检查不能替代对实际画面朝向的验收。

InsMetaDataSDK 的 `GetFirstTimeStamp()` 是陀螺仪时间基准，不能作为拍摄日期。SDK 2.0.2 的 GPS `timestampMs` 字段名与头文件的秒单位注释存在歧义，因此轨迹保留原值，不猜测时间单位或转换成 UTC 日期。缺失的拍摄时间会记入日志，不从文件名或目录名推断。

**元数据保留范围**：SDK 暴露的 GPS、陀螺仪、曝光、延时轨迹完整保存在源文件旁的 JSON 中，不保证嵌入 JPG/MP4。SDK 与 ExifTool 均未公开的防抖、深度、镜头映射和未知二进制数据仍保留在原片中。SDK 解析失败与单个轨迹不存在分别记录，不会把缺失数据写成有效的零值。

可识别的旧版目录报告 `insta360-preview-report.txt` 会在成功迁移后改用新名称和标准 JSON 格式，保留原有文件记录；迁移过程中保护无法确认归属的文件。更早版本生成的 `/data/metadata` 历史文件和逐文件 `*_preview.txt` 说明不会自动删除。升级后，后续扫描会按新规则在源文件旁生成元数据 JSON，并按实际目录汇总处理报告；确认归档和备份后可自行整理历史文件。

## GitHub 自动构建和 GHCR

工作流见 [`.github/workflows/container.yml`](.github/workflows/container.yml)。PR 运行测试和构建，不登录/推送；`main` 更新、`v*` 标签和手动触发在测试通过后构建 **linux/amd64** 并发布。测试同时覆盖开发用 Python 3.12 和镜像内 Python 3.10/系统媒体工具；镜像内测试使用非 root 用户和只读根文件系统，成功后才推送。只使用 GitHub 提供的 `GITHUB_TOKEN`，不需要将 SDK 或个人 PAT 放进 Actions secrets。

测试失败时，Actions 的摘要和注解会显示失败测试与错误堆栈。完整日志保存在本次运行的 `public-test-logs` 或 `container-test-logs` 附件中，保留 14 天；公共测试日志同时记录 Python、FFmpeg 和 ExifTool 版本。

镜像地址：`ghcr.io/lonespectator/insta360-openconverter`。`main` 发布 `latest`、`main` 和 `sha-…`；`v0.1.0` 发布 `0.1.0`、`0.1` 和 `sha-…`。生产部署建议固定版本或镜像 digest。仓库需允许 GitHub Actions，包策略需允许工作流写入 GHCR。公开包支持匿名拉取，私有包需要先登录 GHCR。

镜像内包含应用、公共工具和适配器源码。宿主机 `sdk/` 中的官方压缩包只读挂载到 `/sdk`，由容器在 `/data/sdk` 自动准备运行文件。SDK 压缩包、动态库、模型和授权文件不纳入公开仓库或默认镜像。

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

本地提供官方元数据适配器时，可通过 `INSTA360_METADATA_READER=/绝对路径/bin/insta360-metadata` 启用真实 SDK 加载、Python 调用时的独立空输出目录及非法输入测试。公开 CI 不保存 SDK；未提供该变量时，相关私有依赖测试明确跳过。

## 参考文档

官方参考：[MediaSDK 仓库](https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp)、[MediaSDK 接口文档](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/media.md)。命令行参数和包布局以实际安装版本自带的 README 和 `MediaSDKTest -help` 为准。

## AI 编码声明

本项目由 AI 编码。项目的应用代码、自动化测试、容器部署配置、GitHub Actions 工作流及文档等工作，主要由 **OpenAI Codex（GPT-6）** 根据项目发布者的需求生成、修改和整理。

项目发布者负责对 AI 生成的内容进行实际测试、审核、部署与后续维护。

## 许可证

项目采用 [AGPL-3.0](LICENSE) 许可证。Insta360 SDK 的使用和分发遵循其独立许可条款。
