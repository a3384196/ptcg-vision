"""
寶可夢卡片辨識 — 第 1 階段原型

執行：  python app.py              （預設第 0 支相機）
        python app.py --camera 1   （外接相機通常是 1）
        python app.py --import 照片資料夾   （用照片批次註冊，檔名就是卡名）

鍵盤：
  R  切換「註冊模式」/「辨識模式」
  空白鍵  （註冊模式）把畫面中最大的那張卡註冊進資料庫，接著在終端機輸入卡名
  E  匯出卡表 decklist.csv
  D  顯示 / 隱藏偵測除錯畫面（調整燈光、背景時用）
  S  存截圖
  Q / Esc  離開
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from vision import CARD_H, CARD_W, KINDS, CardDB, find_cards, guess_kind, warp_card

# ---------------------------------------------------------------- 中文字
FONT_PATHS = [
    "C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/msjhbd.ttc", "C:/Windows/Fonts/mingliu.ttc",
    "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
_fonts: dict[int, ImageFont.ImageFont] = {}


def font(size: int):
    if size not in _fonts:
        for p in FONT_PATHS:
            if Path(p).exists():
                _fonts[size] = ImageFont.truetype(p, size)
                break
        else:
            print("⚠ 找不到中文字型，畫面上的中文會變成方塊（不影響辨識）")
            _fonts[size] = ImageFont.load_default()
    return _fonts[size]


def draw_texts(img: np.ndarray, items: list[tuple]):
    """一次畫多段文字：items = [(x, y, text, size, (b,g,r), bg(b,g,r)|None), ...]"""
    if not items:
        return img
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    for x, y, text, size, color, bg in items:
        f = font(size)
        if bg is not None:
            l, t, r, b = d.textbbox((x, y), text, font=f)
            d.rounded_rectangle((l - 6, t - 4, r + 6, b + 4), radius=6, fill=bg[::-1])
        d.text((x, y), text, font=f, fill=color[::-1])
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------- 追蹤：同一張卡沒移動就不用重算
class Tracker:
    def __init__(self, db: CardDB):
        self.db = db
        self.tracks: list[dict] = []

    def update(self, frame, quads, budget=2):
        now = time.time()
        new_tracks = []
        for q in quads:
            c = q.mean(0)
            old = min(self.tracks, key=lambda t: np.linalg.norm(t["c"] - c), default=None)
            if old is not None and np.linalg.norm(old["c"] - c) < 25 and not any(old is t for t in new_tracks):
                old.update(q=q, c=c, seen=now)
                new_tracks.append(old)
            else:
                new_tracks.append({"q": q, "c": c, "seen": now, "res": None, "votes": {}, "t": 0})
        # 每張卡最多每 0.4 秒重新辨識一次；每格最多算 budget 張（保持流暢）
        todo = sorted(new_tracks, key=lambda t: t["t"])
        for t in todo[:budget]:
            if now - t["t"] < 0.4 and t["res"] is not None:
                continue
            res = self.db.identify(warp_card(frame, t["q"]))
            t["t"] = now
            key = res.entry.name if res.entry else None
            t["votes"][key] = t["votes"].get(key, 0) + 1 + res.score
            t["res"] = res
        self.tracks = new_tracks
        return self.tracks


def stable_name(t):
    """多次辨識投票，避免字跳來跳去"""
    if not t["votes"]:
        return None, 0
    name, v = max(t["votes"].items(), key=lambda kv: kv[1])
    total = sum(t["votes"].values())
    return name, v / total


# ---------------------------------------------------------------- 註冊
def ask(prompt, default=""):
    try:
        s = input(prompt).strip()
    except EOFError:
        s = ""
    return s or default


def register_interactive(db: CardDB, card_img: np.ndarray, last: dict):
    cv2.imshow("register", card_img)
    cv2.waitKey(1)
    names = db.names()
    print("\n────────── 註冊新卡 ──────────")
    if names:
        print("已註冊：" + "、".join(names[-12:]) + ("…" if len(names) > 12 else ""))
    name = ask("卡名（直接 Enter = 取消；輸入已有的卡名 = 放進另一副牌組或多加照片）：")
    if not name:
        print("已取消")
        cv2.destroyWindow("register")
        return
    existing = name in db.names()
    if existing:
        kind = db.kind_of(name)
        now = db.decks_of(name)
        print(f"→「{name}」已經註冊過（{KINDS.get(kind, kind)}）" + (f"，目前在：" + "、".join(f"{d}×{q}" for d, q in now.items()) if now else ""))
    else:
        guess = guess_kind(name, last.get("kind", "pokemon"))
        menu = ["pokemon", "item", "tool", "supporter", "stadium", "energy", "special_energy"]
        k = ask("種類 " + " ".join(f"{i + 1}={KINDS[m]}" for i, m in enumerate(menu)) + f" [預設 {KINDS[guess]}]：")
        kind = menu[int(k) - 1] if k.isdigit() and 1 <= int(k) <= len(menu) else guess
    deck = ask(f"放進哪一副牌組 [預設 {last.get('deck', '牌組A')}]：", last.get("deck", "牌組A"))
    q = ask(f"這張卡在「{deck}」裡有幾張 [預設 1]：", "1")
    qty = int(q) if q.isdigit() else 1
    last.update(kind=kind, deck=deck)
    if existing and ask("要多存一張照片幫助辨識嗎？(y/N)：").lower() != "y":
        db.set_qty(deck, name, qty)
        print(f"✅ 已把「{name}」×{qty} 加進「{deck}」")
        cv2.destroyWindow("register")
        return
    db.add(card_img, name, kind, deck, qty)
    print(f"✅ 已註冊：{name}（目前共 {len(db.names())} 種卡、{len(db.entries)} 張照片）")
    cv2.destroyWindow("register")


def export_csv(db: CardDB, path="decklist.csv"):
    rows = db.decklist()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig：Excel 開中文不會亂碼
        w = csv.writer(f)
        w.writerow(["牌組", "卡名", "種類", "張數", "註冊照片數"])
        for r in rows:
            w.writerow([r["deck"], r["name"], KINDS.get(r["kind"], r["kind"]), r["qty"], r["photos"]])
    by_deck = {}
    for r in rows:
        by_deck[r["deck"]] = by_deck.get(r["deck"], 0) + r["qty"]
    print(f"\n📄 已匯出 {path}：" + "，".join(f"{d} 共 {n} 張" for d, n in by_deck.items()))
    for d, n in by_deck.items():
        if n != 60:
            print(f"   ⚠ {d} 目前是 {n} 張（完整牌組應該是 60 張）")


def import_folder(db: CardDB, folder: str, kind: str, deck: str):
    """用照片批次註冊：每張照片裡放一張卡，檔名 = 卡名（例：火紅不倒翁.jpg、火紅不倒翁_2.jpg）"""
    files = [p for p in Path(folder).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    ok = 0
    for p in sorted(files):
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        quads = find_cards(img, min_area=0.01, max_area=0.98)
        if quads:
            q = max(quads, key=lambda q: cv2.contourArea(q))
            card = warp_card(img, q)
        else:   # 已經是裁好的卡片圖
            card = cv2.resize(img, (CARD_W, CARD_H))
        name = p.stem.split("_")[0]
        k = guess_kind(name, kind)
        db.add(card, name, k, deck)
        ok += 1
        print(f"  ✅ {name}{'' if quads else '（找不到邊框，用整張圖）'}")
    print(f"共註冊 {ok} 張")


# ---------------------------------------------------------------- 主程式
def open_camera(idx: int, w: int, h: int):
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if sys.platform.startswith("win") else cv2.VideoCapture(idx)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    if not cap.isOpened():
        sys.exit(f"打不開相機 {idx}。外接相機請試試 --camera 1 或 --camera 2")
    return cap


def main():
    ap = argparse.ArgumentParser(description="寶可夢卡片辨識原型")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--db", default="db")
    ap.add_argument("--import", dest="imp", help="用照片資料夾批次註冊")
    ap.add_argument("--kind", default="pokemon", choices=list(KINDS))
    ap.add_argument("--deck", default="牌組A")
    a = ap.parse_args()

    db = CardDB(a.db)
    print(f"資料庫：{len(db.names())} 種卡、{len(db.entries)} 張照片")
    if a.imp:
        import_folder(db, a.imp, a.kind, a.deck)
        return

    cap = open_camera(a.camera, a.width, a.height)
    tracker = Tracker(db)
    mode = "register" if not db.entries else "recognize"
    show_debug = False
    last = {}
    fps, t_prev = 0.0, time.time()
    print(__doc__)

    while True:
        ok, frame = cap.read()
        if not ok:
            print("讀不到相機畫面"); break
        dbg = {} if show_debug else None
        quads = find_cards(frame, debug=dbg)
        view = frame.copy()
        texts = []

        if mode == "recognize":
            tracks = tracker.update(frame, quads)
            for t in tracks:
                name, agree = stable_name(t)
                res = t["res"]
                pts = t["q"].astype(int)
                color = (80, 220, 80) if name else (60, 60, 255)
                cv2.polylines(view, [pts], True, color, 3)
                x, y = pts[:, 0].min(), pts[:, 1].min() - 34
                label = f"{name}  {int(res.score * 100) if res else 0}%" if name else "？未註冊"
                texts.append((int(x), int(max(0, y)), label, 22, (255, 255, 255), color))
        else:
            big = max(quads, key=lambda q: cv2.contourArea(q)) if quads else None
            for q in quads:
                cv2.polylines(view, [q.astype(int)], True, (0, 200, 255) if q is big else (120, 120, 120), 3 if q is big else 1)
            if big is not None:
                preview = cv2.resize(warp_card(frame, big), (CARD_W // 2, CARD_H // 2))
                view[10:10 + preview.shape[0], view.shape[1] - preview.shape[1] - 10:view.shape[1] - 10] = preview

        # 狀態列
        now = time.time()
        fps = fps * 0.9 + 0.1 / max(1e-3, now - t_prev)
        t_prev = now
        cv2.rectangle(view, (0, 0), (view.shape[1], 44), (30, 30, 30), -1)
        title = "【註冊模式】把一張卡放在畫面中，按空白鍵註冊" if mode == "register" else "【辨識模式】"
        texts.append((12, 8, f"{title}　｜偵測到 {len(quads)} 張｜已註冊 {len(db.names())} 種｜{fps:.0f} FPS", 22, (255, 255, 255), None))
        texts.append((12, view.shape[0] - 34, "R 切換模式　空白 註冊　E 匯出卡表　D 除錯　S 截圖　Q 離開", 18, (230, 230, 230), (40, 40, 40)))
        view = draw_texts(view, texts)
        cv2.imshow("PTCG Vision", view)
        if show_debug and dbg:
            cv2.imshow("edges (debug)", cv2.resize(dbg["edges"], None, fx=0.5, fy=0.5))

        k = cv2.waitKey(1) & 0xFF
        if k in (ord("q"), 27):
            break
        elif k == ord("r"):
            mode = "recognize" if mode == "register" else "register"
            tracker.tracks = []
        elif k == ord(" ") and mode == "register":
            if quads:
                big = max(quads, key=lambda q: cv2.contourArea(q))
                register_interactive(db, warp_card(frame, big), last)
            else:
                print("畫面中沒偵測到卡片：換深色背景、讓整張卡入鏡、避免反光")
        elif k == ord("e"):
            export_csv(db)
        elif k == ord("d"):
            show_debug = not show_debug
            if not show_debug:
                cv2.destroyWindow("edges (debug)")
        elif k == ord("s"):
            fn = time.strftime("shot_%H%M%S.png")
            cv2.imencode(".png", view)[1].tofile(fn)
            print("截圖：" + fn)

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
