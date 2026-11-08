# -*- coding: utf-8 -*-
"""结构导入器 · Mod 入口

注意（踩过的坑）：这里**不要** `from ... import si_cfg as cfg` 这种把模块对象
绑进 modMain 命名空间的写法。引擎的 ImportModMain 会遍历 modMain 模块的
命名空间，把任何带 MOD_NAME 属性的对象当成 Mod 类去实例化；si_cfg 里正好
定义了 MOD_NAME，模块对象不可调用 → 日志刷
`TypeError: 'module' object is not callable`（引擎已知 bug，无异常捕获）。
所以下面只按名字导入需要的常量。
"""

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

from Script_NeteaseMod_TtCLQKGx.si_cfg import (
    MOD_NAME, SERVER_SYSTEM_NAME, SERVER_CLS,
    CLIENT_SYSTEM_NAME, CLIENT_CLS,
)


@Mod.Binding(name=MOD_NAME, version="1.0.0")
class StructureImMod(object):

    def __init__(self):
        pass

    @Mod.InitServer()
    def on_server_init(self):
        serverApi.RegisterSystem(MOD_NAME, SERVER_SYSTEM_NAME, SERVER_CLS)

    @Mod.DestroyServer()
    def on_server_destroy(self):
        pass

    @Mod.InitClient()
    def on_client_init(self):
        clientApi.RegisterSystem(MOD_NAME, CLIENT_SYSTEM_NAME, CLIENT_CLS)

    @Mod.DestroyClient()
    def on_client_destroy(self):
        pass
