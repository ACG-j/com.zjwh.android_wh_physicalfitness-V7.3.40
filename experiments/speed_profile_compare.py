#!/usr/bin/env python3
"""离线对比：旧配速算法 vs 新的 project 实现。

纯本地绘图（numpy + Pillow），不联网、不生成记录。
"拟议"一侧直接调用 whsport.running_protocol.generate_synthetic_gps_track，
所以图里就是真实要提交的曲线。
运行：python experiments/speed_profile_compare.py
"""
import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from whsport.running_protocol import generate_synthetic_gps_track

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speed_compare.png")
FONT = "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"

LAT, LON = 43.9042, 81.3074
DISTANCE_M, DURATION_S, TOTAL_STEPS = 2000.0, 830.0, 1100
N = int(DURATION_S) // 5
BASE_SPEED = DISTANCE_M / DURATION_S
DT = DURATION_S / (N - 1)
T = np.linspace(0.0, 1.0, N)


# ── 旧算法（照抄改动前的 _speed_profile / cadence）──────────────────
def old_speed(rng, fitness=0.5):
    speeds = []
    for i in range(N):
        t = i / (N - 1)
        warm = 0.92 + 0.08 * min(1.0, t / 0.03)
        fade = (1.0 - (0.025 * (1.0 - fitness)) * ((t - 0.6) / 0.4)) if t > 0.6 else 1.0
        wave = (1.0 + 0.020 * np.sin(2 * np.pi * t * 7.0)
                + 0.013 * np.sin(2 * np.pi * t * 23.0 + 1.1)
                + 0.006 * np.sin(2 * np.pi * t * 61.0))
        terrain = 1.0 + 0.014 * np.sin(2 * np.pi * t * 1.8 + 0.4)
        v = BASE_SPEED * warm * fade * wave * terrain * (1.0 + rng.normal(0, 0.016))
        if rng.random() < 1.0 / 130.0:
            v *= rng.uniform(0.93, 0.96)
        speeds.append(max(0.6, v))
    w = max(2, N // 120)
    sm = np.array([np.mean(speeds[max(0, i - w):min(N, i + w + 1)]) for i in range(N)])
    sm *= BASE_SPEED / sm.mean()
    return sm


def old_cadence(rng, speeds):
    drift, prof = 0.0, np.zeros(N)
    for i in range(N):
        drift = drift * 0.97 + rng.normal(0, 0.010)
        prof[i] = ((max(0.35, speeds[i]) / BASE_SPEED) ** 0.16) * (1 + drift + rng.normal(0, 0.025))
    integ = np.sum(0.5 * (prof[:-1] + prof[1:]) / 60.0 * DT)
    return prof * (TOTAL_STEPS / integ)


# ── 新算法：直接用项目实现 ──────────────────────────────────────────
def new_from_project(seed):
    pts, _ = generate_synthetic_gps_track(
        LAT, LON, int(DISTANCE_M), int(DURATION_S), 0,
        total_steps=TOTAL_STEPS, rng=random.Random(seed),
    )
    v = np.array([p.speed for p in pts])
    c = np.array([p.cadence for p in pts])
    ele = np.array([p.ele for p in pts])
    cum = np.array([p.cum_dist for p in pts])
    return v, c, ele, cum


# ── 指标 ────────────────────────────────────────────────────────────
def pace(v):
    return 1000.0 / (v * 60.0)


def fmt(s):
    return "%d'%02d\"" % (int(s // 60), int(round(s % 60)))


def km_splits(v):
    dist = np.concatenate([[0.0], np.cumsum(0.5 * (v[:-1] + v[1:]) * DT)])
    dist *= DISTANCE_M / dist[-1]
    tm = np.arange(N) * DT
    out = []
    for a, b in ((0.0, 1000.0), (1000.0, DISTANCE_M)):
        out.append(np.interp(b, dist, tm) - np.interp(a, dist, tm))
    return out


def windowed_cadence(c):
    win, ts, cs = 10.0, [], []
    for k in range(int(DURATION_S // win)):
        lo, hi = k * win, (k + 1) * win
        steps = 0.0
        for i in range(N - 1):
            ov = min((i + 1) * DT, hi) - max(i * DT, lo)
            if ov > 0:
                steps += 0.5 * (c[i] + c[i + 1]) / 60.0 * ov
        ts.append((lo + hi) / 2)
        cs.append(round(steps) * 60.0 / win)
    return np.array(ts), np.array(cs)


# ── 绘图 ────────────────────────────────────────────────────────────
def font(sz):
    return ImageFont.truetype(FONT, sz)


CUR, NEW = (150, 150, 150), (26, 158, 84)
BG, GRID, AXIS, TEXT = (255, 255, 255), (232, 232, 232), (120, 120, 120), (40, 40, 40)


def panel(d, box, title, series, xlabel, ylabel, invert=False):
    x0, y0, x1, y1 = box
    d.rectangle(box, outline=AXIS, width=1)
    ally = np.concatenate([s[2] for s in series])
    allx = np.concatenate([s[1] for s in series])
    xmin, xmax = float(allx.min()), float(allx.max())
    ymin, ymax = float(ally.min()), float(ally.max())
    pad = (ymax - ymin) * 0.15 or 1.0
    ymin, ymax = ymin - pad, ymax + pad

    def px(x):
        return x0 + (x - xmin) / (xmax - xmin) * (x1 - x0)

    def py(y):
        r = (y - ymin) / (ymax - ymin)
        return y1 - (1 - r if invert else r) * (y1 - y0)

    for k in range(5):
        yy = ymin + (ymax - ymin) * k / 4
        d.line([(x0, py(yy)), (x1, py(yy))], fill=GRID)
        d.text((x0 - 6, py(yy)), "%.0f" % yy, font=font(15), fill=TEXT, anchor="rm")
    for k in range(5):
        xx = xmin + (xmax - xmin) * k / 4
        d.line([(px(xx), y0), (px(xx), y1)], fill=GRID)
        d.text((px(xx), y1 + 4), "%.0f" % xx, font=font(15), fill=TEXT, anchor="ma")
    for _, xs, ys, color in series:
        d.line([(px(x), py(y)) for x, y in zip(xs, ys)], fill=color, width=2)
    d.text(((x0 + x1) / 2, y0 - 24), title, font=font(19), fill=TEXT, anchor="mm")
    d.text((x0 - 52, (y0 + y1) / 2), ylabel, font=font(15), fill=TEXT, anchor="mm")
    d.text(((x0 + x1) / 2, y1 + 26), xlabel, font=font(15), fill=TEXT, anchor="mm")


def main():
    oc = np.random.default_rng(20261008)
    v_old = old_speed(oc)
    c_old = old_cadence(oc, v_old)
    v_new, c_new, ele, cum = new_from_project(7)
    assert len(v_new) == N, (len(v_new), N)

    tm = np.arange(N) * DT
    c_old_w, c_new_w = windowed_cadence(c_old), windowed_cadence(c_new)

    W, H = 1480, 1560
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 30), "配速算法对比  (2.00km · 13:50 · 平均6'55\" · 1100步)", font=font(24), fill=TEXT, anchor="lm")
    for i, (lb, col) in enumerate([("旧算法", CUR), ("新算法(含地形耦合)", NEW)]):
        yy = 30 + i * 24
        d.line([(1030, yy), (1056, yy)], fill=col, width=3)
        d.text((1064, yy), lb, font=font(16), fill=TEXT, anchor="lm")
    d.text((40, 78), "旧：疲劳≈1% + 噪声被平滑 → 平线", font=font(17), fill=(180, 60, 60), anchor="lm")
    d.text((40, 104), "新：疲劳递减 + 随机波动 + AR(1)抖动 + 上坡变慢", font=font(17), fill=(26, 120, 70), anchor="lm")

    panel(d, (110, 170, 1440, 470), "配速 (分/公里)   越快越靠上",
          [("旧", tm, pace(v_old), CUR), ("新", tm, pace(v_new), NEW)], "时间 (秒)", "分/公里", invert=True)
    panel(d, (110, 560, 760, 850), "步频 (步/分钟, 10秒窗取整)",
          [("旧", c_old_w[0], c_old_w[1], CUR), ("新", c_new_w[0], c_new_w[1], NEW)], "时间 (秒)", "步/分")
    panel(d, (820, 560, 1440, 850), "步幅 (厘米)",
          [("旧", tm, v_old / (c_old / 60) * 100, CUR), ("新", tm, v_new / (c_new / 60) * 100, NEW)],
          "时间 (秒)", "厘米")
    panel(d, (110, 950, 760, 1240), "每公里分段用时 (秒)",
          [("旧", np.array([500, 1500]), np.array(km_splits(v_old)), CUR),
           ("新", np.array([500, 1500]), np.array(km_splits(v_new)), NEW)], "分段 (米)", "秒")
    panel(d, (820, 950, 1440, 1240), "海拔 (米) 与 速度随坡度变化",
          [("海拔", tm, ele, (210, 140, 40))], "时间 (秒)", "米")

    grade = np.gradient(ele, cum)
    corr = np.corrcoef(grade, v_new)[0, 1]

    def stat(name, v, cw):
        pc = pace(v)
        k = km_splits(v)
        return (f"{name}  配速 最快{fmt(pc.min()*60)}/最慢{fmt(pc.max()*60)}/平均{fmt(pc.mean()*60)}  "
                f"波动{(pc.max()-pc.min())/pc.mean()*100:.0f}%  km1={k[0]:.0f}s km2={k[1]:.0f}s  "
                f"步频(10s){cw[1].min():.0f}~{cw[1].max():.0f}")

    d.text((40, 1400), stat("旧 ", v_old, c_old_w), font=font(17), fill=CUR, anchor="lm")
    d.text((40, 1432), stat("新 ", v_new, c_new_w), font=font(17), fill=NEW, anchor="lm")
    d.text((40, 1466), f"新算法 corr(坡度, 速度) = {corr:+.2f}   (负值越大 = 上坡越慢)",
           font=font(17), fill=TEXT, anchor="lm")

    img.save(OUT)
    print("saved:", OUT)
    print(stat("old", v_old, c_old_w))
    print(stat("new", v_new, c_new_w))
    print("corr(grade, speed) = %+.2f" % corr)


if __name__ == "__main__":
    main()
