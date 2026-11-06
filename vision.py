"""
寶可夢卡片視覺核心
- find_cards():   在相機畫面裡找出所有卡片（四邊形）
- warp_card():    把斜拍的卡片拉正成固定大小的直式圖
- CardDB:         註冊過的卡片資料庫；identify() 判斷畫面上的卡是哪一張

辨識方法（兩種互補）：
1. ORB 特徵點 + RANSAC 幾何驗證：適合圖案豐富的寶可夢卡、訓練家卡
2. 感知雜湊（dHash）+ 顏色分佈：適合圖案很單純的基本能量卡
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

CARD_W, CARD_H = 315, 440          # 卡片標準比例 63 x 88 mm
CARD_RATIO = CARD_W / CARD_H       # ≈ 0.716

# 卡片種類（照官方分類；規則引擎會用到：支援者每回合限 1 張、道具掛在寶可夢身上、競技場留在場上…）
KINDS = {
    "pokemon": "寶可夢",
    "item": "物品",
    "tool": "寶可夢道具",
    "supporter": "支援者",
    "stadium": "競技場",
    "energy": "基本能量",
    "special_energy": "特殊能量",
    "trainer": "訓練家（未分類）",   # 舊版資料相容用
}
KIND_GROUP = {"pokemon": "寶可夢", "item": "訓練家", "tool": "訓練家", "supporter": "訓練家", "stadium": "訓練家",
              "trainer": "訓練家", "energy": "能量", "special_energy": "能量"}


def guess_kind(name: str, default: str = "pokemon") -> str:
    """從卡名猜種類：「基本火能量」→ 基本能量；「雙重渦輪能量」→ 特殊能量；「能量回收」不是能量卡"""
    if name.endswith("能量"):
        return "energy" if name.startswith("基本") else "special_energy"
    return default


# ---------------------------------------------------------------- 偵測
def order_quad(pts: np.ndarray) -> np.ndarray:
    """把四個點排成 左上、右上、右下、左下。"""
    pts = pts.reshape(4, 2).astype(np.float32)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def _side_lengths(q: np.ndarray):
    w = (np.linalg.norm(q[0] - q[1]) + np.linalg.norm(q[3] - q[2])) / 2
    h = (np.linalg.norm(q[0] - q[3]) + np.linalg.norm(q[1] - q[2])) / 2
    return w, h


def find_cards(frame: np.ndarray, min_area: float = 0.008, max_area: float = 0.8,
               ratio_tol: float = 0.14, debug: dict | None = None) -> list[np.ndarray]:
    """回傳畫面中每張卡片的四個角點（已排序）。

    建議：相機從正上方拍、桌面用深色素面墊子，卡片之間不要重疊。
    """
    H, W = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    # 自動門檻的 Canny：依畫面亮度中位數調整
    med = float(np.median(gray))
    edges = cv2.Canny(gray, int(max(10, 0.5 * med)), int(min(255, 1.3 * med + 30)))
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    edges = cv2.erode(edges, np.ones((3, 3), np.uint8), iterations=1)
    if debug is not None:
        debug["edges"] = edges

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    quads = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area * W * H or area > max_area * W * H:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            q = order_quad(approx)
        else:
            # 卡片是圓角，輪廓常常不是剛好 4 個點 → 夠「實心」就用最小外接矩形
            rect = cv2.minAreaRect(c)
            rw, rh = rect[1]
            if rw * rh == 0 or area / (rw * rh) < 0.82:
                continue
            q = order_quad(cv2.boxPoints(rect))
        w, h = _side_lengths(q)
        if min(w, h) < 20:
            continue
        r = min(w, h) / max(w, h)
        if abs(r - CARD_RATIO) > ratio_tol:
            continue
        quads.append(q)
    # 由左到右、由上到下排序，畫面比較穩定
    quads.sort(key=lambda q: (round(q[:, 1].mean() / 80), q[:, 0].mean()))
    return quads


def warp_card(frame: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """透視校正成 CARD_W x CARD_H 的直式卡片。橫放的卡會自動轉成直的。"""
    q = quad.copy()
    w, h = _side_lengths(q)
    if w > h:  # 卡片橫放：起點改成左下角，讓輸出變直式
        q = np.array([q[3], q[0], q[1], q[2]], dtype=np.float32)
    dst = np.array([[0, 0], [CARD_W - 1, 0], [CARD_W - 1, CARD_H - 1], [0, CARD_H - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(q, dst)
    return cv2.warpPerspective(frame, M, (CARD_W, CARD_H))


# ---------------------------------------------------------------- 特徵
_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
_orb = cv2.ORB_create(nfeatures=900, scaleFactor=1.2, nlevels=8, fastThreshold=10)
_orb_region = cv2.ORB_create(nfeatures=2500, scaleFactor=1.2, nlevels=8, fastThreshold=8)   # 區域比對用：一塊區域裡可能有兩張卡
_bf = cv2.BFMatcher(cv2.NORM_HAMMING)


def _norm_gray(img: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return _clahe.apply(g)


def orb_features(img: np.ndarray):
    kp, des = _orb.detectAndCompute(_norm_gray(img), None)
    pts = np.float32([k.pt for k in kp]) if kp else np.zeros((0, 2), np.float32)
    return pts, des


def dhash(img: np.ndarray, size: int = 16) -> np.ndarray:
    g = cv2.resize(_norm_gray(img), (size + 1, size), interpolation=cv2.INTER_AREA)
    return (g[:, 1:] > g[:, :-1]).ravel()


def color_hist(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # 只看卡片中間（避開黃邊與手指），H-S 2D 直方圖
    m = hsv[40:-40, 30:-30]
    h = cv2.calcHist([m], [0, 1], None, [24, 8], [0, 180, 0, 256])
    cv2.normalize(h, h, 1, 0, cv2.NORM_L1)
    return h



_CORNERS = np.float32([[0, 0], [CARD_W, 0], [CARD_W, CARD_H], [0, CARD_H]]).reshape(-1, 1, 2)
_FLIPPED = _CORNERS[[2, 3, 0, 1]]


def plausible_homography(Hm: np.ndarray | None) -> bool:
    """兩張卡都已拉正成同尺寸直式圖，所以真正同一張卡時，轉換只會是「幾乎不動」或「轉 180 度」。
    重複文字（例如招式說明裡到處都有的「傷害」「能量」）常讓 RANSAC 找到把點壓成一條線的退化解，這裡擋掉。"""
    if Hm is None or not np.isfinite(Hm).all():
        return False
    proj = cv2.perspectiveTransform(_CORNERS, Hm)
    err = min(np.linalg.norm(proj - _CORNERS, axis=2).max(), np.linalg.norm(proj - _FLIPPED, axis=2).max())
    return err < CARD_W * 0.28


@dataclass
class Entry:
    id: str
    name: str
    kind: str = "pokemon"
    deck: str = ""
    qty: int = 1
    img_file: str = ""
    # 執行時計算
    pts: np.ndarray | None = field(default=None, repr=False)
    des: np.ndarray | None = field(default=None, repr=False)
    hashes: list = field(default_factory=list, repr=False)
    hist: np.ndarray | None = field(default=None, repr=False)
    kpts: np.ndarray | None = field(default=None, repr=False)   # 剔除版型後的特徵點
    kdes: np.ndarray | None = field(default=None, repr=False)

    def meta(self):
        return {"id": self.id, "name": self.name, "kind": self.kind, "deck": self.deck, "qty": self.qty, "img_file": self.img_file}


def region_quad(Hm: np.ndarray | None, shape) -> np.ndarray | None:
    """資料庫卡片 → 區域影像 的轉換是否合理（像一張真的卡，而不是重複文字造成的退化解）；合理就回傳卡片四角"""
    if Hm is None or not np.isfinite(Hm).all():
        return None
    q = cv2.perspectiveTransform(_CORNERS, Hm).reshape(4, 2)
    if not cv2.isContourConvex(q.astype(np.float32).reshape(-1, 1, 2)):
        return None
    h, w = shape[:2]
    area = cv2.contourArea(q.astype(np.float32))
    if not (0.04 * w * h < area < 2.0 * w * h):
        return None
    s = [np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]
    if min(s[0], s[2]) / max(s[0], s[2]) < 0.7 or min(s[1], s[3]) / max(s[1], s[3]) < 0.7:   # 對邊要差不多長
        return None
    r = ((s[0] + s[2]) / 2) / ((s[1] + s[3]) / 2)
    if abs(min(r, 1 / r) - CARD_RATIO) > 0.15:
        return None
    return q


def quad_from_change(ref: np.ndarray, cur: np.ndarray, min_frac: float = 0.12) -> np.ndarray | None:
    """跟「放上去之前」的區域畫面比，找出新疊上去的那張卡的四角（區域座標）。
    疊歪的能量卡圖案太單純、特徵點不夠時，靠這個找到它再辨識。"""
    if ref is None or ref.shape != cur.shape:
        return None
    a = cv2.GaussianBlur(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.int16)
    b = cv2.GaussianBlur(cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.int16)
    b = b - int(np.median(b - a))                       # 抵銷整體亮度變化（相機自動曝光）
    hsv_d = cv2.absdiff(cv2.cvtColor(ref, cv2.COLOR_BGR2HSV)[..., 0], cv2.cvtColor(cur, cv2.COLOR_BGR2HSV)[..., 0])
    hsv_d = np.minimum(hsv_d, 180 - hsv_d)              # 色相是環狀的
    mask = ((np.abs(b - a) > 28) | (hsv_d > 18)).astype(np.uint8) * 255
    k = np.ones((7, 7), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < min_frac * mask.size:
        return None
    rect = cv2.minAreaRect(c)
    rw, rh = rect[1]
    if rw * rh == 0 or area / (rw * rh) < 0.72:         # 要像一個實心的長方形
        return None
    if abs(min(rw, rh) / max(rw, rh) - CARD_RATIO) > 0.12:
        return None
    return order_quad(cv2.boxPoints(rect))


@dataclass
class Result:
    entry: Entry | None
    score: float       # 0~1 信心
    method: str        # "orb" | "hash" | "none"
    detail: str = ""


class CardDB:
    """卡片資料庫：db/cards.json + db/images/*.png"""

    MIN_INLIERS = 12

    def __init__(self, folder: str | Path = "db"):
        self.dir = Path(folder)
        (self.dir / "images").mkdir(parents=True, exist_ok=True)
        self.json = self.dir / "cards.json"
        self.decks_json = self.dir / "decks.json"
        self.entries: list[Entry] = []
        # 牌組與卡片分開存：同一張卡（例如先機球）可以同時在好幾副牌組裡，各自有不同張數
        self.decks: dict[str, dict[str, int]] = {}
        self.load()

    # ---------- 讀寫
    def load(self):
        self.entries = []
        if self.json.exists():
            for m in json.loads(self.json.read_text(encoding="utf-8")):
                e = Entry(**m)
                p = self.dir / "images" / e.img_file
                img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR) if p.exists() else None  # 支援中文路徑
                if img is None:
                    continue
                self._prepare(e, img)
                self.entries.append(e)
        if self.decks_json.exists():
            self.decks = json.loads(self.decks_json.read_text(encoding="utf-8"))
        else:   # 舊版資料：牌組記在每張照片上 → 轉成獨立的牌組表
            self.decks = {}
            for e in self.entries:
                if e.deck:
                    d = self.decks.setdefault(e.deck, {})
                    d[e.name] = max(d.get(e.name, 0), e.qty)
            if self.decks:
                self.save()
        self._build_index()

    def _build_index(self):
        """建立比對索引。

        關鍵：所有卡片共用同一個版型（黃框、名字列、HP、招式框），這些「版型特徵點」在每張卡上
        長得一樣、位置也一樣，會讓幾何驗證誤以為兩張不同的卡是同一張。所以先找出
        「在別種卡的同一個位置也出現」的特徵點並剔除，只留下每張卡獨有的部分（插圖、卡名、招式文字）。
        """
        cands = [e for e in self.entries if e.des is not None and len(e.des) >= 8]
        for e in cands:                       # 預設：全部保留
            e.kpts, e.kdes = e.pts, e.des
        names = {e.name for e in cands}
        if len(names) >= 3:
            allf = cv2.FlannBasedMatcher(dict(algorithm=6, table_number=8, key_size=14, multi_probe_level=1), dict(checks=64))
            allf.add([e.des for e in cands])
            allf.train()
            for i, e in enumerate(cands):
                keep = np.ones(len(e.des), bool)
                for nn in allf.knnMatch(e.des, k=10):
                    if not nn:
                        continue
                    hits = set()
                    for m in nn:
                        o = cands[m.imgIdx]
                        if o.name == e.name or m.distance > 48:
                            continue
                        p, q = e.pts[m.queryIdx], o.pts[m.trainIdx]
                        flip = np.array([CARD_W, CARD_H]) - q        # 對方可能是顛倒註冊的
                        if min(np.abs(p - q).max(), np.abs(p - flip).max()) < 18:
                            hits.add(o.name)
                    if len(hits) >= max(1, min(2, len(names) - 2)):
                        keep[nn[0].queryIdx] = False
                if keep.sum() >= 20:              # 剩太少就不要過濾（避免圖案很單純的卡被清空）
                    e.kpts, e.kdes = e.pts[keep], e.des[keep]

        self._idx_entries = [e for e in cands if len(e.kdes) >= 8]
        self._flann = None
        if self._idx_entries:
            self._flann = cv2.FlannBasedMatcher(dict(algorithm=6, table_number=8, key_size=14, multi_probe_level=1), dict(checks=64))
            self._flann.add([e.kdes for e in self._idx_entries])
            self._flann.train()

    def save(self):
        self.json.write_text(json.dumps([e.meta() for e in self.entries], ensure_ascii=False, indent=1), encoding="utf-8")
        self.decks = {d: {n: q for n, q in cards.items() if q > 0} for d, cards in self.decks.items()}
        self.decks = {d: c for d, c in self.decks.items() if c}
        self.decks_json.write_text(json.dumps(self.decks, ensure_ascii=False, indent=1), encoding="utf-8")

    def _prepare(self, e: Entry, img: np.ndarray):
        e.pts, e.des = orb_features(img)
        e.hashes = [dhash(img), dhash(cv2.rotate(img, cv2.ROTATE_180))]
        e.hist = color_hist(img)

    def add(self, card_img: np.ndarray, name: str, kind: str = "pokemon", deck: str = "", qty: int = 1) -> Entry:
        """註冊一張卡的照片（同一張卡可以有多張不同光線的照片，會提高辨識率）。
        有給 deck 就同時把它放進該牌組（張數 = qty）。"""
        eid = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        fn = f"{eid}.png"
        cv2.imencode(".png", card_img)[1].tofile(str(self.dir / "images" / fn))
        old = next((x for x in self.entries if x.name == name), None)
        e = Entry(id=eid, name=name, kind=old.kind if old else kind, deck=deck, qty=qty, img_file=fn)
        self._prepare(e, card_img)
        self.entries.append(e)
        if deck:
            self.decks.setdefault(deck, {})[name] = max(1, qty)
        self.save()
        self._build_index()
        return e

    # ---------- 牌組
    def set_qty(self, deck: str, name: str, qty: int):
        """設定某副牌組裡某張卡的張數；0 = 從這副牌組拿掉（卡片本身仍保留，可放進其他牌組）"""
        d = self.decks.setdefault(deck, {})
        if qty > 0:
            d[name] = qty
        else:
            d.pop(name, None)
        self.save()

    def decks_of(self, name: str) -> dict[str, int]:
        return {d: c[name] for d, c in self.decks.items() if name in c}

    def kind_of(self, name: str) -> str:
        return next((e.kind for e in self.entries if e.name == name), "pokemon")

    def set_kind(self, name: str, kind: str):
        for e in self.entries:
            if e.name == name:
                e.kind = kind
        self.save()

    def rename(self, name: str, new: str):
        for e in self.entries:
            if e.name == name:
                e.name = new
        for d in self.decks.values():
            if name in d:
                d[new] = d.get(new, 0) + d.pop(name)
        self.save()
        self._build_index()

    def remove_name(self, name: str) -> int:
        """刪除卡片（所有照片，並從所有牌組拿掉）"""
        keep = [e for e in self.entries if e.name != name]
        n = len(self.entries) - len(keep)
        self.entries = keep
        for d in self.decks.values():
            d.pop(name, None)
        self.save()
        self._build_index()
        return n

    def names(self) -> list[str]:
        seen, out = set(), []
        for e in self.entries:
            if e.name not in seen:
                seen.add(e.name)
                out.append(e.name)
        return out

    def decklist(self) -> list[dict]:
        """每副牌組的卡表：[{deck, name, kind, qty, photos}]"""
        photos = {}
        for e in self.entries:
            photos[e.name] = photos.get(e.name, 0) + 1
        order = list(KINDS)
        rows = [{"deck": d, "name": n, "kind": self.kind_of(n), "qty": q, "photos": photos.get(n, 0)}
                for d, cards in self.decks.items() for n, q in cards.items()]
        return sorted(rows, key=lambda r: (r["deck"], order.index(r["kind"]) if r["kind"] in order else 9, r["name"]))

    # ---------- 辨識
    def _orb_score(self, q_pts, q_des, e: Entry) -> int:
        if q_des is None or e.des is None or len(q_des) < 8 or len(e.des) < 8:
            return 0
        pairs = _bf.knnMatch(q_des, e.des, k=2)
        good = [m for m, *rest in pairs if rest and m.distance < 0.78 * rest[0].distance]
        if len(good) < 8:
            return 0
        src = q_pts[[m.queryIdx for m in good]]
        dst = e.pts[[m.trainIdx for m in good]]
        Hm, mask = cv2.findHomography(src, dst, cv2.RANSAC, 6.0)
        return int(mask.sum()) if plausible_homography(Hm) else 0

    def identify(self, card_img: np.ndarray) -> Result:
        if not self.entries:
            return Result(None, 0, "none", "資料庫是空的")
        q_pts, q_des = orb_features(card_img)
        q_hash = dhash(card_img)
        q_hist = color_hist(card_img)

        # 1) ORB：用索引找候選 → 對前幾名做 RANSAC 幾何驗證（同名卡取最高）
        top_s, top_e, second = 0, None, 0
        if self._flann is not None and q_des is not None and len(q_des) >= 8:
            buckets: dict[int, list] = {}
            for nn in self._flann.knnMatch(q_des, k=3):
                if not nn:
                    continue
                m = nn[0]
                name0 = self._idx_entries[m.imgIdx].name
                # 比率測試：要明顯比「別張卡」的最近點更近（同一張卡的其他照片不算競爭者）
                rival = next((x for x in nn[1:] if self._idx_entries[x.imgIdx].name != name0), None)
                if rival is None or m.distance < 0.8 * rival.distance:
                    buckets.setdefault(m.imgIdx, []).append(m)
            cands = sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:5]
            best_by_name: dict[str, tuple[int, Entry]] = {}
            for ei, ms in cands:
                e = self._idx_entries[ei]
                s_ = 0
                if len(ms) >= 6:
                    src = q_pts[[m.queryIdx for m in ms]]
                    dst = e.kpts[[m.trainIdx for m in ms]]
                    Hm, mask = cv2.findHomography(src, dst, cv2.RANSAC, 6.0)
                    s_ = int(mask.sum()) if plausible_homography(Hm) else 0
                if s_ > best_by_name.get(e.name, (-1, None))[0]:
                    best_by_name[e.name] = (s_, e)
            ranked = sorted(best_by_name.values(), key=lambda t: -t[0])
            if ranked:
                top_s, top_e = ranked[0]
                second = ranked[1][0] if len(ranked) > 1 else 0
        if top_e is not None and top_s >= self.MIN_INLIERS and top_s >= second * 2 + 4:  # 要明顯勝過第二名，寧可認不出也不要認錯
            conf = min(1.0, 0.5 + top_s / 120) * (1 - second / max(top_s, 1) * 0.5)
            return Result(top_e, round(conf, 2), "orb", f"inliers={top_s} 次高={second}")

        # 2) 雜湊 + 顏色（能量卡這類圖案單純的卡）
        best = None
        for e in self.entries:
            hd = min(np.count_nonzero(q_hash != h) for h in e.hashes) / q_hash.size   # 0 最像
            hc = cv2.compareHist(q_hist, e.hist, cv2.HISTCMP_BHATTACHARYYA)              # 0 最像
            d = hd * 0.6 + hc * 0.4
            if best is None or d < best[0]:
                best = (d, e, hd, hc)
        d, e, hd, hc = best
        if hd < 0.22 and hc < 0.45:
            return Result(e, round(max(0.3, 1 - d * 2), 2), "hash", f"hash={hd:.2f} color={hc:.2f}")
        return Result(None, 0, "none", f"最像 {e.name}（inliers={top_s} hash={hd:.2f}）")

    def identify_region(self, img: np.ndarray, accept=None) -> tuple[Result, np.ndarray | None]:
        """在一塊區域（卡片疊歪、被手指擋住、找不到完整輪廓）裡，找出「最上面」那張已註冊的卡。

        方法：區域的特徵點直接跟資料庫比對，對每個候選卡算出它在區域裡的位置；
        被別的卡壓住的卡，會有很多特徵點「應該看得到卻沒對上」→ 可見比例低。
        取可見比例最高、而且明顯勝出的那張；回傳 (結果, 卡片在區域裡的四角)。
        accept(cx, cy)：卡片中心（區域影像座標）是否算在這一格；壓到隔壁格的卡不算。
        """
        none = (Result(None, 0, "none", ""), None)
        if self._flann is None:
            return none
        h0, w0 = img.shape[:2]
        scale = 560 / max(h0, w0)                       # 放大到跟註冊圖差不多的尺度
        im = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale > 1.05 else img
        scale = im.shape[0] / h0
        kp, des = _orb_region.detectAndCompute(_norm_gray(im), None)
        if des is None or len(des) < 20:
            return none
        pts = np.float32([k.pt for k in kp])
        buckets: dict[int, list] = {}
        for nn in self._flann.knnMatch(des, k=3):
            if not nn:
                continue
            m = nn[0]
            name0 = self._idx_entries[m.imgIdx].name
            rival = next((x for x in nn[1:] if self._idx_entries[x.imgIdx].name != name0), None)
            if rival is None or m.distance < 0.8 * rival.distance:
                buckets.setdefault(m.imgIdx, []).append(m)
        found = {}
        for ei, ms in sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:6]:
            if len(ms) < 8:
                continue
            e = self._idx_entries[ei]
            src = e.kpts[[m.trainIdx for m in ms]]
            dst = pts[[m.queryIdx for m in ms]]
            Hm, mask = cv2.findHomography(src, dst, cv2.RANSAC, 8.0)
            q = region_quad(Hm, im.shape)
            if q is None:
                continue
            if accept is not None:
                cx, cy = q.mean(0) / scale
                if not accept(cx, cy):          # 這張卡的中心在隔壁格 → 不是這一格的卡
                    continue
            inl = int(mask.sum())
            # 這張卡的特徵點投影到區域裡、落在畫面內的有幾個 → 可見比例
            proj = cv2.perspectiveTransform(e.kpts.reshape(-1, 1, 2), Hm).reshape(-1, 2)
            inside = np.count_nonzero((proj[:, 0] >= 0) & (proj[:, 1] >= 0) & (proj[:, 0] < im.shape[1]) & (proj[:, 1] < im.shape[0]))
            vis = inl / max(inside, 1)
            if inl > found.get(e.name, (0, 0, None))[0]:
                found[e.name] = (inl, vis, q, e)
        good = [v for v in found.values() if v[0] >= self.MIN_INLIERS]
        if not good:
            return none
        good.sort(key=lambda v: -v[1])
        inl, vis, q, e = good[0]
        if len(good) > 1 and vis < good[1][1] * 1.3:      # 分不出哪張在上面 → 寧可說不知道
            return (Result(None, 0, "none", f"疊卡分不出上下：{e.name}/{good[1][3].name}"), None)
        return (Result(e, round(min(1.0, 0.4 + vis), 2), "region", f"inliers={inl} 可見={vis:.2f}"), q / scale)
