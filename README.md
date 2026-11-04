# RAIDFORGE — 服务器 RAID 管理与硬盘擦除工具（Debian Live ISO + Python TUI）

一套可在裸机上启动的**一次性、可审计**的服务器存储运维环境：基于 Debian 12
(bookworm) + live-build 构建的 UEFI/BIOS 双引导 Live ISO，开机后自动在 tty1 以
root 启动 Textual TUI，完成「阵列卡探测 → RAID/物理盘查看 → RAID 创建/删除/清配置
→ 磁盘枚举 → 干跑确认 → 串行提交擦除命令 → 生成证据 → 上报」的完整闭环。

```
┌──────────────────────────────────────────────────────────┐
│ SN:xxx  tool:storcli  ctrl:1  disk:12  sel:2   net:up    │  ← 顶部固定状态栏
├──────────────────────────────────────────────────────────┤
│                                                          │
│  Dashboard / Controllers / ControllerDetail / CreateRAID  │  ← RAID 管理
│  DiskList / ConfirmModal / Progress / RaidProgress        │  ← 擦除与执行
│                                                          │
├──────────────────────────────────────────────────────────┤
│ Space 勾选   A 全选   N 清空   Enter 执行   Q 退出        │  ← 底部固定快捷键栏
└──────────────────────────────────────────────────────────┘
   黑底白字（纯 #000000 / #FFFFFF，16 色控制台安全）
```

---

## 1. 目录结构

```
raidforge/
├── build.sh                      构建入口（前置校验 / 拉 wheel / lb clean+build / 产出重命名）
├── Makefile                      build / plan / run / test / pytest / webplane 等快捷目标
├── README.md                     本文档
├── .gitignore
│
├── auto/{config,build,clean}     【必须在项目根】live-build ≥ 2023 的 auto 脚本入口
├── config/                       live-build 配置树
│   ├── package-lists/*.list.chroot
│   ├── includes.chroot/
│   │   ├── usr/local/bin/        【需手动放入】storcli/storcli64、perccli/perccli64、arcconf、ssacli/hpssacli
│   │   ├── etc/raidforge/        站点 env + 证据私钥（config.yaml 与 config.d/ 由 hook 在镜像内创建）
│   │   └── etc/systemd/network/  10-raidforge-dhcp.network —— 所有非 lo 网卡 DHCP
│   └── hooks/live/
│       ├── 010-install-app.hook.chroot     /opt/raidforge + venv + 离线装 wheel + 工厂 config.yaml
│       ├── 020-autologin.hook.chroot       tty1 自动登录 root
│       ├── 030-bash-profile.hook.chroot    /root/.bash_profile 在 tty1 exec TUI
│       ├── 040-services.hook.chroot        裁剪服务、建证据目录
│       ├── 045-networkd.hook.chroot        启用 systemd-networkd（多网卡 DHCP）
│       ├── 050-boot-timeout.hook.binary    引导菜单 10s 倒计时后自动进默认项（BIOS+UEFI）
│       └── 900-cleanup.hook.chroot        清理构建残留、写 /etc/raidforge-build-info
│
├── app/
│   ├── run.py / pyproject.toml / requirements.txt / config.yaml.example / agent.yaml.example
│   └── raidforge/
│       ├── main.py               编排：解析 SN → 建会话 → 录制 → TUI → 收尾 → 上报
│       ├── config.py             唯一配置中心（defaults < config.yaml < config.d < RAIDFORGE_*）
│       ├── agent/                Agent 客户端：心跳/轮询/串行执行/确认/证据/补传
│       ├── core/
│       │   ├── models.py         DiskInfo / CommandPlan / EraseRun / VolumeInfo / RaidLayout / RaidBundle…
│       │   ├── utils.py          run_cmd() —— 全项目 subprocess 唯一出口
│       │   ├── serverid.py       dmidecode SN + 兜底阶梯
│       │   ├── detect.py         探测链 storcli→perccli→arcconf→ssacli→sg3_utils（支持 64 位/别名）
│       │   ├── enum_disks.py     多厂商物理盘解析 + smartctl Rotation Rate 判介质
│       │   ├── scan.py           一次扫描：探测 + 枚举 + RAID 拓扑
│       │   ├── commands.py       擦除命令表（纯函数，产出 argv 与干跑文本）
│       │   ├── erase.py          擦除串行执行，timeout=60，rc==0 记为 ASYNC_SUBMITTED
│       │   ├── raid/             RAID 核心层
│       │   │   ├── base.py        provider 抽象接口
│       │   │   ├── layout.py      级别校验 / RAID10 span 配对 / 容量 / 名称
│       │   │   ├── commands.py    RAID0/1/5/10 创建、删除 VD、清配置命令表
│       │   │   ├── lsi.py         storcli / perccli
│       │   │   ├── adaptec.py     arcconf
│       │   │   ├── hpe.py         ssacli / hpssacli
│       │   │   ├── generic.py     无卡只读降级
│       │   │   ├── collect.py     拓扑采集 + 能力判定
│       │   │   └── execute.py     RAID 命令串行执行
│       │   ├── evidence.py       会话目录 / events.jsonl / manifest.json + SHA-256
│       │   ├── record.py         asciinema 包裹式录制
│       │   ├── screenshots.py    export_screenshot() → snapshots/*.svg
│       │   ├── svg_raster.py     内嵌 Rich SVG → PNG（仅 Pillow，无系统依赖）
│       │   ├── erase_report.py   服务器维度 DOCX 擦除报告（多轮汇总 + 截图）
│       │   ├── publish.py        每轮报告、manifest、上传编排
│       │   └── report.py         rsync→HTTP→pending 三级上报 + 补传
│       └── ui/
│           ├── app.py / theme.py / state.py / scan.py
│           ├── widgets/{status_bar,key_bar,disk_table,raid_tables}.py
│           └── screens/{dashboard,controllers,controller_detail,raid_create,
│                        raid_confirm,raid_progress,disk_list,confirm,progress,settings}.py
│
├── webplane/                    RaidForge Control Plane（Flask + Vue3 + Element Plus，Agent 管理）
├── tests/
│   ├── test_detection.sh        交付物 E：探测链 / 枚举 / 干跑命令构造（Linux/WSL）
│   ├── test_core.py             解析层与命令层单测（跨平台）
│   ├── test_erase.py            执行层：串行、超时、dry-run、取消
│   ├── test_pipeline.py         伪进程层端到端测试（跨平台）
│   ├── test_config.py           config.d 覆盖顺序与 TUI 站点写入
│   ├── test_report_network.py   任一块网卡拿到 IPv4 即视为网络就绪
│   ├── test_report_ssh.py       设置页探测：SSH 登录 + 远端目录可写
│   ├── test_report_http.py      HTTP 端点推导 / healthz 探测 / 传输顺序与兜底
│   ├── test_settings_screen.py  设置页：只改 IP、保存后探测（需 7.5.0）
│   ├── test_svg_raster.py       Rich SVG → PNG 栅格化（需 Pillow）
│   ├── test_erase_report.py     DOCX 报告：命名/多轮/状态/manifest/包裹模式（需 docx）
│   ├── test_report_fidelity.py  QEMU 形状数据：全量磁盘、FW/RAID、命令、manifest 一致性
│   ├── test_report_live.py      真实驱动两轮擦除后生成报告（需 7.5.0 + docx）
│   ├── test_report_publish_ui.py 每轮结束即时生成/上传，Q 前 DOCX 已存在
│   ├── test_publish.py          发布锁、manifest 顺序、即时失败不排队、父进程继承
│   ├── test_dashboard_enter.py  Dashboard 焦点按钮 Enter / 无焦点默认 / R/U/S 回归
│   ├── test_sync_flags.py       sync.sh 默认上传私钥 / --no-keys
│   ├── test_launcher_menu.py    退出菜单：shell 退出后回到菜单（Linux pty）
│   ├── test_mono_static.py      黑白配色的静态守卫（CSS/主题/Rich 标记）
│   ├── test_mono_live.py        渲染后的真实帧配色守卫（需镜像同版本 Textual）
│   ├── test_quit_path.py        Progress 屏 Q 语义回归（需镜像同版本 Textual）
│   ├── test_raid_layout.py      RAID0/1/5/10 规则、RAID0 分组、RAID10 配对
│   ├── test_raid_commands.py    三厂商 RAID 创建/删除/清配置精确 argv
│   ├── test_raid_parsers.py     storcli/arcconf/ssacli 拓扑解析
│   ├── test_raid_eligibility.py 空闲盘资格与 blocked 原因
│   ├── test_raid_execute.py     RAID 串行执行、失败继续、超时、取消
│   ├── test_raid_evidence.py    raid_actions.json / manifest @2
│   ├── test_raid_ui.py          RAID 屏幕与闸门（需 7.5.0）
│   ├── test_detect_aliases.py   storcli64/perccli64/hpssacli 别名
│   ├── test_rename_guard.py     旧命名残留守卫
│   ├── test_agent_config.py     Agent 默认控制面/token、HTTP/HTTPS、配置优先级
│   ├── test_agent_*.py          Agent 客户端/队列/心跳/轮询/执行/证据/恢复
│   ├── test_remote_task_confirm_ui.py  远程破坏性任务确认屏（严格 yes）
│   ├── ui_smoke.py              无头 UI 冒烟（80×25）
│   ├── conftest.py
│   └── fixtures/                伪造的 storcli JSON / arcconf JSON / ssacli detail / smartctl 输出
├── tools/
│   ├── check.sh                 L0：pytest + 探测链 + UI 冒烟（~30s）
│   ├── chroot_tty.py            L1：在构建 chroot 的真实 pty 里跑 TUI（~15s）
│   ├── qemu_drive.py            L4：无头 qemu 启动 ISO、驱动 TUI、验证退出菜单（--erase-rounds）
│   ├── sync.sh                  把本地产物同步到构建主机（增量 / 监听 / 远端执行）
│   ├── rssh.py                  免交互 SSH 小工具（exec / put / push，读 .remote.env）
│   └── .remote.env              构建主机账号（已 gitignore）
├── artifacts/                   tools 生成的截图 / 日志（已 gitignore）
└── vendor/wheels/               build.sh 下载的 Python wheel（已 gitignore）
```

---

## 2. 环境准备

### 2.1 构建机

Debian 12 (bookworm) 或 Ubuntu 22.04+，x86_64，root 或 sudo：

```bash
sudo apt update
sudo apt install -y live-build debootstrap xorriso isolinux syslinux-utils \
                    grub-pc-bin grub-efi-amd64-bin grub-common mtools dosfstools \
                    squashfs-tools rsync python3 python3-pip
```

> macOS / Windows 无法直接构建 live-boot 镜像（需要 debootstrap 与 chroot）。
> 请在 WSL2 + Debian/Ubuntu，或一台真 Debian 机器上执行 `./build.sh`。

**实测通过的构建环境**：Debian 12.15 amd64 / 2 vCPU / 1.9 GB RAM（+3 GB swap）/
live-build 20230502 / 磁盘 ≥ 12 GB。低于 2 GB 内存时 `mksquashfs` 有 OOM 风险，
建议先 `fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile`。

### 2.2 厂商工具获取（必须手工放置）

这些 CLI 是厂商分发物，**不能随仓库分发**。把解压出的 ELF 二进制直接放进
`config/includes.chroot/usr/local/bin/`，文件名必须精确为下表中的名字（64 位版本
`storcli64`/`perccli64` 也可以直接放；镜像会按需创建兼容 shim）：

| 工具 | 来源 | 常见包内路径 |
|---|---|---|
| `storcli` / `storcli64` | Broadcom / OEM 支持页 → "StorCLI MR7.x" | `storcli_all_os/Linux/storcli`（有些包是 `univ_viva_cli_rel/storcli_all_os/Linux/storcli`） |
| `perccli` / `perccli64` | Dell 支持页 → "PERC CLI" | `perccli` / `perccli64`（与 storcli 同语法） |
| `arcconf` | Microsemi / Adaptec → "ARCCONF CLI" | `cmdline/arcconf`（也有 `ARCCONF_*.tar.gz`） |
| `ssacli` / `hpssacli` | HPE Support Center → "SSA CLI" | `ssacli-*.rpm` → `usr/sbin/ssacli`；或 `.deb` → `dpkg-deb -x` |

```bash
# storcli（示例）
unzip StorCLI_MR7.20.zip
cp versionChangeSet/univ_viva_cli_rel/storcli_all_os/Linux/storcli \
   config/includes.chroot/usr/local/bin/storcli
chmod 755 config/includes.chroot/usr/local/bin/storcli

# ssacli（rpm 示例）
rpm2cpio ssacli-*.x86_64.rpm | cpio -idmv
cp usr/sbin/ssacli config/includes.chroot/usr/local/bin/ssacli
chmod 755 config/includes.chroot/usr/local/bin/ssacli

# arcconf（示例）
tar xf arcconf_v3*.tar.gz
cp linux_x64/cmdline/arcconf config/includes.chroot/usr/local/bin/arcconf
chmod 755 config/includes.chroot/usr/local/bin/arcconf
```

自检：

```bash
./build.sh --preflight          # 一个厂商 CLI 都没有时会报错；跑通用模式可设 ALLOW_MISSING_TOOLS=1
```

> 一个都没有也没关系：镜像会以 `sg3_utils + smartctl + hdparm + nvme-cli` 的
> **generic 模式**启动，但只能擦到操作系统直接可见的盘（RAID 卡后面的盘看不到），
> 且 RAID 管理功能全部禁用。

**兼容 shim**：如果只放了 `storcli64` / `perccli64` / `hpssacli`，`010` hook 会在镜像内
创建 `storcli` / `ssacli` shim，保证原有擦除命令表（argv[0] 固定为 canonical 名）
不需要改动。RAID provider 则直接使用探测到的实际二进制。

---

## 3. 构建步骤

```bash
./build.sh               # 默认：preflight → 下载 wheel → lb clean（保留缓存）→ lb build（~6m45s）
./build.sh --refresh     # 复用 chroot，只重跑携带 app 的阶段（~4m）
./build.sh --fast        # --refresh + zstd level 1 + 1s 引导倒计时（~2m，产物带 -test 后缀）
./build.sh --full-clean  # 从零重建：lb clean --all，丢掉 bootstrap 树与 deb 缓存
./build.sh --vendor-only # 只拉 Python wheel（离线环境的准备工作）
./build.sh --no-vendor   # 复用已有的 vendor/wheels/
./build.sh --preflight   # 只做环境校验
./build.sh --clean       # 清理
```

日常改 `app/` 不需要每次都走 ISO：先 `./tools/check.sh`（~15s），必要时
`./build.sh --fast` 出测试镜像，最后才用完整构建出发布镜像。完整验证阶梯见 §8.1。
`--fast` 的产物永远带 `-test` 后缀，避免测试镜像被误当成发布版。

`--keep-cache` 现在是默认行为（`--no-keep-cache` 为其反向别名，等价于 `--full-clean`）。
保留缓存时 `lb clean` 只做 `--binary --chroot`，`cache/bootstrap/`（整棵树）和
`cache/packages.*` 都留着，重复构建直接从 chroot 阶段起步，省掉约 700 MB 下载；
需要干净的 chroot / 换过镜像源时再加 `--full-clean`。

**Agent 控制面参数**（生成 `/etc/raidforge/agent.yaml`，不写 token）：

```bash
./build.sh --server-url http://192.168.40.132:8080 \
           --api-prefix /api/raidforge \
           --poll-mode short \
           --ca-bundle /etc/raidforge/ca.pem   # 可选，仅 HTTPS 使用
```

`--server-url` 默认 `http://192.168.40.132:8080`，HTTP/HTTPS 均可；`--poll-mode`
v1 只接受 `short`。Agent 的默认 token 是 `raidforge`（见 §5.2.2），不会写进
`agent.yaml`，可由运行时环境覆盖。

`build.sh` 在清理之后会做一次**缓存一致性检查**：`.build/<stage>` 标记还在、
但对应的 `bootstrap/`（或 `chroot/`）目录已经不存在时，自动把这些标记删掉重新
激活该阶段——这正是下面第 2 条坑的自动修复。

镜像默认指向 `mirrors.163.com`（含 `bookworm-security`）。换源不必改脚本：

```bash
MIRROR=http://mirrors.aliyun.com/debian/ \
SECURITY=http://mirrors.aliyun.com/debian-security/ ./build.sh --full-clean
```

> **保温缓存的两个坑**（都踩过）：
> 1. 缓存里的 bootstrap 树带着上一轮的 `chroot/etc/apt/sources.list`，
>    `lb chroot_archives` 见到文件已存在就不会重写。换过镜像源后必须
>    `./build.sh --full-clean`（或手工 `rm -rf cache/bootstrap bootstrap .build`）再构建，否则新源不生效。
> 2. 真正的阶段标记是 `.build/` 下的空文件，不是 `config/stage`；手工删过
>    `bootstrap/` 之后若不删 `.build/bootstrap`，live-build 会打印
>    `W: Skipping bootstrap, already done`，随后在空 chroot 里执行命令，报
>    `chroot: failed to run command '/usr/bin/env'`。现在脚本会自动 re-arm，
>    日志里会有一行 `stale .build/bootstrap marker but bootstrap/ is missing - re-arming`；
>    但如果是**缓存本身**被清空，仍然需要 `--full-clean`。

> **厂商 CLI 权限**：vendor 压缩包解出来通常是没有 +x 的 0644，而
> `includes.chroot` 会原样保留权限。`build.sh` 的 preflight 会自动补 +x（日志
> `added missing +x`），`010-install-app.hook.chroot` 在镜像里再 `chmod 0755` 兜一次，
> 两处都失效时只会静默退化成 generic 模式。

产出（`<YYYYmmdd>` 是构建当天日期，`--fast` 会多一个 `-test` 后缀）：

```
raidforge-<version>-<YYYYmmdd>.iso
raidforge-<version>-<YYYYmmdd>.iso.sha256
```

写入 U 盘（会整体擦除目标设备，`sdX` 换成实际盘符，不要带分区号）：

```bash
sudo dd if=raidforge-<version>-<YYYYmmdd>.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

> UEFI + BIOS 双引导由 `--binary-images iso-hybrid --bootloaders syslinux,grub-efi`
> 提供。`dd` 出来的盘在两种固件下都能起；如果用 Ventoy/Rufus，请选择 **DD 模式**而非 ISO 模式。

### Python 依赖为什么走 wheel

Debian bookworm 源里的 `textual` 版本不可控，而 `App.export_screenshot()`、
`DataTable` 的光标/焦点行为、`run_test()` 的 pilot 语义都与版本相关（7.5.0 与 8.x
差得很远）。所以 `app/requirements.txt` 写死 `textual==7.5.0`，`build.sh` 会：
`pip download --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 --python-version 3.11 --abi cp311`
把 wheel 拉到 `vendor/wheels/`，hook 里 `pip install --no-index --find-links` 离线安装，
保证每次构建出的 ISO 依赖一致，且构建过程可完全离线。

DOCX 报告还需要 `python-docx==1.1.2`、`Pillow==10.4.0` 和 `lxml==4.9.4`（python-docx
的 XML 后端）。Pillow 同时负责把 Textual 的 SVG 截图在本地栅格化，**不安装
`librsvg2-bin` 之类的系统包**；锁定 `lxml` 4.9.4 是为了拿到
`manylinux_2_17` 轮子。这两个平台标签缺一不可：bookworm 的工具链能装 2_17，
较新的构建环境只会给 Pillow 提供 2_28。改依赖后需
`rm -rf vendor/wheels && ./build.sh --vendor-only` 重新下载（脚本只在目录为空时下载）。

### 同步到构建主机（192.168.40.132）

构建在另一台 Debian 12 主机上跑，本地改完必须推过去：

```bash
./tools/sync.sh              # 只传变动文件（size+mtime 比对，幂等），默认含证据私钥
./tools/sync.sh --dry-run    # 先看会传什么，不落盘
./tools/sync.sh --all        # 无视 mtime 全量重传
./tools/sync.sh --no-keys    # 不上传证据私钥（共享构建机时用，需手工放置）
./tools/sync.sh --watch      # 常驻监听，改一个传一个
./tools/sync.sh --run "./build.sh"   # 同步后在远端跑命令（不传文件）
make sync / make remote-build        # 同上两个的 make 封装
```

默认目标 `/root/raidforge`，可用 `REMOTE_DIR=...` 覆盖；账号密码来自
`tools/.remote.env`（已 gitignore）。

> **私钥默认同步**：`config/includes.chroot/etc/raidforge/keys/` 下的证据上传私钥会
> 随默认同步进入构建主机并烘进 ISO。没有 key 的 ISO，`try_rsync()` 会直接返回
> `ssh key missing`，一次会话的证据只会进 pending 队列——这是现场“文件上传失败”
> 最常见的原因。同步完成后远端会把 key 目录强制 0700、私钥强制 0600（OpenSSH
> 拒绝 group/other 可读的私钥）。本地目录为空时 `sync.sh` 与 `build.sh` 都会告警。
> 确实不想把私钥放到共享构建机时用 `--no-keys`，再手工把 key 放到远端同一路径；
> `--with-keys` 保留为兼容别名（默认已包含 key）。

几个已经踩过的点，脚本里都处理了：

- **不上传** `cache/ chroot/ binary/ .build/ *.iso`：这些是构建主机的产物，传回来既慢又会把构建状态搞坏。
- **Git Bash 路径改写**：`/root/...` 会被改写成 `C:/Program Files/Git/root/...`，在远端建出一个字面量 `C:` 目录。脚本内 `export MSYS_NO_PATHCONV=1` 关掉它，同时用 `cygpath -w` 把本地路径转成 Windows 形式（否则原生 python 的 `os.walk` 一个文件都找不到）。
- **可执行位**：Windows 上 `stat()` 没有 exec 位，SFTP 传完全是 0644，`./build.sh` 会 permission denied。传完在远端统一 `chmod 0755` 一次（`build.sh`、`auto/*`、`config/hooks/live/*`、`tests/*.sh`、厂商 CLI）。
- **解释器**：必须挑一个真有 paramiko 的 python（本机是 `C:/Python312/python.exe`）；`py` 启动器会读脚本 shebang 落到别的解释器上，出现「装了却 import 失败」。

---

## 4. 启动流程（约束 2）

```
固件 → isolinux/grub（10s 倒计时，无人值守自动进默认项）
     → 内核参数 boot=live components hostname=raidforge username=root ip=dhcp quiet
     → live-boot 挂载 squashfs → systemd
     → systemd-networkd（并行启动，不阻塞；所有非 lo 网卡各自 DHCP）
     → getty@tty1 (--autologin root) → login → /root/.bash_profile
     → 判断 tty == /dev/tty1 且 /run/raidforge.done 不存在
     → exec /usr/local/sbin/raidforge-tui → venv python -m raidforge
```

### 引导菜单倒计时

机架上的机器不会有人等着按键，所以两个固件路径都设了 **10 秒倒计时后自动进默认项**：

| 引导 | 配置 | 值 |
|---|---|---|
| BIOS (isolinux/vesamenu) | `isolinux/isolinux.cfg` | `timeout 100`（单位是 1/10 秒）+ `prompt 0` |
| UEFI (grub) | `boot/grub/grub.cfg` | `set timeout=10` + `set timeout_style=menu` |

菜单底部会实时显示 `Automatic boot in 10 seconds... Press [Tab] to edit options`；
**按任意键即停止倒计时**，fail-safe / Utilities 仍然一个键就能选到。

实现方式是 `config/hooks/live/050-boot-timeout.hook.binary`（binary 阶段、`cd binary` 之后执行，
此时 live-build 已经把两个 cfg 生成好、ISO 还没打包），所以不依赖 live-build 版本里的
`lb config` 选项（20230502 根本没有 timeout 参数）。想改秒数：

```bash
RAIDFORGE_BOOT_TIMEOUT=5 ./build.sh
```

### 网络（多网卡 DHCP）

`ip=dhcp` 只作用于 **initramfs** 阶段；进入系统后由 `systemd-networkd` 接管。
镜像内置 `etc/systemd/network/10-raidforge-dhcp.network`，匹配所有 `Type=ether` 且非 `lo`
的网卡（`eno1` / `ens33` / `eth0` … 都匹配），每块网卡独立发 DHCP，
**任意一块拿到 IPv4 就算网络就绪**。TUI 不会等网络：Dashboard 先显示
`waiting for DHCP`，就绪后变成 `ready (ens33 192.168.40.50)`；
会话结束时上传最多等 30 秒（见 5.3）。

排查：

```bash
ip -o -4 addr show scope global     # 与 TUI 判定用的是同一条命令
networkctl status                   # systemd-networkd 视角
journalctl -u systemd-networkd -b   # 一直没 lease 时看这里
```

> 不要看 `/etc/network/interfaces`：镜像里没有 ifupdown，live-config 运行期写出的那段
> `iface dhcp inet static` 是死配置。`systemd-resolved` 也仍是关闭的
> （`040-services.hook.chroot`），所以接收端地址请用 **IP 字面量**。

### 退出菜单

TUI 正常退出（`Q`）后会进入选择菜单：`1` 关机 / `2` 重启 / `3` 再次启动 TUI / `4` shell。

* 第 4 项进入的是**子 shell（非 login shell）**，敲 `exit` 会回到这张菜单，不再退出
  整个登录会话；`Ctrl-D`（EOF）才结束会话。
* 第 3 项会先删 `/run/raidforge.done` 再启动 TUI；TUI 退出后又重新创建该标记，
  所以下一次自动登录会停在 root shell，避免 "退出即重启 TUI" 的死循环。
  手工再次进入：`rm -f /run/raidforge.done` 或 `/usr/local/sbin/raidforge-tui`。
* 维护用别名：`.bashrc` 里提供了 `raidforge-tui`（不录制）/ `raidforge-plan`（只打印计划）/`raidforge-ev`（列出证据）。
* 内核参数在 `auto/config` 的 `--bootappend-live` 一处维护。

---

## 5. 远端接收端配置

### 5.0 接收端参数放在哪里（先看这张表）

IP、用户名、私钥这些参数**只在两个地方**有实际作用：仓库里的构建输入，和 ISO
里的运行期文件。下表一行对应一个参数，直接告诉你「改仓库哪个文件 / 运行期落到哪 /
谁写的 / 重启后还在不在」。

| 参数 | 仓库里改这里 | ISO 运行期路径 | 谁写 | 持久性 |
|---|---|---|---|---|
| 接收端 IP `ssh_host`（出厂值） | `config/hooks/live/010-install-app.hook.chroot` 的工厂模板 heredoc | `/etc/raidforge/config.yaml`（`/opt/raidforge/config.yaml` 是它的符号链接） | 构建 hook，每次构建重新生成 | 烘进镜像，永久 |
| 优先传输 `transport`（`ssh`/`http`，出厂 `ssh`） | 同一 hook 的 `report:` 段 | `/etc/raidforge/config.yaml` | 构建 hook；TUI 可改 | 永久默认；TUI 写入仅本次开机 |
| 接收端地址/端口（现场值） | 不用改仓库：TUI Dashboard 按 `S` | `/etc/raidforge/config.d/90-site.yaml` | TUI 设置页保存 | **仅本次开机**（Live 的 `/etc` 是 tmpfs） |
| `ssh_user` / `ssh_key_path` / `ssh_remote_root` | 同一 hook 的 `report:` 段 | `/etc/raidforge/config.yaml` | 构建 hook；TUI 只读 | 永久；可被 `config.d/*.yaml` 或 `RAIDFORGE_REPORT_*` 覆盖 |
| `ssh_port` / `http_port`（出厂 22 / 8080） | 同一 hook 的 `report:` 段 | `/etc/raidforge/config.yaml` | 构建 hook；TUI 可改 | 永久默认；TUI 写入仅本次开机 |
| `http_host` / `http_port`（HTTP 证据接收端） | 同一 hook 或 `config.d` | `/etc/raidforge/config.yaml` | 构建 hook；TUI 可改；`http_host` 为空且 `transport=http` 时跟随 webplane | 永久默认；TUI 写入仅本次开机 |
| `http_base_url`（完整 URL 覆盖） | 同一 hook 或 `config.d` | `/etc/raidforge/config.yaml` | 手工显式设置；TUI 保存时清空旧自动推导值 | 永久默认；TUI 写入仅本次开机 |
| webplane 地址/端口 `webplane.host/port/scheme` | 不用改仓库：TUI Settings → Connection | `/etc/raidforge/config.d/90-site.yaml` + `/run/raidforge/agent.env` | TUI 设置页保存并立即重连 Agent | **仅本次开机**（Live 的 `/etc`、`/run` 都是 tmpfs） |
| Agent 控制面 URL `server_url` | `build.sh --server-url`（默认 `http://192.168.40.132:8080`） | `/etc/raidforge/agent.yaml` | `build.sh` 生成；可被 `RF_AGENT_SERVER_URL` / `/run/raidforge/agent.env` 覆盖 | 烘进镜像；运行时环境优先 |
| Agent token | 代码默认 `raidforge`；运行时可覆盖 | 内存 / `/run/raidforge/agent.env` | `RF_AGENT_TOKEN`、U 盘 `RAIDFORGE-CFG/agent.env`、内核参数 `rf.token=` | **不落盘**；生产必须更换 |
| 证据上传私钥 | `config/includes.chroot/etc/raidforge/keys/evidence_ed25519`（文件名必须与 `ssh_key_path` 的 basename 一致） | `/etc/raidforge/keys/evidence_ed25519`（目录 0700 / 文件 0600） | 人工放置一次；`sync.sh` 默认上传 | 永久（每次构建进 squashfs） |
| `RAIDFORGE_REPORT_*` 环境变量 | `config/includes.chroot/etc/raidforge/env` | `/etc/raidforge/env` | 人工（默认全部注释） | 永久；优先级最高，会盖掉 TUI |

> `app/config.yaml.example` 只是字段参考，**不会被安装进镜像**。镜像内工厂模板的
> 唯一权威是上面那个 hook 的 heredoc；直接改 `/etc/raidforge/config.yaml` 或
> `/opt/raidforge/config.yaml` 都会被下一次构建覆盖。

配置读取优先级（低 → 高）：dataclass 默认值 < `/etc/raidforge/config.yaml` <
`/etc/raidforge/config.d/*.yaml`（按文件名排序）< `--config` < `RAIDFORGE_*` 环境变量 <
`--set` / TUI 内存值。

### 5.1 主通道：rsync over SSH（推荐）

接收机上（`rsync` 是硬性依赖：擦除端调用的是本机 `rsync`，远端也必须装好）：

```bash
sudo apt install -y rsync openssh-server
sudo useradd -r -m -d /srv/evidence -s /bin/bash evidence
sudo install -d -o evidence -g evidence -m 0755 /srv/evidence/incoming
sudo install -d -o evidence -g evidence -m 0700 /home/evidence/.ssh

# 在管理机上生成专用密钥（无人值守上传不支持口令交互，见下）
ssh-keygen -t ed25519 -N '' -C erase-box -f erase_ed25519
cat erase_ed25519.pub            # 追加到 /home/evidence/.ssh/authorized_keys
```

`authorized_keys` 必须是 `600`、归属 `evidence`，`sshd` 必须已启动（`systemctl
status ssh`）；否则探测会停在 `Permission denied (publickey)`。

#### 为什么密钥没有口令，`BatchMode` 是否必要

`BatchMode=yes` **不是**“必须无口令”的原因，它只是把交互式提示变成**立即失败**。
上传发生在擦除任务内（TUI 仍在前台）：没有 ssh-agent、没有 askpass，单次上传预算由
`report.run_upload_seconds` 控制（默认 30 秒）；如果 ssh 停下来等待口令，会拖住当前
擦除任务并与 `/dev/tty` 抢输入。`BatchMode=yes` 让这种情况立即失败并在任务日志/报告
状态中显示，而不是挂起。

无口令私钥被烘进 squashfs，确实意味着**拿到 ISO 的人可以使用该密钥**，这是静态
安全上的真实代价。更稳妥的运维做法（本镜像未强制实施）是：在接收端
`authorized_keys` 用 `from=`、`restrict`、`command=` 或 `rrsync` 限制该密钥只能写入
`/srv/evidence/incoming`；每个站点使用独立密钥；把 ISO 当作凭证介质管理；HTTP
通道保持在内网/VPN 后（参考接收端**无鉴权**）。

私钥放进**镜像**（这是唯一会被烘进 ISO 的路径）：

```bash
install -d config/includes.chroot/etc/raidforge/keys
cp erase_ed25519 config/includes.chroot/etc/raidforge/keys/evidence_ed25519
chmod 600 config/includes.chroot/etc/raidforge/keys/evidence_ed25519
```

> `tools/sync.sh` **默认上传**这个私钥；`--no-keys` 才排除（共享构建机时用）。
> 没有 key 的 ISO 里 `ssh_key_path` 指向不存在的文件，`try_rsync()` 直接返回
> `ssh key missing`，rsync 通道永远不会启动。无论是否走同步，远端都会在传完后把
> `keys/` 改成 0700、私钥改成 0600——OpenSSH 拒绝 group/other 可读的私钥。本地
> 目录为空时 `sync.sh` 与 `build.sh` 都会告警，避免静默产出无法上传的镜像。
> `--with-keys` 保留为兼容别名（默认已包含 key）。

#### 接收端地址的优先级与日常操作

| # | 位置 | 谁写 | 内容 | 何时用 |
|---|---|---|---|---|
| 1 | `/etc/raidforge/config.yaml` | 构建期工厂模板（`010-install-app.hook.chroot` 每次构建重新生成） | `transport: ssh`、`ssh_host: 192.168.40.129`、user `evidence`、key `/etc/raidforge/keys/evidence_ed25519`、`ssh_port: 22`、`http_port: 8080`、remote root `/srv/evidence/incoming` | 改**出厂默认值**（烘进镜像，永久生效） |
| 2 | `/etc/raidforge/config.d/90-site.yaml` | TUI `S` 设置页（或手工写） | TUI 写入 `report.transport`、`ssh_host`、`ssh_port`、`http_host`、`http_port`、`http_base_url: ""` 与 `webplane.host/port/scheme`；其它键保留 | 现场临时改传输方式/地址/端口/控制面 |
| 3 | `/etc/raidforge/env` 里的 `RAIDFORGE_*` | 人工 | 任意配置项（`__` 分隔层级，如 `RAIDFORGE_REPORT_TRANSPORT`） | 调试/自动化；**优先级最高**，默认注释掉 |

- **日常改接收端**：开机 → Dashboard 按 `S` → 选择 `SSH` 或 `HTTP`（选中的方式优先，
  另一种仍兜底）→ 输入 IPv4 和端口（SSH 默认 22，HTTP 默认 8080；可留空 IP = 只进
  不上传）→ `Enter` 保存。写入 `config.d/90-site.yaml` 并立即生效，下一次擦除上传与启动补传
  都现读配置。保存后设置页只探测**当前选中**的通道：SSH 验证登录 + 远端根目录可写；
  HTTP 请求 `/healthz`。成功显示 `receiver <ip> reachable`；失败显示
  `saved, but <SSH|HTTP> check failed: ... - will try <另一种>, then queue locally`。
  **失败不会回滚刚保存的值**，证据会进待传队列，修好网络后在 Dashboard 按 `U` 补传。
- **用户名 / 私钥 / 远端根目录在 TUI 里只读**：它们随镜像固定。要改就改
  `config/hooks/live/010-install-app.hook.chroot` 的工厂模板后重新构建，或在
  `config.d/` 里写一个排序更靠后的片段（如 `99-site.yaml`），才能盖过 TUI 写入的
  `90-site.yaml`。SSH/HTTP 端口与优先传输方式可以直接在设置页改。
- `RAIDFORGE_REPORT_TRANSPORT`、`RAIDFORGE_REPORT_SSH_HOST`、`RAIDFORGE_REPORT_SSH_PORT`、
  `RAIDFORGE_REPORT_HTTP_PORT`、`RAIDFORGE_REPORT_HTTP_BASE_URL` 在 `env` 中默认**注释掉**：
  它们的优先级高于 TUI，一旦启用，TUI 的修改不会生效，设置页会明确提示。
- **tmpfs 提醒**：Live 系统的 `/etc` 在内存里，TUI 保存的 `90-site.yaml` 只在本次
  开机有效；永久默认值必须改 hook 模板后重新构建 ISO。

工厂模板的样子（**不要**在 kept chroot 里手改它，构建会覆盖）：

```yaml
report:
  transport: ssh
  ssh_host: "192.168.40.129"
  ssh_user: "evidence"
  ssh_port: 22
  ssh_key_path: "/etc/raidforge/keys/evidence_ed25519"
  ssh_remote_root: "/srv/evidence/incoming"
  http_host: ""          # 空 = transport=http 时跟随 webplane
  http_base_url: ""      # 显式完整 URL 覆盖（一般留空）
  http_port: 8080
webplane:
  host: ""               # 空 = 跟随 /etc/raidforge/agent.yaml 的 server_url
  port: 8080
  scheme: http
  api_prefix: "/api/raidforge"
```

#### 上传失败排查（擦除完成后在 Tasks/Evidence 页或 root shell 里）

```bash
ls -l /etc/raidforge/keys/evidence_ed25519          # 必须存在且 0600
ssh -p 22 -i /etc/raidforge/keys/evidence_ed25519 \
    -o BatchMode=yes evidence@192.168.40.129 \
    "test -d /srv/evidence/incoming && test -w /srv/evidence/incoming && echo OK"
rsync --version                                     # 擦除机与接收机都要有
ip -o -4 addr show scope global                     # 至少要有一块网卡拿到 IPv4
```

四条全过仍然失败时，检查接收端的 `authorized_keys` 权限、`sshd` 是否运行，以及
接收端是否安装了 `rsync`——设置页的探测只验证登录和目录可写，**不验证远端
rsync**。

### 5.2 RaidForge Control Plane（webplane）

`webplane/` 是独立的 Agent 控制面（Flask + Vue 3 + Element Plus），负责 agent 注册/
心跳、任务下发、进度与结果回收、证据上传和取消控制。**UI 与 `/api/cp` 不做认证**，
只能部署在内网/VPN 后。

> webplane 现在同时提供证据接收端 `POST /sessions`（multipart `file` + `server_sn`
> + `session_id`，tar.gz 解包到 `WEBPLANE_SESSION_DIR`，带路径穿越/链接校验与
> sha256 去重）。`GET /healthz` 继续用于设置页探活；HTTP 证据上传的默认目标就是
> webplane 的地址/端口（`report.http_host` 为空且 `transport=http` 时）。
> 该端点与 UI/`/api/cp` 一样不做认证，只能部署在内网/VPN 后。

启动（开发机需要 Python 3.11+ 与 Node 20+）：

```bash
cd webplane
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cd web && npm install && npm run build && cd ..
WEBPLANE_DATA_DIR=/srv/raidforge-control python -m webplane
# UI: http://<host>:8080/   API: /api/cp/*
```

也可以分两段跑：`python -m webplane` 起后端，`cd web && npm run dev` 起 Vite
（5173，`/api` 自动代理到 8080）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEBPLANE_DATA_DIR` | `./webplane-data` | SQLite、任务证据与 `sessions/` 根目录 |
| `WEBPLANE_SESSION_DIR` | `<data>/sessions` | HTTP `POST /sessions` 的会话归档目录 |
| `WEBPLANE_MAX_SESSION_BYTES` | `536870912` | 单个 HTTP 会话归档上限（512 MiB） |
| `WEBPLANE_AGENT_TOKEN` / `RAIDFORGE_CP_TOKEN` | `raidforge` | 校验 `X-Raidforge-Token`；显式设为空串则关闭校验 |
| `WEBPLANE_LEASE_SECONDS` | `900` | 任务租约时长（覆盖 agent 确认屏） |
| `WEBPLANE_AGENT_OFFLINE_SECONDS` | `45` | 超过该时间无心跳判离线 |
| `WEBPLANE_PORT` / `WEBPLANE_HOST` | `8080` / `0.0.0.0` | 监听地址 |

Agent 侧默认配置：`server_url: http://192.168.40.132:8080`、`api_prefix:
/api/raidforge`、`token: raidforge`。`server_url` 支持 `http://` 与 `https://`，
不再限制回环地址；HTTP 为明文传输（令牌与证据不加密），建议只在受控内网使用。
服务端 agent token 默认也是 `raidforge`（`WEBPLANE_AGENT_TOKEN`），两端一致即可
直接联调；token 不写入 `agent.yaml`。控制面端点（与 agent 客户端一致）：

```
POST /api/raidforge/agent/heartbeat
GET  /api/raidforge/agent/tasks/next?server_sn=...
GET  /api/raidforge/agent/tasks/{task_id}
POST /api/raidforge/agent/tasks/{task_id}/started|progress|evidence|result|release
GET  /api/raidforge/agent/tasks/{task_id}/cancel_requested
```

#### 5.2.1 UI 层级：服务器 → 阵列卡 → 硬盘/RAID

前端不再以“任务”为第一入口，而是按现场对象组织：

1. **服务器**（`/servers`）：已注册 agent 列表，显示在线状态、SN、型号、阵列卡数量、
   最后心跳与当前任务。
2. **服务器详情**（`/servers/:agentId`）：硬件信息 + 阵列卡列表；阵列卡项显示
   厂商/型号、固件、工具、物理盘数、RAID 数与数据采集时间。
3. **阵列卡详情**（`/servers/:agentId/controllers/:controllerId`）：
   - **硬盘** Tab：serial、WWN、容量、介质、状态、RAID 成员、启动保护；可多选后
     “擦除磁盘”或“用选中盘建 RAID”。
   - **RAID** Tab：VD 列表与成员；可“创建 RAID”“删除 VD”“清除控制器配置”。

数据来源与刷新：

- 只读任务的 `list_pds` / `list_vds` 结果会缓存到 `controller_snapshots`，阵列卡页面
  直接读取缓存。
- 首次打开某个阵列卡且缓存缺失时，自动下发一次 `list_pds` + `list_vds`；之后由
  “刷新数据 / 刷新硬盘 / 刷新 RAID”按钮手动触发。
- 所有写操作仍通过 `POST /api/cp/tasks` 下发，破坏性对话框默认 `dry_run=true`；
  提交后留在当前页，任务进度抽屉每 2 秒轮询，任务结束后自动重载当前阵列卡数据。
  任务失败时保留旧缓存并提示数据时间。

新增只读端点：

```
GET /api/cp/agents/<agent_id>/controllers
GET /api/cp/agents/<agent_id>/controllers/<controller_id>
```

旧 `/agents`、`/agents/:id` 会重定向到 `/servers`、`/servers/:id`。UI 仍无认证。

#### 5.2.2 Agent 客户端接入

Agent 位于 `app/raidforge/agent/`，随 ISO 一起启动；控制面不可达时只影响状态栏
（连续 6 次心跳失败后显示离线），本地 TUI 与擦除/建卷功能照常可用。

配置优先级（低 → 高）：

```
内置默认值 < /etc/raidforge/agent.yaml < /run/raidforge/agent.env
          < 进程环境变量 RF_AGENT_* < 显式覆盖
```

- 默认：`server_url=http://192.168.40.132:8080`、`api_prefix=/api/raidforge`、
  `token=raidforge`。构建时可用 `./build.sh --server-url ...` 写入镜像默认值；
  `agent.yaml` 只包含非秘密键。
- Token 覆盖顺序：`RF_AGENT_TOKEN` → `/run/raidforge/agent.env` →
  卷标 `RAIDFORGE-CFG` 的 U 盘 `agent.env` → 内核参数 `rf.token=` → 默认
  `raidforge`。运行时 token 只驻留内存/tmpfs；出厂默认值内置于代码，不写入
  `agent.yaml`、状态文件和证据。
- HTTP/HTTPS 都允许，不做回环或 TLS 强制；`verify_tls` / `ca_bundle` 仅对
  HTTPS 生效。生产环境建议使用 HTTPS 或至少放在受控内网。
- 自检（不执行任何写命令）：

```bash
cd /opt/raidforge && ./venv/bin/python -m raidforge.agent --selftest --dry
```

- **安全提示**：`raidforge` 是公开的出厂默认令牌，会随 ISO/代码分发；生产部署
  必须通过 `RF_AGENT_TOKEN` 或服务端 `WEBPLANE_AGENT_TOKEN` 更换为随机值，
  并避免在不可信网络使用明文 HTTP。

容器：

```bash
docker build -t raidforge-webplane webplane
docker run -d -p 8080:8080 -v rf-data:/data raidforge-webplane
```

#### 5.2.3 一键部署到控制面主机

`tools/deploy_webplane.sh` 按顺序完成：本地 `npm ci && npm run build` → `tools/sync.sh`
同步源码 → 单独上传 `webplane/web/dist` → 远端创建/复用 `webplane/.venv` 并安装
依赖 → 用 PID 文件重启服务 → 健康检查。

```bash
# 凭据来自 tools/.remote.env（gitignore）：
#   REMOTE_HOST=192.168.40.132  REMOTE_USER=root  REMOTE_PASS=...  REMOTE_PORT=22
./tools/deploy_webplane.sh
# 或 make webplane-deploy
```

默认在 `/root/raidforge` 下以 `WEBPLANE_PORT=8080`、
`WEBPLANE_AGENT_TOKEN=raidforge` 启动；数据在
`/root/raidforge/webplane-data`，日志 `webplane.log`，PID `webplane.pid`。
脚本只按 PID 文件重启 webplane，不会触碰远端正在运行的 ISO 构建。

HTTP 证据地址解析顺序（`core/report.py:http_endpoint`）：

1. 显式 `report.http_base_url`（旧版本 TUI 自动写入的 `http://<ssh_host>:<http_port>`
   会被识别为“旧推导值”并忽略）；
2. `report.http_host` + `report.http_port`；
3. `report.transport=http` 且 `http_host` 为空 → `webplane.host/port/scheme`；
4. 兼容旧行为：`http://<ssh_host>:<http_port>`。

Settings → Connection 现在把 **SSH 证据接收端**、**HTTP 证据接收端**、**webplane
控制面** 分成三组字段；HTTP 接收端 host 留空即跟随 webplane。保存会同时写
`config.d/90-site.yaml` 与 `/run/raidforge/agent.env`，并在没有破坏性任务时立即
切换 Agent 的控制面地址（有任务时延迟到任务结束）。永久默认值仍改 hook 工厂模板。

### 5.3 传输顺序与补传

1. **每轮擦除命令执行完**立即生成累计报告并做一次上传尝试（TUI 仍在前台），
   预算 `report.run_upload_seconds`（默认 30 秒），成功/部分成功/失败提交都会生成报告；
2. 如网络尚未就绪，先等默认路由或任意一块网卡拿到全局 IPv4，最多
   `report.wait_for_route_seconds`（默认 30 秒）；
3. 路由就绪 → 先试 `report.transport` 选中的方式（SSH 或 HTTP）；
4. 失败 → 再试另一种；
5. 两条都失败 → 当前擦除任务记录 `report_failed` / `upload` 事件并显示失败状态；
   **不会**等到退出程序再重试或补传；
6. `pending/` 仅在原有队列已有内容时由启动后台线程补传；本版本不再在退出时新增
   待传会话。
7. TUI 的 Dashboard 上按 `U` 可手动重刷一次。

> webplane 的 `POST /sessions` 会解包 tar.gz 并写入
> `WEBPLANE_SESSION_DIR/<session_id>/`（同时保留 `<session_id>.tar.gz`、`_receipt.json`
> 与 append-only `receipts.jsonl`）。重复上传同一 sha256 返回 200 `duplicate`。
> 该端点无认证，仅限内网/VPN；SSH/rsync 仍是默认首选通道（§5.1）。
>
> 退出阶段只重新计算 `manifest.json` 的哈希，不再生成 DOCX、不再上传；报告和上传
> 只在擦除命令执行完后发生。每轮即时上传共用同一把发布锁。

---

## 6. 使用说明

### 6.1 屏幕与按键

新界面是单壳七模块：顶部 1 行状态栏 + 3 行水平 Tab（激活项 `border: tab`）+ 1 行面包屑 + 动态内容区 + 1 行热键栏。80×25 下内容区 19 行。页面是常驻 Widget，切换后筛选、光标、滚动位置保留；NavBar 可聚焦，←/→ 移光标、Enter 激活，表格支持 J/K、Home/End、PgUp/PgDn、Space、Ctrl+A、Ctrl+I。

右下角有一个常驻的 **messages** 浮层（独立 `messages` 层，不占用 19 行内容区），显示 Agent 日志、本地任务日志和操作提示；心跳/轮询 INFO 自动过滤，WARN/ERROR 与任务消息保留。日志不再写到真实 stderr，所以不会覆盖 NavBar。被 push 的模态屏会暂时盖住该浮层，返回主屏后补显。Evidence 页的报告/上传状态已移到右上，右下空间留给消息浮层；Settings 表单底部留有滚动余量，Save/Probe 不会被浮层遮挡。

| 页面 / 压栈屏 | 内容 | 按键 |
|---|---|---|
| **Dashboard** | 2×2 卡片（Server / Controllers / Storage / Health）+ Recent Tasks | `←/→` 切 Tab · `Enter` 进入内容 · `F5` 刷新 · `Ctrl+P` 命令面板 |
| **Controllers** | 左侧控制器列表 + 右侧摘要/VD；`Enter` 压入详情 | `←/→` 切 Tab · `Enter` 详情 · `F7` 建卷 · `F5` 刷新 |
| **Physical** | 筛选条 + 80 列满宽多选表 + 选中行底部抽屉；VD 成员/UNKNOWN/blocked 灰显不可选 | `←/→` 切 Tab · `Enter` 进入表格 · `Space` 勾选 · `A` 全选 · `N` 清空 · `/` 搜索 · `F8` 擦除（红） |
| **Virtual** | 同构满宽布局；`Enter` 压入成员盘 | `←/→` 切 Tab · `Enter` 成员 · `F7` 建卷（红）· `F9` 删卷（红） |
| **Tasks** | 串行任务队列 + RichLog 实时日志 | `←/→` 切 Tab · `C` 取消 · `F5` 刷新 |
| **Evidence** | 会话列表 + 右上报告/上传状态 + 文件树 | `←/→` 切 Tab · `R` 重扫 · `F5` 刷新 |
| **Settings** | 左分类 + 右表单（连接/上报/证据/外观/关于） | `←/→` 切 Tab · `Enter` 保存 · `Tab` 字段 |
| **ControllerDetail**（压栈） | 控制器 KV、VD 表、物理盘表 | `F7` 建卷 · `F9` 删卷 · `F10` 清配置（红）· `F5` · `Esc` |
| **RAID 向导**（压栈） | ①级别 ②选盘 ③预览（span/容量/warnings/命令序列）④ConfirmModal | `0/1/5/T` 级别 · `Space` 选盘 · `Enter` 下一步 · `Esc` 返回 |
| **VolumeMembers**（压栈） | VD 成员盘详情 | `Esc` 返回 |
| **ConfirmModal** | 目标摘要、逐条 dry-run 命令、确认输入 | 擦除/建卷/删卷/清配置统一输入 `yes`；`Tab` 切换输入/按钮，`Esc` 取消 |
| **命令面板 / 帮助** | `Ctrl+P` 模糊过滤命令；`F1` 上下文帮助 | `Up/Down` · `Enter` · `Esc` |

全局键：NavBar 聚焦时 `←/→` 直接切换页面，`Enter` 进入当前页内容，`Esc` 从内容返回 NavBar；`Ctrl+Q` 退出（运行中任务第一次只取消），`Ctrl+P` 命令面板，`F1` 帮助，`F5` 全量刷新。

> 磁盘表进屏即获得键盘焦点，不用先按 `Tab`；`Tab` 只是把焦点从别处移回表格。
> 光标是**首列的 `>` 文本标记**（不是背景色块）。16 色控制台上 Textual 的行高亮
> 只会染上该行最后一个单元格，看起来像渲染坏了；文本标记在任何终端、任何配色下都在。
>
> Progress 屏的 `Q` 语义：擦除**运行中**第一次按只停止后续提交（不打断正在执行的
> 命令，屏幕会先显示 cancelling 再落到 STOPPED 汇总），再按一次退出应用；
> 擦除**已结束**后按 `Q` 直接退出应用——不会再出现卡在 cancelling 假状态的问题
> （回归测试：`tests/test_quit_path.py`）。

### 6.1.1 双主题与 16 色回退（重要）

默认主题是方案 7.2 的深蓝配色：背景 `#002B49`、高亮 `#4FC3F7`、危险 `#FF5252`，
当前顶部 Tab 背景 `#0066A1`，焦点色为高对比的琥珀 `#FFC107`（`$focus`）。
`TERM=linux` 不再自动把主题切到黑白；单色主题只在显式选择时使用：

| 条件 | 主题 |
|---|---|
| `auto` / 未配置 / `raidforge` | `raidforge`（7.2 深蓝，默认） |
| `RAIDFORGE_THEME=mono` 或 `ui.theme: raidforge-mono` | `raidforge-mono`（纯 `#000000` / `#FFFFFF`） |
| `RAIDFORGE_MONO=1` | `raidforge-mono`（显式环境变量） |
| `RAIDFORGE_THEME=blue` | 强制深蓝 |
| Settings → Appearance | 本次会话手动切换 |

TCSS 只引用主题变量（`$background`/`$accent`/`$error`/`$focus`/`$nav-active-bg`/`$table-zebra` 等），
两套主题共用同一份布局。聚焦控件使用 `$focus` 高亮：表格光标、表头、输入框、按钮、
下拉、选项列表、树和导航 Tab 都有独立的 `:focus` 样式；单色主题下退化为纯白/黑反色。
面包屑右侧还会显示 `▸ Focus: <控件>`，任务运行时显示当前任务和进度。
`tests/test_mono_live.py` 在 mono 主题下检查实际渲染段只有纯黑/纯白；
`tests/test_mono_static.py` 检查 TCSS 不写死颜色且焦点变量存在。

### 6.1.2 RAID 管理（屏幕与流程）

```
顶部 Tab: Dashboard / Controllers / Physical / Virtual / Tasks / Evidence / Settings

Controllers --Enter--> ControllerDetail
                          |-- F7 --> RAID 向导（4 步）--> ConfirmModal(yes) --> Tasks --> 定向刷新
                          |-- F9 --> ConfirmModal(yes) -----------------------> Tasks --> 定向刷新
                          |-- F10 -> ConfirmModal(c0) ------------------------> Tasks --> 定向刷新
                          '-- F5 --> 只刷新该控制器

Physical --Space 选盘--> F8 --> ConfirmModal(yes) --> Tasks（擦除 + 报告）--> 只刷新相关盘
Virtual  --Enter--> 成员盘；F7 建卷；F9 删卷
Tasks    --Enter--> 实时日志；C 取消
```

**支持的级别与规则**

| 级别 | 规则 | 容量估算 |
|---|---|---|
| RAID0（单 VD） | N ≥ 1，所有选中盘组成一个 VD | Σ size |
| RAID0（一盘一 VD） | N ≥ 1，每块盘各建一个 RAID0 | 每 VD = 该盘容量 |
| RAID1 | 恰好 2 块盘 | min(size) |
| RAID5 | ≥ 3 块盘 | (N−1)×min(size) |
| RAID10 | ≥ 4 且偶数，每个 span 恰好 2 块盘 | (N/2)×min(size) |

- 只能选择**空闲盘**：不能是任何 VD 的成员、不能是 Foreign/Failed/Rebuilding，
  且状态必须在该厂商白名单内（LSI `UGood`、Adaptec `Ready`、HPE `unassigned`）。
  状态不确定时一律 blocked，绝不猜测。
- RAID10 的 span 配对是确定性的：按控制器/位置排序后相邻两盘成一组，预览会显示
  `1) e32/s0 + e32/s1 2) ...`。
- 条带固定 `raid.stripe_kb`（默认 256K）；不显式设置缓存策略；不显式指定初始化方式，
  走控制器默认（创建后可能仍在后台初始化，需重扫确认）。
- 名称可选，清洗为 `[A-Za-z0-9_-]`，最长 `raid.name_max_len`（默认 15）；一盘一 VD
  模式会自动加 `-d01`、`-d02` 后缀。

**删除 VD / 清除配置**

- `D` 删除光标所在 VD：LSI `delete force`、Adaptec `delete logicaldrive ... noprompt`、
  HPE `ld ... delete forced`。成员盘随后回到空闲状态。
- `X` 清除控制器配置：LSI `vall delete force` + `fall delete`；Adaptec
  `delete array ALL noprompt` + `uninit all`；HPE `ld all delete forced` +
  `modify clearconfig forced`。这会删除所有 VD，并清除 foreign/残余 RAID 元数据；
  **不会**重置控制器的缓存/BBU 等设置。

**执行语义**：命令串行提交，`rc==0` 只表示控制器接受；执行完会自动重扫并给出
`confirmed`（目标 VD 已经可见）/ `not_visible`（固件仍在后台构建）/ `mismatch`。
UI 不会把“已接受”写成“已完成”。

### 6.2 安全闸门（约束 8）

1. **UNKNOWN 介质不可勾选**。`smartctl` 的 `Rotation Rate` 为空、或厂商 JSON 的 `Med`
   为空时，该盘在表中不能被 Space 选中（`DiskInfo.selectable` 为 False），
   进入 `plan_for_selection()` 时还会再过滤一次。
2. **必须先过 ConfirmModal**，屏幕上逐条列出将要执行的命令原文。
3. **必须手工敲入 `yes`**（不分大小写）才能解锁；敲别的只提示不执行，且输入框会被清空，
   避免"再补一个字符就变成合法值"。
4. **RAID 写操作有独立闸门**：创建 RAID / 删除 VD / 清除控制器配置统一输入 `yes`；
   闸门同样逐条展示 dry-run argv。
5. 想先看看会执行什么而不动盘：`raidforge --dry-run --print-plan`
   （擦除计划）或 `raidforge --print-raid-plan`（控制器/VD/空闲盘/能力，纯只读）。

> 所有破坏性操作统一使用短确认词 `yes`（大小写不敏感）。`server_id.confirm_token`
> 仍写进 `manifest.json` / `events.jsonl` 作为审计元数据，但不再作为输入闸门。

### 6.3 完成语义（重要）

约束 12 明确：**不等擦除完成**。每条命令 `subprocess.run(timeout=60)`，
返回码 0 只代表**控制器已接受作业**，记为 `ASYNC_SUBMITTED`；真正的擦除在控制器固件里继续。

因此 Progress 屏里写的是 *submitted N/M*，没有任何百分比。这是刻意为之——
`storcli/arcconf/ssacli` 的 erase 都是异步任务，谎报完成度比不报更危险。

RAID 创建/删除同样不等待固件完成：`rc==0` 记为 `ACCEPTED`，随后自动重扫并记录
`observed=confirmed|not_visible|mismatch`。例如新建 RAID10 后立即重扫可能还是
`not_visible`，表示控制器仍在后台初始化，按 `R` 稍后复查即可。

需要确认真实完成情况时，请在 TUI 退出后的 root shell 里用厂商工具自行查询，例如：

```bash
storcli /c0/eall/sall show erase          # LSI 擦除进度
arcconf getstatus 1 task                  # PMC 任务状态
ssacli ctrl slot=0 pd all show status     # HPE
```

### 6.4 服务器 SN（约束 11）

`dmidecode -s system-serial-number` → 若返回 `Not Specified` / `To be filled by O.E.M.` /
`Default string` / 纯 `0` 等占位值，则依次回落到：

1. `dmidecode -s baseboard-serial-number`
2. `dmidecode -s system-uuid`（必须匹配标准 UUID 形态）
3. `UNK<sha256(uuid+mac)[:12]>` —— 由 UUID 与第一块网卡 MAC 派生，**同一台机器永远得到同一个值**

派生 SN 时 Dashboard 会用粗体 `!! WARNING` 告警（界面只有黑白两色，没有"红字"），
`manifest.json` 里的 `confirm_token` 随之变成 `CONFIRM-UNKxxxxxxxxxxxx`
（仅作审计元数据，输入闸门固定是 `yes`）。

### 6.5 证据（约束 9）

```
/var/log/raidforge-evidence/{server_sn}_{YYYYmmddTHHMMSSZ}/
├── events.jsonl          append-only，每行一个事件，写完 fsync
├── session.cast          asciinema 录制（包裹式，覆盖启动→操作→退出全过程）
├── {server_sn}-raidforge-report-YYYYMMDD.docx   服务器维度擦除报告（一次启动一份）
├── snapshots/
│   ├── 01-dashboard.svg
│   ├── 02-controllers.svg
│   ├── 02b-controller-detail.svg
│   ├── 02c-raid-create.svg
│   ├── 03-disk-list.svg
│   ├── 04-confirm.svg         ← 干跑命令 + 确认界面，核心审计材料
│   ├── 04-raid-confirm.svg    ← RAID 创建/删除/清配置的干跑确认
│   ├── 05-progress.svg
│   └── 05-raid-progress.svg
├── raid_actions.json         每次 RAID 写操作的完整记录（argv/rc/observed/前后拓扑）
├── controller_raw/           厂商工具原始输出（解析前），storcli JSON / ssacli detail / smartctl 等
├── server_id.json
└── manifest.json         除自身外全部文件的 path/sha256/size 清单（schema @2）
```

截图文件名里的序号由 `snapshot_counter` 递增决定（`01-`、`02-`…）；同一屏第二次出现时
序号继续增加，例如不进入设置页时第二轮擦除的确认/进度截图是 `05-confirm.svg`、
`06-progress.svg`。报告按截图的 `run_epoch` 归属到具体擦除记录（按开始时间标识），
不依赖文件名。

关键事件（可按 `event` 字段 grep）：

`session_started` → `detection` → `inventory` → `selection_confirmed_ui`（含 `run_epoch`）→
`confirm_modal_shown` → (`confirm_ack_rejected` | `confirm_ack_accepted`) →
`erase_run_started` → `erase_submit_started` ×N → `erase_submit` ×N →
`erase_run_finished` → `erase_run_recorded` → …（可多轮）→
`report_generated` / `report_failed` → `upload` / `tui_stopped`；

RAID 操作另有一条独立事件流：
`raid_confirm_shown` → (`raid_confirm_rejected` | `raid_confirm_accepted` | `raid_confirm_cancelled`) →
`raid_run_started` → `raid_submit_started` ×N → `raid_submit` ×N →
`raid_run_finished` → `raid_run_recorded` → `raid_rescan_after`（含 `observed`）。
`report_generated`、`report_failed`、`upload` 都带 `phase`；应用运行时只有
`phase=run`（每轮擦除结束）。`upload` 另带 `queued`（是否进入待传队列）。每轮结束
会各出现一次，因此同一会话可有多条 `phase=run` 记录；退出阶段不再产生报告/上传事件。
设置页保存会额外写 `settings_changed`（含新 IP），并在后台探测前后写
`settings_ssh_check_started` / `settings_ssh_check`（含 `ok` 与 `message`，失败时
保留 `detail` 的 stderr 尾部 2000 字符）。

### 6.5.1 数据擦除报告（DOCX）

每轮擦除命令执行完后，程序会把本次启动的**全部擦除记录**汇总成**一个** Word 报告，
命名 `<服务器序列号>-raidforge-report-<YYYYMMDD>.docx`。
**报告和上传都在擦除任务内立即完成**（TUI 不需要退出）；第二轮会覆盖同名文件并再次
上传，最终内容包含全部轮次。应用退出时只更新 `manifest.json`，不再生成报告或上传。

- 服务器序列号按 `[A-Za-z0-9._-]` 清洗，非法字符换成 `_`，太长截到 64 字符，
  为空时用 `UNKNOWN`；日期取**会话开始时间（UTC）**，不是报告生成时刻。
- 每轮结束时先生成报告；即时上传受 `report.run_upload_seconds`（默认 30 秒）限制；
  失败只写状态/事件，**不会**把仍在变化的会话复制进 pending，也不会等到 Q 退出再上传。
- 顶部信息表只有四项：**服务器序列号、会话 ID、检测工具、生成时间**。不写 SN 来源、
  主机名、操作模式或擦除轮次。
- 内容分两部分：
  - **一、服务器信息**：服务器序列号、会话开始时间；硬盘表列出本次启动扫描到的
    **全部磁盘**（包括未选中/不可选的盘），列为 序号 / 磁盘 ID / 容量 / 类型
    （如 `SSD / SATA`、`HDD / SAS`）/ 型号 / 序列号 / 设备或位置 / 固件或 RAID /
    本次擦除（`已选中`、`已选中（未成功）`、`未选中`）。
  - **二、擦除过程**：每条擦除记录按开始时间标识（不编号），包含计划/已提交/失败/
    超时/跳过/未提交汇总，以及逐盘明细：磁盘 ID / 方法 / **实际命令** / 状态 /
    返回码 / 耗时 / 详情。命令取自 `plan.dry_run_text`，与真正执行的 argv 一致。
- 失败、超时、跳过（未提交）、取消（含“取消未提交”和“未执行”）都会写进报告——
  报告要能解释“哪些盘没有擦”，而不是只列成功的。没有对应计划的提交结果会单独列出，
  不会借用其它磁盘的行。
- 截图在本地用 Pillow 从 Textual 的 SVG 转成 PNG 后嵌入；说明文字只写
  `确认界面（文件名）` / `提交结果（文件名）`，不写轮次。某张截图渲染失败时写成
  文字说明并保留原始 SVG，不影响其余内容。
- 排版按公文格式：标题 **方正小标宋简体 二号（22pt）居中不加粗**；一级标题
  **黑体三号（16pt）**、二级 **楷体_GB2312 三号**、三级 **仿宋_GB2312 三号**，
  均不使用自动编号；正文 **仿宋_GB2312 三号（16pt）**、首行缩进 2 字符
  （同时写 `firstLineChars` 与 640 twips 回退值，兼容 Word/LibreOffice）、固定行距
  28 磅；所有西文与数字统一 **Times New Roman**。
- **表格例外**：信息表、硬盘表和逐盘明细表按附表排版，使用
  **仿宋_GB2312 / Times New Roman 五号（10.5pt）**、单倍行距、零缩进；表格固定列宽，
  表头行跨页重复。图注与页脚小于正文。上述中文字体只写入 DOCX 字体名，ISO 不需要
  安装；阅读端缺失时 Word 会静默替换，打印前请确认目标机已装
  方正小标宋简体 / 仿宋_GB2312 / 楷体_GB2312。
- 报告生成失败只记 `report_failed`，擦除任务状态仍按擦除结果判定；不会在退出时补生成。
- 报告就写在会话根目录，随现有 rsync / HTTP / pending 整目录链路一起上传，并进入
  `manifest.json` 的 SHA-256 清单。manifest 同时记录：
  - `report_file` / `report_generated_at`；
  - `erase_runs`（每轮计划、结果、磁盘快照）；
  - `inventory`（本次启动的完整磁盘枚举）；
  - `disk_count`（等于 `inventory` 长度）；
  - `submissions`（本次启动所有轮次的实际提交结果，不再只是最后一轮或父进程的空值）。
- 包裹模式（asciinema）下由子 TUI 进程生成；父进程收尾时继承子清单里的
  `report_file`、`erase_runs`、`tool`、`disk_count`、`submissions`、`inventory` 并重新哈希，
  不会生成第二份，也不会让父子清单互相矛盾。
- **注意**：QEMU L4 演练报告必然显示 `QEMU NVMe Ctrl`、`ERASE01`、2.00 GB 等虚拟硬件；
  这是运行环境，不是报告造假。真实硬件验收必须在目标服务器上启动 ISO 后执行擦除，
  再把报告与 `events.jsonl` 的 `inventory`、`controller_raw/` 原始输出逐项对照。

### 6.6 命令行

```bash
python3 -m raidforge                       # 正常（会尝试录制）
python3 -m raidforge --dry-run             # 只记录 SKIPPED，任何命令都不发出
python3 -m raidforge --no-record           # 不套 asciinema
python3 -m raidforge --no-upload           # 不自动上报
python3 -m raidforge --print-plan          # 打印本次将要构造的命令后退出
python3 -m raidforge --config /path.yaml   # 额外配置文件（在 config.d 之后、env 之前）
python3 -m raidforge --set report.http_base_url=http://10.0.0.1:8080
python3 -m raidforge --set report.transport=http --set report.http_port=8080
```

`--config` 指向的文件不存在时静默忽略（脚本化调用方便）；读取顺序见 §5.0。
环境变量等价写法（优先级最高）：`RAIDFORGE_TOOLS_FORCED_TOOL`、`RAIDFORGE_REPORT_ENABLED`、
`RAIDFORGE_EVIDENCE_ROOT`、`RAIDFORGE_DRY_RUN`……；接收端相关的是 `RAIDFORGE_REPORT_TRANSPORT`、
`RAIDFORGE_REPORT_SSH_HOST`、`RAIDFORGE_REPORT_SSH_USER`、`RAIDFORGE_REPORT_SSH_PORT`、
`RAIDFORGE_REPORT_SSH_KEY_PATH`、`RAIDFORGE_REPORT_SSH_REMOTE_ROOT`、
`RAIDFORGE_REPORT_HTTP_PORT`、`RAIDFORGE_REPORT_HTTP_BASE_URL`、`RAIDFORGE_REPORT_RUN_UPLOAD_SECONDS`。

---

## 7. 阵列卡支持矩阵与命令表（约束 5/6/7）

探测优先级固定为 **storcli → perccli → arcconf → ssacli → sg3_utils**，第一个「存在且能应答」的工具胜出，
后续全部流程按该厂商走；每次探测（含失败）都写进 `attempts` 存证据。同一 rung 支持多个
二进制名：`storcli/storcli64`、`perccli/perccli64`、`ssacli/hpssacli`。

| 厂商 | 探测 | 枚举命令 | 介质判定 |
|---|---|---|---|
| LSI/Broadcom | `storcli show J`（或 `storcli64`） | `storcli /c{c}/eall/sall show J` | JSON 的 `Med`，缺失时回看 `Sp` 转速 |
| Dell PERC | `perccli show J`（或 `perccli64`） | `perccli /c{c}/eall/sall show J` | 同 storcli（同一语法） |
| PMC/Adaptec | `arcconf list` | `arcconf getconfigjson {c}`（失败回落 `arcconf getconfig {c} pd` 文本） | `MediaType`／`RotationRate` |
| HPE | `ssacli ctrl all show`（或 `hpssacli`） | `ssacli ctrl slot={c} pd all show detail` | `Rotational Speed` 含数字=HDD，含 Solid=SSD |
| Generic | `sg_scan` 存在 | `lsscsi -g` + `smartctl -i {dev}` + `nvme list -o json` | `Rotation Rate`（**唯一判据**） |

**RAID 读取 / 管理命令**（`app/raidforge/core/raid/commands.py` 与各 provider）：

| 厂商 | VD 列表/成员 | 创建 | 删除 VD | 清除配置 |
|---|---|---|---|---|
| LSI/PERC | `storcli /cN/vall show all J` | `add vd r0/r1/r5/r10 drives=E:S,... size=all strip=256 [pdperarray=2] [name=]` | `/cN/vM delete force` | `/cN/vall delete force` + `/cN/fall delete` |
| Adaptec | `arcconf getconfigjson N` / `getconfig N ld` | `create N logicaldrive [stripesize 256] [name X] MAX <level> <ch> <dev>... noprompt` | `delete N logicaldrive M noprompt` | `delete N array ALL noprompt` + `uninit N all` |
| HPE | `ssacli ctrl slot=N ld all show detail` + `pd all show` | `create type=ld drives=ref,... raid=0/1/5/1+0 ss=256 [label=X] forced` | `ld M delete forced` | `ld all delete forced` + `modify clearconfig forced` |
| Generic | 无 | 不支持 | 不支持 | 不支持 |

> ⚠️ **RAID 命令同样需要现场核对**：源码中标 `VERIFY` 的项包括
> `vall delete force` 的 force 接受度、`arcconf stripesize/name` 的位置、
> `uninit` 是否接受 `noprompt`、`ssacli clearconfig` 是否隐含删 LD、HPE 紧凑
> `pd all show` 的 unassigned 分组、以及 RAID10 span 顺序。上线前请先用
> `raidforge --print-raid-plan` 只读核对拓扑与空闲盘，再在实验室盘上按
> 「RAID0 单卷 → RAID0 一盘一卷 → RAID1 → RAID5 → RAID10 → 删除 VD → 清除配置」
> 顺序验证；确认后只改对应模板一行。

命令构造集中在 `app/raidforge/core/commands.py`，改擦除命令只需改那张表：

| 方法 | HDD | SSD |
|---|---|---|
| LSI | `storcli /c{c}/e{e}/s{s} start erase {thorough\|simple\|normal}` | `patterna`（blockerase）/ `secure`（sanitize，需 SED） |
| PMC | `arcconf TASK START {c} DEVICE {ch} {dev} erase NOPROMPT` | `secureerase`（blockerase）/ `sanitize` |
| HPE | `ssacli ctrl slot={c} pd {ref} modify erase erasepattern=overwrite forced` | `modify erase erasepattern=block`（blockerase）/ `erasepattern=crypto`（sanitize，需 SED） |
| Generic | SAS → `sg_format --format [--verify]`；SATA → `hdparm --security-erase[-enhanced]` | SAS → `sg_sanitize --block/--crypto`；SATA → `blkdiscard`；NVMe → `nvme format --ses=1/2` |

默认策略：HDD 一律 `thorough`；SSD 只在**明确知道是 SED**（LSI JSON 的 `SED: Y`）或
NVMe 时用 `sanitize`，其余一律 `blockerase` —— 不猜。

> ⚠️ **需要现场核对**：源码里标了 `VERIFY` 的行是依据常见固件语法整理的，
> 不同固件小版本（尤其 `storcli secure` 与 `ssacli` 的擦除模式）支持度不同。
> 上线前请在目标机型上先用 `--dry-run --print-plan` 核对命令，
> 必要时只需修改 `METHOD_TABLE` / `GENERIC_TABLE` 中对应的一行。
>
> HPE 一侧已按官方 CLI 参考校正过（`<target> modify erase [erasepattern=...]`，
> 合法模式只有 `zero|random_zero|random_random_zero|crypto|block|overwrite`，
> `block` 仅 SSD、`overwrite` 仅 HDD，没有独立的 `sanitize` 动词）。

---

## 8. 测试

```bash
pytest tests -q                      # 跨平台全量；装齐依赖并使用 7.5.0 时全绿
pytest webplane/tests -q             # 控制面：agent 协议 / 管理 API / 并发领取 / 阵列卡快照
./tools/check.sh                     # L0：bash -n + pytest + 探测链 mock + 80×25 UI 冒烟（~30s）
tests/test_detection.sh --mock       # Linux/WSL/macOS：用伪造 CLI 跑真实进程链
tests/test_detection.sh --real       # 在真机上打印它构造出的命令（不执行任何东西）
raidforge --print-raid-plan          # 只读：控制器/VD/成员/空闲盘/能力
```

镜像固定 Textual **7.5.0**（`app/requirements.txt`）。有 20 项测试直接驱动真实渲染
（`test_mono_live.py`、`test_quit_path.py`、`test_settings_screen.py`、
`test_report_live.py`），在其它 Textual 版本上会自动 skip 并打印原因；
`test_launcher_menu.py` 需要 POSIX pty，Windows 上整模块 skip。
DOCX 与截图相关测试（`test_svg_raster.py`、`test_erase_report.py`、`test_report_live.py`）
需要 Pillow / python-docx，缺依赖时整模块 skip 而不是假通过。

开发机（Windows + Textual 8.x、未装 python-docx/Pillow）会看到一部分 UI/DOCX
测试被 skip；装齐依赖并使用 7.5.0 后应在构建主机或 `/root/tv` 里全绿。`tools/check.sh` 只会挑版本匹配的解释器做 UI 检查，不匹配时明确
警告而不是给出误导性失败。

`test_detection.sh` 覆盖八种情形：storcli 胜出 / 降级到 arcconf / 降级到 ssacli /
降级到 sg3_utils / 一个工具都没有 / storcli64 别名 + RAID 拓扑 /
perccli64 别名 / hpssacli 别名。`tests/fixtures/` 里的样例输出可以直接改成你们
机房真实机型的输出来做回归。

### 在构建主机上验证「镜像里真正跑的那个版本」

`pytest` 用的是开发机上的 Textual，可能比 ISO 里装的新。要验证**出货版本**，
在构建主机上用 vendor wheel 建一个同版本 venv 再跑无头 UI 测试：

```bash
cd /root/raidforge
rm -rf vendor/wheels && ./build.sh --vendor-only   # 拉齐 python-docx / Pillow / lxml
python3 -m venv /root/tv
/root/tv/bin/pip install --no-index --find-links vendor/wheels -r app/requirements.txt
/root/tv/bin/python -m pytest tests -q    # 期望全绿（含 RAID、两轮报告与真实 SVG 栅格化）
/root/tv/bin/python tests/ui_smoke.py     # 五屏 + 勾选闸门 + token 闸门 + 证据落盘
```

`tests/ui_smoke.py` 走完整链路：Dashboard → Settings（改 IP 并保存）→
DiskList（Space 勾选、UNKNOWN 拒选）→ ConfirmModal（输错不放行、输 `yes` 放行）→
Progress，并断言 `events.jsonl` 与 SVG 快照确实落盘。

### 8.1 验证阶梯（由快到慢）

| 层 | 命令 | 耗时 | 验证内容 |
|---|---|---|---|
| L0 | `./tools/check.sh` | ~30s | bash 语法、pytest、探测链 mock、80×25 UI 冒烟 |
| L1 | `python3 tools/chroot_tty.py`（构建主机） | ~15s | chroot + 真实 pty + `TERM=linux` + 16 色；加 `--with-menu --keys '\r\x1bq4\nexit\n4\nexit\n\x04'` 可验证 shell 退出后菜单重现 |
| L2 | `./build.sh --fast` | ~2m | zstd + 1s 倒计时的测试 ISO（`-test` 后缀） |
| L3 | `./build.sh` | ~7m | 发布 ISO（xz） |
| L4 | `python3 tools/qemu_drive.py --iso <iso> --settings-ip 10.255.255.1 --erase-rounds 2` | ~6m | 真启动、设置页保存不可达 IP（探测失败但不冻结）、驱动两轮擦除、确认报告累计两轮、退出菜单、VM 关机 |

每次构建都会把 `app/**/*.py` 的哈希写成 `BUILD_ID` 塞进镜像；`verify_freshness()`
用 `unsquashfs -cat` 从产物 squashfs 里读回比对，不一致直接失败——防止"改了代码，
镜像里还是旧版"这类被 mtime 掩盖的问题（`--fast` 与发布版共用 SOURCE_DATE_EPOCH）。

### 远端调试辅助

`tools/rssh.py` 是用密码 SSH 跑构建主机的调试脚本（本机没有 `sshpass` 时用），
凭据放 `tools/.remote.env`（已 gitignore）：

```bash
python tools/rssh.py exec "lb --version"
python tools/rssh.py put  app/requirements.txt /root/x/requirements.txt
python tools/rssh.py push app /root/raidforge/app --exclude "__pycache__"
```

Windows 的 Git Bash 会把 `/root/...` 改写成盘符路径，调用前加 `MSYS_NO_PATHCONV=1`。

---

## 9. 已知限制

* **不等待擦除完成**，见 6.3。
* 一次只处理「探测到的第一个厂商」的控制器；混合品牌机器请用
  `tools.forced_tool` 分次处理。
* RAID 管理仅支持硬件阵列卡，**不支持** mdadm 软件 RAID、NVMe VROC、
  MegaCLI/sas2ircu 旧工具；不支持扩容/迁移/修复/热备盘管理。
* 擦除流程**不阻止**选择 RAID 成员盘（保持原有擦除语义）；成员盘状态已在
  DiskList 的 FW/RAID 列展示。请先在该控制器的 `D` 删除 VD，再擦除成员盘。
* 新建 RAID 后控制器可能仍在后台初始化；重扫显示 `not_visible` 时按 `R` 复查，
  不要把“已接受”当成“已可用”。
* 清除控制器配置只删除 VD + foreign/残余元数据，**不会**重置缓存/BBU/控制器设置。
* RAID 操作记录写入 `events.jsonl`、`controller_raw/`、`raid_actions.json` 和
  `manifest.json`；现有 DOCX 报告仍只记录擦除轮次，未包含 RAID 章节。
* `ssacli` / `arcconf` 的部分高级擦除模式和全部 RAID 命令依赖固件支持，见 §7 的
  VERIFY 提示。
* `webplane/` 控制面 UI 与 `/api/cp` 无鉴权，仅供内网/VPN 使用；agent 面使用
  `WEBPLANE_AGENT_TOKEN` 做共享令牌校验，默认值与 Agent 一致（`raidforge`）。
  Agent 默认允许 HTTP/HTTPS，HTTP 为明文传输；生产必须更换默认 token，并优先
  使用 HTTPS 或受控内网。
* 证据上传私钥随 ISO 分发且无口令；持有 ISO 等同于持有该接收账号的凭证。接收端应
  用 `authorized_keys` 的 `from=`/`restrict`/forced command 把权限限制到
  `/srv/evidence/incoming`。
* 所有厂商 CLI 都没放进镜像时，只能擦到 OS 可见的直连盘（generic 模式），
  且 RAID 管理全部禁用。
* TUI 设置页保存的传输方式、SSH/HTTP 证据接收端、webplane 地址/端口写在 Live 的
  tmpfs（`90-site.yaml` 与 `/run/raidforge/agent.env`）里，**重启即失效**；永久值要改
  `010-install-app.hook.chroot` 的工厂模板后重建镜像。
* 设置页的 SSH 探测只验证「密钥能登录 + 远端根目录可写」，**不验证接收端是否装了
  `rsync`**，也不做 rsync dry-run（那会在每次保存时传数据）；远端缺 rsync 时探测仍
  会显示 reachable，但正式上传会失败并进入待传队列。HTTP 探测只发 `GET /healthz`；
  webplane 的 `POST /sessions` 负责真正接收 HTTP 证据，端点无认证，必须限制在内网/VPN。
* 设置页保存时会清空旧的自动推导 `http_base_url`；HTTP 证据优先使用 `http_host`，
  留空且 `transport=http` 时使用 webplane 地址。显式手工写入的不同 `http_base_url`
  仍保留最高优先级。
* `10-raidforge-dhcp.network` 会匹配所有 ether 类型网卡（含残留网桥/虚拟网卡）；
  若现场存在无法路由的桥接口，可把 `Name=!lo` 收紧为 `Name=en* eth*`。
