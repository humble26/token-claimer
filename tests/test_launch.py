# -*- coding: utf-8 -*-
r"""launch() / do_launch() 启动客户端的回归测试。

背景（真实缺陷）：launch() 把 CREATE_NEW_PROCESS_GROUP 当作 creationflags 使用，
但该常量从未在模块里定义，调用必然抛 NameError。当时 do_launch 只捕获 OSError，
异常于是逃逸到 Tkinter 的 after 回调里；程序以 pythonw（无控制台）运行时 stderr
无人接管，异常被静默丢弃 —— 用户点「立即领取」后日志戛然而止，一个客户端都不会
启动，且查不到任何痕迹。

运行：python -m unittest discover -s tests
"""

import sys
import types
import unittest
from pathlib import Path

# token_claimer.py 在仓库根目录，且模块级 import tkinter；
# 测试环境可能没有 tkinter，先注入桩再导入。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for name in ("tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk"):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if name == "tkinter":
            mod.Tk = object
            mod.Toplevel = object
            mod.StringVar = object
            mod.BooleanVar = object
            mod.IntVar = object
        sys.modules[name] = mod

import token_claimer as tc  # noqa: E402

EXE = r"E:\Zcode\ZCode.exe"


class _StubCfg:
    """不落盘的极简配置，避免测试读写用户真实的 config.json。"""

    def __init__(self, **overrides):
        self.data = {"click_enabled": False, "per_app_click": {}}
        self.data.update(overrides)

    def save(self):
        pass


class _FakePopen:
    calls: list = []

    def __init__(self, cmd, **kwargs):
        _FakePopen.calls.append((cmd, kwargs))


class LaunchTest(unittest.TestCase):
    def setUp(self):
        _FakePopen.calls = []
        self._real_popen = tc.subprocess.Popen
        tc.subprocess.Popen = _FakePopen

    def tearDown(self):
        tc.subprocess.Popen = self._real_popen

    def test_creation_flag_constants_are_defined(self):
        """三个 creationflags 常量都必须存在且为整数（原缺陷即缺失其一）。"""
        for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS",
                     "CREATE_NEW_PROCESS_GROUP"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(tc, name), f"{name} 未定义")
                self.assertIsInstance(getattr(tc, name), int)

    def test_launch_does_not_raise(self):
        """原缺陷的直接复现：不带参数调用 launch() 不得抛 NameError。"""
        eng = tc.ClaimEngine(_StubCfg())
        eng.launch(EXE)          # 抛异常即测试失败

    def test_launch_builds_expected_command(self):
        eng = tc.ClaimEngine(_StubCfg())
        eng.launch(EXE, ("--force-renderer-accessibility",))
        self.assertEqual(len(_FakePopen.calls), 1)
        cmd, kw = _FakePopen.calls[0]
        self.assertEqual(cmd, [EXE, "--force-renderer-accessibility"])
        self.assertEqual(kw["cwd"], r"E:\Zcode")
        self.assertTrue(kw["creationflags"] & tc.DETACHED_PROCESS)
        self.assertTrue(kw["creationflags"] & tc.CREATE_NEW_PROCESS_GROUP)

    def test_do_launch_reports_unexpected_error_instead_of_raising(self):
        """启动失败必须写进日志并返回 False，不能把异常抛给 Tk 回调。"""
        logs = []
        eng = tc.ClaimEngine(_StubCfg(),
                             log=lambda m, tag="info": logs.append((m, tag)))
        app = {"key": "zcode", "label": "ZCode", "exe": EXE}

        def boom(cmd, **kwargs):
            raise NameError("name 'X' is not defined")

        tc.subprocess.Popen = boom
        self.assertFalse(eng.do_launch(app))          # 不抛异常
        self.assertTrue(any(tag == "warn" and "启动 ZCode 失败" in m
                            for m, tag in logs), logs)

    def test_do_launch_returns_true_on_success(self):
        logs = []
        eng = tc.ClaimEngine(_StubCfg(),
                             log=lambda m, tag="info": logs.append((m, tag)))
        app = {"key": "zcode", "label": "ZCode", "exe": EXE}
        self.assertTrue(eng.do_launch(app))
        self.assertTrue(any("已启动 ZCode" in m for m, _ in logs), logs)


if __name__ == "__main__":
    unittest.main()
