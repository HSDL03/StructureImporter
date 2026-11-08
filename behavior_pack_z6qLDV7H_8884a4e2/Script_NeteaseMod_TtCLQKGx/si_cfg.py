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
CMD_ACTIONS = ["ui", "dir", "scan", "list", "place", "stop", "pause", "info", "selftest", "test", "split", "map", "wash",
               "zwait", "znum", "zsec", "bigwait", "budget", "threads", "parallel",
               "nbtlimit", "freeze", "freezetime", "mapspeed", "maparea", "turbo", "fast",
               "manual", "help2"]

# ---------------------------------------------------------------- 存档
# SetExtraData 只接受 str，中文必须编码成 utf-8 字节串
EXTRA_KEY = "si_dir"
# 配置版本：低于这个值的存档会被迁移（2 = 地图画速度档强制回到「稳」）
CFG_VERSION = 2

# ---------------------------------------------------------------- 分区导入
SPLIT_DEFAULT = False     # 默认关，用 /structureim split on 或界面按钮打开
SPLIT_SIZE = 16           # 分区边长（格）：默认按 16×16 的区块切片
# 自适应分区：16×16 对"又薄又大"的平面是灾难——768×768 单层会被切成 2304 个区，
# 每区才 256 块，光是来回 TP + 等区块就比写方块慢十倍。按体量把边长放大到
# 每区 SECTION_TARGET 块左右，分区数直接掉两个数量级。
SPLIT_AUTO = True         # 关掉就永远用 SPLIT_SIZE
SPLIT_SIZE_MAX = 128      # 自适应上限（128 格 = 8×8 区块，正好在玩家加载半径内）
SECTION_TARGET = 16000    # 每区目标方块数（越大分区越少、TP 越少，但单区更重）

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
BATCH = 600               # 每 tick 写入多少方块（自适应算法的起始值/上限参考）
# ---------------------------------------------------------------- 写入节流（防卡）
# 从"每 tick 固定写 N 块"改成"每 tick 固定时间预算"：机器快就多写、慢就少写，
# 帧率不会再被单 tick 的写入量拖崩。耗时用实测吞吐反推下一 tick 的批量。
WRITE_BUDGET_MS = 12.0         # 普通模式：每 tick 最多花多少毫秒写方块（≈旧版固定 600 块）
WRITE_BUDGET_MS_TURBO = 20.0   # 急速模式：几乎榨满一 tick（50ms 里留 20ms 写）
WRITE_BATCH_MIN = 24           # 单 tick 最少写多少（保证推进）
WRITE_BATCH_MAX = 3000         # 普通模式单 tick 上限（急速自动 ×2）
WRITE_BATCH_START = 200        # 起步批量：慢机不会第一 tick 就卡 200ms，三几 tick 自己涨上去
WRITE_PROBE_STEP = 24          # 探时间的初始间隔（之后按实测单块耗时自动收紧）

# ---------------------------------------------------------------- 多线程（后台解析）
# 引擎限制：SetBlockNew 等接口只能在主线程调，所以「并行写方块」不存在。
# 能并行的是**解析**（读文件 / 解压 / NBT 遍历），把它挪出主线程后，
# 点下放置不会再卡住一两秒（大结构尤其明显）。
THREADS_DEFAULT = 1            # 1 = 主线程同步解析（最稳）；>=2 = 后台线程解析
THREADS_MAX = 8
BG_LOAD_MIN_KB = 128           # 小于这个体积的结构同步解析更快（省掉线程开销）

# ---------------------------------------------------------------- 相邻分区并行
# 引擎限制：SetBlockNew 只能在主线程调，所以「多线程写方块」不存在。
# 能真正并行的是**区块加载**（引擎异步做的）：写当前分区时，顺手给接下来的
# 相邻分区挂常加载区块，等轮到它时区块早已就绪 —— 效果就是邻区并行处理。
PARALLEL_DEFAULT = 1           # 1 = 关；2~4 = 同时/提前处理这么多个相邻分区
PARALLEL_MAX = 4
PREFETCH_WAIT = 8              # 已预取的分区，轮到它时只需再等多少 tick（0.4 秒）
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
MAP_TA_RADIUS = 4          # 旧版圆形常加载半径（已改用方形 _map_ta_add，仅留作对照）
MAP_TA_WAIT = 60           # 常加载区块挂上后等多少 tick 让区块真正加载完再 give
MAP_AREA_SQUARE = True     # 常加载形状：True 方形（一次盖住整幅）/ False 圆形（老写法，兼容用）

# ---------------------------------------------------------------- 地图画速度档
# 慢的根源：每张图都要「挂常加载 -> 等 3 秒 -> TP 九个区、每区停 3 秒 -> 拿图 4 秒」。
# 三处提速：①常加载改成方形精确覆盖整幅（圆会切掉四角，所以以前得靠巡逻补）
#          ②下一张图的常加载在画当前图时提前挂好（等待与绘制重叠）
#          ③每区等待从"秒级"降到"tick 级"（区块本来就都加载着）
# 0 = 稳（默认：老的圆形常加载 + 每区按秒等，最保险）  1 = 快  2 = 极速
# 挂画阶段一旦报错就留在 0，别开 1/2：快档的等待被压到 tick 级，区块没跟上就会缺图/报错
MAP_SPEED_DEFAULT = 0
MAP_SQ_Y = 40              # 方形常加载的纵向厚度（区块只看 xz，给够就行）
MAP_TA_WAIT_FAST = 30      # 快档：本张图第一次挂常加载后等多少 tick
MAP_ZONE_WAIT = 10         # 快档：巡逻每个区停多少 tick
MAP_ZONE_WAIT_FAST = 4     # 极速档：每区停多少 tick
MAP_HOLD_TICKS = 30        # 快档：地图在主手停留多少 tick（1.5 秒）
MAP_HOLD_FAST = 10         # 极速档：0.5 秒
MAP_PREFETCH = True        # 快档/极速档：提前给下一张图挂常加载（overlap 掉等待）
MAP_MOUNT_BATCH = 4        # 挂框每 tick 挂几张（原来一张 5 tick，10 张要 2.5 秒）
MAP_MOUNT_WAIT = 20        # 全部画完回墙前等多少 tick（墙区块加载）

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
    "dropper", "hopper", "copper_bulb", "target",
    "note_block", "daylight_detector", "sculk_sensor", "crafter",
)
# 这些方块绝对不能进"红石唤醒"批次：它们是**多格结构/需要支撑**的方块，
# 带邻居更新重写一次就会当场判定失去支撑然后掉落（门只剩半扇、铁轨弹飞）。
# 真机踩坑：门在自己的位置被重写后，另一半还没写上去 -> 整扇门变成掉落物。
WAKE_SKIP = ("door", "gate", "rail", "bed", "tall_grass", "tallgrass",
             "sunflower", "lilac", "rose_bush", "peony", "vine", "ladder",
             "sign", "banner", "carpet", "snow_layer", "candle", "chain")

# 液体冲刷校正：水放下去会冲掉一批"靠支撑站着"的方块（火把、拉杆、按钮、
# 铁轨、告示牌、花、红石线…）。液体阶段结束后按原结构挨个核对一遍，
# 少了就补回去（每个位置只补一次，且不触发邻居更新）。
WASH_HINTS = (
    "torch", "lantern", "lever", "button", "rail", "sign", "banner", "ladder",
    "vine", "flower", "tulip", "paeonia", "allium", "cornflower", "dandelion",
    "poppy", "blue_orchid", "azure_bluet", "oxeye", "lily", "rose", "sunflower",
    "lilac", "peony", "sapling", "wheat", "carrot", "potato", "beetroot",
    "sugar_cane", "bamboo", "kelp", "seagrass", "coral", "tripwire", "wire",
    "repeater", "comparator", "string", "carpet", "snow_layer", "banner",
    "candle", "chain", "pot", "skull", "head", "frame", "painting",
)
WASH_MAX_FIX = 800        # 一次导入最多补多少个被水冲掉的方块（防止无限补）
SKIP_AIR = True           # 跳过空气
SKIP_VOID = True          # 跳过结构空位（block_indices = -1）

# ---------------------------------------------------------------- 旧名 / 引擎里不存在的方块
# 结构文件里有些名字是别的版本/工具的遗留写法，直接 SetBlockNew 会静默返回 False
# （然后被当成"区块没加载"无限重试）。这里换成引擎认的等价方块。
BLOCK_ALIAS = {
    "minecraft:flowing_water": "minecraft:water",     # 基岩版只有 water + liquid_depth
    "minecraft:flowing_lava": "minecraft:lava",
    "minecraft:lit_furnace": "minecraft:furnace",
    "minecraft:lit_pumpkin": "minecraft:jack_o_lantern",
    "minecraft:unlit_redstone_torch": "minecraft:redstone_torch",
    "minecraft:powered_repeater": "minecraft:unpowered_repeater",
    "minecraft:powered_comparator": "minecraft:unpowered_comparator",
    "minecraft:reeds": "minecraft:reeds",
}
# 这些是"怎么放都会掉/放不出来"的运行时方块，出现就跳过并单独计数，不进失败重试
SKIP_BLOCKS = (
    "minecraft:moving_block", "minecraft:piston_arm_collision",
    "minecraft:sticky_piston_arm_collision", "minecraft:light_block",
    "minecraft:fire_block", "minecraft:unknown", "minecraft:deny",
    "minecraft:allow", "minecraft:border_block", "minecraft:client_request_placeholder_block",
)
# 液体：单独排队到最后写（先放固体再放水，水才不会在方块没到位时到处流）
LIQUID_BLOCKS = ("minecraft:water", "minecraft:lava")
# 支持含水的方块（states 里有 waterlogged 就自动带水）
WATERLOG_KEYS = ("waterlogged",)

# ---------------------------------------------------------------- 冻结（水不乱跑）
# 导入期间把"时间流逝"按住：液体单独最后写入，写完再守若干秒把被引擎改动的水改回去；
# 同时尽量关掉随机刻/昼夜/天气（只改能读到的游戏规则，结束时原样恢复）。
FREEZE_DEFAULT = True     # 默认开启
FREEZE_SECS = 6.0         # 全部写完后继续守多少秒液体（0 = 不守，只做最后写入）
FREEZE_SECS_MAX = 60.0
FREEZE_BUDGET_MS = 2.5    # 守液体时每 tick 最多花多少毫秒
FREEZE_CHECKS = 600       # 守液体时每 tick 最多巡检多少格（读比写便宜，先读后写）
FREEZE_MAX_FIX = 3        # 同一格最多补写几次（补不进去说明它本来就该流走）
FREEZE_FIX_CAP = 4000     # 累计补写超过这个数就提前收手（说明水在真流，越写越卡）回写

# ---------------------------------------------------------------- 物品 NBT 安全阈值
# 「盒中盒」递归结构（潜影盒里装潜影盒，几十层、单件 2MB）原样灌给引擎会卡死/崩，
# 超过阈值就只放物品本体（名称/数量/耐久），NBT 丢掉，并在结束时告诉玩家。
ITEM_NBT_MAX_KB = 0         # 0 = 不裁剪（默认，完整保留盒中盒）；>0 才裁到该 KB 上限
ITEM_NBT_MAX_DEPTH = 12     # NBT 嵌套深度上限（层）
ITEM_NBT_SCAN_NODES = 8000  # 判断时的遍历节点上限（早停，避免判断本身卡住）

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
