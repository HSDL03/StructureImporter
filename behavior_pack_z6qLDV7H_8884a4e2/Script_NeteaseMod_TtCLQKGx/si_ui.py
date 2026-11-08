# -*- coding: utf-8 -*-
"""界面：输入目录 + 结构列表 + 翻页。

注意：这个文件被引擎通过 UI 类名单独加载，运行时它的模块全局里**只有本文件
自己定义的名字**（外部的 `from ... import si_cfg` 拿不到，会报 NameError）。
所以这里不依赖 si_cfg / si_util，常量和工具函数全部本地自带。
"""

import time

import mod.client.extraClientApi as clientApi

ScreenNode = clientApi.GetScreenNodeCls()
ROOT = "/si_root"

ROWS = 6
TITLE = u"结构导入器"

try:
    _text = unicode
except NameError:          # 离线校验脚本用 py3 跑
    _text = str


def _u(value):
    if value is None:
        return u""
    if isinstance(value, _text):
        return value
    try:
        return value.decode("utf-8")
    except Exception:
        try:
            return value.decode("gbk", "replace")
        except Exception:
            return _text(value)


def _short(text, limit):
    t = _u(text)
    if len(t) <= limit:
        return t
    return u"…" + t[-(limit - 1):]


def _log(msg):
    try:
        print(u"[StructureIm] " + _u(msg))
    except Exception:
        pass


class SiScreen(ScreenNode):

    def __init__(self, namespace, name, param):
        super(SiScreen, self).__init__(namespace, name, param)
        self.sys = (param or {}).get("client")
        self.page = 0
        self.data = None
        self._dir_shown = u""
        self._born = None
        self._split_on = False

    # ---------------------------------------------------------- 生命周期
    def Create(self):
        self._born = time.time()
        if self.sys is not None:
            self.sys.screen = self
        for i in range(ROWS):
            self._touch("si_row%d" % i, self._row_cb(i))
        self._touch("si_apply", self._on_apply)
        self._touch("si_prev", self._on_prev)
        self._touch("si_next", self._on_next)
        self._touch("si_rescan", self._on_rescan)
        self._touch("si_split", self._on_split)
        self._touch("si_setting", self._on_setting)
        self._touch("si_close", self._on_close)
        self._title(TITLE)
        self._status(u"载入中…")
        self._render_rows([])
        self._render_split(False)
        if self.sys is not None and self.sys.last_data:
            self.apply(self.sys.last_data)

    def Destroy(self):
        if self.sys is not None:
            self.sys.screen = None

    # ---------------------------------------------------------- 控件封装
    def _ctl(self, name):
        try:
            return self.GetBaseUIControl(ROOT + "/" + name)
        except Exception:
            return None

    def _touch(self, name, cb):
        c = self._ctl(name)
        if c is None:
            _log("找不到控件 " + name)
            return
        try:
            b = c.asButton()
            b.AddTouchEventParams({"isSwallow": True})
            b.SetButtonTouchUpCallback(cb)
        except Exception:
            _log("绑定触摸失败 " + name)

    def _label(self, path, text):
        try:
            self.GetBaseUIControl(path).asLabel().SetText(_u(text))
        except Exception:
            pass

    def _title(self, text):
        self._label(ROOT + "/si_title", text)

    def _status(self, text):
        self._label(ROOT + "/si_status", text)

    def _row_text(self, index, text):
        self._label(ROOT + "/si_row%d/button_label" % index, text)

    def _row_visible(self, index, visible):
        c = self._ctl("si_row%d" % index)
        if c is None:
            return
        try:
            c.SetVisible(bool(visible))
        except Exception:
            pass

    def _render_split(self, on):
        self._split_on = bool(on)
        self._label(ROOT + "/si_split/button_label",
                    u"分区：开" if on else u"分区：关")

    def _get_path(self):
        c = self._ctl("si_path")
        if c is None:
            return u""
        try:
            return _u(c.asTextEditBox().GetEditText())
        except Exception:
            return u""

    def _set_path(self, text):
        c = self._ctl("si_path")
        if c is None:
            return
        try:
            c.asTextEditBox().SetEditText(_u(text))
        except Exception:
            _log("写入输入框失败")

    # ---------------------------------------------------------- 渲染
    def apply(self, data):
        self.data = data or {}
        self.page = int(self.data.get("page") or 0)

        d = _u(self.data.get("dir"))
        if d and d != self._dir_shown:
            self._set_path(d)
            self._dir_shown = d

        rows = self.data.get("rows") or []
        self._render_rows(rows)
        self._render_split(bool(self.data.get("split")))

        err = _u(self.data.get("err"))
        total = int(self.data.get("total") or 0)
        pages = int(self.data.get("pages") or 1)
        if err:
            self._status(err)
        elif self.data.get("busy"):
            self._status(u"放置中… 共 %d 个结构" % total)
        else:
            self._status(u"%d 个结构 · 第 %d/%d 页 · 点一下放在身前" %
                         (total, self.page + 1, pages))

    def _render_rows(self, rows):
        for i in range(ROWS):
            if i < len(rows):
                item = rows[i]
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    num, name, rel = item[0], _u(item[1]), _u(item[2])
                else:
                    num, name, rel = i + 1, _u(item), _u(item)
                text = u"%s. §f%s §7%s" % (_u(num), _short(name, 20), _short(rel, 24))
                self._row_text(i, text)
                self._row_visible(i, True)
            else:
                self._row_text(i, u"")
                self._row_visible(i, False)

    # ---------------------------------------------------------- 交互
    def _row_cb(self, index):
        def cb(args):
            rows = (self.data or {}).get("rows") or []
            if index < len(rows):
                which = rows[index][1]
                if self.sys is not None:
                    self.sys.place(which, 0)
        return cb

    def _on_apply(self, args):
        if self.sys is None:
            return
        text = self._get_path().strip()
        if not text:
            self._status(u"先在输入框里填一个目录")
            return
        self._dir_shown = text
        self.sys.request(0, root=text)

    def _on_prev(self, args):
        if self.sys is not None and self.page > 0:
            self.sys.request(self.page - 1)

    def _on_next(self, args):
        if self.sys is not None:
            pages = int((self.data or {}).get("pages") or 1)
            if self.page + 1 < pages:
                self.sys.request(self.page + 1)

    def _on_rescan(self, args):
        if self.sys is not None:
            self.sys.request(self.page, rescan=True)

    def _on_split(self, args):
        if self.sys is None:
            return
        want = not bool(getattr(self, "_split_on", False))
        self._render_split(want)          # 先本地翻，服务端回来会再校正一次
        self.sys.toggle_split(want)

    def _on_setting(self, args):
        # 创建瞬间的伪触摸一样要挡
        if self._born is not None and time.time() - self._born < 0.6:
            return
        if self.sys is not None:
            self.sys.open_settings()

    def _on_close(self, args):
        # 界面刚推入时会被塞一次伪触摸事件，0.6 秒内忽略，否则一开就闪没
        if self._born is not None and time.time() - self._born < 0.6:
            _log("忽略创建瞬间的关闭事件")
            return
        if self.sys is not None:
            self.sys.close()

# ---------------------------------------------------------------- 设置页行表
# 顺序与 resource_pack/.../ui/structure_im_setting.json 的 si_r0..si_r15 一一对应
SET_ROWS = [
    ("split",      u"分区模式",   "bool"),
    ("splitz",     u"分区边长",   "splitz"),
    ("tick",       u"防缺失",     "bool"),
    ("chunk",      u"分块模式",   "bool"),
    ("map",        u"地图画模式", "bool"),
    ("turbo",      u"急速模式",   "bool"),
    ("freeze",     u"冻结时间",   "bool"),
    ("ta",         u"纯常加载",   "bool"),
    ("maparea",    u"常加载形状", "maparea"),
    ("threads",    u"解析线程",   "int"),
    ("parallel",   u"邻区并行",   "int"),
    ("budget",     u"每tick预算", "ms"),
    ("mapspeed",   u"地图速度",   "mapspeed"),
    ("znum",       u"每幅巡区",   "zone"),
    ("zwait",      u"每区停留",   "sec"),
    ("freezetime", u"守水秒数",   "sec"),
    ("bigwait",    u"跨行降速",   "sec"),
    ("nbtlimit",   u"物品NBT",    "kb"),
]


def _opt_text(kind, value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 0
    if kind == "bool":
        return u"§a开" if v else u"§7关"
    if kind == "maparea":
        return u"方形" if v else u"圆形"
    if kind == "splitz":
        return u"自动" if v <= 0 else u"%d 格" % v
    if kind == "mapspeed":
        return [u"稳", u"快", u"极速"][max(0, min(2, v))]
    if kind == "ms":
        return u"%d ms" % v
    if kind == "sec":
        return u"%d 秒" % v
    if kind == "zone":
        return u"%d 区" % v
    if kind == "kb":
        return u"不裁剪" if v <= 0 else u"%d KB" % v
    return u"%d" % v


class SiSettingScreen(ScreenNode):
    """设置页：一行一个可调参数，点 − / + 就地改，自动存档

    控件名固定 si_r{i}_name/val/minus/plus，行顺序由 SET_ROWS 决定。
    以后要加参数：SET_ROWS 追加一行 + 重新生成 ui json 即可。
    """

    ROOT = "/si_set_root"

    def __init__(self, namespace, name, param):
        super(SiSettingScreen, self).__init__(namespace, name, param)
        self.sys = (param or {}).get("client")
        self._born = None
        self._vals = {}

    def Create(self):
        self._born = time.time()
        if self.sys is not None:
            self.sys.set_screen = self
        for i, row in enumerate(SET_ROWS):
            self._touch("si_r%d_minus" % i, self._on_step(row[0], -1))
            self._touch("si_r%d_plus" % i, self._on_step(row[0], 1))
            self._label(self.ROOT + "/si_r%d_name" % i, row[1])
        self._touch("si_fast_btn", self._on_fast)
        self._touch("si_set_close", self._on_close)
        for i, row in enumerate(SET_ROWS):
            self._render_row(i, 0)
        data = self.sys.last_data if self.sys is not None else None
        if data:
            self.apply(data)

    def Destroy(self):
        if self.sys is not None and self.sys.set_screen is self:
            self.sys.set_screen = None

    # ---- 控件封装
    def _ctl(self, name):
        try:
            return self.GetBaseUIControl(self.ROOT + "/" + name)
        except Exception:
            return None

    def _touch(self, name, cb):
        c = self._ctl(name)
        if c is None:
            _log("找不到控件 " + name)
            return
        try:
            b = c.asButton()
            b.AddTouchEventParams({"isSwallow": True})
            b.SetButtonTouchUpCallback(cb)
        except Exception:
            _log("绑定触摸失败 " + name)

    def _label(self, path, text):
        try:
            self.GetBaseUIControl(path).asLabel().SetText(_u(text))
        except Exception:
            pass

    def _render_row(self, i, value):
        row = SET_ROWS[i]
        self._vals[row[0]] = value
        self._label(self.ROOT + "/si_r%d_val" % i, _opt_text(row[2], value))

    # ---- 数据
    def apply(self, data):
        data = data or {}
        opts = data.get("opts") or {}
        legacy = {"split": data.get("split"), "tick": data.get("tick"),
                  "chunk": data.get("chunk"), "map": data.get("map"),
                  "turbo": data.get("turbo"), "threads": data.get("threads")}
        for i, row in enumerate(SET_ROWS):
            key = row[0]
            if key in opts:
                value = opts.get(key)
            elif key == "threads" and legacy.get("threads") is not None:
                value = 2 if legacy.get("threads") else 1
            elif legacy.get(key) is not None:
                value = 1 if legacy.get(key) else 0
            else:
                value = self._vals.get(key, 0)
            self._render_row(i, value)

    # ---- 交互
    def _on_step(self, key, delta):
        def cb(args):
            if self.sys is not None:
                self.sys.set_opt(key, delta)
        return cb

    def _on_fast(self, args):
        if self.sys is not None:
            self.sys.run_fast()

    def _on_close(self, args):
        # 创建瞬间的伪触摸同样挡掉，避免设置页一开就关
        if self._born is not None and time.time() - self._born < 0.6:
            _log("忽略设置页创建瞬间的关闭事件")
            return
        if self.sys is not None:
            self.sys.close_settings()
