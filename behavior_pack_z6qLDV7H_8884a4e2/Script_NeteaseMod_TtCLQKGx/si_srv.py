# -*- coding: utf-8 -*-
"""服务端：自定义指令、目录扫描、分批放置"""

import math
import time
import traceback

import mod.server.extraServerApi as serverApi

from Script_NeteaseMod_TtCLQKGx import si_cfg as cfg
from Script_NeteaseMod_TtCLQKGx import si_nbt as mcst
from Script_NeteaseMod_TtCLQKGx import si_scan as scan
from Script_NeteaseMod_TtCLQKGx import si_util as util

try:
    basestring                      # py2 游戏
    _PY2 = True
    _range = xrange
except NameError:
    _PY2 = False
    _range = range

def _val(node, default=None):
    """取 typed 节点里的裸值；不是 typed 节点就原样返回"""
    if isinstance(node, dict) and "__type__" in node:
        return node.get("__value__", default)
    return default if node is None else node


def _merge_nbt(dst, src):
    """把结构里的 NBT 深合并进引擎返回的数据

    不能直接 dict.update：告示牌的 FrontText 这类 compound 整块替换会把它自带的
    FilteredText / TextOwner 等子字段抹掉，引擎随即判为无效，文字不生效
    （实测：只做浅合并时 Text 读回来仍是空串）。
    """
    for key, val in src.items():
        cur = dst.get(key)
        if isinstance(cur, dict) and isinstance(val, dict):
            _merge_nbt(cur, val)
        else:
            dst[key] = val
    return dst


# 命令方块名 -> SetCommandBlock 的 mode 枚举（PULSE / CYCLE / CHAIN）
CMD_BLOCK_MODE = {
    "minecraft:command_block": 0,
    "minecraft:repeating_command_block": 1,
    "minecraft:chain_command_block": 2,
}


def _item_dict(entry):
    """mcstructure 物品 NBT -> SpawnItemToContainer 认的物品字典"""
    node = _val(entry)
    if not isinstance(node, dict):
        return None
    name = _val(node.get("Name"))
    if not name or not isinstance(name, basestring if _PY2 else str):
        return None
    try:
        count = int(_val(node.get("Count")) or 0)
        slot = int(_val(node.get("Slot")) or 0)
        aux = int(_val(node.get("Damage")) or 0)
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None
    item = {"itemName": name, "count": count, "auxValue": aux}
    tag = _val(node.get("tag")) or _val(node.get("ItemTag"))
    if isinstance(tag, dict) and tag:
        item["userData"] = tag
    return {"dict": item, "slot": slot}


HELP = [
    u"§6===== 结构导入器 =====",
    u"§7输入 §f/structureim §7后按 §eTab §7补全功能",
    u"§f/structureim ui §7打开界面",
    u"§f/structureim dir <路径> §7设置目录（用反斜杠）",
    u"§f/structureim scan §7重新扫描",
    u"§f/structureim list [页] §7聊天栏列出结构",
    u"§f/structureim place <序号|名字> [旋转0-3] §7放置",
    u"§f/structureim stop §7中止放置",
    u"§f/structureim pause §7暂停 / 继续当前导入",
    u"§f/structureim info §7查看状态",
    u"§f/structureim split <开|关> §7分区导入开关",
    u"§f/structureim zwait <1-30> §7巡图每区停留秒数（默认3）",
    u"§f/structureim znum <1-9> §7每幅图巡几个区（默认3，实测3区即画满）",
    u"§f/structureim bigwait <1-30> §7跨行远传后降速秒数（默认4）",
    u"§f/structureim budget <2-20> §7每tick写入预算ms（默认7，越大越快越卡；急速自动翻倍）",
    u"§f/structureim selftest §7环境自检",
    u"§f/structureim manual §7看分步教程（也可右键使用说明书物品）",
]


class Job(object):
    """一次放置任务（不用 __slots__，方便热重载时整类替换）

    任务被切成若干"分区"（默认 16×16 的区块列）。每个分区内部走
    方块 -> 红石唤醒 -> 方块实体 三个阶段，干完再进下一个分区。
    分区导入关闭时，整张结构就是唯一的一个分区。
    """

    def __init__(self, pid, title, blocks, base, dim, entities=None, wake=None,
                 sections=None, mobs=None):
        self.pid = pid
        self.title = title
        self.blocks = blocks
        self.entities = entities or []
        self.wake = wake or []
        self.mobs = mobs or []
        self.mob_cursor = 0
        self.mob_ok = 0
        self.mob_fail = 0
        self.sections = sections or [{"blocks": range(len(blocks)),
                                      "wake": [], "ents": [], "label": u"全部"}]
        self.sec_i = 0
        self.cursor = 0
        self.base = base
        self.dim = dim
        self.total = len(blocks)
        self.tick = 0
        self.ok = 0
        self.fail = 0
        self.err = None
        self.ent_cursor = 0
        self.ent_ok = 0
        self.ent_fail = 0
        self.item_ok = 0
        self.ent_fixed = 0
        self.cmd_fixed = 0
        self.wake_cursor = 0
        self.blk_at = {}
        self.phase = "blocks"
        # 防大型结构缺失：每区先加常加载区块，等加载再放置
        self.tick_enabled = False
        self.speed_ticks = 0
        self.waiting = False
        self.wait_until = 0
        self.tp_wait = 0          # 本分区刚 TP 过 -> 强制等区块加载
        self.areas = []          # 已创建的 /tickingarea 名字，收尾时逐个移除
        # 暂停 / 速度统计
        self.paused = False
        self.work_ticks = 0      # 实际干活的 tick 数（暂停和等区块不算），算速度用
        # 写失败的方块（多为区块未加载），分区末尾自动重试一轮
        self.retry = []
        self.retry_cursor = 0
        self.retry_pass = 0
        # 写入节流：每 tick 可写的块数（按实测吞吐自适应，越跑越贴机器）
        self.batch = min(cfg.BATCH, cfg.WRITE_BATCH_START)
        self.probe = cfg.WRITE_PROBE_STEP      # 多少块探一次时间（按单块耗时自动收紧）
        # 上一分区的失败数：为 0 说明区块跟得上 -> 下一区少等一点；有失败就多等
        self.ok_mark = 0
        self.fail_mark = 0
        self.last_fail = 0


class SiServerSystem(serverApi.GetServerSystemCls()):

    def __init__(self, namespace, systemName):
        super(SiServerSystem, self).__init__(namespace, systemName)
        self.factory = serverApi.GetEngineCompFactory()
        self.levelId = serverApi.GetLevelId()
        self.library = scan.Library()
        self.job = None
        self.split = cfg.SPLIT_DEFAULT
        self.tick_mode = cfg.TICK_DEFAULT
        self.tick_speed = cfg.SPEED_DEFAULT
        self.chunk = cfg.CHUNK_DEFAULT
        self.map_mode = cfg.MAP_DEFAULT
        self.turbo = cfg.TURBO_DEFAULT
        self.zone_secs = cfg.TP_ZONE_SECS   # 巡图每区停留秒数（可 /structureim zwait 调）
        self.zones_n = 3                    # 每幅图实际巡几个区（1~9，可 /structureim znum 调）
        self.big_wait = cfg.TP_BIG_WAIT / 20.0   # 跨行远传后降速秒数（/structureim bigwait 调）
        self.budget = cfg.WRITE_BUDGET_MS        # 每 tick 写方块的时间预算（ms，/structureim budget 调）
        self.last_bpt = 0.0                      # 上次实测「每 tick 写多少块」，用于预估耗时
        self._page = 0
        self.map_queue = None    # 导入完成后逐个发的地图
        self._map_known = set()  # 玩家背包里已有的地图 uuid（用于找新图编号）
        self._map_cd = 0
        self.tp_route = None     # 九宫格巡逻路线（导入跟随 TP）
        self.replay_cmds = []    # 代理循环的展示类命令（导入的循环命令方块不跑，mod 替它跑）
        self._manual_cd = {}     # 说明书使用冷却（防长按刷屏）
        self._replay_cd = 0
        self._blockInfo = None
        self._stateInfo = None
        self._msgInfo = None
        self._itemInfo = None
        self._beInfo = None
        self._cmdInfo = None
        self._gameInfo = None

        self._load()
        self._listen()
        util.log("server ready, 目录 =", self.library.root, "结构 =", len(self.library.entries))

    # ------------------------------------------------------------ 组件
    def _blocks(self):
        if self._blockInfo is None:
            self._blockInfo = self.factory.CreateBlockInfo(self.levelId)
        return self._blockInfo

    def _states(self):
        if self._stateInfo is None:
            self._stateInfo = self.factory.CreateBlockState(self.levelId)
        return self._stateInfo

    def _msg(self):
        if self._msgInfo is None:
            self._msgInfo = self.factory.CreateMsg(self.levelId)
        return self._msgInfo

    def _items(self):
        if self._itemInfo is None:
            try:
                self._itemInfo = self.factory.CreateItem(self.levelId)
            except Exception:
                util.log_exc("CreateItem")
                self._itemInfo = False
        return self._itemInfo or None

    def _blockEnt(self):
        if self._beInfo is None:
            try:
                self._beInfo = self.factory.CreateBlockEntity(self.levelId)
            except Exception:
                util.log_exc("CreateBlockEntity")
                self._beInfo = False
        return self._beInfo or None

    def _command(self):
        if self._cmdInfo is None:
            try:
                self._cmdInfo = self.factory.CreateCommand(self.levelId)
            except Exception:
                util.log_exc("CreateCommand")
                self._cmdInfo = False
        return self._cmdInfo or None

    def _game(self):
        if self._gameInfo is None:
            try:
                self._gameInfo = self.factory.CreateGame(self.levelId)
            except Exception:
                util.log_exc("CreateGame")
                self._gameInfo = False
        return self._gameInfo or None

    def tell(self, pid, text):
        if not pid:
            return
        try:
            # 网易这族消息接口只吃 UTF-8 字节串：传 unicode 中文会被静默丢弃
            # （SetTipMessage/SetExtraData/NotifyOneMessage 同款坑，实测）
            self._msg().NotifyOneMessage(pid, util.b(text), "")
        except Exception:
            util.log_exc("tell")

    # ------------------------------------------------------------ T显（tip 通知）
    def _show_tip(self, job, force=False):
        """把当前进度 / 速度显示在物品栏上方的 tip 通知里

        SetTipMessage 是全服广播（单机场景等价于对准本人），每 0.5 秒刷一次。
        速度只按"真正干活的 tick"算：暂停和等区块加载的时间不算进去。
        """
        game = self._game()
        if game is None:
            return
        done = job.ok + job.fail
        if job.total:
            pct = int(done * 100 / job.total)
        else:
            pct = 100
        secs = job.work_ticks / 20.0
        speed = ("§a%d块/s" % int(done / secs)) if secs >= 0.5 else u"§7测速中"

        if job.paused:
            text = u"§e⏸ 结构导入 §c已暂停 §7%d%% %s §8(/structureim pause 继续)" % (pct, speed)
        elif job.waiting:
            text = u"§e⏳ 结构导入 §f%d%% %s §7等区块加载 %s" % (pct, speed, self._sec_tag(job))
        else:
            phase = {u"blocks": u"§f放置", u"wake": u"§b红石唤醒",
                     u"ents": u"§d方块实体", u"mobs": u"§6生成实体"}.get(job.phase, job.phase)
            text = u"§b▶ 结构导入 §f%d%% §7%d/%d %s §7· %s%s" % (
                pct, done, job.total, speed, phase, self._sec_tag(job))
        try:
            # 实测 SetTipMessage 和 SetExtraData 一样只吃 UTF-8 字节串，
            # 传 unicode 直接返回 False 且没有任何提示
            game.SetTipMessage(util.b(text))
        except Exception:
            pass

    # ------------------------------------------------------------ 存档
    def _load(self):
        path = u""
        self.split = cfg.SPLIT_DEFAULT
        self.tick_mode = cfg.TICK_DEFAULT
        self.tick_speed = cfg.SPEED_DEFAULT
        self.chunk = cfg.CHUNK_DEFAULT
        self.map_mode = cfg.MAP_DEFAULT
        self.turbo = cfg.TURBO_DEFAULT
        self.zone_secs = cfg.TP_ZONE_SECS
        self.zones_n = 3
        self.big_wait = cfg.TP_BIG_WAIT / 20.0
        self.budget = cfg.WRITE_BUDGET_MS
        try:
            raw = self.factory.CreateExtraData(self.levelId).GetExtraData(cfg.EXTRA_KEY)
            if isinstance(raw, dict):
                path = util.u(raw.get("dir"))
                flag = util.u(raw.get("split")).strip()
                if flag:
                    self.split = flag == u"1"
                tflag = util.u(raw.get("tick")).strip()
                if tflag:
                    self.tick_mode = tflag == u"1"
                cflag = util.u(raw.get("chunk")).strip()
                if cflag:
                    self.chunk = cflag == u"1"
                mflag = util.u(raw.get("map")).strip()
                if mflag:
                    self.map_mode = mflag == u"1"
                tbflag = util.u(raw.get("turbo")).strip()
                if tbflag:
                    self.turbo = tbflag == u"1"
                try:
                    zs = int(util.u(raw.get("zsecs")))
                    self.zone_secs = max(1, min(30, zs))
                except (TypeError, ValueError):
                    pass
                try:
                    zn = int(util.u(raw.get("znum")))
                    self.zones_n = max(1, min(9, zn))
                except (TypeError, ValueError):
                    pass
                try:
                    bw = float(util.u(raw.get("bigwait")))
                    self.big_wait = max(1.0, min(30.0, bw))
                except (TypeError, ValueError):
                    pass
                try:
                    bg = float(util.u(raw.get("budget")))
                    self.budget = max(2.0, min(20.0, bg))
                except (TypeError, ValueError):
                    pass
                try:
                    speed = int(util.u(raw.get("speed")))
                    self.tick_speed = max(cfg.SPEED_MIN, min(cfg.SPEED_MAX, speed))
                except (TypeError, ValueError):
                    pass
            elif raw:
                path = util.u(raw)
        except Exception:
            util.log_exc("load config")
        if path:
            self.library.set_root(path)
        else:
            self.library.rescan()

    def _save(self):
        # SetExtraData 只吃 str：unicode 键/值都会失败，中文路径要编码成 utf-8
        try:
            self.factory.CreateExtraData(self.levelId).SetExtraData(
                str(cfg.EXTRA_KEY),
                {"dir": util.b(self.library.root),
                 "split": util.b(u"1" if self.split else u"0"),
                 "tick": util.b(u"1" if self.tick_mode else u"0"),
                 "speed": util.b(str(self.tick_speed)),
                 "chunk": util.b(u"1" if self.chunk else u"0"),
                 "map": util.b(u"1" if self.map_mode else u"0"),
                 "turbo": util.b(u"1" if self.turbo else u"0"),
                 "zsecs": util.b(str(self.zone_secs)),
                 "znum": util.b(str(self.zones_n)),
                 "bigwait": util.b(str(self.big_wait)),
                 "budget": util.b(str(self.budget))})
        except Exception:
            util.log_exc("save config")

    # ------------------------------------------------------------ 事件
    def _listen(self):
        ns = serverApi.GetEngineNamespace()
        sn = serverApi.GetEngineSystemName()
        self.ListenForEvent(ns, sn, "CustomCommandTriggerServerEvent", self, self.on_command)
        # 这个事件没有参数，回调不能强制要求 args
        self.ListenForEvent(ns, sn, "OnScriptTickServer", self, self.on_tick)
        self.ListenForEvent(cfg.MOD_NAME, cfg.CLIENT_SYSTEM_NAME, cfg.EV_REQ, self, self.on_req)
        self.ListenForEvent(cfg.MOD_NAME, cfg.CLIENT_SYSTEM_NAME, cfg.EV_PLACE, self, self.on_place)
        # 右键使用说明书物品 -> 打印教程（网易引擎事件名随版本有差异，多挂几个兜底）
        for ev in ("ServerItemUseOnEvent", "ScriptServerItemUseOnEvent",
                   "ServerItemTryUseEvent", "ScriptServerItemTryUseEvent",
                   "ItemUseAfterServerEvent", "ServerItemUseEvent"):
            try:
                self.ListenForEvent(ns, sn, ev, self, self.on_use_item)
            except Exception:
                pass

    # ------------------------------------------------------------ 指令
    def on_use_item(self, args):
        try:
            pid = args.get("playerId") or args.get("entityId")
            it = args.get("itemDict") or {}
            name = it.get("newItemName") or it.get("itemName")
            if name != "si:manual" or not pid:
                return
            now = util.now_ms() if hasattr(util, "now_ms") else None
            if now is None:
                import time
                now = int(time.time() * 1000)
            if now - self._manual_cd.get(pid, 0) < 3000:
                return
            self._manual_cd[pid] = now
            self.cmd_manual(pid)
        except Exception:
            util.log_exc("use manual")

    def cmd_manual(self, pid):
        self.tell(pid, u"§6════ 结构导入说明书 ════")
        self.tell(pid, u"§e一、准备：§f/structureim dir <路径> §7设置结构文件夹（如 D:////我的结构）")
        self.tell(pid, u"§7  然后 §fscan §7重新扫描，§flist §7看列表，§fui §7开界面")
        self.tell(pid, u"§e二、放置：§f/structureim place <序号或名字> [旋转0-3]")
        self.tell(pid, u"§7  · 大结构自动分区导入，你人不用动（会自动跟随传送）")
        self.tell(pid, u"§7  · §fstop §7中止，§fpause §7暂停/继续，§finfo §7看进度")
        self.tell(pid, u"§e三、地图墙（地图画模式开时自动生效）：")
        self.tell(pid, u"§7  · 导完自动搭墙并给你编号地图，凑近看就是整幅画")
        self.tell(pid, u"§7  · 巡图参数：§fznum <1-9> §7每幅巡几区（默认3）、§fzwait <1-30> §7每区停几秒（默认3）")
        self.tell(pid, u"§e四、调速：§fbudget <2-20> §7每 tick 写入预算（ms，越大越快越卡，默认7）")
        self.tell(pid, u"§7  写入量会按机器实测速度自动调，卡就调小、闲着就调大")
        self.tell(pid, u"§e五、其它开关：§fsplit 开/关 §7分区、§fturbo §7急速、§fselftest §7自检")
        self.tell(pid, u"§7详情输入 §f/structureim §7看完整命令列表。")

    def on_command(self, args):
        try:
            if util.u(args.get("command")).lower() != cfg.CMD_NAME:
                return
            args["return_msg_key"] = u"§7·"
        except Exception:
            util.log_exc("command head")
            return
        try:
            origin = args.get("origin") or {}
            pid = origin.get("entityId")
            argv = []
            for item in (args.get("args") or []):
                value = item.get("value") if isinstance(item, dict) else None
                if isinstance(value, (list, tuple)):      # enum 未传值时可能给整张候选表
                    value = value[0] if value else None
                argv.append(util.u(value))
            while len(argv) < 3:
                argv.append(u"")
            action = (argv[0] or u"ui").strip().lower()
            self.dispatch(pid, action, argv[1].strip(), argv[2].strip())
        except Exception:
            util.log_exc("command body")

    def dispatch(self, pid, action, a1, a2):
        if action in (u"ui", u"open"):
            self.cmd_ui(pid)
        elif action in (u"dir", u"set"):
            self.cmd_dir(pid, a1)
        elif action == u"scan":
            self.library.rescan()
            self.report(pid)
        elif action == u"list":
            self.cmd_list(pid, a1)
        elif action == u"place":
            self.cmd_place(pid, a1, a2)
        elif action == u"stop":
            if self.job is not None:
                self._clear_areas(self.job)
            self.job = None
            self.tell(pid, u"§a已中止")
        elif action in (u"pause", u"resume", u"p"):
            self.cmd_pause(pid)
        elif action == u"info":
            self.cmd_info(pid)
        elif action == u"selftest":
            self.cmd_selftest(pid)
        elif action in (u"split", u"zone"):
            self.cmd_split(pid, a1)
        elif action in (u"zwait", u"zsec"):
            self.cmd_zwait(pid, a1)
        elif action == u"bigwait":
            self.cmd_bigwait(pid, a1)
        elif action in (u"budget", u"tick"):
            self.cmd_budget(pid, a1)
        elif action == u"znum":
            self.cmd_znum(pid, a1)
        elif action in (u"manual", u"help2", u"书"):
            self.cmd_manual(pid)
        elif action == u"test":
            self.cmd_test(pid)
        else:
            for line in HELP:
                self.tell(pid, line)

    # ------------------------------------------------------------ 各子指令
    def cmd_ui(self, pid):
        if not pid:
            util.log("ui 需要玩家身份（命令方块 / 服务器控制台无法开界面）")
            return
        self.NotifyToClient(pid, cfg.EV_OPEN, {"page": 0})

    def cmd_dir(self, pid, text):
        path = util.norm_path(text)
        if not path:
            self.tell(pid, u"§e用法：/structureim dir D:\\我的结构")
            return
        if not util.is_dir(path):
            self.tell(pid, u"§c目录不存在：" + path)
            return
        self.library.set_root(path)
        self._save()
        self.report(pid)

    def report(self, pid):
        lib = self.library
        self.tell(pid, u"§a目录：§f" + util.short(lib.root, 60))
        if lib.err:
            self.tell(pid, u"§c" + lib.err)
            return
        self.tell(pid, u"§f共 §e%d §f个结构，用 §f/structureim list §f看列表" % len(lib.entries))

    def cmd_list(self, pid, page_text):
        lib = self.library
        if not lib.entries:
            self.tell(pid, u"§c" + (lib.err or u"列表是空的"))
            return
        page = 1
        try:
            page = max(1, int(page_text or 1))
        except Exception:
            page = 1
        pages = max(1, (len(lib.entries) + cfg.ROWS - 1) // cfg.ROWS)
        page = min(page, pages)
        start = (page - 1) * cfg.ROWS
        self.tell(pid, u"§6第 %d/%d 页" % (page, pages))
        for i in range(start, min(start + cfg.ROWS, len(lib.entries))):
            self.tell(pid, u"§7%d. §f%s" % (i + 1, util.short(lib.entries[i].rel, 40)))

    def cmd_split(self, pid, text):
        t = util.u(text).strip().lower()
        if t in (u"", u"toggle", u"switch"):
            self.split = not self.split
        elif t in (u"on", u"true", u"1", u"open", u"开"):
            self.split = True
        elif t in (u"off", u"false", u"0", u"close", u"关"):
            self.split = False
        else:
            self.tell(pid, u"§e用法：/structureim split <开/关>")
            return
        self._save()
        self.tell(pid, u"§a分区导入：§f%s §7（每区 %d 格）" %
                  (u"开" if self.split else u"关", cfg.SPLIT_SIZE))
        self.push(pid, self._page)

    def cmd_zwait(self, pid, text):
        try:
            v = int((text or u"").strip())
        except ValueError:
            self.tell(pid, u"§e用法：/structureim zwait <1-30> §7当前 %d 秒" % self.zone_secs)
            return
        if not (1 <= v <= 30):
            self.tell(pid, u"§c范围 1~30 秒")
            return
        self.zone_secs = v
        self._save()
        self.tell(pid, u"§a巡图每区停留：§f%d 秒" % v)

    def cmd_bigwait(self, pid, text):
        try:
            v = float((text or u"").strip())
        except ValueError:
            self.tell(pid, u"§e用法：/structureim bigwait <1-30> §7当前 %.1f 秒" % self.big_wait)
            return
        if not (1.0 <= v <= 30.0):
            self.tell(pid, u"§c范围 1~30 秒")
            return
        self.big_wait = v
        self._save()
        self.tell(pid, u"§a跨行远传后降速：§f%.1f 秒" % v)

    def cmd_budget(self, pid, text):
        """每 tick 写方块的时间预算（ms）——导入快慢/卡不卡的直接旋钮"""
        try:
            v = float((text or u"").strip())
        except ValueError:
            self.tell(pid, u"§e用法：/structureim budget <2-20> §7当前 %.0f ms%s" %
                      (self.budget, u"（急速模式下自动翻倍）" if self.turbo else u""))
            return
        if not (2.0 <= v <= 20.0):
            self.tell(pid, u"§c范围 2~20 毫秒")
            return
        self.budget = v
        self._save()
        self.tell(pid, u"§a每 tick 写入预算：§f%.0f ms §7（约 %d 块/秒起，实际按机器自动调速）" %
                  (v, int(cfg.BATCH * 20)))

    def cmd_znum(self, pid, text):
        try:
            v = int((text or u"").strip())
        except ValueError:
            self.tell(pid, u"§e用法：/structureim znum <1-9> §7当前 %d 区" % self.zones_n)
            return
        if not (1 <= v <= 9):
            self.tell(pid, u"§c范围 1~9 区")
            return
        self.zones_n = v
        self._save()
        self.tell(pid, u"§a每幅图巡区数：§f%d 区" % v)

    def cmd_place(self, pid, which, rot_text):
        entry = self.library.find(which)
        if entry is None:
            self.tell(pid, u"§c找不到这个结构：%s" % (which or u"(空)"))
            return
        rot = 0
        try:
            rot = int(rot_text or 0) % 4
        except Exception:
            rot = 0
        self.start_job(pid, entry, rot)

    def cmd_pause(self, pid):
        job = self.job
        if job is None:
            self.tell(pid, u"§e当前没有进行中的导入任务")
            return
        job.paused = not job.paused
        if job.paused:
            self.tell(pid, u"§e导入已暂停：§f%s §7（再输入 §f/structureim pause §7继续）" %
                      util.short(job.title, 30))
        else:
            self.tell(pid, u"§a导入已继续：§f%s" % util.short(job.title, 30))
        self._show_tip(job, True)

    def cmd_info(self, pid):
        lib = self.library
        self.tell(pid, u"§6目录：§f" + (lib.root or u"(未设置)"))
        self.tell(pid, u"§6结构数：§f%d" % len(lib.entries))
        self.tell(pid, u"§6放置中：§f%s" % (u"是" if self.job else u"否"))
        self.tell(pid, u"§6分区导入：§f%s §7（每区 %d 格）" %
                  (u"开" if self.split else u"关", cfg.SPLIT_SIZE))
        self.tell(pid, u"§6每 tick：§f%.0f ms 预算§7（约 %d 块起，自动调速）" %
                  (self._write_budget(), (self.job.batch if self.job else cfg.BATCH)))

    def cmd_test(self, pid):
        """在身前写一块金块，用来确认 SetBlockNew 本身是否可用"""
        comp = self._blocks()
        if comp is None:
            self.tell(pid, u"§c方块组件为空（CreateBlockInfo 失败）")
            return
        base, dim = self._base(pid, (1, 1, 1))
        pos = (base[0], base[1], base[2])
        self.tell(pid, u"§7尝试写入 §f%s §7维度 §f%d" % (util.u(pos), dim))
        try:
            ok = comp.SetBlockNew(pos, {"name": "minecraft:gold_block", "aux": 0}, 0, dim)
            self.tell(pid, u"§6结果：§f%s" % (u"成功" if ok else u"返回 False"))
        except Exception:
            self.tell(pid, u"§c异常：§f" + util.short(
                util.u(traceback.format_exc().strip().splitlines()[-1]), 60))

    def cmd_selftest(self, pid):
        import sys
        ok_dir = util.is_dir(self.library.root)
        self.tell(pid, u"§6Python：§f" + util.u(sys.version.split()[0]))
        self.tell(pid, u"§6目录：§f%s §7(%s)" % (util.short(self.library.root or u"(未设置)", 46),
                                              u"可读" if ok_dir else u"不可读"))
        self.tell(pid, u"§6结构数：§f%d §7/ 上限 %d" % (len(self.library.entries), cfg.MAX_ENTRIES))
        try:
            comps = (self._blocks() is not None, self._states() is not None)
        except Exception:
            comps = (False, False)
        self.tell(pid, u"§6方块组件：§f%s  状态组件：§f%s" %
                  (u"OK" if comps[0] else u"缺失", u"OK" if comps[1] else u"缺失"))

    # ------------------------------------------------------------ 放置
    def start_job(self, pid, entry, rotation):
        try:
            data = mcst.load(entry.path, entry.name, cfg.SKIP_AIR, cfg.SKIP_VOID)
        except Exception as exc:
            util.log_exc("load " + entry.path)
            self.tell(pid, u"§c读取失败：§f%s" % util.u(exc))
            return
        if data.count <= 0:
            self.tell(pid, u"§c这个结构里没有可放置的方块")
            return
        if data.count > cfg.MAX_BLOCKS:
            self.tell(pid, u"§c结构太大：%d 方块（上限 %d）" % (data.count, cfg.MAX_BLOCKS))
            return

        # 超大文件自动强制分块模式：反正会切成 <50KB 的小块逐个导入，不会卡死
        use_chunk = self.chunk or data.count > cfg.BIG_FILE_BLOCKS
        if use_chunk and not self.chunk:
            self.tell(pid, u"§e大型结构（%d 方块）：已自动开启分块导入" % data.count)

        blocks, size = mcst.rotate(data.blocks, data.size, rotation)
        blocks = self._with_aux(blocks)
        ents = mcst.rotate_entities(data.block_entities, data.size, rotation)
        mobs = mcst.rotate_entities(data.entities, data.size, rotation)
        base, dim = self._base(pid, size)
        if self.map_mode:
            # 地图网格实测：单元以 128 的倍数为中心，边界在 128k - 64。
            # 基点必须对到"边界"而不是中心——对到中心会把画劈成两张地图各一半。
            # 对齐后 基点+64 是 128 的倍数（=第 0 张地图单元中心），发放地图时
            # TP 到 基点+c*128+64 正好是每张图的中心。
            ms = cfg.MAP_SIZE
            base = (int(round((base[0] + 64.0) / ms)) * ms - 64, base[1],
                    int(round((base[2] + 64.0) / ms)) * ms - 64)
            self.tell(pid, u"§7地图画模式：基点已对齐地图网格 §f%d, %d §7（尺寸 %dx%d → %d 张地图）" %
                      (base[0], base[2],
                       max(b[0] for b in blocks) - min(b[0] for b in blocks) + 1,
                       max(b[2] for b in blocks) - min(b[2] for b in blocks) + 1,
                       int(math.ceil((max(b[0] for b in blocks) - min(b[0] for b in blocks) + 1) / float(ms))) *
                       int(math.ceil((max(b[2] for b in blocks) - min(b[2] for b in blocks) + 1) / float(ms)))))

        # 红石相关方块最后再触发一次邻居更新，否则刚放下去时容易被判成"失去支撑"
        wake = []
        for i in range(len(blocks)):
            low = blocks[i][3].lower()
            for kw in cfg.REDSTONE_HINTS:
                if kw in low:
                    wake.append(i)
                    break

        sections = self._build_sections(blocks, wake, ents, use_chunk)
        self.job = Job(pid, entry.rel, blocks, base, dim, ents, wake, sections, mobs)
        self.replay_cmds = []
        self.job.tick_enabled = self.tick_mode
        self.job.speed_ticks = int(self.tick_speed) * 20
        if ents:
            # 坐标 -> 原本该放什么方块，写实体后要拿它核对/还原
            table = {}
            for (x, y, z, name, aux, states) in blocks:
                table[(x, y, z)] = (name, aux, states)
            self.job.blk_at = table
        else:
            self.job.blk_at = {}
        self.tell(pid, u"§a开始放置 §f%s" % util.short(entry.rel, 40))
        self.tell(pid, u"§7%d×%d×%d · %d 方块 · 旋转 %d" %
                 (size[0], size[1], size[2], len(blocks), int(rotation) % 4))
        if len(sections) > 1:
            if use_chunk:
                self.tell(pid, u"§7分块导入：§f开 §7· 切成 §f%d §7块（每块 <§f%dKB§7）" %
                          (len(sections), cfg.CHUNK_MAX_BYTES // 1024))
            else:
                self.tell(pid, u"§7分区导入：§f开 §7· 切成 §f%d §7个分区（每区 %d 格）" %
                          (len(sections), cfg.SPLIT_SIZE))
        if self.tick_mode:
            self.tell(pid, u"§7防缺失：§f开 §7· 每区等待 §f%d 秒 §7（用掉一个常加载空位）" %
                      self.tick_speed)
        if ents:
            self.tell(pid, u"§7含 %d 个方块实体（箱子 / 告示牌等）" % len(ents))
        if mobs:
            self.tell(pid, u"§7含 %d 个实体（矿车 / 船 / 生物等）" % len(mobs))
        est = self._estimate_secs(len(blocks), ents, wake, sections, mobs)
        est_msg = u"§7预计耗时：§f约 %.1f 秒" % est
        if self.tick_mode:
            est_msg += u" §7（其中区块等待 %d×%d 秒）" % (len(sections), self.tick_speed)
        self.tell(pid, est_msg)
        self._prepare_section(self.job)
        self.push(pid, 0)

    # ------------------------------------------------------------ 地图画模式
    # 地图画模式：玩家面前搭一面"地图墙"（墙块 + 展示框方块），地图按编号挂上去
    # 展示框实测是方块（minecraft:frame），物品走 SetFrameItem 方块实体接口
    # aux 实测语义 = 框面朝向（2北3南4西5东）；框面朝玩家 = 视线的反向，
    # aux 填错框会因找不到支撑变成掉落物（真机踩坑）
    _MAP_VIEW = {2: (0, 0, -1), 3: (0, 0, 1), 4: (-1, 0, 0), 5: (1, 0, 0)}
    _MAP_FACE = {2: 3, 3: 2, 4: 5, 5: 4}

    def _queue_maps(self, job):
        """导入完成后：算出地图网格 -> 玩家面前搭墙 -> 排队逐张 TP+give+挂框"""
        if not job.blocks or not job.pid:
            return
        xs = [b[0] for b in job.blocks]
        ys = [b[1] for b in job.blocks]
        zs = [b[2] for b in job.blocks]
        ms = cfg.MAP_SIZE
        x0, x1 = min(xs), max(xs)
        z0, z1 = min(zs), max(zs)
        cols = int(math.ceil((x1 - x0 + 1) / float(ms)))
        rows = int(math.ceil((z1 - z0 + 1) / float(ms)))
        n = cols * rows
        if n > 400:
            self.tell(job.pid, u"§c地图太多（%d 张），放弃发放" % n)
            return
        # 每张图 9 个区（3×3）：发图前依次 TP 巡逻一圈，区块充分加载再生成
        dv = max(1, int(cfg.TP_ZONE_DIV))
        cells = []
        for r in range(rows):
            for c in range(cols):
                cx0 = job.base[0] + x0 + c * ms
                cz0 = job.base[2] + z0 + r * ms
                # 整数运算防浮点误差（区中心必须精确落在地图单元网格上）
                zones = [(cx0 + (rx * ms + ms // 2) // dv,
                          cz0 + (rz * ms + ms // 2) // dv)
                         for rz in range(dv) for rx in range(dv)]
                cells.append({"zones": zones, "k": r * cols + c + 1, "zi": 0,
                              "anchor": (cx0, cz0)})

        # ---- 玩家面前搭地图墙
        built = self._build_map_wall(job.pid, cols, rows)
        frames, wall_info = (built if built else ([None] * n, None))
        if built is None:
            self.tell(job.pid, u"§c地图墙搭建失败，改为发放到背包")
        self.map_queue = {"pid": job.pid, "title": util.short(
            util.u(job.title), 20), "cells": cells, "i": 0, "n": n,
            "cols": cols, "rows": rows, "frames": frames, "stage": "zone",
            "retry": 0, "dim": job.dim, "areas": [], "maps": [],
            "wall": wall_info,
            "y": job.base[1] + max(ys) + 6.0}
        self._map_cd = 0
        self.tell(job.pid, u"§a地图墙已搭好（%d列×%d行）。开始巡图：每张图分 §f%d×%d §a区，每幅巡前 §f%d §a区、每区停 §f%d §a秒（znum/zwait 可调）…" %
                  (cols, rows, dv, dv, self.zones_n, self.zone_secs))

    def _build_map_wall(self, pid, cols, rows):
        """在玩家面前搭一面 cols×rows 的墙：展示框方块 + 背后墙块

        返回 [第k张地图对应的展示框坐标]；失败返回 None。
        """
        try:
            comp = self.factory.CreatePos(pid)
            pos = comp.GetFootPos()
            rot = self.factory.CreateRot(pid).GetRot()
            yaw = float(rot[1])
            # MC yaw：0=南(+z) 90=西(-x) 180=北(-z) 270=东(+x)
            if yaw >= -45 and yaw < 45:
                view, v = 3, (0, 0, 1)          # 朝南
            elif yaw >= 45 and yaw < 135:
                view, v = 4, (-1, 0, 0)         # 朝西
            elif yaw >= -135 and yaw < -45:
                view, v = 5, (1, 0, 0)          # 朝东
            else:
                view, v = 2, (0, 0, -1)         # 朝北
            # 水平垂直方向：必须指向玩家右手边（c 增大 = 眼中从左到右），
            # 取反会整面墙左右镜像（实测踩坑）
            pv = (-v[2], 0, v[0])
            ax, ay, az = int(math.floor(pos[0] + v[0] * 10)), int(math.floor(pos[1] + 2)), int(math.floor(pos[2] + v[2] * 10))
            bi = self._blocks()
            if bi is None:
                return None
            frames = [None] * (cols * rows)
            for r in range(rows):               # r=0 顶行
                for c in range(cols):
                    fc = c - (cols - 1) // 2
                    fr = (rows - 1) - r
                    fp = (ax + pv[0] * fc, ay + fr, az + pv[2] * fc)
                    sp = (fp[0] + v[0], fp[1] + v[1], fp[2] + v[2])
                    bi.SetBlockNew(sp, {"name": "minecraft:polished_andesite", "aux": 0}, 0, 0, True, False)
                    if bi.SetBlockNew(fp, {"name": "minecraft:frame",
                                           "aux": self._MAP_FACE[view]}, 0, 0, True, False):
                        frames[r * cols + c] = fp
            # 观景位：墙中心前方 8 格（玩家站这里时整面墙区块都是加载的）
            view_pos = (ax - v[0] * 8, ay + rows / 2.0, az - v[2] * 8)
            return frames, view_pos
        except Exception:
            util.log_exc("build map wall")
            return None

    def _give_maps(self):
        """逐张：TP 到单元中心 -> 等区块加载 -> /give 生成地图 -> 取走挂到墙上"""
        q = self.map_queue
        self._map_cd -= 1
        if self._map_cd > 0:
            return
        pid = q["pid"]
        try:
            if q["stage"] == "zone":
                cell = q["cells"][q["i"]]
                # 地图由携带者持续绘制周围地形：一区就 give，跟着人走完九区，
                # 走完才收回来挂墙。TA 保证整幅单元区块加载（含give前的第一帧）。
                cx, cz = cell["zones"][0]
                name = u"si_map_%d" % cell["k"]
                cmd = self._command()
                if cmd:
                    cmd.SetCommand(util.b(u"/tickingarea add circle %d %d %d %d %s" %
                                          (cx, int(q["y"]), cz, cfg.MAP_TA_RADIUS, name)))
                    q["areas"].append(name)
                self.factory.CreatePos(pid).SetPos((cx, q["y"], cz))
                q["stage"] = "give"
                q["retry"] = 0
                self._map_cd = cfg.MAP_TA_WAIT      # 等整幅区块真正加载完再给图
                return
            if q["stage"] == "give":
                cell = q["cells"][q["i"]]
                cell["zi"] = 1                      # 一区已站；实测前 3 区就能画满整幅
                q["stage"] = "give2"
                self._map_cd = 10
                return
            if q["stage"] == "give2":
                cmd = self._command()
                if not cmd or not cmd.SetCommand(util.b(u'/give @a filled_map 1')):
                    self.map_queue = None
                    return
                q["stage"] = "equip"
                self._map_cd = 10                       # 等 give 到包
                return
            if q["stage"] == "equip":
                # 地图必须先在玩家主手里停留渲染，引擎才把地形画上去；
                # give 后直接挂墙就是空白图。占住选中槽（备份原物品），渲染完恢复。
                found = self._find_new_map(pid)
                if found is None:
                    q["retry"] += 1
                    if q["retry"] <= 3:
                        q["stage"] = "give2"
                        self._map_cd = 20
                        return
                    self.tell(pid, u"§c第 %d 张地图生成失败，跳过" % q["cells"][q["i"]]["k"])
                    q["i"] += 1
                    q["stage"] = "zone"
                    self._map_cd = 5
                    return
                mu, slot, mapd = found
                try:
                    it = self.factory.CreateItem(pid)
                    sel = it.GetSelectSlotId()
                    items = it.GetPlayerAllItems(0, True) or []
                    q["restore"] = dict(items[sel]) if sel < len(items) and items[sel] else {}
                    it.SetPlayerAllItems({(0, sel): mapd})
                    q["map_uuid"] = mu
                    q["hold_slot"] = sel
                    q["stage"] = "patrol"
                    self._map_cd = max(1, int(self.zone_secs)) * 20
                except Exception:
                    util.log_exc("equip map")
                    q["map_uuid"] = mu
                    q["stage"] = "grab"
                    self._map_cd = 1
                return
            if q["stage"] == "patrol":
                # 地图在主手随人走，引擎持续把周围地形画进地图
                cell = q["cells"][q["i"]]
                zi = cell.get("zi", 1)
                # 只巡前 zones_n 区（实测 3 区即画满整幅；znum 可调 1~9）
                if zi >= self.zones_n or zi >= len(cell["zones"]):
                    q["stage"] = "grab"             # 巡完，收图
                    self._map_cd = 1
                    return
                zx, zz = cell["zones"][zi]
                cell["zi"] = zi + 1
                self.factory.CreatePos(pid).SetPos((zx, q["y"], zz))
                self._map_cd = max(1, int(self.zone_secs)) * 20
                return
            if q["stage"] == "mount":
                if q["i"] >= len(q["maps"]):
                    self.tell(pid, u"§a地图墙挂好了，凑近看就是完整的一幅画")
                    self.map_queue = None
                    return
                k, item = q["maps"][q["i"]]
                fp = q["frames"][k - 1] if q["frames"] else None
                if fp is not None:
                    # 关键：用引擎自己产出的物品 dict（含正确 aux 地图编号与完整
                    # userData）；自己拼 dict 引擎会当空白图渲染
                    d = dict(item)
                    be = self._blockEnt()
                    if not be.SetFrameItem(fp, q.get("dim", 0), d):
                        self.tell(pid, u"§c第 %d 张挂框失败" % k)
                q["i"] += 1
                self._map_cd = 5
                return
            # grab：主手渲染完 -> 恢复原主手物品 -> 挂上墙
            k = q["cells"][q["i"]]["k"]
            mu = q.get("map_uuid")
            held = None
            try:
                it = self.factory.CreateItem(pid)
                sel = q.get("hold_slot")
                items = it.GetPlayerAllItems(0, True) or []
                if sel is not None and sel < len(items):
                    held = items[sel]          # 主手里的地图（引擎渲染后的最新状态）
                    it.SetPlayerAllItems({(0, sel): q.get("restore") or {}})
            except Exception:
                util.log_exc("restore hand")
            if held is not None:
                q["maps"].append((k, held))     # 引擎原生 dict，挂框时原样塞回
            elif mu is not None:
                q["maps"].append((k, {"itemName": "minecraft:filled_map", "count": 1,
                                      "auxValue": 0,
                                      "userData": {"map_uuid": mu}}))
            else:
                self.tell(pid, u"§c第 %d 张地图生成失败，跳过" % k)
            # 这张图发完，撤掉它的常加载区块
            while q["areas"]:
                nm = q["areas"].pop(0)
                rc = self._command()
                if rc:
                    rc.SetCommand(util.b(u"/tickingarea remove %s" % nm))
            row = (k - 1) // q["cols"] + 1
            col = (k - 1) % q["cols"] + 1
            self.tell(pid, u"§7地图 §f%d/%d §8(第 %d 行 第 %d 列) §7已生成" %
                      (k, q["n"], row, col))
            q["i"] += 1
            if q["i"] >= q["n"]:
                # 全部生成完 -> 回到墙前统一挂框（墙区块跟着人加载，
                # 避免对已卸载区块的展示框写数据触发引擎崩溃）
                q["stage"] = "mount"
                q["i"] = 0
                w = q.get("wall")
                if w:
                    try:
                        self.factory.CreatePos(pid).SetPos(w)
                    except Exception:
                        util.log_exc("tp to wall")
                self._map_cd = 60                    # 等墙区块加载
                return
            q["stage"] = "zone"
            self._map_cd = 5
        except Exception:
            util.log_exc("give maps")
            try:
                while q["areas"]:
                    nm = q["areas"].pop(0)
                    rc = self._command()
                    if rc:
                        rc.SetCommand(util.b(u"/tickingarea remove %s" % nm))
            except Exception:
                pass
            self.map_queue = None

    def _find_new_map(self, pid):
        """diff 背包找刚 /give 的地图，返回 (uuid, 槽位, 完整物品dict)；不动背包"""
        try:
            it = self.factory.CreateItem(pid)
            items = it.GetPlayerAllItems(0, True) or []
            known = self._map_known
            for i, x in enumerate(items):
                if x and (x.get("newItemName") or x.get("itemName")) == "minecraft:filled_map":
                    mu = (x.get("userData") or {}).get("map_uuid")
                    mu = mu.get("__value__") if isinstance(mu, dict) else mu
                    if mu not in known:
                        known.add(mu)
                        return (mu, i, x)
            return None
        except Exception:
            util.log_exc("find map")
            return None

    def _estimate_secs(self, n_blocks, ents, wake, sections, mobs=()):
        """点击放置时给个预期

        名义吞吐：每 tick 最多 BATCH 个方块（20 tick/秒），方块实体同理；
        实体走 summon 指令，每秒 BATCH_MOB*20 个（实际指令更慢，只当下限）。
        防缺失模式下每个分区还要先等区块加载。
        实际速度受世界卡顿影响，仅供预期，T显里会显示实测速度。
        """
        if self.last_bpt > 0:
            rate = self.last_bpt * 20.0     # 上次真机实测的吞吐，比名义值准
        else:
            rate = max(1, cfg.BATCH) * 20.0
            if self.turbo:
                rate *= 2.0
        secs = n_blocks / rate + len(wake) / rate
        secs += len(ents) / float(max(1, cfg.BATCH_ENT) * 20)
        secs += len(mobs) / float(max(1, cfg.BATCH_MOB) * 20)
        if self.tick_mode:
            secs += len(sections) * self.tick_speed
        elif self.turbo and len(sections) > 1:
            secs += len(sections) * (cfg.TP_LOAD_WAIT_TURBO // 2) / 20.0
        return max(0.1, secs)

    def _build_sections(self, blocks, wake, ents, use_chunk=None):
        """按水平区块把整张结构切成若干分区

        分区模式下，一区的方块放完 -> 补红石唤醒 -> 写方块实体，再进下一区。
        这样一次只动一小片区块，巨型结构不会一口气把主线程顶死。
        防缺失模式开启时强制走分区（哪怕玩家没开分区开关）。
        分块模式再进一步：每个分区若数据量超过 50KB，就按 Y 继续切成
        更小的带状分块，直到全部分块都小于阈值（把一个文件当多个小文件处理）。
        use_chunk 为 None 时用 self.chunk；超大文件会强制传 True。
        """
        if use_chunk is None:
            use_chunk = self.chunk
        if not (self.split or self.tick_mode or use_chunk):
            return [{"blocks": _range(len(blocks)),
                     "wake": wake,
                     "ents": _range(len(ents)),
                     "label": u"全部"}]

        size = max(1, int(cfg.SPLIT_SIZE))
        groups = {}

        def group(x, z):
            key = (int(x) // size, int(z) // size)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"blocks": [], "wake": [], "ents": [],
                                   "label": u"%d,%d" % key}
            return g

        for i in _range(len(blocks)):
            b = blocks[i]
            group(b[0], b[2])["blocks"].append(i)
        for i in wake:
            b = blocks[i]
            group(b[0], b[2])["wake"].append(i)
        for i in _range(len(ents)):
            e = ents[i]
            group(e[0], e[2])["ents"].append(i)

        if not use_chunk:
            return [groups[k] for k in sorted(groups.keys())]

        # 分块模式：超重的 16x16 列按 Y 再切，直到每块数据量 < 50KB
        out = []
        for key in sorted(groups.keys()):
            g = groups[key]
            if self._chunk_weight(g, blocks) <= cfg.CHUNK_MAX_BYTES:
                out.append(g)
                continue
            # 方块的打包预算要给实体/唤醒的权重留出空间
            budget = max(1, cfg.CHUNK_MAX_BYTES -
                         len(g["ents"]) * 200 - len(g["wake"]) * 4)
            order = sorted(g["blocks"], key=lambda i: blocks[i][1])
            bands = []                 # 按新顺序切成若干 Y 带状分块
            cur, cur_w = [], 0
            for i in order:
                w = self._blk_weight(blocks[i])
                if cur and cur_w + w > budget:
                    bands.append(cur)
                    cur, cur_w = [], 0
                cur.append(i)
                cur_w += w
            if cur:
                bands.append(cur)
            y_band = {}
            for bi, band in enumerate(bands):
                for i in band:
                    y_band.setdefault(blocks[i][1], bi)
            for bi, band in enumerate(bands):
                y_lo = min(blocks[i][1] for i in band)
                y_hi = max(blocks[i][1] for i in band)
                out.append({"blocks": band,
                            "wake": [i for i in g["wake"]
                                     if y_band.get(blocks[i][1]) == bi],
                            "ents": [i for i in g["ents"]
                                     if y_band.get(ents[i][1]) == bi],
                            "label": u"%s#%d" % (g["label"], bi + 1)})
        return out

    def _blk_weight(self, block):
        """单个方块的估算数据量（名字 + states 序列化近似）

        方块元组是 (x, y, z, name, aux, states)——注意 states 在第 6 位，
        第 5 位是 aux；写成 5 元组解包会 TypeError（真机踩过）。
        """
        name = block[3]
        states = block[5] if len(block) > 5 else block[4]
        w = len(name) + 16
        for k in (states or {}):
            w += len(str(k)) + 6
        return w

    def _chunk_weight(self, sec, blocks):
        return (sum(self._blk_weight(blocks[i]) for i in sec["blocks"]) +
                len(sec["ents"]) * 200 + len(sec["wake"]) * 4)

    def _with_aux(self, blocks):
        """把 states 翻成 aux，每个调色板项只算一次"""
        cache = {}
        out = []
        append = out.append
        for (x, y, z, name, states) in blocks:
            if states:
                key = name + u"|" + u";".join([util.u(k) + u"=" + util.u(v)
                                              for k, v in sorted(states.items())])
            else:
                key = name
            if key in cache:
                aux = cache[key]
            else:
                aux = 0
                if states:
                    try:
                        aux = int(self._states().GetBlockAuxValueFromStates(name, states))
                    except Exception:
                        aux = 0
                cache[key] = aux
            append((x, y, z, name, aux, states))
        return out

    def _base(self, pid, size):
        px, py, pz = 0.0, 64.0, 0.0
        yaw = 0.0
        dim = 0
        try:
            pos = self.factory.CreatePos(pid).GetFootPos()
            px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        except Exception:
            util.log_exc("foot pos")
        try:
            # GetRot 返回 (俯仰, 朝向)，实测 SetRot((30,90)) -> (30.0, 90.0)
            yaw = float(self.factory.CreateRot(pid).GetRot()[1])
        except Exception:
            util.log_exc("rot")
        try:
            dim = int(self.factory.CreateDimension(pid).GetEntityDimensionId())
        except Exception:
            dim = 0

        rad = math.radians(yaw)
        fx = -math.sin(rad)
        fz = math.cos(rad)
        sx, sy, sz = size
        bx = int(math.floor(px + fx * cfg.FORWARD - sx / 2.0))
        bz = int(math.floor(pz + fz * cfg.FORWARD - sz / 2.0))
        by = int(math.floor(py))
        return (bx, by, bz), dim

    def _manual_fix_tick(self):
        # lang 方案失效时兜底：直接把名字写进物品 userData
        self._mfix_cd = getattr(self, "_mfix_cd", 0) - 1
        if self._mfix_cd > 0:
            return
        self._mfix_cd = 40
        try:
            players = serverApi.GetPlayerList()
        except Exception:
            return
        for pid in players or []:
            try:
                it = self.factory.CreateItem(pid)
                items = it.GetPlayerAllItems(0, True) or []
                changed = {}
                for i, x in enumerate(items):
                    if not x or (x.get("newItemName") or x.get("itemName")) != "si:manual":
                        continue
                    ud = x.get("userData") or {}
                    if ud.get("display") and ud["display"].get("Name"):
                        continue
                    nx = dict(x)
                    nx["userData"] = dict(ud)
                    nx["userData"]["display"] = {"Name": u"§e结构导入说明书§r\n§f右键查看教程"}
                    changed[(0, i)] = nx
                if changed:
                    it.SetPlayerAllItems(changed)
            except Exception:
                pass

    def on_tick(self, args=None):
        self._manual_fix_tick()
        job = self.job
        if job is None:
            if self.map_queue:
                self._give_maps()
            if self.replay_cmds:
                self._replay_cmds()
            return
        try:
            if job.paused:
                # 暂停时也保持 tip 心跳，让玩家知道任务还活着
                if job.tick % 20 == 0:
                    self._show_tip(job)
                return
            self._step(job)
        except Exception:
            tb = traceback.format_exc()
            util.log(u"tick 异常\n" + util.u(tb))
            self._clear_areas(job)
            self.job = None
            self.tp_route = None
            self.tell(job.pid, u"§c放置中断：§f" +
                      util.short(util.u(tb.strip().splitlines()[-1]), 54))
            self.push(job.pid, self._page)

    def _step(self, job):
        comp = self._blocks()
        if comp is None:
            self.job = None
            self.tell(job.pid, u"§c取不到方块组件（CreateBlockInfo 返回空）")
            self.push(job.pid, self._page)
            return

        job.tick += 1
        if job.waiting:
            if job.tick < job.wait_until:
                if job.tick % 10 == 0:
                    self._show_tip(job)      # 等区块时也刷进度
                return
            job.waiting = False
        job.work_ticks += 1
        if job.tick % 10 == 0 or job.tick == 1:
            self._show_tip(job)
        if job.phase == "blocks":
            self._step_blocks(job, comp)
        elif job.phase == "retry":
            self._step_retry(job, comp)
        elif job.phase == "wake":
            self._step_wake(job, comp)
        elif job.phase == "ents":
            self._step_ents(job, comp)
        elif job.phase == "mobs":
            self._step_mobs(job)
        else:
            job.phase = "done"

        if job.phase == "done":
            self._finish(job)

    def _place_one(self, comp, pos, name, aux, states, dim, update_neighbors):
        """写一个方块：优先用完整 states，拿不到就退回 aux

        返回是否成功——SetBlockNew 在未加载的区块会静默返回 False，
        不记下来就没法重试（大结构边缘区块的"小块导入失败"就是它）。
        """
        return self._place_block(comp, pos, {"name": name, "aux": int(aux)},
                                 states, dim, update_neighbors)

    def _place_block(self, comp, pos, block, states, dim, update_neighbors):
        """写一个方块（block 字典可复用：同一分区里同名同 aux 的方块共用一个）"""
        try:
            ok = comp.SetBlockNew(pos, block, 0, dim, True, bool(update_neighbors))
        except TypeError:
            ok = comp.SetBlockNew(pos, block, 0, dim)
        except Exception:
            return False
        if states:
            try:
                self._states().SetBlockStates(pos, states, dim)
            except Exception:
                pass
        return bool(ok)

    def _write_budget(self):
        """本 tick 允许花在写方块上的毫秒数

        预算是上限不是目标：写满就收手，把 tick 剩下的时间还给引擎渲染，
        这样一 tick 写多少完全由机器实测速度决定，帧率不会被导入拖崩。
        """
        b = self.budget
        if self.turbo:
            b = max(b, cfg.WRITE_BUDGET_MS_TURBO)
        return b

    def _tune(self, job, written, used_ms, hit_time):
        """按实测吞吐把下一 tick 的批量调到刚好贴着预算（自适应）"""
        if written <= 0 or used_ms <= 0.0:
            return
        rate = written / used_ms                       # 块 / 毫秒
        # 探针间隔按单块耗时收紧：保证"超预算"最多多写 1/8 个预算的量
        cost = used_ms / written
        if cost > 0:
            job.probe = max(1, min(64, int(self._write_budget() * 0.12 / cost)))
        target = int(rate * self._write_budget())
        cap = cfg.WRITE_BATCH_MAX * (2 if self.turbo else 1)
        if hit_time:
            # 撞到时间墙：压到目标值，宁可少写也不掉帧
            job.batch = max(cfg.WRITE_BATCH_MIN, min(job.batch, target))
        elif written >= job.batch:
            # 写满批量还有余量：试探上调（最多翻倍，防止一次冲太猛）
            job.batch = max(cfg.WRITE_BATCH_MIN,
                            min(cap, max(job.batch, min(target, job.batch * 2))))

    def _sec_tell(self, job, text):
        """分区级提示节流：几百个分区时只报里程碑，聊天框刷屏本身也掉帧"""
        n = len(job.sections)
        if n > 40:
            step = max(1, n // 10)
            if job.sec_i % step != 0 and job.sec_i != n - 1:
                return
        self.tell(job.pid, text)

    def _step_blocks(self, job, comp):
        sec = self._section(job)
        if sec is None:
            job.phase = "done"
            return
        idx = sec["blocks"]
        total = len(idx)
        written = 0
        bx, by, bz = job.base
        dim = job.dim
        blocks = job.blocks

        budget = self._write_budget()
        limit = max(cfg.WRITE_BATCH_MIN, job.batch)
        t0 = time.time()
        hit_time = False
        bdict = {}                         # (name, aux) -> 方块字典，本区复用

        while job.cursor < total and written < limit:
            i = idx[job.cursor]
            x, y, z, name, aux, states = blocks[i]
            job.cursor += 1
            written += 1
            try:
                key = (name, aux)
                blk = bdict.get(key)
                if blk is None:
                    blk = {"name": name, "aux": int(aux)}
                    bdict[key] = blk
                # 建造阶段不触发邻居更新：否则红石/火把会边放边被弹掉
                if self._place_block(comp, (bx + x, by + y, bz + z),
                                     blk, states, dim, False):
                    job.ok += 1
                else:
                    job.fail += 1
                    job.retry.append(i)      # 未加载区块导致的失败，稍后重试
                    if job.err is None:
                        job.err = u"方块写入返回失败（区块未加载？）"
            except Exception:
                job.fail += 1
                job.retry.append(i)
                if job.err is None:
                    job.err = util.u(traceback.format_exc().strip().splitlines()[-1])
            # 每写一小撮探一次时间：到点收手，本 tick 余下时间还给引擎
            if written % job.probe == 0 and \
                    (time.time() - t0) * 1000.0 >= budget:
                hit_time = True
                break

        self._tune(job, written, (time.time() - t0) * 1000.0, hit_time)
        if written:
            self.last_bpt = (written if self.last_bpt <= 0
                             else self.last_bpt * 0.85 + written * 0.15)

        if job.cursor < total:
            # 聊天框进度降到 3 秒一条（tip 仍是实时的），否则百来条消息本身掉帧
            if job.tick % 60 == 0:
                done = job.ok + job.fail
                pct = int(done * 100 / job.total) if job.total else 100
                self.tell(job.pid, u"§7放置中 %d%%（%d/%d）%s" %
                          (pct, done, job.total, self._sec_tag(job)))
            return

        # 分区内部顺序：方块 ->（失败重试）-> 红石唤醒 -> 方块实体
        # 唤醒阶段会整块重写红石方块，写之前就把实体数据铺好会被清掉
        if job.retry:
            job.phase = "retry"
            job.retry_cursor = 0
            job.retry_pass = 0
        elif sec["wake"]:
            job.phase = "wake"
            job.wake_cursor = 0
        elif sec["ents"]:
            job.phase = "ents"
            job.ent_cursor = 0
        else:
            self._next_section(job)

    def _step_retry(self, job, comp):
        """重试本分区里写入失败的方块（多为区块未加载；TP 后一 tick 就能写上）

        一轮重试后只要"还有失败且有进展"就再来一轮（最多 5 轮），
        彻底没进展才放弃进入下一阶段。
        """
        sec = self._section(job)
        n = 0
        bx, by, bz = job.base
        total = len(job.retry)
        still = []
        # 重试同样按时间预算收口：失败块重写也走 SetBlockNew，一 tick 塞太多照样卡
        budget = self._write_budget()
        limit = max(cfg.WRITE_BATCH_MIN, job.batch)
        t0 = time.time()
        while job.retry_cursor < total and n < limit:
            i = job.retry[job.retry_cursor]
            job.retry_cursor += 1
            n += 1
            x, y, z, name, aux, states = job.blocks[i]
            try:
                if self._place_one(comp, (bx + x, by + y, bz + z),
                                   name, aux, states, job.dim, False):
                    job.ok += 1
                    job.fail -= 1
                else:
                    still.append(i)
            except Exception:
                still.append(i)
            if n % cfg.WRITE_PROBE_STEP == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
        if job.retry_cursor < total:
            still.extend(job.retry[job.retry_cursor:])
        if still and len(still) < total and job.retry_pass < 5:
            # 有进展但没清零 -> 再来一轮（区块正在陆续加载）
            job.retry = still
            job.retry_cursor = 0
            job.retry_pass += 1
            return
        job.retry = still
        if sec is not None and sec["wake"]:
            job.phase = "wake"
            job.wake_cursor = 0
        elif sec is not None and sec["ents"]:
            job.phase = "ents"
            job.ent_cursor = 0
        else:
            self._next_section(job)

    def _step_ents(self, job, comp):
        """把方块实体数据写回去

        实测结论：
            1. 引擎只在叶子字段上认 {'__type__': 标签类型, '__value__': 值} 包装，
               compound / list 本身是普通 dict / list
            2. 容器里的 Items 写不进去（引擎忽略），必须走 SpawnItemToContainer
            3. x / y / z 是存档里的旧坐标，留着会把实体数据带歪，先剔除；id 必须保留
            4. **写实体数据会把命令方块重置成脉冲型**（repeating / chain 全变 impulse），
               所以要拿原方块核对一遍，变了就重放（重放不会丢刚写进去的命令）
        """
        sec = self._section(job)
        if sec is None:
            job.phase = "done"
            return
        idx = sec["ents"]
        total = len(idx)
        n = 0
        bx, by, bz = job.base
        dim = job.dim
        while job.ent_cursor < total and n < cfg.BATCH_ENT:
            x, y, z, nbt = job.entities[idx[job.ent_cursor]]
            job.ent_cursor += 1
            n += 1
            pos = (bx + x, by + y, bz + z)
            data = dict(nbt)
            for key in ("x", "y", "z"):
                data.pop(key, None)
            items = data.pop("Items", None)
            try:
                cur = comp.GetBlockEntityData(dim, pos)
                if isinstance(cur, dict):
                    payload = _merge_nbt(cur, data)
                else:
                    payload = data
                if payload:
                    comp.SetBlockEntityData(dim, pos, payload)
                self._restore_block(job, comp, (x, y, z), pos, dim)
                self._fix_command_block(job, (x, y, z), pos, dim, nbt)
                job.ent_ok += 1
            except Exception:
                job.ent_fail += 1
                if job.err is None:
                    job.err = u"方块实体写入失败：" + util.u(
                        traceback.format_exc().strip().splitlines()[-1])
            if items:
                job.item_ok += self._fill_items(items, pos, dim)

        if job.ent_cursor >= total:
            self._next_section(job)

    def _section(self, job):
        if job.sec_i >= len(job.sections):
            return None
        return job.sections[job.sec_i]

    def _sec_tag(self, job):
        n = len(job.sections)
        if n <= 1:
            return u""
        return u" §7· 分区 %d/%d" % (job.sec_i + 1, n)

    def _next_section(self, job):
        self._drop_area(job)                 # 上一区的常加载区块用完就还
        # 记录本区失败数：0 说明这台机器的区块跟得上，下一区可以少等一点；
        # 有失败说明等得不够，下一区自动加长（等于按机器实测调速）
        job.last_fail = job.fail - job.fail_mark
        job.fail_mark = job.fail
        job.ok_mark = job.ok
        job.sec_i += 1
        job.cursor = 0
        job.wake_cursor = 0
        job.ent_cursor = 0
        job.retry = []
        job.retry_cursor = 0
        if job.sec_i >= len(job.sections):
            # 所有分区跑完 -> 最后统一生成实体（矿车 / 船 / 生物 / 掉落物）
            job.phase = "mobs" if job.mobs else "done"
            return
        job.phase = "blocks"
        self._prepare_section(job)

    def _step_mobs(self, job):
        """生成结构里的实体

        实测 35/486 个结构带 entities（矿车 / 船 / 骆驼 / 猪灵 / 掉落物）。
        掉落物（minecraft:item）走 SpawnItemToLevel 把物品 NBT 完整带过去；
        其余用 /summon 指令（位置 + 默认 NBT，深 NBT 刷怪引擎没有公开入口）。
        """
        total = len(job.mobs)
        n = 0
        bx, by, bz = job.base
        cmdComp = None
        items = None
        while job.mob_cursor < total and n < cfg.BATCH_MOB:
            x, y, z, ident, nbt = job.mobs[job.mob_cursor]
            job.mob_cursor += 1
            n += 1
            try:
                if ident == u"minecraft:item":
                    # 掉落物：取 Item 字段还原成物品字典，直接生成到世界里
                    node = nbt.get("Item") or nbt.get("item") or {}
                    name = util.u(_val(node.get("Name")))
                    if not name:
                        job.mob_fail += 1
                        continue
                    count = int(_val(node.get("Count")) or 1)
                    aux = int(_val(node.get("Damage")) or 0)
                    if items is None:
                        items = self._items()
                    if items is None:
                        job.mob_fail += 1
                        continue
                    pos = (bx + x + 0.5, by + y + 0.5, bz + z + 0.5)
                    if items.SpawnItemToLevel(
                            {"itemName": name, "count": count, "auxValue": aux},
                            job.dim, pos):
                        job.mob_ok += 1
                    else:
                        job.mob_fail += 1
                    continue
                if nbt.get("RideID"):
                    # 是别的实体的乘客，先跳过（主人刷出来后再说，best-effort）
                    job.mob_fail += 1
                    continue
                if cmdComp is None:
                    cmdComp = self._command()
                if cmdComp is None:
                    job.mob_fail += 1
                    continue
                cmd = u"/summon %s %d.5 %d %d.5" % (ident, bx + x, by + y, bz + z)
                rot = nbt.get("Rotation")
                if isinstance(rot, (list, tuple)) and len(rot) >= 1:
                    try:
                        cmd += u" %d" % int(float(rot[0]))       # yaw
                    except (TypeError, ValueError):
                        pass
                if cmdComp.SetCommand(util.b(cmd)):
                    job.mob_ok += 1
                else:
                    job.mob_fail += 1
            except Exception:
                job.mob_fail += 1
                if job.err is None:
                    job.err = u"实体生成失败：" + util.short(util.u(ident), 24)

        if job.mob_cursor >= total:
            job.phase = "done"

    def _prepare_section(self, job):
        """进入分区前的准备：跟随 TP + 防缺失时挂常加载区块"""
        tpd = self._tp_to_section(job)
        if tpd:
            job.tp_wait = 1
        if job.tp_wait > 0:
            # 刚跟随 TP 过来：普通模式等 2 秒让区块加载完；
            # 急速模式只等 0.3 秒（给 TP 包处理时间），其余写不进的靠分区末尾
            # 自动重试兜底——这才是急速的本意，不能让强制等待把它盖掉
            job.waiting = True
            wait = cfg.TP_LOAD_WAIT_TURBO if self.turbo else cfg.TP_LOAD_WAIT
            # 竖排导入跨行时是远距离瞬移（如 1,45 -> 2,0），区块最易跟不上导致
            # 该分区大量写失败：强制降速等 4 秒再恢复原速
            big = tpd >= cfg.TP_BIG_JUMP
            if big:
                wait = max(1, int(self.big_wait * 20))
            elif self.turbo:
                wait = max(2, cfg.TP_LOAD_WAIT_TURBO // 2)   # 急速：0.1 秒就走
            elif job.last_fail == 0 and job.ok > 0:
                # 上一区一块都没失败 -> 区块跟得上，等待砍到 1/3
                wait = max(8, wait // 3)
            else:
                # 上一区有写不进的 -> 加长等待，比事后重试划算
                wait = min(70, int(wait * 1.5))
            job.wait_until = job.tick + wait
            job.tp_wait = 0
            if len(job.sections) > 1:
                if big:
                    self._sec_tell(job, u"§e分区 %d/%d：§7跨行远传（§f%d 格§7），降速等区块 §f%.1f 秒 §7后恢复" %
                                   (job.sec_i + 1, len(job.sections), int(tpd), wait / 20.0))
                else:
                    self._sec_tell(job, u"§7分区 %d/%d：%s等区块 §f%.1f 秒" %
                                   (job.sec_i + 1, len(job.sections),
                                    u"§f急速§7" if self.turbo else u"", wait / 20.0))
            return
        if not job.tick_enabled:
            return
        sec = self._section(job)
        if sec is None or not sec["blocks"]:
            return
        bx, by, bz = job.base
        xs = [job.blocks[i][0] for i in sec["blocks"]]
        ys = [job.blocks[i][1] for i in sec["blocks"]]
        zs = [job.blocks[i][2] for i in sec["blocks"]]
        name = u"si_%d_%d" % (job.sec_i, len(xs))
        cmd = u"/tickingarea add %d %d %d %d %d %d %s" % (
            bx + min(xs), by + min(ys), bz + min(zs),
            bx + max(xs), by + max(ys), bz + max(zs), name)
        cmdComp = self._command()
        ok = False
        if cmdComp:
            try:
                ok = bool(cmdComp.SetCommand(util.b(cmd)))
            except Exception:
                ok = False
        if ok:
            job.areas.append(name)
        job.waiting = True
        if self.turbo:
            # 急速模式：不按秒等，下一 tick 就开始（区块没跟上靠重试兜底）
            job.wait_until = job.tick + 1
            if len(job.sections) > 1:
                self._sec_tell(job, u"§7分区 %d/%d：§f急速§7（不等区块，写失败自动重试）" %
                               (job.sec_i + 1, len(job.sections)))
        else:
            job.wait_until = job.tick + max(20, job.speed_ticks)
            if len(job.sections) > 1:
                self._sec_tell(job, u"§7分区 %d/%d：常加载区块%s，等 §f%d 秒 §7后开始" %
                               (job.sec_i + 1, len(job.sections),
                                u"已添加" if ok else u"添加失败(继续尝试)", self.tick_speed))

    def _tp_to_section(self, job):
        """玩家跟着正在导入的分区自动传送

        巨型结构以玩家为中心居中放置，导入却从最远的角落开始——
        那边区块没加载，SetBlockNew 会静默失败（就是"开始区域小块导入失败"）。
        分区中心离玩家超过 TP_MIN_DIST 就把玩家 TP 过去，区块随即加载。
        """
        sec = self._section(job)
        if sec is None or not sec["blocks"] or not job.pid:
            return 0
        bx, by, bz = job.base
        xs = [job.blocks[i][0] for i in sec["blocks"]]
        ys = [job.blocks[i][1] for i in sec["blocks"]]
        zs = [job.blocks[i][2] for i in sec["blocks"]]
        cx = bx + (min(xs) + max(xs)) / 2.0
        cz = bz + (min(zs) + max(zs)) / 2.0
        cy = by + max(ys) + 6.0        # 站到分块上方面空处，别埋进方块里
        try:
            p = self.factory.CreatePos(job.pid).GetFootPos()
            dist = max(abs(p[0] - cx), abs(p[2] - cz))
        except Exception:
            return 0
        if dist < cfg.TP_MIN_DIST or not cfg.TP_FOLLOW:
            return 0
        try:
            if self.factory.CreatePos(job.pid).SetPos((cx, cy, cz)):
                if len(job.sections) > 1:
                    self.tell(job.pid, u"§7已跟随传送到分块 §f%s" %
                              util.short(util.u(sec.get("label") or u""), 16))
                return int(dist)
        except Exception:
            util.log_exc("tp to section")
        return 0

    def _drop_area(self, job):
        while job.areas:
            name = job.areas.pop()
            cmdComp = self._command()
            if cmdComp:
                try:
                    cmdComp.SetCommand(util.b(u"/tickingarea remove %s" % name))
                except Exception:
                    pass

    def _clear_areas(self, job):
        self._drop_area(job)

    def _restore_block(self, job, comp, offset, pos, dim):
        """写完实体数据后核对方块有没有被引擎换掉，变了就重放

        命令方块最典型：`SetBlockEntityData` 会按 tile entity 的默认变体重建方块，
        循环 / 连锁统统退化成脉冲型。重放回原方块后命令内容仍在（实测）。
        """
        want = job.blk_at.get(offset)
        if not want:
            return
        try:
            now = comp.GetBlockNew(pos, dim)
            name = (now or {}).get("name")
        except Exception:
            return
        if not name or name == want[0]:
            return
        try:
            self._place_one(comp, pos, want[0], want[1], want[2], dim, False)
            job.ent_fixed += 1
        except Exception:
            pass

    def _replay_cmds(self):
        """代理执行导入结构里的循环展示命令（每秒一次）"""
        self._replay_cd -= 1
        if self._replay_cd > 0:
            return
        self._replay_cd = 40
        cmd = self._command()
        if not cmd:
            return
        for c in self.replay_cmds:
            try:
                cmd.SetCommand(util.b(c if c.startswith(u"/") else u"/" + c))
            except Exception:
                pass

    def _fix_command_block(self, job, offset, pos, dim, nbt):
        """把命令方块的 类型 / 条件约束 / 红石控制 设置回结构里的原样

        实测 SetBlockEntityData 会把命令方块重建为默认模板：变体变脉冲、
        auto 强制 1（保持开启），auto 字段写了也不生效。好在引擎有专门的
        SetCommandBlock(pos, dim, cmd, name, mode, isConditional, redstoneMode)，
        实测它不破坏方块变体，三个设置项都能正确落盘。
        """
        want = job.blk_at.get(offset)
        if not want:
            return
        mode = CMD_BLOCK_MODE.get(want[0])
        be = self._blockEnt()
        if mode is None or be is None:
            return
        cmd = util.u(_val(nbt.get("Command")))
        name = util.u(_val(nbt.get("CustomName")))
        cond = _val(nbt.get("conditionalMode"))
        if cond is None:                       # 没有字段就按方块状态推
            states = want[2] or {}
            cond = 1 if states.get("conditional_bit") in (True, 1) else 0
        try:
            cond = 1 if int(cond) else 0
        except (TypeError, ValueError):
            cond = 0
        auto = _val(nbt.get("auto"))
        # NBT: auto=1 保持开启；枚举: KEEP_ON=0 / REDSTONE_CONTROL=1
        rmode = 0 if auto in (1, True) else 1
        try:
            if be.SetCommandBlock(pos, dim, cmd, name, mode, cond, rmode):
                job.cmd_fixed += 1
        except Exception:
            pass
        # 实测部分版本导入后循环命令方块不自动执行（设置读回都对也不跑）。
        # 换思路：展示类命令（tellraw/title/say/me，无副作用）由 mod 每秒
        # 代理执行一次，效果等同循环+保持开启；其余命令仍留在方块里走红石。
        low = cmd.strip().lower()
        if (mode == CMD_BLOCK_MODE.get("minecraft:repeating_command_block")
                and rmode == 0 and low.split(" ")[0].lstrip("/") in
                ("tellraw", "title", "say", "me") and cmd not in self.replay_cmds):
            self.replay_cmds.append(cmd)

    def _fill_items(self, node, pos, dim):
        """箱子 / 漏斗 / 木桶 / 潜影盒 / 发射器 里的物品"""
        comp = self._items()
        if comp is None:
            return 0
        entries = node if isinstance(node, list) else _val(node)
        if not isinstance(entries, list):
            return 0
        ok = 0
        for entry in entries:
            item = _item_dict(entry)
            if not item:
                continue
            try:
                if comp.SpawnItemToContainer(item["dict"], item["slot"], pos, dim):
                    ok += 1
            except Exception:
                pass
        return ok

    def _step_wake(self, job, comp):
        """红石那批方块补一次带更新的写入，让它们重新认邻居"""
        sec = self._section(job)
        if sec is None:
            job.phase = "done"
            return
        n = 0
        idx = sec["wake"]
        total = len(idx)
        bx, by, bz = job.base
        # 这一阶段每个方块都要触发邻居更新，单块开销是普通写入的数倍：
        # 只给半个预算，避免红石密集区一 tick 顶死主线程
        budget = self._write_budget() * 0.5
        limit = max(cfg.WRITE_BATCH_MIN, job.batch // 2)
        t0 = time.time()
        while job.wake_cursor < total and n < limit:
            x, y, z, name, aux, states = job.blocks[idx[job.wake_cursor]]
            job.wake_cursor += 1
            n += 1
            try:
                self._place_one(comp, (bx + x, by + y, bz + z),
                                name, aux, states, job.dim, True)
            except Exception:
                pass
            if n % 8 == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
        if job.wake_cursor >= total:
            if sec["ents"]:
                job.phase = "ents"
                job.ent_cursor = 0
            else:
                self._next_section(job)

    def _finish(self, job):
        self.job = None
        self.tp_route = None
        self._clear_areas(job)
        if job.ok == 0:
            self.tell(job.pid, u"§c放置失败：%d 个方块全部没写进去" % job.fail)
            if job.err:
                self.tell(job.pid, u"§7原因：§f" + job.err)
            self.tell(job.pid, u"§7先跑 §f/structureim test §7看单块写入是否正常")
        else:
            msg = u"§a放置完成：§f%s §7(%d 块" % (util.short(job.title, 28), job.ok)
            if job.fail:
                msg += u" / %d 失败" % job.fail
            msg += u"，%.1f 秒)" % (job.tick / 20.0)
            if job.entities:
                msg += u" §7· 方块实体 §f%d" % job.ent_ok
                if job.ent_fail:
                    msg += u" §c(%d 失败)" % job.ent_fail
                if job.item_ok:
                    msg += u" §7· 物品 §f%d" % job.item_ok
                if job.ent_fixed:
                    msg += u" §7· 校正方块 §f%d" % job.ent_fixed
                if job.cmd_fixed:
                    msg += u" §7· 命令方块设置 §f%d" % job.cmd_fixed
            if job.mobs:
                msg += u" §7· 实体 §f%d" % job.mob_ok
                if job.mob_fail:
                    msg += u" §c(%d 失败)" % job.mob_fail
            self.tell(job.pid, msg)
            self.tell(job.pid, u"§7基准位置：§f%d, %d, %d" % job.base)
            if self.map_mode:
                self._queue_maps(job)
        self.push(job.pid, self._page)

    # ------------------------------------------------------------ 客户端来的请求
    def on_req(self, args):
        try:
            pid = args.get("pid")
            if not pid:
                return
            if args.get("split") is not None:
                # 界面上的开关：直接给目标状态，不做取反，避免连点错乱
                self.split = bool(args.get("split"))
                self._save()
            if args.get("tick") is not None:
                self.tick_mode = bool(args.get("tick"))
                if self.tick_mode and not self.split:
                    # 防缺失依赖分区，自动把分区模式一起打开
                    self.split = True
                self._save()
            if args.get("chunk") is not None:
                self.chunk = bool(args.get("chunk"))
                self._save()
            if args.get("map") is not None:
                self.map_mode = bool(args.get("map"))
                self._save()
            if args.get("turbo") is not None:
                self.turbo = bool(args.get("turbo"))
                self._save()
            if args.get("speed") is not None:
                try:
                    speed = int(util.u(args.get("speed")))
                except (TypeError, ValueError):
                    speed = cfg.SPEED_DEFAULT
                self.tick_speed = max(cfg.SPEED_MIN, min(cfg.SPEED_MAX, speed))
                self._save()
            root = util.norm_path(args.get("root") or u"")
            if root and root != self.library.root:
                if util.is_dir(root):
                    self.library.set_root(root)
                    self._save()
                else:
                    self.library.err = u"目录不存在：" + root
                    self.library.entries = []
            elif args.get("rescan"):
                self.library.rescan()
            self.push(pid, int(args.get("page") or 0))
        except Exception:
            util.log_exc("req")

    def on_place(self, args):
        try:
            pid = args.get("pid")
            which = args.get("which") or u""
            rot = args.get("rot") or 0
            if pid:
                self.cmd_place(pid, which, rot)
        except Exception:
            util.log_exc("place req")

    def push(self, pid, page=0):
        if not pid:
            return
        self._page = page
        lib = self.library
        total = len(lib.entries)
        pages = max(1, (total + cfg.ROWS - 1) // cfg.ROWS) if total else 1
        page = max(0, min(int(page or 0), pages - 1))
        start = page * cfg.ROWS
        rows = []
        for i in range(start, min(start + cfg.ROWS, total)):
            e = lib.entries[i]
            rows.append([i + 1, e.name, e.rel])
        data = {
            "dir": lib.root,
            "err": lib.err,
            "total": total,
            "page": page,
            "pages": pages,
            "rows": rows,
            "busy": self.job is not None,
            "chunk": bool(self.chunk),
            "map": bool(self.map_mode),
            "turbo": bool(self.turbo),
            "split": bool(self.split),
            "tick": bool(self.tick_mode),
            "speed": int(self.tick_speed),
        }
        try:
            self.NotifyToClient(pid, cfg.EV_DATA, data)
        except Exception:
            util.log_exc("push")
