# -*- coding: utf-8 -*-
r"""按应用「领取日」的回归测试。

背景：不同客户端的领取节奏不一样 —— 用户的实际配置是 ZCode 只在周末领，
WorkBuddy / TraeWork CN 每天领。但定时计划（每天 08:00/12:00/20:00）是全局
共享的，所以频率必须落到"每个应用自己决定今天该不该领"这一层。

三个容易踩的点，各自一条用例：
  1. 判定本身：weekday() 的 0=周一，周末是 5/6，别写反。
  2. 接线：过滤必须发生在 plan_claim 里，定时和手动两条路径才都覆盖得到
     （只在 poll_due 里过滤的话，「立即领取」会绕过领取日）。
  3. 持久化：AppConfig._load 会按 key 重建应用列表，重建时若不带上 days，
     用户在下拉框里选的「仅周末」重启后就被悄悄改回「每天」。

运行：python -m unittest discover -s tests
"""

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
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

MON = datetime(2026, 9, 28, 12, 0)   # 周一
SAT = datetime(2026, 10, 3, 12, 0)   # 周六
SUN = datetime(2026, 10, 4, 12, 0)   # 周日

ZCODE = {"key": "zcode", "label": "ZCode", "days": "weekend"}
WORKBUDDY = {"key": "workbuddy", "label": "WorkBuddy", "days": "daily"}
WEEKDAY_ONLY = {"key": "x", "label": "X", "days": "weekday"}


class DayMatchesTest(unittest.TestCase):
    """day_matches：单个应用今天该不该领。"""

    def test_daily_matches_every_day(self):
        for now in (MON, SAT, SUN):
            with self.subTest(day=now.weekday()):
                self.assertTrue(tc.day_matches(WORKBUDDY, now))

    def test_weekend_only_matches_sat_and_sun(self):
        self.assertTrue(tc.day_matches(ZCODE, SAT))
        self.assertTrue(tc.day_matches(ZCODE, SUN))
        self.assertFalse(tc.day_matches(ZCODE, MON))

    def test_weekday_only_matches_mon_to_fri(self):
        self.assertTrue(tc.day_matches(WEEKDAY_ONLY, MON))
        self.assertFalse(tc.day_matches(WEEKDAY_ONLY, SAT))

    def test_missing_days_field_falls_back_to_daily(self):
        """老 config.json 里没有 days 字段，必须按「每天」处理而不是报错。"""
        self.assertTrue(tc.day_matches({"key": "zcode"}, SAT))

    def test_unknown_days_value_falls_back_to_daily(self):
        self.assertTrue(tc.day_matches({"key": "zcode", "days": "每月"}, SAT))


class EligibleAppsTest(unittest.TestCase):
    """eligible_apps：过滤 + 把跳过原因写进日志。"""

    def setUp(self):
        logs = []
        self.eng = tc.ClaimEngine(_StubCfg(),
                                  log=lambda m, tag="info": logs.append(m))
        self.logs = logs

    def test_weekend_run_keeps_daily_and_weekend_apps(self):
        kept = self.eng.eligible_apps([ZCODE, WORKBUDDY], SAT)
        self.assertEqual([a["key"] for a in kept], ["zcode", "workbuddy"])

    def test_monday_run_drops_weekend_app(self):
        kept = self.eng.eligible_apps([ZCODE, WORKBUDDY], MON)
        self.assertEqual([a["key"] for a in kept], ["workbuddy"])

    def test_skipped_app_is_reported(self):
        self.eng.eligible_apps([ZCODE, WORKBUDDY], MON)
        self.assertTrue(any("ZCode" in m and "跳过" in m for m in self.logs),
                        self.logs)

    def test_no_skip_means_no_noise(self):
        self.eng.eligible_apps([WORKBUDDY], MON)
        self.assertEqual(self.logs, [])


class _StubCfg:
    def __init__(self, **overrides):
        self.data = {"click_enabled": False, "per_app_click": {},
                     "apps": [], "skip_if_running": False}
        self.data.update(overrides)

    def save(self):
        pass


class PlanClaimDayFilterTest(unittest.TestCase):
    """接线：领取日过滤必须作用于 plan_claim 的每一条入口。"""

    def _engine(self, apps):
        # 用当前解释器当"客户端程序"，保证 Path(exe).exists() 成立；
        # skip_if_running=False 让 plan 不依赖真实进程状态。
        exe = sys.executable
        return tc.ClaimEngine(_StubCfg(
            apps=[dict(a, exe=exe, enabled=True) for a in apps],
            skip_if_running=False))

    def test_manual_path_filters_by_day(self):
        """「立即领取」也不能绕过领取日，否则周末配置形同虚设。"""
        eng = self._engine([ZCODE, WORKBUDDY])
        plan = eng.plan_claim(now=MON)
        self.assertEqual([p["app"]["key"] for p in plan], ["workbuddy"])

    def test_scheduled_path_filters_by_day(self):
        eng = self._engine([ZCODE, WORKBUDDY])
        due = [dict(ZCODE, exe=sys.executable), dict(WORKBUDDY, exe=sys.executable)]
        plan = eng.plan_claim(due, now=MON)
        self.assertEqual([p["app"]["key"] for p in plan], ["workbuddy"])

    def test_weekend_plan_includes_zcode(self):
        eng = self._engine([ZCODE, WORKBUDDY])
        plan = eng.plan_claim(now=SAT)
        self.assertEqual([p["app"]["key"] for p in plan], ["zcode", "workbuddy"])


class NextRunTextTest(unittest.TestCase):
    r"""状态栏「下次执行」文案必须容错。

    本函数由 status_tick 每秒调用一次；一旦抛异常，after 链条断裂，状态栏和
    「下次执行」从此不再刷新，而界面看不出任何异样。原缺陷用 t[:2]/t[-2:] 切片
    解析时间，而 TIME_RE 允许 "8:05" 这种单位数小时，切片得到 int("8:") 直接
    ValueError。
    """

    def _eng(self, **over):
        data = {"mode": "daily", "daily_times": ["08:00"], "fired_log": {},
                "interval_minutes": 720}
        data.update(over)
        return tc.ClaimEngine(_StubCfg(**data))

    def test_single_digit_hour_does_not_crash(self):
        txt = self._eng(daily_times=["8:05"]).next_run_text(
            now=datetime(2026, 10, 3, 12, 0))
        self.assertIn("08:05", txt)

    def test_upcoming_time_today_is_reported(self):
        txt = self._eng(daily_times=["20:00"]).next_run_text(
            now=datetime(2026, 10, 3, 12, 0))
        self.assertIn("10-03 20:00", txt)

    def test_all_times_passed_rolls_to_tomorrow(self):
        txt = self._eng(daily_times=["08:00"]).next_run_text(
            now=datetime(2026, 10, 3, 12, 0))
        self.assertIn("10-04 08:00", txt)

    def test_no_times_reports_unset(self):
        self.assertEqual(self._eng(daily_times=[]).next_run_text(), "未设置时间")

    def test_broken_interval_timestamp_does_not_crash(self):
        txt = self._eng(mode="interval",
                        fired_log={"interval-last": "坏值"}).next_run_text()
        self.assertIn("就绪", txt)


class ConfigPersistenceTest(unittest.TestCase):
    """AppConfig：领取日必须能存下来、读回来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = tc.config_dir
        tc.config_dir = lambda: Path(self.tmp.name)

    def tearDown(self):
        tc.config_dir = self._orig_dir
        self.tmp.cleanup()

    def _write(self, payload: dict):
        (Path(self.tmp.name) / "config.json").write_text(
            json.dumps(payload, ensure_ascii=False), "utf-8")

    def test_saved_days_survive_reload(self):
        """_load 按 key 重建应用列表时若漏掉 days，用户的选择会被改回每天。"""
        self._write({"apps": [{"key": "zcode", "days": "weekend"}]})
        cfg = tc.AppConfig()
        zcode = next(a for a in cfg.data["apps"] if a["key"] == "zcode")
        self.assertEqual(zcode["days"], "weekend")

    def test_other_apps_default_to_daily_when_absent(self):
        self._write({"apps": [{"key": "zcode", "days": "weekend"}]})
        cfg = tc.AppConfig()
        workbuddy = next(a for a in cfg.data["apps"] if a["key"] == "workbuddy")
        self.assertEqual(workbuddy["days"], "daily")

    def test_fresh_config_defaults_all_apps_to_daily(self):
        cfg = tc.AppConfig()
        self.assertTrue(all(a["days"] == "daily" for a in cfg.data["apps"]))

    def test_round_trip_through_save(self):
        cfg = tc.AppConfig()
        for a in cfg.data["apps"]:
            if a["key"] == "zcode":
                a["days"] = "weekend"
        cfg.save()
        again = tc.AppConfig()
        zcode = next(a for a in again.data["apps"] if a["key"] == "zcode")
        self.assertEqual(zcode["days"], "weekend")

    def test_broken_structural_fields_fall_back(self):
        """config.json 被手改坏（null / 字符串）也不能让程序起不来。"""
        self._write({"fired_log": None, "daily_times": None,
                     "per_app_click": "oops", "apps": "oops"})
        cfg = tc.AppConfig()
        self.assertEqual(cfg.data["fired_log"], {})
        self.assertEqual(cfg.data["daily_times"], ["08:00", "12:00", "20:00"])
        self.assertEqual(cfg.data["per_app_click"], {})
        self.assertEqual([a["key"] for a in cfg.data["apps"]],
                         [d["key"] for d in tc.DEFAULT_APPS])

    def test_broken_numeric_fields_fall_back(self):
        """一个 "abc" 就能让 poll_due 每轮抛异常、调度循环整体停摆。"""
        self._write({"interval_minutes": "abc", "keep_minutes": None,
                     "click_max_attempts": "很多"})
        cfg = tc.AppConfig()
        self.assertEqual(cfg.data["interval_minutes"], 720)
        self.assertEqual(cfg.data["keep_minutes"], 10)
        self.assertEqual(cfg.data["click_max_attempts"], 10)

    def test_negative_numeric_field_is_clamped(self):
        self._write({"interval_minutes": -5})
        self.assertEqual(tc.AppConfig().data["interval_minutes"], 1)


class LabelMapTest(unittest.TestCase):
    """界面下拉框靠 label 反查 key，映射表不能错。"""

    def test_labels_cover_every_preset(self):
        self.assertEqual(set(tc.DAY_LABELS), set(tc.DAY_PRESETS))

    def test_reverse_lookup_round_trips(self):
        for key in tc.DAY_PRESETS:
            self.assertEqual(tc.DAY_BY_LABEL[tc.DAY_LABELS[key]], key)

    def test_weekend_preset_is_sat_sun(self):
        self.assertEqual(tc.DAY_PRESETS["weekend"], {5, 6})


if __name__ == "__main__":
    unittest.main()
