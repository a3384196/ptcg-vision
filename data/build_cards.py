"""
從使用者實拍照片整理的卡片資料（劍盾 V起始牌組：快龍V 牌組、朽木妖V 牌組）
輸出：cards.json（卡片資料）、decks.json（兩副牌組）、name_fixes.json（註冊名 → 正式卡名）

effects 是給規則引擎執行的指令（op），text 是卡面原文，兩者並存方便核對。
"""
import json
from pathlib import Path

T = {"草": "grass", "火": "fire", "水": "water", "雷": "lightning", "超": "psychic", "鬥": "fighting",
     "惡": "darkness", "鋼": "metal", "龍": "dragon", "無": "colorless"}


def cost(s):            # "草草無" → ["grass","grass","colorless"]
    return [T[c] for c in s]


def atk(name, c, dmg=0, text="", effects=None, mod=None):
    a = {"name": name, "cost": cost(c), "damage": dmg, "text": text, "effects": effects or []}
    if mod:
        a["damage_mod"] = mod          # "x"：傷害 = 倍數 × 次數
    return a


def mon(no, name, hp, typ, stage, attacks, weak=None, resist=None, retreat=1, evolves_from=None, ability=None, rule=None):
    return {"no": no, "name": name, "kind": "pokemon", "stage": stage, "evolves_from": evolves_from,
            "hp": hp, "type": T[typ], "rule": rule, "prize": 2 if rule == "V" else 1,
            "weakness": {"type": T[weak], "mult": 2} if weak else None,
            "resistance": {"type": T[resist], "value": -30} if resist else None,
            "retreat": retreat, "ability": ability, "attacks": attacks}


def tr(no, name, kind, text, effects, **extra):
    d = {"no": no, "name": name, "kind": kind, "text": text, "effects": effects}
    d.update(extra)
    return d


ST_POISON = {"op": "status", "target": "opponent_active", "status": ["poisoned"]}

CARDS = [
    # ---------------- 草
    mon("001/159", "綠毛蟲", 50, "草", "basic", [
        atk("群聚", "無", 0, "從自己的牌庫選擇1張「綠毛蟲」卡，放置於備戰區。並且重洗牌庫。",
            [{"op": "search_deck_to_bench", "filter": {"name": "綠毛蟲"}, "max": 1}]),
        atk("蟲咬", "草", 10)], weak="火", retreat=1),
    mon("002/159", "鐵甲蛹", 80, "草", "stage1", [atk("衝撞", "草", 10)], weak="火", retreat=3, evolves_from="綠毛蟲",
        ability={"name": "堅硬身軀", "text": "這隻寶可夢受到招式的傷害「-20」點。",
                 "effects": [{"op": "passive_damage_reduction", "amount": 20}]}),
    mon("003/159", "巴大蝶", 120, "草", "stage2", [atk("起風", "草無", 90)], weak="火", retreat=1, evolves_from="鐵甲蛹",
        ability={"name": "三色鱗粉", "text": "在自己的回合，當從手牌使出這張卡並完成進化時，可使用1次。將對手的戰鬥寶可夢中毒、灼傷與混亂。",
                 "trigger": "on_evolve_from_hand", "optional": True,
                 "effects": [{"op": "status", "target": "opponent_active", "status": ["poisoned", "burned", "confused"]}]}),
    mon("004/159", "蘑蘑菇", 60, "草", "basic", [atk("撞擊", "草", 10), atk("種子炸彈", "草無", 20)], weak="火", retreat=1),
    mon("005/159", "斗笠菇", 120, "草", "stage1", [
        atk("頭錘", "草", 30),
        atk("衝擊打擊", "草草無", 150, "在下個自己的回合，這隻寶可夢無法使用「衝擊打擊」。",
            [{"op": "lock_attack_next_turn", "attack": "衝擊打擊"}])], weak="火", retreat=2, evolves_from="蘑蘑菇"),
    mon("012/159", "朽木妖V", 210, "草", "basic", [
        atk("生命吸取", "草無", 30, "將這隻寶可夢恢復「30」HP。", [{"op": "heal", "target": "self", "amount": 30}]),
        atk("暗影爪", "草草無", 120, "在不看正面的情況下，選擇1張對手的手牌，將其丟棄。",
            [{"op": "discard_opponent_hand_random", "count": 1}])], weak="火", retreat=2, rule="V"),
    # ---------------- 水
    mon("039/159", "伽勒爾 火紅不倒翁", 80, "水", "basic", [
        atk("突擊", "水", 20, "擲1次硬幣若為反面，則這隻寶可夢也受到10點傷害。",
            [{"op": "coin", "on": "tails", "then": [{"op": "self_damage", "amount": 10}]}])], weak="鋼", retreat=2),
    mon("040/159", "伽勒爾 達摩狒狒", 130, "水", "stage1", [
        atk("細雪", "水無", 30, "將對手的戰鬥寶可夢睡眠。", [{"op": "status", "target": "opponent_active", "status": ["asleep"]}]),
        atk("達摩頭擊", "水水無", 130, "若這隻寶可夢身上放置有傷害指示物，則這個招式只需要1個水能量即可使用。",
            [{"op": "alt_cost_if_damaged", "cost": ["water"]}])], weak="鋼", retreat=2, evolves_from="伽勒爾 火紅不倒翁"),
    mon("046/159", "刺梭魚", 60, "水", "basic", [atk("啄", "水", 10)], weak="雷", retreat=1),
    mon("047/159", "戽斗尖梭", 120, "水", "stage1", [atk("突刺", "水", 50)], weak="雷", retreat=1, evolves_from="刺梭魚"),
    mon("050/159", "冰砌鵝", 120, "水", "basic", [
        atk("雪花冰", "水", 20),
        atk("冰塊頭", "水無無", 70, "在下個對手的回合，這隻寶可夢不會受到基礎寶可夢招式的傷害。",
            [{"op": "prevent_damage_next_turn", "from": {"stage": "basic"}}])], weak="鋼", retreat=2),
    # ---------------- 惡
    mon("096/159", "阿勃梭魯", 100, "惡", "basic", [
        atk("拖出", "無無", 0, "選擇對手的1隻備戰寶可夢，與戰鬥寶可夢互換。然後，新上場的寶可夢受到30點傷害。",
            [{"op": "gust_opponent"}, {"op": "damage", "target": "opponent_active", "amount": 30}]),
        atk("劈開", "惡無無", 80)], weak="草", retreat=1),
    mon("097/159", "不良蛙", 60, "惡", "basic", [atk("推擊", "惡", 20)], weak="鬥", retreat=1),
    mon("098/159", "毒骷蛙", 110, "惡", "stage1", [
        atk("惡棍猛毒", "惡", 0, "將對手的戰鬥寶可夢中毒。因這個中毒而放置的傷害指示物的數量改為4個。",
            [{"op": "status", "target": "opponent_active", "status": ["poisoned"], "poison_counters": 4}]),
        atk("增量拳", "惡無無", 90)], weak="鬥", retreat=1, evolves_from="不良蛙"),
    mon("099/159", "破破袋", 70, "惡", "basic", [
        atk("呼朋引伴", "惡", 0, "從自己的牌庫選擇1張基礎寶可夢卡，放置於備戰區。並且重洗牌庫。",
            [{"op": "search_deck_to_bench", "filter": {"stage": "basic"}, "max": 1}]),
        atk("毒之氣息", "惡無", 20, "將對手的戰鬥寶可夢中毒。", [ST_POISON])], weak="鬥", retreat=2),
    mon("100/159", "灰塵山", 120, "惡", "stage1", [
        atk("毒瓦斯", "惡", 30, "將對手的戰鬥寶可夢中毒。", [ST_POISON]),
        atk("污泥旋渦", "惡惡無", 130)], weak="鬥", retreat=3, evolves_from="破破袋"),
    # ---------------- 鋼
    mon("103/159", "伽勒爾 喵喵", 70, "鋼", "basic", [
        atk("叫聲", "鋼", 0, "在下個對手的回合，受到這個招式的寶可夢使用招式的傷害「-20」點。",
            [{"op": "weaken_defender_next_turn", "amount": 20}]),
        atk("劈開", "無無", 20)], weak="火", resist="草", retreat=1),
    mon("104/159", "伽勒爾 喵頭目", 120, "鋼", "stage1", [
        atk("鼓勵", "鋼", 0, "從自己的牌庫選擇最多2張基本能量卡，以任意方式附於自己的寶可夢身上。並且重洗牌庫。",
            [{"op": "search_deck_attach_energy", "filter": {"basic_energy": True}, "max": 2, "to": "any_own"}]),
        atk("鐵頭碰", "無無無", 80)], weak="火", resist="草", retreat=1, evolves_from="伽勒爾 喵喵"),
    mon("105/159", "鑰圈兒", 70, "鋼", "basic", [
        atk("解鎖", "無", 10, "從自己的牌庫抽出2張卡。", [{"op": "draw", "count": 2}])], weak="火", resist="草", retreat=1),
    mon("106/159", "銅象", 100, "鋼", "basic", [
        atk("滾動", "無", 10),
        atk("十萬馬力", "鋼無無", 80, "這隻寶可夢也受到20點傷害。", [{"op": "self_damage", "amount": 20}])],
        weak="火", resist="草", retreat=3),
    mon("107/159", "大王銅象", 190, "鋼", "stage1", [
        atk("怪力", "鋼無無", 90),
        atk("十萬馬力", "鋼無無無", 160, "這隻寶可夢也受到30點傷害。", [{"op": "self_damage", "amount": 30}])],
        weak="火", resist="草", retreat=4, evolves_from="銅象"),
    # ---------------- 龍
    mon("108/159", "快龍V", 230, "龍", "basic", [
        atk("撕裂", "無無", 50, "這個招式的傷害不計算對手的戰鬥寶可夢身上的附加效果。", [{"op": "ignore_defender_effects"}]),
        atk("龍之強風", "水水雷", 250, "自己的所有備戰寶可夢也各受到20點傷害。［備戰寶可夢不計算弱點・抵抗力。］",
            [{"op": "damage", "target": "own_bench_all", "amount": 20}])], weak=None, retreat=3, rule="V"),
    # ---------------- 無
    mon("110/159", "伊布", 50, "無", "basic", [
        atk("連續舞步", "無", 30, "擲硬幣直到出現反面，造成正面出現的次數×30點傷害。",
            [{"op": "flip_until_tails_damage", "per": 30}], mod="x")], weak="鬥", retreat=1),
    mon("112/159", "驚角鹿", 110, "無", "basic", [
        atk("後踢", "無", 20),
        atk("瘋狂奇襲", "無無", 30, "造成對手的戰鬥寶可夢身上附加的能量的數量×30點傷害。",
            [{"op": "damage_per_defender_energy", "per": 30}], mod="x")], weak="鬥", retreat=1),
    mon("113/159", "向尾喵", 60, "無", "basic", [
        atk("胡思亂撞", "無", 30, "擲1次硬幣若為反面，則這個招式失敗。", [{"op": "coin", "on": "tails", "then": [{"op": "attack_fails"}]}])],
        weak="鬥", retreat=1),
    mon("114/159", "優雅貓", 100, "無", "stage1", [
        atk("愛管閒事", "無", 0, "查看對手的手牌，選擇其中1張卡，放回對手的牌庫下方。",
            [{"op": "look_opponent_hand_put_bottom", "count": 1}]),
        atk("連環巴掌", "無無", 50, "擲2次硬幣，造成正面出現的次數×50點傷害。", [{"op": "flip_n_damage", "n": 2, "per": 50}], mod="x")],
        weak="鬥", retreat=1, evolves_from="向尾喵"),

    # ---------------- 物品
    tr("121/159", "能量回收", "item", "從自己的棄牌區選擇最多2張基本能量卡，在給對手看過後加入手牌。",
       [{"op": "discard_to_hand", "filter": {"basic_energy": True}, "max": 2}]),
    tr("122/159", "能量輸送", "item", "從自己的牌庫選擇1張基本能量卡，在給對手看過後加入手牌。並且重洗牌庫。",
       [{"op": "search_deck_to_hand", "filter": {"basic_energy": True}, "max": 1}]),
    tr("124/159", "先機球", "item", "這張卡必須將自己的1張手牌丟棄才可使用。從自己的牌庫選擇1張基礎寶可夢卡，在給對手看過後加入手牌。並且重洗牌庫。",
       [{"op": "search_deck_to_hand", "filter": {"kind": "pokemon", "stage": "basic"}, "max": 1}],
       play_cost=[{"op": "discard_from_hand", "count": 1}]),
    tr("125/159", "超級球", "item", "查看自己的牌庫上方7張卡。選擇其中1張寶可夢卡，在給對手看過後加入手牌。將剩餘卡放回牌庫並重洗。",
       [{"op": "look_top_pick", "look": 7, "filter": {"kind": "pokemon"}, "max": 1}]),
    tr("128/159", "普通釣竿", "item", "從自己的棄牌區選擇最多2張寶可夢卡與最多2張基本能量卡，在給對手看過後放回牌庫並重洗。（可只選擇寶可夢卡或者只選擇基本能量卡。）",
       [{"op": "discard_to_deck", "groups": [{"filter": {"kind": "pokemon"}, "max": 2}, {"filter": {"basic_energy": True}, "max": 2}]}]),
    tr("129/159", "寶可齒輪3.0", "item", "查看自己的牌庫上方7張卡。選擇其中1張支援者卡，在給對手看過後加入手牌。將剩餘卡放回牌庫並重洗。",
       [{"op": "look_top_pick", "look": 7, "filter": {"kind": "supporter"}, "max": 1}]),
    tr("130/159", "寶可夢交替", "item", "將自己的戰鬥寶可夢與備戰寶可夢互換。", [{"op": "switch_own"}]),
    tr("131/159", "寶可夢捕捉器", "item", "擲1次硬幣若為正面，則選擇對手的1隻備戰寶可夢，與戰鬥寶可夢互換。",
       [{"op": "coin", "on": "heads", "then": [{"op": "gust_opponent"}]}]),
    tr("132/159", "救援行李箱", "item", "從自己的棄牌區選擇最多2張HP為「90」以下的寶可夢卡，在給對手看過後加入手牌。",
       [{"op": "discard_to_hand", "filter": {"kind": "pokemon", "hp_max": 90}, "max": 2}]),
    # ---------------- 支援者
    tr("136/159", "奇巴納", "supporter", "這張卡必須在上個對手的回合自己的寶可夢氣絕了才可使用。從自己的棄牌區選擇1張基本能量卡，附於自己的寶可夢身上。然後，從自己的牌庫任意選擇1張卡加入手牌。並且重洗牌庫。",
       [{"op": "attach_from_discard", "filter": {"basic_energy": True}, "max": 1, "to": "any_own"},
        {"op": "search_deck_to_hand", "filter": {}, "max": 1}], condition="own_ko_last_turn"),
    tr("137/159", "可爾妮的氣勢", "supporter", "從牌庫抽卡直到自己的手牌滿6張為止。", [{"op": "draw_until", "hand": 6}]),
    tr("138/159", "莎娜", "supporter", "將自己的手牌全部放回牌庫並重洗。然後，從牌庫抽出5張卡。",
       [{"op": "shuffle_hand_into_deck"}, {"op": "draw", "count": 5}]),
    tr("139/159", "希巴", "supporter", "將自己的手牌全部放回牌庫並重洗。然後，從牌庫抽出4張卡。在上個對手的回合，若自己的寶可夢氣絕了，則改為抽出7張卡。",
       [{"op": "shuffle_hand_into_deck"}, {"op": "draw", "count": 4, "if_own_ko_last_turn": 7}]),
    tr("140/159", "女學生", "supporter", "從自己的牌庫抽出2張卡。若對手剩餘獎賞卡的張數為6張、4張、2張，則再抽出2張卡。",
       [{"op": "draw", "count": 2, "bonus_if_opponent_prizes_in": [6, 4, 2], "bonus": 2}]),
    tr("142/159", "博士的研究", "supporter", "將自己的手牌全部丟棄，從牌庫抽出7張卡。",
       [{"op": "discard_hand"}, {"op": "draw", "count": 7}]),
    tr("144/159", "寶可夢小朋友", "supporter", "從自己的牌庫選擇1張寶可夢卡，在給對手看過後加入手牌。並且重洗牌庫。",
       [{"op": "search_deck_to_hand", "filter": {"kind": "pokemon"}, "max": 1}]),
    tr("145/159", "赫普", "supporter", "從自己的牌庫抽出3張卡。", [{"op": "draw", "count": 3}]),
    tr("149/159", "模仿少女", "supporter", "將自己的手牌全部放回牌庫並重洗。然後，從自己的牌庫抽出與對手的手牌張數相同數量的卡。",
       [{"op": "shuffle_hand_into_deck"}, {"op": "draw_equal_opponent_hand"}]),
    # ---------------- 競技場
    tr("151/159", "草路競技場", "stadium", "雙方玩家在每個自己的回合，可使用1次，可從自己的牌庫選擇1張草屬性的進化寶可夢卡，在給對手看過後加入手牌。並且重洗牌庫。",
       [{"op": "search_deck_to_hand", "filter": {"kind": "pokemon", "type": "grass", "evolution": True}, "max": 1}],
       usage="once_per_turn_each_player"),
    # ---------------- 特殊能量
    tr("152/159", "芳香草能量", "special_energy", "只要這張卡附於寶可夢身上，視為提供1個草能量。附有這張卡的草寶可夢不會陷入特殊狀態，並將受到的特殊狀態全部恢復。",
       [{"op": "grass_holder_status_immune"}], provides=["grass"]),
    tr("159/159", "極光能量", "special_energy", "必須將自己的1張手牌丟棄才可從手牌將這張卡附於寶可夢身上。只要這張卡附於寶可夢身上，視為提供1個所有屬性的能量。",
       [], provides=["any"], attach_cost=[{"op": "discard_from_hand", "count": 1}]),
]
for t, code in [("草", "GRA"), ("水", "WAT"), ("雷", "LIG"), ("惡", "DAR"), ("鋼", "MET")]:
    CARDS.append({"no": code, "name": f"基本{t}能量", "kind": "energy", "provides": [T[t]], "text": "", "effects": []})

# 兩副牌組（使用者實際卡表，已換成正式卡名）
DECKS = {
    "快龍V牌組": {"不良蛙": 2, "伊布": 1, "伽勒爾 火紅不倒翁": 2, "伽勒爾 達摩狒狒": 1, "優雅貓": 1, "冰砌鵝": 1, "刺梭魚": 2,
               "向尾喵": 2, "快龍V": 1, "戽斗尖梭": 1, "毒骷蛙": 1, "灰塵山": 2, "破破袋": 2, "阿勃梭魯": 1,
               "先機球": 1, "寶可夢交替": 2, "寶可夢捕捉器": 2, "救援行李箱": 1, "普通釣竿": 1, "能量回收": 1, "能量輸送": 1, "超級球": 1,
               "博士的研究": 2, "可爾妮的氣勢": 2, "奇巴納": 1, "寶可夢小朋友": 2, "模仿少女": 1, "莎娜": 1, "赫普": 2,
               "基本惡能量": 7, "基本水能量": 6, "基本雷能量": 4, "極光能量": 2},
    "朽木妖V牌組": {"伽勒爾 喵喵": 3, "伽勒爾 喵頭目": 2, "巴大蝶": 2, "大王銅象": 1, "蘑蘑菇": 2, "斗笠菇": 1, "朽木妖V": 1,
                "綠毛蟲": 3, "銅象": 2, "鐵甲蛹": 2, "鑰圈兒": 1, "驚角鹿": 1,
                "先機球": 1, "寶可夢交替": 2, "寶可夢捕捉器": 2, "寶可齒輪3.0": 1, "救援行李箱": 1, "普通釣竿": 1, "超級球": 1,
                "博士的研究": 2, "女學生": 1, "寶可夢小朋友": 2, "希巴": 2, "莎娜": 1, "赫普": 2, "草路競技場": 1,
                "基本草能量": 10, "基本鋼能量": 7, "極光能量": 1, "芳香草能量": 1},
}

# 註冊時輸入的名字 → 正式卡名（卡面寫法）
NAME_FIXES = {
    "伽勒火紅不倒翁": "伽勒爾 火紅不倒翁", "伽勒達摩沸沸": "伽勒爾 達摩狒狒", "伽勒爾喵喵": "伽勒爾 喵喵", "伽勒爾貓頭目": "伽勒爾 喵頭目",
    "八大蝶": "巴大蝶", "摩摩菇": "蘑蘑菇", "銅像": "銅象", "大王銅像": "大王銅象", "毒菇蛙": "毒骷蛙",
    "寶可夢交換": "寶可夢交替", "及光能量": "極光能量", "芳香能量": "芳香草能量",
    "草基本能量": "基本草能量", "水基本能量": "基本水能量", "電基本能量": "基本雷能量", "惡基本能量": "基本惡能量", "鋼基本能量": "基本鋼能量",
}

if __name__ == "__main__":
    names = {c["name"] for c in CARDS}
    assert len(names) == len(CARDS), "卡名重複"
    for d, m in DECKS.items():
        missing = [n for n in m if n not in names]
        assert not missing, (d, missing)
        assert sum(m.values()) == 60, (d, sum(m.values()))
    for c in CARDS:
        if c["kind"] == "pokemon" and c["evolves_from"]:
            assert c["evolves_from"] in names, c["name"]
    out = Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)
    (out / "cards.json").write_text(json.dumps(CARDS, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "decks.json").write_text(json.dumps(DECKS, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "name_fixes.json").write_text(json.dumps(NAME_FIXES, ensure_ascii=False, indent=1), encoding="utf-8")
    from collections import Counter
    print("卡片種類：", len(CARDS), dict(Counter(c["kind"] for c in CARDS)))
    for d, m in DECKS.items():
        print(d, sum(m.values()), "張")
