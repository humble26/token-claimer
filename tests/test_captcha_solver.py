# -*- coding: utf-8 -*-
r"""captcha_solver 的离线核验（不碰真实界面、不移动鼠标）。

求解库的定位算法，以及本模块的几何换算 / 浮层解析，都能脱离 COM 与真实界面
验证：
  · locate_gap：合成「背景 + 缺口 + 左侧拼图块」，验证缺口定位，并覆盖
    「拼图块轮廓比缺口更实」时靠 piece_zone 排除后重找的兜底路径；
  · find_overlay / _pick_handle / _handle_center：喂入假 UIA 属性元组，验证
    拼图区、无名手柄、特征文本的解析与固定几何兜底；
  · solve：把截图 / 拖拽 / 浮层探测全换成假的，验证「成功即返回、失败给原因」
    的编排契约。真实拖拽会移动鼠标并按下按键，绝不在这里跑。

缺 numpy / opencv 或 comtypes 时整体跳过（精简解释器下不影响其余用例）。

运行：python -m unittest discover -s tests
"""

import sys
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

try:
    import captcha_solver as cs
    _HAVE = cs.available()
except Exception:      # noqa: BLE001 - 缺依赖时跳过整个模块
    cs = None
    _HAVE = False

_skip = unittest.skipUnless(_HAVE, "需要 numpy/opencv/comtypes")


class _R:
    """wt.RECT 的最小替身（left/top/right/bottom）。"""

    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


CFG = {"min_gap_score": 0.55, "piece_zone_pct": 15.0, "drag_scale": 1.0,
       "drag_bias": 0.0, "piece_x0": 0.0, "settle_seconds": 0.0}


def _noise(h, w, seed=7):
    import numpy as np
    return np.random.default_rng(seed).integers(150, 210, size=(h, w, 3),
                                                dtype=np.uint8)


@_skip
class LocateGapCase(unittest.TestCase):
    def test_finds_gap_and_ignores_left_piece(self):
        img = _noise(201, 301)
        img[90:134, 190:234] = 40        # 缺口
        img[90:134, 8:52] = 20           # 左侧拼图块
        gap, err = cs.locate_gap(img, CFG)
        self.assertEqual(err, "")
        self.assertIsNotNone(gap)
        self.assertLess(abs(gap["x"] - 190), 8)
        self.assertGreaterEqual(gap["score"], 0.55)

    def test_piece_zone_excludes_dominant_left_contour(self):
        # 左侧拼图块又大又实（fill≈1.0），缺口被掏空后 fill 更低 —— 若不做
        # piece_zone 排除，find_gap 会选中左侧拼图块。
        img = _noise(201, 301, seed=11)
        img[70:150, 6:86] = 10
        gx, gy = 210, 80
        img[gy:gy + 44, gx:gx + 44] = 10
        img[gy + 14:gy + 30, gx + 14:gx + 30] = 200
        gap, err = cs.locate_gap(img, CFG)
        self.assertEqual(err, "")
        self.assertGreater(gap["x"], 301 * 0.15)      # 没选中左侧拼图块
        self.assertLess(abs(gap["x"] - gx), 8)

    def test_weak_contour_is_rejected_by_score_gate(self):
        # 直角三角形 fill≈0.5：过得了 edge_only 的 0.4 轮廓筛，但低于 0.55 门槛
        img = _noise(201, 301, seed=5)
        for i in range(44):
            img[90 + i, 190:190 + i + 1] = 30
        gap, err = cs.locate_gap(img, CFG)
        self.assertIsNone(gap)
        self.assertIn("置信度不足", err)

    def test_no_gap_returns_reason_without_raising(self):
        img = _noise(201, 301, seed=3)                # 无缺口，纯噪声
        gap, err = cs.locate_gap(img, CFG)
        if gap is not None:
            self.assertGreaterEqual(gap["score"], 0.55)
        else:
            self.assertTrue(err)      # 契约：失败必须给出可读原因，不抛异常


@_skip
class OverlayCase(unittest.TestCase):
    ELS = [
        ("请完成安全验证", 50020, 0, _R(703, 316, 816, 337), True),
        ("刷新验证码", 50000, 0, _R(971, 350, 1004, 383), True),
        ("要获取缺失的图片说明，请打开上下文菜单。", 50006, 0,
         _R(703, 350, 1004, 551), True),
        ("拖动滑块完成拼图", 50020, 0, _R(793, 567, 914, 589), True),
        ("", 50020, 0, _R(715, 571, 732, 588), True),      # 无名手柄 17x17
        ("CertifyId: 43CTJ6FnKg", 50020, 0, _R(703, 603, 766, 611), True),
    ]

    def _with_elements(self, els):
        orig = cs._elements
        cs._elements = lambda name: list(els)
        self.addCleanup(lambda: setattr(cs, "_elements", orig))

    def test_parses_puzzle_handle_and_marker(self):
        self._with_elements(self.ELS)
        geo = cs.find_overlay("ZCode.exe")
        self.assertIsNotNone(geo)
        self.assertEqual((geo["puzzle"].left, geo["puzzle"].top), (703, 350))
        self.assertEqual((geo["puzzle"].right, geo["puzzle"].bottom),
                         (1004, 551))
        self.assertEqual(geo["handle"], (723.5, 579.5))     # 手柄元素中心
        self.assertEqual(geo["marker"].left, 703)

    def test_handle_center_falls_back_to_fixed_geometry(self):
        puzzle = _R(703, 350, 1004, 551)
        self.assertEqual(cs._handle_center(puzzle, None), (723.0, 579.0))

    def test_no_marker_means_no_overlay(self):
        self._with_elements([("无关元素", 50020, 0, _R(0, 0, 10, 10), True)])
        self.assertIsNone(cs.find_overlay("ZCode.exe"))
        self.assertFalse(cs.overlay_present("ZCode.exe"))

    def test_crop_clips_to_image_bounds(self):
        img = _noise(201, 301)
        self.assertEqual(cs._crop(img, _R(0, 0, 301, 201), 0, 0).shape,
                         (201, 301, 3))
        # 裁剪后不足 20px -> 视为几何异常，返回 None
        self.assertIsNone(cs._crop(img, _R(295, 195, 300, 200), 0, 0))


@_skip
class SolveOrchestrationCase(unittest.TestCase):
    """solve 的编排契约：成功/失败/无浮层三种出口。"""

    def _patch(self, name, value):
        orig = getattr(cs, name)
        setattr(cs, name, value)
        self.addCleanup(lambda: setattr(cs, name, orig))

    def _fake_env(self, overlay_present):
        img = _noise(201, 301)
        img[90:134, 190:234] = 40
        geo = {"puzzle": _R(0, 0, 301, 201), "handle": (20.0, 229.0),
               "marker": _R(0, 0, 10, 10)}
        self._patch("find_overlay", lambda name: dict(geo))
        self._patch("capture_screen", lambda: (img, 0, 0))
        self._patch("overlay_present", lambda name: overlay_present)
        calls = []
        self._patch("drag", lambda start, dist, cfg: calls.append((start, dist)))
        return calls

    def test_success_returns_true_and_drags(self):
        calls = self._fake_env(overlay_present=False)
        ok, detail = cs.solve("ZCode.exe", CFG, log=lambda *a, **k: None)
        self.assertTrue(ok, detail)
        self.assertEqual(len(calls), 1)
        self.assertLess(abs(calls[0][1] - 190), 8)      # 拖动距离≈缺口 x

    def test_overlay_remaining_returns_false(self):
        self._fake_env(overlay_present=True)
        ok, detail = cs.solve("ZCode.exe", CFG, log=lambda *a, **k: None)
        self.assertFalse(ok)
        self.assertIn("浮层仍在", detail)

    def test_no_overlay_counts_as_success(self):
        self._patch("find_overlay", lambda name: None)
        ok, detail = cs.solve("ZCode.exe", CFG, log=lambda *a, **k: None)
        self.assertTrue(ok)
        self.assertIn("未检测到验证码浮层", detail)


if __name__ == "__main__":
    unittest.main()
