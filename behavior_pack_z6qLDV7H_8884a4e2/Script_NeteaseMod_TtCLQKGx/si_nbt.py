# -*- coding: utf-8 -*-
"""NBT（小端）+ .mcstructure 解码

关键实测结论：
    1. 文件可能是 gzip、zlib 或裸 NBT，按首字节嗅探
    2. block_indices 的索引序是 index = z + y*sz + x*sz*sy（x 最慢、z 最快），
       已用官方 blank3x3opensky 等结构交叉验证；写成常见的 x 最快会解成条纹噪点
    3. 方块实体不在 structure.block_entities，而在
       structure.palette.default.block_position_data["<线性下标>"].block_entity_data
       （265 个官方结构实测，block_entities 一个都没有）
    4. 生态引擎要的 NBT 是"叶子带类型"：字段值写成 {'__type__': 标签号, '__value__': 值}，
       compound / list 本身仍是普通 dict / list，见 to_engine()
"""

import gzip
import io
import math
import struct
import zlib

TAG_END, TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG = 0, 1, 2, 3, 4
TAG_FLOAT, TAG_DOUBLE, TAG_BYTE_ARR, TAG_STR = 5, 6, 7, 8
TAG_LIST, TAG_COMPOUND, TAG_INT_ARR, TAG_LONG_ARR = 9, 10, 11, 12

try:                                     # py2 游戏 / py3 本地测试都要能 import
    STR_TYPES = (str, unicode)           # noqa: F821
except NameError:
    STR_TYPES = (str,)

_F1 = struct.Struct("<b")
_F2 = struct.Struct("<h")
_F4 = struct.Struct("<i")
_F8 = struct.Struct("<q")
_D4 = struct.Struct("<f")
_D8 = struct.Struct("<d")


class Reader(object):
    __slots__ = ("s", "i", "n")

    def __init__(self, s):
        self.s = s
        self.i = 0
        self.n = len(s)

    def u1(self):
        v = struct.unpack_from("<B", self.s, self.i)[0]
        self.i += 1
        return v

    def i1(self):
        v = _F1.unpack_from(self.s, self.i)[0]
        self.i += 1
        return v

    def i2(self):
        v = _F2.unpack_from(self.s, self.i)[0]
        self.i += 2
        return v

    def i4(self):
        v = _F4.unpack_from(self.s, self.i)[0]
        self.i += 4
        return v

    def i8(self):
        v = _F8.unpack_from(self.s, self.i)[0]
        self.i += 8
        return v

    def f4(self):
        v = _D4.unpack_from(self.s, self.i)[0]
        self.i += 4
        return v

    def f8(self):
        v = _D8.unpack_from(self.s, self.i)[0]
        self.i += 8
        return v

    def raw(self, n):
        v = self.s[self.i:self.i + n]
        self.i += n
        return v

    def text(self):
        n = struct.unpack_from("<H", self.s, self.i)[0]   # 小端 NBT：字符串长度也是小端
        self.i += 2
        return self.raw(n).decode("utf-8", "replace")


def _payload(r, t):
    if t == TAG_BYTE:
        return r.i1()
    if t == TAG_SHORT:
        return r.i2()
    if t == TAG_INT:
        return r.i4()
    if t == TAG_LONG:
        return r.i8()
    if t == TAG_FLOAT:
        return r.f4()
    if t == TAG_DOUBLE:
        return r.f8()
    if t == TAG_BYTE_ARR:
        n = r.i4()
        return r.raw(n)
    if t == TAG_STR:
        return r.text()
    if t == TAG_LIST:
        et = r.u1()
        n = r.i4()
        if et == TAG_END:
            return []
        return [_payload(r, et) for _ in range(n)]
    if t == TAG_COMPOUND:
        out = {}
        while True:
            ct = r.u1()
            if ct == TAG_END:
                return out
            name = r.text()
            out[name] = _payload(r, ct)
    if t == TAG_INT_ARR:
        n = r.i4()
        if n <= 0:
            return []
        out = list(struct.unpack_from("<%di" % n, r.s, r.i))
        r.i += 4 * n                # unpack_from 不会推进指针，必须手动走
        return out
    if t == TAG_LONG_ARR:
        n = r.i4()
        if n <= 0:
            return []
        out = list(struct.unpack_from("<%dq" % n, r.s, r.i))
        r.i += 8 * n
        return out
    raise ValueError("unknown tag %d at %d" % (t, r.i))


def parse(data):
    """裸 NBT -> 根 compound"""
    if len(data) < 3:
        raise ValueError("文件太小")
    r = Reader(data)
    t = r.u1()
    if t != TAG_COMPOUND:
        raise ValueError("根标签不是 compound（%d）" % t)
    r.text()                       # 根名
    return _payload(r, TAG_COMPOUND)


# ---------------------------------------------------------------------------
# 带类型解析：方块实体要写回引擎时必须保留 NBT 类型，否则 SetBlockEntityData
# 会把字段按位置错配（实测给告示牌传纯 int 会写串位，字符串直接丢）
# 节点表示一律是 (tag, value)
# ---------------------------------------------------------------------------
def _payload_t(r, t):
    if t == TAG_BYTE:
        return (t, r.i1())
    if t == TAG_SHORT:
        return (t, r.i2())
    if t == TAG_INT:
        return (t, r.i4())
    if t == TAG_LONG:
        return (t, r.i8())
    if t == TAG_FLOAT:
        return (t, r.f4())
    if t == TAG_DOUBLE:
        return (t, r.f8())
    if t == TAG_BYTE_ARR:
        return (t, r.raw(r.i4()))
    if t == TAG_STR:
        return (t, r.text())
    if t == TAG_LIST:
        et = r.u1()
        n = r.i4()
        if et == TAG_END:
            return (t, [])
        return (t, [_payload_t(r, et) for _ in range(n)])
    if t == TAG_COMPOUND:
        out = {}
        while True:
            ct = r.u1()
            if ct == TAG_END:
                return (t, out)
            name = r.text()          # 必须先读名字再读值：赋值语句右边先求值
            out[name] = _payload_t(r, ct)
    if t == TAG_INT_ARR:
        n = r.i4()
        if n <= 0:
            return (t, [])
        out = list(struct.unpack_from("<%di" % n, r.s, r.i))
        r.i += 4 * n
        return (t, out)
    if t == TAG_LONG_ARR:
        n = r.i4()
        if n <= 0:
            return (t, [])
        out = list(struct.unpack_from("<%dq" % n, r.s, r.i))
        r.i += 8 * n
        return (t, out)
    raise ValueError("unknown tag %d at %d" % (t, r.i))


def parse_t(data):
    """裸 NBT -> (TAG_COMPOUND, {...})，保留全部类型信息"""
    if len(data) < 3:
        raise ValueError("文件太小")
    r = Reader(data)
    t = r.u1()
    if t != TAG_COMPOUND:
        raise ValueError("根标签不是 compound（%d）" % t)
    r.text()
    return _payload_t(r, TAG_COMPOUND)


def plain(node):
    """(tag, value) 树 -> 普通 python 值"""
    t, v = node
    if t == TAG_COMPOUND:
        return dict((k, plain(c)) for k, c in v.items())
    if t == TAG_LIST:
        return [plain(c) for c in v]
    return v


def member(node, name):
    """取 compound 节点里的子节点，取不到返回 None"""
    if not node or node[0] != TAG_COMPOUND:
        return None
    return node[1].get(name)


def to_engine(node):
    """(tag, value) 树 -> 引擎 SetBlockEntityData 认的格式

    实测格式不是"全量包装"：引擎只在**叶子**字段上加 {'__type__', '__value__'}，
    compound / list 本身就是普通 dict / list，里面装包装过的叶子。
    （GetBlockEntityData 读出来也是这样；把 compound 也包一层会被静默忽略）
    """
    t, v = node
    if t == TAG_COMPOUND:
        return dict((k, to_engine(c)) for k, c in v.items())
    if t == TAG_LIST:
        return [to_engine(c) for c in v]
    if t == TAG_BYTE_ARR:
        n = len(v)
        return list(struct.unpack("<%db" % n, v)) if n else []
    if t in (TAG_INT_ARR, TAG_LONG_ARR):
        return list(v)
    return {"__type__": t, "__value__": v}


def _decompress(data):
    # py2 里 b"" 就是 str，py3 里是 bytes，两种写法放进元组一次判断即可通吃
    if data[:2] in (b"\x1f\x8b", "\x1f\x8b"):
        return gzip.GzipFile(fileobj=io.BytesIO(data)).read()
    try:
        return zlib.decompress(data)
    except Exception:
        return data


class Structure(object):
    """一个已解码的结构

    不用 __slots__：热重载时新旧类的 slot 描述符会打架
    （TypeError: descriptor 'block_entities' ... doesn't apply）
    """

    def __init__(self, name, path, size, blocks, entity_count, block_entities=None,
                 entities=None):
        self.name = name
        self.path = path
        self.size = size
        self.blocks = blocks
        self.entity_count = entity_count
        # 方块实体：箱子内容、告示牌文字、刷怪笼等，统一存 (x, y, z, nbt)
        self.block_entities = block_entities or []
        # 实体：矿车 / 船 / 生物 / 掉落物，存 (x, y, z, identifier, nbt)
        self.entities = entities or []

    @property
    def count(self):
        return len(self.blocks)

    def describe(self):
        return u"%s %dx%dx%d / %d 方块" % (
            self.name, self.size[0], self.size[1], self.size[2], len(self.blocks))


AIR_BLOCKS = ("minecraft:air", "minecraft:void_air", "minecraft:structure_void")


def _index_pos(index, size):
    """block_indices / block_position_data 的线性下标 -> (x, y, z)"""
    sx, sy, sz = size
    plane = sy * sz
    x = index // plane
    rest = index - x * plane
    y = rest // sz
    z = rest - y * sz
    return x, y, z


def _collect_entities(root_t, size, names, layer0):
    """从 palette.default.block_position_data 取方块实体

    实测：.mcstructure 里方块实体不是一个列表，而是
        structure.palette.default.block_position_data["<线性下标>"] = {
            "block_entity_data": {..完整 NBT..},
            "tick_queue_data": {..待处理刻..}
        }
    下标和 block_indices 同一套编号，用 _index_pos 换算即可。
    """
    dflt = member(member(member(root_t, "structure"), "palette"), "default")
    bpd = member(dflt, "block_position_data")
    out = []
    if not bpd or bpd[0] != TAG_COMPOUND:
        return out
    limit = len(layer0)
    for key, item in bpd[1].items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        be = member(item, "block_entity_data")
        if not be or be[0] != TAG_COMPOUND or not be[1]:
            continue
        if 0 <= index < limit:                 # 空气位置的残留数据不要
            v = layer0[index]
            if 0 <= v < len(names) and names[v] in AIR_BLOCKS:
                continue
        x, y, z = _index_pos(index, size)
        out.append((x, y, z, to_engine(be)))
    return out


def load_bytes(data, name=None, skip_air=True, skip_void=True):
    """从内存字节加载结构（内置库/审核合规路径）"""
    root_t = parse_t(_decompress(data))
    root = plain(root_t)

    size = root.get("size")
    if not isinstance(size, list) or len(size) != 3:
        raise ValueError("缺少 size")
    sx, sy, sz = int(size[0]), int(size[1]), int(size[2])
    if sx <= 0 or sy <= 0 or sz <= 0:
        raise ValueError("尺寸非法 %s" % (size,))

    body = root.get("structure") or {}
    indices = body.get("block_indices") or []
    if not indices:
        raise ValueError("缺少 block_indices")
    layer0 = indices[0]

    palette = ((body.get("palette") or {}).get("default") or {}).get("block_palette") or []
    names = []
    states = []
    for item in palette:
        names.append(item.get("name") or "")
        states.append(item.get("states") or {})

    expect = sx * sy * sz
    total = len(layer0)
    if total < expect:
        raise ValueError("block_indices 长度不足：%d < %d" % (total, expect))

    blocks = []
    append = blocks.append
    for index in range(expect):
        v = layer0[index]
        if v < 0:
            if skip_void:
                continue
            v = 0
        if v >= len(names):
            continue
        block_name = names[v]
        if not block_name:
            continue
        if skip_air and block_name in AIR_BLOCKS:
            continue
        x, y, z = _index_pos(index, (sx, sy, sz))
        append((x, y, z, block_name, states[v]))

    ents = body.get("entities")
    entity_count = len(ents) if isinstance(ents, list) else 0

    # 方块实体：箱子内容、告示牌文字、刷怪笼、比较器输出等
    block_entities = _collect_entities(root_t, (sx, sy, sz), names, layer0)

    # 兼容极少数工具导出的顶层 block_entities 列表（带平铺 nbt 或 typed nbt）
    for item in (body.get("block_entities") or []):
        if not isinstance(item, dict):
            continue
        p = item.get("pos")
        nbt = item.get("nbt")
        if not isinstance(nbt, dict) or nbt.get("__type__") == TAG_COMPOUND:
            continue
        if isinstance(p, list) and len(p) == 3:
            ex, ey, ez = int(p[0]), int(p[1]), int(p[2])
        elif isinstance(p, dict):
            ex, ey, ez = int(p.get("x", 0)), int(p.get("y", 0)), int(p.get("z", 0))
        else:
            continue
        block_entities.append((ex, ey, ez, _wrap_plain_compound(nbt)))

    # 实体（矿车 / 船 / 生物 / 掉落物）：structure.entities
    # 实测是扁平 dict：identifier、Pos、Rotation 和完整实体 NBT 都在顶层，没有 nbt 包装
    # 部分导出工具会把 entities 写成 None（tag end），or [] 兜住
    struct_ents = []
    for item in (ents or []):
        if not isinstance(item, dict):
            continue
        ident = item.get("identifier") or ""
        if not ident:
            continue
        p = item.get("Pos") or item.get("pos")
        if isinstance(p, list) and len(p) == 3:
            ex, ey, ez = int(math.floor(p[0])), int(math.floor(p[1])), int(math.floor(p[2]))
        elif isinstance(p, dict):
            ex = int(math.floor(p.get("x", 0)))
            ey = int(math.floor(p.get("y", 0)))
            ez = int(math.floor(p.get("z", 0)))
        else:
            continue
        struct_ents.append((ex, ey, ez, ident, item))

    return Structure(name or "memory", None, (sx, sy, sz), blocks, entity_count,
                     block_entities, struct_ents)


def _wrap_plain_compound(d):
    """兜底：把纯 python dict 按猜测类型包成引擎格式（只用于旧格式列表）"""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _wrap_plain_compound(v)
        elif isinstance(v, (list, tuple)):
            out[k] = [_wrap_plain_compound(i) if isinstance(i, dict)
                      else {"__type__": TAG_STR, "__value__": i}
                      for i in v]
        elif isinstance(v, STR_TYPES):
            out[k] = {"__type__": TAG_STR, "__value__": v}
        elif isinstance(v, int):
            out[k] = {"__type__": TAG_INT, "__value__": v}
        elif isinstance(v, float):
            out[k] = {"__type__": TAG_DOUBLE, "__value__": v}
        else:
            out[k] = {"__type__": TAG_STR, "__value__": v}
    return out


def rotate_entities(ents, size, times):
    """实体坐标跟着结构一起转

    兼容两种元组：方块实体 (x, y, z, nbt) 和实体 (x, y, z, identifier, nbt)，
    前 3 位是坐标，其余原样带走。
    """
    times = int(times) % 4
    if times == 0 or not ents:
        return ents
    sx, sy, sz = size
    out = []
    for item in ents:
        x, y, z = item[0], item[1], item[2]
        rest = tuple(item[3:])
        if times == 1:
            out.append((sz - 1 - z, y, x) + rest)
        elif times == 2:
            out.append((sx - 1 - x, y, sz - 1 - z) + rest)
        else:
            out.append((z, y, sx - 1 - x) + rest)
    return out


def rotate(blocks, size, times):
    """绕 Y 轴旋转 0~3 个 90°，返回 (新方块表, 新尺寸)"""
    times = int(times) % 4
    if times == 0:
        return blocks, size

    sx, sy, sz = size
    out = []
    append = out.append
    if times == 1:
        new_size = (sz, sy, sx)
        for (x, y, z, name, st) in blocks:
            append((sz - 1 - z, y, x, name, st))
    elif times == 2:
        new_size = (sx, sy, sz)
        for (x, y, z, name, st) in blocks:
            append((sx - 1 - x, y, sz - 1 - z, name, st))
    else:
        new_size = (sz, sy, sx)
        for (x, y, z, name, st) in blocks:
            append((z, y, sx - 1 - x, name, st))
    return out, new_size


def load(path, name=None, skip_air=True, skip_void=True):
    """本地调试用：从磁盘文件加载（平台审核版请用 load_bytes）"""
    f = open(path, "rb")
    try:
        data = f.read()
    finally:
        f.close()
    return load_bytes(data, name, skip_air, skip_void)
