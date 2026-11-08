# -*- coding: utf-8 -*-
"""结构导入器 · 全部可调参数"""

# ---------------------------------------------------------------- 身份
MOD_NAME = "NeteaseMod_TtCLQKGx"
PKG = "Script_NeteaseMod_TtCLQKGx"

SERVER_SYSTEM_NAME = "SiServerSystem"
CLIENT_SYSTEM_NAME = "SiClientSystem"

SERVER_CLS = PKG + ".si_srv.SiServerSystem"
CLIENT_CLS = PKG + ".si_cli.SiClientSystem"
UI_CLS = PKG + ".si_ui.SiScreen"

# 资源包 ui/structure_im.json 里的 namespace.main
UI_KEY = "StructureImUI"
UI_DEF = "structure_im.main"
# 设置页（风格与主界面同一套贴图）
SET_KEY = "StructureImSet"
SET_DEF = "structure_im_set.main"
SET_CLS = PKG + ".si_ui.SiSettingScreen"

# ---------------------------------------------------------------- 指令
# 必须全小写：带大写会让引擎注册指令本体失败，再注册参数重载时断言崩溃
CMD_NAME = "structureim"
# 指令 json 的参数项只认 name / type / default / options 四个键，
# 写 enum / values 会被引擎丢弃（日志：child 'enum' not valid here）
CMD_ACTIONS = ["ui", "dir", "scan", "list", "place", "stop", "pause", "info", "selftest", "test", "split",
               "zwait", "znum", "zsec", "bigwait", "manual", "help2"]

# ---------------------------------------------------------------- 存档
# SetExtraData 只接受 str，中文必须编码成 utf-8 字节串
EXTRA_KEY = "si_dir"

# ---------------------------------------------------------------- 分区导入
SPLIT_DEFAULT = False     # 默认关，用 /structureim split on 或界面按钮打开
SPLIT_SIZE = 16           # 分区边长（格）：默认按 16×16 的区块切片

# ---------------------------------------------------------------- 防大型结构缺失
# 开启后给每个分区临时加原版 /tickingarea 常加载区块，等它加载完再放置，
# 避免未加载区块里的方块放不出来。会显著拖慢导入速度。
TICK_DEFAULT = False
SPEED_DEFAULT = 3         # 每个分区等待多少秒（给区块加载留时间）
SPEED_MIN = 1
SPEED_MAX = 10

# ---------------------------------------------------------------- 扫描
MAX_DEPTH = 10            # 目录递归深度
MAX_ENTRIES = 400         # 最多收录多少个结构
EXTS = (".mcstructure", ".nbt")

# ---------------------------------------------------------------- 放置
BATCH = 600               # 每 tick 写入多少方块
MAX_BLOCKS = 2000000      # 单结构方块上限；超大的靠分块模式切成小块逐个导入，不再怕卡
BIG_FILE_BLOCKS = 150000  # 超过这个数就自动强制分块模式
TP_FOLLOW = True          # 导入时玩家自动跟随当前分区传送（保证区块加载）
TP_MIN_DIST = 48          # 分区中心离玩家超过这个距离才 TP
TP_LOAD_WAIT = 40         # 跟随 TP 后强制等多少 tick（2 秒）给区块加载
TP_LOAD_WAIT_TURBO = 6    # 急速模式：TP 后只等 0.3 秒，剩余交给写失败自动重试
TP_BIG_JUMP = 128         # 跟随 TP 一步跳多远算"跨行远传"（竖排导入换行最典型）
TP_BIG_WAIT = 80          # 跨行远传后强制等多少 tick（4 秒）让区块追上，再恢复原速
TP_ZONE_SECS = 3           # 九宫格巡逻：每个区停多少秒（保证区块加载稳定）
TP_ZONE_DIV = 3            # 每幅地图切 3x3=9 个区
MAP_GIVE_WAIT = 40        # 发地图：TP 到单元中心后等多少 tick 再 give（防空白图）
MAP_HOLD_SECS = 4          # 地图放进玩家主手停留几秒让引擎渲染地形（否则挂墙是空白图）
MAP_TA_RADIUS = 4          # 发图前给整幅 128x128 单元挂常加载区块（圆半径 4 chunk 覆盖 8x8）
MAP_TA_WAIT = 60           # 常加载区块挂上后等多少 tick 让区块真正加载完再 give

TURBO_DEFAULT = False     # 急速模式：分区等待降到 1 tick（速度换卡顿风险）

# ---------------------------------------------------------------- 地图画模式
MAP_DEFAULT = False       # 对齐地图边缘导入，导完自动给玩家编号地图
MAP_SIZE = 128            # 一张地图覆盖 128×128 格
FORWARD = 3.0             # 放在身前多少格
BATCH_ENT = 200           # 每 tick 写多少个方块实体
BATCH_MOB = 8             # 每 tick 生成多少个实体（summon 指令较慢，控制在个位数）

# ---------------------------------------------------------------- 分块模式
# 把大结构切成"数据量"小于阈值的分块依次导入（把一个文件当多个小文件处理），
# 防止巨型结构一次性解析/写入卡顿。方块数据量按调色板条目大小估算。
CHUNK_DEFAULT = False
CHUNK_MAX_BYTES = 50 * 1024   # 每个分块的数据量上限（50KB）
# 这些名字里带关键字的方块，最后会再写一次并触发邻居更新，避免红石/火把被判成失去支撑
REDSTONE_HINTS = (
    "torch", "repeater", "comparator", "wire", "piston", "observer", "lever",
    "button", "pressure_plate", "redstone", "lamp", "tripwire", "dispenser",
    "dropper", "hopper", "door", "gate", "copper_bulb", "target", "rail",
    "note_block", "daylight_detector", "sculk_sensor", "crafter",
)
SKIP_AIR = True           # 跳过空气
SKIP_VOID = True          # 跳过结构空位（block_indices = -1）

# ---------------------------------------------------------------- 界面
ROWS = 6                  # 每页显示几行
PATH_MAX = 240            # 输入框最大长度
TITLE = u"结构导入器"

# ---------------------------------------------------------------- 调试桥
# 发布作品前改成 False，否则玩家端会开两个本地端口
DEBUG_BRIDGE = False  # 审核要求：已下线调试桥（依赖 socket/xupdate）
DEBUG_BRIDGE_NS = "DebugBridge"                 # 必须和 dbridge/config.py 的 ModName 一致
DEBUG_BRIDGE_SERVER_NAME = "ServerSystem"
DEBUG_BRIDGE_CLIENT_NAME = "ClientSystem"
DEBUG_BRIDGE_SERVER = PKG + ".dbridge.serverSystem.ServerSystem"
DEBUG_BRIDGE_CLIENT = PKG + ".dbridge.clientSystem.ClientSystem"

# ---------------------------------------------------------------- 事件
EV_OPEN = "SiOpen"        # 服务端 -> 客户端：打开界面
EV_DATA = "SiData"        # 服务端 -> 客户端：列表数据
EV_TOAST = "SiToast"      # 服务端 -> 客户端：界面内提示
EV_REQ = "SiReq"          # 客户端 -> 服务端：请求数据 / 切目录
EV_PLACE = "SiPlace"      # 客户端 -> 服务端：放置
