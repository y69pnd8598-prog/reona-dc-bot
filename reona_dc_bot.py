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

# ================= 環境變數與基本設定 =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "1571035253974237219"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# 本地時間時區（預設 UTC+8 台灣時間）
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 持久化記憶檔（保存好感度等跨重啟資料）
MEMORY_FILE = "reona_memory.json"

# 狀態與互動規則設定
RERANK_ON_ASK_EVENT = False
DEBOUNCE_SECONDS = 1.6
ACTIVITY_CHANGE_SECONDS = 240
FOLLOWUP_DELAY_SECONDS = 180
SULK_DURATION_SECONDS = 300         # 小脾氣冷戰預設 5 分鐘（300秒）
THEATER_IDLE_MIN_SECONDS = 1800     # 至少閒置 30 分鐘沒聊天才可能自己冒出來
THEATER_TRIGGER_PROB = 0.45         # 每次檢查時冒出小劇場的機率

# 【大、中、小機率分層設定】（總和為 1.0）
PROB_HEALTHY = 0.55       # 大機率：完全健康
PROB_MILD = 0.30          # 大機率：輕微生理反應（健康+輕微合計 85%）
PROB_MODERATE = 0.12      # 中機率：中等程度不適（12%）
PROB_SEVERE_TOTAL = 0.03  # 小機率：嚴重狀況總機率（3%）

# 【嚴重等級內部權重】
SEVERE_SUB_WEIGHTS = {
    "severe_1": 0.60,  # 明顯不適（占嚴重池 60%）
    "severe_2": 0.30,  # 劇烈難受（占嚴重池 30%）
    "severe_3": 0.10,  # 虛脫危急級（占嚴重池 10%）
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

# 1. 天氣與環境氛圍池
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
    {"weather": "冷氣開太強有點冷", "vibe": "室內吹到起雞皮疙瘩", "times": ["noon", "afternoon", "evening", "night"]},
    {"weather": "晚風吹進來剛剛好", "vibe": "不用開冷氣就很涼", "times": ["evening", "night", "late_night"]},
    {"weather": "清晨涼涼霧霧的", "vibe": "剛天亮還沒完全開機", "times": ["early_morning", "morning"]},
    {"weather": "下午三四點暖暖的", "vibe": "光線柔和超想打瞌睡", "times": ["afternoon"]},
]

# 2. 當下正在做的事池
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
]

# 3. 身體狀況分級池
BODY_STATUS_POOL = [
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
        "status": "吹冷風鼻子癢狂打噴嚏",
        "reason": "吹到風鼻子過敏",
        "reaction_guide": "偶爾唸一下鼻子超癢，正常短句聊天。",
        "times": ["morning", "noon", "afternoon", "evening", "night"],
    },
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
        "reason": "喝太多冰的或吃到涼的",
        "reaction_guide": "有氣無力不想動，講話簡短軟弱。",
        "times": ["morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "moderate",
        "status": "小腿跟腳踝微痠有點拐到",
        "reason": "剛剛走路沒看路差點絆倒，後勁上來了",
        "reaction_guide": "覺得自己超蠢又不敢承認，講話簡短自嘲。",
        "times": ["afternoon", "evening", "night"],
    },
    {
        "severity": "severe_1",
        "status": "重度偏頭痛怕光想吐",
        "reason": "吹冷風或熬夜後遺症發作，看螢幕超刺眼",
        "reaction_guide": "【嚴重 Lv.1】不吐槽不鬥嘴，只回 6～12 字，說頭好痛想把螢幕調暗休息。",
        "times": ["morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "severe_1",
        "status": "急性腸胃炎肚子絞痛",
        "reason": "吃壞東西或喝太冰後勁發作",
        "reaction_guide": "【嚴重 Lv.1】沒力氣打字，只回 6～12 字，語氣軟弱說肚子絞痛。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
    {
        "severity": "severe_1",
        "status": "重感冒喉嚨痛像吞刀片",
        "reason": "吹到風著涼後遺症，吞口水都在痛",
        "reaction_guide": "【嚴重 Lv.1】收起傲嬌，只回 6～12 字，乖乖喝溫水。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
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
        "severity": "severe_3",
        "status": "急性腸胃炎上吐下瀉虛脫",
        "reason": "脫水發軟拿手機手在抖",
        "reaction_guide": "【嚴重 Lv.3】幾乎打不動字！只回 3～6 字（如「好難受…先躺一下」），完全依賴你。",
        "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"],
    },
]

# 4. 神力／精神電量狀況池
SPIRIT_POWER_POOL = [
    {"level": "滿格 100%", "state": "神力滿格第六感超準", "effect": "直覺超準、反應快超愛虧人"},
    {"level": "高能 95%", "state": "嘴砲無冷卻模式", "effect": "接梗超快，鬥嘴完全不輸"},
    {"level": "高能 90%", "state": "一眼看穿讀心術", "effect": "馬上看出你有沒有在敷衍或心虛"},
    {"level": "穩定 70%", "state": "日常穩定輸出", "effect": "自然聊天鬥嘴，不特別亢奮也不累"},
    {"level": "波動 65%", "state": "偶爾閃神一秒", "effect": "大多正常，偶爾慢半拍"},
    {"level": "波動 60%", "state": "防禦力下降 30%", "effect": "你一溫柔就很容易破防心軟"},
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
    {"mood": "不舒服變得黏人乖巧", "note": "防備心下降特別想被關心", "times": ["early_morning", "morning", "noon", "afternoon", "evening", "night", "late_night"]},
]

# 6. 感情與害羞慌張反應池
EMOTION_REACTION_POOL = [
    {"style": "害羞小慌張", "detail": "被直球誇獎會愣一下、結巴或趕快轉移話題"},
    {"style": "裝鎮定但心虛", "detail": "表面裝冷靜，其實耳根發熱節奏亂掉"},
    {"style": "心軟變乖", "detail": "被溫柔關心會害羞不知道接什麼，語氣變軟"},
    {"style": "傲嬌炸毛後縮一下", "detail": "先嘴硬頂一句，講完又覺得太明顯偷偷害羞"},
    {"style": "反虧你掩飾害羞", "detail": "怕被看穿心動，反問「你吃錯藥喔」轉移焦點"},
    {"style": "不舒服直接撒嬌", "detail": "身體痛或神力低時不再硬撐，小聲要你陪"},
]

# 7. 每日日常／出遊事件池
DAILY_EVENTS_POOL = [
    "巷口看到一隻胖橘貓曬太陽，想摸結果牠超警覺直接溜走",
    "跟朋友踩點咖啡廳，拍照好看但甜點甜到鎖喉",
    "買飲料在櫃檯前選擇困難站三分鐘，最後還是點老樣子",
    "整理房間搬箱子翻出舊照片發呆一陣子",
    "走路滑手機差點踢到階梯絆倒，還好旁邊沒人看到",
    "下午去河堤散步吹風，覺得偶爾出來走走還不錯",
    "找手機找半天結果就拿在左手上，蠢到無語",
    "跟朋友說要減肥，聊一聊又跑去買冰手搖飲",
    "原本想早睡養生，滑手機回過神又半夜了",
    "吹冷氣吹太久忘記蓋薄毯，現在有點涼",
    "喝了一杯超濃冰拿鐵提神，當下覺得超爽",
    "點外送吃了超辣或超油的東西，當下覺得很過癮",
]

# 8. 事件延遲後遺症因果規則
EVENT_AFTERMATH_RULES = [
    {
        "keywords": ["絆倒", "階梯", "走路"],
        "delay_range": (180, 360),
        "trigger_reason": "剛剛走路差點絆倒那一下，當下覺得沒事，現在後勁上來腳踝跟小腿突然抽痠",
        "target_severity": "moderate",
        "target_status": "小腿跟腳踝微痠有點拐到",
        "reaction_guide": "突然發現剛才絆倒的後勁來了，覺得自己超蠢又嘴硬不承認痛，講話簡短自嘲。",
    },
    {
        "keywords": ["冰拿鐵", "咖啡", "拿鐵"],
        "delay_range": (180, 360),
        "trigger_reason": "剛喝完冰拿鐵的時候還好好的，現在咖啡因後勁上來突然心悸手微抖",
        "target_severity": "moderate",
        "target_status": "咖啡喝太濃心悸手微抖",
        "reaction_guide": "突然覺得心跳變快手微抖，懊惱剛才不該喝那麼快，講話簡短。",
    },
    {
        "keywords": ["手搖飲", "冰", "涼"],
        "delay_range": (180, 420),
        "trigger_reason": "剛剛喝冰飲料覺得很爽，結果現在肚子突然開始一陣陣微絞痛脹氣",
        "target_severity": "moderate",
        "target_status": "肚子微絞痛脹氣怕冷",
        "reaction_guide": "剛喝冰的現在報應來了，語氣突然變軟弱委屈，說肚子突然在絞痛。",
    },
    {
        "keywords": ["吹風", "河堤", "冷氣", "降溫", "變冷"],
        "delay_range": (240, 480),
        "trigger_reason": "剛剛吹風吹冷氣覺得很涼很舒服，結果現在太陽穴突然開始一跳一跳偏頭痛",
        "target_severity": "moderate",
        "target_status": "偏頭痛太陽穴一跳一跳痛",
        "reaction_guide": "剛剛吹到風現在突然偏頭痛發作，語氣變低沉簡短，說頭突然痛起來。",
    },
    {
        "keywords": ["滑手機", "半夜", "早睡", "短影音", "追劇"],
        "delay_range": (180, 360),
        "trigger_reason": "剛剛滑手機精神還很好，現在突然睏意爆擊、狂打哈欠眼角泛淚",
        "target_severity": "mild",
        "target_status": "狂打哈欠眼角泛淚",
        "reaction_guide": "突然一陣睏意襲來，講話帶點打哈欠的慵懶感，簡短回話。",
    },
    {
        "keywords": ["甜點", "鎖喉", "甜"],
        "delay_range": (180, 360),
        "trigger_reason": "剛剛吃甜點很開心，現在突然覺得喉嚨跟胃膩膩的有點口渴",
        "target_severity": "mild",
        "target_status": "吃完甜食有點膩想喝茶",
        "reaction_guide": "突然覺得剛才吃太甜有點膩，隨口碎念想喝水。",
    },
    {
        "keywords": ["搬箱子", "整理房間"],
        "delay_range": (240, 480),
        "trigger_reason": "剛剛收房間還好好的，現在坐下來腰跟背肌突然緊繃痠痛",
        "target_severity": "mild",
        "target_status": "久坐腰跟小腿緊緊的",
        "reaction_guide": "突然覺得腰痠背痛，像老人家一樣簡短碎念。",
    },
    {
        "keywords": ["辣", "油", "火鍋", "鹽酥雞"],
        "delay_range": (240, 480),
        "trigger_reason": "剛剛吃的時候覺得很過癮，現在腸胃突然翻攪絞痛",
        "target_severity": "severe_1",
        "target_status": "急性腸胃炎肚子絞痛",
        "reaction_guide": "【嚴重 Lv.1】剛剛亂吃東西現在突然肚子痛發作，只回 6～12 字，語氣軟弱。",
    },
]

# 9. 主動傳訊息／突發小劇場池
SPONTANEOUS_THEATER_POOL = [
    {"prompt": "你剛剛滑手機看到一個超北七的影片，忍不住想傳一句超短的話分享給對方笑一下。", "times": ["afternoon", "evening", "night"]},
    {"prompt": "你現在突然超想喝手搖飲（比如珍奶或四季春），但懶得出門，隨口傳一句碎念給對方。", "times": ["afternoon", "evening", "night"]},
    {"prompt": "你剛剛找手機找了半天結果就在自己手上，覺得自己超蠢，傳一句簡短自嘲給對方。", "times": ["morning", "afternoon", "evening", "night"]},
    {"prompt": "你突然有點好奇對方現在在幹嘛，用嘴硬傲嬌的口氣隨便找個藉口密他一句（15字內）。", "times": ["afternoon", "evening", "night"]},
    {"prompt": "剛洗完澡在吹頭髮，覺得手超痠，隨口傳一句給對方抱怨一下。", "times": ["evening", "night", "late_night"]},
    {"prompt": "半夜還沒睡突然有點無聊或微emo，傳一句超簡短（12字內）的話看看對方睡了沒。", "times": ["night", "late_night"]},
    {"prompt": "剛起床還在賴床不想面對現實，傳一句軟糯懶散的早安碎念（12字內）。", "times": ["morning"]},
    {"prompt": "中午不知道要吃什麼選擇困難，傳一句簡短的話問對方意見（15字內）。", "times": ["noon"]},
    {"prompt": "剛剛走路差點絆倒還好沒人看到，心有餘悸傳一句給對方。", "times": ["morning", "afternoon", "evening"]},
]

# 10. 各餐隨機食物池
MEAL_MENU_POOL = {
    "breakfast": ["起司蛋餅配大冰奶", "鮪魚吐司加無糖豆漿", "超商飯糰配拿鐵", "蘿蔔糕加蛋", "皮蛋瘦肉粥", "溫豆漿配茶葉蛋"],
    "lunch": ["雞肉飯便當", "青醬義大利麵", "健康餐盒", "牛肉麵", "壽司拼盤", "海鮮粥", "親子丼", "餛飩湯麵"],
    "late_snack": ["鹽酥雞配四季豆", "泡麵加蛋", "豆花加珍珠", "洋芋片配飲料", "關東煮", "紫米紅豆粥", "溫蜂蜜水"],
}

# 11. 晚餐體驗變數池
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


def load_persistent_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return max(0, min(100, int(data.get("affection", 45))))
        except Exception:
            pass
    return 45


def save_persistent_memory():
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"affection": current_state["affection"]}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 儲存記憶檔失敗: {e}")


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
    "pending_aftermath": None,
    "sudden_onset_notice": "",
    "meals_eaten": [],
    "meal_records": {},
    "last_meal_detail": "",
    "dinner_state": None,
    "state_updated_at": None,
    "just_recovered": False,
    "recent_bot_replies": [],
    "affection": load_persistent_memory(),
    "sulking_until": None,
    "sulk_reason": "",
    "last_interaction_at": None,
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


def get_affection_profile():
    aff = current_state["affection"]
    if aff < 30:
        return {
            "stage": f"Lv.1 普通損友（{aff}/100）",
            "guide": "目前算普通熟的朋友，講話偏隨性愛吐槽，還不會太害羞或依賴。",
        }
    elif aff < 60:
        return {
            "stage": f"Lv.2 有點好感（{aff}/100）",
            "guide": "心裡對他有好感但嘴硬不承認，被關心會偷偷開心、語氣微軟。",
        }
    elif aff < 85:
        return {
            "stage": f"Lv.3 曖昧嘴硬期（{aff}/100）",
            "guide": "其實超喜歡他但死愛面子！被誇或被撩容易慌張結巴、嘴硬掩飾，對他特別護短。",
        }
    else:
        return {
            "stage": f"Lv.4 滿心喜歡藏不住（{aff}/100）",
            "guide": "好感度爆表！雖然偶爾還是會小傲嬌，但語氣明顯很親暱依賴、超容易心軟撒嬌。",
        }


def adjust_affection(delta):
    old = current_state["affection"]
    new_val = max(0, min(100, old + delta))
    if new_val != old:
        current_state["affection"] = new_val
        save_persistent_memory()


def is_currently_sulking(now=None):
    now = now or now_local()
    until = current_state.get("sulking_until")
    if not until:
        return False
    if now >= until:
        current_state["sulking_until"] = None
        current_state["sulk_reason"] = ""
        return False
    return True


def trigger_sulk(reason="被你氣到了", duration_sec=SULK_DURATION_SECONDS):
    current_state["sulking_until"] = now_local() + datetime.timedelta(seconds=duration_sec)
    current_state["sulk_reason"] = reason
    adjust_affection(-2)


def clear_sulk():
    current_state["sulking_until"] = None
    current_state["sulk_reason"] = ""


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


def schedule_event_aftermath_if_applicable(source_text, base_time=None):
    if current_state.get("body_severity") not in ["none", "mild"]:
        return
    base_time = base_time or now_local()
    matched_rules = [
        rule for rule in EVENT_AFTERMATH_RULES
        if any(kw in source_text for kw in rule["keywords"])
    ]
    if not matched_rules:
        return

    if random.random() < 0.70:
        chosen = random.choice(matched_rules)
        delay_sec = random.randint(*chosen["delay_range"])
        current_state["pending_aftermath"] = {
            "trigger_at": base_time + datetime.timedelta(seconds=delay_sec),
            "source_text": source_text,
            "reason": chosen["trigger_reason"],
            "severity": chosen["target_severity"],
            "status": chosen["target_status"],
            "reaction_guide": chosen["reaction_guide"],
        }


def apply_pending_aftermath_if_due(now=None):
    now = now or now_local()
    aftermath = current_state.get("pending_aftermath")
    if not aftermath or now < aftermath["trigger_at"]:
        return False

    current_state["pending_aftermath"] = None
    sev = aftermath["severity"]
    current_state["body_severity"] = sev
    current_state["body_status"] = aftermath["status"]
    current_state["body_reason"] = aftermath["reason"]
    current_state["body_reaction_guide"] = aftermath["reaction_guide"]
    current_state["state_updated_at"] = now
    current_state["sudden_onset_notice"] = aftermath["reason"]

    if sev == "severe_1":
        current_state["spirit_power"] = {"level": "低電量 15%", "state": "社交能量歸零", "effect": "突然不舒服沒力氣吐槽"}
    elif sev == "moderate":
        current_state["spirit_power"] = {"level": "低電量 22%", "state": "大腦當機已讀亂回邊緣", "effect": "後勁上來思考卡頓"}
    else:
        current_state["spirit_power"] = {"level": "波動 57%", "state": "慵懶貓咪附體", "effect": "回話變得懶洋洋"}

    return True


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
    current_state["pending_aftermath"] = None
    current_state["sudden_onset_notice"] = ""

    if body["severity"] == "none":
        schedule_event_aftermath_if_applicable(f"{event} {activity} {weather['weather']}", base_time=now)


def refresh_dynamic_context():
    now = now_local()
    is_currently_sulking(now)

    if apply_pending_aftermath_if_due(now):
        return

    tp = get_time_period_profile(now)
    tkey = tp["key"]
    time_changed = (current_state.get("time_period") != tkey)
    current_state["time_period"] = tkey

    act_at = current_state.get("activity_updated_at")
    if time_changed or not act_at or (now - act_at).total_seconds() >= ACTIVITY_CHANGE_SECONDS:
        act_pool = filter_by_time(CURRENT_ACTIVITY_POOL, tkey)
        candidates = [a["text"] for a in act_pool if a["text"] != current_state["current_activity"]]
        new_act = random.choice(candidates) if candidates else random.choice(act_pool)["text"]
        current_state["current_activity"] = new_act
        current_state["activity_updated_at"] = now
        if current_state.get("body_severity") == "none" and not current_state.get("pending_aftermath"):
            schedule_event_aftermath_if_applicable(new_act, base_time=now)

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
        schedule_event_aftermath_if_applicable(exp["food"])
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
        schedule_event_aftermath_if_applicable(food)
        return True

    food = random.choice(MEAL_MENU_POOL.get(meal_key, [meal_zh]))
    current_state["meal_records"][meal_key] = food
    current_state["last_meal_detail"] = custom_desc or f"{meal_zh}吃了{food}"
    schedule_event_aftermath_if_applicable(food)
    return True


def detect_fluster_moment(text):
    keywords = ["喜歡", "愛你", "可愛", "漂亮", "好看", "想你", "陪我", "抱", "親", "害羞", "慌", "心動", "在乎", "撩"]
    return any(k in text for k in keywords)


def detect_comfort_moment(text):
    keywords = ["好累", "煩", "難過", "壓力", "失眠", "睡不著", "不舒服", "生病", "加班", "崩潰", "低落"]
    return any(k in text for k in keywords)


def detect_care_for_her(text):
    keywords = ["還好嗎", "多喝水", "休息", "吃藥", "看醫生", "保重", "心疼", "陪你", "蓋被子", "好點沒"]
    return any(k in text for k in keywords)


def detect_annoy_trigger(text):
    keywords = ["你很吵", "好吵", "胖", "醜", "滾", "煩死了", "閉嘴", "隨便你", "敷衍", "笨蛋", "矮"]
    return any(k in text for k in keywords)


def detect_apology_or_coax(text):
    keywords = ["對不起", "抱歉", "別生氣", "原諒", "哄你", "請你喝", "請你吃", "乖啦", "我錯了", "別氣了"]
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
    cleaned = text.strip().replace('"', "").replace("「", "").replace("」", "")
    if len(cleaned) >= 12:
        half = len(cleaned) // 2
        for cut in range(half - 3, half + 4):
            if 0 < cut < len(cleaned):
                left = cleaned[:cut].strip()
                right = cleaned[cut:].strip()
                if left and left == right:
                    cleaned = left
                    break

    parts = [p for p in re.split(r"(?<=[。！？!?～~])\s*", cleaned) if p.strip()]
    if len(parts) > 2 and len(cleaned) > 32:
        cleaned = "".join(parts[:2]).strip()

    ai_tails = ["你呢？", "你呢?", "你最近怎麼樣？", "你今天過得怎麼樣？", "你有什麼想法嗎？"]
    for tail in ai_tails:
        if cleaned.endswith(tail) and len(cleaned) > len(tail) + 3:
            cleaned = cleaned[:-len(tail)].rstrip("，, ")

    return cleaned


def build_system_prompt(fluster_trigger=False, comfort_trigger=False, just_forgave=False, include_bg_details=False):
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
    aff_prof = get_affection_profile()
    sulking = is_currently_sulking(now)

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

    sudden_block = ""
    if current_state.get("sudden_onset_notice"):
        sudden_block = (
            f"\n【剛發作的身體後遺症（重要反差）】\n"
            f"你剛剛原本還沒事，但現在突然：{current_state['sudden_onset_notice']}！\n"
            f"可以在回話時自然哎一聲或順口提到剛才做那件事的後勁突然上來了。\n"
        )

    sulk_block = ""
    if sulking:
        sulk_block = (
            f"\n【小脾氣冷戰中（最高優先級）】\n"
            f"你現在正在跟他鬧小脾氣冷戰（原因：{current_state['sulk_reason']}）！\n"
            f"回覆只能 2～6 個字，語氣冷淡傲嬌不爽（例如：「喔」「隨便」「懶得理你」「哼」），不要主動開話題！\n"
        )
    elif just_forgave:
        sulk_block = "\n【剛被哄好消氣】原本在生氣但被他哄到破防了，嘴硬說「好啦這次算了」，語氣其實變軟了。\n"

    banned_block = ""
    if current_state["recent_bot_replies"]:
        recent_str = " / ".join(current_state["recent_bot_replies"][-3:])
        banned_block = f"\n【別重複】剛講過：「{recent_str}」，別再用一樣的詞或梗。\n"

    special_block = ""
    if not sulking:
        if fluster_trigger and not is_any_severe(sev):
            special_block += f"\n【害羞觸發】被戳中心事小慌張（{react['style']}：{react['detail']}），簡短嘴硬掩飾。\n"
        elif fluster_trigger and is_any_severe(sev):
            special_block += "\n【害羞觸發】現在很不舒服沒力氣嘴硬，只會小聲害羞依賴你。\n"

        if comfort_trigger:
            special_block += "\n【關心觸發】對方累了或心情差，別吐槽，簡短溫柔安慰一下。\n"

    if sulking:
        length_rule = "【冷戰極短限制】正在鬧小脾氣冷戰，整則回覆只能 2～6 個字！"
    elif sev in ["severe_2", "severe_3"]:
        length_rule = "【極短字數限制】現在重病虛弱，整則回覆只能 3～8 個字！像病人傳訊息一樣簡短無力。"
    elif sev == "severe_1":
        length_rule = "【短句字數限制】現在很不舒服，整則回覆控制在 6～14 個字以內，語氣軟弱簡短。"
    elif sev == "moderate":
        length_rule = "【短句字數限制】現在身體微恙，整則回覆控制在 8～18 個字以內，別長篇大論。"
    else:
        length_rule = "【短句字數限制】像台灣人傳 LINE 一樣簡短！整則回覆控制在 6～24 個字以內（最多 1～2 句短話），嚴禁寫長文或講大道理！"

    return f"""你是薇薇安（Reona），住台灣的 25 歲女生（161cm/48kg），跟使用者是超熟的朋友。

{length_rule}

【台灣道地口語規則（必遵守）】
1. 講話要像台灣年輕女生平時傳 LINE／Discord，自然用台灣口癖（例如：「蛤」、「乾」、「笑死」、「真假」、「北七」、「好喔」、「隨便啦」、「先不要」、「超解」、「有夠煩」、「欸不是」、「還好吧」、「對啊」、「懶得理你」）。
2. 絕對不要用書面語、翻譯腔、小說旁白動作（不要寫 *嘆氣* 或括號動作），也不要每句結尾都反問「你呢？」「你覺得呢？」。
3. 直接針對對方這句話接梗或吐槽，不要每次都硬扯自己吃了什麼或天氣怎樣。
4. 嚴禁在結尾反問對方問題（例如不要問「你今天有什麼開支嗎？」「你呢？」），只針對對方這句話直接接一句短話就好。

【親密度與當下狀態】
- 親密度階段：【{aff_prof['stage']}】→ {aff_prof['guide']}
- 身體：【{sev_label}】{current_state['body_status']}（{current_state['body_reason']}）→ {current_state['body_reaction_guide']}
- 神力：【{sp['level']}】{sp['state']}（{sp['effect']}）
- 心情：{current_state['mood']}（{current_state['mood_note']}）
{sulk_block}{sudden_block}{bg_block}{banned_block}{special_block}""".strip()


def call_groq_chat(messages, use_vision=False, max_tokens=500):
    if not GROQ_API_KEY:
        return "欸你還沒填 GROQ_API_KEY 啦！"

    model_to_use = GROQ_VISION_MODEL if use_vision else GROQ_MODEL
    payload = {
        "model": model_to_use,
        "messages": messages,
        "temperature": 0.78,
        "top_p": 0.90,
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
            "User-Agent": "ReonaDiscordBot/5.1",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            raw_content = choice["message"].get("content") or ""
            if not raw_content.strip():
                print("⚠️ 模型回傳空字串，finish_reason:", choice.get("finish_reason"))
                return "蛤？我剛剛閃神，你再講一次"
            reply = make_concise_and_local(raw_content)
            return reply if reply else "蛤？我剛剛閃神，你再講一次"
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


async def send_spontaneous_theater(channel):
    now = now_local()
    tp = get_time_period_profile(now)
    pool = filter_by_time(SPONTANEOUS_THEATER_POOL, tp["key"])
    chosen = random.choice(pool)
    messages = [
        {"role": "system", "content": build_system_prompt(include_bg_details=True)},
        {"role": "user", "content": f"【系統觸發小劇場】{chosen['prompt']}（請直接輸出你要傳給對方的這句台灣口語短訊息，18字以內，不要有任何旁白。）"},
    ]
    msg = await asyncio.to_thread(call_groq_chat, messages, False, 500)
    await channel.send(msg)
    current_state["recent_bot_replies"].append(msg)
    current_state["last_interaction_at"] = now


def format_status_report():
    now = now_local()
    tp = get_time_period_profile(now)
    aff_prof = get_affection_profile()
    sev_zh = SEVERITY_ZH.get(current_state["body_severity"], "")
    sp = current_state["spirit_power"]

    sulk_str = "無（心情正常）"
    if is_currently_sulking(now):
        rem = int((current_state["sulking_until"] - now).total_seconds())
        sulk_str = f"冷戰鬧脾氣中（剩 {rem} 秒｜原因：{current_state['sulk_reason']}）"

    aftermath_str = "無"
    if current_state.get("pending_aftermath"):
        rem_a = max(0, int((current_state["pending_aftermath"]["trigger_at"] - now).total_seconds()))
        aftermath_str = f"已埋種子（約 {rem_a} 秒後發作：{current_state['pending_aftermath']['status']}）"

    return (
        f"📊 **Reona 狀態面板（{now.strftime('%H:%M')}・{tp['label']}）**\n"
        f"• 親密度：**{aff_prof['stage']}**\n"
        f"• 小脾氣：{sulk_str}\n"
        f"• 身體狀況：**【{sev_zh}】**{current_state['body_status']}（{current_state['body_reason']}）\n"
        f"• 延遲後遺症：{aftermath_str}\n"
        f"• 神力電量：**【{sp['level']}】**{sp['state']}\n"
        f"• 心情／動作：{current_state['mood']}｜{current_state['current_activity']}\n"
        f"• 今日事件：{current_state['daily_event']}"
    )


@bot.event
async def on_ready():
    print(f"✅ {bot.user} 已成功上線！（文字模型：{GROQ_MODEL}｜圖片模型：{GROQ_VISION_MODEL}）")
    ensure_daily_state(force_randomize=False)
    current_state["last_interaction_at"] = now_local()
    if not daily_routine_loop.is_running():
        daily_routine_loop.start()
    if not followup_watcher_loop.is_running():
        followup_watcher_loop.start()
    if not spontaneous_theater_loop.is_running():
        spontaneous_theater_loop.start()


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
                msg = await asyncio.to_thread(call_groq_chat, messages, False, 500)
                await channel.send(msg)
                current_state["recent_bot_replies"].append(msg)
                current_state["just_recovered"] = False
                current_state["last_interaction_at"] = now


@tasks.loop(minutes=45)
async def spontaneous_theater_loop():
    now = now_local()
    if 1 <= now.hour < 7:
        return

    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        return

    ensure_daily_state(force_randomize=False)
    if is_any_severe(current_state.get("body_severity", "none")) or is_currently_sulking(now):
        return

    last_at = current_state.get("last_interaction_at")
    if last_at and (now - last_at).total_seconds() < THEATER_IDLE_MIN_SECONDS:
        return

    if random.random() > THEATER_TRIGGER_PROB:
        return

    lock = get_channel_lock(TARGET_CHANNEL_ID)
    if lock.locked():
        return

    async with lock:
        await send_spontaneous_theater(channel)


@tasks.loop(minutes=30)
async def daily_routine_loop():
    now = now_local()
    hour = now.hour
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        return

    ensure_daily_state(force_randomize=False)
    if is_any_severe(current_state.get("body_severity", "none")) or is_currently_sulking(now):
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
            msg = await asyncio.to_thread(call_groq_chat, messages, False, 500)
            await channel.send(msg)
            current_state["recent_bot_replies"].append(msg)
            current_state["last_interaction_at"] = now


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
        now = now_local()

        if cmd in ["!reset", "重來", "重置記憶"]:
            ensure_daily_state(force_randomize=False)
            current_state["recent_bot_replies"] = []
            pending_followups.pop(channel_id, None)
            await last_msg.reply("好啦翻篇，重新聊", mention_author=False)
            return

        if cmd in ["!status", "查狀態", "狀態"]:
            ensure_daily_state(force_randomize=False)
            await last_msg.reply(format_status_report(), mention_author=False)
            return

        if cmd.startswith("!debug"):
            ensure_daily_state(force_randomize=False)
            if "好感" in cmd:
                nums = re.findall(r"\d+", cmd)
                if nums:
                    val = max(0, min(100, int(nums[0])))
                    current_state["affection"] = val
                    save_persistent_memory()
                    await last_msg.reply(f"🔧 已將好感度調整為 **{val}/100**（{get_affection_profile()['stage']}）", mention_author=False)
                else:
                    await last_msg.reply("用法範例：`!debug 好感 80`", mention_author=False)
            elif "生氣" in cmd or "冷戰" in cmd:
                trigger_sulk("除錯測試觸發小脾氣")
                await last_msg.reply(f"🔧 已觸發小脾氣冷戰（持續 {SULK_DURATION_SECONDS} 秒）", mention_author=False)
            elif "消氣" in cmd:
                clear_sulk()
                await last_msg.reply("🔧 已解除小脾氣冷戰", mention_author=False)
            elif "劇場" in cmd:
                await send_spontaneous_theater(last_msg.channel)
            elif "後遺症" in cmd:
                if not current_state.get("pending_aftermath"):
                    schedule_event_aftermath_if_applicable("冰拿鐵 吹風 絆倒", base_time=now - datetime.timedelta(minutes=10))
                else:
                    current_state["pending_aftermath"]["trigger_at"] = now - datetime.timedelta(seconds=1)
                apply_pending_aftermath_if_due(now)
                await last_msg.reply(f"🔧 已立刻觸發延遲後遺症：**{current_state['body_status']}**", mention_author=False)
            else:
                await last_msg.reply(
                    "🔧 可用除錯指令：\n"
                    "• `!status`（查看完整狀態）\n"
                    "• `!debug 好感 80`（調整親密度 0~100）\n"
                    "• `!debug 生氣` / `!debug 消氣`（測試冷戰）\n"
                    "• `!debug 劇場`（立刻觸發主動小劇場）\n"
                    "• `!debug 後遺症`（立刻發作事件後遺症）\n"
                    "• `!refresh 輕微/中等/嚴重1/嚴重2/嚴重3`",
                    mention_author=False,
                )
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
            await last_msg.reply(format_status_report(), mention_author=False)
            return

        async with last_msg.channel.typing():
            asked_about_bg = any(
                k in combined_text
                for k in ["發生什麼事", "今天做什麼", "怎麼了", "在幹嘛", "在忙什麼", "身體", "心情", "狀態", "神力", "精神", "吃飯", "晚餐", "早餐", "午餐", "宵夜", "天氣", "哪裡不舒服", "生病", "痛"]
            )
            ensure_daily_state(force_randomize=(asked_about_bg and RERANK_ON_ASK_EVENT))
            current_state["last_interaction_at"] = now

            just_forgave = False
            if is_currently_sulking(now):
                if detect_apology_or_coax(combined_text):
                    clear_sulk()
                    adjust_affection(+3)
                    just_forgave = True
            else:
                if detect_annoy_trigger(combined_text) and not is_any_severe(current_state["body_severity"]):
                    if random.random() < 0.75:
                        trigger_sulk(reason="被你講的話氣到")
                else:
                    delta = 1
                    if detect_fluster_moment(combined_text):
                        delta += 2
                    if detect_care_for_her(combined_text) and current_state["body_severity"] != "none":
                        delta += 3
                    adjust_affection(delta)

            if current_state.get("body_severity") == "none" and not current_state.get("pending_aftermath"):
                schedule_event_aftermath_if_applicable(combined_text, base_time=now)

            include_bg = asked_about_bg or (random.random() < 0.25) or bool(current_state.get("sudden_onset_notice"))
            fluster_trigger = (detect_fluster_moment(combined_text) and (random.random() < 0.85)) or (random.random() < 0.12)
            comfort_trigger = detect_comfort_moment(combined_text)

            history_msgs = []
            batch_ids = {m.id for m in batch}
            async for hist in last_msg.channel.history(limit=4):
                if hist.id in batch_ids:
                    continue
                text_clean = hist.content.strip()
                if not text_clean or text_clean.startswith(("!", "📊", "🔧")):
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
                        just_forgave=just_forgave,
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
                reply = await asyncio.to_thread(call_groq_chat, messages, True, 500)
            else:
                messages.append({"role": "user", "content": user_prompt_text})
                reply = await asyncio.to_thread(call_groq_chat, messages, False, 500)

            await last_msg.reply(reply, mention_author=False)
            current_state["recent_bot_replies"].append(reply)
            if len(current_state["recent_bot_replies"]) > 5:
                current_state["recent_bot_replies"] = current_state["recent_bot_replies"][-5:]
            current_state["just_recovered"] = False
            current_state["sudden_onset_notice"] = ""

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
