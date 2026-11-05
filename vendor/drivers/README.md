# 驱动与固件目录

本目录是 ISO 的驱动扩展点。`catalog.json` / `firmware-map.json` 是**提交到仓库**的清单；
二进制载荷目录被 `.gitignore` 排除，由现场按需投放。

```
vendor/drivers/
  catalog.json                设备/PCI → 内核模块、固件包、厂商 CLI
  firmware-map.json           固件文件 → Debian 固件包（离线解析）
  probes/*.json               现场 raidforge-probe 报告（自动合并，按 arch 过滤）
  amd64/
    debs/                     现场投放：树外驱动 .deb（KMP/DKMS 构建产物）
    ko/                       现场投放：裸 .ko（目录层级不限）
    dkms/                     现场投放：DKMS 源码（含 dkms.conf 的目录）
    firmware/                 现场投放：额外固件，按 /lib/firmware 相对路径放置
  arm64/
    ...                       同上
```

把 `raidforge-probe` 生成的报告直接放进 `vendor/drivers/probes/`，构建时会自动
合并（同 `slot+vendor:device` 去重，按报告里的 arch 过滤），不需要再传 `--probe`；
`--probe` 仍然可用于一次性报告。

## 新增一块阵列卡 / 网卡

1. 在目标服务器（或启动本 ISO）上运行 `raidforge-probe > hw.json`；
2. 构建前执行：
   ```bash
   python3 tools/drivers.py resolve --arch amd64 --probe hw.json
   ```
   工具会匹配 `catalog.json` / 镜像内核的 `modules.alias`，自动生成固件包清单；
   无法识别的设备会列出 `vendor:device` 并中止（可用 `--allow-unknown` 降级）；
3. 如果 Debian 12 内核没有该驱动：
   - 优先把厂商提供的 `.deb` 放进 `vendor/drivers/<arch>/debs/`，或
   - 把裸 `.ko` 放进 `vendor/drivers/<arch>/ko/`，或
   - 把含 `dkms.conf` 的源码包放进 `vendor/drivers/<arch>/dkms/`（构建时临时编译）；
   - 然后重新 `resolve` 并构建。以上三种载荷都会进入驱动清单，构建后自动校验。

`catalog.json` 里没有的新型号，可以补一条 `devices` 记录（vendor/class 或精确
`vendor:device`），并把对应模块写进 `modules`。

**华为 HiRAID**：卡使用非主线 `hiraid` 驱动（aarch64-only），catalog 已登记
`19e5:0104 → hiraid`。把华为的 DKMS 源码包放进 `arm64/dkms/<name>/`（含 `dkms.conf`）
或预编译 `.deb` 放进 `arm64/debs/`；`070-drivers.hook.chroot` 安装后会自动写
`/etc/modules-load.d/raidforge-drivers.conf` 保证开机加载。缺少驱动时构建会告警，
`hiraidadm` 将看不到卡。

## 架构

`amd64` 与 `arm64` 的载荷严格分开。构建时只安装当前架构目录下的内容，并通过
`dpkg-deb -f Architecture` 校验 `.deb` 架构，架构不符会直接中止构建。

## 校验

```bash
python3 tools/drivers.py verify --arch amd64 --chroot chroot
```

检查镜像里的模块文件、固件文件、`/etc/raidforge/drivers.installed.json`
以及 `modprobe --dry-run` 结果。构建流程在 `lb build` 之后自动执行一次。
