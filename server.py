"""
PTCG Vision 網頁伺服器（在 Docker 裡跑）

瀏覽器開相機 → 每張畫面用 WebSocket 傳到這裡 → 找卡片、辨識 → 把結果傳回去畫在網頁上。
"""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ai import AI
from board import DEFAULT_ZONES, Board, CardBook, UNKNOWN, zone_of
from game import Game, load_decks
from vision import CARD_H, CARD_W, KIND_GROUP, KINDS, CardDB, dhash, find_cards, guess_kind, quad_from_change, warp_card

BASE = Path(__file__).parent
DB_DIR = os.environ.get("PTCG_DB", str(BASE / "db"))
db = CardDB(DB_DIR)
db_lock = asyncio.Lock()
book = CardBook(BASE / "data")                      # 卡片效果資料（第 2、3 階段用）
ZONES_FILE = Path(DB_DIR) / "zones.json"


SLOTS_AND_TRAINER = {"active", "bench1", "bench2", "bench3", "bench4", "bench5", "trainer"}


def load_zones():
    try:
        return json.loads(ZONES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_ZONES

app = FastAPI(title="PTCG Vision")


# ---------------------------------------------------------------- 追蹤：同一張卡沒移動就不用重算，並多格投票
class Tracker:
    def __init__(self):
        self.tracks: list[dict] = []
        self.next_id = 1

    def update(self, frame, quads, budget=3):
        now = time.time()
        diag = np.hypot(*frame.shape[:2])
        new = []
        for q in quads:
            c = q.mean(0)
            old = min(self.tracks, key=lambda t: np.linalg.norm(t["c"] - c), default=None)
            if old is not None and np.linalg.norm(old["c"] - c) < diag * 0.03 and not any(old is t for t in new):
                old.update(q=q, c=c, seen=now)
                new.append(old)
            else:
                new.append({"id": self.next_id, "q": q, "c": c, "seen": now, "res": None, "votes": {}, "t": 0.0, "n": 0, "ref": None})
                self.next_id += 1
        # 卡面外觀改變（例如把能量卡疊到寶可夢上，位置幾乎沒動）→ 視為換了一張卡，重新辨識
        for t in new:
            t["img"] = warp_card(frame, t["q"])
            h = dhash(t["img"])
            if t["ref"] is not None and np.count_nonzero(h != t["ref"]) / h.size > 0.25:
                t.update(res=None, votes={}, n=0, t=0.0, ref=None)
            t["h"] = h
        # 先辨識新出現、還沒認出的卡；同一張卡每 0.5 秒最多重算一次
        todo = sorted(new, key=lambda t: (t["n"] >= 3, t["t"]))
        done = 0
        for t in todo:
            if done >= budget:
                break
            if t["res"] is not None and now - t["t"] < 0.5:
                continue
            votes = t["votes"]
            settled = t["n"] >= 3 and votes and max(votes.values()) >= 0.75 * sum(votes.values()) and max(votes, key=votes.get)
            if settled and now - t["t"] < 2.0:      # 已經穩定認出：2 秒複查一次就好，把運算留給新放上桌的卡
                continue
            res = db.identify(t["img"])
            t.update(res=res, t=now, n=t["n"] + 1, ref=t["h"])
            key = res.entry.name if res.entry else None
            t["votes"][key] = t["votes"].get(key, 0) + (0.3 if key is None else 1 + res.score)
            done += 1
        self.tracks = new
        return new


def occupancy(crop):
    """區域裡「亮的東西」佔多少比例（桌墊是暗的；卡片、手是亮的）"""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    return float(np.count_nonzero((hsv[..., 2] > 110) | (hsv[..., 1] > 90))) / max(1, crop.shape[0] * crop.shape[1])


def zone_crop(frame, z, pad=0.08):
    H, W = frame.shape[:2]
    x0 = int(max(0, (z["x"] - z["w"] * pad) * W)); x1 = int(min(W, (z["x"] + z["w"] * (1 + pad)) * W))
    y0 = int(max(0, (z["y"] - z["h"] * pad) * H)); y1 = int(min(H, (z["y"] + z["h"] * (1 + pad)) * H))
    return frame[y0:y1, x0:x1], (x0, y0)


def center_rule(zones, zid, frame, offset):
    """區域影像裡的一點 → 換回整張畫面的座標 → 是否屬於這一格（跟追蹤卡片用同一套規則）"""
    H, W = frame.shape[:2]
    ox, oy = offset
    return lambda cx, cy: zone_of(zones, (cx + ox) / W, (cy + oy) / H) == zid


class ZoneSense:
    """每個區域：判斷「空的 / 有東西」，以及在找不到完整卡片輪廓時（疊歪、被手擋）找出最上面那張"""

    def __init__(self):
        self.base = {}      # 空格時的亮度比例（持續取最小值）
        self.ref = {}       # 上一次穩定時的區域畫面（拿來跟「新疊上去的卡」比）
        self.ref_t = {}
        self.cache = {}     # 區域比對結果快取 zone -> (時間, 卡名)

    def occupied(self, zid, crop):
        occ = occupancy(crop)
        # 空格基準：取看過的最小值，但最多 0.08（桌墊是暗的）。
        # 否則開局時卡已經在桌上，就會把「有卡」當成空格的樣子，之後輪廓一抓不到就誤判成空。
        b = min(self.base.get(zid, 0.08), occ, 0.08)
        self.base[zid] = b
        return occ - b > 0.10

    def region_name(self, zid, crop, now, accept=None):
        hit = self.cache.get(zid)
        if hit and now - hit[0] < 0.4:
            return hit[1]
        name = None
        q = quad_from_change(self.ref.get(zid), crop)
        if q is not None and (accept is None or accept(*q.mean(0))):
            r = db.identify(warp_card(crop, q))
            name = r.entry.name if r.entry else None
        if name is None:
            r, _ = db.identify_region(crop, accept)
            name = r.entry.name if r.entry else None
        self.cache[zid] = (now, name)
        return name

    def remember(self, zid, crop, now):
        if now - self.ref_t.get(zid, 0) > 0.5:
            self.ref[zid] = crop.copy()
            self.ref_t[zid] = now


def track_json(t):
    votes = t["votes"]
    name = max(votes, key=votes.get) if votes else None
    entry = next((e for e in db.entries if e.name == name), None) if name else None
    res = t["res"]
    return {
        "id": t["id"],
        "quad": t["q"].round(1).tolist(),
        "name": name,
        "kind": entry.kind if entry else None,
        "deck": "、".join(db.decks_of(name)) if name else None,
        "score": round(res.score, 2) if res and res.entry and res.entry.name == name else (0.5 if name else 0),
        "method": res.method if res else None,
        "detail": res.detail if res else "",
    }


def jpeg_b64(img, q=85):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return "data:image/jpeg;base64," + base64.b64encode(buf).decode()


def observe_board(frame, quads, zones, tracker, sense, board, now):
    """對戰墊模式的一格畫面：每個區域看到什麼 → 交給 Board 判斷動作。回傳 (各區觀察, 追蹤中的卡, 新動作)"""
    tracks = tracker.update(frame, quads)
    H, W = frame.shape[:2]
    best = {}
    for t in tracks:
        z = zone_of(zones, t["c"][0] / W, t["c"][1] / H)
        if not z:
            continue
        votes = t["votes"]
        name = max(votes, key=votes.get) if votes else None
        o = name if name else UNKNOWN          # 還沒認出 / 認不出 → 維持原判斷
        area = cv2.contourArea(t["q"])
        if z not in best or area > best[z][0]:
            best[z] = (area, o)
    obs, budget = {}, 2
    for z in zones:
        zid = z["id"]
        crop, off = zone_crop(frame, z)
        occupied = sense.occupied(zid, crop)
        o = best.get(zid, (0, None))[1]
        if o is None or o == UNKNOWN:
            if not occupied and o is None:
                o = None                                       # 真的空了
            elif zid in SLOTS_AND_TRAINER and budget > 0:
                budget -= 1                                    # 疊歪／被擋：直接在區域裡找最上面那張
                o = sense.region_name(zid, crop, now, center_rule(zones, zid, frame, off)) or UNKNOWN
            else:
                o = UNKNOWN                                    # 有東西但看不清楚 → 維持原判斷
        obs[zid] = o
        w = board.watches.get(zid)
        if (o is None and not occupied) or (o not in (None, UNKNOWN) and w and w.committed == o and zid in best):
            sense.remember(zid, crop, now)                     # 記住穩定時的樣子
    return obs, tracks, board.update(obs, now)


def run_cpu(game):
    """輪到電腦就讓電腦下完（電腦回合結束後可能又因為氣絕流程輪到電腦，最多跑幾次）"""
    if game is None:
        return
    for _ in range(4):
        if game.phase == "turn" and game.current == 1:
            game.ai.take_turn(game, 1)
        else:
            break


def new_game(board, cpu_deck):
    decks = load_decks(BASE / "data")
    if cpu_deck not in decks:
        cpu_deck = next(iter(decks))
    you_deck = next((d for d in decks if d != cpu_deck), cpu_deck)
    g = Game(book, decks, you_deck, cpu_deck, p0_physical=True, board=board)
    g.ai = AI()
    g.say(f"新對戰：你用「{you_deck}」，電腦用「{cpu_deck}」", "turn")
    mull = g.digital_setup(1, g.ai)
    g.need("把牌組洗好，抽 7 張當手牌")
    g.need("手牌沒有基礎寶可夢：給電腦看一下、洗回去重抽，並按「我重抽了」（電腦可以多抽 1 張）")
    g.need("選 1 隻基礎寶可夢放到戰鬥場，想放的話再放最多 5 隻到備戰區（正面朝上，相機才看得到）")
    g.need("從牌庫上方拿 6 張卡，背面朝上放旁邊當獎賞卡")
    if mull:
        g.need(f"電腦重抽了 {mull} 次 → 你可以從牌庫多抽 {mull} 張")
    g.need("擺好之後按「開始對戰」")
    return g


# ---------------------------------------------------------------- WebSocket：即時畫面
@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    tracker = Tracker()
    mode = "recognize"
    last_card = None          # 註冊模式：目前畫面中最大的那張卡（拉正後）
    board = Board(book)       # 對戰墊模式：場面狀態
    zones = load_zones()
    sense = ZoneSense()
    game = None               # 對戰模式：規則引擎（真人 vs 電腦）
    sent = {"seq": None}      # 已經把哪一條紀錄的場面狀態傳給瀏覽器了（之後只傳新的，省流量）

    async def send_game():
        await sock.send_json({"type": "game", "game": game.snapshot(since=sent["seq"]) if game else None, "board": board.snapshot()})
        if game:
            sent["seq"] = game.seq
    try:
        while True:
            msg = await sock.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text"):
                cmd = json.loads(msg["text"])
                if cmd.get("type") == "mode":
                    mode = cmd["mode"]
                    tracker = Tracker()
                elif cmd.get("type") == "zones":
                    zones = load_zones()
                elif cmd.get("type") == "board_reset":
                    board.reset()
                    tracker = Tracker()
                    sense = ZoneSense()
                    await sock.send_json({"type": "board", "board": board.snapshot(), "events": []})
                elif cmd.get("type") == "board_undo":
                    ev = board.undo(time.time())
                    await sock.send_json({"type": "board", "board": board.snapshot(), "events": ev})
                    if game is not None:
                        await send_game()
                elif cmd.get("type") == "board_resolve":
                    ev = board.resolve(cmd.get("picks") or [], time.time())
                    if game is not None:
                        game.on_board_events(ev)
                    await sock.send_json({"type": "board", "board": board.snapshot(), "events": ev})
                    if game is not None:
                        await send_game()
                elif cmd.get("type", "").startswith("game_"):
                    t = cmd["type"]
                    if t == "game_new":
                        sent["seq"] = None
                        board.reset()
                        tracker, sense = Tracker(), ZoneSense()
                        game = new_game(board, cmd.get("cpu") or "")
                    elif game is None:
                        await sock.send_json({"type": "error", "msg": "請先按「開始新對戰」"})
                        continue
                    elif t == "game_start":
                        if game.phase != "setup":
                            pass
                        elif not game.sides[0].active():
                            await sock.send_json({"type": "error", "msg": "戰鬥場還沒有你的寶可夢（相機要看得到）"})
                            continue
                        else:
                            game.todo = []
                            first = cmd.get("first")
                            game.start(first if first in (0, 1) else None)
                    elif t == "game_mulligan":
                        game.human_mulligan()
                    elif t == "game_attack":
                        game.human_attack(int(cmd.get("index", 0)))
                    elif t == "game_end_turn":
                        if game.phase == "turn" and game.current == 0:
                            game.say("你結束了回合", "info", 0)
                            game.end_turn(0)
                    elif t == "game_choose":
                        game.resolve_choice(cmd.get("pick"))
                    elif t == "game_stadium":
                        game.human_use_stadium()
                    elif t == "game_deckout":
                        game.human_deck_out()
                    elif t == "game_todo":
                        game.todo_done(int(cmd.get("index", -1)))
                    elif t == "game_quit":
                        game = None
                    run_cpu(game)
                    await send_game()
                elif cmd.get("type") == "register":
                    if last_card is None:
                        await sock.send_json({"type": "error", "msg": "畫面中沒有偵測到卡片"})
                        continue
                    name = (cmd.get("name") or "").strip()
                    if not name:
                        await sock.send_json({"type": "error", "msg": "請輸入卡名"})
                        continue
                    kind = cmd.get("kind") if cmd.get("kind") in KINDS else guess_kind(name)
                    deck = (cmd.get("deck") or "").strip()
                    qty = max(1, int(cmd.get("qty") or 1))
                    known = name in db.names()
                    async with db_lock:
                        if known and not cmd.get("photo"):
                            # 已經註冊過的卡（例如另一副牌組也有的先機球）：只加進這副牌組，不用多存照片
                            await asyncio.to_thread(db.set_qty, deck, name, qty)
                        else:
                            await asyncio.to_thread(db.add, last_card, name, kind, deck, qty)
                    await sock.send_json({"type": "registered", "name": name, "deck": deck, "qty": qty,
                                          "photo": (not known) or bool(cmd.get("photo")), "total": len(db.names())})
                continue

            data = msg.get("bytes")
            if not data:
                continue
            t0 = time.time()
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue

            def work():
                quads = find_cards(frame)
                out = {"type": "frame", "w": frame.shape[1], "h": frame.shape[0], "mode": mode}
                if mode == "recognize":
                    out["cards"] = [track_json(t) for t in tracker.update(frame, quads)]
                    return out, None
                if mode in ("board", "game"):
                    H, W = frame.shape[:2]
                    now = time.time()
                    obs, tracks, events = observe_board(frame, quads, zones, tracker, sense, board, now)
                    if game is not None and mode == "game":
                        game.on_board_events(events, now)
                        run_cpu(game)
                        out["game"] = game.snapshot(since=sent["seq"])
                        sent["seq"] = game.seq
                    out["events"] = events
                    out["obs"] = obs
                    out["board"] = board.snapshot()
                    out["cards"] = [dict(track_json(t), zone=zone_of(zones, t["c"][0] / W, t["c"][1] / H)) for t in tracks]
                    out["zones"] = zones
                    return out, None
                big = max(quads, key=lambda q: cv2.contourArea(q)) if quads else None
                out["cards"] = [{"quad": q.round(1).tolist(), "big": q is big} for q in quads]
                card = warp_card(frame, big) if big is not None else None
                if card is not None:
                    out["preview"] = jpeg_b64(cv2.resize(card, (CARD_W // 2 * 2, CARD_H // 2 * 2)), 80)
                    # 順便告訴使用者：這張是不是已經註冊過了
                    r = db.identify(card)
                    out["already"] = r.entry.name if r.entry else None
                    out["alreadyDecks"] = db.decks_of(r.entry.name) if r.entry else {}
                return out, card

            async with db_lock:
                out, card = await asyncio.to_thread(work)
            if mode == "register":
                last_card = card
            out["ms"] = int((time.time() - t0) * 1000)
            await sock.send_json(out)
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------- 卡片資料 API
@app.get("/api/cards")
async def cards():
    order = list(KINDS)
    by = {}
    for e in db.entries:
        d = by.setdefault(e.name, {"name": e.name, "kind": e.kind, "kindName": KINDS.get(e.kind, e.kind),
                                   "group": KIND_GROUP.get(e.kind, ""), "photos": [], "decks": db.decks_of(e.name)})
        d["photos"].append(f"/api/img/{e.img_file}")
    sort_key = lambda n: (order.index(by[n]["kind"]) if n in by and by[n]["kind"] in order else 9, n)
    decks = {}
    for dname, members in db.decks.items():
        groups = {}
        for n, q in members.items():
            g = KIND_GROUP.get(db.kind_of(n), "其他")
            groups[g] = groups.get(g, 0) + q
        decks[dname] = {"total": sum(members.values()), "groups": groups,
                        "cards": [{"name": n, "qty": members[n]} for n in sorted(members, key=sort_key)]}
    loose = sorted([n for n in by if not by[n]["decks"]], key=sort_key)    # 已註冊、但還沒放進任何牌組
    return {"cards": sorted(by.values(), key=lambda d: sort_key(d["name"])), "decks": decks, "loose": loose, "kinds": KINDS}


@app.get("/api/img/{fn}")
async def img(fn: str):
    p = Path(DB_DIR) / "images" / Path(fn).name
    return FileResponse(p) if p.exists() else Response(status_code=404)


@app.post("/api/cards/update")
async def update_card(name: str = Form(...), new_name: str = Form(""), kind: str = Form("")):
    """改種類（全系統共用）或改卡名"""
    async with db_lock:
        if kind in KINDS:
            db.set_kind(name, kind)
        if new_name.strip() and new_name.strip() != name:
            db.rename(name, new_name.strip())
    return {"ok": True}


@app.post("/api/decks/set")
async def deck_set(deck: str = Form(...), name: str = Form(...), qty: int = Form(...)):
    """設定某副牌組裡某張卡的張數（0 = 從這副牌組拿掉，卡片本身保留）"""
    async with db_lock:
        db.set_qty(deck, name, max(0, min(60, qty)))
    return {"ok": True}


@app.post("/api/cards/delete")
async def delete_card(name: str = Form(...)):
    async with db_lock:
        n = db.remove_name(name)
    return {"ok": True, "removed": n}


@app.post("/api/import")
async def import_photos(files: list[UploadFile] = File(...), deck: str = Form("牌組A"), kind: str = Form("pokemon")):
    """用照片批次註冊：檔名 = 卡名（火紅不倒翁.jpg、火紅不倒翁_2.jpg）"""
    results = []
    for f in files:
        raw = await f.read()
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        name = Path(f.filename or "").stem.split("_")[0].strip()
        if img is None or not name:
            results.append({"file": f.filename, "ok": False, "msg": "讀不到圖片"})
            continue
        quads = find_cards(img, min_area=0.01, max_area=0.98)
        card = warp_card(img, max(quads, key=lambda q: cv2.contourArea(q))) if quads else cv2.resize(img, (CARD_W, CARD_H))
        k = guess_kind(name, kind if kind in KINDS else "pokemon")
        async with db_lock:
            await asyncio.to_thread(db.add, card, name, k, "", 1)
            if deck and name not in db.decks.get(deck, {}):
                db.set_qty(deck, name, 1)
        results.append({"file": f.filename, "ok": True, "name": name, "cropped": bool(quads)})
    return {"results": results}


@app.get("/api/decklist.csv")
async def decklist():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["牌組", "卡名", "種類", "張數", "註冊照片數"])
    for r in db.decklist():
        w.writerow([r["deck"], r["name"], KINDS.get(r["kind"], r["kind"]), r["qty"], r["photos"]])
    data = ("﻿" + buf.getvalue()).encode("utf-8")      # BOM：Excel 開中文不亂碼
    return Response(data, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=decklist.csv"})


@app.get("/api/game/cards")
async def game_cards():
    """卡片資料（放大檢視時顯示招式、效果文字）"""
    return {"cards": list(book.cards.values())}


@app.get("/api/game/decks")
async def game_decks():
    return {"decks": list(load_decks(BASE / "data").keys())}


@app.get("/api/zones")
async def get_zones():
    return {"zones": load_zones(), "default": DEFAULT_ZONES}


@app.post("/api/zones")
async def set_zones(body: dict):
    zones = body.get("zones") or DEFAULT_ZONES
    ZONES_FILE.write_text(json.dumps(zones, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True}


@app.get("/api/health")
async def health():
    return JSONResponse({"ok": True, "cards": len(db.names()), "photos": len(db.entries)})


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
