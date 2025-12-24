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
    "channel_modes": {},      
    "bot_mappings": {},       
    "output_styles": {},
    "processing_scopes": {}
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
    # 1. 【优先】去除 Markdown 格式的链接 [text](url) -> 保留 text
    text = re.sub(r'\[([^\]]*)\]\(https?://\S+\)', r'\1', text) 
    # 2. 去除所有裸 URL 
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    # 3. 强力清理残留的括号和方括号组合
    text = text.replace('[](', '').replace('[]', '')
    text = re.sub(r'\[\s*\]\(\s*\)', '', text) 
    text = re.sub(r'\[\s*\]', '', text)        
    # 4. 去除特定 Emoji
    text = text.replace('📷', '')
    return text.strip()

def translate_text_sync(text):
    text = clean_text(text)
    if not text: return ""
    
    # ------------------ 修改区域 ------------------
    # 修改要求：英文少于15个字母的内容不要翻译
    if len(text) < 15: return text
    # ---------------------------------------------

    if re.search(r'[\u4e00-\u9fff]', text): return text
    
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
        if detection['language'].startswith('zh'): return text
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
    original_raw_content = message.content or ""

    if parts['content']:
        parts['content'] = await async_translate_text(parts['content'])

    if message.attachments:
        print(f"[IMG_DEBUG] 📥 发现 {len(message.attachments)} 个附件")
        for attachment in message.attachments:
            parts['image_urls'].append(attachment.url)

    for i, embed in enumerate(message.embeds):
        should_rebuild_embed = False
        if embed.type in ['rich', 'article']:
            should_rebuild_embed = True
        
        # 日志
        if embed.image: print(f"[IMG_DEBUG] 📥 Embed[{i}] Image: {embed.image.url}")
        if embed.thumbnail: print(f"[IMG_DEBUG] 📥 Embed[{i}] Thumbnail: {embed.thumbnail.url}")

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
            # 链接预览，提取图片
            if embed.image:
                parts['image_urls'].append(embed.image.url)
            elif embed.thumbnail:
                parts['image_urls'].append(embed.thumbnail.url)

    print(f"[IMG_DEBUG] ✅ 提取完成. 当前图片队列数: {len(parts['image_urls'])}")
    return parts, original_raw_content

def apply_output_style(parts, style):
    if style == 'auto': return parts 

    if style == 'flat':
        new_content_blocks = []
        if parts['content']:
            new_content_blocks.append(parts['content'])
        for em in parts['embeds']:
            if em['author']['name']: new_content_blocks.append(f"**{em['author']['name']}**")
            if em['title']: new_content_blocks.append(f"**{em['title']}**")
            if em['description']: new_content_blocks.append(em['description'])
            for field in em['fields']:
                new_content_blocks.append(f"**{field['name']}**: {field['value']}")
            if em['footer']['text']: new_content_blocks.append(f"_{em['footer']['text']}_")
            if em['image']: parts['image_urls'].append(em['image'])
            if em['thumbnail']: parts['image_urls'].append(em['thumbnail'])
        parts['embeds'] = [] 
        parts['content'] = "\n\n".join(new_content_blocks).strip() 
        return parts

    if style == 'embed':
        if not parts['embeds'] and (parts['content'] or parts['image_urls']):
            new_embed = {
                'title': "", 'description': parts['content'], 'color': 0x2b2d31, 
                'url': None, 'timestamp': None,
                'author': {'name': None, 'icon_url': None}, 'footer': {'text': None, 'icon_url': None},
                'image': None, 'thumbnail': None, 'fields': []
            }
            # 设置主图
            if parts['image_urls']:
                new_embed['image'] = parts['image_urls'][0]
                print(f"[IMG_DEBUG] 🖼️ 设置 Embed 主图: {parts['image_urls'][0]}")
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
    except Exception as e:
        print(f"❌ Webhook 获取失败: {e}")
        return None

async def send_translated_content(webhook, parts, display_name, avatar_url):
    send_kwargs = {'username': display_name, 'avatar_url': avatar_url, 'wait': True}
    final_content = parts['content']
    if parts['image_urls']:
        if final_content: final_content += "\n"
        final_content += "\n".join(parts['image_urls'])
    embeds_obj = rebuild_embeds(parts['embeds'])
    
    if embeds_obj and embeds_obj[0].image:
        print(f"[IMG_DEBUG] 🚀 最终 Embed 包含 Image: {embeds_obj[0].image.url}")
    
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
    if message.content and message.content.startswith('/'):
        await bot.process_commands(message)
        return
    if message.webhook_id:
        current_wh = await get_webhook(message.channel)
        if current_wh and message.webhook_id == current_wh.id: return

    cid = str(message.channel.id)
    uid = str(message.author.id)
    name = message.author.display_name 
    
    channel_mappings = global_config["bot_mappings"].get(cid, {})
    target_config = channel_mappings.get(uid) or channel_mappings.get(name)
    channel_mode = global_config["channel_modes"].get(cid, 'off')
    output_style = global_config["output_styles"].get(cid, 'auto')
    processing_scope = global_config["processing_scopes"].get(cid, 'translate_only')

    if not target_config and channel_mode == 'off':
        await bot.process_commands(message)
        return

    # 【修复图片丢失核心逻辑】
    # 如果消息没有显式附件，也没有Embeds，但内容不为空（可能是链接）
    # 我们等待 2 秒，让 Discord 有时间生成预览图
    if not message.attachments and not message.embeds and message.content:
        print(f"[DELAY] ⏳ 等待链接预览加载... (Message ID: {message.id})")
        await asyncio.sleep(2.0) 
        try:
            # 重新获取消息最新状态
            message = await message.channel.fetch_message(message.id)
            print(f"[DELAY] 🔄 重新获取消息成功。当前 Embeds 数: {len(message.embeds)}")
        except Exception as e:
            print(f"[DELAY] ⚠️ 重新获取消息失败 (可能已删除): {e}")
            return # 如果原消息没了，就停止处理

    try:
        parts, original_raw_content = await process_message_content(message)
    except: return
    
    should_send = False
    
    if target_config:
        should_send = True
        parts = apply_output_style(parts, output_style)
    else:
        original_clean = clean_text(original_raw_content).strip()
        trans_clean = (parts['content'] or "").strip()
        has_text_change = (original_clean != trans_clean)
        has_media = bool(message.embeds or message.attachments)
        
        if processing_scope == 'all_messages':
            if original_clean or has_media:
                should_send = True
        else: 
            if has_text_change:
                should_send = True
            elif has_media and not has_text_change:
                should_send = False

        if should_send:
            parts = apply_output_style(parts, output_style)

    if not should_send:
        await bot.process_commands(message)
        return

    log(f"⚡ 转发消息: [{message.author.display_name}]")
    
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
        if target_config or channel_mode == 'replace':
            try: await message.delete()
            except: pass
        # 降级发送略...

# ==================== Slash 命令 ====================

@bot.tree.command(name='set_scope', description='设置处理范围：仅翻译英文 或 强制处理所有消息(包括中文)')
@discord.app_commands.choices(scope=[
    discord.app_commands.Choice(name="Translate Only (默认: 仅翻译英文，中文忽略)", value="translate_only"),
    discord.app_commands.Choice(name="All Messages (强制: 所有消息都处理，中文也会被格式化)", value="all_messages")
])
async def set_scope(interaction: discord.Interaction, scope: discord.app_commands.Choice[str]):
    cid = str(interaction.channel.id)
    global_config["processing_scopes"][cid] = scope.value
    save_config()
    desc = "现在机器人会**忽略中文**，只翻译英文。" if scope.value == "translate_only" else "现在机器人会**接管所有消息**，中文也会被强制应用格式 (如 Embed)。"
    await interaction.response.send_message(f"⚙️ 范围已更新: **{scope.name}**\n{desc}", ephemeral=True)

@bot.tree.command(name='translation_status', description='查看当前所有频道的翻译设置状态')
async def translation_status(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 自动翻译配置状态", color=0x3498db)
    all_cids = set(global_config["channel_modes"].keys()) | \
               set(global_config["bot_mappings"].keys()) | \
               set(global_config["output_styles"].keys()) | \
               set(global_config["processing_scopes"].keys())
    
    if not all_cids:
        await interaction.response.send_message("💤 当前没有任何频道开启翻译或设置规则。", ephemeral=True)
        return

    for cid in all_cids:
        channel = bot.get_channel(int(cid))
        channel_name = channel.mention if channel else f"Unknown ({cid})"
        mode = global_config["channel_modes"].get(cid, "Off")
        style = global_config["output_styles"].get(cid, "Auto")
        scope = global_config["processing_scopes"].get(cid, "Translate Only")
        mappings = global_config["bot_mappings"].get(cid, {})
        
        status_text = f"**模式**: {mode}\n**样式**: {style}\n**范围**: {scope}\n"
        if mappings:
            targets = []
            for target, config in mappings.items():
                targets.append(f"• `{target}` → {config['name']}")
            status_text += "**监听**: \n" + "\n".join(targets)
        else:
            status_text += "**监听**: 无"
        embed.add_field(name=f"📺 {channel_name}", value=status_text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
        parts = apply_output_style(parts, 'embed')
        final_text = parts['content']
        if parts['image_urls']: 
            if final_text: final_text += "\n"
            final_text += "\n".join(parts['image_urls'])
        embeds_obj = rebuild_embeds(parts['embeds'])
        if not final_text and not embeds_obj:
            await interaction.followup.send("⚠️ 消息为空", ephemeral=True)
            return
        await interaction.followup.send(content=final_text, embeds=embeds_obj, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 错误: {e}", ephemeral=True)

# ==================== 新增：提取文字功能 (修复 iOS 无法复制问题) ====================
@bot.tree.context_menu(name='获取纯文本')
async def get_raw_text(interaction: discord.Interaction, message: discord.Message):
    """
    iOS 专用辅助功能：
    长按 Embed 消息 -> Apps -> 获取纯文本
    这会发送一条只有你自己可见的(Ephemeral)纯文本消息，方便复制。
    """
    content_list = []
    
    # 1. 提取普通消息内容
    if message.content:
        content_list.append(message.content)
    
    # 2. 提取 Embeds 中的所有文本 (标题, 描述, 字段)
    for embed in message.embeds:
        if embed.title:
            content_list.append(f"【标题】 {embed.title}")
        if embed.description:
            content_list.append(embed.description)
        for field in embed.fields:
            content_list.append(f"【{field.name}】: {field.value}")
        if embed.footer and embed.footer.text:
            content_list.append(f"_{embed.footer.text}_")

    final_text = "\n\n".join(content_list)
    
    if not final_text:
        await interaction.response.send_message("⚠️ 这条消息没有任何可复制的文本内容。", ephemeral=True)
    else:
        # 使用代码块包裹，防止格式混乱，且方便全选
        # ephemeral=True 确保只有你自己能看到
        await interaction.response.send_message(f"{final_text}", ephemeral=True)

async def main():
    if not TOKEN:
        print('❌ 错误: 未设置 DISCORD_TOKEN')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())

