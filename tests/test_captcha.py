# -*- coding: utf-8 -*-
r"""滑块验证码自动处理（路线 B）的回归测试。

覆盖三层：
  1. config 的 captcha 块归一化：缺字段补默认、坏值夹回区间、route 走白名单。
  2. 两个开关的"与"关系：总开关（captcha.enabled）× 应用开关
     （per_app_click.<key>.captcha）。
  3. 领取流程里的验证码阶段：关闭时行为与不带本功能完全一致；打开时点开入口
     后进入验证码阶段，成功即收工，失败按预算重试，超限转人工，绝不无限重试。

真实验证码求解（截图/缺口定位/拖拽）依赖 COM 与真实界面，这里把
captcha_solver.solve 换成脚本化的假实现 —— 状态机本身可以脱离界面单独验证。

运行：python -m unittest discover -s tests
"""

import json
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

ZCODE = {"key": "zcode", "label": "ZCode", "exe": r"C:\x\ZCode.exe"}
PLAIN = {"key": "trae", "label": "Trae CN", "exe": r"C:\x\Trae.exe"}
NEVER_EXPIRE = 9e9


class _StubCfg:
    def __init__(self, **over):
        self.data = {
            "click_enabled": True,
            "click_keywords": list(tc.CLICK_KEYWORDS_DEFAULT),
            "per_app_click": {},
            "click_wait_window": 25,
            "click_retry_seconds": 15,
            "click_max_attempts": 10,
            "keep_minutes": 10,
            "captcha": dict(tc.CAPTCHA_DEFAULTS),
        }
        self.data.update(over)

    def save(self):
        pass


class _FakeUia:
    """脚本化的假 find_and_click：只跑正式点击那条队列。"""

    def __init__(self, script):
        self.script = list(script)

    def __call__(self, image, keywords, negative=(), clicked_names=None,
                 point_pct=None, region_pct=None, probe=False):
        if probe:
            return "not-found", "目标不可见"
        if not self.script:
            raise AssertionError("假实现被多调用了一次，脚本已用尽")
        return self.script.pop(0)


class _EngineCase(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.cfg = _StubCfg()
        self.eng = tc.ClaimEngine(
            self.cfg, log=lambda m, tag="info": self.logs.append(m))

    def _install_uia(self, script):
        fake = _FakeUia(script)
        orig = tc.uia_click.find_and_click
        tc.uia_click.find_and_click = fake
        self.addCleanup(lambda: setattr(tc.uia_click, "find_and_click", orig))
        return fake

    def _install_solve(self, fn):
        orig = tc.captcha_solver.solve
        tc.captcha_solver.solve = fn
        self.addCleanup(lambda: setattr(tc.captcha_solver, "solve", orig))

    def _job(self, app=ZCODE, **over):
        job = self.eng.make_click_job(app, will_launch=False, start_ts=1000.0)
        job["deadline"] = NEVER_EXPIRE
        job.update(over)
        return job

    def _log_text(self):
        return "\n".join(self.logs)


# ---------------------------------------------------- config 归一化 ------
class ConfigNormalizeCase(unittest.TestCase):
    def _load(self, raw: dict) -> dict:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
            orig = tc.config_dir
            tc.config_dir = lambda: Path(d)
            self.addCleanup(lambda: setattr(tc, "config_dir", orig))
            return tc.AppConfig().data["captcha"]

    def test_missing_block_falls_back_to_defaults(self):
        cap = self._load({"apps": []})
        self.assertEqual(cap, dict(tc.CAPTCHA_DEFAULTS))

    def test_default_is_disabled(self):
        # 兼容性硬约束：代码默认关闭，老配置升级后行为不变
        self.assertFalse(tc.CAPTCHA_DEFAULTS["enabled"])

    def test_bad_values_are_clamped(self):
        cap = self._load({"captcha": {
            "enabled": "yes", "route": "CDP", "max_attempts": "abc",
            "retry_seconds": 1, "drag_scale": 99, "piece_x0": -3,
        }})
        self.assertTrue(cap["enabled"])
        self.assertEqual(cap["route"], "cdp")
        self.assertEqual(cap["max_attempts"], 5)      # 坏值回默认
        self.assertEqual(cap["retry_seconds"], 2)     # 1 夹到下限 2
        self.assertEqual(cap["drag_scale"], 2.0)      # 99 夹到上限 2.0
        self.assertEqual(cap["piece_x0"], 0.0)        # 负数夹到 0

    def test_unknown_route_falls_back_to_screen(self):
        cap = self._load({"captcha": {"route": "magic"}})
        self.assertEqual(cap["route"], "screen")


# ---------------------------------------------------- 开关组合 ------
class SwitchCase(_EngineCase):
    def test_captcha_for_requires_both_switches(self):
        # 默认：应用侧标了 captcha，但总开关默认关 → 不启用
        self.assertFalse(self.eng.captcha_for(ZCODE))
        self.cfg.data["captcha"]["enabled"] = True
        self.assertTrue(self.eng.captcha_for(ZCODE))
        # 未标注 captcha 的应用即使总开关打开也不启用
        self.assertFalse(self.eng.captcha_for(PLAIN))

    def test_job_carries_captcha_flags(self):
        self.cfg.data["captcha"]["enabled"] = True
        job = self._job()
        self.assertTrue(job["captcha"])
        self.assertFalse(job["captcha_pending"])
        self.assertEqual(job["captcha_tries"], 0)
        self.assertEqual(job["captcha_wait"],
                         tc.CAPTCHA_DEFAULTS["wait_seconds"])

    def test_job_disabled_when_switch_off(self):
        job = self._job()
        self.assertFalse(job["captcha"])


# ---------------------------------------------------- 领取流程 ------
class FlowCase(_EngineCase):
    def test_disabled_keeps_old_behaviour(self):
        """captcha 关：点开入口即收工并给人工提示，与不带本功能时一致。"""
        self._install_uia([("clicked", "打开")])
        job = self._job()
        self.eng.process_click_job(job, 1003.0)
        self.assertTrue(job["done"])
        self.assertFalse(job["captcha_pending"])
        self.assertIn("已自动点击", self._log_text())
        self.assertIn("手动拖滑块", self._log_text())   # ZCode 预置 hint

    def test_enabled_enters_captcha_phase(self):
        self.cfg.data["captcha"]["enabled"] = True
        self._install_uia([("clicked", "打开")])
        job = self._job()
        self.eng.process_click_job(job, 1003.0)
        self.assertFalse(job["done"])            # 未收工，等验证码
        self.assertTrue(job["captcha_pending"])
        self.assertEqual(job["next"], 1003.0 + job["captcha_wait"])
        self.assertIn("等待安全验证", self._log_text())

    def test_solve_success_finishes_job(self):
        self.cfg.data["captcha"]["enabled"] = True
        self._install_uia([("clicked", "打开")])
        self._install_solve(lambda image, cfg, log=print: (True, "score=0.9"))
        job = self._job()
        self.eng.process_click_job(job, 1003.0)
        self.eng.process_click_job(job, job["next"])   # 验证码阶段
        self.assertTrue(job["done"])
        self.assertIn("安全验证已通过", self._log_text())

    def test_failure_retries_then_falls_back(self):
        self.cfg.data["captcha"].update({"enabled": True, "max_attempts": 2,
                                         "retry_seconds": 3})
        self._install_uia([("clicked", "打开")])
        calls = []

        def _fail(image, cfg, log=print):
            calls.append(1)
            return False, "浮层仍在"

        self._install_solve(_fail)
        job = self._job()
        self.eng.process_click_job(job, 1003.0)
        t = job["next"]
        self.eng.process_click_job(job, t)          # 第 1 次失败 → 排下一次
        self.assertFalse(job["done"])
        self.assertEqual(job["captcha_tries"], 1)
        self.assertEqual(job["next"], t + 3)
        self.eng.process_click_job(job, job["next"])  # 第 2 次失败 → 转人工
        self.assertTrue(job["done"])
        self.assertEqual(len(calls), 2)             # 不超过预算
        self.assertIn("转人工", self._log_text())

    def test_missing_deps_falls_back_without_attempts(self):
        self.cfg.data["captcha"]["enabled"] = True
        self._install_uia([("clicked", "打开")])
        orig = tc.captcha_solver.available
        tc.captcha_solver.available = lambda: False
        self.addCleanup(lambda: setattr(tc.captcha_solver, "available", orig))
        job = self._job()
        self.eng.process_click_job(job, 1003.0)
        self.eng.process_click_job(job, job["next"])
        self.assertTrue(job["done"])
        self.assertEqual(job["captcha_tries"], 0)
        self.assertIn("缺少 numpy/opencv", self._log_text())

    def test_already_claimed_skips_captcha(self):
        """今日已领：最后一步撞上完成标识，直接收工，不进验证码阶段。"""
        self.cfg.data["captcha"]["enabled"] = True
        self._install_uia([("already", "已领取")])
        job = self._job()
        self.eng.process_click_job(job, 1003.0)
        self.assertTrue(job["done"])
        self.assertFalse(job["captcha_pending"])


if __name__ == "__main__":
    unittest.main()
