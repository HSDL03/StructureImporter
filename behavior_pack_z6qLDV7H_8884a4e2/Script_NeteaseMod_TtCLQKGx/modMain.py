# -*- coding: utf-8 -*-
"""结构导入器 · Mod 入口"""

import os
import sys

try:
    from mod.common.mod import Mod
except ImportError:
    from common.mod import Mod

# 兼容引擎不同的加载布局：包根与脚本目录都塞进 sys.path
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    for _p in (os.path.dirname(_here), _here):
        if _p and _p not in sys.path:
            sys.path.insert(0, _p)
except Exception:
    pass

import mod.server.extraServerApi as serverApi
import mod.client.extraClientApi as clientApi

from Script_NeteaseMod_TtCLQKGx import si_cfg as cfg


def _bind_debug_bridge(api, side):
    """把调试桥挂到同一套 Mod 上：它用自己的命名空间注册系统，和我们互不影响"""
    if not cfg.DEBUG_BRIDGE:
        return
    try:
        if side == "server":
            api.RegisterSystem(cfg.DEBUG_BRIDGE_NS,
                               cfg.DEBUG_BRIDGE_SERVER_NAME,
                               cfg.DEBUG_BRIDGE_SERVER)
        else:
            api.RegisterSystem(cfg.DEBUG_BRIDGE_NS,
                               cfg.DEBUG_BRIDGE_CLIENT_NAME,
                               cfg.DEBUG_BRIDGE_CLIENT)
    except Exception:
        import traceback
        print("[StructureIm] debug bridge 注册失败:\n" + traceback.format_exc())


@Mod.Binding(name=cfg.MOD_NAME, version="1.0.0")
class StructureImMod(object):

    def __init__(self):
        pass

    @Mod.InitServer()
    def on_server_init(self):
        serverApi.RegisterSystem(cfg.MOD_NAME, cfg.SERVER_SYSTEM_NAME, cfg.SERVER_CLS)
        _bind_debug_bridge(serverApi, "server")

    @Mod.DestroyServer()
    def on_server_destroy(self):
        pass

    @Mod.InitClient()
    def on_client_init(self):
        clientApi.RegisterSystem(cfg.MOD_NAME, cfg.CLIENT_SYSTEM_NAME, cfg.CLIENT_CLS)
        _bind_debug_bridge(clientApi, "client")

    @Mod.DestroyClient()
    def on_client_destroy(self):
        pass


# 引擎 ImportModMain 会在 modMain 模块/包上找名为 modMain 的可调用入口并实例化；
# 缺了这个别名它会拿到"子模块对象"去调用，日志里报
# TypeError: 'module' object is not callable（不影响功能，但难看）。类实例化无副作用。
modMain = StructureImMod
