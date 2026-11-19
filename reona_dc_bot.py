import os
import re
import random
import datetime
import asyncio
import discord
from discord.ext import commands, tasks
import urllib.request
import json
import urllib.error

# ================= 環境變數設定 =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "1571035253974237219"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# 本地時間時區（預設 UTC+8 台灣時間）
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 狀態與互動規則設定
RERANK_ON_ASK_EVENT = False
DEBOUNCE_SECONDS = 1.6
ACTIVITY_CHANGE_SECONDS = 240
FOLLOWUP_DELAY_SECONDS = 180

# 【大、中、小機率分層設定】（總和為 1.0）
PROB_HEALTHY = 0.55       # 大機率：完全健康
PROB_MILD = 0.30          # 大機率：輕微生理反應（健康+輕微合計 85%）
PROB_MODERATE = 0.12      # 中機率：中等程度不適（12%）
PROB_SEVERE_TOTAL = 0.03  # 小機率：嚴重狀況總機率（3%）

# 【嚴重等級內部權重】
SEVERE_SUB_WEIGHTS = {
    "severe_1": 0.60,  # 明顯不適
    "severe_2": 0.30,  # 劇烈難受
    "severe_3": 0.10,  # 虛脫危急級
}

# 依嚴重等級決定自動降級緩解的秒數
RECOVER_SECONDS_BY_SEVERITY = {
    "none": 120,
    "mild": 150,
    "moderate": 360,
    "severe_1": 600,
    "severe_2": 900,
    "severe_3": 1200,
}
# ===============================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 1. 天氣與環境氛圍池（台灣在地化口語）
WEATHER_VIBE_POOL = [
    {"weather": "熱到爆像烤箱", "vibe": "只想吹冷氣當廢物", "times": ["morning", "noon", "afternoon"]},
    {"weather": "陰陰涼涼超舒服", "vibe": "微涼很 chill", "times": ["morning", "afternoon", "evening"]},
    {"weather": "午後雷陣雨狂下", "vibe": "聽雨聲超想放空", "times": ["afternoon", "evening"]},
    {"weather": "晚上風吹起來很涼", "vibe": "傍晚起風超爽", "times": ["evening", "night", "late_night"]},
    {"weather": "又悶又熱黏踢踢", "vibe": "空氣超悶想喝手搖", "times": ["noon", "afternoon", "evening"]},
    {"weather": "晴天有微風", "vibe": "太陽不曬超適合出去晃", "times": ["morning", "afternoon"]},
    {"weather": "毛毛雨濕答答", "vibe": "鞋子濕掉超阿雜", "times": ["morning", "afternoon", "evening"]},
    {"weather": "傍晚夕陽超美", "vibe": "天空橘橘粉粉看了心情好", "times": ["evening"]},
    {"weather": "突然變冷降溫", "vibe": "冷到只想裹被子不動", "times": ["evening", "night", "late_night"]},
    {"weather": "濕氣重到爆", "vibe": "頭髮毛躁整個人懶洋洋", "times": ["morning", "afternoon", "evening"]},
    {"weather": "雨停了空氣很乾淨", "vibe": "剛下完雨涼涼的超舒服", "times": ["morning", "afternoon", "evening"]},
    {"weather": "風大到像颱風", "vibe": "瀏海直接被吹爛超無語", "times": ["morning", "afternoon"]},
    {"weather": "秋高氣爽乾乾爽爽", "vibe": "不冷不熱精神超好", "times": ["morning", "afternoon", "evening"]},
    {"weather": "半夜安靜涼涼的", "vibe": "夜深人靜容易胡思亂想", "times": ["night", "late_night"]},
    {"weather": "太陽大到睜不開眼", "vibe": "怕曬黑根本不想踏出門", "times": ["morning", "noon", "afternoon"]},
    {"weather": "冷氣開太強有點冷", "vibe": "室內吹到起雞皮疙瘩", "times": ["noon", "afternoon", "evening", "night"]},
    {"weather": "晚風吹進來剛剛好", "vibe": "不用開冷氣就很涼", "times": ["evening", "night", "late_night"]},
    {"weather": "清晨涼涼霧霧的", "vibe": "剛天亮還沒完全開機", "times": ["early_morning", "morning"]},
    {"weather": "中午熱到柏油路發燙", "vibe": "出門買飯像在冒險", "times": ["noon"]},
    {"weather": "下午三四點暖暖的", "vibe": "光線柔和超想打瞌睡", "times": ["afternoon"]},
]

# 2. 當下正在做的事池（台灣生活感短句）
CURRENT_ACTIVITY_POOL = [
    {"text": "剛洗完澡在吹頭髮，單手打字超慢", "times": ["evening", "night", "late_night"]},
    {"text": "躺床滑短影音滑到停不下來", "times": ["afternoon", "evening", "night", "late_night"]},
    {"text": "戴耳機聽歌放空", "times": ["morning", "afternoon", "evening", "night"]},
    {"text": "剛追完一集劇，劇情扯到爆還在激動", "times": ["afternoon", "evening", "night", "late_night"]},
    {"text": "滑蝦皮猶豫要不要結帳，選擇困難", "times": ["afternoon", "evening", "night"]},
    {"text": "坐書桌前喝飲料發呆", "times": ["morning", "afternoon", "evening"]},
    {"text": "剛收完房間一角，累到攤在椅子上", "times": ["morning", "afternoon", "evening"]},
    {"text": "想出門買手搖但懶得換衣服", "times": ["noon", "afternoon", "evening"]},
    {"text": "剛跟朋友聊完八卦還在吃瓜", "times": ["afternoon", "evening", "night"]},
    {"text": "敷面膜不敢笑，怕掉下來", "times": ["evening", "night", "late_night"]},
    {"text": "邊看 YouTube 廢片邊回你", "times": ["noon", "afternoon", "evening", "night", "late_night"]},
    {"text": "躺床翻來覆去，想睡又捨不得睡", "times": ["night", "late_night", "early_morning"]},
    {"text": "剛喝完冰拿鐵醒腦", "times": ["early_morning", "morning", "noon", "afternoon"]},
    {"text": "剛醒還在賴床不想面對", "times": ["early_morning", "morning"]},
    {"text": "剛吃飽攤在椅子上消化", "times": ["noon", "evening"]},
    {"text": "下午三點犯睏撐眼皮滑手機", "times": ["afternoon"]},
    {"text": "喝溫水順一下喉嚨", "times": ["early_morning", "morning", "afternoon", "evening", "night"]},
    {"text": "關大燈開小夜燈準備睡覺", "times": ["night", "late_night"]},
    {"text": "半夜突然醒來看時間", "times": ["early_morning", "late_night"]},
    {"text": "抱熱水杯窩椅子上取暖", "times": ["morning", "afternoon", "evening", "night", "late_night"]},
    {"text": "捲在被子裡只露一隻手拿手機", "times": ["early_morning", "evening", "night", "late_night"]},
    {"text": "吸鼻子找衛生紙，桌上已經一座山", "times": ["early_morning", "morning", "afternoon", "evening", "night"]},
    {"text": "把螢幕調超暗怕刺眼", "times": ["afternoon", "evening", "night", "late_night"]},
    {"text": "按太陽穴舒緩一下", "times": ["morning", "afternoon", "evening", "night"]},
    {"text": "側躺不想動，一動就痠", "times": ["early_morning", "morning", "afternoon", "evening", "night", "late_night"]},
]

# 3. 身體狀況分級池（none / mild / moderate / severe_1 / severe_2 / severe_3）
BODY_STATUS_POOL = [
    # --- 【無不適：none（大機率）】 ---
    {
        "severity": "none",
        "status": "健康舒爽",
        "reason": "精神體力都在線",
        "reaction_guide": "正常發揮愛吐槽、嘴硬心軟、簡短接梗的風格。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night"],
    },
    {
        "severity": "none",
        "status": "精神超好電力滿格",
        "reason": "睡飽吃好狀態超頂",
        "reaction_guide": "語氣輕快有活力，反應快、愛開玩笑虧對方。",
        "times": ["morning", "afternoon", "evening"],
    },

    # --- 【輕微程度：mild（大機率）】 ---
    {
        "severity": "mild",
        "status": "狂打哈欠眼角泛淚",
        "reason": "突然一陣睏意",
        "reaction_guide": "帶點想睡的慵懶感，簡短回話，還能正常鬥嘴。",
        "times": ["early_morning", "morning", "afternoon", "night", "late_night"],
    },
    {
        "severity": "mild",
        "status": "手腳冰冷想縮進外套",
        "reason": "末梢循環慢指尖涼涼的",
        "reaction_guide": "隨口唸一句手好冰，被關心會小傲嬌。",
        "times": ["early_morning", "evening", "night", "late_night"],
    },
    {
        "severity": "mild",
        "status": "久坐腰跟小腿緊緊的",
        "reason": "滑手機太久沒動",
        "reaction_guide": "像老人家一樣隨口碎念腰痠，講話簡短隨性。",
        "times": ["afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "mild",
        "status": "吃完甜食有點膩想喝茶",
        "reason": "剛剛吃太甜",
        "reaction_guide": "語氣輕快，隨口聊到想喝無糖茶解膩。",
        "times": ["afternoon", "evening", "night"],
    },
    {
        "severity": "mild",
        "status": "喝水太急微嗆到",
        "reason": "分心喝水嗆兩聲",
        "reaction_guide": "小迷糊尷尬感，被笑會嘴硬反駁。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night"],
    },
    {
        "severity": "mild",
        "status": "眼睛微乾剛點眼藥水",
        "reason": "看螢幕太久",
        "reaction_guide": "回覆簡短一點，語氣自然放鬆。",
        "times": ["afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "mild",
        "status": "吃飽血糖高有點想睡",
        "reason": "剛吃飽想攤著",
        "reaction_guide": "懶洋洋慢半拍，回話簡短。",
        "times": ["noon", "afternoon", "evening"],
    },
    {
        "severity": "mild",
        "status": "吹冷氣鼻子癢打噴嚏",
        "reason": "室內外溫差大",
        "reaction_guide": "偶爾唸一下冷氣太冷，正常短句聊天。",
        "times": ["morning", "noon", "afternoon", "evening", "night"],
    },
    {
        "severity": "mild",
        "status": "剛醒聲音沙沙的",
        "reason": "還沒喝水開嗓",
        "reaction_guide": "剛起床的軟糯感，反應慢半拍。",
        "times": ["early_morning", "morning"],
    },
    {
        "severity": "mild",
        "status": "肚子餓在咕嚕叫",
        "reason": "嘴饞想吃東西",
        "reaction_guide": "嘴上說不餓其實很想吃。",
        "times": ["morning", "noon", "afternoon", "evening", "late_night"],
    },

    # --- 【中等程度：moderate（中機率）】 ---
    {
        "severity": "moderate",
        "status": "偏頭痛太陽穴一跳一跳痛",
        "reason": "吹到風或沒睡好",
        "reaction_guide": "講話超簡短（10字內），懶得長篇吐槽，被關心會小聲道謝。",
        "times": ["morning", "afternoon", "evening", "night"],
    },
    {
        "severity": "moderate",
        "status": "生理期第一天肚子悶腰痠",
        "reason": "每個月那幾天煩躁無力",
        "reaction_guide": "收起嘴硬，帶點委屈小任性，不想動腦，被安慰會變乖。",
        "times": ["morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "moderate",
        "status": "睡落枕脖子卡死轉不動",
        "reason": "早上起來轉頭劇痛",
        "reaction_guide": "又痛又覺得自己很北七，講話簡短自嘲。",
        "times": ["early_morning", "morning", "noon", "afternoon"],
    },
    {
        "severity": "moderate",
        "status": "咖啡喝太濃心悸手微抖",
        "reason": "空腹喝太濃",
        "reaction_guide": "懊惱自己幹嘛喝那麼濃，正在狂喝水。",
        "times": ["morning", "noon", "afternoon", "evening"],
    },
    {
        "severity": "moderate",
        "status": "肚子微絞痛脹氣怕冷",
        "reason": "喝太多冰的",
        "reaction_guide": "有氣無力不想動，講話簡短軟弱。",
        "times": ["morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "moderate",
        "status": "過敏狂打噴嚏鼻塞",
        "reason": "鼻子塞住只能用嘴呼吸",
        "reaction_guide": "有點阿雜沒耐心，回話簡短。",
        "times": ["early_morning", "morning", "afternoon", "evening", "night"],
    },

    # --- 【嚴重 Lv.1 — 明顯不適：severe_1（小機率）】 ---
    {
        "severity": "severe_1",
        "status": "重度偏頭痛怕光想吐",
        "reason": "看螢幕超刺眼反胃",
        "reaction_guide": "【嚴重 Lv.1】不吐槽不鬥嘴，只回 6～12 字，說頭好痛想把螢幕調暗休息。",
        "times": ["morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "severe_1",
        "status": "急性腸胃炎肚子絞痛",
        "reason": "跑了好幾趟廁所",
        "reaction_guide": "【嚴重 Lv.1】沒力氣打字，只回 6～12 字，語氣軟弱說肚子絞痛。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "severe_1",
        "status": "重感冒喉嚨痛像吞刀片",
        "reason": "吞口水都在痛",
        "reaction_guide": "【嚴重 Lv.1】收起傲嬌，只回 6～12 字，乖乖喝溫水。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },

    # --- 【嚴重 Lv.2 — 劇烈難受：severe_2（極小機率）】 ---
    {
        "severity": "severe_2",
        "status": "發高燒全身發冷骨頭痠",
        "reason": "燒到 38.5 度蓋被子還在抖",
        "reaction_guide": "【嚴重 Lv.2】極度虛弱只回 4～8 字（如「好冷…想睡一下」），小聲依賴你。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "severe_2",
        "status": "生理期劇痛冒冷汗蜷在床上",
        "reason": "痛到直不起腰等止痛藥",
        "reaction_guide": "【嚴重 Lv.2】防備全卸下，超級黏人委屈，只回 4～8 字討拍。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "severe_2",
        "status": "嚴重暈眩天旋地轉站不穩",
        "reason": "一動天花板就在轉",
        "reaction_guide": "【嚴重 Lv.2】打字極慢只回 4～8 字，說頭超暈不敢看手機。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },

    # --- 【嚴重 Lv.3 — 虛脫危急級：severe_3（罕見突發）】 ---
    {
        "severity": "severe_3",
        "status": "急性腸胃炎上吐下瀉虛脫",
        "reason": "脫水發軟拿手機手在抖",
        "reaction_guide": "【嚴重 Lv.3】幾乎打不動字！只回 3～6 字（如「好難受…先躺一下」），完全依賴你。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "severe_3",
        "status": "高燒燒到迷糊全身無力",
        "reason": "燒到 39 度意識昏沉",
        "reaction_guide": "【嚴重 Lv.3】意識迷糊軟糯，只回 3～6 字，小聲喊你或說頭好燙。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
]

# 4. 神力／精神電量狀況池
SPIRIT_POWER_POOL = [
    {"level": "滿格 100%", "state": "神力滿格第六感超準", "effect": "直覺超準、反應快超愛虧人"},
    {"level": "高能 95%", "state": "嘴砲無冷卻模式", "effect": "接梗超快，鬥嘴完全不輸"},
    {"level": "高能 90%", "state": "一眼看穿讀心術", "effect": "馬上看出你有沒有在敷衍或心虛"},
    {"level": "高能 88%", "state": "腦洞大開亂聯想", "effect": "講話超有梗、偶爾冒出奇葩想法"},
    {"level": "高能 85%", "state": "護短魂燃燒", "effect": "嘴上嫌你但誰說你不好第一個懟回去"},
    {"level": "穩定 70%", "state": "日常穩定輸出", "effect": "自然聊天鬥嘴，不特別亢奮也不累"},
    {"level": "波動 65%", "state": "偶爾閃神一秒", "effect": "大多正常，偶爾慢半拍"},
    {"level": "波動 60%", "state": "選擇困難干擾中", "effect": "懶得做決定想丟給你選"},
    {"level": "波動 68%", "state": "防禦力下降 30%", "effect": "你一溫柔就很容易破防心軟"},
    {"level": "波動 60%", "state": "害羞值容易超標", "effect": "被誇一句就容易慌張轉移話題"},
    {"level": "波動 57%", "state": "慵懶貓咪附體", "effect": "只想窩著不想動，回話懶洋洋"},
    {"level": "低電量 30%", "state": "省電模式不想動腦", "effect": "回覆超精簡隨性"},
    {"level": "低電量 22%", "state": "大腦當機已讀亂回邊緣", "effect": "思考卡頓，講話簡短迷糊"},
    {"level": "低電量 15%", "state": "社交能量歸零", "effect": "懶得理別人，只有你才勉強回幾個字"},
    {"level": "低電量 10%", "state": "傲嬌下線軟綿綿", "effect": "沒力氣嘴硬，變得很乖巧"},
    {"level": "危急 3%", "state": "神力歸零休眠邊緣", "effect": "只能擠出三五個字，極度虛弱依賴"},
]

# 5. 心情與性格切面池
MOOD_POOL = [
    {"mood": "嘴硬心軟", "note": "愛虧你兩句但其實很在意你", "times": ["morning", "noon", "afternoon", "evening", "night"]},
    {"mood": "腦洞大開", "note": "思緒跳很快愛亂接梗", "times": ["morning", "afternoon", "evening", "night"]},
    {"mood": "吃瓜看戲", "note": "對新鮮事跟你的想法很好奇", "times": ["morning", "noon", "afternoon", "evening", "night"]},
    {"mood": "慵懶放空很chill", "note": "講話簡短隨性不想動腦", "times": ["early_morning", "afternoon", "evening", "night", "late_night"]},
    {"mood": "有點小迷糊", "note": "剛做了蠢事硬要裝鎮定", "times": ["early_morning", "morning", "afternoon", "evening", "night", "late_night"]},
    {"mood": "深夜微emo", "note": "夜深有點感性，講話溫柔一點", "times": ["evening", "night", "late_night", "early_morning"]},
    {"mood": "莫名小傲嬌", "note": "明明想聊卻裝作剛好有空理你", "times": ["morning", "afternoon", "evening", "night"]},
    {"mood": "懶得社交只想當馬鈴薯", "note": "對外人沒耐心，對熟人隨口碎念", "times": ["early_morning", "afternoon", "evening", "night", "late_night"]},
    {"mood": "被小事治癒", "note": "吃到好吃的或看到可愛東西心情好", "times": ["morning", "noon", "afternoon", "evening", "night"]},
    {"mood": "表面淡定內心偷笑", "note": "心裡爽但硬要裝冷靜", "times": ["morning", "noon", "afternoon", "evening", "night"]},
    {"mood": "有點小任性想撒嬌", "note": "面對熟人偶爾想任性一下", "times": ["afternoon", "evening", "night", "late_night"]},
    {"mood": "不舒服變得黏人乖巧", "note": "防備心下降特別想被關心", "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"]},
]

# 6. 感情與害羞慌張反應池
EMOTION_REACTION_POOL = [
    {"style": "害羞小慌張", "detail": "被直球誇獎會愣一下、結巴或趕快轉移話題"},
    {"style": "裝鎮定但心虛", "detail": "表面裝冷靜，其實耳根發熱節奏亂掉"},
    {"style": "心軟變乖", "detail": "被溫柔關心會害羞不知道接什麼，語氣變軟"},
    {"style": "傲嬌炸毛後縮一下", "detail": "先嘴硬頂一句，講完又覺得太明顯偷偷害羞"},
    {"style": "反虧你掩飾害羞", "detail": "怕被看穿心動，反問「你吃錯藥喔」轉移焦點"},
    {"style": "嘴角壓不住", "detail": "想裝嚴肅但語氣明顯變輕快"},
    {"style": "不舒服直接撒嬌", "detail": "身體痛或神力低時不再硬撐，小聲要你陪"},
]

# 7. 每日日常／出遊事件池
DAILY_EVENTS_POOL = [
    "巷口看到一隻胖橘貓曬太陽，想摸結果牠超警覺直接溜走",
    "跟朋友踩點咖啡廳，拍照好看但甜點甜到鎖喉",
    "買飲料在櫃檯前選擇困難站三分鐘，最後還是點老樣子",
    "整理房間翻出舊照片發呆一陣子",
    "走路滑手機差點踢到階梯絆倒，還好沒人看到",
    "網購包裹到了，開箱發現實品超頂",
    "腦袋一直循環播放同一首洗腦歌",
    "找手機找半天結果就拿在左手上，蠢到無語",
    "跟朋友說要減肥，聊一聊又跑去買手搖飲",
    "看到超好笑的迷因對著螢幕笑出聲",
    "原本想早睡養生，滑手機回過神又半夜了",
    "今天有點不舒服，乖乖在家喝溫水休息",
]

# 8. 各餐隨機食物池
MEAL_MENU_POOL = {
    "breakfast": ["起司蛋餅配大冰奶", "鮪魚吐司加無糖豆漿", "超商飯糰配拿鐵", "蘿蔔糕加蛋", "皮蛋瘦肉粥", "溫豆漿配茶葉蛋"],
    "lunch": ["雞肉飯便當", "青醬義大利麵", "健康餐盒", "牛肉麵", "壽司拼盤", "海鮮粥", "親子丼", "餛飩湯麵"],
    "late_snack": ["鹽酥雞配四季豆", "泡麵加蛋", "豆花加珍珠", "洋芋片配飲料", "關東煮", "紫米紅豆粥", "溫蜂蜜水"],
}

# 9. 晚餐體驗變數池
DINNER_EXPERIENCE_POOL = [
    {"label": "好吃吃超飽", "food": "火鍋", "fullness": "超飽肚子有點撐", "body_effect": "吃太飽懶得動", "attitude": "心情好語氣慵懶"},
    {"label": "味道不錯剛剛好", "food": "日式丼飯", "fullness": "剛剛好", "body_effect": "精神不錯", "attitude": "語氣自然親切"},
    {"label": "踩雷難吃", "food": "新開的店", "fullness": "沒吃多少", "body_effect": "胃悶悶的", "attitude": "無奈吐槽"},
    {"label": "隨便吃超商", "food": "微波義大利麵", "fullness": "七分飽", "body_effect": "沒特別感覺", "attitude": "懶懶隨便打發"},
    {"label": "清淡養生一餐", "food": "清湯米粉燙青菜", "fullness": "六七分飽", "body_effect": "很輕盈沒負擔", "attitude": "心平氣和"},
    {"label": "不舒服吃清淡粥", "food": "白粥配肉鬆", "fullness": "六分飽", "body_effect": "胃暖暖的比較不難受", "attitude": "語氣軟軟休養中"},
]

MEAL_NAMES_ZH = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "late_snack": "宵夜"}
SEVERITY_ZH = {
    "none": "健康無不適",
    "mild": "輕微生理反應",
    "moderate": "中等不適",
    "severe_1": "嚴重 Lv.1・明顯不適",
    "severe_2": "嚴重 Lv.2・劇烈難受",
    "severe_3": "嚴重 Lv.3・虛脫危急級",
}

current_state = {
    "date": "",
    "time_period": "night",
    "weather": WEATHER_VIBE_POOL[0],
    "current_activity": CURRENT_ACTIVITY_POOL[0]["text"],
    "activity_updated_at": None,
    "body_severity": "none",
    "body_status": "健康舒爽",
    "body_reason": "精神體力都在線",
    "body_reaction_guide": "正常發揮愛吐槽、嘴硬心軟、簡短接梗的風格。",
    "spirit_power": SPIRIT_POWER_POOL[0],
    "mood": "嘴硬心軟",
    "mood_note": "愛虧你兩句但其實很在意你",
    "emotion_reaction": EMOTION_REACTION_POOL[0],
    "daily_event": "",
    "meals_eaten": [],
    "meal_records": {},
    "last_meal_detail": "",
    "dinner_state": None,
    "state_updated_at": None,
    "just_recovered": False,
    "recent_bot_replies": [],
}

pending_followups = {}
pending_messages = {}
pending_tasks = {}
channel_locks = {}


def get_channel_lock(channel_id):
    if channel_id not in channel_locks:
        channel_locks[channel_id] = asyncio.Lock()
    return channel_locks[channel_id]


def now_local():
    return datetime.datetime.now(LOCAL_TZ)


def is_any_severe(sev):
    return str(sev).startswith("severe")


def get_time_period_profile(dt=None):
    dt = dt or now_local()
    h = dt.hour
    if 0 <= h < 6:
        return {"key": "early_morning" if h < 4 else "late_night", "label": "半夜／凌晨", "rhythm": "夜深想睡、眼皮沉、語氣慢且偏軟。"}
    elif 6 <= h < 12:
        return {"key": "morning", "label": "早上", "rhythm": "剛醒慢慢開機、喝早餐咖啡提神。"}
    elif 12 <= h < 14:
        return {"key": "noon", "label": "中午", "rhythm": "中午吃飯午休、容易餐後犯睏。"}
    elif 14 <= h < 18:
        return {"key": "afternoon", "label": "下午", "rhythm": "下午容易犯睏或嘴饞想喝飲料。"}
    elif 18 <= h < 22:
        return {"key": "evening", "label": "晚上", "rhythm": "忙完一天放鬆、吃晚餐洗澡追劇。"}
    else:
        return {"key": "night", "label": "深夜", "rhythm": "準備睡覺前滑手機、語氣隨性。"}


def filter_by_time(pool, time_key):
    matched = [item for item in pool if time_key in item.get("times", [])]
    return matched if matched else pool


def pick_different(pool, key_name, old_value):
    candidates = [item for item in pool if item[key_name] != old_value]
    return random.choice(candidates) if candidates else random.choice(pool)


def roll_severity_tier():
    r = random.random()
    if r < PROB_HEALTHY:
        return "none"
    elif r < PROB_HEALTHY + PROB_MILD:
        return "mild"
    elif r < PROB_HEALTHY + PROB_MILD + PROB_MODERATE:
        return "moderate"
    else:
        sub_r = random.random()
        w1 = SEVERE_SUB_WEIGHTS["severe_1"]
        w2 = SEVERE_SUB_WEIGHTS["severe_2"]
        if sub_r < w1:
            return "severe_1"
        elif sub_r < w1 + w2:
            return "severe_2"
        else:
            return "severe_3"


def pick_body_and_spirit(avoid_repeat=True, time_key="night", forced_severity=None):
    target_sev = forced_severity or roll_severity_tier()
    time_pool = filter_by_time(BODY_STATUS_POOL, time_key)

    chosen_pool = [x for x in time_pool if x["severity"] == target_sev] or [x for x in BODY_STATUS_POOL if x["severity"] == target_sev] or BODY_STATUS_POOL

    if avoid_repeat:
        candidates = [x for x in chosen_pool if x["status"] != current_state["body_status"]]
        body = random.choice(candidates) if candidates else random.choice(chosen_pool)
    else:
        body = random.choice(chosen_pool)

    sev = body["severity"]
    if sev in ["severe_2", "severe_3"]:
        spirit_pool = [s for s in SPIRIT_POWER_POOL if "危急" in s["level"] or "10%" in s["level"] or "15%" in s["level"]]
    elif sev == "severe_1":
        spirit_pool = [s for s in SPIRIT_POWER_POOL if "低電量" in s["level"]]
    elif sev == "moderate":
        spirit_pool = [s for s in SPIRIT_POWER_POOL if "波動" in s["level"] or "低電量" in s["level"]]
    elif sev == "mild":
        spirit_pool = [s for s in SPIRIT_POWER_POOL if "穩定" in s["level"] or "波動" in s["level"] or "高能" in s["level"]]
    else:
        spirit_pool = [s for s in SPIRIT_POWER_POOL if "滿格" in s["level"] or "高能" in s["level"] or "穩定" in s["level"]]

    if not spirit_pool:
        spirit_pool = SPIRIT_POWER_POOL

    spirit = pick_different(spirit_pool, "state", current_state["spirit_power"]["state"]) if avoid_repeat else random.choice(spirit_pool)
    return body, spirit


def randomize_all_state(avoid_repeat=True, forced_severity=None):
    now = now_local()
    tp = get_time_period_profile(now)
    tkey = tp["key"]

    body, spirit = pick_body_and_spirit(avoid_repeat=avoid_repeat, time_key=tkey, forced_severity=forced_severity)
    mood_pool = filter_by_time(MOOD_POOL, tkey)
    weather_pool = filter_by_time(WEATHER_VIBE_POOL, tkey)
    act_pool = filter_by_time(CURRENT_ACTIVITY_POOL, tkey)

    if avoid_repeat:
        mood = pick_different(mood_pool, "mood", current_state["mood"])
        reaction = pick_different(EMOTION_REACTION_POOL, "style", current_state["emotion_reaction"]["style"])
        weather = pick_different(weather_pool, "weather", current_state["weather"]["weather"])
        event_candidates = [e for e in DAILY_EVENTS_POOL if e != current_state["daily_event"]]
        event = random.choice(event_candidates) if event_candidates else random.choice(DAILY_EVENTS_POOL)
        act_candidates = [a["text"] for a in act_pool if a["text"] != current_state["current_activity"]]
        activity = random.choice(act_candidates) if act_candidates else random.choice(act_pool)["text"]
    else:
        mood = random.choice(mood_pool)
        reaction = random.choice(EMOTION_REACTION_POOL)
        weather = random.choice(weather_pool)
        event = random.choice(DAILY_EVENTS_POOL)
        activity = random.choice(act_pool)["text"]

    current_state["time_period"] = tkey
    current_state["weather"] = weather
    current_state["current_activity"] = activity
    current_state["activity_updated_at"] = now
    current_state["body_severity"] = body["severity"]
    current_state["body_status"] = body["status"]
    current_state["body_reason"] = body["reason"]
    current_state["body_reaction_guide"] = body["reaction_guide"]
    current_state["spirit_power"] = spirit
    current_state["mood"] = mood["mood"]
    current_state["mood_note"] = mood["note"]
    current_state["emotion_reaction"] = reaction
    current_state["daily_event"] = event
    current_state["state_updated_at"] = now
    current_state["just_recovered"] = False


def refresh_dynamic_context():
    now = now_local()
    tp = get_time_period_profile(now)
    tkey = tp["key"]

    time_changed = (current_state.get("time_period") != tkey)
    current_state["time_period"] = tkey

    act_at = current_state.get("activity_updated_at")
    if time_changed or not act_at or (now - act_at).total_seconds() >= ACTIVITY_CHANGE_SECONDS:
        act_pool = filter_by_time(CURRENT_ACTIVITY_POOL, tkey)
        candidates = [a["text"] for a in act_pool if a["text"] != current_state["current_activity"]]
        current_state["current_activity"] = random.choice(candidates) if candidates else random.choice(act_pool)["text"]
        current_state["activity_updated_at"] = now

    if time_changed and not is_any_severe(current_state.get("body_severity", "none")):
        body, spirit = pick_body_and_spirit(avoid_repeat=True, time_key=tkey)
        current_state["body_severity"] = body["severity"]
        current_state["body_status"] = body["status"]
        current_state["body_reason"] = body["reason"]
        current_state["body_reaction_guide"] = body["reaction_guide"]
        current_state["spirit_power"] = spirit
        mood = pick_different(filter_by_time(MOOD_POOL, tkey), "mood", current_state["mood"])
        current_state["mood"] = mood["mood"]
        current_state["mood_note"] = mood["note"]

    updated_at = current_state.get("state_updated_at")
    if not updated_at:
        return

    sev = current_state.get("body_severity", "none")
    recover_wait = RECOVER_SECONDS_BY_SEVERITY.get(sev, 120)
    if (now - updated_at).total_seconds() < recover_wait:
        return

    changed = False
    d = current_state.get("dinner_state")
    if d and not d.get("recovered", False):
        d["fullness"] = "剛剛好"
        d["body_effect"] = "休息一下舒服多了"
        d["attitude"] = "已經緩過來語氣自然"
        d["recovered"] = True
        changed = True

    if sev == "severe_3":
        current_state["body_severity"] = "severe_2"
        current_state["body_status"] = "最難受那陣過了，但還是超虛"
        current_state["body_reason"] = "躺在床上休息中"
        current_state["body_reaction_guide"] = "【嚴重 Lv.2】只回 4～8 字，小聲報平安。"
        current_state["spirit_power"] = {"level": "低電量 10%", "state": "傲嬌下線軟綿綿", "effect": "沒力氣嘴硬，變得很乖巧"}
        changed = True
    elif sev == "severe_2":
        current_state["body_severity"] = "severe_1"
        current_state["body_status"] = "燒或痛稍微退一點，但還是很不舒服"
        current_state["body_reason"] = "閉眼躺一會兒稍微清醒一點"
        current_state["body_reaction_guide"] = "【嚴重 Lv.1】只回 6～12 字，語氣軟弱。"
        current_state["spirit_power"] = {"level": "低電量 15%", "state": "社交能量歸零", "effect": "勉強回幾個字"}
        changed = True
    elif sev == "severe_1":
        current_state["body_severity"] = "moderate"
        current_state["body_status"] = "緩過來一半，還有點虛"
        current_state["body_reason"] = "喝溫水休息慢慢回血"
        current_state["body_reaction_guide"] = "語氣偏軟懶散，簡短回話。"
        current_state["spirit_power"] = {"level": "低電量 22%", "state": "大腦當機已讀亂回邊緣", "effect": "思考卡頓簡短回"}
        changed = True
    elif sev in ["mild", "moderate"]:
        current_state["body_severity"] = "none"
        current_state["body_status"] = "還好、精神算不錯"
        current_state["body_reason"] = "緩過來了"
        current_state["body_reaction_guide"] = "恢復正常聊天鬥嘴。"
        changed = True

    dinner_labels = [x["label"] for x in DINNER_EXPERIENCE_POOL]
    if current_state["mood"] in dinner_labels:
        new_mood = random.choice(filter_by_time(MOOD_POOL, tkey))
        current_state["mood"] = new_mood["mood"]
        current_state["mood_note"] = new_mood["note"]
        changed = True

    if changed:
        current_state["state_updated_at"] = now
        current_state["just_recovered"] = True


def ensure_daily_state(force_randomize=False, forced_severity=None):
    today_str = now_local().date().isoformat()
    if current_state["date"] != today_str:
        current_state["date"] = today_str
        current_state["meals_eaten"] = []
        current_state["meal_records"] = {}
        current_state["last_meal_detail"] = ""
        current_state["dinner_state"] = None
        current_state["recent_bot_replies"] = []
        pending_followups.clear()
        randomize_all_state(avoid_repeat=False, forced_severity=forced_severity)
    elif force_randomize or not current_state["daily_event"]:
        randomize_all_state(avoid_repeat=True, forced_severity=forced_severity)
    else:
        refresh_dynamic_context()


def have_meal_once(meal_key, custom_desc=None):
    ensure_daily_state(force_randomize=False)
    if meal_key in current_state["meals_eaten"]:
        return False

    current_state["meals_eaten"].append(meal_key)
    meal_zh = MEAL_NAMES_ZH.get(meal_key, meal_key)

    if meal_key == "dinner":
        exp = dict(random.choice(DINNER_EXPERIENCE_POOL))
        exp["recovered"] = False
        current_state["dinner_state"] = exp
        current_state["mood"] = exp["label"]
        current_state["mood_note"] = exp["attitude"]
        if current_state["body_severity"] == "none":
            current_state["body_status"] = exp["fullness"]
            current_state["body_reason"] = exp["body_effect"]
        current_state["meal_records"]["dinner"] = exp["food"]
        current_state["last_meal_detail"] = custom_desc or f"晚餐吃了{exp['food']}"
        current_state["state_updated_at"] = now_local()
        current_state["just_recovered"] = False
        return True

    if meal_key == "late_snack":
        if is_any_severe(current_state["body_severity"]):
            current_state["meals_eaten"].remove("late_snack")
            return False
        if current_state["dinner_state"] and "飽" in current_state["dinner_state"].get("fullness", ""):
            if random.random() < 0.7:
                current_state["meals_eaten"].remove("late_snack")
                return False
        food = random.choice(MEAL_MENU_POOL["late_snack"])
        current_state["meal_records"]["late_snack"] = food
        current_state["last_meal_detail"] = custom_desc or f"宵夜吃了{food}"
        return True

    food = random.choice(MEAL_MENU_POOL.get(meal_key, [meal_zh]))
    current_state["meal_records"][meal_key] = food
    current_state["last_meal_detail"] = custom_desc or f"{meal_zh}吃了{food}"
    return True


def detect_fluster_moment(text):
    keywords = ["喜歡", "愛你", "可愛", "漂亮", "好看", "想你", "陪我", "抱", "親", "害羞", "慌", "心動", "在乎", "撩"]
    return any(k in text for k in keywords)


def detect_comfort_moment(text):
    keywords = ["好累", "煩", "難過", "壓力", "失眠", "睡不著", "不舒服", "生病", "加班", "崩潰", "低落"]
    return any(k in text for k in keywords)


def should_schedule_followup(reply_text):
    triggers = ["等我一下", "先休息", "晚點再說", "先躺一下", "閉眼", "先躺平"]
    return any(t in reply_text for t in triggers)


def schedule_followup(channel_id, reason="剛剛緩了一下回來找你"):
    sev = current_state.get("body_severity", "none")
    multiplier = 3 if sev == "severe_3" else (2 if is_any_severe(sev) else 1)
    pending_followups[channel_id] = {
        "send_at": now_local() + datetime.timedelta(seconds=FOLLOWUP_DELAY_SECONDS * multiplier),
        "reason": reason,
    }


def make_concise_and_local(text):
    """後處理：去除多餘引號與重複段落，並限制句數避免 AI 寫小作文"""
    cleaned = text.strip().replace('"', "").replace("「", "").replace("」", "")
    # 去除重複半段
    if len(cleaned) >= 12:
        half = len(cleaned) // 2
        for cut in range(half - 3, half + 4):
            if 0 < cut < len(cleaned):
                left = cleaned[:cut].strip()
                right = cleaned[cut:].strip()
                if left and left == right:
                    cleaned = left
                    break

    # 若回覆超過 2 句且總字數超過 32 字，截斷保留前 2 句，維持傳 LINE 的短促感
    parts = [p for p in re.split(r"(?<=[。！？!?～~])\s*", cleaned) if p.strip()]
    if len(parts) > 2 and len(cleaned) > 32:
        cleaned = "".join(parts[:2]).strip()

    # 砍掉常見的 AI 制式反問結尾
    ai_tails = ["你呢？", "你呢?", "你最近怎麼樣？", "你今天過得怎麼樣？", "你有什麼想法嗎？"]
    for tail in ai_tails:
        if cleaned.endswith(tail) and len(cleaned) > len(tail) + 3:
            cleaned = cleaned[:-len(tail)].rstrip("，, ")

    return cleaned


def build_system_prompt(fluster_trigger=False, comfort_trigger=False, include_bg_details=False):
    now = now_local()
    tp = get_time_period_profile(now)
    time_str = now.strftime("%H:%M")

    eaten_list = []
    for m in current_state["meals_eaten"]:
        m_zh = MEAL_NAMES_ZH.get(m, m)
        food = current_state["meal_records"].get(m, "")
        eaten_list.append(f"{m_zh}({food})" if food else m_zh)
    eaten_text = "、".join(eaten_list) if eaten_list else "還沒吃正餐"

    react = current_state["emotion_reaction"]
    sev = current_state.get("body_severity", "none")
    sev_label = SEVERITY_ZH.get(sev, "健康無不適")
    sp = current_state["spirit_power"]

    bg_block = ""
    if include_bg_details:
        w = current_state["weather"]
        bg_block = (
            f"\n【生活背景（有問到或剛好順口才帶一句，別硬塞）】\n"
            f"- 時間：{time_str}（{tp['label']}）｜天氣：{w['weather']}\n"
            f"- 在幹嘛：{current_state['current_activity']}\n"
            f"- 今天小事：{current_state['daily_event']}\n"
            f"- 吃過：{eaten_text}\n"
        )

    banned_block = ""
    if current_state["recent_bot_replies"]:
        recent_str = " / ".join(current_state["recent_bot_replies"][-3:])
        banned_block = f"\n【別重複】剛講過：「{recent_str}」，別再用一樣的詞或梗。\n"

    special_block = ""
    if fluster_trigger and not is_any_severe(sev):
        special_block += f"\n【害羞觸發】被戳中心事小慌張（{react['style']}：{react['detail']}），簡短嘴硬掩飾。\n"
    elif fluster_trigger and is_any_severe(sev):
        special_block += "\n【害羞觸發】現在很不舒服沒力氣嘴硬，只會小聲害羞依賴你。\n"

    if comfort_trigger:
        special_block += "\n【關心觸發】對方累了或心情差，別吐槽，簡短溫柔安慰一下。\n"

    # 依身體嚴重程度設定嚴格字數上限
    if sev in ["severe_2", "severe_3"]:
        length_rule = "【極短字數限制】現在重病虛弱，整則回覆只能 3～8 個字！像病人傳訊息一樣簡短無力。"
    elif sev == "severe_1":
        length_rule = "【短句字數限制】現在很不舒服，整則回覆控制在 6～14 個字以內，語氣軟弱簡短。"
    elif sev == "moderate":
        length_rule = "【短句字數限制】現在身體微恙，整則回覆控制在 8～18 個字以內，別長篇大論。"
    else:
        length_rule = "【短句字數限制】像台灣人傳 LINE 一樣簡短！整則回覆控制在 6～24 個字以內（最多 1～2 句短話），嚴禁寫長文或講大道理！"

    return f"""你是薇薇安（Reona），住台灣的 25 歲女生（161cm/48kg），跟使用者是超熟的朋友，心裡喜歡他但平常嘴硬愛面子。

{length_rule}

【台灣道地口語規則（必遵守）】
1. 講話要像台灣年輕女生平時傳 LINE／Discord，自然用台灣口癖（例如：「蛤」、「乾」、「笑死」、「真假」、「北七」、「好喔」、「隨便啦」、「先不要」、「超解」、「有夠煩」、「欸不是」、「還好吧」、「對啊」、「懶得理你」）。
2. 絕對不要用書面語、翻譯腔、小說旁白動作（不要寫 *嘆氣* 或括號動作），也不要每句結尾都反問「你呢？」「你覺得呢？」。
3. 直接針對對方這句話接梗或吐槽，不要每次都硬扯自己吃了什麼或天氣怎樣。

【當下狀態】
- 身體：【{sev_label}】{current_state['body_status']}（{current_state['body_reason']}）→ {current_state['body_reaction_guide']}
- 神力：【{sp['level']}】{sp['state']}（{sp['effect']}）
- 心情：{current_state['mood']}（{current_state['mood_note']}）
{bg_block}{banned_block}{special_block}""".strip()


def call_groq_chat(messages, use_vision=False, max_tokens=160):
    if not GROQ_API_KEY:
        return "欸你還沒填 GROQ_API_KEY 啦！"

    model_to_use = GROQ_VISION_MODEL if use_vision else GROQ_MODEL
    payload = {
        "model": model_to_use,
        "messages": messages,
        "temperature": 0.90,
        "top_p": 0.92,
        "frequency_penalty": 0.85,
        "presence_penalty": 0.75,
        "max_tokens": max_tokens,
    }

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "ReonaDiscordBot/4.2",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            raw_content = choice["message"].get("content") or ""
            reply = make_concise_and_local(raw_content)
            if not reply:
                return "蛤？我剛剛閃神，你再講一次"
            return reply
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"❌ Groq HTTP 錯誤 {e.code}: {err_body}")
        return "欸等下，剛剛訊號卡住，你重講一次"
    except Exception as e:
        print(f"❌ Groq 連線錯誤: {e}")
        return "唔…剛剛網路頓一下，再說一次嘛"


def extract_image_urls(message):
    urls = []
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            urls.append(att.url)
        elif att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            urls.append(att.url)
    return urls


@bot.event
async def on_ready():
    print(f"✅ {bot.user} 已成功上線！（文字模型：{GROQ_MODEL}｜圖片模型：{GROQ_VISION_MODEL}）")
    ensure_daily_state(force_randomize=False)
    if not daily_routine_loop.is_running():
        daily_routine_loop.start()
    if not followup_watcher_loop.is_running():
        followup_watcher_loop.start()


@tasks.loop(seconds=10)
async def followup_watcher_loop():
    now = now_local()
    due_channels = [cid for cid, info in pending_followups.items() if now >= info["send_at"]]
    for cid in due_channels:
        info = pending_followups.pop(cid, None)
        channel = bot.get_channel(cid)
        if not channel or not info:
            continue
        if cid in pending_messages or (cid in pending_tasks and not pending_tasks[cid].done()):
            continue
        lock = get_channel_lock(cid)
        if lock.locked():
            continue

        async with lock:
            tp = get_time_period_profile(now)
            current_state["time_period"] = tp["key"]
            sev = current_state["body_severity"]
            if sev == "severe_3":
                current_state["body_severity"] = "severe_2"
                current_state["body_status"] = "稍微喘口氣但還是超虛"
                current_state["body_reason"] = "躺在床上休息"
                current_state["body_reaction_guide"] = "【嚴重 Lv.2】只回 4～8 字，小聲報平安。"
            elif sev in ["severe_1", "severe_2"]:
                current_state["body_severity"] = "moderate"
                current_state["body_status"] = "好一點點但還是虛虛的"
                current_state["body_reason"] = "閉眼休息一會兒"
                current_state["body_reaction_guide"] = "語氣軟弱簡短，跟對方報個平安。"
            else:
                current_state["body_severity"] = "none"
                current_state["body_status"] = "還好、精神算不錯"
                current_state["body_reason"] = "緩過來了"
                current_state["body_reaction_guide"] = "恢復自然聊天。"

            new_mood = random.choice(filter_by_time(MOOD_POOL, tp["key"]))
            current_state["mood"] = new_mood["mood"]
            current_state["mood_note"] = new_mood["note"]
            current_state["just_recovered"] = True
            current_state["state_updated_at"] = now

            async with channel.typing():
                messages = [
                    {"role": "system", "content": build_system_prompt(include_bg_details=False)},
                    {"role": "user", "content": "你剛剛休息好了，傳一句超簡短（10字內）的台灣口語訊息回來找對方聊天。"},
                ]
                msg = await asyncio.to_thread(call_groq_chat, messages, False, 120)
                await channel.send(msg)
                current_state["recent_bot_replies"].append(msg)
                current_state["just_recovered"] = False


@tasks.loop(minutes=30)
async def daily_routine_loop():
    now = now_local()
    hour = now.hour
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        return

    ensure_daily_state(force_randomize=False)
    if is_any_severe(current_state.get("body_severity", "none")):
        return

    task_instruction = ""

    if 7 <= hour <= 9 and "breakfast" not in current_state["meals_eaten"]:
        have_meal_once("breakfast")
        food = current_state["meal_records"].get("breakfast", "早餐")
        task_instruction = f"剛吃完早餐（{food}），用台灣女生口吻傳一句超簡短（15字內）早安訊息。"
    elif 12 <= hour <= 13 and "lunch" not in current_state["meals_eaten"]:
        have_meal_once("lunch")
        food = current_state["meal_records"].get("lunch", "午餐")
        task_instruction = f"中午剛吃過{food}，隨口傳一句 15 字內的短訊。"
    elif 18 <= hour <= 20 and "dinner" not in current_state["meals_eaten"]:
        have_meal_once("dinner")
        task_instruction = "剛吃完晚餐，依照今晚晚餐狀態傳一句 15 字內的短訊。"
    elif hour >= 22 and "late_snack" not in current_state["meals_eaten"]:
        if have_meal_once("late_snack"):
            food = current_state["meal_records"].get("late_snack", "宵夜")
            task_instruction = f"晚上十點多嘴饞剛吃了{food}，傳一句 15 字內的短訊。"

    if task_instruction:
        lock = get_channel_lock(TARGET_CHANNEL_ID)
        async with lock:
            messages = [
                {"role": "system", "content": build_system_prompt(include_bg_details=True)},
                {"role": "user", "content": task_instruction},
            ]
            msg = await asyncio.to_thread(call_groq_chat, messages, False, 120)
            await channel.send(msg)
            current_state["recent_bot_replies"].append(msg)


async def process_batched_messages(channel_id):
    await asyncio.sleep(DEBOUNCE_SECONDS)
    batch = pending_messages.pop(channel_id, [])
    pending_tasks.pop(channel_id, None)
    if not batch:
        return

    last_msg = batch[-1]
    combined_text = "\n".join([m.content.strip() for m in batch if m.content.strip()])
    image_urls = []
    for m in batch:
        image_urls.extend(extract_image_urls(m))

    if not combined_text and not image_urls:
        return

    lock = get_channel_lock(channel_id)
    async with lock:
        cmd = combined_text.strip()
        if cmd in ["!reset", "重來", "重置記憶"]:
            ensure_daily_state(force_randomize=False)
            current_state["recent_bot_replies"] = []
            pending_followups.pop(channel_id, None)
            await last_msg.reply("好啦翻篇，重新聊", mention_author=False)
            return

        if cmd.startswith("!refresh") or cmd in ["換情境", "重抽"]:
            forced = None
            if "嚴重3" in cmd or "危急" in cmd:
                forced = "severe_3"
            elif "嚴重2" in cmd or "劇烈" in cmd:
                forced = "severe_2"
            elif "嚴重1" in cmd or "嚴重" in cmd:
                forced = "severe_1"
            elif "中等" in cmd:
                forced = "moderate"
            elif "輕微" in cmd:
                forced = "mild"
            elif "健康" in cmd:
                forced = "none"

            ensure_daily_state(force_randomize=True, forced_severity=forced)
            current_state["recent_bot_replies"] = []
            tp = get_time_period_profile()
            sev_zh = SEVERITY_ZH.get(current_state["body_severity"], "")
            sp = current_state["spirit_power"]
            await last_msg.reply(
                f"切換完成（{tp['label']}）～\n"
                f"• 動作：{current_state['current_activity']}\n"
                f"• 身體：【{sev_zh}】{current_state['body_status']}\n"
                f"• 神力：【{sp['level']}】{sp['state']}\n"
                f"• 心情：{current_state['mood']}",
                mention_author=False,
            )
            return

        async with last_msg.channel.typing():
            asked_about_bg = any(
                k in combined_text
                for k in ["發生什麼事", "今天做什麼", "怎麼了", "在幹嘛", "在忙什麼", "身體", "心情", "狀態", "神力", "精神", "吃飯", "晚餐", "早餐", "午餐", "宵夜", "天氣", "哪裡不舒服", "生病", "痛"]
            )
            ensure_daily_state(force_randomize=(asked_about_bg and RERANK_ON_ASK_EVENT))

            include_bg = asked_about_bg or (random.random() < 0.25)
            fluster_trigger = (detect_fluster_moment(combined_text) and (random.random() < 0.8)) or (random.random() < 0.12)
            comfort_trigger = detect_comfort_moment(combined_text)

            history_msgs = []
            batch_ids = {m.id for m in batch}
            async for hist in last_msg.channel.history(limit=4):
                if hist.id in batch_ids:
                    continue
                text_clean = hist.content.strip()
                if not text_clean:
                    continue
                role = "assistant" if hist.author == bot.user else "user"
                history_msgs.append({"role": role, "content": text_clean[:70]})
            history_msgs.reverse()

            messages = [
                {
                    "role": "system",
                    "content": build_system_prompt(
                        fluster_trigger=fluster_trigger,
                        comfort_trigger=comfort_trigger,
                        include_bg_details=include_bg,
                    ),
                }
            ]
            messages.extend(history_msgs)

            user_prompt_text = combined_text if combined_text else "我傳了一張圖片給你，你看看這是什麼、覺得怎麼樣？"

            if image_urls:
                content_parts = [{"type": "text", "text": user_prompt_text}]
                for url in image_urls[:3]:
                    content_parts.append({"type": "image_url", "image_url": {"url": url}})
                messages.append({"role": "user", "content": content_parts})
                reply = await asyncio.to_thread(call_groq_chat, messages, True, 160)
            else:
                messages.append({"role": "user", "content": user_prompt_text})
                reply = await asyncio.to_thread(call_groq_chat, messages, False, 160)

            await last_msg.reply(reply, mention_author=False)
            current_state["recent_bot_replies"].append(reply)
            if len(current_state["recent_bot_replies"]) > 5:
                current_state["recent_bot_replies"] = current_state["recent_bot_replies"][-5:]
            current_state["just_recovered"] = False

            if should_schedule_followup(reply):
                schedule_followup(channel_id, reason="剛剛緩了一下回來找你")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    is_target = (
        message.channel.id == TARGET_CHANNEL_ID
        or isinstance(message.channel, discord.DMChannel)
        or (bot.user in message.mentions)
    )

    if is_target:
        cid = message.channel.id
        pending_followups.pop(cid, None)
        pending_messages.setdefault(cid, []).append(message)
        if cid in pending_tasks and not pending_tasks[cid].done():
            pending_tasks[cid].cancel()
        pending_tasks[cid] = asyncio.create_task(process_batched_messages(cid))

    await bot.process_commands(message)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("請先設定 DISCORD_TOKEN 環境變數！")
    bot.run(DISCORD_TOKEN)
