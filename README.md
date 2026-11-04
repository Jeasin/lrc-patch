# lrcpatch

**给本地音乐库批量补歌词：匹配 → 计划 → 写入 → 校验 → 回滚。**

[![CI](https://github.com/Jeasin/lrc-patch/actions/workflows/ci.yml/badge.svg)](https://github.com/Jeasin/lrc-patch/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)
[![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-orange)](LICENSE.md)

从网易云音乐与 QQ 音乐取歌词，写进 `m4a` / `mp3` / `dsf` 的标签里。先出计划再动手，写入前逐首打分，写入后能校验、能回滚。

> 本项目与网易云音乐、QQ 音乐、Apple 均无关联，只是调用它们的公开取词接口。歌词版权归各自权利人，本工具不存储、不分发歌词内容。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [主要特性](#主要特性)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令一览](#命令一览)
- [状态符号](#状态符号)
- [匹配与打分](#匹配与打分)
- [歌词格式](#歌词格式)
- [写入安全](#写入安全)
- [常见用法](#常见用法)
- [配置文件](#配置文件)
- [数据、网络与缓存](#数据网络与缓存)
- [已知限制](#已知限制)
- [开发](#开发)
- [许可](#许可)

## 它解决什么问题

音乐库里几百首日文歌，标签整齐，但歌词栏是空的。手动一首首找、复制、粘贴，还得担心贴错版本、贴成 TV size、把原来的封面弄丢。

`lrcpatch` 把这件事拆成可以分别检查的几步：

1. **匹配**：按标题 / 歌手 / 专辑 / 时长打分，同一个录音的现场版、翻唱版、Instrumental 会被区分开；
2. **出计划**：哪些有把握、哪些要人确认、哪些根本没找到，一次列清楚，**不动任何音频文件**；
3. **写入**：只写已经确认的那部分，写完立刻回读校验；
4. **可回滚**：每次写入前的状态记在 journal 里，`rollback` 可以还原（连"原本就有歌词、被我覆盖了"也能还原）。

## 主要特性

- **先计划后写入**：`plan` 是只读的，默认命令就是它，拖一堆文件夹进去也不会误写。
- **双源 + 可换源**：默认先问网易云（`ne`），缺的再问 QQ（`qq`）；`lrclib` 需要显式 `--src lrclib`。
- **打分而不是模糊搜索**：满分 100，≥85 且时长在容差内才自动写入，其余一律 `!` 等你确认。
- **原生格式流水线**：取词时把源的原生歌词（YRC / QRC）一起存下，`apply --a2` / `--eslrc` / `--ttml` / `--qrc` 只是本地转换，**换格式不用重新联网**。
- **原子写入**：写临时副本 → 回读校验（歌词 / 其他标签 / 封面 / 音频数据逐项比对）→ `os.replace` 替换，失败则原文件一个字节都不变。
- **断点续跑**：结论存在 SQLite 状态库里，1000 首中断后重跑只查没结论的那些。
- **离线可重复**：HTTP 响应按稳定键缓存 30 天，同一首歌不会每跑一次就骚扰一次接口。
- **一条命令装好**：纯命令行，无 GUI、无守护进程、无云同步。

## 安装

需要 Python 3.9+，以及 `mutagen`（写标签）与 `curl_cffi`（浏览器 TLS 指纹，部分接口会拦普通请求）。

用 [uv](https://docs.astral.sh/uv/) 安装（推荐，会自动建独立环境）：

```bash
uv tool install git+https://github.com/Jeasin/lrc-patch
lrcpatch -h
```

用 pipx：

```bash
pipx install git+https://github.com/Jeasin/lrc-patch
```

或者不安装，直接跑仓库里那个自带 PEP 723 依赖声明的脚本（首次运行 uv 会把依赖装进缓存）：

```bash
git clone https://github.com/Jeasin/lrc-patch
cd lrc-patch
./lrcpatch scan --library ~/Music/JMusic
```

`python3 lrcpatch` 也能用：脚本发现当前解释器没有 `mutagen` 时会自己 re-exec 到 `uv tool` 装好的那个解释器（可用 `LYRICS_MUTAGEN_PYTHON` 指定）。

## 快速开始

```bash
# 1. 先看看库里是什么情况（不联网）
lrcpatch scan

# 2. 出计划（只读，不写音频）
lrcpatch plan

# 3. 确认没问题后写入
lrcpatch apply
```

`plan` 的真实输出（`--no-color`，4 首演示曲目）：

```
  plan  整个音乐库  ·  4 首
  ────────────────────────────────────────────────────────────────────────────
  源  ne,qq   复用 0   查询 4   并发 4
  查询 4 首…（Ctrl+C 可中断）
  查询 4/4 · 0.6 首/秒

  ▸ 米津玄師/STRAY SHEEP   2 首  ✓2
    ├─ ✓ 01 Lemon.m4a   ne  100分  Δ0.1s  LRC 45 行  米津玄師；原生 YRC
    └─ ✓ 02 馬と鹿.m4a  ne  100分  Δ0.3s  LRC 51 行  米津玄師

  ▸ YOASOBI/THE BOOK   2 首  ✓2
    ├─ ✓ 01 夜に駆ける.m4a  ne  100分  Δ0.8s  LRC 63 行  YOASOBI
    └─ ✓ 02 群青.m4a        ne   85分  Δ0.4s  LRC 87 行  YOASOBI

  ────────────────────────────────────────────────────────────────────────────
  汇总  ✓ 可写入 4
  报告  ~/.local/state/lrc-patch/reports/plan-20261104-125036.csv
  状态  ~/.local/state/lrc-patch/lyrics.sqlite3
  请求  ne 8 次（搜索 4 · 取词 4）  ·  传输 curl_cffi

  未修改任何音频文件。确认后运行 lrcpatch apply
```

`apply` 与 `verify`：

```
  apply  整个音乐库  ·  4 首
  ────────────────────────────────────────────────────────────────────────────
  方式  临时副本验证后原子替换；失败则原文件不动   并发 4

  ▸ YOASOBI/THE BOOK   2 首  ✓2
    ├─ ✓ 01 夜に駆ける.m4a  ne · 写入并校验成功
    └─ ✓ 02 群青.m4a        ne · 写入并校验成功

  汇总  ✓ 已写入 4
  网络  未发起请求，直接使用 plan 结果
```

```
  verify  整个音乐库  ·  4 首
  内容  歌词、其他标签、封面、音频数据与写入时逐项比对

  汇总  ✓ 通过 4
```

逐行里的信息依次是：状态 / 文件 / 源 / 分数 / 时长差 / 歌词格式与行数 / 歌手或说明。专辑列对齐，跨格式也不会串位。

## 命令一览

| 命令 | 作用 | 会写音频吗 |
|---|---|---|
| `scan` | 只读扫描：格式汇总 + 逐文件明细 | 否 |
| `plan` | 查询候选、打分、生成计划（默认命令） | 否 |
| `candidates` | 列出候选 `SOURCE:ID` 与分数，供 `--pick` 用 | 否 |
| `apply` | 写入 ✓ 可写入的曲目，逐文件校验后原子替换 | 是 |
| `verify` | 复核已写入的歌词、标签、封面与音频数据 | 否 |
| `rollback` | 还原本工具写入的歌词（原歌词还在就还原，不在就移除） | 是 |
| `status` | 预览状态库：计划分布、缓存、回滚记录与报告 | 否 |
| `clean` | 清理过期缓存、旧报告、旧导出 | 否 |
| `config` | 查看生效的默认值；`--init` 生成配置模板 | 否 |

省略命令等于 `plan`：`lrcpatch "YOASOBI"` 只是出计划，不会写。

常用参数：

```
--library DIR       音乐库根目录（默认 ~/Music/JMusic）
--src ne,qq         歌词源与顺序
--workers N         并发 1..10
--limit N           最多处理 N 首
--refresh           忽略 HTTP 缓存重新请求（只管网络）
--force             忽略"已有歌词/已有计划"强制重排（只管现状）
--pick SOURCE:ID    人工指定候选，可连写多个
--lrc / --lrc-dir   同时导出 .lrc 文件到目录
```

每个命令的完整说明：`lrcpatch plan -h`。

## 状态符号

| 符号 | 状态 | 含义 | `apply` 会写吗 |
|---|---|---|---|
| `✓` | ready / applied / ok | 可写入 / 已写入 / 校验通过 | 会 |
| `!` | ambiguous | 分数不足、或时长超出容差，需要人确认 | 不会 |
| `!` | suspect | 时间轴可疑：末行时间戳超出音频时长、覆盖不足 55%、或只有纯文本 | 不会 |
| `!` | changed / conflict | 文件在计划之后变过 / 歌词被改过，拒绝回滚 | 不会 |
| `✗` | no_match | 所有源都没找到像样的候选 | 不会 |
| `✗` | error | 网络失败、文件损坏等 | 不会 |
| `·` | skip_existing | 文件里已有歌词（`--force` 可重排） | 不会 |
| `♪` | instrumental | 源标注为纯音乐 | 不会 |
| `↩` | rolled_back / pending | 已回滚 / 待移除 | — |

## 匹配与打分

标题 55 分、歌手 30 分、专辑 15 分，满分 100。**时长不是加分项，是闸门**：超出容差（默认 2 秒）直接淘汰，容差内不加减分，只用来排序。

| 项 | 加分 | 说明 |
|---|---|---|
| 标题完全相同 | +55 | |
| 标题里的 feat. / with 名字已在歌手里 | +55 | 同一个录音，不是两个版本 |
| 标题附译名或假名读音 | +50 | `Hz(ヘルツ)`、`歩く (漫步)`、`标题 - English Title` |
| 仅版本后缀不同 | +42 | Live / Remaster / Da Capo Version / TV Size… |
| 其它附加后缀 | +35 | |
| 合作者不同 | +30 | feat. 版本 |
| 标题主体不同 | +0 | 歌手与专辑仍会累加，但封顶 70，到不了 85 |
| 歌手全部对上 | +30 | 少一部分合作者 +26，只是片段相同 +18 |
| 专辑完全相同 | +15 | Deluxe / Tour / Single / EP 等版本 +12，包含关系 +8 |

两类特殊情况直接淘汰（0 分）：纯音乐与有词版相反、语言版本相反。

常见组合：

| 分数 | 情形 |
|---|---|
| 100 | 标题 + 歌手 + 专辑全中 |
| 97 | 专辑只是 Deluxe / Tour 等版本 |
| 93 | 标题附译名或读音 |
| 87 | 标题是版本变体，专辑对得上 |
| 85 | 标题 + 歌手全中，专辑不符 |
| 75 | 合作者不同（feat. 版本）——一律 `!` |

排序规则：**先看是否在时长容差内**（容差外的一律排在后面），再按不含时长的元数据分，再按含时长的分，再按时长差，最后偏好带 LRC 的。

这条规则解决一个很常见的坑：现场版比正片长 13 秒时，即使标题一字不差，也不会排到专辑对得上的那条前面。

`!` 行会把每个源的分数和最佳候选直接写出来，可以复制去 `--pick`：

```
! 08 印地安老斑鸠.m4a  ne 72 · qq 72；最佳 qq:244115386 印地安老斑鸠 (Live) / 周杰伦 / 巡演 / 208s / Δ97.0s
```

## 歌词格式

| 缩写 | 全称 | 样子 |
|---|---|---|
| TXT | 纯文本 | 没有时间轴 |
| LRC | 逐行时间轴 | `[00:12.34]歌词` |
| A2 | LRC A2 逐字 | `[00:12.34]<00:12.34>逐<00:12.50>字` |
| ESLRC | ESLyric / SPL 逐字 | `[00:12.34][00:12.34]逐[00:12.50]字` |
| YRC | 网易云逐字 | `[1200,5000](1200,300,0)逐(1500,300,0)字` |
| QRC | QQ 音乐逐字 | `[1200,5000]逐(1200,300)字(1500,300)` |
| TTML | Apple Music | XML |

`plan` 取词时会把源的原生歌词一起存进状态库，`plan` 与 `candidates` 的"原生"列标明源给的是什么：

- 换成 `--a2` / `--eslrc` / `--ttml` / `--qrc` 是**本地转换**，不联网；
- 换成原生格式本身则原样输出；
- 原生是逐行 LRC 时无法凭空生成逐字，会明说而不是硬写；
- 导出后缀跟着格式走：`.alrc` / `.eslrc` / `.yrc` / `.qrc` / `.ttml`，逐行用 `.lrc`，纯文本用 `.txt`。

私有格式（`--yrc` / `--qrc`）只在原生就是它时才可能：YRC 只来自网易云，QRC 只来自 QQ。

## 写入安全

写一个音频文件最怕两件事：写坏了，和被覆盖了没处找。

`apply` 的顺序是：

1. 在歌曲同目录建临时副本，只改副本的歌词标签；
2. 回读副本，逐项比对：歌词内容、其它标签的 sha、封面 sha、音频数据（mdat）sha；
3. 全部一致才 `os.replace` 原子替换；任何一项不符就丢掉副本，原文件不动；
4. 写入前把「原本有没有歌词、原来的歌词是什么」记进 journal。

所以 `rollback` 不是"删掉歌词"，而是**还原**：原本有歌词的还原成原文，原本没有的才移除，两种都在预览里分别写明。歌词在你写入之后被改过的会判 `!` 拒绝回滚，不覆盖你的修改。

默认**不覆盖已有歌词**（`·` 已有歌词会跳过）。`apply --force` 才允许覆盖，并会额外提示覆盖了几首、原歌词已进回滚记录。

## 常见用法

**只处理一张专辑**

```bash
lrcpatch plan "YOASOBI/THE BOOK"
lrcpatch apply "YOASOBI/THE BOOK"
```

**某些曲目匹配不准，人工指定**

```bash
# candidates 默认只列上一次 plan 留下的 ! 需确认，以及来自别的源的 ✓
lrcpatch candidates "Aimer"

# 指定后直接写
lrcpatch plan --pick ne:123 --apply "Aimer/某首.m4a"
```

`candidates` 的默认范围是**上一次 plan 没能定下来的那几首**（`!` 需确认 / `!` 时间轴可疑），不是整个库：库里有 2000 首没 plan 过的曲子时，整个库会把真正要你决定的那三首淹掉。想扩大范围：

| 用法 | 列出的曲目 |
|---|---|
| `lrcpatch candidates` | 只有 `!` 需确认的（所在范围从没 plan 过时，退回列出这些没结论的） |
| `lrcpatch candidates --all` | 该范围内**所有**缺歌词的曲目，含已经 ✓ 的与已有歌词的 |
| `lrcpatch candidates --src qq` | 加上「计划来自 ne」的那些：换源就还没定论 |
| `lrcpatch candidates --refresh` | 同样范围，但忽略 HTTP 缓存重新请求（不改范围） |

`--all` 不带路径 = 整个音乐库里所有曲目（含已有歌词的），不是"上次 plan 那批"。

一次 `--pick` 可连写多个 ID，程序先查每个 ID 的标题 / 歌手 / 时长，再按分数配对到范围内缺歌词的曲目，并把配对结果打印出来。整张专辑要批量指定时，用模板：

```bash
lrcpatch candidates --template "Aimer"    # 生成 picks.tsv 模板
lrcpatch plan --pick-file picks.tsv       # 按模板指定
```

挑中的 ID 在源上**没有歌词**时（QQ 上很常见：翻唱、Remix、纯音乐版），会直接说明是"这个 ID 没有歌词"，而不是含糊的失败；同一次搜索里还有别的 ID 时会把它们列出来供你换一个：

```
✗ 06 Takeaway (feat. Lennon Stella).m4a   人工指定 qq:466588397（时长已核验 Δ0.9s）；qq 上这个 ID 没有歌词。同一次搜索里还有：qq:234939651 Takeaway (209s)  换一个用 --pick
```

**原来的歌词是错的（TXT、错版本），强制重写**

```bash
lrcpatch plan --force --apply "某人/(2018)老专辑"
```

`--refresh` 与 `--force` 分工明确，互不重叠，可以叠加：

| 参数 | 作用于 | 效果 |
|---|---|---|
| `--refresh` | 网络 | 忽略 HTTP 缓存重新请求；计划里已有的结论照样复用 |
| `--force`（plan） | 现状 | 忽略"文件已有歌词"和"状态库已有计划"，强制重查重排 |
| `--force`（apply） | 现状 | 连 `!` 时间轴可疑一起写，并允许覆盖已有歌词 |

**换源会覆盖旧结论**：状态库里一个文件只有一条计划。先跑 `plan --force`（源 ne,qq）拿到 ne 的结果，再跑 `plan --src qq --force`，第二次的结果会替换第一次的，不会两份并存；plan 会打印两个计数：

```
换源  12 首改由 qq 定论（一个文件只留一条计划，旧结论已被覆盖；切回原来的源会重查，取词已缓存）
降级  其中 3 首原本 ✓ 可写入，新源只给到 ! / ✗
```

想并存比较两边，用 `candidates --src qq --all`：它只读、不动计划，一次列出两个源各自的候选与分数；想切回原来的源直接 `plan --src ne`，取词结果还在缓存里，很快。

**想要逐字歌词**

```bash
lrcpatch plan --a2 "Ado"        # 计划里就按 A2 生成
lrcpatch apply --eslrc          # 或者计划之后再换格式，不重新联网
```

**只想导出 .lrc，不动音频**

```bash
lrcpatch plan --lrc --lrc-dir ~/Desktop/lyrics "米津玄師"
```

**纯音乐被 `!` 挡住，但我就是想写**

```bash
lrcpatch apply --force          # 连 suspect 一起写；空歌词也不会写进去
```

## 配置文件

`~/.config/lrc-patch/config.toml`（遵循 `$XDG_CONFIG_HOME`，可用 `LRCPATCH_CONFIG` 指向别处）。只提供默认值，命令行参数永远优先。

```bash
lrcpatch config --init     # 生成带注释的模板，已存在则不覆盖
lrcpatch config            # 看当前生效值与来源
```

```toml
library = "~/Music/JMusic"
src = ["ne", "qq"]
workers = 4
```

## 数据、网络与缓存

| 内容 | 位置 |
|---|---|
| 状态库（计划 / journal / HTTP 缓存） | `~/.local/state/lrc-patch/lyrics.sqlite3` |
| CSV 报告与 HTML 复核页 | 同上目录下的 `reports/` |
| `--lrc` 导出镜像 | `~/.cache/lrc-patch/lrc/` |

`clean` 像 `brew cleanup`：默认只清过期缓存与 30 天前的报告，逐类说明删了几项、释放多少；`clean --all` 才不限天数。计划状态与回滚记录只在明确指定 `--plans` / `--journal` / `--all` 时删除——删了就要重查，且无法回滚。

想看状态库里到底存了什么，用只读的 `status`：

```
  status  ~/.local/state/lrc-patch
  ────────────────────────────────────────────────────────────────────────────
  状态库  30.0 MB   WAL 0 B
  配置  ~/.config/lrc-patch/config.toml   不存在，使用默认值
  音乐库  ~/Music/JMusic

  ▸ 计划   1337 条   最近 2026-11-04 20:15
    ✓ 已写入 326   ✓ 可写入 173   ! 需确认 8   ✗ 无匹配 3   ✗ 失败 49   ♪ 纯音乐 26   · 已有歌词 752
    来源  ne 377   qq 176   lrclib 3

  ▸ 缓存   609 条   24.9 MB   无过期   最旧 14 小时前
  ▸ 记录   380 条可回滚   最近写入 2026-11-04 20:16
  ▸ 报告   36 个   120.8 KB
```

它不扫描音乐库、不联网、也不抢锁，规划跑一半也能看；`status --json` 给脚本用。库里的曲目与歌词格式汇总看 `scan`。

网络只发生在 `plan` / `candidates`：只请求公开的搜索与取词接口，只带一个普通浏览器 User-Agent，不发任何账号信息，无遥测。写入、校验、回滚完全离线。

## 已知限制

- **时长依赖文件标签**：容器里没写时长或写错时会判不出匹配，`scan` 会把可疑项列出来。
- **只处理 m4a / mp3 / dsf**：FLAC、OGG 等暂不支持。
- **私有格式有来源限制**：YRC 只能来自网易云，QRC 只能来自 QQ；别的源无法凭空生成逐字。
- **接口随时会变**：网易云与 QQ 的取词接口都不是公开 API，行为变化会导致失败（会明确报错，不会静默写坏文件）。
- **平台**：在 macOS 上开发与验证（Python 3.9 / 3.13）。Linux 理论可用但未验证；Windows 未测试。
- **不做去重与自动改名**：它只补歌词，不动你的标签与文件名。

## 开发

```bash
git clone git@github.com:Jeasin/lrc-patch.git
cd lrc-patch

# 单独建环境（uv）
uv venv && uv pip install -e . mutagen curl_cffi

# 跑测试（只用标准库 unittest，无需 pytest）
./.venv/bin/python -m unittest discover -s tests -q
```

代码结构：

```
lrcpatch            # 入口脚本（PEP 723），可单独拷走使用
lrc_patch/
  cli.py            # 参数解析与命令分发
  cli_args.py       # 帮助文本（中文，这里是"文档"的真正来源）
  commands.py       # scan / plan / candidates / apply / verify / rollback
  sources.py        # 网易云 / QQ / LRCLIB，重试、限速与缓存
  matching.py       # 打分、候选配对、状态机
  audio.py          # 读写标签、原子替换、校验
  lyric_fmt.py      # 各格式解析与互相转换
  qrc.py            # QQ QRC 解密（3DES + zlib）
  state.py          # SQLite 状态库
  render.py         # 终端表格与对齐
tests/test_core.py  # 153 个用例，覆盖解析、打分、写入、回滚与 CLI
```

CI 在 Python 3.9 / 3.11 / 3.13 上跑同一套测试。

## 许可

[PolyForm Noncommercial License 1.0.0](LICENSE.md)：可自由用于个人学习、研究、爱好项目与非营利组织，**不得用于商业用途**。商业授权请开 issue 联系。

```
Required Notice: Copyright 2026 Jeasin (https://github.com/Jeasin)
```
