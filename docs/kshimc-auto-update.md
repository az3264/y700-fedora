# kShimC 设备的 rpm-ostree 部署与 boot_a 自动重建

本文记录 TB322FC（Legion Y700 4 代，SM8750，`lenovo,elden`）上
"`rpm-ostree install` 成功但重启后新部署不生效"的根因、boot_a 的真实结构、
AVB 签名约束，以及仓库里为此新增的自动 repack / 签名 / 状态机（provision、
adopt、sync、record-boot）。

## 1. 现象与根因

现象：`rpm-ostree install usbutils` 返回成功（`Staging deployment... done`），
`ostree admin status` 能看到新部署，但重启后仍在旧部署里，`usbutils` 消失；
`bootc usroverlay` 本身是正常的（`Development mode enabled.`）。

根因有两层：

1. **启动指针被烧死在 boot_a 里。** EDL 打包时
   `build_files/make-edl-raw-package.py:1079` 把
   `armada.deployment=default/<checksum>.<serial>` 和
   `ostree=/ostree/armada/<checksum>.<serial>` 写进 boot_a 的 cmdline。
   设备上的 `/usr/lib/dracut/modules.d/91armada-ostree-fallback` 只有在
   "绑定部署已消失" 时才重映射，绑定仍在时直接 `exit 0`，于是永远进入同
   一个部署。设备实测：`/proc/cmdline` 里固定为
   `6890b27f….0`，而 `rpm-ostree` 生成的是 `36a7f3a4….0`。

2. **boot_a 里没有内核。** boot_a 只装 kShimC 运行时 + 原厂 Android 镜像；
   Linux 的 kernel/initramfs/DTB 位于 `ARMADA_BOOT`（`sda17`）的
   `armada/seed/`，并由 `manifest.sig`（P-256，公钥在运行时里编译进去）
   认证。设备自己没有签名私钥，所以本机 `rpm-ostree` 产生的新部署不可能
   直接变成可启动的签名 generation。

修复方向就是：**由设备自己在每次部署 finalize 时重建 boot_a（内联
kernel/initrd），并用 AOSP test key 重新做 AVB 签名**——这条路径不需要
release 私钥，因为 boot_a 的校验只依赖公开的 AOSP 测试私钥。

## 2. boot_a 结构与 AVB 事实（设备实测，2026-11-25）

- 分区：`boot_a` = `/dev/sde12`，96 MiB（100663296 字节）；`boot_b` =
  `/dev/sde45`；`vbmeta_a` = `/dev/sde15`；另有 `init_boot_a/b`、
  `vendor_boot_a/b`、`vbmeta_system_a/b`。
- boot_a = 4096 字节 `ANDROID!` 头 + gzip 压缩的 kShimC 运行时 + AVB
  （`AVB0`/`AVBf`）。压缩流用 `zlib.decompressobj(31)` 解，`gzip` 模块会因
  AVB trailer 报 `Not a gzipped file (b'AV')`。
- 运行时里的 packer manifest（`KPMF`，version 1.0，`entryCount=4`，
  `defaultEntry=1`，`timeoutMs=8000`）四个条目：
  | # | 名称 | type | 说明 |
  |---|------|------|------|
  | 0 | `原厂 Android` | 1 | 内嵌 36.7 MiB 原厂 Android boot image |
  | 1 | `机身 Linux` | 7 | 64 字节 `ARMDSK1` 描述符（boot/root UUID） |
  | 2 | `Linux 诊断模式` | 7 | 同样的描述符 |
  | 3 | `Attached Shim` | 3 | 4 MiB kShimC 运行时 |
  条目为 56 字节头 + 4 字节对齐的名字。
- AVB：`Algorithm: SHA256_RSA4096`，`Public key (sha1):
  2597c218aae470a130f61162feaae70afd97f011`，`Rollback Index: 1738713600`，
  `Release String: 'freedom-edl-direct'`。该公钥 sha1 等于
  `vendor/aosp/testkey_rsa4096.pem` 的 AVB 公钥 sha1（installer 仓库
  `EdlRawBootBuilder.Envelope.cs` 也是用它签名），所以重签后的 boot_a 在
  **未解锁**的 bootloader 上仍可通过 AVB。
- `vbmeta_a` 对 `boot`、`recovery`、`vbmeta_system` 使用 chain partition
  描述符，key sha1 相同、`boot` 的 rollback index 为 3。写入时必须保持
  key 与 rollback index 不低于现值，否则 anti-rollback 会拒绝启动。
- `ARMADA_BOOT` 内容（`/boot`）：`armada/boot -> seed`，`armada/seed/`
  下 `binding.bin`（`ARMDEP1\0` + bootcsum）、`kernel.img`、
  `initramfs.img`、`TB322FC/{board.dtb,manifest.bin,manifest.sig}`。
  运行时用 `src/core/boot_disk_load.c` 校验 manifest 签名，**没有降级路径**。

## 3. 仓库中的实现

### 3.1 initramfs：跟随 ostree 的默认部署

`system_files/usr/lib/dracut/modules.d/91armada-ostree-fallback/armada-ostree-fallback.sh`
在绑定部署仍存在时，额外读取 `boot=UUID=…` 挂载点里的
`loader/entries/ostree-N.conf`（N 最大者为 ostree 的 rank 0），当它指向
同一 bootcsum、但比绑定部署更新时，改写 `ostree=` 与
`armada.deployment=` 两个参数（保持原有指针形态），并创建
`/ostree/armada/<csum>.<serial>` 符号链接；标记文件为
`/run/armada/ostree-default-remapped`，修正后的 cmdline 保留给用户空间
（`boot_context` 依赖它做自洽校验）。bootcsum 不同（内核已变）时不做重映射，
只记录需要重建启动镜像。

### 3.2 AVB 签名器

`system_files/usr/libexec/armada/kshim-sign-boot.py`：对
`kshim-build-boot.py` 产出的 unsigned boot.img（`Algorithm: NONE`）就地进行
`avbtool add_hash_footer --algorithm SHA256_RSA4096` 签名、随后
`verify_image` 校验，并刷新 offline report（`outputs.final_boot` 长度/摘要、
`inputs.signer`、`avb_signing`）。默认 key
`/usr/lib/armada/kshimc/testkey_rsa4096.pem`，默认拒绝其他 key
（`--expect-key-sha1`）。

### 3.3 状态机

`system_files/usr/libexec/armada/armada-kshim-state`：

- `provision`：安装器（EDL 流程）用 `--stock-boot --runtime --current-image
  --current-report --partuuid --sign-key` 建立 `/var/lib/armada/kshimc`
  状态目录（stock boot、runtime、签名 key 的哈希、签名策略与 rollback
  index）。
- `adopt`：**已装机设备**在没有 installer report 时接管：从 boot_a 读出当前
  镜像（校验 `ANDROID!`、解析 AVB），用当前 booted 部署建立基线，写入同样的
  状态目录，此后所有部署 finalize 都能自动重签。
- `sync`：部署变化时重建 boot_a（候选 + 回退镜像各准备一次，写入前重新校验
  目标哈希，失败退回已准备的 booted 镜像），签名后的镜像要与
  `--sign-rollback-index` 一致。
- `record-boot`：核对当前 booted 部署与状态里的 payload 身份一致。

### 3.4 单元与路由

- `armada-bootimg-update` / `armada-bootimg-finalize`：internal 介质下，
  仅当 `/etc/armada/boot-method=kshimc` **且** `/var/lib/armada/kshimc`
  已 provision 时，才走状态机；否则维持 "installed boot images are
  runtime-owned" 的 no-op 行为。
- installer（`system_files/usr/libexec/armada/armada-installer`）不再屏蔽
  `armada-bootimg-sync.service` 与 `ostree-finalize-staged.service.d/
  10-armada-bootimg.conf`（未 provision 时它们仍是 no-op），只屏蔽
  `armada-esp-rename.service`（internal 路径不改 ESP 卷标）。

## 4. 操作与验证

```bash
# 一次性接管已装机设备（stock boot.img + runtime 来自 EDL 包）
sudo /usr/libexec/armada/armada-kshim-state adopt \
  --stock-boot /root/stock-boot.img \
  --runtime /usr/lib/armada/kshimc/kShimC.bin \
  --sign-key /usr/lib/armada/kshimc/testkey_rsa4096.pem \
  --partuuid "$(blkid -s PARTUUID -o value /dev/disk/by-partlabel/boot_a)"

# 手工验证签名
python3 /usr/libexec/armada/kshim-sign-boot.py --check \
  --image /tmp/boot.img --key /usr/lib/armada/kshimc/testkey_rsa4096.pem

# 重新部署后（自动）核对
sudo /usr/libexec/armada/armada-kshim-state sync
sudo /usr/libexec/armada/armada-kshim-state record-boot
```

安全约束（写入 boot_a 前必须满足）：

1. 绝不在未解锁设备上写 `Algorithm: NONE` 的镜像；
2. rollback index 不低于当前值（实测 1738713600）；
3. `vbmeta_a` 的 chain 描述符（key sha1 + rollback index 3）保持不变；
4. 写前保留 boot_a 原始备份（实测 sha256
   `5cb3ebdfb6a06432fef4cef7bdb111e77f68fb2cf70bbdb134db0a093fd4280c`）；
5. 任何异常的最后手段是 EDL 9008 恢复。

## 5. 待办

- 用原厂 EDL 备份里的 `stock-boot.img` 在真机跑一次 `adopt` + 重启验证。
- installer 首次安装时调用 `provision`（需要把 stock boot 与 runtime 一起
  带进镜像/安装介质）。
- 若以后要让 boot_a 保持不动，则需要运行时支持"本地未签名 generation"
  （重编 runtime + 一次性重刷），或在 `ARMADA_BOOT` 之外增加可写 generation。
- TB323FU / TB710FU 不在本次范围（仅 TB322FC）。
