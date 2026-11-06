"""
第 3 階段：規則引擎（真人 vs 電腦，或電腦 vs 電腦自我對戰）

兩邊都用同一套「格子」資料（跟 board.py 一樣）：
  {"pokemon": [進化鏈], "energy": [...], "tool": [...], "damage": 0, "status": [...], "flags": {...}, "turn": 放上場的回合, "evolved": 進化的回合}
真人那一邊（physical=True）的格子就是 Board 的格子，相機看到的動作會直接改它；
牌庫、手牌是實體的，系統看不到 → 需要真人做的事情會放進「待辦」(todo) 告訴他。
電腦那一邊（physical=False）全部是數位的。

回合編號 turn：1, 2, 3...（每個玩家的回合各算一次）。先攻是 turn 1。
"""
from __future__ import annotations

import copy
import json
import random
from collections import Counter
from pathlib import Path

from board import SLOTS, SLOT_NAMES, empty_slot

STATUS_NAMES = {"poisoned": "中毒", "burned": "灼傷", "asleep": "睡眠", "confused": "混亂", "paralyzed": "麻痺"}
ROTATING = {"asleep", "confused", "paralyzed"}          # 這三種互斥，新的會取代舊的
TYPE_NAMES = {"grass": "草", "fire": "火", "water": "水", "lightning": "雷", "psychic": "超", "fighting": "鬥",
              "darkness": "惡", "metal": "鋼", "dragon": "龍", "colorless": "無", "any": "全"}
BENCH = SLOTS[1:]


# ================================================================ 小工具
def top(slot):
    return slot["pokemon"][-1] if slot and slot.get("pokemon") else None


def can_pay(cost: list[str], provided: list[str]) -> bool:
    """能量夠不夠付招式：先付指定屬性（「全」可以當任何屬性），剩下的張數要 ≥ 無色需求"""
    pool = list(provided)
    for t in [c for c in cost if c != "colorless"]:
        if t in pool:
            pool.remove(t)
        elif "any" in pool:
            pool.remove("any")
        else:
            return False
    return len(pool) >= cost.count("colorless")


class Side:
    def __init__(self, name: str, deck_name: str, physical: bool, slots_getter=None):
        self.name = name
        self.deck_name = deck_name
        self.physical = physical
        self._slots = None if physical else {s: empty_slot() for s in SLOTS}
        self._getter = slots_getter
        self.deck: list[str] = []
        self.hand: list[str] = []
        self.discard: list[str] = []
        self.prizes: list[str] = []
        self.prize_count = 6              # 實體玩家：系統只記張數
        self.reset_turn_flags()
        self.ko_last_turn = False         # 上個對手回合自己的寶可夢有沒有氣絕（希巴、奇巴納用）
        self.ko_flag_pending = False
        self.hand_estimate = 5            # 實體玩家的手牌張數（系統看不到，模仿少女用估計值）

    def reset_turn_flags(self):
        self.energy_attached = 0
        self.supporter_played = False
        self.retreated = False
        self.stadium_used = False

    @property
    def slots(self):
        return self._getter() if self.physical else self._slots

    def prizes_left(self):
        return self.prize_count if self.physical else len(self.prizes)

    def in_play(self):
        return [(s, self.slots[s]) for s in SLOTS if top(self.slots[s]) and not self.slots[s].get("ko")]

    def active(self):
        a = self.slots["active"]
        return a if top(a) and not a.get("ko") else None

    def bench(self):
        return [(s, self.slots[s]) for s in BENCH if top(self.slots[s]) and not self.slots[s].get("ko")]

    def free_bench(self):
        return [s for s in BENCH if not top(self.slots[s])]


# ================================================================ 遊戲
class Game:
    def __init__(self, book, decks: dict, p0_deck: str, p1_deck: str, p0_physical: bool = True,
                 board=None, seed=None, names=("你", "電腦")):
        self.book = book
        self.rng = random.Random(seed)
        self.board = board
        getter = (lambda: self.board.slots) if p0_physical else None
        self.sides = [Side(names[0], p0_deck, p0_physical, getter), Side(names[1], p1_deck, False)]
        for side in self.sides:
            if not side.physical:
                side.deck = [n for n, q in decks[side.deck_name].items() for _ in range(q)]
                self.rng.shuffle(side.deck)
        self.turn = 0
        self.current = None          # 0 / 1：現在是誰的回合
        self.first = None
        self.phase = "setup"         # setup → turn → (promote) → over
        self.stadium = None          # (卡名, 擁有者)
        self.winner = None
        self.log: list[dict] = []
        self.todo: list[str] = []    # 要真人做的實體動作
        self.choice = None           # 等真人選擇
        self.free_attach = 0         # 招式／卡片效果讓真人貼的能量（不算每回合 1 次）
        self.expect_bench = 0        # 招式／卡片效果讓真人從牌庫放到備戰區的寶可夢
        self.seq = 0
        self.ai = None               # 電腦的大腦（ai.AI），由外面設定
        self.pending_next = None

    # ---------------------------------------------------------------- 共用
    def other(self, i):
        return 1 - i

    def card(self, name):
        return self.book.get(name) or {}

    def say(self, text, kind="info", who=None, **fx):
        """寫一條紀錄。fx：給畫面播動畫用（攻擊、傷害、氣絕、出牌…）；state：這一刻的場面（畫面依序重播）"""
        self.seq += 1
        e = {"n": self.seq, "turn": self.turn, "text": text, "kind": kind, "who": who, "state": self.field_state()}
        if fx:
            e["fx"] = fx
        self.log.append(e)
        if len(self.log) > 400:
            self.log = self.log[-300:]

    def locate(self, slot):
        for i, side in enumerate(self.sides):
            for sid in SLOTS:
                if side.slots[sid] is slot:
                    return i, sid
        return None, None

    def compact(self, i, sid):
        sl = self.sides[i].slots[sid]
        name = top(sl)
        if not name:
            return None
        c = self.card(name)
        return {"name": name, "chain": list(sl["pokemon"]), "hp": c.get("hp", 0), "damage": sl.get("damage", 0),
                "energy": list(sl.get("energy", [])), "tool": list(sl.get("tool", [])),
                "status": [STATUS_NAMES[x] for x in sl.get("status", []) if x in STATUS_NAMES], "ko": bool(sl.get("ko"))}

    def field_state(self):
        out = {"turn": self.turn, "current": self.current, "phase": self.phase, "stadium": self.stadium[0] if self.stadium else None, "sides": []}
        for i, s in enumerate(self.sides):
            hidden = self.phase == "setup" and not s.physical
            out["sides"].append({
                "slots": {sid: (None if hidden else self.compact(i, sid)) for sid in SLOTS},
                "prizes": s.prizes_left(), "hand": None if s.physical else len(s.hand),
                "deck": None if s.physical else len(s.deck), "discard": None if s.physical else len(s.discard),
                "discard_top": None if s.physical or not s.discard else s.discard[-1]})
        return out

    def need(self, text):
        """要真人做的事（實體卡片）"""
        self.todo.append(text)
        self.say("👉 " + text, "todo", 0)

    def flip(self, why=""):
        h = self.rng.random() < 0.5
        self.say(f"🪙 {why}擲硬幣：{'正面' if h else '反面'}", "coin", fx="coin", heads=h)
        return h

    def provided(self, slot):
        out = []
        for e in slot.get("energy", []):
            p = self.card(e).get("provides") or ["colorless"]
            out.append(p[0])
        return out

    def hp(self, slot):
        return self.card(top(slot)).get("hp", 0)

    def remaining(self, slot):
        return self.hp(slot) - slot.get("damage", 0)

    def is_first_turn_of(self, i):
        return (self.turn == 1 and self.first == i) or (self.turn == 2 and self.first != i)

    def deck_names(self, i):
        return self.sides[i].deck

    # ---------------------------------------------------------------- 開局
    def draw(self, i, n=1):
        s = self.sides[i]
        got = []
        for _ in range(n):
            if not s.deck:
                break
            got.append(s.deck.pop(0))
        s.hand += got
        return got

    def digital_setup(self, i, ai):
        """電腦：抽 7 張（沒有基礎寶可夢就重抽，對手可以多抽）、放戰鬥場與備戰區、放 6 張獎賞卡"""
        s = self.sides[i]
        mull = 0
        while True:
            s.deck += s.hand
            s.hand = []
            self.rng.shuffle(s.deck)
            self.draw(i, 7)
            if any(self.card(c).get("stage") == "basic" for c in s.hand):
                break
            mull += 1
        if mull:
            self.say(f"{s.name} 手牌沒有基礎寶可夢，重抽了 {mull} 次", who=i)
        ai.setup(self, i)
        s.prizes = [s.deck.pop(0) for _ in range(6)]
        return mull

    def start(self, first: int | None = None):
        """雙方擺好之後開始。first=None 就擲硬幣決定先攻"""
        if first is None:
            first = 0 if self.flip("決定先攻：") else 1
        self.first = first
        self.say(f"{self.sides[first].name} 先攻", "turn")
        for side in self.sides:
            for _, sl in side.in_play():
                sl["turn"] = 0
        self.phase = "turn"
        self.begin_turn(first)

    # ---------------------------------------------------------------- 回合
    def begin_turn(self, i):
        self.turn += 1
        self.current = i
        s = self.sides[i]
        s.reset_turn_flags()
        self.free_attach = 0
        self.expect_bench = 0
        self.say(f"── 第 {self.turn} 回合：{s.name} ──", "turn", i)
        if s.physical:
            self.need("從牌庫抽 1 張卡")
            if self.is_first_turn_of(i) and self.first == i:
                self.say("先攻的第一回合不能攻擊，也不能使用支援者", "rule", i)
        else:
            if not s.deck:
                self.say(f"{s.name} 牌庫沒有卡可以抽了", "rule", i)
                return self.win(self.other(i), "對手牌庫抽完")
            self.draw(i)
            self.say(f"{s.name} 抽了 1 張卡（手牌 {len(s.hand)} 張）", who=i)

    def end_turn(self, i):
        """回合結束：寶可夢檢查（中毒、灼傷、睡眠）→ 氣絕 → 換對手"""
        if self.phase != "turn" or self.current != i:
            return
        if self.sides[i].physical:
            self.todo = []                       # 自己回合的待辦：回合結束就不用再提醒
            self.choice = None
        self.checkup()
        if self.phase == "over":
            return
        self.process_ko(attacker=None)
        if self.phase == "over":
            return
        nxt = self.other(i)
        # 下一位玩家：在剛結束的這個回合（對手的回合）裡，自己有沒有寶可夢氣絕
        self.sides[nxt].ko_last_turn = self.sides[nxt].ko_flag_pending
        for side in self.sides:
            side.ko_flag_pending = False
        self.after_ko_promote(nxt)

    def after_ko_promote(self, nxt):
        """氣絕後要換上新的戰鬥寶可夢；真人要自己把卡放上去，放好才繼續"""
        for j, side in enumerate(self.sides):
            if not side.active() and not side.physical:
                self.ai_promote(j)
        for side in self.sides:
            if not side.active() and side.physical and side.bench():
                self.phase = "promote"
                self.pending_next = nxt
                self.need("從備戰區選 1 隻寶可夢放到戰鬥場")
                return
        self.phase = "turn"
        self.begin_turn(nxt)

    def ai_promote(self, j):
        side = self.sides[j]
        bench = side.bench()
        if not bench:
            return
        sid, _ = max(bench, key=lambda b: (self.attack_ready_score(j, b[1]), self.remaining(b[1])))
        side.slots["active"], side.slots[sid] = side.slots[sid], empty_slot()
        self.say(f"{side.name} 把 {top(side.slots['active'])} 換上戰鬥場", who=j, fx="place", side=j, slot="active")

    def promote_done(self):
        """真人把新的寶可夢放到戰鬥場之後呼叫"""
        if self.phase == "promote" and self.sides[0].active():
            self.todo = [t for t in self.todo if "戰鬥場" not in t]
            self.phase = "turn"
            self.begin_turn(self.pending_next)

    def attack_ready_score(self, i, slot):
        c = self.card(top(slot))
        best = 0
        for a in c.get("attacks", []):
            if can_pay(a["cost"], self.provided(slot)):
                best = max(best, a["damage"] or 10)
        return best

    def win(self, i, why):
        if self.phase == "over":
            return
        self.phase = "over"
        self.winner = i
        self.say(f"🏆 {self.sides[i].name} 獲勝！（{why}）", "win", i)

    # ---------------------------------------------------------------- 狀態
    def apply_status(self, slot, status, poison_counters=None):
        name = top(slot)
        c = self.card(name)
        if c.get("type") == "grass" and "芳香草能量" in slot.get("energy", []):
            self.say(f"{name} 有芳香草能量，不會陷入特殊狀態", "rule")
            return
        st = slot.setdefault("status", [])
        if status in ROTATING:
            for r in ROTATING:
                if r in st:
                    st.remove(r)
        if status not in st:
            st.append(status)
        if status == "poisoned":
            slot.setdefault("flags", {})["poison"] = 10 * (poison_counters or 1)
        side, sid = self.locate(slot)
        self.say(f"{name} 陷入{STATUS_NAMES[status]}", "status", fx="status", side=side, slot=sid, status=STATUS_NAMES[status])

    def clear_status(self, slot):
        slot["status"] = []
        slot.setdefault("flags", {}).pop("poison", None)

    def checkup(self):
        """寶可夢檢查：兩邊的戰鬥寶可夢"""
        for i in (self.current, self.other(self.current)):
            a = self.sides[i].active()
            if not a:
                continue
            name = top(a)
            st = a.get("status", [])
            if "poisoned" in st:
                d = a.get("flags", {}).get("poison", 10)
                a["damage"] = a.get("damage", 0) + d
                self.say(f"{name} 中毒受到 {d} 點傷害", "status", i, fx="damage", side=i, slot="active", amount=d)
            if "burned" in st:
                a["damage"] = a.get("damage", 0) + 20
                self.say(f"{name} 灼傷受到 20 點傷害", "status", i, fx="damage", side=i, slot="active", amount=20)
                if self.flip(f"{name} 灼傷檢查："):
                    st.remove("burned")
                    self.say(f"{name} 的灼傷治好了", "status", i)
            if "asleep" in st and self.flip(f"{name} 睡眠檢查："):
                st.remove("asleep")
                self.say(f"{name} 醒來了", "status", i)
            if "paralyzed" in st and a.get("flags", {}).get("paralyzed_turn", 0) <= self.turn:
                st.remove("paralyzed")

    # ---------------------------------------------------------------- 氣絕
    def process_ko(self, attacker):
        """檢查所有寶可夢的 HP；氣絕的話對手拿獎賞卡"""
        for j, side in enumerate(self.sides):
            for sid, sl in list(side.in_play()):
                if self.remaining(sl) > 0:
                    continue
                name = top(sl)
                prize = self.card(name).get("prize", 1)
                taker = self.sides[self.other(j)]
                self.say(f"💥 {side.name}的 {name} 氣絕了！{taker.name} 拿 {prize} 張獎賞卡", "ko", j, fx="ko", side=j, slot=sid)
                side.ko_flag_pending = True
                if side.physical:
                    sl["ko"] = True
                    self.need(f"把 {name}（和身上的能量）放到棄牌區")
                else:
                    side.discard += sl["pokemon"] + sl["energy"] + sl["tool"]
                    side.slots[sid] = empty_slot()
                if taker.physical:
                    taker.prize_count = max(0, taker.prize_count - prize)
                    self.need(f"拿 {prize} 張獎賞卡加入手牌（剩 {taker.prize_count} 張）")
                else:
                    for _ in range(prize):
                        if taker.prizes:
                            taker.hand.append(taker.prizes.pop())
                if taker.prizes_left() == 0:
                    return self.win(self.other(j), "獎賞卡全部拿完")
        for j, side in enumerate(self.sides):
            if not side.in_play():
                return self.win(self.other(j), f"{side.name} 場上沒有寶可夢了")

    # ---------------------------------------------------------------- 傷害
    def calc_damage(self, i, base, att_slot, def_slot, ignore_def=False):
        """招式對對手戰鬥寶可夢的傷害：攻擊方效果 → 弱點 ×2 → 抵抗力 -30 → 防守方效果"""
        if base <= 0:
            return 0, []
        notes = []
        att = self.card(top(att_slot))
        dfn = self.card(top(def_slot))
        dmg = base
        w = att_slot.get("flags", {}).get("weakened")
        if w and w >= self.turn:
            dmg -= 20
            notes.append("叫聲 -20")
        if dfn.get("weakness") and dfn["weakness"]["type"] == att.get("type"):
            dmg *= 2
            notes.append("弱點 ×2")
        if dfn.get("resistance") and dfn["resistance"]["type"] == att.get("type"):
            dmg -= 30
            notes.append("抵抗力 -30")
        if not ignore_def:
            prev = def_slot.get("flags", {}).get("prevent_basic")
            if prev and prev >= self.turn and att.get("stage") == "basic":
                notes.append("冰塊頭：不受基礎寶可夢的招式傷害")
                return 0, notes
            ab = dfn.get("ability") or {}
            for e in ab.get("effects", []):
                if e["op"] == "passive_damage_reduction":
                    dmg -= e["amount"]
                    notes.append(f"{ab['name']} -{e['amount']}")
        return max(0, dmg), notes

    def put_damage(self, slot, amount, why=""):
        slot["damage"] = slot.get("damage", 0) + amount
        side, sid = self.locate(slot)
        self.say(f"{top(slot)} 受到 {amount} 點傷害{why}（剩 {max(0, self.remaining(slot))} HP）", "damage",
                 fx="damage", side=side, slot=sid, amount=amount)

    # ---------------------------------------------------------------- 招式
    def attack_cost(self, slot, a):
        for e in a.get("effects", []):
            if e["op"] == "alt_cost_if_damaged" and slot.get("damage", 0) > 0:
                return e["cost"]
        return a["cost"]

    def attack_options(self, i):
        """戰鬥寶可夢的招式，以及現在能不能用、為什麼不能"""
        a = self.sides[i].active()
        if not a:
            return []
        out = []
        for k, atk in enumerate(self.card(top(a)).get("attacks", [])):
            why = None
            cost = self.attack_cost(a, atk)
            if self.phase != "turn" or self.current != i:
                why = "不是你的回合"
            elif self.turn == 1 and self.first == i:
                why = "先攻第一回合不能攻擊"
            elif "asleep" in a.get("status", []) or "paralyzed" in a.get("status", []):
                why = "睡眠／麻痺中不能攻擊"
            elif a.get("flags", {}).get("lock") == [atk["name"], self.turn]:
                why = "上回合用過，這回合不能用"
            elif not can_pay(cost, self.provided(a)):
                why = "能量不夠"
            out.append({"index": k, "name": atk["name"], "cost": cost, "damage": atk["damage"], "mod": atk.get("damage_mod"),
                        "text": atk.get("text", ""), "ok": why is None, "why": why,
                        "needs": [e["op"] for e in atk.get("effects", []) if e["op"] in ("gust_opponent", "look_opponent_hand_put_bottom")]})
        return out

    def attack(self, i, index, params=None):
        """使用招式。params：{"target": 對手備戰格子}（拖出）、{"pick": 對手手牌位置}（愛管閒事）"""
        params = params or {}
        opts = self.attack_options(i)
        if index >= len(opts) or not opts[index]["ok"]:
            self.say(f"不能使用這個招式：{opts[index]['why'] if index < len(opts) else '?'}", "warn", i)
            return False
        me, opp = self.sides[i], self.sides[self.other(i)]
        a = me.active()
        atk = self.card(top(a))["attacks"][index]
        self.say(f"⚔️ {me.name}的 {top(a)} 使用「{atk['name']}」", "attack", i, fx="attack", side=i, slot="active", attack=atk["name"])
        if "confused" in a.get("status", []) and not self.flip(f"{top(a)} 混亂："):
            self.put_damage(a, 30, "（混亂，招式失敗）")
            return self._after_attack(i)

        effects = atk.get("effects", [])
        base = atk["damage"]
        ignore_def = any(e["op"] == "ignore_defender_effects" for e in effects)
        # ---- 決定傷害（擲硬幣等）
        for e in effects:
            op = e["op"]
            if op == "coin" and e["on"] == "tails" and any(t["op"] == "attack_fails" for t in e["then"]):
                if not self.flip():
                    self.say("招式失敗", "info", i)
                    return self._after_attack(i)
            elif op == "flip_until_tails_damage":
                n = 0
                while self.flip():
                    n += 1
                base = e["per"] * n
            elif op == "flip_n_damage":
                base = e["per"] * sum(self.flip() for _ in range(e["n"]))
            elif op == "damage_per_defender_energy":
                d = opp.active()
                base = e["per"] * (len(d["energy"]) if d else 0)
        # ---- 主要傷害
        d = opp.active()
        if d is not None and base > 0:
            dmg, notes = self.calc_damage(i, base, a, d, ignore_def)
            self.put_damage(d, dmg, f"（{'、'.join(notes)}）" if notes else "")
        # ---- 其他效果
        for e in effects:
            self._effect(i, e, params, a)
        return self._after_attack(i)

    def _effect(self, i, e, params, a):
        me, opp = self.sides[i], self.sides[self.other(i)]
        oi = self.other(i)
        op = e["op"]
        d = opp.active()
        if op == "status" and e["target"] == "opponent_active" and d:
            for st in e["status"]:
                self.apply_status(d, st, e.get("poison_counters"))
            if any(st == "paralyzed" for st in e["status"]):
                d["flags"]["paralyzed_turn"] = self.turn + 1
        elif op == "self_damage":
            self.put_damage(a, e["amount"], "（反作用）")
        elif op == "coin" and e["on"] == "tails" and any(t["op"] == "self_damage" for t in e["then"]):
            if not self.flip():
                self.put_damage(a, e["then"][0]["amount"], "（反作用）")
        elif op == "heal" and e["target"] == "self":
            h = min(e["amount"], a.get("damage", 0))
            a["damage"] = a.get("damage", 0) - h
            self.say(f"{top(a)} 恢復了 {h} HP", "heal", i, fx="heal", side=i, slot="active", amount=h)
        elif op == "damage" and e["target"] == "own_bench_all":
            for _, b in me.bench():
                self.put_damage(b, e["amount"], "（龍之強風）")
        elif op == "gust_opponent":
            self._gust(i, params.get("target"))
        elif op == "damage" and e["target"] == "opponent_active":
            d = opp.active()
            if d:
                dmg, notes = self.calc_damage(i, e["amount"], a, d)
                self.put_damage(d, dmg, f"（{'、'.join(notes)}）" if notes else "")
        elif op == "lock_attack_next_turn":
            a["flags"]["lock"] = [e["attack"], self.turn + 2]
        elif op == "prevent_damage_next_turn":
            a["flags"]["prevent_basic"] = self.turn + 1
            self.say(f"{top(a)}：下個對手回合不會受到基礎寶可夢招式的傷害", "info", i)
        elif op == "weaken_defender_next_turn" and d:
            d["flags"]["weakened"] = self.turn + 1
            self.say(f"{top(d)}：下個回合招式傷害 -20", "info", oi)
        elif op == "discard_opponent_hand_random":
            if opp.physical:
                self.need("暗影爪：電腦隨機選了你的 1 張手牌 → 把手牌洗一洗、背面朝上讓電腦「選」最左邊那張丟到棄牌區")
            elif opp.hand:
                c = opp.hand.pop(self.rng.randrange(len(opp.hand)))
                opp.discard.append(c)
                self.say(f"{opp.name} 的手牌「{c}」被丟棄", "discard", oi, fx="discard", side=oi, card=c)
        elif op == "look_opponent_hand_put_bottom":
            if opp.physical:
                self.need("愛管閒事：把你的手牌給電腦看 → 電腦選了最左邊那張，請放到你的牌庫最下面")
            elif opp.hand:
                k = params.get("pick")
                if k is None or not (0 <= k < len(opp.hand)):
                    k = max(range(len(opp.hand)), key=lambda x: self._card_value(opp.hand[x]))
                c = opp.hand.pop(k)
                opp.deck.append(c)
                self.say(f"{opp.name} 的手牌「{c}」被放到牌庫最下面", "discard", oi, fx="reveal", side=oi, card=c)
        elif op == "draw":
            if me.physical:
                self.need(f"從牌庫抽 {e['count']} 張卡")
            else:
                self.draw(i, e["count"])
                self.say(f"{me.name} 抽了 {e['count']} 張", who=i)
        elif op == "search_deck_to_bench":
            self._search_to_bench(i, e)
        elif op == "search_deck_attach_energy":
            if me.physical:
                self.free_attach += e["max"]
                self.need(f"從牌庫找最多 {e['max']} 張基本能量，貼到自己的寶可夢上（疊上去讓相機看到），然後洗牌")
            else:
                self.ai.attach_from_deck(self, i, e["max"])

    def _card_value(self, name):
        c = self.card(name)
        return {"pokemon": 3, "supporter": 4, "item": 2, "stadium": 1, "energy": 2, "special_energy": 3}.get(c.get("kind"), 1) + (5 if c.get("rule") == "V" else 0)

    def _gust(self, i, target):
        """把對手的備戰寶可夢拉到戰鬥場"""
        opp = self.sides[self.other(i)]
        bench = opp.bench()
        if not bench:
            return
        if target not in dict(bench):
            target = min(bench, key=lambda b: self.remaining(b[1]))[0]
        name = top(opp.slots[target])
        old = opp.slots["active"]
        self.clear_status(old)
        opp.slots["active"], opp.slots[target] = opp.slots[target], old
        self.say(f"{opp.name} 的 {name} 被拉到戰鬥場", "info", self.other(i), fx="place", side=self.other(i), slot="active")
        if opp.physical:
            if self.board:
                # 紀錄已經換好了；真人實際換卡之前，相機看到的跟紀錄不同，先不要自動校正
                self.board.no_fix.update({s: w.committed for s, w in self.board.watches.items()})
            self.need(f"把 {name} 跟戰鬥場的 {top(old)} 互換位置（不用付撤退費用）")

    def _search_to_bench(self, i, e):
        me = self.sides[i]
        if me.physical:
            self.expect_bench += e["max"]
            what = f"「{e['filter']['name']}」" if "name" in e["filter"] else "基礎寶可夢"
            self.need(f"從牌庫找 1 張{what}放到備戰區，然後洗牌")
            return
        free = me.free_bench()
        cands = [c for c in me.deck if self._match(c, e["filter"])]
        if free and cands:
            c = max(cands, key=lambda n: self.ai.basic_value(self, i, n))
            me.deck.remove(c)
            sl = dict(empty_slot(), pokemon=[c], turn=self.turn)
            me.slots[free[0]] = sl
            self.rng.shuffle(me.deck)
            self.say(f"{me.name} 從牌庫把 {c} 放到備戰區", who=i)

    def _match(self, name, f):
        c = self.card(name)
        if "name" in f and name != f["name"]:
            return False
        if "kind" in f and c.get("kind") != f["kind"]:
            return False
        if "stage" in f and c.get("stage") != f["stage"]:
            return False
        if "type" in f and c.get("type") != f["type"]:
            return False
        if f.get("evolution") and c.get("stage") == "basic":
            return False
        if f.get("basic_energy") and c.get("kind") != "energy":
            return False
        if "hp_max" in f and c.get("hp", 999) > f["hp_max"]:
            return False
        return True

    def _after_attack(self, i):
        """攻擊完：氣絕檢查 → 回合結束"""
        self.process_ko(attacker=i)
        if self.phase != "over":
            self.end_turn(i)
        return True

    # ---------------------------------------------------------------- 電腦（數位）的動作
    def d_place(self, i, name, sid):
        s = self.sides[i]
        s.hand.remove(name)
        s.slots[sid] = dict(empty_slot(), pokemon=[name], turn=self.turn)
        if self.phase == "setup":                     # 開局擺放：先不公開放了什麼
            self.say(f"{s.name} 放了 1 張寶可夢到{SLOT_NAMES[sid]}（開始後翻開）", who=i)
        else:
            self.say(f"{s.name} 把 {name} 放到{SLOT_NAMES[sid]}", who=i, fx="place", side=i, slot=sid, card=name)

    def can_evolve(self, i, slot, evo):
        c = self.card(evo)
        return (c.get("evolves_from") == top(slot) and self.turn > 2 and slot.get("turn", 0) < self.turn
                and slot.get("evolved", -1) < self.turn)

    def d_evolve(self, i, name, sid):
        s = self.sides[i]
        sl = s.slots[sid]
        old = top(sl)
        s.hand.remove(name)
        sl["pokemon"].append(name)
        sl["evolved"] = self.turn
        self.clear_status(sl)
        self.say(f"{s.name} 的 {old} 進化成 {name}", who=i, fx="evolve", side=i, slot=sid, card=name)
        self.on_evolve_ability(i, sl)

    def on_evolve_ability(self, i, sl):
        ab = self.card(top(sl)).get("ability") or {}
        if ab.get("trigger") == "on_evolve_from_hand":
            d = self.sides[self.other(i)].active()
            self.say(f"特性「{ab['name']}」發動", "ability", i)
            if d:
                for e in ab["effects"]:
                    for st in e.get("status", []):
                        self.apply_status(d, st)

    def d_attach(self, i, name, sid, free=False):
        s = self.sides[i]
        s.hand.remove(name)
        if self.card(name).get("attach_cost"):
            junk = self.ai.pick_discard(self, i, exclude=None)
            if junk is not None:
                s.hand.remove(junk)
                s.discard.append(junk)
                self.say(f"{s.name} 丟棄「{junk}」來貼極光能量", who=i)
        sl = s.slots[sid]
        sl["energy"].append(name)
        if not free:
            s.energy_attached += 1
        if name == "芳香草能量" and self.card(top(sl)).get("type") == "grass":
            self.clear_status(sl)
        self.say(f"{s.name} 把「{name}」貼給 {top(sl)}", who=i, fx="attach", side=i, slot=sid, card=name)

    def d_retreat(self, i, to_sid, discard_energy: list[int]):
        s = self.sides[i]
        a = s.slots["active"]
        for k in sorted(discard_energy, reverse=True):
            s.discard.append(a["energy"].pop(k))
        self.clear_status(a)
        s.slots["active"], s.slots[to_sid] = s.slots[to_sid], a
        s.retreated = True
        self.say(f"{s.name} 的 {top(a)} 撤退，換上 {top(s.slots['active'])}", who=i, fx="place", side=i, slot="active")

    def d_switch(self, i, to_sid):
        s = self.sides[i]
        a = s.slots["active"]
        self.clear_status(a)
        s.slots["active"], s.slots[to_sid] = s.slots[to_sid], a

    # ---------------------------------------------------------------- 訓練家卡效果（電腦用；真人用的在 on_human_trainer）
    def d_trainer(self, i, name, choose=None):
        """電腦使用訓練家卡。choose：AI 提供的選擇函式"""
        s = self.sides[i]
        opp = self.sides[self.other(i)]
        c = self.card(name)
        s.hand.remove(name)
        if c["kind"] == "stadium":
            if self.stadium:                                    # 舊的競技場被換掉 → 放到它主人的棄牌區
                owner = self.sides[self.stadium[1]]
                if owner.physical:
                    self.need(f"把你的競技場「{self.stadium[0]}」放到棄牌區")
                else:
                    owner.discard.append(self.stadium[0])
            self.stadium = (name, i)
            self.say(f"{s.name} 放出競技場「{name}」", "trainer", i, fx="trainer", side=i, card=name)
            return
        if c["kind"] == "supporter":
            s.supporter_played = True
        self.say(f"{s.name} 使用{'支援者' if c['kind'] == 'supporter' else '物品'}「{name}」", "trainer", i, fx="trainer", side=i, card=name)
        if c.get("play_cost"):
            junk = self.ai.pick_discard(self, i, exclude=None)
            if junk is not None:
                s.hand.remove(junk)
                s.discard.append(junk)
                self.say(f"{s.name} 丟棄「{junk}」", who=i)
        for e in c["effects"]:
            self._trainer_effect(i, e, name)
        s.discard.append(name)

    def _trainer_effect(self, i, e, name):
        s = self.sides[i]
        opp = self.sides[self.other(i)]
        op = e["op"]
        if op == "draw":
            n = e["count"]
            if e.get("if_own_ko_last_turn") and s.ko_last_turn:
                n = e["if_own_ko_last_turn"]
            if "bonus_if_opponent_prizes_in" in e and opp.prizes_left() in e["bonus_if_opponent_prizes_in"]:
                n += e["bonus"]
            self.draw(i, n)
            self.say(f"{s.name} 抽了 {n} 張（手牌 {len(s.hand)} 張）", who=i)
        elif op == "draw_until":
            n = max(0, e["hand"] - len(s.hand))
            self.draw(i, n)
            self.say(f"{s.name} 抽了 {n} 張", who=i)
        elif op == "draw_equal_opponent_hand":
            n = opp.hand_estimate if opp.physical else len(opp.hand)
            self.draw(i, n)
            self.say(f"{s.name} 抽了 {n} 張", who=i)
        elif op == "discard_hand":
            s.discard += s.hand
            s.hand = []
        elif op == "shuffle_hand_into_deck":
            s.deck += s.hand
            s.hand = []
            self.rng.shuffle(s.deck)
        elif op in ("search_deck_to_hand", "look_top_pick"):
            pool = s.deck[:e["look"]] if op == "look_top_pick" else s.deck
            cands = [c for c in pool if self._match(c, e["filter"])]
            if cands:
                pick = self.ai.pick_search(self, i, cands)
                s.deck.remove(pick)
                s.hand.append(pick)
                self.say(f"{s.name} 把「{pick}」加入手牌", who=i)
            self.rng.shuffle(s.deck)
        elif op == "discard_to_hand":
            cands = [c for c in s.discard if self._match(c, e["filter"])]
            for c in cands[:e["max"]]:
                s.discard.remove(c)
                s.hand.append(c)
                self.say(f"{s.name} 從棄牌區拿回「{c}」", who=i)
        elif op == "discard_to_deck":
            for g in e["groups"]:
                for c in [c for c in s.discard if self._match(c, g["filter"])][:g["max"]]:
                    s.discard.remove(c)
                    s.deck.append(c)
            self.rng.shuffle(s.deck)
            self.say(f"{s.name} 把棄牌區的卡放回牌庫", who=i)
        elif op == "switch_own":
            b = self.ai.pick_switch(self, i)
            if b:
                self.d_switch(i, b)
                self.say(f"{s.name} 把 {top(s.slots['active'])} 換上戰鬥場", who=i, fx="place", side=i, slot="active")
        elif op == "coin" and e["on"] == "heads":
            if self.flip(f"「{name}」"):
                for t in e["then"]:
                    if t["op"] == "gust_opponent":
                        self._gust(i, self.ai.pick_gust(self, i))
        elif op == "attach_from_discard":
            cands = [c for c in s.discard if self._match(c, e["filter"])]
            if cands and s.in_play():
                c = cands[0]
                s.discard.remove(c)
                s.hand.append(c)
                sid = self.ai.pick_attach_target(self, i, c) or "active"
                self.d_attach(i, c, sid, free=True)

    # ---------------------------------------------------------------- 競技場（草路競技場）
    def d_use_stadium(self, i):
        s = self.sides[i]
        if not self.stadium or s.stadium_used:
            return False
        c = self.card(self.stadium[0])
        cands = [x for x in s.deck if self._match(x, c["effects"][0]["filter"])]
        s.stadium_used = True
        if not cands:
            return False
        pick = self.ai.pick_search(self, i, cands)
        s.deck.remove(pick)
        s.hand.append(pick)
        self.rng.shuffle(s.deck)
        self.say(f"{s.name} 用「{self.stadium[0]}」把 {pick} 加入手牌", "trainer", i)
        return True

    # ================================================================ 真人（相機）那一邊
    def on_board_events(self, events, now=None):
        """相機判斷出來的動作 → 套用規則（每回合限制、進化規則、訓練家效果）"""
        if self.sides[0].physical is False:
            return
        me = self.sides[0]
        my_turn = self.phase == "turn" and self.current == 0
        for ev in events:
            t = ev["type"]
            slot = me.slots.get(ev.get("slot")) if ev.get("slot") else None
            if t == "place":
                if slot is not None:
                    slot["turn"] = self.turn if self.phase != "setup" else 0
                if self.phase == "setup":
                    continue
                if self.expect_bench > 0 and ev["slot"] != "active":
                    self.expect_bench -= 1
                elif not my_turn and self.phase != "promote":
                    self.say(f"現在不是你的回合，但 {ev['card']} 被放到場上了", "warn", 0)
            elif t == "evolve":
                if slot is None:
                    continue
                problems = []
                if not my_turn:
                    problems.append("不是你的回合")
                elif self.turn <= 2:
                    problems.append("雙方的第一回合都不能進化")
                elif slot.get("turn", 0) >= self.turn:
                    problems.append("這回合才放上場的寶可夢不能進化")
                elif slot.get("evolved", -1) >= self.turn:
                    problems.append("同一隻寶可夢一回合只能進化一次")
                if problems:
                    self.say(f"⚠ {ev['card']} 的進化不符合規則：{'、'.join(problems)}", "warn", 0)
                slot["evolved"] = self.turn
                self.clear_status(slot)
                self.on_evolve_ability(0, slot)
            elif t == "attach_energy":
                if self.phase == "setup":
                    self.say("開局擺放時不能貼能量", "warn", 0)
                    continue
                if self.free_attach > 0:
                    self.free_attach -= 1
                    continue
                if not my_turn:
                    self.say("現在不是你的回合，不能貼能量", "warn", 0)
                    continue
                me.energy_attached += 1
                if me.energy_attached > 1:
                    self.say("⚠ 每回合只能從手牌貼 1 張能量", "warn", 0)
                if self.card(ev["card"]).get("attach_cost"):
                    self.need("極光能量：從手牌丟棄 1 張卡到棄牌區")
                if ev["card"] == "芳香草能量" and slot is not None and self.card(top(slot)).get("type") == "grass":
                    self.clear_status(slot)
            elif t == "play_trainer":
                self.on_human_trainer(ev["card"], my_turn)
            elif t in ("move", "retreat_cost"):
                if t == "move" and ev.get("src") == "active" and my_turn and not ev["text"].endswith("不用撤退費用）"):
                    moved = me.slots.get(ev["slot"])
                    if moved is not None:
                        if any(x in moved.get("status", []) for x in ("asleep", "paralyzed")):
                            self.say(f"⚠ {ev['card']} 在睡眠／麻痺中，不能撤退", "warn", 0)
                        self.clear_status(moved)
                    if me.retreated:
                        self.say("⚠ 每回合只能撤退 1 次", "warn", 0)
                    me.retreated = True
                elif t == "move" and ev.get("src") == "active":
                    moved = me.slots.get(ev["slot"])
                    if moved is not None:
                        self.clear_status(moved)
                if t == "move" and ev["slot"] == "active":
                    self.promote_done()
            elif t == "leave":
                pass
        if self.phase == "promote" and me.active():
            self.promote_done()

    def on_human_trainer(self, name, my_turn):
        """真人使用訓練家卡：檢查限制、系統負責的部分直接處理，其餘告訴真人怎麼做"""
        me, cpu = self.sides[0], self.sides[1]
        c = self.card(name)
        if not my_turn:
            self.say(f"⚠ 現在不是你的回合，不能使用「{name}」", "warn", 0)
            return
        if c["kind"] == "supporter":
            if self.turn == 1 and self.first == 0:
                self.say("⚠ 先攻的第一回合不能使用支援者", "warn", 0)
            if me.supporter_played:
                self.say("⚠ 每回合只能使用 1 張支援者", "warn", 0)
            me.supporter_played = True
            if c.get("condition") == "own_ko_last_turn" and not me.ko_last_turn:
                self.say("⚠ 奇巴納只能在上個對手回合自己的寶可夢氣絕時使用", "warn", 0)
        self.say(f"你使用了「{name}」", "trainer", 0, fx="trainer", side=0, card=name)
        done = "，用完放到棄牌區"
        if name == "寶可夢捕捉器":
            if self.flip("寶可夢捕捉器："):
                opts = [s for s, _ in cpu.bench()]
                if opts:
                    self.choice = {"kind": "gust", "prompt": "選電腦的 1 隻備戰寶可夢拉到戰鬥場",
                                   "options": [{"id": s, "label": f"{top(cpu.slots[s])}（剩 {self.remaining(cpu.slots[s])} HP）"} for s in opts]}
            self.need("把寶可夢捕捉器放到棄牌區")
            return
        if name == "模仿少女":
            self.need(f"手牌全部放回牌庫洗牌，再抽 {len(cpu.hand)} 張（電腦現在有 {len(cpu.hand)} 張手牌）" + done)
            return
        if name == "女學生":
            n = 4 if cpu.prizes_left() in (6, 4, 2) else 2
            self.need(f"從牌庫抽 {n} 張卡（電腦剩 {cpu.prizes_left()} 張獎賞卡）" + done)
            return
        if name == "奇巴納":
            self.free_attach += 1
            self.need("從棄牌區拿 1 張基本能量貼到自己的寶可夢上（疊上去讓相機看到），再從牌庫任選 1 張卡加入手牌、洗牌" + done)
            return
        if name == "希巴":
            n = 7 if me.ko_last_turn else 4
            self.need(f"手牌全部放回牌庫洗牌，再抽 {n} 張" + done)
            return
        if c["kind"] == "stadium":
            if self.stadium and self.stadium[1] == 1:
                cpu.discard.append(self.stadium[0])
            self.stadium = (name, 0)
            self.need("把競技場卡放在場邊（不用放到棄牌區）")
            return
        self.need(f"照卡片效果做：{c.get('text', '')}" + done)

    def human_use_stadium(self):
        me = self.sides[0]
        if not self.stadium or self.current != 0 or self.phase != "turn":
            return
        if me.stadium_used:
            self.say("⚠ 競技場效果每回合只能用 1 次", "warn", 0)
            return
        me.stadium_used = True
        self.need("草路競技場：從牌庫找 1 張草屬性進化寶可夢加入手牌（給電腦看），然後洗牌")

    def human_attack(self, index):
        """真人按了招式按鈕：需要選目標的先跳出選擇"""
        opts = self.attack_options(0)
        if not (0 <= index < len(opts)):
            return False
        o = opts[index]
        if not o["ok"]:
            self.say(f"不能使用「{o['name']}」：{o['why']}", "warn", 0)
            return False
        cpu = self.sides[1]
        if "gust_opponent" in o["needs"] and len(cpu.bench()) > 1:
            self.choice = {"kind": "attack", "index": index, "param": "target", "prompt": f"{o['name']}：選電腦的 1 隻備戰寶可夢拉到戰鬥場",
                           "options": [{"id": s, "label": f"{top(sl)}（剩 {self.remaining(sl)} HP）"} for s, sl in cpu.bench()]}
            return True
        if "look_opponent_hand_put_bottom" in o["needs"] and cpu.hand:
            self.say(f"電腦的手牌：{'、'.join(cpu.hand)}", "info", 1)
            self.choice = {"kind": "attack", "index": index, "param": "pick", "prompt": f"{o['name']}：選電腦的 1 張手牌放到牌庫最下面",
                           "options": [{"id": k, "label": c} for k, c in enumerate(cpu.hand)]}
            return True
        return self.attack(0, index)

    def human_mulligan(self):
        """真人重抽 1 次：電腦可以多抽 1 張"""
        if self.phase != "setup":
            return
        self.draw(1, 1)
        self.say("你重抽了手牌（沒有基礎寶可夢）→ 電腦多抽 1 張", "rule", 1)

    def human_deck_out(self):
        if self.phase == "turn" and self.current == 0:
            self.say("你的牌庫沒有卡可以抽了", "rule", 0)
            self.win(1, "對手牌庫抽完")

    def todo_done(self, k):
        if 0 <= k < len(self.todo):
            self.todo.pop(k)

    def resolve_choice(self, pick):
        c = self.choice
        if not c:
            return
        ids = [o["id"] for o in c.get("options", [])]
        if pick not in ids:
            return
        self.choice = None
        if c["kind"] == "gust":
            self._gust(0, pick)
        elif c["kind"] == "attack":
            self.attack(0, c["index"], {c["param"]: pick})

    # ================================================================ 給畫面的資料
    def slot_view(self, i, sid):
        sl = self.sides[i].slots[sid]
        name = top(sl)
        if not name or sl.get("ko"):
            return None
        c = self.card(name)
        return {"slot": sid, "name": name, "hp": c.get("hp"), "damage": sl.get("damage", 0), "type": c.get("type"),
                "stage": c.get("stage"), "rule": c.get("rule"), "energy": list(sl.get("energy", [])),
                "status": [STATUS_NAMES[x] for x in sl.get("status", [])], "chain": list(sl["pokemon"]),
                "attacks": [{"name": a["name"], "cost": a["cost"], "damage": a["damage"], "mod": a.get("damage_mod"), "text": a.get("text", "")} for a in c.get("attacks", [])],
                "ability": (c.get("ability") or {}).get("name"), "weakness": c.get("weakness"), "resistance": c.get("resistance"),
                "retreat": c.get("retreat")}

    def snapshot(self, since=None):
        sides = []
        for i, s in enumerate(self.sides):
            sides.append({"name": s.name, "deck": s.deck_name, "physical": s.physical, "prizes": s.prizes_left(),
                          "hand": None if s.physical else len(s.hand), "deck_count": None if s.physical else len(s.deck),
                          "discard": None if s.physical else len(s.discard),
                          "discard_list": None if s.physical else list(s.discard),
                          "slots": {sid: (None if self.phase == "setup" and not s.physical else self.slot_view(i, sid)) for sid in SLOTS},
                          "flags": {"energy": s.energy_attached, "supporter": s.supporter_played, "retreated": s.retreated}})
        log = [e if since is None or e["n"] > since else {k: v for k, v in e.items() if k != "state"} for e in self.log[-60:]]
        return {"phase": self.phase, "turn": self.turn, "current": self.current, "first": self.first, "winner": self.winner,
                "sides": sides, "stadium": self.stadium[0] if self.stadium else None, "log": log, "todo": self.todo,
                "state": self.field_state(), "seq": self.seq,
                "choice": self.choice, "attacks": self.attack_options(0) if self.sides[0].physical else []}


def load_decks(folder):
    return json.loads((Path(folder) / "decks.json").read_text(encoding="utf-8"))
