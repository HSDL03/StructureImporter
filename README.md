<p align="center">
	<img
		src="https://zh.minecraft.wiki/images/Structure_Block_JE2_BE1.png"
		width="120px"
		align="center" alt="结构方块"
	/>
	<h1 align="center">结构导入器 Structure Importer</h1>
	<p align="center">
		网易《我的世界》中国版 MOD：本地 .mcstructure 结构一键导入世界，<br/>
		超大结构分区导入 + 自动地图画生成。
	</p>
</p>

<p align="center">
	<a href="https://github.com/HSDL03/StructureImporter/releases">
		<img
			src="https://img.shields.io/badge/version-0.0.3-brightgreen.svg"
			alt="Version: 0.0.3"
		/>
	</a>
	<a href="https://opensource.org/license/mit">
		<img
			src="https://img.shields.io/badge/License-MIT-blue.svg"
			alt="License: MIT"
		/>
	</a>
	<a href="https://mc.163.com">
		<img
			src="https://img.shields.io/badge/Minecraft-Netease_Edition-green"
			alt="Minecraft Netease Edition"
		/>
	</a>
	<a href="https://github.com/HSDL03/StructureImporter">
		<img
			src="https://img.shields.io/badge/%E7%94%B1_AI_%E7%94%9F%E6%88%90-WorkBuddy_Agent-8a63d2"
			alt="AI Generated"
		/>
	</a>
</p>

---

> [!IMPORTANT]
> **本项目由 AI 生成**：代码、UI、文档均由 AI（WorkBuddy 智能体）在人类指导下编写与调试，经真机多轮实测迭代而成。仅供学习交流使用，请自行评估后再使用。

---

## 目录

- [项目简介](#项目简介)
- [主要功能](#主要功能)
- [快速开始](#快速开始)
  - [安装要求](#安装要求)
  - [安装步骤](#安装步骤)
- [命令一览](#命令一览)
- [地图画是怎么画出来的](#地图画是怎么画出来的)
- [目录结构](#目录结构)
- [平台上架须知](#平台上架须知)
- [常见问题](#常见问题)
- [许可证](#许可证)

---

## 项目简介

这是 Minecraft（我的世界）中国版 MOD **结构导入器** 的开源仓库。

你在电脑上辛苦搭好的城堡、朋友分享的地图组件、网上下载的 `.mcstructure` 结构文件，只需放进一个文件夹、在游戏里输一条命令，就能原样落地到你的世界——方块、朝向、细节，一个不差。

本项目全程由 AI 与开发者协作完成：功能设计、真机测试反馈由人主导，代码编写、UI 排版、文档由 AI 生成，历经数十轮真机迭代调试。

## 主要功能

✔️ **一键导入** - 精确保留方块、方块状态、朝向、实体与方块实体（箱子内容/告示牌文字等）
✔️ **超大结构无忧** - 自动 16×16 分区导入，玩家跟随传送保证区块加载，百万方块也能完整落地
✔️ **写失败自动重试** - 区块没跟上也不怕，缺失方块自动补写
✔️ **急速模式** - 跳过区块等待直接写入，速度拉满
✔️ **分块防卡顿** - 巨型结构自动切块依次导入，手机与低配机也流畅
✔️ **自动地图画** - 导完自动搭墙、巡图、挂框，一面地图墙就是一幅完整像素画
✔️ **跨行降速** - 分区换行的大距离传送自动降速等区块，参数可调
✔️ **图文说明书** - 游戏内自定义书本，右键翻开，四大目录覆盖全部功能

## 快速开始

### 安装要求

- Minecraft 中国版最新版本（您可以下载[官服](https://mc.163.com/)）
- Minecraft 中国版开发工作台（您可以在[官网](https://mc.163.com/dev/)下载）

### 安装步骤

**方式一：直接用发布包（推荐）**

1. 到 [Releases](https://github.com/HSDL03/StructureImporter/releases) 下载最新的 `StructureImporter_v0.0.3.zip` 并解压
2. 把解压出的行为包、资源包目录连同 `world_behavior_packs.json`、`world_resource_packs.json` 放进你的开发世界目录（或用 MC Studio 打开）
3. 进入世界，聊天框输入 `/structureim ui` 验证 MOD 已生效

**方式二：从源码**

1. 下载本项目到本地（推荐 `git clone https://github.com/HSDL03/StructureImporter.git`）
2. 用 MC Studio 打开项目，或把行为包与资源包导入你的开发世界（`world_behavior_packs.json` / `world_resource_packs.json` 已配置好挂载关系）
3. 进入世界，聊天框输入 `/structureim ui` 验证 MOD 已生效

> 本 MOD 需要读取本地结构文件夹，请在电脑端使用。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `/structureim dir <路径>` | 设置结构文件夹（如 `D:\我的结构`） |
| `/structureim scan` | 重新扫描 |
| `/structureim list [页]` | 聊天栏列出结构 |
| `/structureim place <序号或名字> [旋转0-3]` | 放置结构（你站的位置 = 结构原点） |
| `/structureim pause` / `stop` / `info` | 暂停继续 / 中止 / 查看进度 |
| `/structureim ui` | 打开图形界面（含设置页） |
| `/structureim znum <1-9>` | 地图画：每幅巡几个分区（默认 3） |
| `/structureim zwait <1-30>` | 地画画：每区停留秒数（默认 3） |
| `/structureim bigwait <1-30>` | 跨行远传后降速秒数（默认 4） |
| `/structureim budget <2-25>` | 每 tick 写方块的时间预算（ms，默认 12；越大越快越卡，写入量本身会自动调速） |
| `/structureim threads <1-8>` | 结构解析线程：≥2 时解析走后台，点放置不再卡主线程 |
| `/structureim parallel <1-4>` | 相邻分区并行：提前加载相邻分区区块，轮到它时不用等 |
| `/structureim nbtlimit <0\|8-1024>` | 物品 NBT 上限（KB）：默认 0 = 不裁剪，盒中盒原样保留 |
| `/structureim split <开\|关>` | 分区导入：超大结构切块写，配合跟随传送 |
| `/structureim splitz <0\|16-128>` | 分区边长：0 = 自动（按实测直方图定），16-128 = 自定义 |
| `/structureim ta <开\|关>` | 纯常加载：整张结构挂 tickingarea，玩家不再跟随传送 |
| `/structureim tac` | 清理本插件残留的常加载区（只删 `si_` 开头，不动别人的指令区） |
| `/structureim verify <开\|关>` | 导完逐块回读，只报差多少块，**不补写**（默认关） |
| `/structureim preset <稳\|均衡\|极速>` | 一键套用整套参数 |
| `/structureim fast` | 一键提速（后台解析 + 邻区并行 + 急速 + 满预算） |
| `/structureim manual` | 聊天框版图文教程 |
| `/structureim selftest` | 环境自检 |

所有设置自动保存进存档，重启世界无需重调。

## 地图画是怎么画出来的

地图由**携带者**持续绘制周围地形，所以流程是全自动的四步：

1. 传送到地图单元的第 1 区就把地图放进你主手
2. 带着地图自动巡完各分区（默认 3 区即画满）
3. 收回地图，换下一幅
4. 全部收完 → 回到墙前逐张挂框，凑近看就是整幅画

巡图时跟着传送走即可，不要手动乱跑。参数不合适可用 `znum` / `zwait` 调整。

## 它基于结构方块

本 MOD 解析的 `.mcstructure` 文件正是游戏内**结构方块（Structure Block）**保存的格式——先用结构方块框选并保存建筑，再用本 MOD 批量、自动地放置它们。

<p align="center">
	<img src="https://zh.minecraft.wiki/images/Structure_Block_JE2_BE1.png" width="180px" alt="结构方块" />
	<img src="https://zh.minecraft.wiki/images/Structure_Block_Corner_JE2_BE1.png" width="180px" alt="结构方块（角落模式）" />
	<img src="https://zh.minecraft.wiki/images/Structure_Block_Bedrock_ClassicUI_Simplified.png" width="240px" alt="存档界面" />
	<img src="https://zh.minecraft.wiki/images/Structure_Block_Bedrock_ClassicUI_Load_Simplified.png" width="240px" alt="加载界面" />
</p>

<p align="center"><sub>图片来自 <a href="https://zh.minecraft.wiki/w/%E7%BB%93%E6%9E%84%E6%96%B9%E5%9D%97">中文 Minecraft Wiki · 结构方块</a>，版权归 Mojang Studios 所有</sub></p>

## 目录结构

```
behavior_pack_.../Script_NeteaseMod_TtCLQKGx/
  modMain.py     入口与系统注册
  si_srv.py      服务端主逻辑（导入状态机 / 地图画状态机）
  si_scan.py     本地结构目录扫描
  si_nbt.py      NBT 解析与引擎写入格式转换
  si_cfg.py      全部可调参数
  si_ui.py       图形界面（主界面 + 设置页）
  si_cli.py      客户端系统
behavior_pack_.../customBooks/si_manual/   游戏内说明书（自定义书本）
resource_pack_.../ui/                       MOD UI 布局
```

## 平台上架须知

中国版组件审核环境使用 **Python 模块白名单**（`os` / `sys` 等不在白名单），因此「扫描本地目录」功能无法通过组件市场机审。若要上架组件市场，需要将结构以内置数据模块形式打包。本仓库保留完整本地版，供本地游玩与学习交流。

## 常见问题

❓ **Q: 能用在国际版 Minecraft 吗？**
A: 目前仅支持中国版，基于网易 ModAPI 开发。

❓ **Q: 导入个别分区缺方块？**
A: 确认分区导入开着；写失败会自动重试一轮，仍缺可重导一次；急速模式更容易缺。

❓ **Q: 地图画挂墙后是空白？**
A: 请更新到最新版本。旧版存在挂框数据格式与镜像 bug。

❓ **Q: 如何报告 bug 或建议新功能？**
A: 请在 [Issues](https://github.com/HSDL03/StructureImporter/issues) 页面提交详细报告。

## 许可证

本项目采用 **MIT** 许可证 - 详情请参阅 [LICENSE](LICENSE) 文件。

---

<p align="center">
	<sub>由 <a href="https://github.com/HSDL03">HSDL03</a> × AI (WorkBuddy) 协作开发</sub>
</p>
