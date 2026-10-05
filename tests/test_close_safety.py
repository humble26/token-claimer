# -*- coding: utf-8 -*-
r"""关闭客户端 / 校准的安全性回归测试。

背景（一次真实事故）：校准脚本先 `taskkill /IM`、4 秒后升级成
`taskkill /IM /T /F` 硬杀整棵进程树，紧接着又把客户端拉起来 —— 用户正在
用的 TraeWork CN 直接变成"未响应"。根因是两件事：

  1. `taskkill /IM`（不带 /F）对 Electron 客户端等于没用：渲染/GPU/工具子
     进程都没有顶层窗口，关不掉，于是"礼貌关闭"必然失败、必然升级成硬杀。
  2. 硬杀会把 Chromium 的用户数据目录锁与 GPU 缓存留在脏状态，紧接着启动
     就起不来或未响应。

所以这里钉住两条不许回退的约束：礼貌关闭走 WM_CLOSE；校准全程只读。

运行：python -m unittest discover -s tests
"""

import ast
import inspect
import os
import sys
import time
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


class _StubCfg:
    def __init__(self):
        self.data = {"click_keywords": list(tc.CLICK_KEYWORDS_DEFAULT),
                     "per_app_click": {}}

    def save(self):
        pass


def _calibrate_code() -> str:
    """run_calibrate 的可执行代码（去掉 docstring 与注释）。

    注释和 docstring 里会提到"上一版用 taskkill 硬杀"这类历史说明，直接对
    源码字符串做包含判断会被它们误伤。
    """
    tree = ast.parse(inspect.getsource(tc.run_calibrate))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    return ast.unparse(fn)


class GracefulCloseTest(unittest.TestCase):
    """礼貌关闭必须是 WM_CLOSE，不能是 taskkill。"""

    def setUp(self):
        self.calls = []
        self.orig_post = tc._post_wm_close
        self.orig_run = tc.subprocess.run
        tc._post_wm_close = lambda image: (self.calls.append(image), 1)[1]
        tc.subprocess.run = lambda *a, **k: self.calls.append(("shell", a))
        self.addCleanup(lambda: setattr(tc, "_post_wm_close", self.orig_post))
        self.addCleanup(lambda: setattr(tc.subprocess, "run", self.orig_run))
        self.eng = tc.ClaimEngine(_StubCfg())

    def test_graceful_close_posts_wm_close(self):
        self.assertTrue(self.eng.close_graceful("X.exe"))
        self.assertEqual(self.calls, ["X.exe"])

    def test_graceful_close_never_shells_out(self):
        """一旦这里冒出 taskkill，Electron 客户端就又会被硬杀。"""
        self.eng.close_graceful("X.exe")
        self.assertEqual([c for c in self.calls if isinstance(c, tuple)], [])

    def test_graceful_close_on_dead_process_is_false_not_raise(self):
        tc._post_wm_close = lambda image: 0
        self.assertFalse(self.eng.close_graceful("X.exe"))

    def test_force_close_still_available_as_last_resort(self):
        """强制结束保留给"客户端自己不肯退"的兜底场景。"""
        self.eng.close_force("X.exe")
        cmds = [c[1][0] for c in self.calls if isinstance(c, tuple)]
        self.assertTrue(any("taskkill" in cmd for cmd in cmds))


class WmCloseRealWindowTest(unittest.TestCase):
    """真建一个窗口、真发一条 WM_CLOSE —— 这段代码必须真跑起来过一次。

    来历：`ctypes.wintypes` 在只 `import ctypes` 时并不存在（实测
    AttributeError），而上面那些用例把 _post_wm_close 换成了桩，于是这个必
    然崩溃的错误一直藏着，直到真去关客户端才会炸。所以这里不打桩。
    """

    def _real_tk(self):
        """本模块为了无头导入给 tkinter 塞了桩，这里换回真的。"""
        for name in ("tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog",
                     "tkinter"):
            sys.modules.pop(name, None)
        import tkinter
        return tkinter

    def test_wm_close_reaches_a_real_window(self):
        if sys.platform != "win32":
            self.skipTest("仅 Windows")
        try:
            tkinter = self._real_tk()
            root = tkinter.Tk()
        except Exception as e:            # 无显示环境等
            self.skipTest(f"没有可用的 Tk 环境：{e!r}")
        root.title("wmclose_probe")
        root.update()

        def gone() -> bool:
            # 窗口被销毁后连 winfo 都调不了，TclError 本身就是"已经没了"
            try:
                return not root.winfo_exists()
            except tkinter.TclError:
                return True

        def cleanup():
            try:
                root.destroy()
            except tkinter.TclError:
                pass

        self.addCleanup(cleanup)

        # 只对自己这个 PID 发，绝不波及其它 python 进程
        sent = tc._post_wm_close_to({os.getpid()})
        self.assertGreater(sent, 0, "没能向自己的可见窗口发出 WM_CLOSE")

        for _ in range(40):
            root.update()
            if gone():
                break
            time.sleep(0.05)
        self.assertTrue(gone(), "窗口应收到 WM_CLOSE 并被销毁")


class CalibrateSafetyTest(unittest.TestCase):
    """校准只读：不关闭、不重启用户正在运行的应用。"""

    def test_calibrate_never_closes_the_app(self):
        code = _calibrate_code()
        for banned in ("close_graceful", "close_force", "taskkill"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, code)

    def test_calibrate_only_launches_when_not_running(self):
        code = _calibrate_code()
        self.assertIn("has_window", code, "必须先判断它是否在运行")
        self.assertIn("launch", code)

    def test_calibrate_bails_out_on_empty_tree_instead_of_restarting(self):
        """树是空的要提示用户自己退出，而不是替他重启。"""
        self.assertIn("tree_size", _calibrate_code())

    def test_calibrate_takes_open_menu_flag(self):
        sig = inspect.signature(tc.run_calibrate)
        self.assertIn("open_menu", sig.parameters)


if __name__ == "__main__":
    unittest.main()
