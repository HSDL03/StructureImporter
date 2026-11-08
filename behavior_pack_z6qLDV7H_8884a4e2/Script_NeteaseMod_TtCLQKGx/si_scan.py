# -*- coding: utf-8 -*-
"""目录扫描：把一棵目录下所有 .mcstructure 收进列表"""

import os

from Script_NeteaseMod_TtCLQKGx import si_cfg as cfg
from Script_NeteaseMod_TtCLQKGx import si_util as util


class Entry(object):
    __slots__ = ("name", "rel", "path")

    def __init__(self, name, rel, path):
        self.name = name
        self.rel = rel
        self.path = path

    def label(self):
        return self.rel


class Library(object):

    def __init__(self):
        self.root = u""
        self.entries = []
        self.err = u""

    # ---------------------------------------------------------- 目录
    def set_root(self, path):
        self.root = util.norm_path(path)
        return self.rescan()

    def rescan(self):
        self.entries = []
        if not self.root:
            self.err = u"还没设置目录。点上面的输入框填一个，或在聊天栏用 /structureim dir <路径>"
            return self.entries
        if not util.is_dir(self.root):
            self.err = u"目录不存在或打不开：" + self.root
            return self.entries
        self.err = u""

        found = []
        base = self.root
        base_len = len(base.rstrip(u"\\"))
        for dirpath, dirnames, filenames in os.walk(base):
            depth = dirpath[base_len:].count(os.sep)
            if depth >= cfg.MAX_DEPTH:
                dirnames[:] = []
            for fn in filenames:
                lower = fn.lower()
                for ext in cfg.EXTS:
                    if lower.endswith(ext):
                        full = os.path.join(dirpath, fn)
                        rel = full[base_len:].lstrip(u"\\")
                        found.append(Entry(fn[:len(fn) - len(ext)], rel, full))
                        break
                if len(found) >= cfg.MAX_ENTRIES:
                    break
            if len(found) >= cfg.MAX_ENTRIES:
                break

        found.sort(key=lambda e: e.rel.lower())
        self.entries = found
        if not found:
            self.err = u"这个目录里没有 .mcstructure 文件"
        return self.entries

    # ---------------------------------------------------------- 查找
    def find(self, key):
        """key 可以是 1 起的序号，也可以是名字片段"""
        text = util.u(key).strip()
        if not text:
            return None
        if text.isdigit():
            i = int(text) - 1
            if 0 <= i < len(self.entries):
                return self.entries[i]
            return None
        low = text.lower()
        for e in self.entries:
            if low == e.name.lower():
                return e
        for e in self.entries:
            if low in e.rel.lower():
                return e
        return None
