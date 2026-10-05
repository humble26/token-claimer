# -*- coding: utf-8 -*-
"""批量求解运行器（自带浏览器）与命令行入口。

这是"自带浏览器跑批"的薄壳；把求解器嵌入宿主程序时不需要本模块——
直接用 slider_captcha.solve_on_page / solve_on_page_sync 即可（见 README 集成指南）。
"""
import argparse
import asyncio
import logging
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .page_driver import (AsyncPageDriver, Selectors, SolveResult,
                          solve_with_driver)
from .trajectory import Tuning

log = logging.getLogger('slider_captcha.runner')


def demo_url() -> str:
    """内置演示验证码页面的 file:// 地址（随包分发）。"""
    return (Path(__file__).resolve().parent / 'demo' / 'index.html').as_uri()


@dataclass
class BatchStats:
    results: List[SolveResult] = field(default_factory=list)
    elapsed: float = 0.0


async def _worker(context, url, sel, tuning, calibrate, trial_ids, stats, wid):
    if not trial_ids:
        return
    page = await context.new_page()
    drv = AsyncPageDriver(page)
    await page.goto(url)
    await page.wait_for_function(f'(({sel.ready_js}) | 0) >= 1')

    bias = 0.0
    for tid in trial_ids:
        # 事件驱动换题：等题目版本号自增，代替盲等
        seq = int(await drv.eval_js(f'({sel.ready_js}) | 0'))
        await page.evaluate(sel.new_challenge_js)
        await drv.wait_seq_beyond(seq, sel.ready_js)

        r = await solve_with_driver(drv, sel, tuning, bias)
        r.trial = tid          # type: ignore[attr-defined]
        stats.results.append(r)
        g = r.gap
        tag = '成功' if r.ok else f'失败 (偏差 {r.err}px)'
        log.info('[%d] x=%.1f y=%.1f (score=%.2f, %s) 拖动 %.1fpx -> %s',
                 tid, g.x, g.y, g.score, g.method, r.distance, tag)

        if not r.ok:
            if calibrate and r.err is not None:
                bias = 0.7 * bias + 0.3 * r.err   # EMA 估计系统性偏差
            await drv.sleep(900)   # 等页面自动换题，避免其定时器与下次拖拽竞争
    await page.close()


async def run_batch(url, trials=5, workers=4, selectors=None, tuning=None,
                    headless=True, calibrate=True) -> BatchStats:
    """启动临时浏览器并发求解 trials 次（用于验证与压测）。"""
    from playwright.async_api import async_playwright

    sel = selectors or Selectors()
    stats = BatchStats()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        ids = list(range(1, trials + 1))
        chunks = [ids[i::workers] for i in range(min(workers, trials))]
        t0 = time.perf_counter()
        await asyncio.gather(
            *[_worker(context, url, sel, tuning, calibrate, c, stats, i)
              for i, c in enumerate(chunks)])
        stats.elapsed = time.perf_counter() - t0
        await browser.close()
    return stats


def print_summary(stats: BatchStats) -> int:
    results = stats.results
    ok_n = sum(r.ok for r in results)
    scores = [r.gap.score for r in results]
    methods = {}
    for r in results:
        methods[r.gap.method] = methods.get(r.gap.method, 0) + 1
    print(f'\n成功率: {ok_n}/{len(results)}  |  墙钟 {stats.elapsed:.1f}s  '
          f'吞吐 {len(results) / stats.elapsed * 60:.0f} 例/分  '
          f'单例平均 {statistics.mean(r.elapsed for r in results):.2f}s')
    print(f'定位分数: min={min(scores):.3f}  mean={statistics.mean(scores):.3f}')
    print(f'方法分布: {methods}')
    return ok_n


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    ap = argparse.ArgumentParser(description='滑块验证码求解器（async 并发版）')
    ap.add_argument('--url', default=None, help='验证码页面地址，默认使用内置 demo 页面')
    ap.add_argument('--bg', default='#bgCanvas', help='背景图画布/图片选择器')
    ap.add_argument('--piece', default='#pieceCanvas', help='拼图块画布/图片选择器')
    ap.add_argument('--slider', default='#btn', help='滑块按钮选择器')
    ap.add_argument('--trials', type=int, default=5, help='总尝试次数')
    ap.add_argument('--workers', type=int, default=4, help='并发页面数')
    ap.add_argument('--headed', action='store_true', help='有头模式运行，便于观察')
    ap.add_argument('--no-calibrate', action='store_true', help='关闭失败偏差校准')
    args = ap.parse_args()

    url = args.url or demo_url()
    sel = Selectors(bg=args.bg, piece=args.piece, slider=args.slider)

    stats = asyncio.run(run_batch(url, trials=args.trials, workers=args.workers,
                                  selectors=sel, headless=not args.headed,
                                  calibrate=not args.no_calibrate))
    ok_n = print_summary(stats)
    sys.exit(0 if ok_n == len(stats.results) else 1)


if __name__ == '__main__':
    main()
