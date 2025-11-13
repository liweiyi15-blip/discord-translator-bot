import discord
from discord.ext import commands
from discord import app_commands
import requests
import os
from datetime import datetime, timedelta
import pytz

# ===== 环境变量 =====
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FMP_API_KEY = os.getenv("FMP_API_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

# ===== Bot 对象定义 =====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="$", intents=intents)

# ===== 工具函数 =====
def get_ny_time():
    tz = pytz.timezone('America/New_York')
    return datetime.now(tz)

def market_status():
    now = get_ny_time()
    weekday = now.weekday()
    if weekday >= 5:
        return "closed_night"
    
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    aftermarket_end = now.replace(hour=20, minute=0, second=0, microsecond=0)
    premarket_start = now.replace(hour=4, minute=0, second=0, microsecond=0)

    if premarket_start <= now < open_time:
        return "pre_market"
    elif open_time <= now <= close_time:
        return "open"
    elif close_time < now <= aftermarket_end:
        return "aftermarket"
    else:
        return "closed_night"

# ===== 数据源函数 =====
def fetch_finnhub_quote(symbol: str):
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
        if not data or data.get("c") == 0:
            return None
        return data
    except Exception as e:
        print(f"Finnhub 查询失败: {e}")
        return None

def fetch_fmp_stock(symbol: str):
    try:
        url = f"https://financialmodelingprep.com/stable/quote/{symbol}?apikey={FMP_API_KEY}"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
        if not data or len(data) == 0:
            return None
        print(f"[DEBUG] FMP stable quote raw: {data[0]}")  # 打印 raw 诊断
        return data[0]
    except Exception as e:
        print(f"FMP stock 查询失败: {e}")
        return None

def fetch_fmp_aftermarket(symbol: str):
    try:
        url = f"https://financialmodelingprep.com/stable/aftermarket-quote?symbol={symbol}&apikey={FMP_API_KEY}"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"[DEBUG] FMP aftermarket API 失败: {response.status_code}")
            return None
        data = response.json()
        if not data or len(data) == 0:
            print(f"[DEBUG] FMP aftermarket 无数据")
            return None
        item = data[0]
        print(f"[DEBUG] FMP aftermarket raw: {item}")  # 打印 raw
        if 'bidPrice' in item and item['bidPrice'] is not None and item['bidPrice'] > 0:
            print(f"[DEBUG] FMP aftermarket 使用 bidPrice: {item['bidPrice']}")
            return {"bidPrice": item['bidPrice']}
        print(f"[DEBUG] FMP aftermarket 无有效 bidPrice")
        return None
    except Exception as e:
        print(f"FMP aftermarket 查询失败: {e}")
        return None

def fetch_fmp_premarket(symbol: str):
    try:
        url = f"https://financialmodelingprep.com/stable/premarket-quote?symbol={symbol}&apikey={FMP_API_KEY}"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"[DEBUG] FMP premarket API 失败: {response.status_code}")
            return None
        data = response.json()
        if not data or len(data) == 0:
            print(f"[DEBUG] FMP premarket 无数据")
            return None
        item = data[0]
        print(f"[DEBUG] FMP premarket raw: {item}")  # 打印 raw
        if 'bidPrice' in item and item['bidPrice'] is not None and item['bidPrice'] > 0:
            print(f"[DEBUG] FMP premarket 使用 bidPrice: {item['bidPrice']}")
            return {"bidPrice": item['bidPrice']}
        print(f"[DEBUG] FMP premarket 无有效 bidPrice")
        return None
    except Exception as e:
        print(f"FMP premarket 查询失败: {e}")
        return None

# ===== /stock 命令 =====
@bot.tree.command(name="stock", description="查询美股实时价格（支持盘前/盘后）")
@app_commands.describe(symbol="股票代码，例如 TSLA")
async def stock(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer()

    symbol = symbol.upper().strip()
    status = market_status()
    print(f"[DEBUG] 查询 {symbol}，状态: {status}")

    price_to_show = change_amount = change_pct = None
    use_fallback = False
    fallback_note = "🚫 该时段不支持实时查询，使用前收盘价。"

    # 开盘优先 Finnhub
    if status == "open":
        fh = fetch_finnhub_quote(symbol)
        if fh and fh["c"] != 0:
            price_to_show = fh["c"]
            change_amount = fh.get("d", 0)
            change_pct = fh.get("dp", 0)
            print(f"使用 Finnhub 开盘数据: {symbol} - {price_to_show} (d={change_amount}, dp={change_pct}%)")
        else:
            print(f"[DEBUG] Finnhub 开盘失败，回退 FMP")
            fmp = fetch_fmp_stock(symbol)
            if fmp:
                stock_price = fmp.get("price") or fmp.get("lastPrice")
                prev_close = fmp.get("previousClose") or fmp.get("prevClose")
                if not stock_price or not prev_close:
                    await interaction.followup.send("未找到该股票数据")
                    return
                price_to_show = stock_price
                change_amount = fmp.get("change") or (stock_price - prev_close)
                change_pct = fmp.get("changesPercentage") or ((change_amount / prev_close) * 100)
                print(f"使用 FMP 开盘数据: {symbol} - {price_to_show}")
            else:
                await interaction.followup.send("未找到该股票，或当前无实时数据")
                return
    else:
        # 其余时段优先 FMP
        fmp = fetch_fmp_stock(symbol)
        stock_price = None
        if fmp:
            stock_price = fmp.get("price") or fmp.get("lastPrice")
            prev_close = fmp.get("previousClose") or fmp.get("prevClose")
            if not stock_price or not prev_close:
                fmp = None

        extended_price = None
        if status == "pre_market":
            extended = fetch_fmp_premarket(symbol)
            if extended and extended.get("bidPrice"):
                extended_price = extended["bidPrice"]
        elif status == "aftermarket":
            extended = fetch_fmp_aftermarket(symbol)
            if extended and extended.get("bidPrice"):
                extended_price = extended["bidPrice"]

        if extended_price:
            price_to_show = extended_price
            # extended 涨跌: 相对 regular close (stock_price 是上一个收盘价/regular close)
            if stock_price:
                change_amount = extended_price - stock_price
                change_pct = (change_amount / stock_price) * 100
            else:
                change_amount = 0
                change_pct = 0
            print(f"使用 FMP 扩展时段数据: {symbol} - {price_to_show} (vs regular close {stock_price}, change={change_amount:+.2f} ({change_pct:+.2f}%)")
            use_fallback = False  # 有 extended, 无备注
        elif fmp and stock_price:
            # 有 regular 但无 extended: fallback
            use_fallback = True
        else:
            use_fallback = True

        if use_fallback:
            print(f"[DEBUG] 无 FMP extended，回退 Finnhub")
            fh = fetch_finnhub_quote(symbol)
            if fh and fh["c"] != 0:
                price_to_show = fh["c"]
                change_amount = fh.get("d", 0)
                change_pct = fh.get("dp", 0)
                print(f"使用 Finnhub fallback: {symbol} - {price_to_show} (d={change_amount}, dp={change_pct}%)")
            else:
                if fh and fh["pc"] != 0:
                    price_to_show = fh["pc"]
                    change_amount = 0
                    change_pct = 0
                    print(f"使用 Finnhub pc fallback: {symbol} - {price_to_show}")
                else:
                    await interaction.followup.send("未找到该股票，或当前无数据")
                    return

    # 根据涨跌选择表情
    emoji = "📈" if change_amount >= 0 else "📉"

    # 定义市场时段标签
    label_map = {
        "pre_market": "盘前",
        "open": "盘中",
        "aftermarket": "盘后",
        "closed_night": "收盘"
    }
    label = label_map.get(status, "未知")

    # 构建消息
    msg = f"{emoji} **{symbol}** ({label})\n"
    msg += f"当前价: `${price_to_show:.2f}`\n"
    msg += f"涨跌: `${change_amount:+.2f}` (`{change_pct:+.2f}`%)"

    if use_fallback:
        msg += f"\n{fallback_note}"

    await interaction.followup.send(msg)

# ===== 启动事件 =====
@bot.event
async def on_ready():
    await bot.tree.sync()
    ny_time = get_ny_time().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"Bot 已上线: {bot.user}")
    print(f"纽约时间: {ny_time}")
    print(f"Slash 命令已同步")

# ===== 启动 Bot =====
bot.run(DISCORD_TOKEN)
