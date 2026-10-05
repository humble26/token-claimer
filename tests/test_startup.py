# -*- coding: utf-8 -*-
r"""启动阶段的回归测试。

背景（用户反馈："你写的程序一打开就闪退了"）：启动脚本用 pythonw 起，
没有控制台，未捕获异常默认被丢掉 —— 双击一下窗口一闪就没，日志里也
什么都不留。而且旧版 `启动.bat` 优先用 `%LOCALAPPDATA%\Python\bin\pythonw.exe`，
路径存在并不代表它带 tkinter，`import tkinter` 失败同样是"闪退"。

这里钉住两件事：
  1. 启动崩溃必须落日志（report_fatal），不能静默消失。
  2. 重复启动要把已有窗口提到前台，而不是弹个框就退出（看起来也像闪退）。

运行：python -m unittest discover -s tests
"""

import ast
import inspect
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for name in ("tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk"):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if name == "tkinter":
            for attr in ("Tk", "Toplevel", "StringVar", "BooleanVar", "IntVar"):
                setattr(mod, attr, object)
        sys.modules[name] = mod

import token_claimer as tc  # noqa: E402


class _FakeRoot:
    """替掉真的 Tk：report_fatal 里只用到 withdraw/destroy。"""

    def withdraw(self):
        pass

    def destroy(self):
        pass


class StartupFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tc_startup_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.orig_dir = tc.config_dir
        self.orig_tk = tc.tk
        self.orig_mb = tc.messagebox
        tc.config_dir = lambda: self.tmp
        tc.tk = types.SimpleNamespace(Tk=_FakeRoot)
        tc.messagebox = types.SimpleNamespace(showerror=lambda *a, **k: None)
        self.addCleanup(lambda: setattr(tc, "config_dir", self.orig_dir))
        self.addCleanup(lambda: setattr(tc, "tk", self.orig_tk))
        self.addCleanup(lambda: setattr(tc, "messagebox", self.orig_mb))

    def test_report_fatal_writes_traceback_to_log(self):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            tc.report_fatal()
        logs = list((self.tmp / "logs").glob("*.log"))
        self.assertEqual(len(logs), 1, "启动失败必须留下日志")
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("启动失败", text)
        self.assertIn("RuntimeError: boom", text)

    def test_report_fatal_never_raises(self):
        """日志目录不可写时也不该再炸一次。"""
        blocker = self.tmp / "blocker"          # 拿一个"文件"当配置目录
        blocker.write_text("x", encoding="utf-8")
        tc.config_dir = lambda: blocker
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            tc.report_fatal()   # 不应抛出


class SecondLaunchTest(unittest.TestCase):
    def test_main_focuses_existing_window_before_warning(self):
        code = ast.unparse(ast.parse(inspect.getsource(tc.main)))
        self.assertIn("focus_existing_instance", code,
                      "重复启动要先把已有窗口提到前台")

    def test_focus_ignores_windows_of_other_apps(self):
        """标题不以 APP_TITLE 开头的一律不碰。"""
        fake = [(1, 424242, "记事本"), (2, 424242, "Token 领取助手 v1.3.0")]
        orig = tc.list_windows
        tc.list_windows = lambda: fake
        self.addCleanup(lambda: setattr(tc, "list_windows", orig))
        if sys.platform != "win32":
            self.skipTest("仅 Windows")
        # 两个窗口都属于别的 PID，但只有第二个标题匹配；函数应选中它。
        picked = []
        orig_show = tc.ctypes.windll.user32.ShowWindow
        try:
            tc.ctypes.windll.user32.ShowWindow = \
                lambda h, c: (picked.append(h), 1)[1]
            self.assertTrue(tc.focus_existing_instance())
        finally:
            tc.ctypes.windll.user32.ShowWindow = orig_show
        self.assertEqual(picked, [2])


class BatchFileEndingTest(unittest.TestCase):
    r"""cmd 解析 .bat 必须用 CRLF。

    真踩过：用 LF 换行重写 启动.bat 后，cmd 把整段切成碎片，报出
    "'lly' is not recognized as an internal or external command" 这种鬼话，
    启动脚本直接失效。这里把所有 .bat 钉死成 CRLF。
    """

    def test_bat_files_use_crlf(self):
        root = Path(__file__).resolve().parent.parent
        bats = sorted(root.glob("*.bat"))
        self.assertTrue(bats, "项目里至少要有一个 .bat")
        for bat in bats:
            with self.subTest(bat=bat.name):
                data = bat.read_bytes()
                self.assertIn(b"\r\n", data, f"{bat.name} 必须是 CRLF 换行")
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"),
                                 f"{bat.name} 里混着裸 LF，cmd 会解析出错")


if __name__ == "__main__":
    unittest.main()
