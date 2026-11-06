"""
用合成影像測試辨識流程（沒有真的卡片和相機時用）
- 產生 34 種仿卡片（寶可夢卡、訓練家卡、5 種圖案單純的能量卡），其中有「同模板只差插圖」的相似卡
- 註冊進暫存資料庫（模擬：每張卡在相機下拍一次）
- 產生 120 個桌面場景：每個場景 1~4 張卡，隨機傾斜、旋轉、遠近、亮度、模糊、雜訊、反光
- 另外混入「沒註冊過的卡」，檢查會不會被亂認

執行：python tests/synthetic_test.py
"""
import random
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision import CARD_H, CARD_W, CardDB, find_cards, warp_card  # noqa: E402

rng = random.Random(7)
np.random.seed(7)
FONT = None
for p in ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "C:/Windows/Fonts/msjhbd.ttc", "/System/Library/Fonts/PingFang.ttc"]:
    if Path(p).exists():
        FONT = p
        break
F = lambda s: ImageFont.truetype(FONT, s) if FONT else ImageFont.load_default()

NAMES = ["火紅不倒翁", "達摩狒狒", "噴火龍V", "熾焰咆哮虎V", "火球鼠", "暖暖豬", "炭小侍", "焚焰蚣V", "綠毛蟲", "鐵甲蛹", "巴大蝶V",
         "妙蛙花V", "樹才怪", "蘑蘑菇", "寶貝球", "精靈球", "能量回收", "博士的研究", "老大的指令", "瑪俐", "交替推車", "競技場",
         "寶可夢通訊", "大比鳥", "比比鳥", "波波", "毛球", "毛頭小鼠", "小拉達", "拉達", ]
ENERGY = [("基本火能量", (40, 60, 220), "fire"), ("基本草能量", (60, 170, 60), "grass"), ("基本水能量", (210, 120, 40), "water"),
          ("基本雷能量", (40, 200, 230), "lightning"), ("基本超能量", (170, 70, 150), "psychic")]


def artwork(w, h, seed):
    r = np.random.RandomState(seed)
    img = np.zeros((h, w, 3), np.uint8)
    base = r.randint(40, 220, 3)
    yy, xx = np.mgrid[0:h, 0:w]
    for c in range(3):
        img[..., c] = np.clip(base[c] + 60 * np.sin(xx / r.uniform(8, 30) + r.uniform(0, 6)) * np.cos(yy / r.uniform(8, 30)), 0, 255)
    for _ in range(r.randint(8, 18)):
        color = tuple(int(v) for v in r.randint(0, 255, 3))
        kind = r.randint(3)
        if kind == 0:
            cv2.circle(img, (r.randint(w), r.randint(h)), r.randint(6, 40), color, -1)
        elif kind == 1:
            pts = r.randint(0, max(w, h), (r.randint(3, 6), 2)).reshape(-1, 1, 2)
            cv2.fillPoly(img, [pts], color)
        else:
            cv2.line(img, (r.randint(w), r.randint(h)), (r.randint(w), r.randint(h)), color, r.randint(2, 8))
    return img


def make_card(name, i, trainer=False):
    """仿卡片：黃邊、類型色底、名字、HP、插圖、招式文字"""
    card = Image.new("RGB", (CARD_W, CARD_H), (238, 205, 60))
    d = ImageDraw.Draw(card)
    tint = [(230, 120, 90), (130, 200, 120), (120, 160, 230), (200, 200, 210)][i % 4]
    d.rounded_rectangle((12, 12, CARD_W - 12, CARD_H - 12), 10, fill=(225, 225, 230) if trainer else tint)
    d.text((24, 22), name, font=F(24), fill=(20, 20, 20))
    if not trainer:
        d.text((CARD_W - 110, 24), f"HP{rng.choice([60, 70, 90, 120, 210, 220])}", font=F(20), fill=(200, 20, 20))
    art = artwork(CARD_W - 50, 150, 1000 + i)
    card.paste(Image.fromarray(art[..., ::-1]), (25, 62))
    y = 230
    for _ in range(3):
        d.text((26, y), "".join(rng.choice("攻擊傷害能量對手寶可夢抽牌回合狀態") for _ in range(rng.randint(6, 12))) + f"  {rng.choice([20, 30, 60, 90, 120])}", font=F(18), fill=(30, 30, 30))
        y += 46
    return cv2.cvtColor(np.array(card), cv2.COLOR_RGB2BGR)


def make_energy(name, bgr, kind):
    card = np.full((CARD_H, CARD_W, 3), (60, 205, 238), np.uint8)
    cv2.rectangle(card, (12, 12), (CARD_W - 12, CARD_H - 12), tuple(int(v * 0.6 + 100) for v in bgr), -1)
    c = (CARD_W // 2, CARD_H // 2)
    cv2.circle(card, c, 95, (255, 255, 255), -1)
    cv2.circle(card, c, 85, bgr, -1)
    # 每種能量不同的符號
    if kind == "fire":
        cv2.fillPoly(card, [np.array([[c[0], c[1] - 60], [c[0] + 40, c[1] + 40], [c[0] - 40, c[1] + 40]])], (255, 255, 255))
    elif kind == "grass":
        cv2.ellipse(card, c, (30, 60), 30, 0, 360, (255, 255, 255), -1)
    elif kind == "water":
        cv2.circle(card, (c[0], c[1] + 15), 38, (255, 255, 255), -1)
    elif kind == "lightning":
        cv2.fillPoly(card, [np.array([[c[0] + 10, c[1] - 60], [c[0] - 30, c[1] + 5], [c[0], c[1] + 5], [c[0] - 10, c[1] + 60], [c[0] + 30, c[1] - 5], [c[0], c[1] - 5]])], (255, 255, 255))
    else:
        cv2.circle(card, c, 45, (255, 255, 255), 10)
    pil = Image.fromarray(card[..., ::-1]); ImageDraw.Draw(pil).text((24, CARD_H - 52), name, font=F(22), fill=(20, 20, 20))
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def place(scene, card, center, scale, angle, tilt):
    """把卡片以透視方式貼到桌面上（模擬相機斜拍）"""
    h, w = card.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    sw, sh = w * scale, h * scale
    pts = np.float32([[-sw / 2, -sh / 2], [sw / 2, -sh / 2], [sw / 2, sh / 2], [-sw / 2, sh / 2]])
    pts[0, 0] += tilt * sw; pts[1, 0] -= tilt * sw          # 上緣變窄 = 往後傾
    a = np.deg2rad(angle)
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    dst = (pts @ R.T + center).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(card, M, (scene.shape[1], scene.shape[0]))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (scene.shape[1], scene.shape[0]))
    scene[mask > 0] = warped[mask > 0]
    return dst


def make_scene(cards):
    H, W = 720, 1280
    scene = np.random.randint(25, 55, (H, W, 3)).astype(np.uint8)        # 深色桌墊
    scene = cv2.GaussianBlur(scene, (0, 0), 3)
    slots = rng.sample([(x, y) for x in (230, 640, 1050) for y in (205, 515)], len(cards))  # 卡片完整入鏡、彼此不重疊
    truth = []
    for (cx, cy), (name, img) in zip(slots, cards):
        dst = place(scene, img, (cx + rng.uniform(-12, 12), cy + rng.uniform(-8, 8)),
                    rng.uniform(0.40, 0.55), rng.choice([0, 0, 90, 180, 270]) + rng.uniform(-25, 25), rng.uniform(0, 0.08))
        truth.append((name, dst.mean(0)))
    # 拍照瑕疵：亮度、對比、模糊、雜訊、反光
    scene = cv2.convertScaleAbs(scene, alpha=rng.uniform(0.75, 1.25), beta=rng.uniform(-30, 30))
    if rng.random() < 0.5:
        scene = cv2.GaussianBlur(scene, (0, 0), rng.uniform(0.6, 1.6))
    scene = np.clip(scene + np.random.normal(0, rng.uniform(2, 8), scene.shape), 0, 255).astype(np.uint8)
    if rng.random() < 0.4:
        glare = np.zeros_like(scene, np.float32)
        cv2.circle(glare, (rng.randint(0, W), rng.randint(0, H)), rng.randint(60, 140), (90, 90, 90), -1)
        scene = np.clip(scene + cv2.GaussianBlur(glare, (0, 0), 40), 0, 255).astype(np.uint8)
    return scene, truth


def main():
    tmp = Path(tempfile.mkdtemp())
    db = CardDB(tmp / "db")
    cards = {}
    for i, n in enumerate(NAMES):
        cards[n] = make_card(n, i, trainer=i >= 14 and i <= 22)
    for n, bgr, k in ENERGY:
        cards[n] = make_energy(n, bgr, k)
    unknown = {f"未註冊{i}": make_card(f"沒見過的卡{i}", 100 + i) for i in range(4)}

    # 註冊：模擬把每張卡放在相機下拍一次（也經過偵測 + 拉正，跟實際流程一樣）
    for n, img in cards.items():
        sc, _ = make_scene([(n, img)])
        qs = find_cards(sc)
        assert qs, f"註冊時偵測不到 {n}"
        db.add(warp_card(sc, max(qs, key=cv2.contourArea)), n, "energy" if "能量" in n else "pokemon", "測試", 1)
    print(f"已註冊 {len(db.names())} 種卡")

    det_total = det_ok = id_ok = id_wrong = id_none = 0
    unk_total = unk_false = 0
    confusions = []
    pool = list(cards.items())
    for s in range(120):
        chosen = rng.sample(pool, rng.randint(1, 4))
        if rng.random() < 0.25:
            chosen.append(rng.choice(list(unknown.items())))
        scene, truth = make_scene(chosen)
        quads = find_cards(scene)
        for name, center in truth:
            q = min(quads, key=lambda q: np.linalg.norm(q.mean(0) - center), default=None)
            is_unknown = name.startswith("未註冊")
            if is_unknown:
                unk_total += 1
            else:
                det_total += 1
            if q is None or np.linalg.norm(q.mean(0) - center) > 40:
                continue
            res = db.identify(warp_card(scene, q))
            got = res.entry.name if res.entry else None
            if is_unknown:
                if got:
                    unk_false += 1
                continue
            det_ok += 1
            if got == name:
                id_ok += 1
            elif got is None:
                id_none += 1
            else:
                id_wrong += 1
                confusions.append((name, got, res.method, res.detail))
        if s == 0:
            cv2.imwrite(str(Path(__file__).parent / "sample_scene.png"), scene)

    print(f"偵測率        {det_ok}/{det_total} = {det_ok / det_total:.1%}")
    print(f"辨識正確      {id_ok}/{det_ok} = {id_ok / det_ok:.1%}")
    print(f"認不出（安全）{id_none}")
    print(f"認錯（危險）  {id_wrong}")
    print(f"未註冊卡被誤認 {unk_false}/{unk_total}")
    for c in confusions[:10]:
        print("  認錯：", c)
    shutil.rmtree(tmp)
    return id_wrong == 0 and det_ok / det_total > 0.9 and id_ok / det_ok > 0.9


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
