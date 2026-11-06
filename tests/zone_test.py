"""
跨格情境測試：合成畫面直接跑伺服器的對戰墊觀察邏輯（observe_board），每 0.1 秒一格
  S1 卡片放歪、壓到隔壁格            → 只能算一格
  S2 兩張相鄰的卡碰在一起（輪廓黏成一塊） → 兩張都要在正確的格子
  S3 開局時卡已經在桌上，之後被手擋住 → 不能被當成「空了」
  S4 拿著卡經過別格再放下            → 經過的格子不算
  S5 撤退換位時卡片壓到別格          → 只有兩個換位，不能多出寶可夢
執行：python tests/zone_test.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import synthetic_test as T  # noqa: E402

W, H = 960, 540
TMP = Path(tempfile.mkdtemp())
os.environ["PTCG_DB"] = str(TMP / "db")

from board import DEFAULT_ZONES, Board  # noqa: E402
from vision import CardDB, find_cards, warp_card  # noqa: E402

NAMES = ["綠毛蟲", "鐵甲蛹", "快龍V", "不良蛙", "伊布", "驚角鹿", "刺梭魚", "博士的研究"]
CARDS = {n: T.make_card(n, i, trainer=(n == "博士的研究")) for i, n in enumerate(NAMES)}
CARDS["基本草能量"] = T.make_energy("基本草能量", (60, 170, 60), "grass")
Z = {z["id"]: z for z in DEFAULT_ZONES}
BG = cv2.GaussianBlur(np.random.RandomState(1).randint(25, 55, (H, W, 3)).astype(np.uint8), (0, 0), 3)
CW = 440 * 0.43 * 315 / 440   # 卡片在畫面上的寬（像素）


def center(zid):
    z = Z[zid]
    return W * (z["x"] + z["w"] / 2), H * (z["y"] + z["h"] / 2)


def scene(items, hand=None, seed=0):
    """items: [(卡名, 區域, x 偏移(卡寬倍數), 角度)]"""
    sc = BG.copy()
    for n, zid, dx, ang in items:
        cx, cy = center(zid)
        T.place(sc, CARDS[n], (cx + dx * CW, cy), 0.43, ang, 0.02)
    if hand:
        cx, cy = center(hand)
        cv2.ellipse(sc, (int(cx + 40), int(cy + 10)), (85, 40), 25, 0, 360, (120, 160, 210), -1)
    rs = np.random.RandomState(seed)
    return np.clip(sc + rs.normal(0, 3, sc.shape), 0, 255).astype(np.uint8)


def register():
    db = CardDB(TMP / "db")
    for n, img in CARDS.items():
        sc = scene([(n, "active", 0, 0)])
        db.add(warp_card(sc, max(find_cards(sc), key=cv2.contourArea)), n, "pokemon", "t", 1)


register()
import server  # noqa: E402  （要在註冊完之後才載入，伺服器才會讀到資料庫）


class Run:
    def __init__(self):
        self.board = Board(server.book)
        self.tracker = server.Tracker()
        self.sense = server.ZoneSense()
        self.t = 1000.0
        self.events = []
        self.seed = 0

    def hold(self, sec, items, hand=None):
        for _ in range(int(sec * 10)):
            self.t += 0.1
            self.seed += 1
            fr = scene(items, hand, self.seed)
            obs, _, ev = server.observe_board(fr, find_cards(fr), DEFAULT_ZONES, self.tracker, self.sense, self.board, self.t)
            self.events += ev
        return self

    def texts(self):
        return [e["text"] for e in self.events]

    def slot(self, s):
        p = self.board.slots[s]["pokemon"]
        return p[-1] if p else None


def check(name, cond, info=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"\n     {info}"))
    return cond


def main():
    ok = True
    print("S1 卡片放歪、壓到隔壁格（中心還在備戰1）")
    r = Run().hold(4, [("綠毛蟲", "bench1", 0.42, 6)])
    ok &= check("只放到備戰1", r.slot("bench1") == "綠毛蟲" and r.slot("bench2") is None, r.texts())

    print("S1b 放得更歪：中心靠近兩格交界、還有點晃動")
    r = Run()
    for k in range(8):
        r.hold(0.5, [("綠毛蟲", "bench3", 0.55 + 0.06 * ((-1) ** k), 6 + k)])
    ok &= check("只算一隻", sum(1 for s_ in ["bench1", "bench2", "bench3", "bench4", "bench5"] if r.slot(s_)) == 1, r.texts())

    print("S2 兩張相鄰的卡碰在一起")
    r = Run().hold(5, [("綠毛蟲", "bench2", 0.30, 3), ("不良蛙", "bench3", -0.30, -3)])
    ok &= check("兩張都在正確的格子、沒有多出來",
                r.slot("bench2") == "綠毛蟲" and r.slot("bench3") == "不良蛙" and sum(1 for e in r.events if e["type"] == "place") == 2, r.texts())

    print("S3 開局時卡已經在桌上，之後被手擋住 4 秒")
    r = Run().hold(2, [("快龍V", "active", 0, 2)]).hold(4, [("快龍V", "active", 0, 2)], hand="active").hold(6, [("快龍V", "active", 0, 2)])
    ok &= check("戰鬥場一直是快龍V、沒有離場", r.slot("active") == "快龍V" and not any(e["type"] == "leave" for e in r.events), r.texts())

    print("S4 拿著卡經過別格（停 0.6 秒）再放到戰鬥場")
    r = Run().hold(0.6, [("伊布", "bench3", 0.5, 20)]).hold(0.6, [("伊布", "bench2", 0.1, 10)]).hold(3, [("伊布", "active", 0, 0)])
    ok &= check("只放到戰鬥場", r.slot("active") == "伊布" and all(r.slot(f"bench{i}") is None for i in range(1, 6)), r.texts())

    print("S5 撤退換位時卡片壓到別格")
    r = Run().hold(2.5, [("快龍V", "active", 0, 2), ("刺梭魚", "bench1", 0, -2)])
    r.hold(0.4, [])
    r.hold(3, [("刺梭魚", "active", 0, 0), ("快龍V", "bench1", 0.45, 8)])          # 快龍V 放歪，壓到備戰2
    r.hold(6, [("刺梭魚", "active", 0, 0), ("快龍V", "bench1", 0.45, 8)])
    pokes = [r.slot(s) for s in ["active", "bench1", "bench2", "bench3", "bench4", "bench5"]]
    ok &= check("場上剛好兩隻：戰鬥場刺梭魚、備戰1快龍V", pokes == ["刺梭魚", "快龍V", None, None, None, None], (pokes, r.texts()))

    print("S6 戰鬥場的卡跟訓練家區的卡碰在一起（開局就在桌上）")
    r = Run().hold(6, [("快龍V", "active", 0.33, 0), ("博士的研究", "trainer", -0.33, 0)])
    ok &= check("戰鬥場要顯示快龍V", r.slot("active") == "快龍V", r.texts())

    print("\n全部通過 ✅" if ok else "\n有失敗 ❌")
    shutil.rmtree(TMP, ignore_errors=True)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
