# Insta360 OpenConverter

面向 Linux x86_64 的离线预览服务。递归扫描事件档案，调用官方 **Insta360 MediaSDK 的 `MediaSDKTest`** 拼接 INSP、INSV、LRV，生成同目录的 `_preview.jpg` / `_preview.mp4`，附带说明 TXT 和独立的文件操作审计日志。默认每 60 秒扫描一次，顺序处理，避免多个高分辨率任务争抢显存。

## AI 编码声明

项目需求与部署目标由维护者提出；当前初始版本的应用代码、自动化测试、Docker/Compose 配置、GitHub Actions 工作流及文档，由 **OpenAI Codex（AI 编码工具）根据维护者提供的需求与反馈生成和修改**。已执行的验证及仍待完成的真实 SDK 验收范围在下文中列明。

**当前验证范围**：公共依赖、扫描/重试/文件保护、真实 JPG/MP4 元数据读写和容器构建可以独立测试。仓库不包含 SDK、相机样本或授权；真正的拼接质量、机型/LRV 支持和 GPU 执行必须在挂载官方 SDK 后用原片验收。自动测试使用明确的 SDK 替身，不代表已验证官方拼接。

## 你提供的 SDK 怎么用

| 文件 | 本项目用途 |
| --- | --- |
| `MediaSDK-3.1.5-20260819-linux64.tar_1787191716602.gz` | **需要**。离线拼接和转码，使用包内的 `MediaSDKTest`、动态库和模型 |
| `InsMetaDataSDK-20260629_184813-2.0.2-linux64-default.tar_1782730793365.gz` | 可保留用于进一步解析私有元数据；当前版本使用 ExifTool，不加载这个 SDK。接入其 C++ API 需要检查实际头文件和示例 |
| `CameraSDK-2.1.8-20260828_171805-linux-x86_64.tar_1787909155024.gz` | 相机控制和实时流；本项目读取磁盘档案，不需要 |
| `CameraSDK` 的 gcc-arm / gcc-linaro / jetson 包 | ARM64 目标的不同工具链版本；不用于本项目 x86_64 镜像，也不是给 x86_64 主机增加 CPU 转码能力的包 |

文件扩展名中的 `tar_时间戳.gz` 不影响 gzip/tar 解包。不要将 SDK 压缩包、`.so`、模型或授权文件提交到 Git。

官方 [Desktop 指南](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/guide.md) 明确要求 **MediaSDK 3.x 有独立 GPU**。因此你现有的 **3.1.5 按 NVIDIA GPU 模式部署**；不能把 `-enable_soft_encode` 误认为无 GPU 也能运行。纯 CPU 的调度和参数路径已预留，但需官方提供兼容 CPU 的 MediaSDK，核实其命令行参数并通过真实原片测试，才能宣布支持。当前没有捆绑替代拼接器，也不会直接把原始鱼眼文件改扩展名后标记为全景。

## 部署（优先 NVIDIA GPU）

宿主机建议 Ubuntu 22.04 x86_64，安装 Docker Engine、Compose v2 和 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。GPU 驱动需兼容 SDK 包内的 CUDA 运行时；官方列出的驱动下限为 470，实际以该 SDK 的 README 为准。驱动在宿主机安装；不要将宿主机驱动装进镜像。SDK 自带的 CUDA 等库需保持完整。

在本仓库根目录执行：

```bash
mkdir -p data sdk
cp config.example.toml data/config.toml
cp .env.example .env

# 在 Linux 宿主机准备 SDK；需要 Python >= 3.10 和 dpkg-deb。
python3 scripts/prepare_sdk.py \
  /你的下载目录/MediaSDK-3.1.5-20260819-linux64.tar_1787191716602.gz \
  ./sdk/media
```

脚本解压到临时目录；如果包中包含 `.deb`，仅用 `dpkg-deb --extract` 提取，不运行安装脚本；识别 `bin/MediaSDKTest`、`lib/`、`bin/models/` 后整理到 `sdk/media`。已有目标目录不会被覆盖。如果包内布局与线上文档不同，脚本会报告错误，此时先查看实际包内 `README.txt`，不要随意移动单个 `.so`。有官方 SHA256 时可追加 `--sha256 <官方校验值>`；脚本显示的本地 SHA256 本身不是官方真实性证明。

编辑 `.env`：

```dotenv
PHOTO_DIR=/srv/photo
PUID=1000
PGID=1000
NVIDIA_GPU_ID=0
```

`PHOTO_DIR` 是宿主机原始数据目录，挂载到容器 `/photo`。`PUID/PGID` 对应具有档案读写权限的用户（`id -u` / `id -g`）；该用户也需能读写本地 `data/`，读取 `sdk/media/`。原目录需要写权限以创建预览和 TXT。不要用 `chmod -R 777` 或改动原片权限来解决权限问题。

**选择 GPU**：用 `nvidia-smi -L` 查看索引和 UUID，将 `NVIDIA_GPU_ID` 设置为索引（如 `1`）或完整 UUID（如 `GPU-xxxxxxxx-…`，推荐，不随索引排序变化）。Compose 用 `device_ids` 只向容器分配这一张卡，没有 `count: all`。改变设备后重新创建服务：

```bash
docker compose -f compose.yaml -f compose.gpu.yaml build
# 首先检查依赖、SDK 动态库和帮助接口（不等于真实素材拼接验收）。
docker compose -f compose.yaml -f compose.gpu.yaml run --rm converter doctor
# 用少量备份样本目录首次试运行，确认画面/声音/时间/GPS后再用于完整档案。
docker compose -f compose.yaml -f compose.gpu.yaml run --rm converter once
docker compose -f compose.yaml -f compose.gpu.yaml up -d --force-recreate
```

`once` 扫描一遍后退出；失败或仍在等待上传完成的文件使退出码为 1，全部成功/已完成时为 0。`watch` 持续扫描，单个文件失败不会中断其他文件，默认 300 秒后重试；依赖或配置无效则启动失败。`SIGTERM/SIGINT` 终止当前 SDK 子进程组并清理本次临时文件，不发布半成品。

配置改动后重建容器进程即可，无需重建镜像。CPU 兼容 SDK 到位并验收后，使用 `sdk.mode = "cpu"`、`sdk.cpu_compatible = true`，仅用 `compose.yaml` 运行，不加 GPU override；适配器会显式传入软件编解码、禁用 CUDA 和 CPU 渲染参数。不同 SDK 版本若没有这些接口，需要调整适配器，不能单改开关声称已兼容。

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
- LRV 也是独立输入；有些机型的 LRV 不含 SDK 所需拼接信息时会失败并记录原因，不会伪造全景标记。同一拍摄产生的 VID 和 LRV 默认各有一份预览。
- 上传时默认等待最后修改至少 120 秒；转码前记录大小、mtime、inode、SHA256，发布前再检查源文件属性。建议上传到扫描根目录外，完成后原子移入，避免上传过程中长时间暂停导致提前处理。
- 先在同目录临时子目录中生成，检查尺寸、音轨、时长和关键元数据，然后同步落盘并原子发布。底层文件系统需支持硬链接（首次无覆盖发布）和原子重命名。中断后的残余 `.insta360-preview-*` 目录不会作为原片扫描，可结合日志在服务停止后人工清理。
- 重复扫描通过 SQLite 跳过未变文件；源文件或转换配置改变后重新生成本工具拥有的预览。用户原有预览、被用户修改的预览和同名非本工具 TXT 不会被覆盖。删除预览可触发重建；**保留并备份 `/data/state.sqlite3`**，否则服务无法确认既有预览的归属，会拒绝覆盖。
- 日志包含每条外部命令及退出码、SDK stdout/stderr、原片读取/哈希、元数据快照、临时文件、发布/替换/清理、等待/跳过/失败原因。日志仅追加，不自动轮转或删除；可由宿主机配置保留策略。日志和快照可能包含拍摄时间、GPS 和原始路径，请按档案权限管理。运行中更改 GPU 后需重建服务，不能只重启原容器。

## 元数据语义

照片写入 Google **GPano**，视频写入视频轨内的 **GSpherical UUID**（Spherical Video v1），不是只添加一个普通 `360` 文本标签。全景画面为 2:1 单目等距柱状投影。播放器是否显示全景入口仍取决于其格式支持。

ExifTool 复制目标格式支持的 EXIF/XMP/IPTC 或 QuickTime 字段，再显式保留并回读核对可识别的拍摄时间（包含已有时区）、相机品牌/型号、GPS 经纬度；尺寸、方向和全景几何字段更新为预览实际值。MP4 保留可复制的 QuickTime 创建/修改日期，拍摄时间同时写到 XMP。预览文件 mtime 跟随第一原片。原片没有可识别拍摄时间时明确记日志，不从事件名称、文件名或当前时区猜测拍摄时间。

**不同封装无法保证全部元数据逐项相同**：原始逐帧陀螺仪、防抖、深度、原始镜头映射、GPS 轨迹、未知二进制块等不保证嵌入 JPG/MP4；ExifTool 不可识别的数据也不在 JSON 快照中。保留原片是完整保留这些数据的依据。`InsMetaDataSDK 2.0.2` 的进一步提取需要实际包内 API 和真实样本，当前不宣称已经接入。

## GitHub 自动构建和 GHCR

工作流见 [`.github/workflows/container.yml`](.github/workflows/container.yml)。PR 运行测试和构建，不登录/推送；`main` 更新、`v*` 标签和手动触发在测试通过后构建 **linux/amd64** 并发布。测试同时覆盖开发用 Python 3.12 和镜像内 Python 3.10/系统媒体工具；镜像内测试使用非 root 用户和只读根文件系统，成功后才推送。只使用 GitHub 提供的 `GITHUB_TOKEN`，不需要将 SDK 或个人 PAT 放进 Actions secrets。

本仓库对应镜像：`ghcr.io/lonespectator/insta360-openconverter`。`main` 发布 `latest`、`main` 和 `sha-…`；`v0.1.0` 发布 `0.1.0`、`0.1` 和 `sha-…`。生产部署建议固定版本或镜像 digest。工作流文件需要先提交并推送到 GitHub 才会执行；仓库设置需允许 GitHub Actions，组织的包策略需允许该仓库写入 GHCR。新 GHCR 包的可见性以 GitHub 设置为准，匿名拉取需将包设为 public，否则先登录 GHCR。

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

镜像内只有应用和公共工具，SDK 从本机 `sdk/media` 挂载。**当前仓库的 AGPL-3.0 许可不授予重新分发 Insta360 SDK 的权利**，SDK 的使用和分发遵循你与官方的许可约定。

## 开发与验证

Python >= 3.10（3.10 还需 `tomli`，镜像通过系统包安装），运行时不需要 PyPI 依赖。在 Linux 安装 `ffmpeg` 和 `libimage-exiftool-perl` 后：

```bash
python3 -m unittest discover -s tests -v
python3 -m converter --help
# 宿主机运行时：配置内路径需改为实际绝对路径。
python3 -m converter --data-dir /绝对路径/data doctor
python3 -m converter --data-dir /绝对路径/data once
```

测试覆盖：镜头配对、孤立镜头、符号链接、输出冲突、上传等待、幂等、源文件变更、非本工具文件保护、SDK 假成功、重试、超时与中断、实例锁，以及真实媒体工具的时间/GPS/360 UUID/音轨校验。没有 SDK 或原片时，不将这些测试描述为相机转码验收。

官方参考：[MediaSDK 仓库](https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp)、[当前 MediaSDK 接口文档](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/ch/sdk/x-ace-go/desktop/media.md)。部署时优先核对实际 **3.1.5 包内** README/`-help`；线上主分支文档可能晚于该 SDK 版本。
