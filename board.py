"""
第 2 階段：對戰墊動作判斷

相機每格畫面告訴我們「每個區域裡現在看到哪張卡」，這裡把它轉成遊戲動作：
  放置寶可夢、進化、貼能量、貼道具、使用訓練家卡、寶可夢換位（撤退／交替）、寶可夢離場（氣絕或收回）

操作約定（給玩家的）：
  - 寶可夢：放進戰鬥場或備戰區
  - 進化：進化卡直接疊在原本的寶可夢上
  - 能量／寶可夢道具：先疊在要附加的寶可夢「上面」讓相機看一下（約 1 秒），再塞到寶可夢下面
  - 訓練家卡（物品、支援者、競技場）：放進訓練家出牌區
防誤判：
  - 卡片要穩定停留 ON_DELAY 秒才算「出現」，區域要空掉 OFF_DELAY 秒才算「離開」
  - 手在移動、卡片疊歪偵測不到時（看到不明物體）一律維持原判斷，不產生動作
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

ON_DELAY = 0.8      # 新卡穩定出現多久才算數（秒）
OFF_DELAY = 1.5     # 區域空掉多久才算卡片離開
MOVE_WINDOW = 4.0   # 寶可夢離開某格後，幾秒內出現在另一格就算「換位」而不是離場
RECONCILE = 2.5     # 相機穩定看到的跟紀錄不一樣超過這麼久 → 以相機為準自動校正
UNKNOWN = "?"       # 有卡但認不出來

SLOTS = ["active", "bench1", "bench2", "bench3", "bench4", "bench5"]
SLOT_NAMES = {"active": "戰鬥場", **{f"bench{i}": f"備戰{i}" for i in range(1, 6)}}

# 預設區域（0~1 的相對座標，相機畫面左上為原點）：兩張 A4 橫放並排大約佔滿畫面中央
DEFAULT_ZONES = [
    {"id": "active", "label": "戰鬥場", "x": 0.42, "y": 0.06, "w": 0.16, "h": 0.40},
    {"id": "trainer", "label": "訓練家出牌區", "x": 0.64, "y": 0.06, "w": 0.16, "h": 0.40},
    {"id": "discard", "label": "棄牌區", "x": 0.84, "y": 0.06, "w": 0.13, "h": 0.40},
    *[{"id": f"bench{i + 1}", "label": f"備戰{i + 1}", "x": round(0.05 + i * 0.185, 3), "y": 0.54, "w": 0.165, "h": 0.40} for i in range(5)],
]

ENERGY_KINDS = {"energy", "special_energy"}
TRAINER_KINDS = {"item", "supporter", "stadium", "tool", "trainer"}
KIND_NAMES = {"item": "物品", "supporter": "支援者", "stadium": "競技場", "tool": "寶可夢道具", "trainer": "訓練家"}


class CardBook:
    """卡片資料（data/cards.json）。辨識出來的名字若是舊的註冊名，用 name_fixes 轉成正式卡名。"""

    def __init__(self, folder: str | Path):
        folder = Path(folder)
        self.cards = {c["name"]: c for c in json.loads((folder / "cards.json").read_text(encoding="utf-8"))}
        p = folder / "name_fixes.json"
        self.fixes = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def norm(self, name):
        if name in (None, UNKNOWN):
            return name
        return self.fixes.get(name, name)

    def get(self, name):
        return self.cards.get(self.norm(name))

    def kind(self, name):
        c = self.get(name)
        return c["kind"] if c else None


@dataclass
class ZoneWatch:
    """一個區域的去抖動：只有穩定一段時間的變化才算數"""
    committed: str | None = None
    cand: str | None = None
    since: float = 0.0
    at: float = 0.0          # 目前這個判斷是什麼時候確定的

    def observe(self, obs, now):
        if obs == UNKNOWN:                    # 看不清楚：維持現狀
            return None
        if obs != self.cand:
            self.cand, self.since = obs, now
        need = OFF_DELAY if obs is None else ON_DELAY
        if self.cand != self.committed and now - self.since >= need:
            old, self.committed = self.committed, self.cand
            self.at = now
            return old, self.committed
        return None


def empty_slot():
    """一格的狀態。damage/status/flags/turn/evolved 由規則引擎（game.py）使用，會跟著寶可夢一起移動"""
    return {"pokemon": [], "energy": [], "tool": [], "damage": 0, "status": [], "flags": {}, "turn": 0, "evolved": -1}


@dataclass
class Board:
    book: CardBook
    slots: dict = field(default_factory=lambda: {s: empty_slot() for s in SLOTS})
    watches: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)      # 剛離開的寶可夢 slot -> (狀態, 時間)
    events: list = field(default_factory=list)
    trainer_last: str | None = None
    choice: dict | None = None                       # 等玩家選擇（例如撤退要丟哪幾張能量）
    free_switch_until: float = 0.0                   # 剛用了寶可夢交替：這段時間內換位不用付撤退費用
    history: list = field(default_factory=list)      # 復原用
    no_fix: dict = field(default_factory=dict)       # 剛復原的格子：相機還是看到同一張卡時，不要自動校正回去

    # ---------- 對外
    def _state(self):
        return copy.deepcopy((self.slots, self.trainer_last, self.choice, self.free_switch_until, self.pending))

    def update(self, obs: dict, now: float) -> list[dict]:
        """obs: {zone_id: 卡名 | None | "?"}，回傳這次新產生的動作"""
        before = self._state()
        new = []
        # 先處理「離開」再處理「出現」，換位（兩格同時交換）才判斷得出來
        changes = []
        for zid, o in obs.items():
            w = self.watches.setdefault(zid, ZoneWatch())
            ch = w.observe(self.book.norm(o), now)
            if ch:
                changes.append((zid, *ch))
        changes.sort(key=lambda c: c[2] is not None)
        for zid, old, cur in changes:
            if zid in SLOTS:
                new += self._slot_change(zid, old, cur, now)
            elif zid == "trainer":
                new += self._trainer_change(old, cur, now)
        new += self._expire(now)
        new += self._reconcile(now)
        if new:
            self.history = (self.history + [before])[-20:]
        self.events += new
        return new

    def undo(self, now: float) -> list[dict]:
        """復原上一個動作（判斷錯誤時用）"""
        if not self.history:
            return []
        cur = self.slots
        self.slots, self.trainer_last, self.choice, self.free_switch_until, self.pending = self.history.pop()
        # 傷害、特殊狀態是規則引擎記的（不是相機動作），復原時保留現在的值
        for st in self.slots.values():
            if not st["pokemon"]:
                continue
            same = next((o for o in cur.values() if o["pokemon"] and o["pokemon"][0] == st["pokemon"][0]
                         and o["energy"] == st["energy"]), None) or \
                next((o for o in cur.values() if o["pokemon"] and o["pokemon"][0] == st["pokemon"][0]), None)
            if same:
                for k in ("damage", "status", "flags", "ko"):
                    if k in same:
                        st[k] = copy.deepcopy(same[k])
        self.no_fix = {s: w.committed for s, w in self.watches.items()}
        ev = [self._ev(now, "undo", "↶ 已復原上一個動作")]
        self.events += ev
        return ev

    def resolve(self, picks: list[int], now: float) -> list[dict]:
        """玩家選好撤退要丟棄的能量（picks = 能量在清單中的位置）"""
        c = self.choice
        if not c or len(set(picks)) != c["count"]:
            return []
        st = self.slots[c["slot"]]
        dropped = [st["energy"][i] for i in sorted(set(picks), reverse=True) if 0 <= i < len(st["energy"])]
        if len(dropped) != c["count"]:
            return []
        self.history = (self.history + [self._state()])[-20:]
        for i in sorted(set(picks), reverse=True):
            st["energy"].pop(i)
        self.choice = None
        ev = [self._ev(now, "retreat_cost", f"{c['pokemon']} 撤退，丟棄 {'、'.join(dropped)}（請把這些能量放到棄牌區）", slot=c["slot"], cards=dropped)]
        self.events += ev
        return ev

    def snapshot(self):
        def slot_view(s):
            st = self.slots[s]
            top = st["pokemon"][-1] if st["pokemon"] else None
            c = self.book.get(top) if top else None
            return {"slot": s, "label": SLOT_NAMES[s], "pokemon": top, "chain": st["pokemon"], "energy": st["energy"],
                    "tool": st["tool"], "hp": c["hp"] if c else None, "type": c["type"] if c else None}
        return {"slots": [slot_view(s) for s in SLOTS], "trainer": self.trainer_last, "events": self.events[-30:],
                "choice": self.choice, "can_undo": bool(self.history)}

    def reset(self):
        self.slots = {s: empty_slot() for s in SLOTS}
        self.watches, self.pending, self.events, self.trainer_last = {}, {}, [], None
        self.choice, self.free_switch_until, self.history, self.no_fix = None, 0.0, [], {}

    # ---------- 內部
    def _ev(self, now, type_, text, warn=False, **kw):
        return {"t": round(now, 2), "type": type_, "text": text, "warn": warn, **kw}

    def _slot_change(self, sid, old, cur, now):
        st = self.slots[sid]
        top = st["pokemon"][-1] if st["pokemon"] else None
        where = SLOT_NAMES[sid]
        if cur is None:                                   # 區域空了
            if top:
                self.pending[sid] = (copy.deepcopy(st), now)
                self.slots[sid] = empty_slot()
            return []
        kind = self.book.kind(cur)
        if kind is None:
            return [self._ev(now, "unknown", f"{where}出現了資料庫裡沒有的卡「{cur}」", True, slot=sid, card=cur)]

        if top is None and sid in self.pending:
            snap, _ = self.pending[sid]
            ptop = snap["pokemon"][-1]
            c = self.book.get(cur) or {}
            if not snap.get("ko") and (kind in ENERGY_KINDS or kind == "tool" or cur == ptop or c.get("evolves_from") == ptop):
                # 剛剛疊卡時被誤判成「空了」：把原本的寶可夢接回來，再照常處理
                del self.pending[sid]
                self.slots[sid] = snap
                return self._slot_change(sid, old, cur, now)

        if kind in ENERGY_KINDS:
            if not top:
                return [self._ev(now, "warn", f"{where}沒有寶可夢，不能貼「{cur}」", True, slot=sid, card=cur)]
            st["energy"].append(cur)
            return [self._ev(now, "attach_energy", f"把「{cur}」貼給{where}的{top}", slot=sid, card=cur, target=top)]

        if kind == "tool":
            if not top:
                return [self._ev(now, "warn", f"{where}沒有寶可夢，不能掛「{cur}」", True, slot=sid, card=cur)]
            st["tool"].append(cur)
            return [self._ev(now, "attach_tool", f"把「{cur}」掛在{where}的{top}身上", slot=sid, card=cur, target=top)]

        if kind in TRAINER_KINDS:
            return [self._ev(now, "warn", f"「{cur}」是{KIND_NAMES.get(kind, '訓練家')}卡，請放到訓練家出牌區", True, slot=sid, card=cur)]

        # ---- 寶可夢
        card = self.book.get(cur)
        if top is None:
            src = next((s for s, (snap, t) in self.pending.items() if snap["pokemon"] and snap["pokemon"][-1] == cur), None)
            if src is not None:                           # 從別格移過來：換位
                snap, _ = self.pending.pop(src)
                src = src.rstrip("~")                     # "~" = 自動校正時暫存的舊紀錄
                self.slots[sid] = snap
                return self._after_move(self._ev(now, "move", f"{cur} 從{SLOT_NAMES[src]}移到{where}", slot=sid, card=cur, src=src), now)
            # 原本那格已經看起來空了、但還沒過 OFF_DELAY（例如快速把備戰寶可夢拿到戰鬥場）→ 也算換位
            src = next((s for s in SLOTS if s != sid and self.slots[s]["pokemon"] and self.slots[s]["pokemon"][-1] == cur
                        and self.watches.get(s, ZoneWatch()).cand is None), None)
            if src is not None:
                self.slots[sid], self.slots[src] = self.slots[src], empty_slot()
                return self._after_move(self._ev(now, "move", f"{cur} 從{SLOT_NAMES[src]}移到{where}", slot=sid, card=cur, src=src), now)
            st["pokemon"] = [cur]
            st.update(damage=0, status=[], flags={}, evolved=-1)
            if card["stage"] != "basic":
                return [self._ev(now, "warn", f"{cur} 是進化寶可夢，不能直接放到場上", True, slot=sid, card=cur)]
            return [self._ev(now, "place", f"把 {cur} 放到{where}", slot=sid, card=cur)]
        if cur == top or cur in st["pokemon"]:            # 能量塞回去後又看到原本的寶可夢
            return []
        if card.get("evolves_from") == top:
            st["pokemon"].append(cur)
            return [self._ev(now, "evolve", f"{where}的 {top} 進化成 {cur}", slot=sid, card=cur, target=top)]
        # 快速交換：另一格原本的寶可夢被拿過來放在這裡，而且那一格現在已經看不到牠
        src = next((s for s in SLOTS if s != sid and self.slots[s]["pokemon"] and self.slots[s]["pokemon"][-1] == cur
                    and self.watches.get(s, ZoneWatch()).cand != cur), None)
        if src is not None:
            self.pending[sid] = (copy.deepcopy(st), now)          # 這格原本的寶可夢正在移去別的地方
            self.slots[sid], self.slots[src] = self.slots[src], empty_slot()
            return self._after_move(self._ev(now, "move", f"{cur} 從{SLOT_NAMES[src]}移到{where}", slot=sid, card=cur, src=src), now)
        return [self._ev(now, "warn", f"{cur} 不能從 {top} 進化（{where}）", True, slot=sid, card=cur, target=top)]

    def _reconcile(self, now):
        """最後一道保險：相機穩定看到的寶可夢跟紀錄不一樣（各種原因造成不同步）→ 以相機為準"""
        out = []
        if self.choice:
            return out
        for sid in SLOTS:
            w = self.watches.get(sid)
            if not w or w.committed == UNKNOWN or now - w.at < RECONCILE or sid in self.pending:
                continue
            if self.no_fix.get(sid, "<none>") == w.committed:     # 玩家剛按了復原
                continue
            self.no_fix.pop(sid, None)
            st = self.slots[sid]
            top = st["pokemon"][-1] if st["pokemon"] else None
            cur = w.committed
            where = SLOT_NAMES[sid]
            if cur is None:
                if top:                                          # 相機看到空的，紀錄卻有寶可夢
                    self.pending[sid] = (copy.deepcopy(st), now)
                    self.slots[sid] = empty_slot()
                continue
            if self.book.kind(cur) != "pokemon" or top == cur or cur in st["pokemon"]:
                continue
            card = self.book.get(cur)
            if top and card.get("evolves_from") == top:          # 漏掉的進化
                st["pokemon"].append(cur)
                out.append(self._ev(now, "fix", f"自動校正：{where}的 {top} 進化成 {cur}", slot=sid, card=cur))
                continue
            stale = copy.deepcopy(st) if top else None
            src = next((s for s in SLOTS if s != sid and self.slots[s]["pokemon"] and self.slots[s]["pokemon"][-1] == cur
                        and (self.watches.get(s) is None or self.watches[s].committed != cur)), None)
            psrc = next((s for s, (snap, t) in self.pending.items() if snap["pokemon"] and snap["pokemon"][-1] == cur), None)
            if src is not None:                                  # 紀錄說牠在別格，但那格其實已經不是牠
                self.slots[sid], self.slots[src] = self.slots[src], empty_slot()
            elif psrc is not None:
                self.slots[sid] = self.pending.pop(psrc)[0]
            else:
                self.slots[sid] = dict(empty_slot(), pokemon=[cur])
            if stale:
                self.pending[sid + "~"] = (stale, now)           # 原本紀錄的那隻：之後出現在別格就接上，沒出現就算離場
            out.append(self._ev(now, "fix", f"自動校正：{where} 是 {cur}", slot=sid, card=cur))
        return out

    def _after_move(self, ev, now):
        """離開戰鬥場的寶可夢要付撤退費用（剛用寶可夢交替就免）"""
        out = [ev]
        if ev.get("src") != "active" or ev["slot"] == "active":
            return out
        sid, name = ev["slot"], ev["card"]
        if now <= self.free_switch_until:
            self.free_switch_until = 0.0
            ev["text"] += "（寶可夢交替，不用撤退費用）"
            return out
        cost = (self.book.get(name) or {}).get("retreat", 0)
        en = self.slots[sid]["energy"]
        if cost == 0:
            return out
        if len(en) < cost:
            out.append(self._ev(now, "warn", f"{name} 撤退需要 {cost} 張能量，身上只有 {len(en)} 張", True, slot=sid))
            return out
        if len(en) == cost or len(set(en)) == 1:
            dropped = en[:cost]
            del en[:cost]
            out.append(self._ev(now, "retreat_cost", f"{name} 撤退，丟棄 {'、'.join(dropped)}（請把這些能量放到棄牌區）", slot=sid, cards=dropped))
            return out
        self.choice = {"type": "retreat_cost", "slot": sid, "pokemon": name, "count": cost, "options": list(en)}
        out.append(self._ev(now, "choice", f"{name} 撤退需要丟棄 {cost} 張能量，請在右側選擇要丟哪幾張", slot=sid))
        return out

    def _trainer_change(self, old, cur, now):
        if cur is None:
            return []
        kind = self.book.kind(cur)
        if kind not in TRAINER_KINDS:
            return [self._ev(now, "warn", f"「{cur}」不是訓練家卡", True, card=cur)]
        self.trainer_last = cur
        c = self.book.get(cur)
        if cur == "寶可夢交替":
            self.free_switch_until = now + 30
        return [self._ev(now, "play_trainer", f"使用{KIND_NAMES.get(kind, '')}「{cur}」", card=cur, kind=kind, effect=c.get("text", ""))]

    def _expire(self, now):
        out = []
        for sid, (snap, t) in list(self.pending.items()):
            if now - t >= MOVE_WINDOW:
                del self.pending[sid]
                top = snap["pokemon"][-1]
                extra = f"（附帶 {len(snap['energy'])} 張能量）" if snap["energy"] else ""
                out.append(self._ev(now, "leave", f"{top} 離開了{SLOT_NAMES[sid.rstrip('~')]}{extra}", slot=sid.rstrip("~"), card=top,
                                    chain=snap["pokemon"], energy=snap["energy"], tool=snap["tool"]))
        return out


def zone_of(zones, cx, cy, tol=0.04):
    """卡片中心點落在哪一區（座標已正規化 0~1）。落在兩格之間的縫隙時，算最近的那一格。
    一張卡只會屬於一格。"""
    best, bd = None, tol
    for z in zones:
        dx = max(z["x"] - cx, 0, cx - (z["x"] + z["w"]))
        dy = max(z["y"] - cy, 0, cy - (z["y"] + z["h"]))
        d = (dx * dx + dy * dy) ** 0.5
        if d == 0:
            return z["id"]
        if d < bd:
            best, bd = z["id"], d
    return best
