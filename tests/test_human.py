"""
真人（相機）對電腦：模擬相機看到的畫面 → Board 動作 → 規則引擎 → 電腦回合
執行：python tests/test_human.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board import Board, CardBook  # noqa: E402
from game import Game, load_decks, top  # noqa: E402
from ai import AI  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"
BOOK = CardBook(DATA)
DECKS = load_decks(DATA)
FPS = 10


class Table:
    """桌面＋相機＋伺服器的模擬（跟 server.py 對戰模式一樣的流程）"""

    def __init__(self, cpu_deck="朽木妖V牌組", you_deck="快龍V牌組", seed=3):
        self.board = Board(BOOK)
        self.g = Game(BOOK, DECKS, you_deck, cpu_deck, p0_physical=True, board=self.board, seed=seed)
        self.g.ai = AI()
        self.g.digital_setup(1, self.g.ai)
        self.t = 0.0
        self.view = {z: None for z in ["active", "bench1", "bench2", "bench3", "bench4", "bench5", "trainer", "discard"]}
        self.events = []

    def hold(self, sec, **zones):
        self.view.update(zones)
        for _ in range(int(sec * FPS)):
            self.t += 1 / FPS
            ev = self.board.update(dict(self.view), self.t)
            self.events += ev
            self.g.on_board_events(ev, self.t)
            self.cpu()
        return self

    def cpu(self):
        g = self.g
        for _ in range(5):
            if g.phase == "turn" and g.current == 1:
                g.ai.take_turn(g, 1)
            else:
                break

    def attach(self, zone, energy, pokemon):
        return self.hold(1.2, **{zone: energy}).hold(1.2, **{zone: pokemon})

    def texts(self, kind=None):
        return [l["text"] for l in self.g.log if kind is None or l["kind"] == kind]


def check(name, cond, info=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"\n     {info}"))
    return cond


def main():
    ok = True
    print("1. 開局擺放、先攻、第一回合")
    T = Table()
    g = T.g
    ok &= check("開局時看不到電腦的場面", all(v is None for v in g.snapshot()["sides"][1]["slots"].values()))
    T.hold(1.5, active="刺梭魚", bench1="快龍V")
    ok &= check("擺放階段放寶可夢沒有警告", not any(l["kind"] == "warn" for l in g.log), T.texts())
    g.start(first=0)
    ok &= check("你先攻、待辦有「抽 1 張」", g.current == 0 and any("抽 1 張" in t for t in g.todo), g.todo)
    T.attach("active", "基本水能量", "刺梭魚")
    ok &= check("貼能量算進每回合 1 次", g.sides[0].energy_attached == 1)
    ok &= check("先攻第一回合不能攻擊", g.human_attack(0) is False and "先攻第一回合" in T.texts("warn")[-1], T.texts("warn"))
    T.attach("bench1", "基本雷能量", "快龍V")
    ok &= check("第 2 張能量 → 警告", "每回合只能從手牌貼 1 張能量" in "".join(T.texts("warn")), T.texts("warn"))

    print("2. 結束回合 → 電腦自動行動 → 回到你")
    g.end_turn(0)
    T.cpu()
    ok &= check("電腦下完回合、輪到你（第 3 回合）", g.current == 0 and g.turn == 3, (g.turn, g.current, T.texts()[-10:]))
    ok &= check("電腦場上有寶可夢", g.sides[1].active() is not None)
    print("     電腦這回合：" + " / ".join(l["text"] for l in g.log if l["who"] == 1 and l["turn"] == 2))

    print("3. 你攻擊：傷害算在電腦的寶可夢上")
    cpu_active = g.sides[1].active()
    before = cpu_active.get("damage", 0)
    name = top(cpu_active)
    g.human_attack(0)
    T.cpu()
    hit = [t for t in T.texts("damage") if t.startswith(name)]
    ok &= check(f"啄 → {name} 受到傷害", bool(hit) or g.sides[1].slots["active"]["damage"] != before, T.texts()[-8:])

    print("4. 電腦把你的戰鬥寶可夢打倒 → 你換上新的寶可夢")
    T = Table(seed=7)
    g = T.g
    T.hold(1.5, active="刺梭魚", bench1="快龍V")
    g.start(first=1)
    T.cpu()                                                       # 電腦先攻第一回合（不能攻擊）
    g.end_turn(0)                                                 # 你的第 2 回合結束 → 電腦第 3 回合（這裡先不讓 AI 自己下）
    a = g.sides[1].slots["active"]
    a["energy"] = ["基本草能量"] * 3 + ["基本鋼能量"] * 3      # 讓電腦一定打得出招
    a["pokemon"] = ["朽木妖V"]
    a["status"] = []
    g.sides[0].slots["active"]["damage"] = 50
    if g.current == 1:
        g.attack(1, 1)
    ok &= check("你的刺梭魚氣絕 → 等你換上新的戰鬥寶可夢", g.phase == "promote" and any("戰鬥場" in t for t in g.todo), (g.phase, g.todo, T.texts()[-6:]))
    ok &= check("電腦拿了獎賞卡", len(g.sides[1].prizes) == 5)
    T.hold(1.0, active=None).hold(0.3, bench1=None).hold(1.5, active="快龍V").hold(5)
    ok &= check("把快龍V移到戰鬥場 → 輪到你", g.phase == "turn" and g.current == 0 and top(g.sides[0].active()) == "快龍V", (g.phase, T.texts()[-5:]))
    ok &= check("換上去不用付撤退費用、沒有警告", not any(e["warn"] for e in T.events[-5:]), [e["text"] for e in T.events])

    print("5. 電腦把你的備戰寶可夢拉到戰鬥場（拖出）")
    T = Table(seed=11)
    g = T.g
    T.hold(1.5, active="刺梭魚", bench1="快龍V")
    T.attach("bench1", "基本雷能量", "快龍V")
    g.start(first=0)
    g.phase, g.current, g.turn = "turn", 1, 4
    g._gust(1, "bench1")
    T.hold(4)
    ok &= check("紀錄先換好，相機還沒看到換卡也不會被改回去", top(g.sides[0].active()) == "快龍V" and not any(e["type"] == "fix" for e in T.events), [e["text"] for e in T.events])
    g.current = 0
    T.hold(0.5, active=None, bench1=None).hold(1.5, active="快龍V", bench1="刺梭魚").hold(5)
    ok &= check("你實際把卡換好：不扣能量、沒有警告", g.sides[0].slots["active"]["energy"] == ["基本雷能量"] and not any(e["warn"] for e in T.events), [e["text"] for e in T.events])

    print("6. 你的招式要選目標（拖出）、寶可夢捕捉器")
    T = Table(seed=5)
    g = T.g
    T.hold(1.5, active="阿勃梭魯", bench1="刺梭魚")
    g.start(first=1)
    T.cpu()
    for e in ["基本惡能量", "基本水能量"]:
        g.sides[0].slots["active"]["energy"].append(e)
    cpu = g.sides[1]
    if len(cpu.bench()) < 2:
        from board import empty_slot
        cpu.slots["bench4"] = dict(empty_slot(), pokemon=["綠毛蟲"])
        cpu.slots["bench5"] = dict(empty_slot(), pokemon=["銅象"])
    g.human_attack(0)
    ok &= check("跳出選擇：選電腦的備戰寶可夢", g.choice and g.choice["kind"] == "attack", g.choice)
    pick = g.choice["options"][0]["id"]
    target = top(cpu.slots[pick])
    g.resolve_choice(pick)
    ok &= check(f"{target} 被拉到戰鬥場並受到 30 傷害", top(cpu.slots["active"]) == target and cpu.slots["active"]["damage"] >= 30 or any(target in t and "氣絕" in t for t in T.texts()), T.texts()[-6:])
    T.cpu()

    print("7. 真人用訓練家卡 → 系統提示怎麼做")
    T = Table(seed=2)
    g = T.g
    T.hold(1.5, active="刺梭魚")
    g.start(first=0)
    T.hold(1.2, trainer="博士的研究").hold(1.0, trainer=None)
    ok &= check("先攻第一回合用支援者 → 警告", any("先攻的第一回合不能使用支援者" in t for t in T.texts("warn")), T.texts())
    T.hold(1.2, trainer="超級球").hold(1, trainer=None)
    ok &= check("物品卡效果寫進待辦", any("超級球" in t or "牌庫上方 7 張" in t for t in g.todo) or len(g.todo) >= 2, g.todo)
    g.end_turn(0)
    T.cpu()
    ok &= check("你的回合結束時待辦清掉、之後電腦回合產生的待辦保留", all("超級球" not in t for t in g.todo), g.todo)

    print("8. 完整對局：真人照電腦 AI 的建議亂數下（自動化），直到分出勝負")
    wins = 0
    for seed in range(20):
        g = Game(BOOK, DECKS, "快龍V牌組", "朽木妖V牌組", p0_physical=False, seed=seed)
        g.ai = AI()
        # 這裡只確認：引擎在混合（一邊 promote 流程）時不會卡住 → 用數位模擬
        g.digital_setup(0, g.ai); g.digital_setup(1, g.ai); g.start()
        for _ in range(400):
            if g.phase == "over":
                break
            g.ai.take_turn(g, g.current)
        wins += g.phase == "over"
    ok &= check("20 局都分出勝負", wins == 20, wins)

    print("\n全部通過 ✅" if ok else "\n有失敗 ❌")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
