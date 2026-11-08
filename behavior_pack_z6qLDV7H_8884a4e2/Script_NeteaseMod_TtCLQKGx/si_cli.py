# -*- coding: utf-8 -*-
"""客户端系统：注册界面、和对端转发数据"""

import mod.client.extraClientApi as clientApi

from Script_NeteaseMod_TtCLQKGx import si_cfg as cfg
from Script_NeteaseMod_TtCLQKGx import si_util as util


class SiClientSystem(clientApi.GetClientSystemCls()):

    def __init__(self, namespace, systemName):
        super(SiClientSystem, self).__init__(namespace, systemName)
        self.screen = None
        self.set_screen = None
        self.last_data = None
        self.registered = False

        ns = clientApi.GetEngineNamespace()
        sn = clientApi.GetEngineSystemName()
        self.ListenForEvent(ns, sn, "UiInitFinished", self, self.on_ui_init)
        self.ListenForEvent(cfg.MOD_NAME, cfg.SERVER_SYSTEM_NAME, cfg.EV_OPEN, self, self.on_open)
        self.ListenForEvent(cfg.MOD_NAME, cfg.SERVER_SYSTEM_NAME, cfg.EV_DATA, self, self.on_data)
        self.ListenForEvent(cfg.MOD_NAME, cfg.SERVER_SYSTEM_NAME, cfg.EV_TOAST, self, self.on_toast)

    # ---------------------------------------------------------- UI 注册
    def on_ui_init(self, args):
        self.register()

    def register(self):
        if self.registered:
            return
        try:
            clientApi.RegisterUI(cfg.MOD_NAME, cfg.UI_KEY, cfg.UI_CLS, cfg.UI_DEF)
            clientApi.RegisterUI(cfg.MOD_NAME, cfg.SET_KEY, cfg.SET_CLS, cfg.SET_DEF)
            self.registered = True
            util.log("ui registered")
        except Exception:
            util.log_exc("register ui")

    def on_open(self, args):
        self.register()
        # 已经开着就别再推：重复 PushScreen 会叠出多层界面，看起来像在闪
        if self.screen is not None:
            self.request(0)
            return
        self.request(0)
        try:
            clientApi.PushScreen(cfg.MOD_NAME, cfg.UI_KEY, {"client": self})
        except Exception:
            util.log_exc("push screen")

    # ---------------------------------------------------------- 数据
    def request(self, page=0, root=None, rescan=False):
        data = {"pid": self.pid(), "page": int(page)}
        if root is not None:
            data["root"] = util.u(root)
        if rescan:
            data["rescan"] = True
        try:
            self.NotifyToServer(cfg.EV_REQ, data)
        except Exception:
            util.log_exc("request")

    def place(self, which, rot=0):
        try:
            self.NotifyToServer(cfg.EV_PLACE, {"pid": self.pid(), "which": util.u(which), "rot": int(rot)})
        except Exception:
            util.log_exc("place")

    def toggle_split(self, value):
        """界面上的分区开关：直接把目标状态发给服务端"""
        self.send_settings({"split": bool(value)})

    def toggle_tick(self, value):
        self.send_settings({"tick": bool(value)})

    def toggle_chunk(self, value):
        self.send_settings({"chunk": bool(value)})

    def toggle_map(self, value):
        self.send_settings({"map": bool(value)})

    def toggle_turbo(self, value):
        self.send_settings({"turbo": bool(value)})

    def toggle_threads(self, value):
        self.send_settings({"threads": bool(value)})

    def set_speed(self, value):
        self.send_settings({"speed": value})

    def set_opt(self, key, delta):
        """设置页的一行：key + 增减方向（-1 / +1），具体夹值交给服务端"""
        self.send_settings({"opt": util.u(key), "delta": int(delta)})

    def run_fast(self):
        self.send_settings({"opt": "fast", "delta": 0})

    def send_settings(self, payload):
        try:
            data = {"pid": self.pid()}
            if self.last_data:
                data["page"] = int(self.last_data.get("page") or 0)
            data.update(payload)
            self.NotifyToServer(cfg.EV_REQ, data)
        except Exception:
            util.log_exc("send settings")

    def open_settings(self):
        self.register()
        if self.set_screen is not None:
            return
        try:
            clientApi.PushScreen(cfg.MOD_NAME, cfg.SET_KEY, {"client": self})
        except Exception:
            util.log_exc("push settings")

    def pid(self):
        try:
            return clientApi.GetLocalPlayerId()
        except Exception:
            return None

    def on_data(self, args):
        self.last_data = args
        if self.screen is not None:
            self.screen.apply(args)
        if self.set_screen is not None:
            self.set_screen.apply(args)

    def on_toast(self, args):
        util.log("toast", args.get("msg"))

    def close(self):
        # 实测这个 SDK 的 PopScreen 不接受参数（传两个会 TypeError）
        try:
            clientApi.PopScreen()
        except Exception:
            util.log_exc("pop screen")

    def close_settings(self):
        if self.set_screen is None:
            return
        self.set_screen = None
        self.close()
