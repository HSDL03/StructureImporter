# -*- coding: utf-8 -*-
"""服务端：自定义指令、目录扫描、分批放置"""

import math
import os
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
    _STR = basestring
except NameError:
    _PY2 = False
    _range = range
    _STR = str

def _val(node, default=None):
    """取 typed 节点里的裸值；不是 typed 节点就原样返回"""
    if isinstance(node, dict) and "__type__" in node:
        return node.get("__value__", default)
    return default if node is None else node


def _norm_states(st):
    """把结构里的方块状态整理成引擎能吃的样子

    - 结构文件里 state 键有的带 minecraft: 前缀（第三方工具导出的），引擎只认裸名
    - 布尔值引擎要 0/1（`waterlogged: true` 直接喂会当成无效状态被忽略）
    认不出来的键原样留着，宁可多传也不丢。
    """
    if not st:
        return st
    out = None
    for k, v in st.items():
        nk = k[10:] if isinstance(k, _STR) and k.startswith("minecraft:") else k
        nv = v
        if isinstance(v, bool):
            nv = 1 if v else 0
        if nk != k or nv is not v:
            if out is None:
                out = dict(st)
            out.pop(k, None)
            out[nk] = nv
    return out if out is not None else st


def _native(v):
    """py2 交给引擎的字符串一律转 UTF-8 字节串；py3（离线校验）保持原样"""
    return util.b(v) if _PY2 else v


def _native_deep(node):
    """递归把 unicode 键和字符串值转成 UTF-8 字节串（py2 专用）

    这是物品 NBT 丢失的真正原因：引擎收下 itemDict 后会把 userData 里的
    unicode 字符串整条丢掉。实测同一个 {'display': {'Name': u'X'}} 写进去，
    回读 userData 里没有 display；换成 str 版本 'X' 就完整保留。
    """
    if not _PY2:
        return node
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            out[util.b(k) if isinstance(k, _STR) else k] = _native_deep(v)
        return out
    if isinstance(node, list):
        return [_native_deep(v) for v in node]
    if isinstance(node, _STR):
        return util.b(node)
    return node


def _eng_states(st):
    """states 交给引擎前转成 C++ 层能吃的样子

    实测：states 的 key 是 unicode 时会刷 "PyDict's key is not string"，
    每个带状态的方块一条，几十万块就是几十万条，引擎跟着就不稳。
    """
    if not st or not _PY2:
        return st
    out = {}
    for k, v in st.items():
        if isinstance(v, _STR):
            v = util.b(v)
        out[util.b(k)] = v
    return out


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


def _typed_to_plain(node):
    """{'__type__','__value__'} 包装树 -> 普通 python 值（引擎 userData 格式）"""
    if isinstance(node, dict):
        if len(node) == 2 and "__type__" in node and "__value__" in node:
            v = node["__value__"]
            if isinstance(v, list):
                return [_typed_to_plain(i) for i in v]
            return v
        return dict((k, _typed_to_plain(v)) for k, v in node.items())
    if isinstance(node, list):
        return [_typed_to_plain(v) for v in node]
    return node


def _nbt_leaf_size(v):
    """估算一个叶子值的字节量"""
    if isinstance(v, (bytes, bytearray)):
        return len(v) + 8
    try:
        if isinstance(v, basestring if _PY2 else str):
            return len(v) + 8
    except Exception:
        pass
    if isinstance(v, (list, tuple)):
        return 8 * len(v) + 8
    return 8


def _nbt_heavy(node, max_kb=None, max_depth=None):
    """物品 NBT 是否过大/嵌套过深（早停，别为了判断把 2MB 的盒子整个走一遍）

    「制裁盒子」这类结构是潜影盒套潜影盒，深度几十层、单件物品 2MB。
    原样交给引擎要么物品丢失，要么直接把主线程顶死 —— 所以先量一下。
    """
    if max_kb is None:
        max_kb = cfg.ITEM_NBT_MAX_KB
    if max_depth is None:
        max_depth = cfg.ITEM_NBT_MAX_DEPTH
    if not max_kb or max_kb <= 0:
        return False
    limit = max_kb * 1024
    budget = [cfg.ITEM_NBT_SCAN_NODES]
    size = [0]
    heavy = [False]

    def walk(n, depth):
        if heavy[0] or budget[0] <= 0:
            return
        budget[0] -= 1
        if depth > max_depth:
            heavy[0] = True
            return
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "__type__":
                    continue
                if k == "__value__":
                    size[0] += _nbt_leaf_size(v)
                    if size[0] > limit:
                        heavy[0] = True
                        return
                    continue
                size[0] += len(k) + 4
                walk(v, depth + 1)
                if heavy[0]:
                    return
        elif isinstance(n, list):
            size[0] += 4
            for v in n:
                walk(v, depth + 1)
                if heavy[0]:
                    return

    try:
        walk(node, 0)
    except Exception:
        return True
    return heavy[0] or budget[0] <= 0


def _item_dict(entry):
    """mcstructure 物品 NBT -> SpawnItemToContainer 认的物品字典

    产出两份 userData，写入时按顺序试：
        plain —— 纯 python 值（附魔是 [{'id':9,'lvl':3}] 这种），引擎文档里
                 itemDict.userData 就是这个形态，命中率最高
        typed —— 叶子带 {'__type__','__value__'} 包装，SetBlockEntityData 那条路
                 认这个，万一引擎版本换了也能兜住

    以前只做一份（typed）并且"失败就算完"，实测制裁盒子这种
    「附魔 + 自定义名字 + 盒中盒」的结构导进去后 NBT 全没了：
    引擎把 itemDict 收下了、userData 却整个吞掉，代码还以为成功。
    """
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
    # 数量和堆叠上限对不上引擎会直接拒收，先夹住
    if count > 64:
        count = 64
    base = {"itemName": _native(name), "count": count, "auxValue": aux}
    tag = _val(node.get("tag")) or _val(node.get("ItemTag"))
    plain, typed, trim = None, None, False
    if isinstance(tag, dict) and tag:
        if _nbt_heavy(tag):
            # 真的超深/超大：只把"看得见"的部分留下，别整件丢
            tag = _slim_item_tag(tag)
            trim = True
        if tag:
            p = dict(base)
            t = dict(base)
            # typed 版本 = 结构里的 NBT 原样（只做 str 化和 compound 展平）。
            # 这是引擎自己的存档形态，保真度最高：盒中盒、多层嵌套、32767 级附魔、
            # 自定义名字全都原样落在容器里。
            t["userData"] = _nbt_for_engine(_norm_ench(tag))
            # plain 版本（纯 python 值）留作兜底，某些版本只认这种
            p_tag = _typed_to_plain(tag)
            if isinstance(p_tag, dict):
                p["userData"] = _native_deep(p_tag)
                ench = _item_enchants(tag)
                if ench:
                    p["enchantData"] = ench
            plain, typed = p, t
    if plain is None:
        plain = dict(base)
    return {"plain": plain, "typed": typed, "slot": slot, "trim": trim}


def _nbt_for_engine(node):
    """把结构里的 NBT 树整理成引擎 userData 能原样保存的形态

    真机实测的三条规矩（都是"整条被丢"这种静默失败，不问就查不出来）：
      1. 字符串必须是 str（UTF-8 字节串）；unicode 会让整个 userData 变 None
      2. compound(type 10) / list(type 9) 不能再套 {'__type__','__value__'} ——
         引擎只认叶子包装，套了就整份 userData 被丢
      3. 叶子保持原有的 __type__ 包装（short/int/long 各有含义，不能统一成 int）
    """
    if isinstance(node, dict):
        if "__type__" in node and "__value__" in node:
            try:
                t = int(node["__type__"])
            except (TypeError, ValueError):
                t = None
            if t in (9, 10):                 # list / compound：引擎要裸结构
                return _nbt_for_engine(node["__value__"])
            if t is not None:
                # 键也走 _native：py2 下必须是 str，不能靠字面量碰运气
                return {_native("__type__"): t,
                        _native("__value__"): _nbt_for_engine(node["__value__"])}
        out = {}
        for k, v in node.items():
            out[_native(k) if isinstance(k, _STR) else k] = _nbt_for_engine(v)
        return out
    if isinstance(node, list):
        return [_nbt_for_engine(v) for v in node]
    if isinstance(node, _STR):
        return _native(node)
    return node


def _norm_ench(tag):
    """把附魔统一成 engines 唯一认得的形态：[{'id': short, 'lvl': short}]

    实测（真机，钻石头盔写进潜影盒）：
      - userData.ench 用 {'__type__': 2} 包装 -> 成功，32767 级也照样写进去
      - userData.ench 用普通 int             -> 存成 TAG_Int，回读变成 (0, 0)
      - 顶层 enchantData                     -> 同一件物品直接 spawn item failed!
    所以这里统一转 short 包装，并把 Enchantments（Java 写法）并到 ench。
    """
    if not isinstance(tag, dict):
        return tag
    pairs = []
    for key in ("ench", "Enchantments"):
        lst = tag.get(key)
        if not isinstance(lst, list):
            continue
        for node in lst:
            if not isinstance(node, dict):
                continue
            try:
                eid = int(_val(node.get("id")) or 0)
                lvl = int(_val(node.get("lvl")) or _val(node.get("level")) or 0)
            except (TypeError, ValueError):
                continue
            if eid and lvl:
                pairs.append((eid, lvl))
    if not pairs:
        return tag
    out = dict(tag)
    out.pop("Enchantments", None)
    out["ench"] = [
        {"id": {"__type__": 2, "__value__": max(-32768, min(32767, i))},
         "lvl": {"__type__": 2, "__value__": max(-32768, min(32767, l))}}
        for i, l in pairs]
    return out


def _item_enchants(tag):
    """从物品 NBT 里抽出附魔 -> [(id, lvl)]

    兼容两种写法：ench（Bedrock/本结构的实际形态）和 Enchantments（Java 风格）。
    取不出来的直接跳过，绝不让一条坏数据把整件物品带崩。
    """
    out = []
    for key in ("ench", "Enchantments"):
        lst = tag.get(key)
        if not isinstance(lst, list):
            continue
        for node in lst:
            try:
                if not isinstance(node, dict):
                    continue
                eid = int(_val(node.get("id")) or 0)
                lvl = int(_val(node.get("lvl")) or _val(node.get("level")) or 0)
            except (TypeError, ValueError):
                continue
            if eid and lvl > 0:
                out.append((eid, lvl))
    return out


# 物品 NBT 里"玩家看得见"的字段：NBT 大到必须裁剪时只留这些
ITEM_KEEP_KEYS = (
    "display",          # 名字 + Lore
    "CustomName",       # 铁砧改名 / 实体名字
    "ench",             # 附魔（Java 风格存档）
    "Enchantments",     # 附魔（Bedrock 风格存档）
    "RepairCost",
    "Unbreakable",
    "Damage",
    "HideFlags",
    "AttributeModifiers",
    "CanDestroy",
    "CanPlaceOn",
    "StoredEnchantments",
    "minecraft:keep_on_death",
)


def _slim_item_tag(tag):
    """只留名称/附魔/耐久这类关键字段，其余（盒中盒、交易表、蜂巢）丢掉"""
    out = {}
    for k in ITEM_KEEP_KEYS:
        if k in tag:
            out[k] = tag[k]
    # 盒中盒的 Items 是最占地方的，但玩家确实可能想看，浅一层留着
    items = tag.get("Block") or tag.get("Items")
    if isinstance(items, dict) and len(out) < 6:
        out["Items"] = items
    return out


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
    u"§f/structureim map <开|关> §7地图画模式（导完搭墙画图，关掉能省一大截时间）",
    u"§f/structureim zwait <1-30> §7巡图每区停留秒数（默认3）",
    u"§f/structureim znum <1-9> §7每幅图巡几个区（默认3，实测3区即画满）",
    u"§f/structureim mapspeed <0-2> §7地图画速度：0稳(默认) 1快 2极速",
    u"§f/structureim maparea <square|circle> §7地图常加载形状（方形默认；报命令错就切 circle）",
    u"§f/structureim bigwait <1-30> §7跨行远传后降速秒数（默认4）",
    u"§f/structureim budget <2-25> §7每tick写入预算ms（默认12，越大越快越卡；急速自动翻倍）",
    u"§f/structureim fast §7一键提速（后台解析 + 邻区并行 + 急速 + 满预算）",
    u"§f/structureim preset <稳|均衡|极速> §7一键套用整套参数（128图120秒选极速）",
    u"§f/structureim verify <开|关> §7导完逐块回读，只报差多少块（不补写，默认关）",
    u"§f/structureim splitz <0|16-128> §7分区边长：0=自动，16-128=自定义",
    u"§f/structureim ta <开|关> §7纯常加载：整张结构挂 tickingarea，人不跟着 TP",
    u"§f/structureim tac §7清理本插件残留的常加载区（只删 si_ 开头，不动指令区）",
    u"§f/structureim turbo <开|关> §7急速模式（等区块 3 tick，大结构提速主力）",
    u"§f/structureim threads <1-8> §7解析方式（1=主线程分帧，>=2=后台线程，两者都不卡）",
    u"§f/structureim parallel <1-4> §7相邻分区并行（提前加载邻区，默认1=关）",
    u"§f/structureim nbtlimit <0|8-1024> §7物品NBT上限KB，0=不裁剪（默认）",
    u"§f/structureim freeze <开|关> §7导入期间冻结时间流逝（水不乱跑，默认开）",
    u"§f/structureim freezetime <0-60> §7放完液体后再守几秒（默认6）",
    u"§f/structureim selftest §7环境自检",
    u"§f/structureim manual §7看分步教程（也可右键使用说明书物品）",
]


def _bg_load(path, name, box):
    """后台线程里跑的结构解析（分片推进，每片之间让出 GIL）

    只做「读文件 + 解压 + NBT 遍历」，**绝不调用任何引擎接口**
    （SetBlockNew 之类必须在主线程，跨线程调会崩）。
    结果/异常统一塞进 box，由主线程在 tick 里取走。

    为什么还要分片 sleep：纯 Python 的解析循环一直转就会一直攥着 GIL，
    引擎线程拿不到时间片，游戏照样一顿一顿的（甚至像卡死）。
    每片之间 sleep 一下，引擎就有机会跑完这一帧。
    """
    try:
        dec = mcst.read_decompress(path)
        out = {}
        gen = mcst.iter_load_prepared(dec, name, cfg.SKIP_AIR, cfg.SKIP_VOID,
                                       chunk=cfg.BG_PARSE_CHUNK, out=out)
        while True:
            try:
                done, total = next(gen)
            except StopIteration:
                break
            box["done"] = done
            box["total"] = total
            time.sleep(cfg.BG_PARSE_SLEEP)     # 让出 GIL，引擎照常出帧
        data = out.get("data")
        if data is None:
            raise ValueError("解析未产出结构")
        box["data"] = data
    except Exception:
        box["err"] = traceback.format_exc()


class BlockTable(object):
    """方块表：把 (x,y,z,name,aux,states) 从一百多万个元组压成 5 个整型数组

    内存账（象牙城堡 115 万块，实测）：
        元组列表  115万 × (96B 元组 + 3 个新 int × 28B) ≈ 200MB
        本表      115万 × 16B(四个 array) + 调色板几百项 ≈ 20MB
    少掉一百多万个容器对象，分代回收的扫描量和耗时跟着掉一个量级
    （GC 尖峰正是 D 项里那个 191ms 单帧的主要嫌疑）。

    对外只提供 blocks[i] -> 6 元组，长度与切片下标语义不变，
    所以写方块、分区、校验这些路径一行都不用改。
    只读：压缩在 _step_prep 阶段 0 边扫边建，之后不会再改。
    """

    __slots__ = ("xs", "ys", "zs", "pid", "pal", "n")

    def __init__(self, xs, ys, zs, pid, pal, n):
        self.xs = xs
        self.ys = ys
        self.zs = zs
        self.pid = pid
        self.pal = pal
        self.n = n

    def __len__(self):
        return self.n

    def __getitem__(self, i):
        p = self.pal[self.pid[i]]
        return (self.xs[i], self.ys[i], self.zs[i], p[0], p[1], p[2])


def _new_table_parts():
    """建表用的五个容器（py2 的 array 存整数，4 字节/项）"""
    from array import array
    return {"xs": array("i"), "ys": array("i"), "zs": array("i"),
            "pid": array("i"), "pal": [], "pmap": {}}


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
        self.side = cfg.SPLIT_SIZE      # 本次分区的实际边长（_pick_split_size 会覆盖）
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
        self.item_fail = 0
        self.ent_fixed = 0
        self.cmd_fixed = 0
        self.item_trim = 0      # 因 NBT 过大/过深被裁掉数据的物品数
        self.item_bare = 0      # 引擎拒收 NBT、只放下物品本体的件数
        self.item_queue = []    # 待写入容器的物品 [(plainDict, typedDict, slot, pos)]
        self.item_cursor = 0    # 队列游标（用索引推进，不做 pop(0)）
        self.pb_ms = 0.0        # 调色板单块（16³）实测耗时的滑动平均，用来卡预算
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
        self.ta_ok = False       # 本任务真的挂上常加载了才不跟随 TP
        # 暂停 / 速度统计
        self.paused = False
        self.work_ticks = 0      # 实际干活的 tick 数（暂停和等区块不算），算速度用
        self.parse_secs = 0.0    # 解析这一步花了多少秒（收尾时拆账用）
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
        # 相邻分区并行：提前挂常加载区块的记录 [(分区序号, area 名), ...]
        self.pf = []
        # 液体（水/岩浆）：不进分区，全部固体写完后再统一放，放完再守几秒不乱流
        self.liquid_idx = []
        self.liq_cursor = 0
        self.liq_ok = 0
        self.liq_fail = 0
        self.sealed = []          # 已放下的液体 [(pos, name, aux, states)]，守护阶段回写
        self.seal_cursor = 0
        self.seal_until = 0       # 守护到第几 tick（0 = 不守）
        self.seal_pass = 0
        self.seal_fixes = 0       # 守护阶段实际补写了多少格（用来判断"水是不是真在流"）
        self.wash_idx = []        # 怕水的方块（火把/铁轨/告示牌/花…），液体后核对一遍
        self.wash_cursor = 0
        self.wash_fixed = 0
        self.liq_cache = {}       # (name, aux, states) -> 方块字典：几万格水只留几份
        self.freeze = True        # 本次任务是否冻结时间流逝
        self.liq_armed = False    # 放液体前是否已经把人送回结构中心
        self.note = u""           # 临时状态（原分区提示），只显示在 tip 上，不进聊天框


class SiServerSystem(serverApi.GetServerSystemCls()):

    def __init__(self, namespace, systemName):
        super(SiServerSystem, self).__init__(namespace, systemName)
        self.factory = serverApi.GetEngineCompFactory()
        self.levelId = serverApi.GetLevelId()
        self.library = scan.Library()
        self.job = None
        self.split = cfg.SPLIT_DEFAULT
        self.split_size = 0                    # 分区边长：0=自动（按体量自适应），>0=自定义格数
        self.tick_mode = cfg.TICK_DEFAULT
        self.tick_speed = cfg.SPEED_DEFAULT
        self.chunk = cfg.CHUNK_DEFAULT
        self.map_mode = cfg.MAP_DEFAULT
        self.turbo = cfg.TURBO_DEFAULT
        self.zone_secs = cfg.TP_ZONE_SECS   # 巡图每区停留秒数（可 /structureim zwait 调）
        self.zones_n = 3                    # 每幅图实际巡几个区（1~9，可 /structureim znum 调）
        self.big_wait = cfg.TP_BIG_WAIT / 20.0   # 跨行远传后降速秒数（/structureim bigwait 调）
        self.budget = cfg.WRITE_BUDGET_MS        # 每 tick 写方块的时间预算（ms，/structureim budget 调）
        self.threads = cfg.THREADS_DEFAULT       # 多线程：>=2 时结构解析走后台线程
        self.parallel = cfg.PARALLEL_DEFAULT     # 相邻分区并行：提前加载几个邻区
        self.nbt_limit = cfg.ITEM_NBT_MAX_KB     # 物品 NBT 上限（0=不裁剪）
        self._bg = None                          # 正在后台解析的任务
        self._pj = None                          # 正在主线程分帧解析的任务（不卡版）
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
        self._paletteInfo = None
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

    def _pblocks(self):
        """调色板组件（CreateBlock），批量写方块要用它，和 CreateBlockInfo 不是同一个

        这里不用 self.factory：热重载之后引擎重挂的 system 实例可能没走 __init__，
        self.factory 会缺；直接问 serverApi 拿更保险。
        """
        comp = getattr(self, "_paletteInfo", None)
        if comp is None:
            try:
                lid = getattr(self, "levelId", None) or serverApi.GetLevelId()
                comp = serverApi.GetEngineCompFactory().CreateBlock(lid)
            except Exception:
                util.log_exc("CreateBlock")
                comp = False
            self._paletteInfo = comp
        return comp or None

    def _pb_ready(self):
        """引擎是否提供调色板批量写（老版本没有这两支接口，得退回逐块）"""
        comp = self._pblocks()
        if comp is None:
            return False
        return bool(getattr(comp, "GetBlankBlockPalette", None) and
                    getattr(comp, "SetBlockByBlockPalette", None))

    @staticmethod
    def _pb_states(states):
        """mcstructure 的 states -> 调色板的 states

        键必须转 str：引擎只认 str，unicode 键会被静默忽略。树叶的
        persistent_bit 一丢整片树叶就会枯掉，这是实测踩到的坑，别改回去。
        """
        if not states:
            return {}
        out = {}
        for k, v in states.items():
            out[str(k)] = v
        return out

    def _pb_build(self, idx, blocks, st=None, budget_ms=None):
        """把分区内的块按 PALETTE_TILE³ 分桶，返回 (tiles, flat)

        tiles 每项 (起, 止, ox, oy, oz)，flat 是重排后的块下标。
        按 y 升序排（低层先写），上层方块的支撑先到位。

        传 st 就是**分帧**模式：分桶做到一半就返回 None，进度记在 st 里，
        下一帧接着做。16³ 的区有 1.6 万块，一次性跑完在 py2 下要 30ms，
        正好卡在"进入分区"那一帧上（t_pal 测出来的 39ms 尖峰）。
        """
        t = int(cfg.PALETTE_TILE)
        buckets = {}
        if st is None:
            for i in idx:
                b = blocks[i]
                k = (b[0] // t, b[1] // t, b[2] // t)
                lst = buckets.get(k)
                if lst is None:
                    buckets[k] = [i]
                else:
                    lst.append(i)
        else:
            buckets = st["buckets"]
            i = st["i"]
            total = len(idx)
            t0 = time.time()
            while i < total:
                k = blocks[idx[i]]
                key = (k[0] // t, k[1] // t, k[2] // t)
                lst = buckets.get(key)
                if lst is None:
                    buckets[key] = [idx[i]]
                else:
                    lst.append(idx[i])
                i += 1
                if (i & 255) == 0 and budget_ms and \
                        (time.time() - t0) * 1000.0 >= budget_ms:
                    break
            st["i"] = i
            if i < total:
                return None                     # 还没分完，下一帧继续
        tiles = []
        flat = []
        for k in sorted(buckets, key=lambda q: (q[1], q[0], q[2])):
            lst = buckets[k]
            tiles.append((len(flat), len(flat) + len(lst),
                          k[0] * t, k[1] * t, k[2] * t))
            flat.extend(lst)
        return tiles, flat

    def _pb_write(self, job, comp, blocks, flat, st, en, ox, oy, oz):
        """把一个小块的方块一次写进世界

        三个必须踩对的点（全部实测确认，改之前先看这里）：
        - volume 的顺序是 (Z, X, Y)，不是 (X, Y, Z)。传反了正方体看不出来，
          非正方体结构会整块错位（曾经表现为"树叶/雪层凭空消失"）
        - common 下标 = z + x*Z + y*X*Z，z 变化最快 —— 和 mcstructure 的
          x + z*X 正好互为转置
        - 方块名和 states 键都必须是 str；unicode 让接口静默返回 False
        """
        t = int(cfg.PALETTE_TILE)
        common = {}
        states = {}
        for i in range(st, en):
            b = blocks[flat[i]]
            idx = (b[2] - oz) + (b[0] - ox) * t + (b[1] - oy) * t * t
            key = (str(b[3]), int(b[4]))
            lst = common.get(key)
            if lst is None:
                common[key] = [idx]
                states[key] = self._pb_states(b[5])
            else:
                lst.append(idx)
        data = {"states": states, "volume": (t, t, t), "extraStates": {},
                "common": common, "extra": {}, "void": False,
                "eliminateAir": True, "actor": {}}
        try:
            pal = comp.GetBlankBlockPalette()
            if pal is None:
                return False
            if not pal.DeserializeBlockPalette(data):
                return False
            return bool(comp.SetBlockByBlockPalette(
                pal, job.dim,
                (job.base[0] + ox, job.base[1] + oy, job.base[2] + oz), 0, 0))
        except Exception:
            if job.err is None:
                job.err = util.u(traceback.format_exc().strip().splitlines()[-1])
            return False

    def _step_blocks_palette(self, job, sec):
        """批量写当前分区的方块：按小块摊到多个 tick，等价逐块版本但快 40 倍"""
        comp = self._pblocks()
        if comp is None:
            job.pb_cap = False
            return
        blocks = job.blocks
        if getattr(job, "pb_sec", None) is not sec:
            # 分帧建表：1.6 万块的区分桶+排序要 30ms，不能压在这一帧上
            bst = getattr(job, "pb_st", None)
            if bst is None or bst.get("sec") is not sec:
                bst = {"buckets": {}, "i": 0, "sec": sec}
                job.pb_st = bst
            built = self._pb_build(sec["blocks"], blocks, bst,
                                   float(cfg.PREP_SLICE_MS))
            if built is None:
                return                       # 还没分完，这一帧就先不写
            job.pb_tiles, job.pb_flat = built
            job.pb_st = None
            job.pb_sec = sec
            job.pb_i = 0
        tiles = job.pb_tiles
        flat = job.pb_flat
        budget = self._write_budget()
        t0 = time.time()
        n = 0
        # 单块（16³）实测耗时的滑动平均：写之前先算"再写一块会不会超预算"。
        # 以前是写完再检查时间，8ms 一块、预算 12ms，检查时必然还没超 -> 又写一块
        # -> 一 tick 花掉 16~24ms，预算形同虚设，帧率就是这么掉的。
        est = getattr(job, "pb_ms", 0.0) or 8.0
        cap = int(cfg.PALETTE_CELLS)
        while job.pb_i < len(tiles):
            if n >= cap:
                break
            spent = (time.time() - t0) * 1000.0
            if n and spent + est > budget:
                break                      # 再来一块就超了，留给下一 tick
            st, en, ox, oy, oz = tiles[job.pb_i]
            cnt = en - st
            _w0 = time.time()
            if self._pb_write(job, comp, blocks, flat, st, en, ox, oy, oz):
                job.ok += cnt
            else:
                job.fail += cnt
                job.retry.extend(flat[st:en])   # 整块退回逐块重写
                if job.err is None:
                    job.err = u"批量写入返回失败（区块未加载？）"
            dt = (time.time() - _w0) * 1000.0
            est = est * 0.7 + dt * 0.3
            job.pb_i += 1
            n += 1
        job.pb_ms = est
        if job.pb_i < len(tiles):
            return
        job.pb_tiles = None
        job.pb_flat = None
        job.pb_sec = None
        job.cursor = len(sec["blocks"])
        self._after_blocks(job, sec)

    def _after_blocks(self, job, sec):
        """一个分区的方块写完后决定下一步（逐块/批量两条路径共用）"""
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
        msg = util.b(text)
        if not msg:
            return          # 空消息不发：新引擎会判成非法包，直接断开连接
        try:
            # 网易这族消息接口只吃 UTF-8 字节串：传 unicode 中文会被静默丢弃
            # （SetTipMessage/SetExtraData/NotifyOneMessage 同款坑，实测）
            # 第三个参数（颜色）也必须是**非空**字符串：传空串会触发
            # "expected string with at least 1 characters" 的包校验失败，
            # 服务端随后 onDisconnect(reason 41) 把世界关掉。
            self._msg().NotifyOneMessage(pid, msg, util.b(u"\u00a7f"))
        except Exception:
            util.log_exc("tell")

    # ------------------------------------------------------------ 冻结时间流逝
    # 水/岩浆流动走引擎的方块刻，没有"暂停某个区域"的公开接口。能做的是两手：
    #   1. 尽量把随机刻/昼夜/天气按住（只动读得到的规则，结束原样恢复）
    #   2. 液体最后放、放完守几秒原地重写（真正管用的是这招，见 _step_seal）
    _FREEZE_KEYS = ("random_tick_speed", "randomtickspeed",
                    "do_daylight_cycle", "daylight_cycle",
                    "do_weather_cycle", "weather_cycle")

    def _freeze_on(self):
        if self._frozen_rules is not None:
            return
        game = self._game()
        if game is None:
            self._frozen_rules = {}
            return
        try:
            rules = game.GetGameRulesInfoServer()
        except Exception:
            rules = None
        saved = {}
        if isinstance(rules, dict):
            for k, v in rules.items():
                if util.u(k).lower() in self._FREEZE_KEYS:
                    saved[k] = v
        self._frozen_rules = saved
        upd = {}
        for k in saved:
            upd[k] = 0 if "tick" in util.u(k).lower() else False
        if upd:
            try:
                game.SetGameRulesInfoServer(upd)
            except Exception:
                util.log_exc("freeze rules")

    def _freeze_off(self):
        saved = self._frozen_rules
        self._frozen_rules = None
        if not saved:
            return
        game = self._game()
        if game is None:
            return
        try:
            game.SetGameRulesInfoServer(saved)
        except Exception:
            util.log_exc("unfreeze rules")
    # ------------------------------------------------------------ T显（tip 通知）
    def _tip_raw(self, text):
        """不绑定任务的一条 tip（解析阶段、收尾提示用）"""
        game = self._game()
        if game is None:
            return
        try:
            # 空串会让引擎判成非法包，清空小栏时发一个空格
            game.SetTipMessage(util.b(text) or util.b(u" "))
        except Exception:
            pass

    def _show_tip(self, job, force=False):
        """把当前进度 / 速度显示在物品栏上方的 tip 通知里

        这里是整个导入过程唯一的"实时画面"：聊天框只留开始/结束/报错，
        进度、阶段、区块等待、锁水全部压到这一行，每 0.5 秒覆盖刷新。
        速度只按"真正干活的 tick"算：暂停和等区块加载的时间不算进去。
        """
        game = self._game()
        if game is None:
            return
        done = job.ok + job.fail + job.liq_ok + job.liq_fail
        if job.total:
            pct = int(done * 100 / job.total)
        else:
            pct = 100
        secs = job.work_ticks / 20.0
        speed = ("§a%d块/s" % int(done / secs)) if secs >= 0.5 else u"§7测速中"
        # 剩余时间：跑够 1 秒才有意义，免得一开始显示个巨大的数字吓人
        eta = u""
        if secs >= 1.0 and done > 0 and job.total > done:
            left = (job.total - done) / (done / secs)
            eta = u" §7剩%.0fs" % left

        if job.paused:
            text = u"§e⏸ 结构导入 §c已暂停 §7%d%% %s §8(/structureim pause 继续)" % (pct, speed)
        elif job.waiting:
            text = u"§e⏳ 结构导入 §f%d%% %s §7等区块加载 %s" % (pct, speed, self._sec_tag(job))
        elif job.phase == "liquid":
            n = len(job.liquid_idx)
            text = u"§b▶ 结构导入 §f%d%% §7· §9放液体 §f%d/%d §7· %s" % (
                pct, job.liq_cursor, n, speed)
        elif job.phase == "wash":
            text = u"§6▶ 结构导入 §f补被水冲掉的方块 §7%d/%d §7· 已补 %d" % (
                job.wash_cursor, len(job.wash_idx), job.wash_fixed)
        elif job.phase == "seal":
            left = max(0, job.seal_until - job.tick) / 20.0
            text = u"§3■ 结构导入 §f静默守护 §7· 巡检 %d 格 §7· 补 %d §7· 剩 %.0fs" % (
                len(job.sealed), job.seal_fixes, left)
        elif job.phase == "prep":
            cur = int((job.prep or {}).get("cursor") or 0)
            text = u"§b▶ 结构导入 §7准备分区 §f%d/%d §7· 扫描方块（分帧，不卡）" % (
                cur, job.total)
        elif job.phase == "verify":
            text = u"§b▶ 结构导入 §9逐块核对 §f%d/%d §7· 不符 %d §7· 只查不补" % (
                int(getattr(job, "vf_cursor", 0) or 0), job.total,
                int(getattr(job, "vf_miss", 0) or 0))
        else:
            phase = {u"blocks": u"§f放置", u"wake": u"§b红石唤醒",
                     u"ents": u"§d方块实体", u"items": u"§d容器物品",
                     u"mobs": u"§6生成实体"}.get(job.phase, job.phase)
            text = u"§b▶ 结构导入 §f%d%% §7%d/%d %s%s §7· %s%s" % (
                pct, done, job.total, speed, eta, phase, self._sec_tag(job))
            if job.note:
                text += u" §7· " + job.note
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
        self.split_size = 0                    # 分区边长：0=自动（按体量自适应），>0=自定义格数
        self.tick_mode = cfg.TICK_DEFAULT
        self.tick_speed = cfg.SPEED_DEFAULT
        self.chunk = cfg.CHUNK_DEFAULT
        self.map_mode = cfg.MAP_DEFAULT
        self.turbo = cfg.TURBO_DEFAULT
        self.zone_secs = cfg.TP_ZONE_SECS
        self.zones_n = 3
        self.big_wait = cfg.TP_BIG_WAIT / 20.0
        self.budget = cfg.WRITE_BUDGET_MS
        self.threads = cfg.THREADS_DEFAULT
        self.parallel = cfg.PARALLEL_DEFAULT
        self.nbt_limit = cfg.ITEM_NBT_MAX_KB
        self.freeze = cfg.FREEZE_DEFAULT     # 导入期间冻结时间流逝（水不乱流）
        self.freeze_secs = cfg.FREEZE_SECS   # 全部写完后继续守液体多少秒
        self._frozen_rules = None            # 被改过的游戏规则原值，结束时恢复
        self._skipped = 0                    # 上一次整理时跳过的运行时方块数
        self._liquid_idx = []                # 上一次切分区时挑出来的液体索引
        self._cfg_ver = 0                        # 存档里的配置版本（用于一次性迁移）
        self.map_speed = cfg.MAP_SPEED_DEFAULT   # 地图画速度档：0 稳 / 1 快 / 2 极速
        self.ta_mode = cfg.TA_MODE_DEFAULT       # 纯常加载：不跟随 TP，全结构挂 tickingarea
        self.verify = cfg.VERIFY_DEFAULT         # 收尾逐块回读校验（不漏块）
        self._guard_scale = 1.0                  # 单 tick 超时后自动降速的系数
        self.map_area = cfg.MAP_AREA_SQUARE      # 常加载形状：True 方形（盖满）/ False 圆形（老写法，兼容用）
        try:
            raw = self.factory.CreateExtraData(self.levelId).GetExtraData(cfg.EXTRA_KEY)
            if isinstance(raw, dict):
                # 老存档里的配置版本：低于当前版本的项做一次性迁移
                try:
                    self._cfg_ver = int(util.u(raw.get("cfgver")) or 0)
                except (TypeError, ValueError):
                    self._cfg_ver = 0
                path = util.u(raw.get("dir"))
                flag = util.u(raw.get("split")).strip()
                if flag:
                    self.split = flag == u"1"
                if raw.get("splitz") is not None:
                    try:
                        self.split_size = max(0, min(int(cfg.SPLIT_SIZE_MAX),
                                                     int(util.u(raw.get("splitz")))))
                    except (TypeError, ValueError):
                        pass
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
                    th = int(util.u(raw.get("threads")))
                    self.threads = max(1, min(cfg.THREADS_MAX, th))
                except (TypeError, ValueError):
                    pass
                try:
                    pa = int(util.u(raw.get("parallel")))
                    self.parallel = max(1, min(cfg.PARALLEL_MAX, pa))
                except (TypeError, ValueError):
                    pass
                try:
                    nl = int(util.u(raw.get("nbtlimit")))
                    if nl == 0 or 8 <= nl <= 1024:
                        self.nbt_limit = nl
                        cfg.ITEM_NBT_MAX_KB = nl
                except (TypeError, ValueError):
                    pass
                try:
                    speed = int(util.u(raw.get("speed")))
                    self.tick_speed = max(cfg.SPEED_MIN, min(cfg.SPEED_MAX, speed))
                except (TypeError, ValueError):
                    pass
                fz = util.u(raw.get("freeze")).strip()
                if fz:
                    self.freeze = fz == u"1"
                try:
                    fs = float(util.u(raw.get("freezesecs")))
                    self.freeze_secs = max(0.0, min(cfg.FREEZE_SECS_MAX, fs))
                except (TypeError, ValueError):
                    pass
                if self._cfg_ver >= cfg.CFG_VERSION:
                    # 只有新版本存档才认地图画速度档：老存档里被设成极速的，
                    # 升级后一律回到「稳」，避免继续踩挂画阶段那个坑
                    try:
                        ms = int(util.u(raw.get("mapspeed")))
                        self.map_speed = max(0, min(2, ms))
                    except (TypeError, ValueError):
                        pass
                if raw.get("maparea") is not None:
                    self.map_area = util.u(raw.get("maparea")) != u"0"
                if raw.get("ta") is not None:
                    self.ta_mode = util.u(raw.get("ta")) == u"1"
                if raw.get("verify") is not None:
                    self.verify = util.u(raw.get("verify")) != u"0"
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
                 "splitz": util.b(str(int(getattr(self, "split_size", 0)))),
                 "tick": util.b(u"1" if self.tick_mode else u"0"),
                 "speed": util.b(str(self.tick_speed)),
                 "chunk": util.b(u"1" if self.chunk else u"0"),
                 "map": util.b(u"1" if self.map_mode else u"0"),
                 "turbo": util.b(u"1" if self.turbo else u"0"),
                 "zsecs": util.b(str(self.zone_secs)),
                 "znum": util.b(str(self.zones_n)),
                 "bigwait": util.b(str(self.big_wait)),
                 "budget": util.b(str(self.budget)),
                 "threads": util.b(str(self.threads)),
                 "parallel": util.b(str(self.parallel)),
                 "freeze": util.b(u"1" if self.freeze else u"0"),
                 "freezesecs": util.b(str(self.freeze_secs)),
                 "mapspeed": util.b(str(self.map_speed)),
                 "maparea": util.b(u"1" if self.map_area else u"0"),
                 "ta": util.b(u"1" if self.ta_mode else u"0"),
                 "verify": util.b(u"1" if self.verify else u"0"),
                 "cfgver": util.b(str(cfg.CFG_VERSION)),
                 "nbtlimit": util.b(str(self.nbt_limit))})
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
        self.tell(pid, u"§7  · 速度档：§fmapspeed <0-2> §7（0稳/1快/2极速），快档单张约 2~3 秒")
        self.tell(pid, u"§7    快档靠这招提速：常加载改成方形盖住整幅 + 下一张的区块提前挂好")
        self.tell(pid, u"§e四、调速：§fbudget <2-20> §7每 tick 写入预算（ms，越大越快越卡，默认7）")
        self.tell(pid, u"§7  写入量会按机器实测速度自动调，卡就调小、闲着就调大")
        self.tell(pid, u"§e五、并行：§fthreads <1-8> §7解析线程（≥2 后台解析）、§fparallel <1-4> §7相邻分区并行")
        self.tell(pid, u"§e六、含水/液体：§f默认自动处理 §7（海里、水池、含水楼梯、水缸都能还原）")
        self.tell(pid, u"§7  · 液体全部固体放完后才灌，灌完守几秒，期间水不会到处乱流")
        self.tell(pid, u"§7  · §ffreeze <开/关> §7冻结时间流逝、§ffreezetime <0-60> §7守水秒数")
        self.tell(pid, u"§e七、其它开关：§fsplit 开/关 §7分区、§fturbo §7急速、§fselftest §7自检")
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
            self._gc_restore()
            self._bg = None            # 后台解析结果直接丢弃（线程跑完自然退出）
            self._pj = None            # 分帧解析同样直接丢弃
            if self.map_queue:
                for _ci in _range(len(self.map_queue.get("cells") or [])):
                    self._map_ta_del(self.map_queue, _ci)
                self.map_queue = None
            self._freeze_off()         # 中止也要把游戏规则还回去
            self._tip_raw(u"")
            self.tell(pid, u"§a已中止")
        elif action in (u"pause", u"resume", u"p"):
            self.cmd_pause(pid)
        elif action == u"info":
            self.cmd_info(pid)
        elif action == u"selftest":
            self.cmd_selftest(pid)
        elif action in (u"split", u"zone"):
            self.cmd_split(pid, a1)
        elif action in (u"map", u"mapmode"):
            self.cmd_mapmode(pid, a1)
        elif action in (u"zwait", u"zsec"):
            self.cmd_zwait(pid, a1)
        elif action == u"bigwait":
            self.cmd_bigwait(pid, a1)
        elif action in (u"budget", u"tick"):
            self.cmd_budget(pid, a1)
        elif action in (u"threads", u"thread"):
            self.cmd_threads(pid, a1)
        elif action in (u"parallel", u"para"):
            self.cmd_parallel(pid, a1)
        elif action in (u"nbtlimit", u"nbt"):
            self.cmd_nbtlimit(pid, a1)
        elif action in (u"freeze", u"冻结"):
            self.cmd_freeze(pid, a1)
        elif action in (u"freezetime", u"freezesecs", u"frost"):
            self.cmd_freezetime(pid, a1)
        elif action in (u"mapspeed", u"mapspd", u"mspeed"):
            self.cmd_mapspeed(pid, a1)
        elif action in (u"maparea", u"marea"):
            self.cmd_maparea(pid, a1)
        elif action == u"turbo":
            self.cmd_turbo(pid, a1)
        elif action in (u"preset", u"pre", u"tier"):
            self.cmd_preset(pid, a1)
        elif action in (u"verify", u"check"):
            self.cmd_verify(pid, a1)
        elif action in (u"splitz", u"sidesz", u"side"):
            self.cmd_splitz(pid, a1)
        elif action == u"fast":
            self.cmd_fast(pid, a1)
        elif action in (u"ta", u"tacover"):
            self.cmd_ta(pid, a1)
        elif action in (u"tac", u"taclean", u"清理常加载"):
            self.cmd_tac(pid, a1)
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

    def cmd_preset(self, pid, text):
        """一键预设：稳 / 均衡 / 极速（一次改完一整组参数并落盘）"""
        raw = (text or u"").strip().lower()
        alias = {u"稳": "safe", u"安全": "safe", u"safe": "safe", u"1": "safe",
                 u"均衡": "balance", u"平衡": "balance", u"balance": "balance",
                 u"2": "balance",
                 u"极速": "turbo", u"快": "turbo", u"turbo": "turbo",
                 u"3": "turbo"}
        name = alias.get(raw, raw)
        pack = cfg.PRESETS.get(name)
        if not pack:
            cur = u" / ".join(u"%s=%s" % (k, cfg.PRESET_LABELS.get(k, k))
                              for k in ("safe", "balance", "turbo"))
            self.tell(pid, u"§e用法：/structureim preset <稳|均衡|极速> §7（%s）" % cur)
            return
        self.turbo = bool(pack["turbo"])
        self.budget = float(pack["budget"])
        self.threads = max(1, int(pack["threads"]))
        self.parallel = max(1, int(pack["parallel"]))
        self.verify = bool(pack["verify"])
        self.map_speed = max(0, min(2, int(pack["mapspeed"])))
        self.split_size = max(0, min(int(cfg.SPLIT_SIZE_MAX), int(pack["splitz"])))
        self._save()
        self.tell(pid, u"§a已套用预设：§f%s §7（急速 %s · 预算 %d ms · 线程 %d · 并行 %d · "
                       u"地图 %s · 分区 %s · 回读校验 %s）" %
                  (cfg.PRESET_LABELS.get(name, name),
                   u"开" if self.turbo else u"关", int(self.budget), self.threads,
                   self.parallel,
                   [u"稳", u"快", u"极速"][self.map_speed],
                   u"自动" if not self.split_size else u"%d 格" % self.split_size,
                   u"开" if self.verify else u"关"))
        self.push(pid, self._page)

    def cmd_verify(self, pid, text):
        """收尾逐块回读核对：只报差异块数，不补写（默认关）"""
        t = (text or u"").strip().lower()
        if t in (u"on", u"1", u"开", u"true"):
            v = True
        elif t in (u"off", u"0", u"关", u"false"):
            v = False
        elif not t:
            v = not self.verify
        else:
            self.tell(pid, u"§e用法：/structureim verify <开|关> §7当前 %s" %
                      (u"开" if self.verify else u"关"))
            return
        self.verify = v
        self._save()
        self.tell(pid, u"§a逐块回读核对：§f%s §7（%s）" %
                  (u"开" if v else u"关",
                   u"导完读一遍，只告诉你多少块没对上，不补写" if v else u"只信写入返回值，最快"))

    def cmd_splitz(self, pid, text):
        """分区边长：0 = 自动按体量自适应；16~128 = 自定义固定边长"""
        t = util.u(text).strip()
        try:
            v = int(t)
        except ValueError:
            v = -1
        if v < 0:
            cur = int(getattr(self, "split_size", 0) or 0)
            self.tell(pid, u"§e用法：/structureim splitz <0|16-128> §7当前 %s" %
                      (u"自动" if cur <= 0 else u"%d 格" % cur))
            self.tell(pid, u"§7自动=按结构体量挑边长（大平面会自动放大到最多 %d 格）" %
                      cfg.SPLIT_SIZE_MAX)
            return
        self.split_size = max(0, min(int(cfg.SPLIT_SIZE_MAX), v))
        self._save()
        self.tell(pid, u"§a分区边长：§f%s" %
                  (u"自动" if self.split_size == 0 else u"%d 格" % self.split_size))

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
        sz = int(getattr(self, "split_size", 0) or 0)
        self.tell(pid, u"§a分区导入：§f%s §7（每区 %s）" %
                  (u"开" if self.split else u"关",
                   u"%d 格" % sz if sz > 0 else u"自动边长"))
        self.push(pid, self._page)

    def cmd_mapmode(self, pid, text):
        """地图画模式开关：关掉它，导入完就结束，不再发地图、不再搭墙"""
        t = util.u(text).strip().lower()
        if t in (u"", u"toggle", u"switch"):
            self.map_mode = not self.map_mode
        elif t in (u"on", u"true", u"1", u"open", u"开"):
            self.map_mode = True
        elif t in (u"off", u"false", u"0", u"close", u"关"):
            self.map_mode = False
        else:
            self.tell(pid, u"§e用法：/structureim map <开/关>")
            return
        self._save()
        if self.map_mode:
            self.tell(pid, u"§a地图画模式：§f开 §7（导完会搭墙、逐张画；每张约 %.1f 秒）" %
                      ((self._map_first_wait(False) + self._map_give_wait() * 2 +
                        self._map_hold() + self._map_zone_wait() * max(0, self.zones_n - 1)) / 20.0))
        else:
            self.tell(pid, u"§a地图画模式：§f关 §7（导完即结束，不再花时间画地图）")
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

    def cmd_threads(self, pid, text):
        """后台解析线程数：1 = 关闭（主线程解析），>=2 = 结构解析走后台"""
        try:
            v = int((text or u"").strip())
        except ValueError:
            self.tell(pid, u"§e用法：/structureim threads <1-%d> §7当前 %d%s" %
                      (cfg.THREADS_MAX, self.threads,
                       u"（后台解析）" if self.threads > 1 else u"（主线程解析）"))
            self.tell(pid, u"§7写方块受引擎限制只能主线程，线程数只影响解析")
            return
        if not (1 <= v <= cfg.THREADS_MAX):
            self.tell(pid, u"§c范围 1~%d" % cfg.THREADS_MAX)
            return
        self.threads = v
        self._save()
        if v > 1:
            self.tell(pid, u"§a后台解析：§f开 §7（线程 %d）· 结构解析不再卡主线程" % v)
        else:
            self.tell(pid, u"§a后台解析：§f关 §7（主线程同步解析，最稳）")

    def cmd_parallel(self, pid, text):
        """相邻分区并行度：提前给接下来几个邻区挂常加载区块，写当前区时它们在后台加载"""
        try:
            v = int((text or u"").strip())
        except ValueError:
            self.tell(pid, u"§e用法：/structureim parallel <1-%d> §7当前 %d" %
                      (cfg.PARALLEL_MAX, self.parallel))
            self.tell(pid, u"§7>1 时提前加载相邻分区，轮到它时不用等区块（引擎限制：写方块仍主线程）")
            return
        if not (1 <= v <= cfg.PARALLEL_MAX):
            self.tell(pid, u"§c范围 1~%d" % cfg.PARALLEL_MAX)
            return
        self.parallel = v
        self._save()
        if v > 1:
            self.tell(pid, u"§a相邻分区并行：§f%d §7（提前加载 %d 个邻区，与写入重叠）" % (v, v - 1))
        else:
            self.tell(pid, u"§a相邻分区并行：§f关")

    def cmd_freeze(self, pid, text):
        """导入期间冻结时间流逝（水/岩浆不乱跑）：不填 = 开关切换"""
        t = (text or u"").strip().lower()
        if t in (u"on", u"1", u"开", u"true"):
            v = True
        elif t in (u"off", u"0", u"关", u"false"):
            v = False
        elif not t:
            v = not self.freeze
        else:
            self.tell(pid, u"§e用法：/structureim freeze <on/off> §7当前 %s" %
                      (u"开" if self.freeze else u"关"))
            self.tell(pid, u"§7开启后：液体最后统一放，放完再守 %.0f 秒，期间水不会乱流" %
                      self.freeze_secs)
            return
        self.freeze = v
        self._save()
        if v:
            self.tell(pid, u"§a冻结时间流逝：§f开 §7（液体最后放 + 守 %.0f 秒）" % self.freeze_secs)
        else:
            self.tell(pid, u"§a冻结时间流逝：§f关 §7（水流会按引擎规则自己流）")

    def cmd_freezetime(self, pid, text):
        """守液体多少秒：0 = 只做最后写入，不守"""
        try:
            v = float((text or u"").strip())
        except ValueError:
            self.tell(pid, u"§e用法：/structureim freezetime <0-%.0f> §7当前 %.1f 秒" %
                      (cfg.FREEZE_SECS_MAX, self.freeze_secs))
            return
        if not (0.0 <= v <= cfg.FREEZE_SECS_MAX):
            self.tell(pid, u"§c范围 0~%.0f 秒" % cfg.FREEZE_SECS_MAX)
            return
        self.freeze_secs = v
        self._save()
        self.tell(pid, u"§a液体守护时长：§f%.1f 秒" % v)

    def cmd_ta(self, pid, text):
        """纯常加载模式：整张结构挂 tickingarea，玩家原地不动、不跟随 TP"""
        t = (text or u"").strip().lower()
        if t in (u"on", u"1", u"开", u"true"):
            v = True
        elif t in (u"off", u"0", u"关", u"false"):
            v = False
        elif not t:
            v = not self.ta_mode
        else:
            self.tell(pid, u"§e用法：/structureim ta <on/off> §7当前 %s" %
                      (u"开" if self.ta_mode else u"关"))
            self.tell(pid, u"§7开启后整张结构挂一个常加载区块，人不用跟着传送，"
                           u"站在旁边看它长出来")
            return
        self.ta_mode = v
        self._save()
        self.tell(pid, u"§a纯常加载模式：§f%s §7（%s）" %
                  (u"开" if v else u"关",
                   u"人不跟着跑，整张结构常加载" if v else u"恢复跟随 TP"))

    def cmd_tac(self, pid, text):
        """清理本插件残留的常加载区（崩服/中途退出留下的那些）

        **只按 si_ 前缀删自己的**，绝不用 /tickingarea remove_all：
        服务器里通常已经有别人的指令区、出生点常加载，一条都不能动。
        """
        cmd = self._command()
        if cmd is None:
            self.tell(pid, u"§c拿不到命令组件，清理失败")
            return
        prefix = cfg.AREA_PREFIX
        names = []
        for i in _range(max(1, int(cfg.TA_MAX_AREAS))):
            names.append(u"%sta_%d" % (prefix, i))
        for i in _range(64):
            names.append(u"%spf_%d" % (prefix, i))
            names.append(u"%smap_%d" % (prefix, i))
        n = 0
        for nm in names:
            try:
                if cmd.SetCommand(util.b(u"/tickingarea remove %s" % nm)):
                    n += 1
            except Exception:
                pass
        self.tell(pid, u"§a已清理本插件残留常加载 §f%d §7个"
                       u"（只删 %s 开头的，别人的指令区没动）" % (n, prefix))

    def cmd_turbo(self, pid, text):
        """急速模式：分区等待压到最短、写入预算拉满（大结构提速的主要手段）"""
        t = (text or u"").strip().lower()
        if t in (u"on", u"1", u"开", u"true"):
            v = True
        elif t in (u"off", u"0", u"关", u"false"):
            v = False
        elif not t:
            v = not self.turbo
        else:
            self.tell(pid, u"§e用法：/structureim turbo <on/off> §7当前 %s" %
                      (u"开" if self.turbo else u"关"))
            self.tell(pid, u"§7开启后：等区块 3 tick、写入预算 %.0f ms，求快不求稳" %
                      cfg.WRITE_BUDGET_MS_TURBO)
            return
        self.turbo = v
        self._save()
        if v:
            self.tell(pid, u"§a急速模式：§f开 §7（等区块 3 tick、预算 %.0f ms，写失败自动重试兜底）" %
                      cfg.WRITE_BUDGET_MS_TURBO)
        else:
            self.tell(pid, u"§a急速模式：§f关")

    def cmd_fast(self, pid, text):
        """一键提速：后台解析 + 急速 + 满预算 + 邻区并行

        大结构（几十万块）的导入时间基本由三件事决定：
        写得多快（预算）、换区等多久（急速）、解析卡不卡主线程（线程）。
        这条命令把三者一起拉满，等价于手敲四条。
        """
        self.threads = max(2, cfg.THREADS_DEFAULT)
        self.parallel = max(3, cfg.PARALLEL_DEFAULT)
        self.turbo = True
        self.budget = cfg.WRITE_BUDGET_MS_TURBO
        # 地图画 640×640 要在 120 秒内落地，光靠写入提速不够：
        # 25 张图每张都要"挂常加载 -> 站进去 -> 让引擎画"，
        # 这里把地图档一起拉到极速（单张 ≈2 秒）。
        self.map_speed = 2
        if not self.split_size:
            self.split_size = int(cfg.SPLIT_SIZE_MAX)
        self._save()
        self.tell(pid, u"§a已切到提速组合：§f线程 %d · 邻区并行 %d · 急速 · 预算 %.0f ms · 地图极速 · 分区 %d 格" %
                  (self.threads, self.parallel, self.budget, self.split_size))
        self.tell(pid, u"§7回读核对默认关（只信写入返回值）；想点数可 /structureim verify on")

    def cmd_nbtlimit(self, pid, text):
        """物品 NBT 上限：0 = 不裁剪（默认，盒中盒原样保留）"""
        try:
            v = int((text or u"").strip())
        except ValueError:
            cur = self.nbt_limit
            self.tell(pid, u"§e用法：/structureim nbtlimit <0|8-1024> §7当前 %s" %
                      (u"不裁剪" if cur <= 0 else u"%d KB" % cur))
            self.tell(pid, u"§7盒中盒这类超大 NBT 默认完整保留；真卡了再设上限")
            return
        if v != 0 and not (8 <= v <= 1024):
            self.tell(pid, u"§c范围 0（不裁剪）或 8~1024 KB")
            return
        self.nbt_limit = v
        cfg.ITEM_NBT_MAX_KB = v
        self._save()
        if v <= 0:
            self.tell(pid, u"§a物品 NBT：§f不裁剪 §7（完整保留）")
        else:
            self.tell(pid, u"§a物品 NBT 上限：§f%d KB §7（超出的只留物品本体）" % v)

    def cmd_mapspeed(self, pid, text):
        """地图画速度档：0 稳（旧逻辑）/ 1 快（默认）/ 2 极速

        快的来源不是把画质降级，而是三件事：
          1. 常加载从圆形（切四角）改成方形，正好盖住整幅 128×128
          2. 下一张图的常加载在这张画的时候就挂好，等待与绘制重叠
          3. 巡逻点等待从"秒级"压到 tick 级（区块本来就都加载着）
        """
        t = (text or u"").strip().lower()
        table = {u"0": 0, u"稳": 0, u"safe": 0, u"safe0": 0,
                 u"1": 1, u"快": 1, u"fast": 1,
                 u"2": 2, u"极速": 2, u"turbo": 2}
        if not t:
            self.tell(pid, u"§e用法：/structureim mapspeed <0-2> §7当前 §f%d §7（%s）" %
                      (self.map_speed, [u"稳", u"快", u"极速"][self.map_speed]))
            per = self._map_first_wait(False) + self._map_give_wait() * 2 + \
                self._map_hold() + self._map_zone_wait() * max(0, self.zones_n - 1)
            self.tell(pid, u"§7单张约 §f%.1f 秒§7（%d 个巡逻点）；0=最保险，2=最快但可能缺角" %
                      (per / 20.0, min(self.zones_n, 9)))
            return
        if t not in table:
            self.tell(pid, u"§c只认 0 / 1 / 2（稳 / 快 / 极速）")
            return
        self.map_speed = table[t]
        self._save()
        if self.map_speed == 0:
            self.tell(pid, u"§a地图画速度：§f稳§7（默认。老的圆形常加载 + 每区停 %d 秒，最不容易出错）" %
                      self.zone_secs)
            self.tell(pid, u"§7单张约 %.1f 秒；只是慢，但挂画不会出错" %
                      ((self._map_first_wait(False) + self._map_give_wait() * 2 +
                        self._map_hold() + self._map_zone_wait() * max(0, self.zones_n - 1)) / 20.0))
        else:
            per = self._map_first_wait(False) + self._map_give_wait() * 2 + \
                self._map_hold() + self._map_zone_wait() * max(0, self.zones_n - 1)
            self.tell(pid, u"§a地图画速度：§f%s §7（单张约 %.1f 秒，方形常加载 + 提前挂下一张）" %
                      (u"快" if self.map_speed == 1 else u"极速", per / 20.0))
            self.tell(pid, u"§7还能更快：§fznum 1 §7（方形常加载已盖住整幅，理论上一个点就够）")
            if self.map_speed == 2:
                self.tell(pid, u"§7极速档如果出现缺角/空白，改回 §f1 §7或把 §fznum §7调大")

    def cmd_maparea(self, pid, text):
        """常加载形状：square（默认，盖满整幅）/ circle（老写法，兼容用）

        如果你的版本不认方形 tickingarea（聊天框报命令语法错），就切 circle。
        """
        t = (text or u"").strip().lower()
        if not t:
            self.tell(pid, u"§e用法：/structureim maparea <square|circle> §7当前 §f%s" %
                      (u"square 方形" if self.map_area else u"circle 圆形"))
            return
        if t in (u"square", u"1", u"方", u"方形"):
            self.map_area = True
        elif t in (u"circle", u"0", u"圆", u"圆形"):
            self.map_area = False
        else:
            self.tell(pid, u"§c只认 square / circle")
            return
        self._save()
        if self.map_area:
            self.tell(pid, u"§a常加载形状：§f方形§7（只在 §fmapspeed 1/2 §7生效，一次盖住整幅 128×128）")
            if self.map_speed == 0:
                self.tell(pid, u"§7当前是稳档，仍走老的圆形常加载；想用方形先把 §fmapspeed §7调到 1 或 2")
        else:
            self.tell(pid, u"§a常加载形状：§f圆形§7（老写法，四角盖不到，§fznum §7调大些更保险）")

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
        self.tell(pid, u"§6解析线程：§f%d §7（%s）" %
                  (self.threads, u"后台解析" if self.threads > 1 else u"主线程解析"))
        self.tell(pid, u"§6相邻并行：§f%d §7（%s）" %
                  (self.parallel, u"提前加载邻区" if self.parallel > 1 else u"关"))
        self.tell(pid, u"§6冻结时间：§f%s §7（守液体 %.1f 秒）" %
                  (u"开" if self.freeze else u"关", self.freeze_secs))
        self.tell(pid, u"§6地图速度：§f%s §7（单张约 %.1f 秒）" %
                  ([u"稳", u"快", u"极速"][self.map_speed],
                   (self._map_first_wait(False) + self._map_give_wait() * 2 +
                    self._map_hold() + self._map_zone_wait() *
                    max(0, self.zones_n - 1)) / 20.0))

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
        """放置入口：线程数 >=2 且文件够大 -> 后台线程解析，其余走主线程分帧

        两条路都**不会一次性把主线程占住**：后台线程每片之间让出 GIL，
        主线程分帧每 tick 只花 cfg.PARSE_SLICE_MS 毫秒。
        """
        if self.threads > 1 and self._need_bg(entry):
            self._bg_start(pid, entry, rotation)
            return
        self._pj_start(pid, entry, rotation)

    # ------------------------------------------------------------ 分帧解析（主线程）
    def _pj_start(self, pid, entry, rotation):
        """不卡版解析：主线程一帧一帧推，每帧最多 PARSE_SLICE_MS 毫秒"""
        if self._pj is not None:
            self.tell(pid, u"§e上一份结构还在解析，等它一下")
            return
        self._pj = {"pid": pid, "entry": entry, "rot": rotation,
                    "gen": None, "out": {}, "t0": time.time(),
                    "last": 0.0, "done": 0, "expect": 0}
        self._tip_raw(u"§b⏳ 结构导入 §7读取中 §f%s" %
                      util.short(entry.rel, 28))

    def _parse_step(self):
        """推进分帧解析一小片；返回 True 表示这一帧还在解析

        分片粒度 PARSE_CHUNK 块 ≈ 几毫秒，再配合 PARSE_SLICE_MS 预算：
        单帧最多花几毫秒，引擎该出的帧照出，玩家不会看到"卡住不动"。
        """
        pj = self._pj
        if pj is None:
            return False
        if pj["gen"] is None:
            # 第一帧只做「读文件 + 解压」：通常几十毫秒，无法再切
            try:
                dec = mcst.read_decompress(pj["entry"].path)
            except Exception as exc:
                util.log_exc("load " + pj["entry"].path)
                self._pj = None
                self.tell(pj["pid"], u"§c读取失败：§f%s" % util.u(exc))
                return False
            pj["gen"] = mcst.iter_load_prepared(
                dec, pj["entry"].name, cfg.SKIP_AIR, cfg.SKIP_VOID,
                chunk=cfg.PARSE_CHUNK, out=pj["out"])
            return True
        deadline = time.time() + cfg.PARSE_SLICE_MS / 1000.0
        gen = pj["gen"]
        try:
            while True:
                done, total = next(gen)
                pj["done"], pj["expect"] = done, total
                if time.time() >= deadline:
                    break
        except StopIteration:
            data = pj["out"].get("data")
            pid, entry, rot = pj["pid"], pj["entry"], pj["rot"]
            secs = time.time() - pj["t0"]
            self._pj = None
            if data is None:
                self.tell(pid, u"§c解析失败：没有拿到数据")
                return False
            self._parse_secs = secs
            self._begin_job(pid, entry, rot, data)
            return False
        except Exception:
            tb = traceback.format_exc()
            util.log(u"分帧解析异常\n" + util.u(tb))
            self._pj = None
            self.tell(pj["pid"], u"§c读取失败：§f" + util.short(
                util.u(tb.strip().splitlines()[-1]), 60))
            return False
        now = time.time()
        if now - pj["last"] >= 0.5:
            pj["last"] = now
            pct = int(100.0 * pj["done"] / max(1, pj["expect"]))
            self._tip_raw(u"§b⏳ 结构导入 §7解析中 §f%s §7· %d%% · %.1f 秒" %
                          (util.short(pj["entry"].rel, 26), pct, now - pj["t0"]))
        return True

    # ------------------------------------------------------------ 多线程解析
    def _need_bg(self, entry):
        """只有大结构才值得开线程：小文件同步解析反而更快（省线程开销）"""
        try:
            return os.path.getsize(entry.path) >= cfg.BG_LOAD_MIN_KB * 1024
        except Exception:
            return True

    def _bg_start(self, pid, entry, rotation):
        if self._bg is not None:
            self.tell(pid, u"§e上一份结构还在后台解析，等它一下")
            return
        box = {}
        th = None
        try:
            import threading
            th = threading.Thread(target=_bg_load,
                                  args=(entry.path, entry.name, box))
            th.daemon = True
        except Exception:
            util.log_exc("thread")
            th = None
        if th is None:
            # 环境不支持线程 -> 退到主线程分帧解析，一样不卡
            self._pj_start(pid, entry, rotation)
            return
        self._bg = {"th": th, "box": box, "pid": pid, "entry": entry,
                    "rot": rotation, "t0": time.time(), "last": 0.0}
        try:
            th.start()
        except Exception:
            util.log_exc("thread start")
            self._bg = None
            self._pj_start(pid, entry, rotation)
            return
        self._tip_raw(u"§b⏳ 结构导入 §7后台解析中 §f%s §7· 引擎线程不受影响" %
                      util.short(entry.rel, 28))

    def _poll_bg(self):
        """主线程轮询后台解析结果（线程不碰引擎，结果只在这里消费）"""
        bg = self._bg
        if bg is None:
            return
        try:
            alive = bg["th"].is_alive()
        except Exception:
            alive = False
        if alive:
            now = time.time()
            if now - bg["last"] >= 0.5:
                bg["last"] = now
                # 解析进度也只走下方小栏，别在聊天框每秒刷一条
                _d = int(bg["box"].get("done") or 0)
                _t = int(bg["box"].get("total") or 0)
                _pct = int(100.0 * _d / _t) if _t else 0
                self._tip_raw(u"§b⏳ 结构导入 §7解析中 §f%s §7· %d%% · %.1f 秒" %
                              (util.short(bg["entry"].rel, 28), _pct, now - bg["t0"]))
            return
        self._bg = None
        box = bg["box"]
        if box.get("err"):
            util.log(u"后台解析失败\n" + util.u(box["err"]))
            self.tell(bg["pid"], u"§c读取失败：§f" + util.short(
                util.u(box["err"].strip().splitlines()[-1]), 60))
            return
        data = box.get("data")
        if data is None:
            self.tell(bg["pid"], u"§c解析失败：后台没有拿到数据")
            return
        self._parse_secs = time.time() - bg["t0"]
        self._begin_job(bg["pid"], bg["entry"], bg["rot"], data)

    def _begin_job(self, pid, entry, rotation, data):
        if data.count <= 0:
            self.tell(pid, u"§c这个结构里没有可放置的方块")
            return
        if data.count > cfg.MAX_BLOCKS:
            self.tell(pid, u"§c结构太大：%d 方块（上限 %d）" % (data.count, cfg.MAX_BLOCKS))
            return
        self._gc_relax()

        # 分块模式只在玩家手动开时才用。
        # 以前"超过 15 万块自动开分块"，结果把已经按体量放大好的 128 格分区
        # 又按 50KB 切碎——562 个小区，光换区就比写方块慢（a.mcstructure 实测）。
        # 写入早就由时间预算分帧控制，不需要再切小块。
        use_chunk = self.chunk

        blocks, size = mcst.rotate(data.blocks, data.size, rotation)
        # states -> aux 的翻译（_with_aux）挪到 prep 阶段分帧做：
        # 一百多万块时这一步在主线程要跑一两秒，正是"点导入，画面僵住"的来源。
        # 分帧版会原地改写 blocks（跳过项的压缩也在那里完成），见 _step_prep 阶段 0。
        self._skipped = 0
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
            # 坐标是从 0 开始的，按实际包围盒算（结构边上常有空气，size 会偏大，
            # 用 size 会多发几张空白地图）；地图画本身块数不大，扫一遍无所谓
            bx0 = min(b[0] for b in blocks)
            bz0 = min(b[2] for b in blocks)
            bw = max(b[0] for b in blocks) - bx0 + 1
            bh = max(b[2] for b in blocks) - bz0 + 1
            self.tell(pid, u"§7地图画模式：基点已对齐地图网格 §f%d, %d §7（尺寸 %dx%d → %d 张地图）" %
                      (base[0], base[2], bw, bh,
                       int(math.ceil(bw / float(ms))) *
                       int(math.ceil(bh / float(ms)))))

        # 分区边长按体量自适应：大平面不再被切成两千多个小区（每区才 256 块，
        # 换区开销比写方块还贵）。side 同时用于"跨行远传"的判定。
        side = self._pick_split_size(blocks)
        # 扫描（红石唤醒 / 补水候选 / 液体 / 分区归属）与分区构建全部挪到
        # prep 阶段分帧做：40 万块 × 二十多个关键字子串判断一次性跑完要
        # 几百毫秒到几秒，正是"点导入，游戏僵一下"的来源。
        wake = []
        wash = []
        sections = []
        self.job = Job(pid, entry.rel, blocks, base, dim, ents, wake, sections, mobs)
        self.job.sections = []           # Job 默认会给个"全部"占位，这里先清空
        self.job.phase = "prep"
        self.job.prep = {"cursor": 0, "wake": [], "wash": [], "liq": [],
                         "groups": {}, "side": side, "use_chunk": bool(use_chunk)}
        self.job.parse_secs = float(getattr(self, "_parse_secs", 0.0) or 0.0)
        self.job.side = side
        if self.ta_mode:
            self._ta_cover(self.job)
        self.replay_cmds = []
        self.job.tick_enabled = self.tick_mode
        self.job.speed_ticks = int(self.tick_speed) * 20
        self.job.liquid_idx = []          # prep 收尾时按实际扫描结果填
        self.job.freeze = bool(self.freeze)
        if self.job.freeze:
            self._freeze_off()         # 先把上一次可能残留的规则还原，再按
            self._freeze_on()          # 尽量把随机刻/昼夜/天气按住，结束恢复
        # 坐标 -> 原本该放什么方块（写实体后拿它核对/还原）。
        # 只关心"有方块实体的那几十个坐标"，所以不建整张结构的表（一百多万块
        # 建一次表就是一秒的主线程卡顿）：把实体坐标做成集合，prep 扫块时顺路
        # 命中就记下来，见 _step_prep 阶段 0。
        self.job.blk_at = {}
        self.job.need_blk_at = bool(ents)
        if ents:
            self.job.ent_pos = set([(e[0], e[1], e[2]) for e in ents])
        else:
            self.job.ent_pos = None
        # 聊天框只报"开始/结束"这种低频消息，过程全交给下方 tip 小栏
        self.tell(pid, u"§a开始放置 §f%s §7· %d×%d×%d · %d 方块 · 旋转 %d" %
                  (util.short(entry.rel, 36), size[0], size[1], size[2],
                   len(blocks), int(rotation) % 4))
        bits = []
        if len(sections) > 1:
            bits.append(u"%s %d 块" % (u"分块" if use_chunk else u"分区", len(sections)))
        if self.tick_mode:
            bits.append(u"每区等 %d 秒" % self.tick_speed)
        if ents:
            bits.append(u"方块实体 %d" % len(ents))
        if mobs:
            bits.append(u"实体 %d" % len(mobs))
        if self.job.liquid_idx:
            bits.append(u"液体 %d（最后放）" % len(self.job.liquid_idx))
        if getattr(data, "waterlogged", 0):
            bits.append(u"含水方块 %d" % data.waterlogged)
        # 跳过数要等 prep 扫完才知道，那会在 _prep_finish 里用 note 补一句
        est = self._estimate_secs(len(blocks), ents, wake, sections, mobs)
        if self.tick_mode:
            est += len(sections) * self.tick_speed
        if self.freeze and self.job.liquid_idx:
            est += self.freeze_secs
        bits.append(u"预计耗时 %.1f 秒" % est)
        self.tell(pid, u"§7" + u" · ".join(bits))
        self.push(pid, 0)          # prep 阶段跑完后自己进第一个分区

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
                              "anchor": (cx0, cz0), "ta": None})

        # ---- 玩家面前搭地图墙
        self._wall_frames = 0
        built = self._build_map_wall(job.pid, cols, rows)
        if built is None:
            self.tell(job.pid, u"§c地图墙搭建失败§7（%s），这次不画地图了（地图没墙可挂，画了也是白图）" %
                      util.short(util.u(getattr(self, "_wall_err", u"") or u"未知原因"), 40))
            return
        frames, wall_info = built
        ok_frames = len([f for f in frames if f])
        if ok_frames == 0:
            self.tell(job.pid, u"§c地图墙一个展示框都没放上去§7（目标位置被占 / 这个版本认不到展示框），"
                               u"已跳过挂画。换个空地朝墙的方向再试")
            return
        if ok_frames < n:
            self.tell(job.pid, u"§e只放上 §f%d/%d §e个展示框，多出来的地图会直接放进背包" %
                      (ok_frames, n))
        self.map_queue = {"pid": job.pid, "title": util.short(
            util.u(job.title), 20), "cells": cells, "i": 0, "n": n,
            "cols": cols, "rows": rows, "frames": frames, "stage": "zone",
            "retry": 0, "dim": job.dim, "areas": [], "maps": [],
            "wall": wall_info,
            "y": job.base[1] + max(ys) + 6.0}
        self._map_cd = 0
        if self.map_speed >= 1:
            speed_txt = u"§a快" if self.map_speed == 1 else u"§c极速"
            self.tell(job.pid, u"§a地图墙已搭好（%d列×%d行，%s§a档）。逐张画：每张分 §f%d §a个巡逻点、"
                               u"常加载覆盖整幅（mapspeed 可调档，缺角就调回 0）…" %
                      (cols, rows, speed_txt, min(self.zones_n, dv * dv)))
        else:
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
            placed = 0
            for r in range(rows):               # r=0 顶行
                for c in range(cols):
                    fc = c - (cols - 1) // 2
                    fr = (rows - 1) - r
                    fp = (ax + pv[0] * fc, ay + fr, az + pv[2] * fc)
                    sp = (fp[0] + v[0], fp[1] + v[1], fp[2] + v[2])
                    bi.SetBlockNew(sp, {"name": "minecraft:polished_andesite", "aux": 0}, 0, 0, True, False)
                    # 展示框：不同版本 id 不一样（frame / item_frame），两个都试
                    ok = False
                    for fname in (u"minecraft:frame", u"minecraft:item_frame"):
                        try:
                            if bi.SetBlockNew(fp, {"name": fname,
                                                   "aux": self._MAP_FACE[view]}, 0, 0, True, False):
                                ok = True
                                break
                        except Exception:
                            continue
                    if ok:
                        frames[r * cols + c] = fp
                        placed += 1
            self._wall_frames = placed
            # 观景位：墙中心前方 8 格（玩家站这里时整面墙区块都是加载的）
            view_pos = (ax - v[0] * 8, ay + rows / 2.0, az - v[2] * 8)
            return frames, view_pos
        except Exception:
            tb = traceback.format_exc()
            util.log(u"地图墙异常\n" + util.u(tb))
            try:
                self._wall_err = util.u(tb.strip().splitlines()[-1])
            except Exception:
                self._wall_err = u""
            return None

    # ---- 速度档取等待时间
    def _map_zone_wait(self):
        """巡逻每个点停多少 tick"""
        if self.map_speed >= 2:
            return cfg.MAP_ZONE_WAIT_FAST
        if self.map_speed == 1:
            return cfg.MAP_ZONE_WAIT
        return max(1, int(self.zone_secs)) * 20

    def _map_hold(self):
        """地图在主手停留多少 tick（引擎要在这段时间把地形画进图里）"""
        if self.map_speed >= 2:
            return cfg.MAP_HOLD_FAST
        if self.map_speed == 1:
            return cfg.MAP_HOLD_TICKS
        return max(1, int(self.zone_secs)) * 20

    def _map_first_wait(self, fresh):
        """进入一张新图时等多久：常加载早挂好了就短，现挂的才等"""
        if self.map_speed >= 2:
            return 10 if fresh else cfg.MAP_ZONE_WAIT_FAST
        if self.map_speed == 1:
            return cfg.MAP_TA_WAIT_FAST if fresh else cfg.MAP_ZONE_WAIT
        return cfg.MAP_TA_WAIT

    def _map_give_wait(self):
        return 10 if self.map_speed == 0 else 4

    # ---- 常加载：方形精确覆盖整幅
    def _map_ta_add(self, q, i):
        """给第 i 张图的单元挂常加载

        以前用 circle 半径 4：圆角会切掉 128×128 的四个角，所以必须靠巡逻补画。
        改成方形刚好盖住整幅，巡逻点就可以压到最少。
        """
        try:
            cell = q["cells"][i]
        except IndexError:
            return False
        if cell.get("ta"):
            return True
        cmd = self._command()
        if not cmd:
            return False
        cx0, cz0 = cell["anchor"]
        ms = cfg.MAP_SIZE
        y = int(q["y"])
        half = cfg.MAP_SQ_Y // 2
        name = u"si_map_%d" % cell["k"]
        try:
            # 稳档（speed 0）一律走老的圆形常加载：这是实测能用、挂画不出错的那条路。
            # 只有快/极速档才用方形（方形一次盖满，配合提前挂载才吃得下 tick 级等待）。
            if self.map_area and self.map_speed >= 1:
                cmdstr = (u"/tickingarea add %d %d %d %d %d %d %s" %
                          (cx0, y - half, cz0, cx0 + ms - 1, y + half, cz0 + ms - 1, name))
            else:
                # 老写法：圆形常加载（四角盖不到，得靠巡逻补画；纯兼容用）
                cmdstr = (u"/tickingarea add circle %d %d %d %d %s" %
                          (cx0 + ms // 2, y, cz0 + ms // 2, cfg.MAP_TA_RADIUS, name))
            ok = bool(cmd.SetCommand(util.b(cmdstr)))
        except Exception:
            ok = False
        if ok:
            cell["ta"] = name
        return ok

    @staticmethod
    def _inv_item(item, default=None):
        """把 GetPlayerAllItems 的物品字典转成"能塞回背包"的字典

        坑：GetPlayerAllItems 给的是 newItemName/newAuxValue 风格，
        SetPlayerAllItems / SpawnItemToPlayerInv 要的是 itemName/count/auxValue。
        两套键名混用引擎只会丢一行 WARNING（spawn item dict to inv failed!），
        物品当场消失，代码还毫不知情 —— 所以统一从这里过一道。
        """
        d = item if isinstance(item, dict) else {}
        name = d.get("itemName") or d.get("newItemName") or default
        if not name:
            return None
        try:
            cnt = int(d.get("count") or 1)
        except (TypeError, ValueError):
            cnt = 1
        try:
            aux = int(d.get("auxValue", d.get("newAuxValue", 0)) or 0)
        except (TypeError, ValueError):
            aux = 0
        out = {"itemName": name, "count": max(1, cnt), "auxValue": aux}
        ud = d.get("userData")
        if isinstance(ud, dict) and ud:
            out["userData"] = ud
        return out

    def _map_to_inv(self, pid, item):
        """把一张地图塞进玩家背包（墙没搭成时的兜底）"""
        payload = self._inv_item(item, u"minecraft:filled_map")
        if payload is None:
            payload = {"itemName": "minecraft:filled_map", "count": 1, "auxValue": 0}
        try:
            it = self.factory.CreateItem(pid)
            if it.SpawnItemToPlayerInv(payload, pid):
                return True
        except Exception:
            util.log_exc("map to inv")
        return False

    def _frame_ready(self, bi, pos, dim):
        """展示框那格是不是真的加载着

        往未加载区块里的展示框写数据，引擎会拿到悬空的方块实体指针直接崩
        （NonOwnerPointer dangling），所以每次写之前先读一次。
        判据放宽：只要读得出一个非空气方块就算加载了 —— 不同版本展示框的
        名字不完全一样（frame / item_frame），卡名字会误判成没加载。
        """
        try:
            b = bi.GetBlockNew(pos, dim)
        except Exception:
            return False
        if not b:
            return False
        name = b.get("name") or b.get("newBlockName") or u""
        name = util.u(name)
        if name.startswith(u"minecraft:"):
            name = name[10:]
        return name not in (u"", u"air", u"void_air", u"structure_void")

    def _frames_ready(self, q):
        """抽三张（首/中/尾）看看墙是不是加载好了"""
        frames = [f for f in (q.get("frames") or []) if f]
        if not frames:
            return True
        bi = self._blocks()
        if bi is None:
            return False
        dim = q.get("dim", 0)
        picks = [frames[0], frames[len(frames) // 2], frames[-1]]
        for fp in picks:
            if not self._frame_ready(bi, fp, dim):
                return False
        return True

    def _map_ta_del(self, q, i):
        try:
            cell = q["cells"][i]
        except IndexError:
            return
        name = cell.get("ta")
        if not name:
            return
        cmd = self._command()
        if cmd:
            try:
                cmd.SetCommand(util.b(u"/tickingarea remove %s" % name))
            except Exception:
                pass
        cell["ta"] = None

    def _map_prefetch(self, q, i):
        """画第 i 张时，把第 i+1 张的常加载先挂上，轮到时区块已就绪"""
        if not cfg.MAP_PREFETCH or self.map_speed < 1:
            return
        nxt = i + 1
        if nxt < 0 or nxt >= len(q["cells"]):
            return
        if q["cells"][nxt].get("ta"):
            return
        try:
            self._map_ta_add(q, nxt)
        except Exception:
            util.log_exc("map prefetch")

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
                # 地图由携带者持续绘制周围地形：一区就 give，跟着人走完巡逻点，
                # 走完才收回来挂墙。常加载保证整幅单元区块都加载着。
                fresh = not cell.get("ta")
                got_ta = self._map_ta_add(q, q["i"]) if fresh else True
                # 下一张的常加载现在就开始挂（与这张的绘制重叠，省掉一次等区块）
                self._map_prefetch(q, q["i"])
                cx, cz = cell["zones"][0]
                self.factory.CreatePos(pid).SetPos((cx, q["y"], cz))
                q["stage"] = "give"
                q["retry"] = 0
                if fresh and not got_ta:
                    # 常加载挂不上（世界已有 10 个区域 / 同名残留）：多等一会儿再给图
                    self._map_cd = cfg.MAP_ZONE_WAIT * 3
                else:
                    self._map_cd = self._map_first_wait(fresh)
                return
            if q["stage"] == "give":
                cell = q["cells"][q["i"]]
                cell["zi"] = 1                      # 一区已站；实测前 3 区就能画满整幅
                q["stage"] = "give2"
                self._map_cd = self._map_give_wait()
                return
            if q["stage"] == "give2":
                cmd = self._command()
                if not cmd or not cmd.SetCommand(util.b(u'/give @a filled_map 1')):
                    self.map_queue = None
                    return
                q["stage"] = "equip"
                self._map_cd = self._map_give_wait()    # 等 give 到包
                return
            if q["stage"] == "equip":
                # 地图必须先在玩家主手里停留渲染，引擎才把地形画上去；
                # give 后直接挂墙就是空白图。占住选中槽（备份原物品），渲染完恢复。
                found = self._find_new_map(pid)
                if found is None:
                    q["retry"] += 1
                    # 极速档只等 4 tick，背包同步慢一点就会漏；重试逐步拉长间隔
                    if q["retry"] <= 5:
                        q["stage"] = "give2"
                        self._map_cd = 6 + 4 * q["retry"]
                        return
                    self.tell(pid, u"§c第 %d 张地图生成失败，跳过" % q["cells"][q["i"]]["k"])
                    self._map_ta_del(q, q["i"])
                    q["i"] += 1
                    q["stage"] = "zone"
                    self._map_cd = 2
                    return
                mu, slot, mapd = found
                try:
                    it = self.factory.CreateItem(pid)
                    # 主手槽号：优先问引擎，问不到就用地图实际落到的那个槽
                    try:
                        sel = int(it.GetSelectSlotId())
                    except Exception:
                        sel = int(slot or 0)
                    items = it.GetPlayerAllItems(0, True) or []
                    q["restore"] = dict(items[sel]) if sel < len(items) and items[sel] else {}
                    # 地图字典也是 newItemName 风格，得先转一手才塞得回去
                    it.SetPlayerAllItems({(0, sel): self._inv_item(mapd, u"minecraft:filled_map")})
                    q["map_uuid"] = mu
                    q["hold_slot"] = sel
                    q["stage"] = "patrol"
                    self._map_cd = self._map_hold()
                except Exception:
                    tb = traceback.format_exc()
                    util.log(u"equip map\n" + util.u(tb))
                    q["map_uuid"] = mu
                    q["hold_slot"] = slot
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
                self._map_cd = self._map_zone_wait()
                return
            if q["stage"] == "warm":
                if not any(q.get("frames") or []):
                    # 一面墙都没搭成：地图全部塞背包，别让它们凭空消失
                    for _k, _it in (q.get("maps") or []):
                        self._map_to_inv(pid, _it)
                    self.tell(pid, u"§e地图墙没搭成，§f%d §e张地图已放进背包" %
                              len(q.get("maps") or []))
                    self.map_queue = None
                    return
                # 回墙后别急着写展示框：墙那一片区块可能还没加载完，
                # 对未加载区块里的展示框写数据会让引擎崩（dangling 指针）。
                q["warm"] = int(q.get("warm") or 0) + 1
                if self._frames_ready(q):
                    q["stage"] = "mount"
                    q["i"] = 0
                    self._map_cd = 1
                    return
                if q["warm"] > 200:                 # 10 秒还加载不出来，别硬写
                    self.tell(pid, u"§c地图墙区块一直没加载，地图已发放到背包，可自行贴到展示框")
                    self.map_queue = None
                    return
                self._map_cd = 10
                return
            if q["stage"] == "mount":
                # 批量挂框：原来一张 5 tick，10 张就是 2.5 秒白等
                bi = self._blocks()
                be = self._blockEnt()
                dim = q.get("dim", 0)
                if be is None or bi is None:
                    self.tell(pid, u"§c取不到方块实体组件，地图已发放到背包，请自行贴到展示框")
                    self.map_queue = None
                    return
                batch = max(1, cfg.MAP_MOUNT_BATCH if self.map_speed >= 1 else 1)
                bad = 0
                while batch > 0 and q["i"] < len(q["maps"]):
                    k, item = q["maps"][q["i"]]
                    fp = q["frames"][k - 1] if q["frames"] else None
                    if fp is not None:
                        # 关键：用引擎自己产出的物品 dict（含正确 aux 地图编号与完整
                        # userData）；自己拼 dict 引擎会当空白图渲染
                        if self._frame_ready(bi, fp, dim):
                            try:
                                if not be.SetFrameItem(fp, dim, dict(item)):
                                    bad += 1
                            except Exception:
                                bad += 1
                                util.log_exc("SetFrameItem")
                        else:
                            bad += 1
                    q["i"] += 1
                    batch -= 1
                if q["i"] >= len(q["maps"]):
                    if bad:
                        self.tell(pid, u"§e地图墙挂好 %d 张，§c%d 张没挂上§7（展示框被拆/区块刚加载），"
                                       u"其余地图在背包里" % (len(q["maps"]) - bad, bad))
                    else:
                        self.tell(pid, u"§a地图墙挂好了，凑近看就是完整的一幅画")
                    self.map_queue = None
                    return
                self._map_cd = 1
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
                    back = self._inv_item(q.get("restore")) or \
                        {"itemName": "minecraft:air", "count": 0, "auxValue": 0}
                    it.SetPlayerAllItems({(0, sel): back})
            except Exception:
                util.log_exc("restore hand")
            if held is not None:
                if q["frames"] and q["frames"][k - 1] is not None:
                    q["maps"].append((k, held))     # 引擎原生 dict，挂框时原样塞回
                else:
                    # 墙上没位置（墙没搭成/这张的框被拆了）：地图塞回背包，别弄丢
                    self._map_to_inv(pid, held)
                    self.tell(pid, u"§7第 %d 张地图放进背包（墙上没位置）" % k)
            elif mu is not None:
                self._map_to_inv(pid, {"itemName": "minecraft:filled_map", "count": 1,
                                       "auxValue": 0, "userData": {"map_uuid": mu}})
            else:
                self.tell(pid, u"§c第 %d 张地图生成失败，跳过" % k)
            # 这张图发完，撤掉它的常加载区块，顺手把下一张的挂上（等待与绘制重叠）
            self._map_ta_del(q, q["i"])
            self._map_prefetch(q, q["i"])
            while q["areas"]:                       # 旧版本残留的 circle 常加载
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
                q["stage"] = "warm"
                q["warm"] = 0
                q["i"] = 0
                w = q.get("wall")
                if w:
                    try:
                        self.factory.CreatePos(pid).SetPos(w)
                    except Exception:
                        util.log_exc("tp to wall")
                self._map_cd = (cfg.MAP_MOUNT_WAIT if self.map_speed >= 1 else 60)
                return
            q["stage"] = "zone"
            self._map_cd = 2
        except Exception:
            tb = traceback.format_exc()
            util.log(u"give maps\n" + util.u(tb))
            # 出错时把已经画好的地图塞回背包，并把原因说出来（静默失败最难查）
            try:
                for _k, _it in (q.get("maps") or []):
                    self._map_to_inv(pid, _it)
            except Exception:
                pass
            try:
                self.tell(pid, u"§c地图阶段出错：§f" +
                          util.short(util.u(tb.strip().splitlines()[-1]), 48))
            except Exception:
                pass
            try:
                for _ci in _range(len(q.get("cells") or [])):
                    self._map_ta_del(q, _ci)
            except Exception:
                pass
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

    def _pick_split_size(self, blocks):
        """分区边长的初值

        真正的定稿在 prep 首帧（见 _decide_side）：这里只处理"用户给了固定边长"
        和"关掉自适应"两种情况。自适应时返回最小边长占位，等直方图算出来再改，
        避免在主线程里为了估密度先扫一遍 115 万块。
        """
        custom = int(getattr(self, "split_size", 0) or 0)
        if custom > 0:
            return max(int(cfg.SPLIT_SIZE), min(int(cfg.SPLIT_SIZE_MAX), custom))
        return max(1, int(cfg.SPLIT_SIZE))

    def _decide_side(self, hist):
        """按实测直方图选分区边长：区数尽量少，但单个分区不超过 1.5 倍目标量

        旧算法用"水平包围盒面积反推平均密度"，对竖直堆叠的建筑完全失真：
        实测象牙城堡 190×148×239 / 115 万块，估出 side=32，单区 6.9 万块，
        是 SECTION_TARGET(1.6 万) 的 4.3 倍——一区要写好几秒，中途玩家一走
        区块就卸，风险全压在这几个热点区上。

        这里改成从粗到细逐个边长试算，用 16×16 区块列的直方图做区域求和，
        第一个"最大区 ≤ 上限"的就采纳（从粗往细扫，天然是区数最少的解）。

        返回 (边长, 是否必须分区)。第二个值专门用于"自适应结果刚好等于默认
        边长"的情况：以前 _prep_finish 用 `side > SPLIT_SIZE` 判断该不该分区，
        细分到 16 时会被当成"不用分区"，一百多万块一口气全塞进一个区——
        远处区块根本没加载，写了也白写。
        """
        if not cfg.SPLIT_AUTO or not hist:
            return max(1, int(cfg.SPLIT_SIZE)), False
        target = max(256, int(cfg.SECTION_TARGET))
        cap = int(target * 1.5)
        lo = max(16, int(cfg.SPLIT_SIZE))
        lo = ((lo + 15) // 16) * 16
        hi = max(lo, int(cfg.SPLIT_SIZE_MAX))
        total = 0
        for v in hist.values():
            total += v
        best_side, best_max = lo, None
        side = hi
        need = False
        while side >= lo:
            cells = side // 16
            g = {}
            for key, cnt in hist.items():
                k = (key[0] // cells, key[1] // cells)
                g[k] = g.get(k, 0) + cnt
            mx = max(g.values()) if g else 0
            if best_max is None or mx < best_max:
                best_max, best_side = mx, side
            if mx <= cap:
                # 够大且确实会切成多块，才值得为 TP 付代价
                need = (total >= target) and len(g) > 1
                return side, need
            side -= 16
        # 全都不达标（极端密实的立体结构）：退回最大区最小的那个
        return best_side, (total >= target)

    def _build_sections(self, blocks, wake, ents, use_chunk=None, side=None):
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
        # 液体（水/岩浆）不进分区流程：全部固体写完、实体写完，最后统一一次性放水。
        # 先放水再放方块 = 水先流开、方块再插进去，湖就成了一坨岩浆糕。
        liq = [i for i in _range(len(blocks)) if blocks[i][3] in cfg.LIQUID_BLOCKS]
        self._liquid_idx = liq
        liqset = set(liq)
        size = max(1, int(side or cfg.SPLIT_SIZE))
        # 自适应边长被放大（>16）本身就是"这是个该分区的大结构"的信号：
        # 不分区就没人跟着 TP，768 格外的区块根本没加载，写多少失败多少。
        if not (self.split or self.tick_mode or use_chunk or size > cfg.SPLIT_SIZE):
            blk = [i for i in _range(len(blocks)) if i not in liqset]
            return [{"blocks": blk,
                     "wake": wake,
                     "ents": _range(len(ents)),
                     "label": u"全部"}]

        groups = {}

        def group(x, z):
            key = (int(x) // size, int(z) // size)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"blocks": [], "wake": [], "ents": [],
                                   "label": u"%d,%d" % key}
            return g

        for i in _range(len(blocks)):
            if i in liqset:
                continue
            b = blocks[i]
            group(b[0], b[2])["blocks"].append(i)
        for i in wake:
            if i in liqset:
                continue
            b = blocks[i]
            group(b[0], b[2])["wake"].append(i)
        for i in _range(len(ents)):
            e = ents[i]
            group(e[0], e[2])["ents"].append(i)

        if not use_chunk:
            # 只有液体/没有方块的分组直接丢掉（否则会跑一堆空分区白等区块）
            out = [groups[k] for k in sorted(groups.keys())
                   if groups[k]["blocks"] or groups[k]["ents"]]
            # 整张结构都是水时一个分组都不剩：给个空分区占位，
            # 否则分区流程直接结束，最后那批液体永远不会被放出来
            return out or [{"blocks": [], "wake": [], "ents": [], "label": u"全部"}]

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
        return out or [{"blocks": [], "wake": [], "ents": [], "label": u"全部"}]

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
        """【已废弃，保留备查】states -> aux 的翻译现在在 _step_prep 阶段 0 分帧做

        原来这个函数在 _begin_job 里对全表一次跑完，一百多万块要一两秒，
        直接把主线程顶住。分帧版还会顺手统计分区直方图、压缩跳过的运行时方块，
        见 _step_prep。这里留着是因为它把"为什么这么翻"写清楚了：

        顺手做两件"让所有 NBT 都能导"的事：
          1. 老名字换成引擎认的（flowing_water -> water 等），否则永远写失败
          2. states 键去掉 minecraft: 前缀、布尔转 0/1，含水方块才认得出来
        """
        cache = {}
        out = []
        append = out.append
        skipped = 0
        for (x, y, z, name, states) in blocks:
            alias = cfg.BLOCK_ALIAS.get(name)
            if alias:
                name = alias
            if name in cfg.SKIP_BLOCKS:
                skipped += 1
                continue
            if states:
                states = _norm_states(states)
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
                        aux = int(self._states().GetBlockAuxValueFromStates(
                            _native(name), _eng_states(states)))
                    except Exception:
                        aux = 0
                cache[key] = aux
            append((x, y, z, name, aux, states))
        self._skipped = skipped
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
        """lang 方案失效时兜底：直接把名字写进物品 userData

        只在"确实发现没名字的说明书"时才动背包，而且失败 3 次就永久收手：
        以前是无条件每 2 秒 SetPlayerAllItems 一次，字典键名还是错的，
        引擎就一直刷 "spawn item dict to inv failed"，日志几秒一条刷到崩。
        """
        self._mfix_cd = getattr(self, "_mfix_cd", 0) - 1
        if self._mfix_cd > 0:
            return
        # 每 10 秒看一次就够：这只是"lang 没生效时补物品名"的兜底，
        # 以前 2 秒一次，导入过程中每 40 tick 就摸一遍全背包（GetPlayerAllItems
        # 是整包序列化，玩家背包塞满时这一下就能看出顿感）。
        self._mfix_cd = 200
        if getattr(self, "_mfix_fail", 0) >= 3:
            return
        try:
            players = serverApi.GetPlayerList()
        except Exception:
            return
        for pid in players or []:
            if pid in getattr(self, "_mfix_done", ()):
                continue
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
                    nx = self._inv_item(x, u"si:manual")
                    if nx is None:
                        continue
                    d = dict(nx.get("userData") or {})
                    d["display"] = {"Name": u"§e结构导入说明书§r\n§f右键查看教程"}
                    nx["userData"] = d
                    changed[(0, i)] = nx
                if not changed:
                    continue
                if it.SetPlayerAllItems(changed):
                    if not hasattr(self, "_mfix_done"):
                        self._mfix_done = set()
                    self._mfix_done.add(pid)
                else:
                    self._mfix_fail = getattr(self, "_mfix_fail", 0) + 1
            except Exception:
                self._mfix_fail = getattr(self, "_mfix_fail", 0) + 1

    def on_tick(self, args=None):
        self._manual_fix_tick()
        job = self.job
        if job is None:
            if self._pj is not None:
                self._parse_step()          # 主线程分帧解析：每帧只花几毫秒
                return
            if self._bg is not None:
                self._poll_bg()
                return
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
            _t0 = time.time()
            self._step(job)
            self._guard((time.time() - _t0) * 1000.0)
        except Exception:
            tb = traceback.format_exc()
            util.log(u"tick 异常\n" + util.u(tb))
            self._clear_areas(job)
            self.job = None
            self._gc_restore()
            self.tp_route = None
            self._freeze_off()
            self._tip_raw(u"")
            self.tell(job.pid, u"§c放置中断：§f" +
                      util.short(util.u(tb.strip().splitlines()[-1]), 54))
            self.push(job.pid, self._page)

    def _step(self, job):
        comp = self._blocks()
        if comp is None:
            self.job = None
            self._gc_restore()
            self._freeze_off()
            self.tell(job.pid, u"§c取不到方块组件（CreateBlockInfo 返回空）")
            self.push(job.pid, self._page)
            return

        job.tick += 1
        if job.waiting:
            if job.tick < job.wait_until:
                # 就绪探测：区块已经加载好就不用把剩余 tick 等满（快）
                if getattr(job, "ready_probe", False) and self._section_ready(job):
                    job.waiting = False
                    job.ready_probe = False
                else:
                    if job.tick % 10 == 0:
                        self._show_tip(job)      # 等区块时也刷进度
                    return
            job.waiting = False
            job.ready_probe = False
        job.work_ticks += 1
        if job.tick % 10 == 0 or job.tick == 1:
            self._show_tip(job)
        if job.phase == "prep":
            self._step_prep(job)
        elif job.phase == "blocks":
            self._step_blocks(job, comp)
        elif job.phase == "retry":
            self._step_retry(job, comp)
        elif job.phase == "wake":
            self._step_wake(job, comp)
        elif job.phase == "ents":
            self._step_ents(job, comp)
        elif job.phase == "items":
            self._step_items(job)
        elif job.phase == "mobs":
            self._step_mobs(job)
        elif job.phase == "liquid":
            self._step_liquid(job, comp)
        elif job.phase == "wash":
            self._step_wash(job, comp)
        elif job.phase == "seal":
            self._step_seal(job, comp)
        elif job.phase == "verify":
            self._step_verify(job, comp)
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
        """写一个方块（block 字典可复用：同一分区里同名同 aux 的方块共用一个）

        states 尽量和方块一起交给引擎：含水方块 / 液体分成"先放方块、再补状态"
        两步写，中间那一瞬引擎看到的是干方块/无源水，会立刻触发一次流动，
        等 SetBlockStates 补上去时水已经流走了——真机上"水到处乱跑"就是这么来的。
        """
        b = block
        if states and "states" not in b:
            b = dict(block)
            b["states"] = states
        try:
            ok = comp.SetBlockNew(pos, b, 0, dim, True, bool(update_neighbors))
        except TypeError:
            try:
                ok = comp.SetBlockNew(pos, b, 0, dim)
            except Exception:
                return False
        except Exception:
            return False
        if states:
            # 老版本引擎不认 block 里的 states 时，这一次补写就是唯一生效路径。
            # （高频重试的场景要传 states=None，方块字典里已经带状态了，
            #   再补一次等于多排一个方块刻，几万格水连补几秒会把引擎顶爆）
            try:
                self._states().SetBlockStates(pos, _eng_states(states), dim)
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
        scale = float(getattr(self, "_guard_scale", 1.0) or 1.0)
        return b * scale if scale < 1.0 else b

    def _guard(self, used_ms):
        """单帧护栏：这一步花超了就自动降速，绝不让"导入把游戏卡住一秒"发生

        正常走时间预算不会超；但引擎某次写特别慢、或一次刷新了几百个方块实体时，
        单帧还是可能超。这里兜底：超了就把下几帧的写入预算与批量一起压小，
        连续没超再慢慢放回去（0.5 倍速度恢复，避免忽快忽慢）。
        """
        if used_ms >= cfg.TICK_GUARD_MS:
            self._guard_scale = max(0.25, self._guard_scale * 0.6)
            job = self.job
            if job is not None:
                job.batch = max(cfg.WRITE_BATCH_MIN, int(job.batch * 0.6))
            util.log(u"单帧 %.0f ms 超护栏，自动降速到 %.0f%%" %
                     (used_ms, self._guard_scale * 100))
        elif self._guard_scale < 1.0:
            self._guard_scale = min(1.0, self._guard_scale * 1.08)

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
        """分区级提示：不进聊天框

        几百个分区、每个分区一条，聊天框会被刷成瀑布（消息本身还掉帧）。
        这里只把状态记到 job.note，下一次 tip 刷新时作为一小段附在下方小栏里。
        """
        job.note = text

    def _step_prep(self, job):
        """写入前的扫描与分区（分帧做，一帧最多 PREP_SLICE_MS 毫秒）

        每块要判：是不是液体、归哪个分区、要不要进红石唤醒批次、要不要进补水批次。
        这几步都是字符串子串匹配，几十万块乘二十多个关键字，一次性跑完就是
        "点下去卡一下"。分帧之后总时长只多几帧，但没有任何一帧会卡。
        """
        st = job.prep
        blocks = job.blocks
        n = len(blocks)
        groups = st["groups"]
        wake = st["wake"]
        wash = st["wash"]
        liq = st["liq"]
        # 阶段 0：翻 aux（states -> aux）+ 统计 16×16 区块列直方图 + 收集方块名
        # 一次遍历做完三件事，全程分帧。这是原来"点导入卡一两秒"的最大一块：
        # 一百多万块时，_with_aux 在主线程里要跑一两秒。
        # 跳过项（运行时方块）在这里就地压缩掉：写指针 w 永远不超过读指针，
        # 所以边读边写不会覆盖还没读到的块，也就不用再开一份新列表（省一倍内存）。
        if not st.get("auxed"):
            hist = st.get("hist")
            if hist is None:
                hist = st["hist"] = {}
                st["names"] = set()
                st["n0"] = n
                st["skipn"] = 0
                st["blk_at"] = {}
                # 边扫边建表：不再把 6 元组写回 list（那等于多留一份上百万元的清单），
                # 直接塞进四个整型数组 + 一张几百项的调色板
                st["parts"] = _new_table_parts()
            names = st["names"]
            ent_pos = getattr(job, "ent_pos", None)
            bat = st["blk_at"]
            parts = st["parts"]
            xs = parts["xs"]
            ys = parts["ys"]
            zs = parts["zs"]
            pid = parts["pid"]
            pal = parts["pal"]
            pmap = parts["pmap"]
            cache = st.get("aux_cache")
            if cache is None:
                cache = st["aux_cache"] = {}
            alias_map = cfg.BLOCK_ALIAS
            skip_set = cfg.SKIP_BLOCKS
            n0 = st["n0"]
            skipn = st["skipn"]
            cur = st["cursor"]
            t0 = time.time()
            while cur < n0:
                x, y, z, name, states = blocks[cur]
                alias = alias_map.get(name)
                if alias:
                    name = alias
                if name in skip_set:
                    skipn += 1
                    cur += 1
                    continue            # 直接丢掉：不占分区、不进直方图
                if states:
                    # 同一个调色板项的所有方块共用同一个 states 对象（si_nbt 里就
                    # 没复制），所以按对象身份缓存一次就够，不必每块重拼 key
                    ck = id(states)
                    hit = cache.get(ck)
                    if hit is not None:
                        states, aux = hit
                    else:
                        states = _norm_states(states)
                        aux = 0
                        try:
                            aux = int(self._states().GetBlockAuxValueFromStates(
                                _native(name), _eng_states(states)))
                        except Exception:
                            aux = 0
                        cache[ck] = (states, aux)
                else:
                    aux = 0
                pk = (name, aux, id(states))
                p = pmap.get(pk)
                if p is None:
                    p = len(pal)
                    pal.append((name, aux, states))
                    pmap[pk] = p
                pid.append(p)
                xs.append(x)
                ys.append(y)
                zs.append(z)
                if ent_pos is not None:
                    # 有方块实体时顺路记下"这个坐标原本是什么方块"，
                    # 写实体后要拿它核对/还原；只记命中的那几十个坐标
                    pos3 = (x, y, z)
                    if pos3 in ent_pos:
                        bat[pos3] = (name, aux, states)
                k = (x // 16, z // 16)
                hist[k] = hist.get(k, 0) + 1
                names.add(name)
                cur += 1
                # 每 32 块探一次（原来 256）：states 转 aux 要调引擎，个别块能到
                # 0.1ms 级，256 块才发现超时就是一帧 25ms 的尖峰
                if (cur & 31) == 0 and (time.time() - t0) * 1000.0 >= cfg.PREP_SLICE_MS:
                    break
            st["cursor"] = cur
            st["skipn"] = skipn
            if cur < n0:
                return
            st["auxed"] = True
            st["cursor"] = 0
            st["n0"] = None
            st["aux_cache"] = None
            st["parts"] = None
            job.blocks = BlockTable(xs, ys, zs, pid, pal, len(pid))
            self._skipped = skipn
            if cfg.SPLIT_AUTO and not (int(getattr(self, "split_size", 0) or 0) > 0):
                side, need = self._decide_side(hist)
                st["side"] = side
                st["auto_split"] = need
                job.side = side
            st["hist"] = None          # 定完边长就扔掉，别一直占着几十万个键
            return
        side = st["side"]
        # 关键提速：整张结构里方块名字的种类一般只有几十种，先按"唯一名字"
        # 把要不要唤醒/要不要补水算一次，逐块只做字典查表。
        # 400k 块 × 20 多个关键字子串判断 -> 400k 次 dict.get，差两个数量级。
        if "flags" not in st:
            flags = {}
            src = st.get("names")
            if not src:
                src = set([b[3] for b in blocks])
            for nm in src:
                low = nm.lower()
                wake_hit = False
                if not any(kw in low for kw in cfg.WAKE_SKIP):
                    for kw in cfg.REDSTONE_HINTS:
                        if kw in low:
                            wake_hit = True
                            break
                wash_hit = False
                for kw in cfg.WASH_HINTS:
                    if kw in low:
                        wash_hit = True
                        break
                flags[nm] = (nm in cfg.LIQUID_BLOCKS, wake_hit, wash_hit)
            st["flags"] = flags
        flags = st["flags"]
        no_flag = (False, False, False)
        t0 = time.time()
        budget = cfg.PREP_SLICE_MS
        cur = st["cursor"]
        while cur < n:
            x, y, z, name, aux, states = blocks[cur]
            is_liq, wake_hit, wash_hit = flags.get(name, no_flag)
            if is_liq:
                liq.append(cur)              # 液体最后单独放，不进分区
            else:
                key = (int(x) // side, int(z) // side)
                g = groups.get(key)
                if g is None:
                    g = groups[key] = {"blocks": [], "wake": [], "ents": [],
                                       "label": u"%d,%d" % key,
                                       "bb": [x, x, y, y, z, z]}
                else:
                    # 顺路把分区包围盒攒出来：换区时要它算 TP 落点和常加载范围。
                    # 以前是换区那一帧现遍历 1.6 万块（py2 下 5~10ms，正好卡在
                    # TP 同一帧上）；这里每块只多 6 次比较，换区变成 O(1) 查表。
                    bb = g["bb"]
                    if x < bb[0]:
                        bb[0] = x
                    elif x > bb[1]:
                        bb[1] = x
                    if y < bb[2]:
                        bb[2] = y
                    elif y > bb[3]:
                        bb[3] = y
                    if z < bb[4]:
                        bb[4] = z
                    elif z > bb[5]:
                        bb[5] = z
                g["blocks"].append(cur)
                if wake_hit:
                    # 门/铁轨这类多格方块在 flags 里已排除：带邻居更新重写会掉落
                    g["wake"].append(cur)
                    wake.append(cur)
                if wash_hit:
                    wash.append(cur)
            cur += 1
            # 每 32 块探一次时间，到点收手（粒度太粗会在换区/大结构上留尖峰）
            if (cur & 31) == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
        st["cursor"] = cur
        if cur < n:
            return
        # 实体归区（数量少，一遍过）
        ents = job.entities
        for i in _range(len(ents)):
            e = ents[i]
            key = (int(e[0]) // side, int(e[2]) // side)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"blocks": [], "wake": [], "ents": [],
                                   "label": u"%d,%d" % key, "bb": None}
            g["ents"].append(i)
        self._prep_finish(job)

    def _near_first_key(self, job, side):
        """分区排序键：离玩家近的先导

        以前按 (x, z) 字典序从角落扫起 —— 第一区永远在结构最远的那一头，
        开局必吃一次传送 + 区块等待，玩家盯着屏幕什么也看不到。
        结构是以玩家为中心放的（base 已经减掉 size/2），所以改成按到玩家的
        距离排序（自然形成螺旋）：前几个区就在脚下，区块本来就加载着，
        开局零等待，第一眼就能看见东西长出来。
        """
        try:
            p = self.factory.CreatePos(job.pid).GetFootPos()
            px = float(p[0]) - job.base[0]
            pz = float(p[2]) - job.base[2]
        except Exception:
            px = pz = None
        half = side / 2.0
        if px is None:
            # 拿不到坐标就退回"从中心往外"的等价写法（结构中心在 size/2）
            return lambda k: (abs(k[0]), abs(k[1]), k[0], k[1])
        return lambda k: ((k[0] * side + half - px) ** 2 +
                          (k[1] * side + half - pz) ** 2)

    def _prep_finish(self, job):
        """扫描完成后把分区定下来（这一步只剩排序，几十毫秒内）"""
        st = job.prep
        # 有方块实体时，prep 扫块那遍已经顺路把命中坐标记下来了（st["blk_at"]）
        job.blk_at = st.get("blk_at") or {}
        if self._skipped:
            # 这里以前写成 self.note(...)，而 note 只是 job 上的字符串字段，
            # 不是方法 —— 只要结构里有运行时方块被跳过（w(1) 1165 块、
            # 1.8人工商店 66 块）就整个导入中断。
            job.note = u"跳过运行时方块 %d" % self._skipped
        blocks = job.blocks
        side = st["side"]
        self._liquid_idx = st["liq"]
        if st["use_chunk"]:
            # 手动分块模式：仍按数据量再切 Y 带（少见的主动选择）
            sections = self._build_sections(blocks, st["wake"], job.entities,
                                            True, side)
        elif not (self.split or self.tick_mode or side > cfg.SPLIT_SIZE
                  or st.get("auto_split")):
            # 不分区：整张结构一个分区，液体单独挑出去最后放。
            # 没有液体时直接用 range，别为了"排除液体"把几十万个下标抄一遍：
            # 589824 块的结构，这个列表推导实测就是一整帧 28ms（t_perf 里的尖峰）。
            liq = st["liq"]
            if liq:
                liqset = set(liq)
                blk = [i for i in _range(len(blocks)) if i not in liqset]
            else:
                blk = _range(len(blocks))
            sections = [{"blocks": blk, "wake": st["wake"],
                         "ents": _range(len(job.entities)), "label": u"全部"}]
        else:
            keys = sorted(st["groups"].keys(), key=self._near_first_key(job, side))
            sections = [st["groups"][k] for k in keys
                        if st["groups"][k]["blocks"] or st["groups"][k]["ents"]]
            if not sections:
                # 整张都是水：留个空分区占位，否则流程直接结束，水永远不放
                sections = [{"blocks": [], "wake": [], "ents": [], "label": u"全部"}]
        job.sections = sections
        job.wake = st["wake"]
        job.wash_idx = st["wash"]
        job.liquid_idx = list(self._liquid_idx)
        job.prep = None
        job.sec_i = 0
        job.cursor = 0
        job.phase = "blocks"
        self._prepare_section(job)

    def _step_blocks(self, job, comp):
        sec = self._section(job)
        if sec is None:
            job.phase = "done"
            return
        idx = sec["blocks"]
        if getattr(job, "pb_cap", None) is None:
            job.pb_cap = bool(cfg.PALETTE_WRITE and self._pb_ready())
        if job.pb_cap and len(idx) >= int(cfg.PALETTE_MIN):
            self._step_blocks_palette(job, sec)
            return
        total = len(idx)
        written = 0
        bx, by, bz = job.base
        dim = job.dim
        blocks = job.blocks

        budget = self._write_budget()
        limit = max(cfg.WRITE_BATCH_MIN, job.batch)
        t0 = time.time()
        hit_time = False
        bdict = {}                         # (name, aux, states) -> 方块字典，本区复用
        # 快路径：几十万块的无状态方块（石/混凝土/木板…）直接调引擎，省掉
        # 每块一次 _place_block 的函数调用与参数打包。带 states 的才走慢路径。
        set_block = comp.SetBlockNew
        raw_ok = True

        while job.cursor < total and written < limit:
            i = idx[job.cursor]
            x, y, z, name, aux, states = blocks[i]
            job.cursor += 1
            written += 1
            try:
                key = (name, aux, id(states))
                blk = bdict.get(key)
                if blk is None:
                    blk = {"name": name, "aux": int(aux)}
                    if states:
                        # states 跟着方块一起给引擎，含水/液体不会出现中间态
                        blk["states"] = _eng_states(states)
                    bdict[key] = blk
                # 建造阶段不触发邻居更新：否则红石/火把会边放边被弹掉
                pos = (bx + x, by + y, bz + z)
                if not states and raw_ok:
                    try:
                        ok = set_block(pos, blk, 0, dim, True, False)
                    except TypeError:
                        raw_ok = False       # 这个版本签名不同，后面全都走 _place_block
                        ok = self._place_block(comp, pos, blk, states, dim, False)
                else:
                    ok = self._place_block(comp, pos, blk, states, dim, False)
                if ok:
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
            # 进度只走下方 tip 小栏，聊天框保持干净（不刷屏也不掉帧）
            return

        # 分区内部顺序：方块 ->（失败重试）-> 红石唤醒 -> 方块实体
        # 唤醒阶段会整块重写红石方块，写之前就把实体数据铺好会被清掉
        self._after_blocks(job, sec)

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

    def _queue_items(self, job, node, pos):
        """把容器里的物品排进队列（真正写入放到 items 阶段分帧做）"""
        entries = node if isinstance(node, list) else _val(node)
        if not isinstance(entries, list):
            return
        for entry in entries:
            item = _item_dict(entry)
            if not item:
                continue
            if item.get("trim"):
                job.item_trim += 1
            # (首选 plain, 备选 typed, 槽位, 位置)；typed 为 None 表示这件没有 NBT
            job.item_queue.append((item["plain"], item["typed"],
                                   item["slot"], pos))

    def _step_items(self, job):
        """分帧写入容器物品

        盒中盒结构单件物品 NBT 可达 2MB（全盒子十几 MB），一 tick 全灌进去
        会把主线程顶死。这里按时间预算摊到多个 tick，数据一点都不丢。

        写入要过三道关（制裁盒子那种"附魔 + 改名 + 盒中盒"全靠这个）：
            1. 先 plain（纯值 userData）——文档形态，命中率最高
            2. 引擎说成功但回读发现 userData 是空的 -> 说明被吞了，
               换 typed（叶子带 __type__ 包装）再写一次
            3. 还不行，把 userData 里的 Items/Block 摘掉只留 display+ench 再试，
               至少名字和附魔能留下
        """
        comp = self._items()
        if comp is None:
            job.item_queue = []
            job.item_cursor = 0
            self._next_section(job)
            return
        dim = job.dim
        budget = self._write_budget()
        t0 = time.time()
        # 用游标推进，不再 pop(0)：list.pop(0) 要把整个列表往前搬一遍，
        # 盒中盒那种几千件的容器是 O(n²)，越到后面每件越慢（真机导入后期发滞）。
        q = job.item_queue
        qn = len(q)
        cur = int(getattr(job, "item_cursor", 0) or 0)
        while cur < qn:
            plain, typed, slot, pos = q[cur]
            has_ud = isinstance(plain.get("userData"), dict) and bool(plain["userData"])

            # 核对键以 typed（原样 NBT）为准：它才是"完整写入"的那份
            want_keys = []
            for cand in (typed, plain):
                ud = cand.get("userData") if isinstance(cand, dict) else None
                if isinstance(ud, dict) and ud:
                    want_keys.extend(ud.keys())
                    break
            if plain.get("enchantData"):
                want_keys.append("enchantData")

            def try_write(cand):
                """写一次；带 NBT 的必须回读确认没被吞

                回读一定要 getUserData=True —— 少了这个参数，引擎返回的
                userData 永远是空，于是每件"其实写成功"的物品都被判成失败，
                白跑两轮降级还把 NBT 覆盖成裁剪版（曾经的真实故障）。
                """
                try:
                    if not comp.SpawnItemToContainer(cand, slot, pos, dim):
                        return False
                except Exception:
                    return False
                if not want_keys:
                    return True
                return self._item_ud_ok(comp, pos, slot, dim, want_keys)

            # 先写 typed（结构里的 NBT 原样），保真度最高：
            # 盒中盒、32767 级附魔、多层嵌套都靠这一版。
            ok = False
            if typed is not None:
                ok = try_write(typed)
            if not ok:
                ok = try_write(plain)
            if not ok and has_ud:
                # 第三道：只留看得见的字段（名字/附魔/耐久），别让整件物品消失
                slim = dict(plain)
                tight = _slim_item_tag(plain["userData"])
                if tight:
                    slim["userData"] = _native_deep(tight)
                    ok = try_write(slim)
                    if ok:
                        job.item_trim += 1
            if not ok:
                # 第四道：引擎根本不吃这件物品的 NBT（典型是一身 32767 级附魔、
                # moving_block 这种非法物品，日志会刷 spawn item failed!）。
                # 退到裸物品，至少东西还在、位置不空 —— 玩家能看见并自己处理。
                bare = {"itemName": plain["itemName"], "count": plain["count"],
                        "auxValue": plain.get("auxValue", 0)}
                try:
                    if comp.SpawnItemToContainer(bare, slot, pos, dim):
                        ok = True
                        job.item_bare += 1
                except Exception:
                    pass
            if ok:
                job.item_ok += 1
            else:
                job.item_fail += 1
            cur += 1
            # 单件就可能吃掉整个预算，至少保证每 tick 推进一件
            if (time.time() - t0) * 1000.0 >= budget and cur < qn:
                job.item_cursor = cur
                return
        job.item_queue = []
        job.item_cursor = 0
        self._next_section(job)

    def _item_ud_ok(self, comp, pos, slot, dim, want_keys=None):
        """回读容器格位，确认物品的 NBT 真的落进去了

        第 4 个参数 getUserData 必须显式传 True，否则引擎不返回 userData，
        所有写入都会被误判成"被吞了"。
        按 want_keys 逐个核对：只要有一半以上的键还在就算成功（引擎偶尔会
        补 modEnchant 这类字段，键集合不可能完全相等）。
        """
        try:
            cur = comp.GetContainerItem(pos, slot, dim, True)
        except Exception:
            return True
        if not isinstance(cur, dict):
            return False
        ud = cur.get("userData")
        if not want_keys:
            return isinstance(ud, dict) and bool(ud)
        hit = 0
        for k in want_keys:
            if k == "enchantData":
                if cur.get("enchantData"):
                    hit += 1
            elif isinstance(ud, dict) and k in ud:
                hit += 1
        return hit * 2 >= len(want_keys)

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
        # 每块要 3~4 次引擎调用（读实体数据 / 写实体数据 / 核对方块 / 命令方块），
        # 固定 200 块/tick 在密集仓库区能顶到上百毫秒。改成时间预算：
        # 到点就收手，剩下的下一 tick 接着写，一个方块都不会丢。
        budget = float(getattr(cfg, "ENT_SLICE_MS", 6.0))
        cap = int(getattr(cfg, "BATCH_ENT_CAP", cfg.BATCH_ENT))
        t0 = time.time()
        while job.ent_cursor < total and n < cap:
            x, y, z, nbt = job.entities[idx[job.ent_cursor]]
            job.ent_cursor += 1
            n += 1
            if (n & 3) == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
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
                # 容器内容整份塞进方块实体，一次写完：
                # 这是存档的原生形态，盒中盒、多层嵌套、32767 级附魔、
                # moving_block 这类非法物品全部原样保留 —— 逐件 SpawnItemToContainer
                # 会被引擎按物品规则校验并整件拒收（日志刷 spawn item failed!），
                # 而这里根本不走那条校验。
                if items:
                    payload["Items"] = _nbt_for_engine(items)
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
                # 整份写成功就不必逐件再写。
                # 判据只能是"读回来是空的"：引擎在写完瞬间读回可能少几件
                # （重复槽位会被合并），以前拿"件数相等"当判据，结果把
                # 刚写好的完整 NBT 又用裸物品覆盖了一遍（1.8人工商店 182 件全丢）。
                back = None
                try:
                    back = comp.GetBlockEntityData(dim, pos)
                except Exception:
                    back = None
                landed = (back or {}).get("Items") if isinstance(back, dict) else None
                if isinstance(landed, list) and landed:
                    job.item_ok += len(items)
                else:
                    self._queue_items(job, items, pos)

        if job.ent_cursor >= total:
            if job.item_queue:
                job.phase = "items"      # 单独一阶段，按时间预算分摊到多个 tick
                job.item_cursor = 0
            else:
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
        self._drop_pf(job, job.sec_i + 1)    # 只留还没轮到的预取区
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
        job.item_queue = []
        job.item_cursor = 0
        if job.sec_i >= len(job.sections):
            # 所有分区跑完 -> 实体 -> 液体 -> 守水 -> 收尾
            self._final_phase(job)
            return
        job.phase = "blocks"
        self._prepare_section(job)

    def _final_phase(self, job):
        """分区全部跑完后的收尾顺序：实体 -> 液体 -> 守水 -> 完成

        液体刻意排在最后：先放固体骨架，水灌进去才有"池底池壁"接着，
        否则水会顺着还没放方块的地方先流成一片。
        """
        if job.mobs and job.mob_cursor < len(job.mobs):
            job.phase = "mobs"
            return
        if job.liquid_idx and job.liq_cursor < len(job.liquid_idx):
            if job.phase != "liquid":
                job.phase = "liquid"
                job.seal_until = 0        # 液体全放完后再算守护截止时间
                if not job.liq_armed:
                    # 液体可能铺满整个结构，而人还停在最后一个分区：
                    # 先回结构中心把区块带起来，否则最后这一批水会写不进去
                    job.liq_armed = True
                    self._tp_structure_center(job)
                    job.waiting = True
                    job.wait_until = job.tick + (6 if self.turbo else 30)
                    job.note = u"§9准备放液体（%d 格）" % len(job.liquid_idx)
            return
        if job.wash_idx and job.wash_cursor < len(job.wash_idx):
            # 水放完后先补被冲掉的方块（火把/铁轨/告示牌/花…），再做守水巡检。
            # 顺序很重要：先让世界恢复成"结构本来的样子"，再去按住水。
            job.phase = "wash"
            return
        if job.freeze and job.sealed:
            if not job.seal_until:
                job.seal_until = job.tick + int(max(0.0, self.freeze_secs) * 20)
                job.seal_cursor = 0
            if job.tick < job.seal_until:
                job.phase = "seal"
                return
        # 收尾核对：读一遍数差异，只报不补（玩家要求：漏了也不用补）
        if self.verify and not getattr(job, "vf_done", False):
            job.phase = "verify"
            if getattr(job, "vf_cursor", 0) == 0 and \
                    not getattr(job, "vf_announced", False):
                job.vf_announced = True
                job.note = u"§9逐块核对（%d 块，只查不补）" % job.total
            return
        job.phase = "done"

    @staticmethod
    def _same_block(got_name, want_name):
        """回读比对用的名字归一：水/岩浆的流动变体算同一块"""
        if got_name == want_name:
            return True
        if not got_name or not want_name:
            return False
        if "water" in want_name:
            return "water" in got_name
        if "lava" in want_name:
            return "lava" in got_name
        return False

    def _step_verify(self, job, comp):
        """逐块回读：只点数，不补写

        引擎有三种情况"写返回成功但其实没写上"：区块还没完全加载、液体流动把方块
        冲掉、支撑判断把火把/铁轨弹掉。这里读一遍，把对不上的块数报出来，
        **但不自动补写** —— 补写会在液体/重力区域反复重建，跟"导入一次"的预期
        不符（实测霜塔补 63 块多花近 2 秒，象牙城堡这种量级代价更大）。
        要不要补由玩家自己决定，插件只负责把数字说清楚。

        读不到（区块没加载）不算缺失，单独计数上报。
        """
        total = job.total
        blocks = job.blocks
        bx, by, bz = job.base
        dim = job.dim
        t0 = time.time()
        budget = cfg.VERIFY_SLICE_MS
        cur = int(getattr(job, "vf_cursor", 0) or 0)
        miss = int(getattr(job, "vf_miss", 0) or 0)
        unloaded = 0
        while cur < total:
            x, y, z, name, aux, states = blocks[cur]
            cur += 1
            pos = (bx + x, by + y, bz + z)
            try:
                got = comp.GetBlockNew(pos, dim)
            except Exception:
                got = None
            if got is None:
                unloaded += 1            # 区块没加载：不是缺失，单独计
            else:
                gname = got.get("name") if isinstance(got, dict) else None
                if not self._same_block(gname, name):
                    miss += 1
            if (cur & 255) == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
        job.vf_cursor = cur
        job.vf_miss = miss
        job.vf_unloaded = unloaded
        if cur < total:
            return
        job.vf_done = True
        if miss:
            self.tell(job.pid, u"§e回读校验：§f%d 块与结构不一致§7（共 %d 块，不补写）" %
                      (miss, total))
        else:
            self.tell(job.pid, u"§a回读校验：§f%d 块全部对上" % total)
        if job.vf_unloaded:
            self.tell(job.pid, u"§7其中 %d 块因区块未加载没能复核" % job.vf_unloaded)
        self._final_phase(job)

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
            self._final_phase(job)

    def _tp_structure_center(self, job):
        """把人送回结构中心：放液体和守水都在结构内部，区块得先加载

        坐标范围用抽样算（大结构几十万方块，全量扫一遍会卡一下；抽样够用）。
        """
        if not job.pid or not job.blocks:
            return
        step = max(1, len(job.blocks) // 2000)
        xs, ys, zs = [], [], []
        for i in _range(0, len(job.blocks), step):
            b = job.blocks[i]
            xs.append(b[0])
            ys.append(b[1])
            zs.append(b[2])
        if not xs:
            return
        cx = job.base[0] + (min(xs) + max(xs)) / 2.0
        cz = job.base[2] + (min(zs) + max(zs)) / 2.0
        cy = job.base[1] + max(ys) + 6.0
        try:
            self.factory.CreatePos(job.pid).SetPos((cx, cy, cz))
        except Exception:
            util.log_exc("tp structure center")

    def _step_liquid(self, job, comp):
        """最后统一放液体（水 / 岩浆）

        所有固体方块都到位之后才灌水：池底、池壁已经实心，水就只会在原地待着。
        每格放过之后记下坐标和方块字典，守护阶段拿它原地重写（不放新东西，只按住）。
        """
        idx = job.liquid_idx
        total = len(idx)
        bx, by, bz = job.base
        dim = job.dim
        blocks = job.blocks
        budget = self._write_budget()
        limit = max(cfg.WRITE_BATCH_MIN, job.batch)
        t0 = time.time()
        n = 0
        sealed = job.sealed
        while job.liq_cursor < total and n < limit:
            i = idx[job.liq_cursor]
            job.liq_cursor += 1
            n += 1
            x, y, z, name, aux, states = blocks[i]
            pos = (bx + x, by + y, bz + z)
            # 同一调色板项共用一个方块字典：五万格水也就几份，不占内存也不拖 GC
            key = (name, aux, id(states))
            blk = job.liq_cache.get(key)
            if blk is None:
                blk = {"name": name, "aux": int(aux)}
                if states:
                    blk["states"] = _eng_states(states)
                job.liq_cache[key] = blk
            tries = 0
            try:
                if self._place_block(comp, pos, blk, states, dim, False):
                    job.liq_ok += 1
                else:
                    job.liq_fail += 1
                    tries = 1            # 没写进去，记一次，守水阶段只再试有限次
            except Exception:
                job.liq_fail += 1
                tries = 1
            # [坐标, 方块字典, 已补写次数]：守水阶段只"核对并纠正"，不再无脑重写
            sealed.append([pos, blk, tries])
            if n % 8 == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
        if job.liq_cursor >= total:
            self._final_phase(job)

    def _step_wash(self, job, comp):
        """补回被水冲掉的方块

        水一放下去，靠支撑站着的方块（火把、拉杆、铁轨、告示牌、花、红石线…）
        会当场变成掉落物——这不是 bug，是水本身的规则。
        这里按原结构挨个核对一遍，位置不对就补回去：每格只补一次、不触发邻居更新
        （正是邻居更新把它们弄掉的），总数超过 WASH_MAX_FIX 就收手。
        """
        idx = job.wash_idx
        total = len(idx)
        bx, by, bz = job.base
        dim = job.dim
        budget = self._write_budget()
        t0 = time.time()
        n = 0
        while job.wash_cursor < total:
            i = idx[job.wash_cursor]
            job.wash_cursor += 1
            n += 1
            x, y, z, name, aux, states = job.blocks[i]
            pos = (bx + x, by + y, bz + z)
            try:
                cur = comp.GetBlockNew(pos, dim)
            except Exception:
                cur = None
            if cur is None:
                pass                             # 区块没加载，读不到不等于被水冲掉了
            elif cur.get("name") == name:
                pass                             # 还在，什么都别做
            elif job.wash_fixed < cfg.WASH_MAX_FIX:
                blk = {"name": name, "aux": int(aux)}
                if states:
                    blk["states"] = _eng_states(states)
                try:
                    # states 传 None：字典里已经带了，避免再排一次状态写入
                    if self._place_block(comp, pos, blk, None, dim, False):
                        job.wash_fixed += 1
                except Exception:
                    pass
            if n % 16 == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
        if job.wash_cursor >= total:
            self._final_phase(job)

    def _step_seal(self, job, comp):
        """守水：巡检 + 只改不对的（绝不再无脑重写）

        老写法是每个 tick 把所有水原位重写一遍。写一次水就多排一个引擎方块刻，
        几万格水连写几秒 = 引擎的液体更新队列被顶爆，世界直接卡死崩掉（真机踩坑），
        而且重写本身还会把附近的门/火把/铁轨顶掉。
        现在改成先读后写：
          · 位置上的方块名和 aux 都对 -> 什么都不做（绝大多数情况）
          · 真的被引擎改了 -> 只补这一格，同一格最多补 FREEZE_MAX_FIX 次
          · 累计补写超过 FREEZE_FIX_CAP -> 说明水是真的在流，继续写只会更卡，直接收手
        每 tick 还有巡检格数上限，几万格水也只是"读"，不产生任何方块更新。
        """
        items = job.sealed
        n = len(items)
        if not n:
            job.phase = "done"
            return
        budget = cfg.FREEZE_BUDGET_MS
        checks = cfg.FREEZE_CHECKS
        t0 = time.time()
        done = 0
        fixed = 0
        while done < n and done < checks:
            i = job.seal_cursor % n
            ent = items[i]
            job.seal_cursor = (i + 1) % n
            done += 1
            # 巡检大半是"读一下、对上了、跳过"，以前只在补写时查时间，
            # 顺风的时候会一口气读满 600 格（几毫秒到十几毫秒）—— 这里也收口
            if (done & 31) == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
            if ent[2] >= cfg.FREEZE_MAX_FIX:
                continue
            pos = ent[0]
            blk = ent[1]
            try:
                cur = comp.GetBlockNew(pos, job.dim)
            except Exception:
                cur = None
            if cur is None:
                # 区块没加载：读不到不等于"被改掉"，别白耗补写次数
                continue
            if cur.get("name") == blk["name"]:
                # 只比方块名，不比 aux：引擎会把水的 liquid_depth 规范化成自己的值，
                # 拿算出来的 aux 去比对会一直判"不对"，然后又变成无脑重写。
                continue                       # 还是那格水，别碰它
            ent[2] += 1
            fixed += 1
            try:
                self._place_block(comp, pos, blk, None, job.dim, False)
            except Exception:
                pass
            if fixed % 8 == 0 and (time.time() - t0) * 1000.0 >= budget:
                break
        job.seal_fixes += fixed
        job.seal_pass += 1
        if job.tick >= job.seal_until:
            job.phase = "done"
        elif job.seal_fixes > cfg.FREEZE_FIX_CAP:
            # 水已经在真流了：再写下去只会把引擎拖死，立刻收手
            job.note = u"§e水在流动，提前结束守护"
            job.phase = "done"

    def _sec_box(self, job, sec):
        """分区的绝对包围盒 (x1,y1,z1,x2,y2,z2)

        单循环求 min/max：原来分别推导三个列表再各 min/max 一次，
        一个 1.9 万块的区要凭空造 5.7 万个元素的三份列表，
        每换一区就白扔十几毫秒（在换区那一帧里，正好卡在主线程上）。
        """
        bx, by, bz = job.base
        # 分区包围盒在扫描时就攒好了（见 _step_prep 阶段 1），这里直接查表。
        # 回退路径保留：分块模式/不分区建出来的 section 没有 bb。
        bb = sec.get("bb")
        if bb:
            return (bx + bb[0], by + bb[2], bz + bb[4],
                    bx + bb[1], by + bb[3], bz + bb[5])
        idx = sec["blocks"]
        if not idx:
            return (bx, by, bz, bx, by, bz)
        blocks = job.blocks
        p = blocks[idx[0]]
        x1 = x2 = p[0]
        y1 = y2 = p[1]
        z1 = z2 = p[2]
        for k in idx:
            p = blocks[k]
            x = p[0]
            if x < x1:
                x1 = x
            elif x > x2:
                x2 = x
            y = p[1]
            if y < y1:
                y1 = y
            elif y > y2:
                y2 = y
            z = p[2]
            if z < z1:
                z1 = z
            elif z > z2:
                z2 = z
        return (bx + x1, by + y1, bz + z1, bx + x2, by + y2, bz + z2)

    def _sec_center(self, job, sec):
        x1, y1, z1, x2, y2, z2 = self._sec_box(job, sec)
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0, (z1 + z2) / 2.0)

    def _prefetch(self, job):
        """写当前分区时，顺手给接下来的相邻分区挂常加载区块

        区块加载由引擎异步完成：提前挂上，等轮到那一区时数据早就加载好了，
        TP 过去几乎不用等 —— 这就是「相邻区域并行处理」在本引擎里的可行形态。
        （方块写入本身受引擎限制只能主线程，无法真多线程。）
        """
        if self.parallel <= 1:
            return
        n = len(job.sections)
        if job.sec_i >= n:
            return
        cur = job.sections[job.sec_i]
        if not cur.get("blocks"):
            return
        cur_c = self._sec_center(job, cur)
        added = 0
        cmdComp = None
        for k in range(job.sec_i + 1, n):
            if added >= self.parallel - 1:
                break
            sec = job.sections[k]
            if not sec.get("blocks") or sec.get("pf"):
                continue
            c = self._sec_center(job, sec)
            # 只预取相邻的（跨行远传那种跳几百格的不算，加载了也用不上）
            if max(abs(c[0] - cur_c[0]), abs(c[2] - cur_c[2])) > getattr(job, "side", cfg.SPLIT_SIZE) * 2.0:
                continue
            if cmdComp is None:
                cmdComp = self._command()
            if cmdComp is None:
                return
            x1, y1, z1, x2, y2, z2 = self._sec_box(job, sec)
            name = u"si_pf_%d" % k
            ok = False
            try:
                ok = bool(cmdComp.SetCommand(util.b(
                    u"/tickingarea add %d %d %d %d %d %d %s" %
                    (x1, y1, z1, x2, y2, z2, name))))
            except Exception:
                ok = False
            if ok:
                sec["pf"] = True
                job.pf.append((k, name))
                added += 1

    def _drop_pf(self, job, keep_from):
        """释放已经走过的预取区块（常加载空位有限，别一直占着）

        只删自己挂的（si_ 前缀），绝不碰服务器里别人的指令区。
        """
        prefix = cfg.AREA_PREFIX
        keep = []
        cmdComp = None
        for idx, name in job.pf:
            if idx < keep_from:
                if not name or not name.startswith(prefix):
                    continue
                if cmdComp is None:
                    cmdComp = self._command()
                if cmdComp:
                    try:
                        cmdComp.SetCommand(util.b(u"/tickingarea remove %s" % name))
                    except Exception:
                        pass
            else:
                keep.append((idx, name))
        job.pf = keep

    def _section_ready(self, job):
        """当前分区所在区块是否已经加载好：能读到这一区的第一块就算就绪

        以前这里一律死等 40 tick（2 秒）或急速档 6 tick —— 不管区块早就好了。
        实测绝大多数换区 1~3 tick 就加载完，固定等待白扔掉的正是"快"的空间。
        改成"读一下就知道"：就绪立刻开工，最多等到 TP_READY_MAX 兜底。
        """
        sec = self._section(job)
        if sec is None or not sec["blocks"]:
            return True
        comp = self._blocks()
        if comp is None:
            return True
        try:
            b = job.blocks[sec["blocks"][0]]
            got = comp.GetBlockNew((job.base[0] + b[0], job.base[1] + b[1],
                                    job.base[2] + b[2]), job.dim)
        except Exception:
            return False
        return got is not None

    def _prepare_section(self, job):
        """进入分区前的准备：跟随 TP + 防缺失时挂常加载区块"""
        # 先取本区：下面的等待逻辑要看它是否已被预取（提前赋值，别留局部变量坑）
        sec = self._section(job)
        tpd = self._tp_to_section(job)
        if tpd:
            job.tp_wait = 1
        if job.tp_wait > 0:
            # 刚跟随 TP 过来：普通模式等 2 秒让区块加载完；
            # 急速模式只等 0.3 秒（给 TP 包处理时间），其余写不进的靠分区末尾
            # 自动重试兜底——这才是急速的本意，不能让强制等待把它盖掉
            job.waiting = True
            if self.ta_mode and job.ta_ok:
                # 纯常加载：首区等常加载真正把区块拉起来，之后各区只留小间隔。
                # 同样带就绪探测：区块一好就开工，不等满
                wait = cfg.TA_FIRST_WAIT if job.sec_i == 0 else cfg.TA_WAIT
                job.wait_until = job.tick + wait
                job.ready_probe = True
                job.tp_wait = 0
                return
            wait = cfg.TP_LOAD_WAIT_TURBO if self.turbo else cfg.TP_LOAD_WAIT
            # 竖排导入跨行时是远距离瞬移（如 1,45 -> 2,0），区块最易跟不上导致
            # 该分区大量写失败：强制降速等 4 秒再恢复原速。
            # 判据要跟着分区边长走：128 格的分区，相邻两区中心本来就差 128 格，
            # 拿固定的 128 当阈值会把正常换区全判成"跨行远传"，每区白等 4 秒。
            big_jump = max(cfg.TP_BIG_JUMP, getattr(job, "side", cfg.SPLIT_SIZE) * 2)
            big = tpd >= big_jump
            if big:
                wait = max(1, int(self.big_wait * 20))
            elif sec is not None and sec.get("pf"):
                # 这区早在上一区写的时候就挂上常加载了，区块已经加载好。
                # 急速模式下它比普通 TP 还该快——以前这里排在 turbo 判断前面，
                # 反而让"预取好的区"等得比没预取的更久（白等 8 tick）。
                wait = 2 if self.turbo else max(3, cfg.PREFETCH_WAIT)
            elif self.turbo:
                wait = max(2, cfg.TP_LOAD_WAIT_TURBO // 2)   # 急速：0.1 秒就走
            elif job.last_fail == 0 and job.ok > 0:
                # 上一区一块都没失败 -> 区块跟得上，等待砍到 1/3
                wait = max(8, wait // 3)
            else:
                # 上一区有写不进的 -> 加长等待，比事后重试划算
                wait = min(70, int(wait * 1.5))
            job.wait_until = job.tick + max(1, wait)
            # 区块就绪就提前开工：固定等待只是上限，不是必须等满
            job.ready_probe = not big
            job.tp_wait = 0
            if big and len(job.sections) > 1:
                # 只有"跨行远传"这种异常减速值得留一句提示，其余全靠 tip
                job.note = u"§e跨行远传 %d 格 · 降速 %.1f 秒" % (int(tpd), wait / 20.0)
            return
        # 本区开始写入前，先把接下来的相邻分区区块挂上（异步加载与写入重叠）
        try:
            self._prefetch(job)
        except Exception:
            util.log_exc("prefetch")
        if not job.tick_enabled:
            return
        if sec is None or not sec["blocks"]:
            return
        x1, y1, z1, x2, y2, z2 = self._sec_box(job, sec)
        name = u"si_%d_%d" % (job.sec_i, len(sec["blocks"]))
        cmd = u"/tickingarea add %d %d %d %d %d %d %s" % (
            x1, y1, z1, x2, y2, z2, name)
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
        else:
            job.wait_until = job.tick + max(20, job.speed_ticks)
            if not ok:
                job.note = u"§e常加载区块没加上，照常继续"

    def _tp_to_section(self, job):
        """玩家跟着正在导入的分区自动传送

        巨型结构以玩家为中心居中放置，导入却从最远的角落开始——
        那边区块没加载，SetBlockNew 会静默失败（就是"开始区域小块导入失败"）。
        分区中心离玩家超过 TP_MIN_DIST 就把玩家 TP 过去，区块随即加载。
        纯常加载模式下整个结构已经挂了 tickingarea，直接跳过 TP。
        没挂上（名额不够 / 命令组件缺失）时 ta_ok 是 False，照常跟传——
        否则整个导入会在没加载的区块上写成一片空气。
        """
        if self.ta_mode and job.ta_ok:
            return 0
        sec = self._section(job)
        if sec is None or not sec["blocks"] or not job.pid:
            return 0
        x1, y1, z1, x2, y2, z2 = self._sec_box(job, sec)
        cx = (x1 + x2) / 2.0
        cz = (z1 + z2) / 2.0
        cy = y2 + 6.0                  # 站到分块上方面空处，别埋进方块里
        try:
            p = self.factory.CreatePos(job.pid).GetFootPos()
            dist = max(abs(p[0] - cx), abs(p[2] - cz))
        except Exception:
            return 0
        if dist < cfg.TP_MIN_DIST or not cfg.TP_FOLLOW:
            return 0
        try:
            if self.factory.CreatePos(job.pid).SetPos((cx, cy, cz)):
                # 跟传静默处理：几百分区会刷几百条，进度看下方小栏就够了
                return int(dist)
        except Exception:
            util.log_exc("tp to section")
        return 0

    def _ta_cover(self, job):
        """纯常加载模式：给结构挂 tickingarea，玩家不再跟着分区跑

        三条硬规矩（服务器里一般已经有别人的指令区，不能乱来）：
          1. 只按自己的名字（si_ 前缀）挂、按自己的名字删，**永远不用
             /tickingarea remove_all** —— 那会把出生点常加载、指令区一起清掉；
          2. 单个方形常加载有尺寸上限（超过百格引擎拒收），所以按 TA_MAX_SIDE
             切成几块挂，而不是硬塞一个巨区；
          3. 名额不够（世界默认 10 个，本插件最多占 TA_MAX_AREAS 个）就退回
             跟随 TP，并说清楚原因，不抢别人的名额。

        名字记进 job.areas，收尾/中止时由 _drop_area 逐个摘除。
        """
        blocks = job.blocks
        if not blocks:
            return
        bx, by, bz = job.base
        # 单循环求包围盒：一百多万块的结构别扫三遍（每遍都是在主线程上）
        b = blocks[0]
        x1 = x2 = b[0]
        y1 = y2 = b[1]
        z1 = z2 = b[2]
        for k in _range(len(blocks)):
            b = blocks[k]
            x = b[0]
            if x < x1:
                x1 = x
            elif x > x2:
                x2 = x
            y = b[1]
            if y < y1:
                y1 = y
            elif y > y2:
                y2 = y
            z = b[2]
            if z < z1:
                z1 = z
            elif z > z2:
                z2 = z
        # 不加 pad：方块都在结构范围内，多圈一格就多块一整区，
        # 名额是别人也在用的稀缺资源（190×239 的结构，省掉 pad 从 9 个降到 6 个）
        x1 = bx + x1
        x2 = bx + x2
        y1 = by + y1
        y2 = by + y2
        z1 = bz + z1
        z2 = bz + z2

        # 按区块列切：边长上限换算成"每区跨几个 16 格区块"，边界对齐到区块，
        # 这样既不会超出引擎的单区上限，也不会因为零头多占一个名额
        per = max(1, int(cfg.TA_MAX_SIDE) // 16)
        cx0 = x1 >> 4
        cx1 = x2 >> 4
        cz0 = z1 >> 4
        cz1 = z2 >> 4
        nx = (cx1 - cx0) // per + 1
        nz = (cz1 - cz0) // per + 1
        need = nx * nz
        cap = max(1, int(cfg.TA_MAX_AREAS))
        if need > cap:
            self.tell(job.pid, u"§e纯常加载：结构 %dx%d 格需要 %d 个常加载区，"
                               u"超过本插件上限 %d §7本次退回跟随 TP"
                      % (x2 - x1 + 1, z2 - z1 + 1, need, cap))
            return
        cmd = self._command()
        if cmd is None:
            self.tell(job.pid, u"§c拿不到命令组件，纯常加载用不了 §7本次退回跟随 TP")
            return

        done = 0
        fail = 0
        for ix in _range(nx):
            acx0 = cx0 + ix * per
            acx1 = min(cx1, acx0 + per - 1)
            ax1 = acx0 * 16
            ax2 = acx1 * 16 + 15
            for iz in _range(nz):
                acz0 = cz0 + iz * per
                acz1 = min(cz1, acz0 + per - 1)
                az1 = acz0 * 16
                az2 = acz1 * 16 + 15
                name = u"%sta_%d" % (cfg.AREA_PREFIX, ix * nz + iz)
                ok = False
                try:
                    ok = bool(cmd.SetCommand(util.b(
                        u"/tickingarea add %d %d %d %d %d %d %s" %
                        (int(ax1), int(y1), int(az1),
                         int(ax2), int(y2), int(az2), name))))
                except Exception:
                    ok = False
                if ok:
                    job.areas.append(name)
                    done += 1
                else:
                    fail += 1
        if not done:
            self.tell(job.pid, u"§c常加载一个都没挂上（世界已有 10 个区域？）"
                               u"§7本次退回跟随 TP")
            return
        job.tp_wait = 1              # 首区走一次等待分支，给区块加载时间
        job.ta_ok = True             # 挂上了才允许"不跟随 TP"
        if fail:
            self.tell(job.pid, u"§e纯常加载：§f%d 个区挂上、%d 个失败 "
                               u"§7失败部分靠分区重试兜底，建议改用跟随 TP"
                      % (done, fail))
        else:
            self.tell(job.pid, u"§a纯常加载：§f结构已挂 %d 个 tickingarea §7"
                               u"（%d×%d×%d，玩家不跟随传送）"
                      % (done, int(x2 - x1 + 1), int(y2 - y1 + 1),
                         int(z2 - z1 + 1)))

    def _drop_area(self, job):
        """摘掉本任务挂的常加载

        只删自己记录过的名字，并且名字必须带 si_ 前缀才动手 ——
        服务器里的指令区/出生点常加载不属于我们，一条都不能碰。
        （历史版本没有用过 remove_all，以后也不许用。）
        """
        prefix = cfg.AREA_PREFIX
        while job.areas:
            name = job.areas.pop()
            if not name or not name.startswith(prefix):
                continue
            cmdComp = self._command()
            if cmdComp:
                try:
                    cmdComp.SetCommand(util.b(u"/tickingarea remove %s" % name))
                except Exception:
                    pass

    def _clear_areas(self, job):
        self._drop_area(job)
        if getattr(job, "pf", None):
            self._drop_pf(job, len(job.sections) + 1)

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
            # 引擎这个接口只认字节串：传 unicode 会刷
            # "set_command_block error: cmd is not a str"，命令整条丢掉
            if be.SetCommandBlock(pos, dim, _native(cmd), _native(name or u""),
                                  mode, cond, rmode):
                job.cmd_fixed += 1
        except Exception:
            pass
        # 实测部分版本导入后循环命令方块不自动执行（设置读回都对也不跑）。
        # 换思路：展示类命令（tellraw/title/say/me，无副作用）由 mod 每秒
        # 代理执行一次，效果等同循环+保持开启；其余命令仍留在方块里走红石。
        low = cmd.strip().lower()
        if (mode == CMD_BLOCK_MODE.get("minecraft:repeating_command_block")
                and rmode == 0 and low.split(" ")[0].lstrip("/") in
                ("tellraw", "title", "say", "me") and cmd not in self.replay_cmds
                and len(self.replay_cmds) < int(cfg.REPLAY_MAX)):
            self.replay_cmds.append(cmd)

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

    # ------------------------------------------------------------ GC 节流
    def _gc_relax(self):
        """导入期间放宽 GC 阈值

        一百多万块的结构会在堆上留几百万个对象（每块一个 6 元组 + states dict），
        默认阈值(700,10,10)下分代回收隔几秒就全量扫一遍，实测象牙城堡导入的
        第 339 帧被扫掉 216ms —— 玩家看到的就是"突然卡住不动"。
        把第 0 代阈值抬到 2 万，扫描频率降一个数量级，收尾立刻恢复原值。
        """
        try:
            import gc
            if not getattr(self, "_gc_saved", None):
                self._gc_saved = gc.get_threshold()
                gc.set_threshold(20000, 50, 50)
        except Exception:
            pass

    def _gc_restore(self):
        try:
            import gc
            saved = getattr(self, "_gc_saved", None)
            if saved:
                gc.set_threshold(*saved)
                self._gc_saved = None
        except Exception:
            pass

    def _finish(self, job):
        self.job = None
        self._gc_restore()
        self.tp_route = None
        self._clear_areas(job)
        self._freeze_off()          # 恢复导入期间按住的游戏规则（随机刻/昼夜/天气）
        self._tip_raw(u"")          # 小栏清空，别把最后一行卡在屏幕上
        if job.ok == 0 and not job.liq_ok:
            self.tell(job.pid, u"§c放置失败：%d 个方块全部没写进去" % job.fail)
            if job.err:
                self.tell(job.pid, u"§7原因：§f" + job.err)
            self.tell(job.pid, u"§7先跑 §f/structureim test §7看单块写入是否正常")
        else:
            msg = u"§a放置完成：§f%s §7(%d 块" % (util.short(job.title, 28), job.ok)
            if job.fail:
                msg += u" / %d 失败" % job.fail
            msg += u"，%.1f 秒)" % (job.tick / 20.0)
            if job.item_ok or job.item_fail:
                msg += u" §7· 物品 §f%d" % job.item_ok
                if job.item_fail:
                    msg += u"§7(失败 %d)" % job.item_fail
                if job.item_trim:
                    msg += u"§7(裁剪 %d)" % job.item_trim
                if job.item_bare:
                    msg += u"§7(仅本体 %d)" % job.item_bare
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
            if job.liq_ok or job.liq_fail:
                msg += u" §7· 液体 §f%d" % job.liq_ok
                if job.liq_fail:
                    msg += u"§7(失败 %d)" % job.liq_fail
            if job.wash_fixed:
                msg += u" §7· 补回被水冲掉 §f%d" % job.wash_fixed
            if job.seal_fixes:
                msg += u" §7· 守水补写 §f%d" % job.seal_fixes
            self.tell(job.pid, msg)
            # 把时间拆开：慢了到底是"写不动"还是"一直在等区块"，一眼能看出来
            try:
                total_s = job.tick / 20.0
                work_s = job.work_ticks / 20.0
                wait_s = max(0.0, total_s - work_s)
                self.tell(job.pid, u"§7耗时 §f%.1fs§7 = 解析 §f%.1fs§7 + 写入 §f%.1fs§7 + 等区块 §f%.1fs"
                                   u" §8(%.0f 块/秒)" %
                          (total_s + job.parse_secs, job.parse_secs, work_s, wait_s,
                           job.ok / work_s if work_s > 0.05 else 0.0))
            except Exception:
                pass
            if job.sealed and not job.freeze:
                self.tell(job.pid, u"§7冻结已关（§f/structureim freeze on§7 可让水不再乱跑）")
            self.tell(job.pid, u"§7基准位置：§f%d, %d, %d" % job.base)
            if self.map_mode:
                # 地图阶段出任何岔子都只报错，不能把已经导好的结构拖成「放置中断」
                try:
                    self._queue_maps(job)
                except Exception:
                    tb = traceback.format_exc()
                    util.log(u"地图墙异常\n" + util.u(tb))
                    self.map_queue = None
                    self.tell(job.pid, u"§c地图墙启动失败：§f" +
                              util.short(util.u(tb.strip().splitlines()[-1]), 48))
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
            if args.get("threads") is not None:
                # 界面只有一个「并行加速」开关，开 = 解析后台 + 邻区预取，关 = 全关。
                # 想细调用 /structureim threads / parallel
                on = bool(args.get("threads"))
                self.threads = 2 if on else 1
                self.parallel = max(cfg.PARALLEL_DEFAULT, 2) if on else 1
                self._save()
            if args.get("speed") is not None:
                try:
                    speed = int(util.u(args.get("speed")))
                except (TypeError, ValueError):
                    speed = cfg.SPEED_DEFAULT
                self.tick_speed = max(cfg.SPEED_MIN, min(cfg.SPEED_MAX, speed))
                self._save()
            if args.get("opt") is not None:
                # 设置页每一行的 − / + ：key + delta，服务端统一夹到合法区间
                try:
                    delta = int(util.u(args.get("delta")) or 0)
                except (TypeError, ValueError):
                    delta = 0
                self._apply_opt(util.u(args.get("opt")), delta,
                                util.u(args.get("value") or u""))
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

    # ------------------------------------------------------------ 设置页的逐行加减
    # 界面每一行就是一个参数：key + delta（−1 / +1），服务端负责夹到合法区间并存档
    _OPT_INT = {
        u"threads": ("threads", 1, 8),
        u"parallel": ("parallel", 1, 4),
        u"budget": ("budget", 2, 25),
        u"mapspeed": ("map_speed", 0, 2),
        u"znum": ("zones_n", 1, 9),
        u"zwait": ("zone_secs", 1, 30),
        u"freezetime": ("freeze_secs", 0, 60),
        u"bigwait": ("big_wait", 1, 30),
    }
    _OPT_BOOL = {
        u"split": "split",
        u"tick": "tick_mode",
        u"chunk": "chunk",
        u"map": "map_mode",
        u"turbo": "turbo",
        u"freeze": "freeze",
        u"maparea": "map_area",
        u"ta": "ta_mode",
        u"verify": "verify",
    }

    def _apply_opt(self, key, delta, value=u""):
        if key == u"preset":
            # 一键预设：safe / balance / turbo，也可传中文
            self.cmd_preset(None, value or u"balance")
            return
        if key == u"fast":
            # 一键提速：和 /structureim fast 完全一样
            self.threads = max(2, cfg.THREADS_DEFAULT)
            self.parallel = max(3, cfg.PARALLEL_DEFAULT)
            self.turbo = True
            self.budget = cfg.WRITE_BUDGET_MS_TURBO
            self.map_speed = 2
            if not self.split_size:
                self.split_size = int(cfg.SPLIT_SIZE_MAX)
            self._save()
            return
        if key == u"nbtlimit":
            cur = int(self.nbt_limit or 0)
            if delta > 0:
                cur = 8 if cur < 8 else min(1024, cur + 8)
            elif delta < 0:
                cur = 0 if cur <= 8 else cur - 8
            self.nbt_limit = cur
            self._save()
            return
        if key in self._OPT_BOOL:
            attr = self._OPT_BOOL[key]
            value = bool(delta > 0) if delta else (not bool(getattr(self, attr)))
            setattr(self, attr, value)
            if key == u"tick" and value:
                self.split = True          # 防缺失依赖分区
            self._save()
            return
        if key == u"splitz":
            cur = int(getattr(self, "split_size", 0) or 0)
            if delta > 0:
                cur = 16 if cur < 16 else min(int(cfg.SPLIT_SIZE_MAX), cur + 16)
            elif delta < 0:
                cur = 0 if cur <= 16 else cur - 16
            self.split_size = cur
            self._save()
            return
        item = self._OPT_INT.get(key)
        if not item:
            return
        attr, lo, hi = item
        cur = getattr(self, attr)
        try:
            cur = int(round(float(cur)))
        except (TypeError, ValueError):
            cur = lo
        cur += 1 if delta > 0 else (-1 if delta < 0 else 0)
        setattr(self, attr, max(lo, min(hi, cur)))
        self._save()

    def _opts_payload(self):
        """设置页要显示的一堆数值（每一行一个）"""
        return {
            "splitz": int(getattr(self, "split_size", 0) or 0),
            "verify": 1 if self.verify else 0,
            "ta": 1 if self.ta_mode else 0,
            "freeze": 1 if self.freeze else 0,
            "maparea": 1 if self.map_area else 0,
            "threads": int(self.threads),
            "parallel": int(self.parallel),
            "budget": int(round(self.budget)),
            "mapspeed": int(self.map_speed),
            "znum": int(self.zones_n),
            "zwait": int(round(self.zone_secs)),
            "freezetime": int(round(self.freeze_secs)),
            "bigwait": int(round(self.big_wait)),
            "nbtlimit": int(self.nbt_limit or 0),
        }

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
        # 这一包里的字符串一条都不能为空：新引擎按"至少 1 个字符"校验，
        # 空的 dir/err 会让客户端 readNoHeader 失败，服务端随即断开世界。
        def _s(v):
            v = util.u(v)
            return v if v else u" "
        for i in range(start, min(start + cfg.ROWS, total)):
            e = lib.entries[i]
            rows.append([i + 1, _s(e.name), _s(e.rel)])
        data = {
            "dir": _s(lib.root),
            "err": _s(lib.err),
            "total": total,
            "page": page,
            "pages": pages,
            "rows": rows,
            "busy": self.job is not None,
            "chunk": bool(self.chunk),
            "map": bool(self.map_mode),
            "turbo": bool(self.turbo),
            "threads": bool(self.threads > 1),
            "split": bool(self.split),
            "tick": bool(self.tick_mode),
            "speed": int(self.tick_speed),
            "opts": self._opts_payload(),
        }
        try:
            self.NotifyToClient(pid, cfg.EV_DATA, data)
        except Exception:
            util.log_exc("push")
