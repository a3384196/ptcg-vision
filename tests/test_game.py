"""
規則引擎＋電腦 AI 測試（不需要相機）
執行：python tests/test_game.py
"""
import sys
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board import Board, CardBook, empty_slot  # noqa: E402
from game import Game, can_pay, load_decks  # noqa: E402
from ai import AI, play_cpu_vs_cpu  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"
BOOK = CardBook(DATA)
DECKS = load_decks(DATA)
A, B = "快龍V牌組", "朽木妖V牌組"


def check(name, cond, info=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"\n     {info}"))
    return cond


def put(g, i, sid, chain, energy=(), damage=0):
    if isinstance(chain, str):
        chain = [chain]
    g.sides[i].slots[sid] = dict(empty_slot(), pokemon=list(chain), energy=list(energy), damage=damage)
    return g.sides[i].slots[sid]


def fresh(seed=1, turn=5, current=0):
    g = Game(BOOK, DECKS, A, B, p0_physical=False, seed=seed)
    g.ai = AI()
    g.phase, g.turn, g.current, g.first = "turn", turn, current, 0
    return g


def main():
    ok = True
    print("1. 能量付費")
    ok &= check("草＋無色：草草 OK", can_pay(["grass", "colorless"], ["grass", "grass"]))
    ok &= check("水水雷：水水惡 不夠", not can_pay(["water", "water", "lightning"], ["water", "water", "darkness"]))
    ok &= check("極光能量可以當任何屬性", can_pay(["water", "water", "lightning"], ["water", "any", "water"]))
    ok &= check("無色只看張數", can_pay(["colorless"] * 3, ["metal", "grass", "any"]) and not can_pay(["colorless"] * 3, ["metal"]))

    print("2. 傷害計算")
    g = fresh()
    att = put(g, 0, "active", "阿勃梭魯", ["基本惡能量"] * 3)
    dfn = put(g, 1, "active", ["綠毛蟲", "鐵甲蛹"])
    ok &= check("鐵甲蛹「堅硬身軀」-20：劈開 80 → 60", g.calc_damage(0, 80, att, dfn)[0] == 60, g.calc_damage(0, 80, att, dfn))
    att = put(g, 1, "active", "朽木妖V", ["基本草能量"] * 3)
    dfn = put(g, 0, "active", "阿勃梭魯")
    ok &= check("阿勃梭魯弱草 ×2：暗影爪 120 → 240", g.calc_damage(1, 120, att, dfn)[0] == 240)
    dfn = put(g, 0, "active", "銅象")
    g2 = fresh()
    a2 = put(g2, 0, "active", "朽木妖V")
    d2 = put(g2, 1, "active", "銅象")
    ok &= check("銅象抵抗草 -30：生命吸取 30 → 0", g2.calc_damage(0, 30, a2, d2)[0] == 0)
    d3 = put(g2, 1, "active", "冰砌鵝")
    d3["flags"]["prevent_basic"] = g2.turn
    ok &= check("冰塊頭：基礎寶可夢的招式傷害為 0", g2.calc_damage(0, 120, a2, d3)[0] == 0)
    a3 = put(g2, 0, "active", "快龍V", ["基本雷能量"] * 2)
    d4 = put(g2, 1, "active", ["綠毛蟲", "鐵甲蛹"])
    ok &= check("撕裂不受對手效果影響：鐵甲蛹照樣吃 50", g2.calc_damage(0, 50, a3, d4, ignore_def=True)[0] == 50)
    a4 = put(g2, 0, "active", "刺梭魚")
    a4["flags"]["weakened"] = g2.turn
    ok &= check("被「叫聲」後招式 -20", g2.calc_damage(0, 10, a4, put(g2, 1, "active", "伊布"))[0] == 0)

    print("3. 招式與氣絕、獎賞卡")
    g = fresh(turn=5, current=0)
    g.sides[0].prizes = ["基本水能量"] * 6
    g.sides[1].prizes = ["基本草能量"] * 6
    put(g, 0, "active", "快龍V", ["基本水能量", "基本水能量", "基本雷能量"])
    put(g, 0, "bench1", "伊布")
    put(g, 1, "active", "朽木妖V")
    put(g, 1, "bench1", "綠毛蟲")
    g.attack(0, 1)
    ok &= check("龍之強風 250 打倒朽木妖V（V 拿 2 張）", len(g.sides[0].prizes) == 4, [l["text"] for l in g.log])
    ok &= check("自己的備戰區也受 20（伊布剩 30）", g.remaining(g.sides[0].slots["bench1"]) == 30)
    ok &= check("電腦自動把綠毛蟲換上戰鬥場、換對手回合", g.sides[1].slots["active"]["pokemon"] == ["綠毛蟲"] and g.current == 1)

    print("4. 特殊狀態")
    g = fresh(turn=5, current=0)
    put(g, 0, "active", "毒骷蛙", ["基本惡能量"])
    put(g, 1, "active", "冰砌鵝")
    put(g, 1, "bench1", "伊布")
    g.attack(0, 0)
    ok &= check("惡棍猛毒：每次寶可夢檢查 40 傷害", g.sides[1].slots["active"]["damage"] == 40, [l["text"] for l in g.log])
    g = fresh(turn=5, current=0)
    put(g, 0, "active", "蘑蘑菇")
    s = put(g, 1, "active", ["綠毛蟲", "鐵甲蛹"], ["芳香草能量"])
    g.apply_status(s, "poisoned")
    ok &= check("芳香草能量：草屬性寶可夢不會陷入特殊狀態", s["status"] == [])
    s = put(g, 1, "active", "伊布")
    g.apply_status(s, "asleep")
    g.apply_status(s, "confused")
    ok &= check("睡眠被混亂取代", s["status"] == ["confused"])

    print("5. 規則限制")
    g = fresh(turn=1, current=0)
    put(g, 0, "active", "快龍V", ["基本雷能量"] * 2)
    ok &= check("先攻第一回合不能攻擊", not g.attack_options(0)[0]["ok"])
    g = fresh(turn=2, current=0)
    sl = put(g, 0, "bench1", "刺梭魚")
    ok &= check("第 2 回合不能進化", not g.can_evolve(0, sl, "戽斗尖梭"))
    g.turn = 4
    sl["turn"] = 4
    ok &= check("剛放上場的不能進化", not g.can_evolve(0, sl, "戽斗尖梭"))
    sl["turn"] = 2
    ok &= check("上回合放的可以進化", g.can_evolve(0, sl, "戽斗尖梭"))

    print("6. 電腦對電腦自我對戰（兩副牌、先後攻輪流，各 150 局）")
    res = collections.Counter()
    turns = []
    errors = []
    for seed in range(300):
        p0, p1 = (A, B) if seed % 2 else (B, A)
        g = Game(BOOK, DECKS, p0, p1, p0_physical=False, seed=seed, names=(p0, p1))
        ai = AI()
        g.ai = ai
        try:
            g.digital_setup(0, ai)
            g.digital_setup(1, ai)
            g.start()
            w = play_cpu_vs_cpu(g, ai)
            # 卡片數守恆：60 張 = 牌庫 + 手牌 + 棄牌 + 獎賞 + 場上（+ 競技場）
            for i, side in enumerate(g.sides):
                n = len(side.deck) + len(side.hand) + len(side.discard) + len(side.prizes)
                n += sum(len(sl["pokemon"]) + len(sl["energy"]) + len(sl["tool"]) for _, sl in side.in_play())
                n += 1 if g.stadium and g.stadium[1] == i else 0
                if n != 60:
                    errors.append(f"seed {seed}: {side.name} 卡片數 {n}")
            res[g.sides[w].deck_name if w is not None else "沒分出勝負"] += 1
            turns.append(g.turn)
        except Exception as e:  # noqa: BLE001
            errors.append(f"seed {seed}: {type(e).__name__} {e}")
    ok &= check("300 局沒有錯誤、卡片數守恆", not errors, errors[:5])
    ok &= check("每局都分出勝負", res.get("沒分出勝負", 0) == 0, res)
    print(f"     勝場：{dict(res)}，平均 {sum(turns) / len(turns):.1f} 回合")

    print("\n全部通過 ✅" if ok else "\n有失敗 ❌")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
