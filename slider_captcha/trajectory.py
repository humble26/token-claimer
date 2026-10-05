# -*- coding: utf-8 -*-
"""轨迹与交互编排（与传输层无关，同步/异步驱动共用）。"""
import random
from dataclasses import dataclass
from typing import List, Tuple


def human_track(distance):
    """生成拟人拖拽轨迹。

    返回 [(dx, dy, dt_ms), ...]：每次 mouse.move 的横向/纵向增量与间隔毫秒，
    所有 dx 之和恰好等于 distance。真实用户不是匀速直线：起手加速、接近目标
    减速、偶尔停顿、常常轻微过冲后再回调，风控会检测这些特征。
    """
    if distance <= 0:
        return []

    steps = max(12, int(distance / random.uniform(6.0, 9.0)))
    overshoot = random.uniform(2.0, 6.0) if distance > 50 else random.uniform(0.5, 2.0)
    total = distance + overshoot

    track = []
    x = 0.0
    for i in range(1, steps + 1):
        p = i / steps
        eased = p * p * (3.0 - 2.0 * p)          # smoothstep：先加速后减速
        nx = total * eased
        dx = nx - x
        x = nx
        dy = random.choice([0, 0, 0, 0, 1, -1])
        dt = random.uniform(5.0, 13.0)
        if random.random() < 0.05:               # 偶尔中途犹豫
            dt += random.uniform(30.0, 90.0)
        track.append((dx, dy, dt))

    # 过冲后的回调：分 2~4 小步修到精确位置
    remain = distance - x
    n = random.randint(2, 4)
    for k in range(n):
        if k == n - 1:
            step = remain
        else:
            step = remain * random.uniform(0.3, 0.6)
            remain -= step
        track.append((step, random.choice([0, 0, 0, 1, -1]), random.uniform(30.0, 70.0)))
    return track


@dataclass
class Tuning:
    """拖拽行为参数（毫秒）。"""

    reaction_ms: Tuple[int, int] = (120, 320)   # 按下后的反应时间区间
    settle_ms: Tuple[int, int] = (80, 200)      # 松手前的停顿区间
    after_ms: int = 120                          # 松手后等结果的时间


def build_choreography(distance, tuning=None, rng=None) -> List[tuple]:
    """把拖拽编排成与传输层无关的动作序列。

    返回 op 列表：('down',) / ('move', x, y) / ('sleep', ms) / ('up',)，
    其中 move 的 x/y 是相对滑块中心点的累计偏移。
    同步与异步驱动各自解释这份序列，编排逻辑只写一遍。
    """
    rng = rng or random
    t = tuning or Tuning()
    if distance <= 0:
        return [('down',), ('sleep', rng.randint(*t.reaction_ms)), ('up',),
                ('sleep', t.after_ms)]
    ops = [('down',), ('sleep', rng.randint(*t.reaction_ms))]
    x = y = 0.0
    for dx, dy, dt in human_track(distance):
        x += dx
        y += dy
        ops.append(('move', x, y))
        ops.append(('sleep', int(dt)))
    ops.append(('sleep', rng.randint(*t.settle_ms)))
    ops.append(('up',))
    ops.append(('sleep', t.after_ms))
    return ops
