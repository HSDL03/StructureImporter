# -*- coding: utf-8 -*-
# 引擎 ImportModMain 兼容入口：从包上取 modMain 拿到的应是可调用的类，
# 而不是 modMain 子模块对象（否则报 'module' object is not callable）
from Script_NeteaseMod_TtCLQKGx.modMain import StructureImMod as modMain
