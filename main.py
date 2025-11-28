import discord
from discord.ext import commands, tasks
import aiohttp
import datetime
import pytz
import json
import os
import re
import asyncio
import sys
from curl_cffi.requests import AsyncSession 
from google.cloud import translate_v2 as translate
from google.oauth2 import service_account

# ================== 1. 系统配置 ==================
sys.stdout.reconfigure(line_buffering=True)

TOKEN = os.getenv('TOKEN')
FMP_KEY = os.getenv('FMP_KEY') 
SETTINGS_FILE = '/data/settings.json' 

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 时区
ET = pytz.timezone('America/New_York')
BJT = pytz.timezone('Asia/Shanghai')
UTC = pytz.UTC

# ================== 2. 数据源 URL ==================
FMP_CAL_URL = "https://financialmodelingprep.com/stable/economic-calendar"
NASDAQ_CAL_URL = "https://api.nasdaq.com/api/calendar/earnings"
GITHUB_SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"

# ================== 3. 核心关注名单 (全量覆盖) ==================
HOT_STOCKS = {
    # === 用户指定补充 ===
    "LMND", "HIMS", "AMKR", "TEM", 
    "OPEN", "APP", "MP", "CRCL", "BMNR", "CRWV", "NBIS",
    
    # === 热门成长 & 消费新贵 ===
    "CAVA", "SG", "ONON", "CELH", "ELF", "DUOL", "CART", "KVUE", "ROOT",
    
    # === 核电 / 铀矿 / AI能源 ===
    "OKLO", "SMR", "NNE", "LBRT", "CCJ", "LEU", "UEC", "NXE", "BWXT",
    "VST", "CEG", "NRG", "GEV", "TLN", "NEE", "SO",
    
    # === 量子计算 & 硬科技 ===
    "IONQ", "RGTI", "QBTS", "QUBT", "ARQQ", "ALAB", "RDDT",
    
    # === WSB / Meme / 高波动 ===
    "GME", "AMC", "DJT", "CHWY", "KOSS", "BB", "SPCE", "RKLB", "ASTS", "LUNR",
    "CVNA", "UPST", "AFRM", "AI", "SOUN", "BBAI",
    
    # === 顶级流量/七巨头 ===
    "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOG", "GOOGL", "META", "NFLX",
    
    # === 芯片/半导体 ===
    "AMD", "INTC", "TSM", "ASML", "ARM", "AVGO", "QCOM", "MU", "SMCI", "MRVL",
    
    # === 加密货币 ===
    "MSTR", "COIN", "MARA", "RIOT", "CLSK", "HOOD", "BITF", "HUT", "IREN",
    
    # === SaaS / 云计算 ===
    "CRWD", "PANW", "ZS", "NET", "DDOG", "SNOW", "PLTR", "PATH", "MDB", 
    "TEAM", "WDAY", "ADBE", "CRM", "U", "DKNG", "ROKU", "SHOP", "SQ", "ZM",
    
    # === 新能源汽车 ===
    "RIVN", "LCID", "NIO", "XPEV", "LI", "FSLR", "ENPH", "PLUG",
    
    # === 热门中概 ===
    "BABA", "PDD", "JD", "BIDU", "BILI", "FUTU", "TIGR", "YUMC", "LKNCY"
}

FALLBACK_GIANTS = {"NVDA", "AAPL", "MSFT", "AMZN", "TSLA", "GOOG", "META"}

SPEECH_KEYWORDS = ["Speech", "Testimony", "Remarks", "Press Conference", "Hearing"]
WEEKDAY_MAP = {
    'Monday': '周一', 'Tuesday': '周二', 'Wednesday': '周三', 'Thursday': '周四',
    'Friday': '周五', 'Saturday': '周六', 'Sunday': '周日'
}
IMPACT_MAP = {"Low": 1, "Medium": 2, "High": 3}

# 全局变量
settings = {}
sp500_symbols = set() 
translate_client = None

# ================== 4. 辅助工具函数 ==================
def log(msg):
    print(msg, flush=True)

def safe_print_error(prefix, error_obj):
    err_str = str(error_obj)
    if FMP_KEY:
        err_str = err_str.replace(FMP_KEY, "******")
    log(f"❌ {prefix}: {err_str}")

# 初始化 Google 翻译
google_json_str = os.getenv('GOOGLE_JSON_CONTENT') 
google_key_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
try:
    if google_json_str:
        cred_info = json.loads(google_json_str)
        credentials = service_account.Credentials.from_service_account_info(cred_info)
        translate_client = translate.Client(credentials=credentials)
        log('✅ Google Translate SDK (Env) 初始化成功')
    elif google_key_path and os.path.exists(google_key_path):
        credentials = service_account.Credentials.from_service_account_file(google_key_path)
        translate_client = translate.Client(credentials=credentials)
        log('✅ Google Translate SDK (File) 初始化成功')
except Exception as e:
    safe_print_error("SDK 初始化失败", e)

def load_settings():
    global settings
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
                settings = {int(k): v for k, v in raw.items()}
            log(f"已加载设置: {len(settings)} 个服务器")
        except Exception as e:
            log(f"加载设置失败: {e}")
            settings = {}

def save_settings():
    try:
        os.makedirs('/data', exist_ok=True)
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)
    except Exception as e:
        log(f"保存设置失败: {e}")

def clean_title(title):
    return re.sub(r'\s*\([^)]*\)', '', str(title)).strip()

# === 核心修复：异步封装翻译函数，防止阻塞 ===
async def translate_finance_text(text, target_lang='zh'):
    if not text or not translate_client: return str(text).strip()
    text = str(text).strip()
    if re.match(r'^-?\d+(\.\d+)?%?$', text): return text
    
    # 将同步的 Google API 调用放入线程池运行
    try:
        def _do_translate():
            # 内部检测
            if translate_client.detect_language(text)['language'].startswith('zh'):
                return text
            result = translate_client.translate(text, source_language='en', target_language=target_lang)
            return result['translatedText']

        # 使用 asyncio.to_thread (Python 3.9+) 防止卡死
        t = await asyncio.to_thread(_do_translate)
        
        for abbr in ['CPI', 'PPI', 'GDP', 'FOMC', 'Fed', 'YoY', 'MoM']:
            t = re.sub(rf'\b{abbr}\b', abbr, t, flags=re.IGNORECASE)
        return t.strip()
    except Exception as e:
        # 出错不打印堆栈，直接返回原文，避免日志爆炸
        return text

# ================== 5. 核心逻辑：更新白名单 ==================
async def update_sp500_list():
    global sp500_symbols
    log("🔄 正在从 GitHub 更新 S&P 500 名单...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(GITHUB_SP500_URL, timeout=15) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    new_list = set()
                    for line in text.split('\n')[1:]:
                        parts = line.split(',')
                        if parts and parts[0]:
                            new_list.add(parts[0].strip().replace('.', '-'))
                    
                    if len(new_list) > 400:
                        sp500_symbols = new_list
                        log(f"✅ S&P 500 更新成功: {len(sp500_symbols)} 只")
                    else:
                        log("⚠️ GitHub 数据异常，使用备用名单")
                        sp500_symbols.update(FALLBACK_GIANTS)
                else:
                    log(f"⚠️ GitHub 访问失败: {resp.status}")
                    sp500_symbols.update(FALLBACK_GIANTS)
        except Exception as e:
            safe_print_error("更新名单失败", e)
            sp500_symbols.update(FALLBACK_GIANTS)

# ================== 6. 核心逻辑：宏观日历 (FMP) ==================
async def fetch_us_events(target_date_str, min_importance=2):
    try: target_date = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except: return []
    
    params = {"from": target_date_str, "to": target_date_str, "apikey": FMP_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(FMP_CAL_URL, params=params, timeout=10) as resp:
                resp.raise_for_status()
                data = await resp.json()
        
        events = []
        start = BJT.localize(datetime.datetime.combine(target_date, datetime.time(8, 0)))
        end = start + datetime.timedelta(days=1)
        
        # 预筛选，减少后续循环次数
        valid_items = []
        for item in data:
            if item.get("country") != "US": continue
            imp = IMPACT_MAP.get(item.get("impact", "Low").capitalize(), 1)
            if imp < min_importance: continue
            
            dt_str = item.get("date")
            if not dt_str: continue
            
            try:
                utc = UTC.localize(datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S"))
                bjt = utc.astimezone(BJT)
                if start <= bjt < end:
                    item['_bjt'] = bjt
                    item['_et'] = utc.astimezone(ET)
                    item['_imp'] = imp
                    valid_items.append(item)
            except: continue

        # 处理翻译和构建对象
        for item in valid_items:
            bjt = item['_bjt']
            et = item['_et']
            imp = item['_imp']
            
            time_str = f"{bjt.strftime('%H:%M')} ({et.strftime('%H:%M')} ET)"
            title = clean_title(item.get("event", ""))
            
            # === 这里使用 await 调用修复后的异步翻译 ===
            trans_title = await translate_finance_text(title)
            trans_forecast = await translate_finance_text(item.get("estimate", "") or "—")
            trans_prev = await translate_finance_text(item.get("previous", "") or "—")
            
            events.append({
                "time": time_str,
                "importance": "★" * imp,
                "title": trans_title,
                "forecast": trans_forecast,
                "previous": trans_prev,
                "orig_title": title,
                "bjt_timestamp": bjt
            })
        
        unique_events = {}
        for e in events:
            key = e['title']
            if key not in unique_events or e['bjt_timestamp'] < unique_events[key]['bjt_timestamp']:
                unique_events[key] = e
        return sorted(unique_events.values(), key=lambda x: x["bjt_timestamp"])
    except Exception as e:
        safe_print_error("Events API Error", e)
        return []

# ================== 7. 核心逻辑：财报获取 (超级兜底版) ==================
async def fetch_earnings(date_str):
    if not sp500_symbols: await update_sp500_list()
    
    log(f"🚀 [Nasdaq] 正在获取财报数据: {date_str}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
        "Accept": "application/json, text/plain, */*"
    }
    
    params = {"date": date_str}

    try:
        async with AsyncSession(impersonate="chrome110") as session:
            resp = await session.get(NASDAQ_CAL_URL, params=params, headers=headers, timeout=15)
            
            if resp.status_code != 200:
                log(f"❌ Nasdaq API 返回错误: {resp.status_code}")
                return []
            
            try:
                data = resp.json()
            except:
                log("❌ Nasdaq 返回非 JSON 数据")
                return []

            rows = data.get('data', {}).get('rows', [])
            if not rows:
                log("⚠️ Nasdaq 返回空数据")
                return []

            important_stocks = []
            
            # === 🌟 超级兜底字典 (覆盖 HOT_STOCKS 中 99% 的股票) ===
            FALLBACK_MAP = {
                # --- ☀️ 盘前 (能源、中概、传统、消费、非美芯片) ---
                # 中概
                "BABA": 1, "JD": 1, "BIDU": 1, "PDD": 1, "NIO": 1, "LI": 1, "XPEV": 1, "BILI": 1, "FUTU": 1, "TIGR": 1, "YUMC": 1, "LKNCY": 1,
                # 芯片 (非美)
                "TSM": 1, "ASML": 1,
                # 消费/零售/传统
                "ADI": 1, "BBY": 1, "SJM": 1, "LOW": 1, "TGT": 1, "MCD": 1, "MCK": 1, "EMR": 1, "JCI": 1, "SRE": 1, "ALL": 1, "MET": 1,
                "ONON": 1, "CELH": 1, "KVUE": 1, "CHWY": 1, "LUNR": 1,
                # 电力/核电/公用事业
                "CCJ": 1, "LEU": 1, "NXE": 1, "TLN": 1, "VST": 1, "CEG": 1, "NEE": 1, "SO": 1, "NRG": 1, "GEV": 1, "PLUG": 1,
                # 互联网 (部分)
                "DDOG": 1, "SHOP": 1, "DKNG": 1,

                # --- 🌙 盘后 (科技、芯片、SaaS、加密、WSB、成长) ---
                # 科技巨头
                "NVDA": 2, "AMD": 2, "INTC": 2, "AAPL": 2, "MSFT": 2, "GOOG": 2, 
                "AMZN": 2, "META": 2, "TSLA": 2, "NFLX": 2,
                # 芯片 (美国)
                "QCOM": 2, "ARM": 2, "AVGO": 2, "MU": 2, "SMCI": 2, "MRVL": 2, "AMKR": 2, "ALAB": 2, "TEM": 2,
                # 软件/SaaS
                "CRWD": 2, "PANW": 2, "ZS": 2, "NET": 2, "SNOW": 2, "PLTR": 2, "PATH": 2, "MDB": 2, 
                "TEAM": 2, "WDAY": 2, "ADBE": 2, "CRM": 2, "U": 2, "ROKU": 2, "SQ": 2, "ZM": 2,
                "APP": 2, "OPEN": 2, "LMND": 2, "HIMS": 2, "DUOL": 2, "FTNT": 2, "DASH": 2,
                # 加密货币
                "MSTR": 2, "COIN": 2, "HOOD": 2, "MARA": 2, "RIOT": 2, "CLSK": 2, "BITF": 2, "HUT": 2, "IREN": 2,
                # WSB / Meme / 太空 / 妖股
                "GME": 2, "AMC": 2, "DJT": 2, "KOSS": 2, "BB": 2, "RDDT": 2,
                "RKLB": 2, "ASTS": 2, "SPCE": 2, "AI": 2, "SOUN": 2, "BBAI": 2, "ROOT": 2, "CVNA": 2, "UPST": 2, "AFRM": 2,
                # EV
                "RIVN": 2, "LCID": 2, "FSLR": 2, "ENPH": 2,
                # 核电/量子 (新兴)
                "OKLO": 2, "SMR": 2, "NNE": 2, "LBRT": 2, "UEC": 2, "BWXT": 2, "IONQ": 2, "RGTI": 2, "QBTS": 2, "QUBT": 2,
                # 消费新贵
                "CAVA": 2, "SG": 2, "CART": 2, "ELF": 2
            }

            for item in rows:
                raw_symbol = item.get('symbol')
                symbol = re.sub(r'[^A-Z]', '', str(raw_symbol).upper())
                time_str = item.get('time', 'other')
                
                is_hot = symbol in HOT_STOCKS
                is_sp500 = symbol in sp500_symbols
                
                if is_hot or is_sp500:
                    time_code = 'other'
                    t_lower = time_str.lower()
                    
                    if "before" in t_lower or "open" in t_lower: 
                        time_code = 'bmo'
                    elif "after" in t_lower or "close" in t_lower: 
                        time_code = 'amc'
                    
                    # 兜底逻辑生效
                    if time_code == 'other':
                        if symbol in FALLBACK_MAP:
                            guess = FALLBACK_MAP[symbol]
                            time_code = 'bmo' if guess == 1 else 'amc'
                        else:
                            pass

                    important_stocks.append({
                        'symbol': symbol,
                        'time': time_code,
                        'is_hot': is_hot
                    })
            
            unique_dict = {s['symbol']: s for s in important_stocks}
            final_list = list(unique_dict.values())
            final_list.sort(key=lambda x: x['is_hot'], reverse=True)
            
            log(f"✅ Nasdaq 获取完成，筛选后剩余 {len(final_list)} 家")
            return final_list

    except Exception as e:
        safe_print_error("Nasdaq API Error", e)
        return []

# ================== 8. 格式化输出 (防截断 + 自动分页) ==================
def format_calendar_embed(events, date_str, min_imp):
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        month_day = dt.strftime("%m月%d日")
        weekday_cn = WEEKDAY_MAP.get(dt.strftime('%A'), '')
        base_title = f"今日热点 ({month_day}/{weekday_cn})"
    except:
        base_title = f"今日热点 ({date_str})"

    if not events: return [discord.Embed(title=base_title, description="无重要事件", color=0x00FF00)]
    
    # === 核心修复：自动分页 (每25个事件一组) ===
    # Discord 限制每个 Embed 最多 25 个 Field
    embeds = []
    chunk_size = 25
    
    for i in range(0, len(events), chunk_size):
        chunk = events[i:i + chunk_size]
        
        # 如果有分页，标题加页码
        title = base_title
        if len(events) > chunk_size:
            page = (i // chunk_size) + 1
            total_pages = (len(events) + chunk_size - 1) // chunk_size
            title = f"{base_title} ({page}/{total_pages})"
            
        embed = discord.Embed(title=title, color=0x00FF00)
        
        for e in chunk:
            val = f"影响: {e['importance']}" if any(k in e['orig_title'] for k in SPEECH_KEYWORDS) else \
                  f"影响: {e['importance']} | 预期: {e['forecast']} | 前值: {e['previous']}"
            embed.add_field(name=f"{e['time']} {e['title']}", value=val, inline=False)
        
        embeds.append(embed)
        
    return embeds

def format_earnings_embed(stocks, date_str):
    if not stocks: return None
    
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        month_day = dt.strftime("%m月%d日")
        weekday_cn = WEEKDAY_MAP.get(dt.strftime('%A'), '')
        title = f"重点财报 ({month_day}/{weekday_cn})"
    except:
        title = f"重点财报 ({date_str})"

    embed = discord.Embed(title=title, color=0xf1c40f)
    
    # === 智能防截断构建函数 ===
    def build_safe_list(items):
        limit = 1000 # 安全限制
        current_len = 0
        parts = []
        
        for i, s in enumerate(items):
            icon = "🔥" if s['is_hot'] else ""
            # 蓝色字体链接
            entry = f"{icon}[{s['symbol']}](https://finance.yahoo.com/quote/{s['symbol']})"
            
            # 预计算长度 (+3 是因为 " , " 占3个字符)
            entry_len = len(entry) + 3
            
            if current_len + entry_len > limit:
                remaining = len(items) - i
                parts.append(f"...(还有{remaining}家)")
                break
            
            parts.append(entry)
            current_len += entry_len
            
        return " , ".join(parts)

    bmo = [s for s in stocks if s['time'] == 'bmo']
    amc = [s for s in stocks if s['time'] == 'amc']
    other = [s for s in stocks if s['time'] == 'other']

    if bmo: 
        embed.add_field(name="☀️ 盘前", value=build_safe_list(bmo), inline=False)
    
    if amc: 
        embed.add_field(name="🌙 盘后", value=build_safe_list(amc), inline=False)
    
    if other:
        embed.add_field(name="🕒 时间未定", value=build_safe_list(other), inline=False)

    embed.set_footer(text="数据来源: Nasdaq")
    return embed

# ================== 9. 定时任务与事件 ==================
@tasks.loop(minutes=1)
async def main_loop():
    now = datetime.datetime.now(BJT)
    # 08:00 宏观
    if now.hour == 8 and 0 <= now.minute < 5:
        today = now.strftime("%Y-%m-%d")
        lock = f"/data/evt_{today}.lock"
        if not os.path.exists(lock):
            with open(lock, "w") as f: f.write("x")
            log(f"🚀 推送宏观: {today}")
            for gid, conf in settings.items():
                ch = bot.get_channel(conf.get('channel_id'))
                if ch:
                    evts = await fetch_us_events(today, conf.get('min_importance', 2))
                    # format_calendar_embed 现在返回一个列表，需要循环发送
                    embed_list = format_calendar_embed(evts, today, conf.get('min_importance', 2))
                    for em in embed_list:
                        await ch.send(embed=em)

    # 20:00 财报
    elif now.hour == 20 and 0 <= now.minute < 5:
        tmr = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        lock = f"/data/ern_{tmr}.lock"
        if not os.path.exists(lock):
            with open(lock, "w") as f: f.write("x")
            await update_sp500_list()
            log(f"🚀 推送财报: {tmr}")
            data = await fetch_earnings(tmr)
            embed = format_earnings_embed(data, tmr)
            if embed:
                for gid, conf in settings.items():
                    ch = bot.get_channel(conf.get('channel_id'))
                    if ch: await ch.send(embed=embed)

@main_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    load_settings()
    log(f'✅ Bot 已登录: {bot.user}')
    await bot.tree.sync()
    await update_sp500_list()
    if not main_loop.is_running(): main_loop.start()

# ================== 10. 命令 ==================
@bot.tree.command(name="set_channel", description="设置推送频道")
async def set_channel(interaction: discord.Interaction):
    gid = interaction.guild_id
    if gid not in settings: settings[gid] = {}
    settings[gid]['channel_id'] = interaction.channel_id
    save_settings()
    await interaction.response.send_message(f"✅ 绑定成功", ephemeral=True)

@bot.tree.command(name="test_earnings", description="测试财报")
async def test_earnings(interaction: discord.Interaction, date: str = None):
    await interaction.response.defer()
    if not date: date = (datetime.datetime.now(BJT) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    stocks = await fetch_earnings(date)
    embed = format_earnings_embed(stocks, date)
    if embed: await interaction.followup.send(embed=embed)
    else: await interaction.followup.send(f"📅 **{date}** 无重点财报", ephemeral=True)

@bot.tree.command(name="test_push", description="测试宏观日历")
async def test_push(interaction: discord.Interaction):
    await interaction.response.defer()
    today = datetime.datetime.now(BJT).strftime("%Y-%m-%d")
    evts = await fetch_us_events(today, 2)
    embed_list = format_calendar_embed(evts, today, 2)
    for em in embed_list:
        await interaction.followup.send(embed=em)

@bot.tree.command(name="set_importance", description="设置宏观事件最低星级")
@discord.app_commands.choices(level=[
    discord.app_commands.Choice(name="★ (全部)", value=1),
    discord.app_commands.Choice(name="★★ (中高)", value=2),
    discord.app_commands.Choice(name="★★★ (高)", value=3),
])
async def set_importance(interaction: discord.Interaction, level: discord.app_commands.Choice[int]):
    gid = interaction.guild_id
    if gid not in settings: settings[gid] = {}
    settings[gid]['min_importance'] = level.value
    save_settings()
    await interaction.response.send_message(f"✅ 最低星级设为 {level.name}", ephemeral=True)

@bot.tree.command(name="disable_push", description="关闭本服务器推送")
async def disable_push(interaction: discord.Interaction):
    gid = interaction.guild_id
    if gid in settings:
        del settings[gid]
        save_settings()
        await interaction.response.send_message("🚫 已关闭本服务器推送", ephemeral=True)
    else:
        await interaction.response.send_message("本服务器未开启推送", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)
