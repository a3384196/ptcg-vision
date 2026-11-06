"""
對戰墊動作判斷的情境測試（不需要相機）：模擬每 0.1 秒一格畫面，各區域看到什麼
執行：python tests/test_board.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board import Board, CardBook, UNKNOWN  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"
FPS = 10


class Sim:
    def __init__(self):
        self.b = Board(CardBook(DATA))
        self.t = 0.0
        self.view = {z: None for z in ["active", "bench1", "bench2", "bench3", "bench4", "bench5", "trainer", "discard"]}
        self.log = []

    def hold(self, sec, **zones):
        """接下來 sec 秒，畫面維持 zones 指定的內容"""
        self.view.update(zones)
        for _ in range(int(sec * FPS)):
            self.t += 1 / FPS
            self.log += self.b.update(dict(self.view), self.t)
        return self

    def types(self):
        return [e["type"] for e in self.log]

    def texts(self):
        return [e["text"] for e in self.log]


def check(name, cond, info=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"\n     {info}"))
    return cond


def main():
    ok = True

    print("1. 放寶可夢、貼能量（疊上去再塞到下面）、進化")
    s = Sim()
    s.hold(1.2, bench1="綠毛蟲")
    s.hold(0.3, bench1=UNKNOWN)                 # 手拿著能量靠近，看不清楚
    s.hold(1.2, bench1="基本草能量")             # 能量疊在上面
    s.hold(0.4, bench1=None)                    # 塞的過程中短暫偵測不到
    s.hold(1.2, bench1="綠毛蟲")                 # 塞好，看到原本的寶可夢
    s.hold(1.2, bench1="鐵甲蛹")                 # 進化
    s.hold(1.2, bench1="巴大蝶")                 # 再進化
    ok &= check("動作順序正確", s.types() == ["place", "attach_energy", "evolve", "evolve"], s.texts())
    st = s.b.slots["bench1"]
    ok &= check("場面：巴大蝶帶 1 張草能量", st["pokemon"] == ["綠毛蟲", "鐵甲蛹", "巴大蝶"] and st["energy"] == ["基本草能量"], st)

    print("2. 撤退／交替：戰鬥場與備戰區同時交換")
    s = Sim()
    s.hold(1.2, active="快龍V", bench2="不良蛙")
    s.hold(1.2, active="基本雷能量").hold(1.2, active="快龍V")
    s.hold(0.5, active=None, bench2=None)        # 兩張同時拿起來
    s.hold(1.5, active="不良蛙", bench2="快龍V")  # 交換放下
    s.hold(5)
    ok &= check("產生兩個換位，沒有離場", s.types().count("move") == 2 and "leave" not in s.types(), s.texts())
    ok &= check("快龍V 的能量跟著移到備戰2", s.b.slots["bench2"]["energy"] == ["基本雷能量"], s.b.slots)

    print("2b. 慢慢交換：兩隻都拿起來 2 秒才放下")
    s = Sim()
    s.hold(1.2, active="冰砌鵝", bench1="阿勃梭魯").hold(1.2, bench1="基本惡能量").hold(1.2, bench1="阿勃梭魯")
    s.hold(2.0, active=None, bench1=None).hold(1.5, active="阿勃梭魯", bench1="冰砌鵝").hold(5)
    ok &= check("兩個換位、沒有離場", s.types().count("move") == 2 and "leave" not in s.types(), s.texts())
    ok &= check("阿勃梭魯帶著能量到戰鬥場", s.b.slots["active"]["energy"] == ["基本惡能量"], s.b.slots["active"])

    print("3. 快速移動：舊位置還沒判定為空就出現在新位置")
    s = Sim()
    s.hold(1.2, bench3="刺梭魚")
    s.hold(0.2, bench3=None)
    s.hold(1.2, active="刺梭魚")                 # 1.2 秒 < OFF_DELAY，備戰3 還沒正式算空
    s.hold(3)
    ok &= check("判定為換位", s.types() == ["place", "move"], s.texts())
    ok &= check("備戰3 已清空", s.b.slots["bench3"]["pokemon"] == [], s.b.slots["bench3"])

    print("4. 寶可夢氣絕／收回：離開後一段時間沒出現在別格")
    s = Sim()
    s.hold(1.2, active="伊布")
    s.hold(6, active=None)
    ok &= check("產生離場", s.types() == ["place", "leave"], s.texts())

    print("5. 訓練家卡")
    s = Sim()
    s.hold(1.2, trainer="博士的研究").hold(2, trainer=None).hold(1.2, trainer="超級球")
    ok &= check("使用兩張訓練家卡", s.types() == ["play_trainer", "play_trainer"], s.texts())

    print("6. 防誤判")
    s = Sim()
    s.hold(0.5, bench1="綠毛蟲").hold(0.5, bench1=None)          # 只晃過 0.5 秒
    ok &= check("卡片一閃而過不算數", s.types() == [], s.texts())
    s.hold(1.2, bench1="綠毛蟲").hold(1.0, bench1=None).hold(1.2, bench1="綠毛蟲")   # 手擋住 1 秒
    ok &= check("短暫被手擋住不算離場", s.types() == ["place"], s.texts())
    s.hold(1.2, bench4="巴大蝶")
    ok &= check("進化卡直接放到場上會警告", s.log[-1]["warn"] and "進化寶可夢" in s.log[-1]["text"], s.texts())
    s.hold(1.2, bench5="基本草能量")
    ok &= check("能量放在空格會警告", s.log[-1]["warn"], s.texts())
    s.hold(1.2, bench1="斗笠菇")
    ok &= check("錯誤的進化會警告", s.log[-1]["warn"] and "不能從" in s.log[-1]["text"], s.texts())
    s.hold(1.2, trainer="快龍V")
    ok &= check("寶可夢放進訓練家區會警告", s.log[-1]["warn"], s.texts())

    print("7. 舊註冊名也能用（自動轉成正式卡名）")
    s = Sim()
    s.hold(1.2, bench1="銅像").hold(1.2, bench1="大王銅像")
    ok &= check("銅像 → 銅象、大王銅像 → 大王銅象，進化成立", s.types() == ["place", "evolve"], s.texts())

    print("8. 撤退費用：能量種類一樣 → 自動扣")
    s = Sim()
    s.hold(1.2, active="斗笠菇", bench1="驚角鹿")
    for _ in range(3):
        s.hold(1.2, active="基本草能量").hold(1.2, active="斗笠菇")
    s.hold(0.4, active=None, bench1=None).hold(1.5, active="驚角鹿", bench1="斗笠菇").hold(5)
    ok &= check("斗笠菇撤退（撤退 2）丟 2 張草能量，剩 1 張", s.b.slots["bench1"]["energy"] == ["基本草能量"] and "retreat_cost" in s.types(), s.texts())

    print("9. 撤退費用：能量種類不同 → 讓玩家選")
    s = Sim()
    s.hold(1.2, active="斗笠菇", bench1="驚角鹿")
    for e in ["基本草能量", "極光能量", "芳香草能量"]:
        s.hold(1.2, active=e).hold(1.2, active="斗笠菇")
    s.hold(0.4, active=None, bench1=None).hold(1.5, active="驚角鹿", bench1="斗笠菇").hold(5)
    c = s.b.choice
    ok &= check("跳出選擇：要丟 2 張", c and c["count"] == 2 and c["options"] == ["基本草能量", "極光能量", "芳香草能量"], c)
    s.log += s.b.resolve([0, 2], s.t)
    ok &= check("選了草能量＋芳香草能量 → 剩極光能量", s.b.slots["bench1"]["energy"] == ["極光能量"] and s.b.choice is None, s.b.slots["bench1"])

    print("10. 剛用寶可夢交替 → 換位不用付撤退費用")
    s = Sim()
    s.hold(1.2, active="銅象", bench1="鑰圈兒")
    s.hold(1.2, active="基本鋼能量").hold(1.2, active="銅象")
    s.hold(1.2, trainer="寶可夢交替").hold(1, trainer=None)
    s.hold(0.4, active=None, bench1=None).hold(1.5, active="鑰圈兒", bench1="銅象").hold(5)
    ok &= check("能量沒被扣、沒有警告", s.b.slots["bench1"]["energy"] == ["基本鋼能量"] and not any(e["warn"] for e in s.log), s.texts())

    print("11. 能量不夠撤退 → 警告")
    s = Sim()
    s.hold(1.2, active="銅象", bench1="鑰圈兒")
    s.hold(0.4, active=None, bench1=None).hold(1.5, active="鑰圈兒", bench1="銅象").hold(5)
    ok &= check("警告能量不足", any(e["warn"] and "撤退需要 3" in e["text"] for e in s.log), s.texts())

    print("12. 補救：疊能量時被誤判成空格（手擋了 2.5 秒）")
    s = Sim()
    s.hold(1.2, bench1="綠毛蟲").hold(2.5, bench1=None).hold(1.2, bench1="基本草能量").hold(1.2, bench1="綠毛蟲").hold(5)
    ok &= check("能量正確貼上，沒有警告也沒有離場", s.types() == ["place", "attach_energy"] and s.b.slots["bench1"]["energy"] == ["基本草能量"], s.texts())
    s = Sim()
    s.hold(1.2, bench2="刺梭魚").hold(2.5, bench2=None).hold(1.2, bench2="戽斗尖梭").hold(5)
    ok &= check("進化也一樣能接回來", s.types() == ["place", "evolve"], s.texts())

    print("13. 復原")
    s = Sim()
    s.hold(1.2, bench1="綠毛蟲").hold(1.2, bench1="基本草能量").hold(1.2, bench1="綠毛蟲")
    s.log += s.b.undo(s.t)
    ok &= check("復原後能量拿掉了", s.b.slots["bench1"]["energy"] == [] and s.b.slots["bench1"]["pokemon"] == ["綠毛蟲"], s.b.slots["bench1"])

    print("14. 自動校正：相機看到的跟紀錄不同步")
    s = Sim()
    s.hold(1.2, active="快龍V")
    s.b.slots["active"] = {"pokemon": [], "energy": [], "tool": []}          # 模擬：不知道為什麼紀錄被清掉了
    s.hold(3)
    ok &= check("紀錄空了但相機一直看到快龍V → 補回來", s.b.slots["active"]["pokemon"] == ["快龍V"] and s.types()[-1] == "fix", s.texts())

    s = Sim()
    s.hold(1.2, active="快龍V", bench1="伊布")
    s.b.slots["active"], s.b.slots["bench1"] = s.b.slots["bench1"], s.b.slots["active"]   # 模擬：紀錄把兩隻對調錯了
    s.hold(3).hold(5)
    ok &= check("兩格紀錄錯位 → 校正回相機看到的", s.b.slots["active"]["pokemon"] == ["快龍V"] and s.b.slots["bench1"]["pokemon"] == ["伊布"], s.texts())

    s = Sim()
    s.hold(1.2, bench2="刺梭魚")
    s.b.watches["bench2"].committed = "戽斗尖梭"; s.b.watches["bench2"].cand = "戽斗尖梭"   # 模擬：漏掉了進化
    s.hold(3, bench2="戽斗尖梭")
    ok &= check("漏掉的進化 → 補上（保留進化鏈）", s.b.slots["bench2"]["pokemon"] == ["刺梭魚", "戽斗尖梭"], s.texts())

    print("15. 按了復原的格子不會被自動校正改回去")
    s = Sim()
    s.hold(1.2, bench3="刺梭魚").hold(1.2, bench3="戽斗尖梭")
    s.log += s.b.undo(s.t)
    s.hold(5)
    ok &= check("復原後維持刺梭魚", s.b.slots["bench3"]["pokemon"] == ["刺梭魚"] and "fix" not in s.types(), s.texts())
    s.hold(1.2, bench3=None).hold(6).hold(1.2, bench3="伊布").hold(3)
    ok &= check("之後換了別的卡，正常運作", s.b.slots["bench3"]["pokemon"] == ["伊布"], s.texts())

    print("\n全部通過 ✅" if ok else "\n有失敗 ❌")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
