# -*- coding: utf-8 -*-
"""零碎工具：编码、路径、日志"""

import os
import traceback

try:
    text_type = unicode
    binary_type = str
except NameError:          # 校验脚本用 py3 跑
    text_type = str
    binary_type = bytes


def u(value):
    """任意值 -> unicode"""
    if value is None:
        return u""
    if isinstance(value, text_type):
        return value
    if isinstance(value, binary_type):
        for enc in ("utf-8", "gbk", "mbcs"):
            try:
                return value.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return value.decode("utf-8", "replace")
    return u(str(value))


def b(value):
    """unicode -> utf-8 字节串（SetExtraData 只认 str）"""
    v = u(value)
    try:
        return v.encode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return str(v)


def norm_path(text):
    """玩家输入的路径 -> 规范化字符串。去引号、去空白、正斜杠转反斜杠"""
    p = u(text).strip().strip(u'"').strip(u"'").strip()
    p = p.replace(u"/", u"\\")
    while len(p) > 3 and p.endswith(u"\\"):
        p = p[:-1]
    return p


def is_dir(path):
    try:
        return bool(path) and os.path.isdir(path)
    except Exception:
        return False


def short(text, limit):
    t = u(text)
    if len(t) <= limit:
        return t
    return u"…" + t[-(limit - 1):]


def log(*args):
    try:
        print(u"[StructureIm] " + u" ".join([u(a) for a in args]))
    except Exception:
        pass


def log_exc(tag):
    try:
        print(u"[StructureIm] %s 异常:\n%s" % (u(tag), u(traceback.format_exc())))
    except Exception:
        pass
