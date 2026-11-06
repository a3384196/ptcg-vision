"""
疊卡測試：寶可夢上面疊一張能量／進化卡，故意疊歪（位移、轉角），有時再加一隻「手」擋住一部分。
比較：
  A. 原本的方法（找完整卡片輪廓再辨識）
  B. 區域比對（identify_region：直接在區域裡找最上面那張）
執行：python tests/stack_test.py
"""
import random
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import synthetic_test as T  # noqa: E402
from vision import CardDB, find_cards, quad_from_change, warp_card  # noqa: E402

rng = random.Random(11)
W, H = 960, 540
ZONE = (380, 60, 220, 300)        # x, y, w, h：一個區域（跟對戰墊上的格子差不多大）


def bg():
    return cv2.GaussianBlur(np.random.randint(25, 55, (H, W, 3)).astype(np.uint8), (0, 0), 3)


def put(sc, img, cx, cy, ang, scale=0.43):
    return T.place(sc, img, (cx, cy), scale, ang, 0.02)


def hand(sc):
    """一隻「手」從區域邊緣伸進來（膚色橢圓）"""
    x, y, w, h = ZONE
    cx = rng.choice([x - 10, x + w + 10]); cy = rng.randint(y + 40, y + h - 40)
    cv2.ellipse(sc, (cx, cy), (rng.randint(50, 80), rng.randint(30, 45)), rng.randint(0, 180), 0, 360, (120, 160, 210), -1)


def main():
    tmp = Path(tempfile.mkdtemp())
    db = CardDB(tmp / "db")
    names = T.NAMES[:16]
    cards = {n: T.make_card(n, i) for i, n in enumerate(names)}
    for n, bgr, k in T.ENERGY[:3]:
        cards[n] = T.make_energy(n, bgr, k)
    for n, img in cards.items():                     # 註冊（跟實際一樣：放在桌上拍）
        sc = bg(); put(sc, img, W / 2, H / 2, 0)
        db.add(warp_card(sc, max(find_cards(sc), key=cv2.contourArea)), n, "pokemon", "t", 1)
    pokes = names[:10]
    tops = names[10:16] + [n for n, *_ in T.ENERGY[:3]]

    res = {"A": [0, 0, 0], "B": [0, 0, 0], "C": [0, 0, 0]}           # 正確、認不出、認錯
    wrong = []
    N = 150
    for i in range(N):
        base, top = rng.choice(pokes), rng.choice(tops)
        sc = bg()
        x, y, w, h = ZONE
        cx, cy = x + w / 2, y + h / 2
        a0 = rng.uniform(-6, 6)
        put(sc, cards[base], cx, cy, a0)
        before = cv2.convertScaleAbs(sc, alpha=rng.uniform(0.95, 1.05), beta=rng.uniform(-8, 8))   # 放上去之前的畫面
        off = rng.uniform(0.08, 0.35)                 # 疊歪的程度：位移卡片寬度的 8%～35%
        dx, dy = rng.choice([-1, 1]) * off * 135 * rng.random(), rng.choice([-1, 1]) * off * 190 * rng.random()
        put(sc, cards[top], cx + dx, cy + dy, a0 + rng.uniform(-12, 12))
        if rng.random() < 0.35:
            hand(sc)
        sc = cv2.convertScaleAbs(sc, alpha=rng.uniform(0.92, 1.08), beta=rng.uniform(-10, 10))
        sl = (slice(max(0, y - 30), y + h + 30), slice(max(0, x - 30), x + w + 30))
        crop = sc[sl]

        # A：輪廓法
        qs = [q for q in find_cards(sc) if x - 30 <= q.mean(0)[0] <= x + w + 30]
        got = None
        if qs:
            r = db.identify(warp_card(sc, max(qs, key=cv2.contourArea)))
            got = r.entry.name if r.entry else None
        res["A"][0 if got == top else 1 if got is None else 2] += 1
        # B：區域比對
        r, q = db.identify_region(crop)
        got = r.entry.name if r.entry else None
        res["B"][0 if got == top else 1 if got is None else 2] += 1
        if got not in (None, top):
            wrong.append((base, top, got, r.detail))
        # C：跟放上去之前比 → 找出新卡 → 辨識；找不到再用 B
        got = None
        q = quad_from_change(before[sl], crop)
        if q is not None:
            r = db.identify(warp_card(crop, q))
            got = r.entry.name if r.entry else None
        if got is None:
            r, _ = db.identify_region(crop)
            got = r.entry.name if r.entry else None
        res["C"][0 if got == top else 1 if got is None else 2] += 1
        if got not in (None, top):
            wrong.append((base, top, got, "C"))
        if i < 3:
            cv2.imwrite(str(tmp.parent / f"stack_{i}.png"), crop)

    for k, (ok, none, bad) in res.items():
        label = {"A": "A 輪廓法", "B": "B 區域比對", "C": "C 前後比對＋區域比對"}[k]
        print(f"{label}：正確 {ok}/{N} ({ok / N:.0%})　認不出 {none}　認錯 {bad}")
    for w_ in wrong[:8]:
        print("  認錯：底下", w_[0], "上面", w_[1], "→ 認成", w_[2], w_[3])
    shutil.rmtree(tmp)
    return res["B"][2] == 0 and res["C"][2] == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
