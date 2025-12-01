import discord
from discord.ext import commands
import asyncio
import os
import json
from google.cloud import translate_v2 as translate
from google.oauth2 import service_account
import re
import functools

# ==================== 配置区域 ====================
TOKEN = os.getenv('DISCORD_TOKEN')
MIN_WORDS = 5
DEBUG = True

# 适配 Railway 的持久化存储
DATA_DIR = os.getenv('DATA_DIR', '.') 
CONFIG_FILE = os.path.join(DATA_DIR, 'bot_config.json')

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ==================== Google SDK 初始化 ====================
json_key = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
if json_key:
    try:
        credentials = service_account.Credentials.from_service_account_info(json.loads(json_key))
        client = translate.Client(credentials=credentials)
        print('✅ Google Translate SDK 初始化成功')
    except Exception as e:
        print(f'❌ SDK 初始化失败: {e}')
        client = None
else:
    print('⚠️ JSON Key 未设置')
    client = None

# ==================== 状态存储与持久化 ====================
webhook_cache = {} 

global_config = {
    "channel_modes": {},      # 开关: replace/off
    "bot_mappings": {},       # 定向监听
    "output_styles": {},      # 样式: auto/flat/embed
    "processing_scopes": {}   # 【新】范围: translate_only/all
}

def load_config():
    """从持久化文件加载配置"""
    global global_config
    if DATA_DIR != '.' and not os.path.exists(DATA_DIR):
        try: os.makedirs(DATA_DIR, exist_ok=True)
        except: pass

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for key in global_config.keys():
                    if key in data:
                        global_config[key] = data[key]
            print(f"📂 配置已加载")
        except Exception as e:
            print(f"❌ 加载失败: {e}")
    else:
        print(f"📂 无配置文件，将在首次保存时创建")

def save_config():
    """保存配置"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(global_config, f, ensure_ascii=False, indent=4)
        if DEBUG: print("💾 配置已落盘")
    except Exception as e:
        print(f"❌ 保存失败: {e}")

# ==================== 核心功能函数 ====================

def log(message):
    if DEBUG:
        print(message)

def clean_text(text):
    if not text: return ""
    # 1. 去除 Markdown 链接 (保留文字)
    text = re.sub(r'\[([^\]]*)\]\(https?://\S+\)', r'\1', text) 
    # 2. 去除裸 URL 
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    # 3. 清理残留符号
    text = text.replace('[](', '').replace('[]', '')
    text = re.sub(r'\[\s*\]\(\s*\)', '', text) 
    text = re.sub(r'\[.*?\]\(\s*\)', '', text)
    # 4. 去除特定 Emoji
    text = text.replace('📷', '')
    return text.strip()

def translate_text_sync(text):
    # 先清洗
    text = clean_text(text)
    if not text: return ""
    if len(text.split()) < 1 and not len(text) > 10: return text
    
    # ⚠️ 关键：如果包含中文，直接返回清洗后的原文 (不翻译)
    if re.search(r'[\u4e00-\u9fff]', text):
        return text
    
    mention_placeholders = {}
    counter = 0
    for mention in ['@everyone', '@here']:
        placeholder = f"@@PROTECTED_MENTION_{counter}@@"
        text = text.replace(mention, placeholder)
        mention_placeholders[placeholder] = mention
        counter += 1

    def protect_mention(match):
        nonlocal counter
        placeholder = f"@@PROTECTED_MENTION_{counter}@@"
        mention_placeholders[placeholder] = match.group(0)
        counter += 1
        return placeholder

    text = re.sub(r'<@!?&?\d+>', protect_mention, text)

    try:
        if not client: return text
        detection = client.detect_language(text)
        if detection['language'].startswith('zh'):
            return text
        result = client.translate(
            text, source_language='en', target_language='zh-CN', format_='text'
        )['translatedText']
        
        result = result.replace(' \n', '\n').replace('\n ', '\n')
        orig_double_newlines = text.count('\n\n')
        trans_double_newlines = result.count('\n\n')
        if trans_double_newlines > orig_double_newlines:
             result = re.sub(r'\n+', '\n', result)
        
    except Exception as e:
        print(f'❌ 翻译异常: {e}')
        return text

    for placeholder, original in mention_placeholders.items():
        result = result.replace(placeholder, original)

    return result

async def async_translate_text(text):
    if not text: return ""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(translate_text_sync, text))

async def process_message_content(message):
    """提取和翻译消息"""
    parts = {'content': message.content or "", 'embeds': [], 'image_urls': []}
    
    # 记录原始内容 (用于比对)
    original_raw_content = message.content or ""

    if parts['content']:
        parts['content'] = await async_translate_text(parts['content'])

    if message.attachments:
        for attachment in message.attachments:
            parts['image_urls'].append(attachment.url)

    for embed in message.embeds:
        should_rebuild_embed = False
        if embed.type in ['rich', 'article']:
            should_rebuild_embed = True
        
        has_text = bool(embed.title or embed.description or embed.fields or (embed.footer and embed.footer.text))
        
        if should_rebuild_embed:
            embed_data = {
                'title': await async_translate_text(embed.title) if embed.title else "",
                'description': await async_translate_text(embed.description) if embed.description else "",
                'color': embed.color.value if embed.color else None,
                'url': embed.url,
                'timestamp': embed.timestamp,
                'author': {
                    'name': embed.author.name if embed.author else None,
                    'icon_url': embed.author.icon_url if embed.author else None
                },
                'footer': {
                    'text': await async_translate_text(embed.footer.text) if embed.footer and embed.footer.text else None,
                    'icon_url': embed.footer.icon_url if embed.footer else None
                },
                'image': embed.image.url if embed.image else None,
                'thumbnail': embed.thumbnail.url if embed.thumbnail else None,
                'fields': []
            }
            for field in embed.fields:
                embed_data['fields'].append({
                    'name': await async_translate_text(field.name) if field.name else "",
                    'value': await async_translate_text(field.value) if field.value else "",
                    'inline': field.inline
                })
            parts['embeds'].append(embed_data)
        else:
            if embed.image: parts['image_urls'].append(embed.image.url)
            elif embed.thumbnail: parts['image_urls'].append(embed.thumbnail.url)

    return parts, original_raw_content

def apply_output_style(parts, style):
    if style == 'auto': return parts 

    if style == 'flat':
        new_content_blocks = []
        if parts['content']: new_content_blocks.append(parts['content'])
        for em in parts['embeds']:
            if em['author']['name']: new_content_blocks.append(f"**{em['author']['name']}**")
            if em['title']: new_content_blocks.append(f"**{em['title']}**")
            if em['description']: new_content_blocks.append(em['description'])
            for field in em['fields']: new_content_blocks.append(f"**{field['name']}**: {field['value']}")
            if em['footer']['text']: new_content_blocks.append(f"_{em['footer']['text']}_")
            if em['image']: parts['image_urls'].append(em['image'])
            if em['thumbnail']: parts['image_urls'].append(em['thumbnail'])
        parts['embeds'] = [] 
        parts['content'] = "\n\n".join(new_content_blocks).strip() 
        return parts

    if style == 'embed':
        if not parts['embeds'] and (parts['content'] or parts['image_urls']):
            new_embed = {
                'title': "", 'description': parts['content'], 'color': 0x2b2d31, 'url': None, 'timestamp': None,
                'author': {'name': None, 'icon_url': None}, 'footer': {'text': None, 'icon_url': None},
                'image': None, 'thumbnail': None, 'fields': []
            }
            if parts['image_urls']:
                new_embed['image'] = parts['image_urls'][0]
                parts['image_urls'] = parts['image_urls'][1:]
            parts['embeds'].append(new_embed)
            parts['content'] = "" 
        return parts
    return parts

def rebuild_embeds(embed_data_list):
    embeds = []
    for ed in embed_data_list:
        embed = discord.Embed(
            title=ed['title'], description=ed['description'], 
            color=ed['color'], url=ed['url'], timestamp=ed['timestamp']
        )
        if ed['author']['name']: embed.set_author(name=ed['author']['name'], icon_url=ed['author']['icon_url'])
        if ed['footer']['text']: embed.set_footer(text=ed['footer']['text'], icon_url=ed['footer']['icon_url'])
        if ed['image']: embed.set_image(url=ed['image'])
        if ed['thumbnail']: embed.set_thumbnail(url=ed['thumbnail'])
        for f in ed['fields']: embed.add_field(name=f['name'], value=f['value'], inline=f['inline'])
        embeds.append(embed)
    return embeds

async def get_webhook(channel):
    if channel.id in webhook_cache: return webhook_cache[channel.id]
    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.token: 
                webhook_cache[channel.id] = wh
                return wh
        new_wh = await channel.create_webhook(name="Translation Hook")
        webhook_cache[channel.id] = new_wh
        return new_wh
    except: return None

async def send_translated_content(webhook, parts, display_name, avatar_url):
    send_kwargs = {'username': display_name, 'avatar_url': avatar_url, 'wait': True}
    final_content = parts['content']
    if parts['image_urls']:
        if final_content: final_content += "\n"
        final_content += "\n".join(parts['image_urls'])
    embeds_obj = rebuild_embeds(parts['embeds'])
    if final_content or embeds_obj:
        try: await webhook.send(content=final_content, embeds=embeds_obj, **send_kwargs)
        except Exception as e: print(f"❌ 发送失败: {e}")

# ==================== 事件处理 ====================

@bot.event
async def on_ready():
    print(f'🚀 {bot.user} 已上线！')
    load_config() 
    try: await bot.tree.sync()
    except: pass

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    if not isinstance(message.channel, discord.TextChannel): return
    if message.content and message.content.startswith('/'): return # 忽略命令

    # 1. 敌我识别：防止死循环
    if message.webhook_id:
        current_wh = await get_webhook(message.channel)
        if current_wh and message.webhook_id == current_wh.id:
            return

    cid = str(message.channel.id)
    uid = str(message.author.id)
    name = message.author.display_name 
    
    # 获取配置
    channel_mappings = global_config["bot_mappings"].get(cid, {})
    target_config = channel_mappings.get(uid) or channel_mappings.get(name)
    channel_mode = global_config["channel_modes"].get(cid, 'off')
    output_style = global_config["output_styles"].get(cid, 'auto')
    processing_scope = global_config["processing_scopes"].get(cid, 'translate_only') # 默认为“仅翻译”

    # 全局开关检查
    if not target_config and channel_mode == 'off':
        return

    # 2. 处理内容
    try:
        parts, original_raw_content = await process_message_content(message)
    except: return
    
    should_send = False
    
    # 逻辑 A: 定向监听 (必须发送)
    if target_config:
        should_send = True
        parts = apply_output_style(parts, output_style)
        
    # 逻辑 B: 全局模式
    else:
        # 判断内容是否变化
        original_clean = clean_text(original_raw_content).strip()
        trans_clean = (parts['content'] or "").strip()
        
        has_text_change = (original_clean != trans_clean)
        has_media = bool(message.embeds or message.attachments)
        
        # --- 核心逻辑分支 ---
        
        if processing_scope == 'all_messages':
            # 【强制处理模式】：只要有内容就发送 (包括中文)，以便统一格式
            if original_clean or has_media:
                should_send = True
        
        else: # processing_scope == 'translate_only' (默认)
            # 【仅翻译模式】：只有内容变了(是英文)，或者有附件需要搬运时才发
            if has_text_change:
                should_send = True
            elif has_media and not has_text_change:
                # 如果只是有图但文字没变(中文)，通常不需要重发，除非是 style=embed 且原图不是 embed...
                # 为了防刷屏，保守起见：仅翻译模式下，不翻译中文图片消息
                should_send = False

        if should_send:
            parts = apply_output_style(parts, output_style)

    if not should_send:
        return

    log(f"⚡ 转发消息: {message.author.display_name}")
    
    webhook = await get_webhook(message.channel)
    if webhook:
        if target_config:
            s_name, s_avatar = target_config['name'], target_config['avatar']
            try: await message.delete()
            except: pass
        else:
            s_name, s_avatar = message.author.display_name, (message.author.avatar.url if message.author.avatar else None)
            if channel_mode == 'replace':
                try: await message.delete()
                except: pass

        await send_translated_content(webhook, parts, s_name, s_avatar)
    else:
        # 无 Webhook 降级
        if target_config or channel_mode == 'replace':
            try: await message.delete()
            except: pass
        # ... (降级发送逻辑省略)

# ==================== Slash 命令 ====================

@bot.tree.command(name='set_scope', description='设置处理范围：仅翻译英文 或 强制处理所有消息(包括中文)')
@discord.app_commands.choices(scope=[
    discord.app_commands.Choice(name="Translate Only (默认: 仅翻译英文，中文忽略)", value="translate_only"),
    discord.app_commands.Choice(name="All Messages (强制: 所有消息都处理，中文也会被格式化)", value="all_messages")
])
async def set_scope(interaction: discord.Interaction, scope: discord.app_commands.Choice[str]):
    """设置本频道的处理范围"""
    cid = str(interaction.channel.id)
    global_config["processing_scopes"][cid] = scope.value
    save_config()
    
    desc = "现在机器人会**忽略中文**，只翻译英文。" if scope.value == "translate_only" else "现在机器人会**接管所有消息**，中文也会被强制应用格式 (如 Embed)。"
    await interaction.response.send_message(f"⚙️ 范围已更新: **{scope.name}**\n{desc}", ephemeral=True)

@bot.tree.command(name='translation_status', description='查看状态')
async def translation_status(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 配置状态", color=0x3498db)
    all_cids = set(global_config["channel_modes"].keys()) | set(global_config["processing_scopes"].keys())
    
    for cid in all_cids:
        ch = bot.get_channel(int(cid))
        name = ch.mention if ch else cid
        mode = global_config["channel_modes"].get(cid, "Off")
        style = global_config["output_styles"].get(cid, "Auto")
        scope = global_config["processing_scopes"].get(cid, "Translate Only")
        embed.add_field(name=f"📺 {name}", value=f"Mode: {mode}\nStyle: {style}\nScope: {scope}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ... (其他 set_style, setup_bot, start_translate, context menu 等命令保持不变，直接复制即可) ...
# 为了篇幅，这里隐去了未变动的命令代码，请保留之前版本中的 setup_bot_translator 等命令。

@bot.tree.command(name='set_style', description='设置本频道翻译结果的输出格式')
@discord.app_commands.choices(style=[
    discord.app_commands.Choice(name="Auto (自动: 纯文对纯文，卡片对卡片)", value="auto"),
    discord.app_commands.Choice(name="Flat (扁平: 强制转为纯文本+大图)", value="flat"),
    discord.app_commands.Choice(name="Embed (卡片: 强制转为Embed卡片)", value="embed")
])
async def set_style(interaction: discord.Interaction, style: discord.app_commands.Choice[str]):
    cid = str(interaction.channel.id)
    global_config["output_styles"][cid] = style.value
    save_config()
    await interaction.response.send_message(f"🎨 本频道输出样式已设置为: **{style.name}**", ephemeral=True)

@bot.tree.command(name='setup_bot_translator', description='设定：输入ID 或 名字 来指定机器人，并使用自定义头像和名字发布')
async def setup_bot_translator(interaction: discord.Interaction, target: str, name: str, avatar: discord.Attachment):
    cid = str(interaction.channel.id)
    target_key = target.strip()
    if cid not in global_config["bot_mappings"]:
        global_config["bot_mappings"][cid] = {}
    global_config["bot_mappings"][cid][target_key] = {'name': name, 'avatar': avatar.url}
    save_config()
    await interaction.response.send_message(f"✅ 设定成功！监听目标: `{target_key}`", ephemeral=True)

@bot.tree.command(name='clear_bot_translator', description='清除当前频道对指定目标的翻译设定')
async def clear_bot_translator(interaction: discord.Interaction, target: str):
    cid = str(interaction.channel.id)
    target_key = target.strip()
    mappings = global_config["bot_mappings"].get(cid, {})
    if target_key in mappings:
        del global_config["bot_mappings"][cid][target_key]
        save_config()
        await interaction.response.send_message(f"🗑️ 已移除对 `{target_key}` 的设定。", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ 未找到关于 `{target_key}` 的设定。", ephemeral=True)

@bot.tree.command(name='start_translate', description='开启本频道全员自动翻译')
async def start_translate(interaction: discord.Interaction):
    cid = str(interaction.channel.id)
    global_config["channel_modes"][cid] = 'replace'
    save_config() 
    await interaction.response.send_message('✅ 已开启全频道自动翻译', ephemeral=True)

@bot.tree.command(name='off_mode', description='关闭本频道自动翻译')
async def off_mode(interaction: discord.Interaction):
    cid = str(interaction.channel.id)
    global_config["channel_modes"][cid] = 'off'
    save_config() 
    await interaction.response.send_message('🛑 全频道自动翻译已关闭', ephemeral=True)

@bot.tree.context_menu(name='翻译此消息')
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)
    try:
        parts, _ = await process_message_content(message)
        parts = apply_output_style(parts, 'embed') # 右键强制 Embed
        final_text = parts['content']
        if parts['image_urls']: final_text += "\n" + "\n".join(parts['image_urls'])
        embeds_obj = rebuild_embeds(parts['embeds'])
        if not final_text and not embeds_obj:
            await interaction.followup.send("⚠️ 消息为空", ephemeral=True)
            return
        await interaction.followup.send(content=final_text, embeds=embeds_obj, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 错误: {e}", ephemeral=True)

async def main():
    if not TOKEN:
        print('❌ 错误: 未设置 DISCORD_TOKEN')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
