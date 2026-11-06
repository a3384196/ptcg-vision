"""
電腦對手的大腦（貪婪式＋簡單評分）

每回合的順序：
  1. 找卡：物品（先機球、超級球、寶可齒輪…）、競技場
  2. 放基礎寶可夢到備戰區、進化
  3. 貼能量（挑「最快能打出招式」的對象）
  4. 支援者（抽牌類先用；會把手牌洗掉／丟掉的放在最後）
  5. 再放一次寶可夢、進化（剛抽到的）
  6. 戰鬥寶可夢打不了而備戰有人能打 → 寶可夢交替或撤退
  7. 能用寶可夢捕捉器拉出一隻打得死的 → 拉
  8. 選分數最高的招式攻擊（打得倒 > 傷害高 > 附加效果），沒有能用的就結束回合
"""
from __future__ import annotations

from collections import Counter

from game import BENCH, can_pay, top


class AI:
    def __init__(self, name="電腦"):
        self.name = name

    # ---------------------------------------------------------------- 評估工具
    def missing(self, g, slot, extra=None):
        """離最便宜的招式還差幾張能量（0 = 現在就能打）"""
        prov = g.provided(slot) + ([extra] if extra else [])
        best = 99
        for a in g.card(top(slot)).get("attacks", []):
            cost = g.attack_cost(slot, a)
            if can_pay(cost, prov):
                return 0
            # 估計還缺幾張：總張數差 + 指定屬性缺口
            need = Counter(c for c in cost if c != "colorless")
            have = Counter(prov)
            short = sum(max(0, n - have.get(t, 0)) for t, n in need.items()) - have.get("any", 0)
            best = min(best, max(short, len(cost) - len(prov), 1))
        return best

    def best_damage(self, g, i, slot, defender):
        """這隻寶可夢現在能打出的最高期望傷害"""
        best = 0
        for a in g.card(top(slot)).get("attacks", []):
            if can_pay(g.attack_cost(slot, a), g.provided(slot)):
                base = self.expected_base(g, a, defender)
                if defender and base > 0:
                    dmg, _ = g.calc_damage(i, base, slot, defender)
                    best = max(best, dmg)
        return best

    def expected_base(self, g, a, defender):
        base = a["damage"]
        for e in a.get("effects", []):
            if e["op"] == "flip_until_tails_damage":
                base = e["per"]
            elif e["op"] == "flip_n_damage":
                base = e["per"] * e["n"] * 0.5
            elif e["op"] == "damage_per_defender_energy":
                base = e["per"] * (len(defender["energy"]) if defender else 0)
            elif e["op"] == "coin" and any(t["op"] == "attack_fails" for t in e["then"]):
                base = base * 0.5
        return int(base)

    def can_attack_now(self, g, i, slot):
        if not slot or any(x in slot.get("status", []) for x in ("asleep", "paralyzed")):
            return False
        for a in g.card(top(slot)).get("attacks", []):
            if slot.get("flags", {}).get("lock") == [a["name"], g.turn]:
                continue
            if can_pay(g.attack_cost(slot, a), g.provided(slot)) and (a["damage"] or a.get("effects")):
                return True
        return False

    def basic_value(self, g, i, name):
        c = g.card(name)
        s = g.sides[i]
        v = c.get("hp", 0) / 10
        if c.get("rule") == "V":
            v += 30
        if any(g.card(x).get("evolves_from") == name for x in s.hand + s.deck):
            v += 15
        v += max((a["damage"] for a in c.get("attacks", [])), default=0) / 20
        return v

    def want(self, g, i, name):
        """找牌時這張卡有多想要"""
        s = g.sides[i]
        c = g.card(name)
        inplay = [top(sl) for _, sl in s.in_play()]
        if c.get("kind") == "pokemon":
            if c.get("evolves_from") in inplay and name not in s.hand:
                return 60 + c.get("hp", 0) / 10
            if c.get("stage") == "basic" and s.free_bench():
                return 30 + self.basic_value(g, i, name) - 10 * sum(1 for x in s.hand if g.card(x).get("stage") == "basic")
            if c.get("evolves_from") and any(x == c["evolves_from"] for x in s.hand):
                return 25
            return 5
        if c.get("kind") == "supporter":
            return 20 if not any(g.card(x).get("kind") == "supporter" for x in s.hand) else 5
        if c.get("kind") in ("energy", "special_energy"):
            return 18 if not any(g.card(x).get("kind") in ("energy", "special_energy") for x in s.hand) else 6
        return 8

    def pick_search(self, g, i, cands):
        return max(cands, key=lambda n: self.want(g, i, n))

    def pick_discard(self, g, i, exclude=None):
        s = g.sides[i]
        pool = [c for c in s.hand if c != exclude]
        if not pool:
            return None
        return min(pool, key=lambda n: self.want(g, i, n))

    def pick_switch(self, g, i):
        s = g.sides[i]
        opp = g.sides[g.other(i)].active()
        bench = s.bench()
        if not bench:
            return None
        return max(bench, key=lambda b: (self.can_attack_now(g, i, b[1]), self.best_damage(g, i, b[1], opp), g.remaining(b[1])))[0]

    def pick_gust(self, g, i):
        opp = g.sides[g.other(i)]
        a = g.sides[i].active()
        bench = opp.bench()
        if not bench:
            return None

        def score(b):
            dmg = self.best_damage(g, i, a, b[1]) if a else 0
            ko = dmg >= g.remaining(b[1])
            return (ko, g.card(top(b[1])).get("prize", 1) if ko else 0, -g.remaining(b[1]))
        return max(bench, key=score)[0]

    def energy_gain(self, g, i, sid, slot, energy):
        """把這張能量貼給這隻寶可夢有多好"""
        c = g.card(energy)
        if energy == "極光能量" and len(g.sides[i].hand) < 2:
            return -1
        before = self.missing(g, slot)
        prov = (c.get("provides") or ["colorless"])[0]
        after = self.missing(g, slot, prov)
        gain = (before - after) * 25
        if after == 0 and before > 0:
            gain += 60
        if sid == "active":
            gain += 15
        gain += min(g.remaining(slot), 200) / 20
        if g.card(top(slot)).get("rule") == "V":
            gain += 10
        if before == 0:                                         # 已經能打：多貼一點備用（撤退、大招）
            big = max((len(a["cost"]) for a in g.card(top(slot)).get("attacks", [])), default=0)
            gain = 8 if len(slot["energy"]) < big else 2
        return gain

    def pick_attach_target(self, g, i, energy):
        s = g.sides[i]
        opts = [(self.energy_gain(g, i, sid, sl, energy), sid) for sid, sl in s.in_play()]
        return max(opts)[1] if opts else None

    # ---------------------------------------------------------------- 開局
    def setup(self, g, i):
        s = g.sides[i]
        basics = [c for c in s.hand if g.card(c).get("stage") == "basic"]
        # 戰鬥場：挑耐打、又不是 V（V 被打倒要給 2 張獎賞卡）的；只有 V 就放 V
        act = max(basics, key=lambda n: (g.card(n).get("rule") != "V", g.card(n).get("hp", 0) - 15 * g.card(n).get("retreat", 1)))
        g.d_place(i, act, "active")
        for n in sorted([b for b in basics if b != act], key=lambda n: -self.basic_value(g, i, n))[:5]:
            if s.free_bench():
                g.d_place(i, n, s.free_bench()[0])

    # ---------------------------------------------------------------- 回合
    def take_turn(self, g, i):
        if g.phase != "turn" or g.current != i:
            return
        s = g.sides[i]
        self.use_items(g, i)
        self.stadium(g, i)
        self.place_and_evolve(g, i)
        self.attach(g, i)
        self.supporter(g, i)
        self.use_items(g, i)
        self.place_and_evolve(g, i)
        if s.energy_attached == 0:
            self.attach(g, i)
        self.switch_or_retreat(g, i)
        self.gust(g, i)
        if g.phase == "turn" and g.current == i:
            self.attack_or_end(g, i)

    def place_and_evolve(self, g, i):
        s = g.sides[i]
        for _ in range(12):
            done = False
            for name in sorted(set(s.hand), key=lambda n: -g.card(n).get("hp", 0)):
                c = g.card(name)
                if c.get("kind") != "pokemon" or name not in s.hand:
                    continue
                if c.get("stage") == "basic" and s.free_bench():
                    g.d_place(i, name, s.free_bench()[0])
                    done = True
                elif c.get("stage") != "basic":
                    targets = [(sid, sl) for sid, sl in s.in_play() if g.can_evolve(i, sl, name)]
                    if targets:
                        sid = "active" if any(t[0] == "active" for t in targets) else targets[0][0]
                        g.d_evolve(i, name, sid)
                        done = True
            if not done:
                break

    def attach(self, g, i):
        s = g.sides[i]
        if s.energy_attached or not s.in_play():
            return
        best = None
        for e in set(x for x in s.hand if g.card(x).get("kind") in ("energy", "special_energy")):
            for sid, sl in s.in_play():
                gain = self.energy_gain(g, i, sid, sl, e)
                if best is None or gain > best[0]:
                    best = (gain, e, sid)
        if best and best[0] > 0:
            g.d_attach(i, best[1], best[2])

    def attach_from_deck(self, g, i, mx):
        s = g.sides[i]
        for _ in range(mx):
            cands = [c for c in s.deck if g.card(c).get("kind") == "energy"]
            if not cands or not s.in_play():
                break
            best = max(((self.energy_gain(g, i, sid, sl, e), e, sid) for e in set(cands) for sid, sl in s.in_play()))
            s.deck.remove(best[1])
            s.hand.append(best[1])
            g.d_attach(i, best[1], best[2], free=True)
        g.rng.shuffle(s.deck)

    def stadium(self, g, i):
        s = g.sides[i]
        st = [c for c in s.hand if g.card(c).get("kind") == "stadium"]
        if st and (not g.stadium or g.stadium[0] != st[0]):
            g.d_trainer(i, st[0])
        if g.stadium:
            filt = g.card(g.stadium[0])["effects"][0]["filter"]
            if any(g._match(c, filt) and self.want(g, i, c) >= 25 for c in s.deck):
                g.d_use_stadium(i)

    def use_items(self, g, i):
        s = g.sides[i]
        for _ in range(10):
            used = False
            for name in list(dict.fromkeys(s.hand)):
                if name not in s.hand or g.card(name).get("kind") != "item":
                    continue
                if self.item_useful(g, i, name):
                    g.d_trainer(i, name)
                    used = True
                    break
            if not used:
                break

    def item_useful(self, g, i, name):
        s = g.sides[i]
        has_energy = any(g.card(x).get("kind") in ("energy", "special_energy") for x in s.hand)
        if name == "先機球":
            return bool(s.free_bench()) and any(g.card(c).get("stage") == "basic" for c in s.deck) and len(s.hand) >= 2 \
                and not any(g.card(c).get("stage") == "basic" for c in s.hand)
        if name == "超級球":
            return any(self.want(g, i, c) >= 30 for c in s.deck)
        if name == "寶可齒輪3.0":
            return not s.supporter_played and not any(g.card(c).get("kind") == "supporter" for c in s.hand)
        if name == "能量輸送":
            return not has_energy and s.energy_attached == 0
        if name == "能量回收":
            return any(g.card(c).get("kind") == "energy" for c in s.discard) and not has_energy
        if name == "救援行李箱":
            return any(g._match(c, {"kind": "pokemon", "hp_max": 90}) for c in s.discard)
        if name == "普通釣竿":
            return sum(1 for c in s.discard if g.card(c).get("kind") in ("pokemon", "energy")) >= 3
        return False                         # 寶可夢交替、寶可夢捕捉器：另外判斷

    def supporter(self, g, i):
        s = g.sides[i]
        if s.supporter_played or (g.turn == 1 and g.first == i):
            return
        opp = g.sides[g.other(i)]
        n = len(s.hand) - 1
        need_poke = not any(g.card(c).get("kind") == "pokemon" for c in s.hand)
        score = {
            "博士的研究": 50 - 6 * n,
            "赫普": 30,
            "可爾妮的氣勢": (6 - n) * 9,
            "莎娜": 38 - 8 * n,
            "希巴": 48 if s.ko_last_turn else 34 - 8 * n,
            "女學生": 40 if opp.prizes_left() in (6, 4, 2) else 20,
            "模仿少女": -1 if opp.physical else (len(opp.hand) - n) * 9,
            "寶可夢小朋友": 36 if need_poke else 10,
            "奇巴納": 45 if s.ko_last_turn and any(g.card(c).get("kind") == "energy" for c in s.discard) else -1,
        }
        opts = [(score.get(c, 0), c) for c in set(s.hand) if g.card(c).get("kind") == "supporter"]
        opts = [o for o in opts if o[0] > 0]
        if opts:
            g.d_trainer(i, max(opts)[1])

    def switch_or_retreat(self, g, i):
        s = g.sides[i]
        a = s.active()
        if not a or not s.bench():
            return
        d = g.sides[g.other(i)].active()

        def value(slot):
            if not self.can_attack_now(g, i, slot):
                return 0
            dmg = self.best_damage(g, i, slot, d) if d else 0
            return dmg + (300 if d and dmg >= g.remaining(d) else 0) + 5
        stuck = any(x in a.get("status", []) for x in ("asleep", "paralyzed", "confused"))
        cur = 0 if stuck else value(a)
        to = max((b[0] for b in s.bench()), key=lambda sid: value(s.slots[sid]))
        new = value(s.slots[to])
        if new <= cur + 20:
            return
        if "寶可夢交替" in s.hand:
            g.d_trainer(i, "寶可夢交替")
            return
        cost = g.card(top(a)).get("retreat", 0)
        if s.retreated or any(x in a.get("status", []) for x in ("asleep", "paralyzed")) or len(a["energy"]) < cost:
            return
        if cost > 2 and new < 300:                       # 撤退太貴：只有換上去打得倒才值得
            return
        g.d_retreat(i, to, list(range(len(a["energy"]) - cost, len(a["energy"]))))

    def gust(self, g, i):
        s = g.sides[i]
        a = s.active()
        opp = g.sides[g.other(i)]
        if "寶可夢捕捉器" not in s.hand or not a or not self.can_attack_now(g, i, a) or not opp.bench():
            return
        d = opp.active()
        if d and self.best_damage(g, i, a, d) >= g.remaining(d):
            return                                           # 戰鬥寶可夢已經打得倒
        t = self.pick_gust(g, i)
        if t and self.best_damage(g, i, a, opp.slots[t]) >= g.remaining(opp.slots[t]):
            g.d_trainer(i, "寶可夢捕捉器")

    # ---------------------------------------------------------------- 攻擊
    def eval_attack(self, g, i, opt):
        s = g.sides[i]
        opp = g.sides[g.other(i)]
        a = s.active()
        d = opp.active()
        atk = g.card(top(a))["attacks"][opt["index"]]
        params = {}
        score = 0
        base = self.expected_base(g, atk, d)
        if d and base > 0:
            dmg, _ = g.calc_damage(i, base, a, d, any(e["op"] == "ignore_defender_effects" for e in atk["effects"]))
            score += dmg
            if dmg >= g.remaining(d):
                score += 200 + 100 * g.card(top(d)).get("prize", 1)
        for e in atk.get("effects", []):
            op = e["op"]
            if op == "status" and d and not d.get("status"):
                score += 12 * len(e["status"]) + (25 if e.get("poison_counters") else 0) + (10 if "asleep" in e["status"] else 0)
            elif op == "self_damage":
                score -= e["amount"] + (400 if g.remaining(a) <= e["amount"] else 0)
            elif op == "coin" and e["on"] == "tails" and any(t["op"] == "self_damage" for t in e["then"]):
                score -= 5
            elif op == "heal":
                score += min(e["amount"], a.get("damage", 0))
            elif op == "damage" and e["target"] == "own_bench_all":
                for _, b in s.bench():
                    score -= 15 + (200 * g.card(top(b)).get("prize", 1) if g.remaining(b) <= e["amount"] else 0)
            elif op == "gust_opponent":
                bench = opp.bench()
                if bench:
                    def after(b):
                        dmg, _ = g.calc_damage(i, 30, a, b[1])
                        return (dmg >= g.remaining(b[1]), -g.remaining(b[1]))
                    t = max(bench, key=after)
                    params["target"] = t[0]
                    dmg, _ = g.calc_damage(i, 30, a, t[1])
                    score += dmg + (200 + 100 * g.card(top(t[1])).get("prize", 1) if dmg >= g.remaining(t[1]) else 0)
            elif op == "damage" and e["target"] == "opponent_active" and not any(x["op"] == "gust_opponent" for x in atk["effects"]) and d:
                dmg, _ = g.calc_damage(i, e["amount"], a, d)
                score += dmg
            elif op == "draw":
                score += 8 * e["count"]
            elif op == "search_deck_to_bench":
                score += 25 if s.free_bench() else 0
            elif op == "search_deck_attach_energy":
                score += 40
            elif op in ("discard_opponent_hand_random", "look_opponent_hand_put_bottom"):
                score += 15
            elif op == "prevent_damage_next_turn" and d and g.card(top(d)).get("stage") == "basic":
                score += 25
            elif op == "weaken_defender_next_turn":
                score += 12
            elif op == "lock_attack_next_turn":
                score -= 10
        return score, params

    def attack_or_end(self, g, i):
        opts = [o for o in g.attack_options(i) if o["ok"]]
        if opts:
            scored = [(self.eval_attack(g, i, o), o) for o in opts]
            (sc, params), o = max(scored, key=lambda x: x[0][0])
            if sc > 0:
                g.attack(i, o["index"], params)
                return
        g.say(f"{g.sides[i].name} 結束回合", who=i)
        g.end_turn(i)


def play_cpu_vs_cpu(g, ai, max_turns=200):
    """電腦對電腦自我對戰（測試用）：回傳勝方"""
    while g.phase != "over" and g.turn < max_turns:
        if g.phase == "promote":
            raise RuntimeError("數位玩家不應該進入 promote")
        before = (g.turn, g.current)
        ai.take_turn(g, g.current)
        if (g.turn, g.current) == before and g.phase == "turn":
            raise RuntimeError(f"回合卡住了：turn={g.turn}")
    return g.winner
