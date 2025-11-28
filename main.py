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
    "output_styles": {}       
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
    """
    清洗文本：去除链接、特定emoji、残缺Markdown
    """
    if not text: return ""
    
    # 1. 去除 URL
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    
    # 2. 【修复】去除残缺的 Markdown 链接 []() 或 [] 或 [](
    # 针对你遇到的 [] ( 这种残留
    text = text.replace('[](', '') 
    text = re.sub(r'\[\s*\]\(\s*\)', '', text) # 去除 [ ]()
    text = re.sub(r'\[.*?\]\(\s*\)', '', text) # 去除 [text]()
    text = re.sub(r'\[\s*\]', '', text)       # 去除 []
    
    # 3. 去除特定 Emoji
    text = text.replace('📷', '')
    
    return text.strip()

def translate_text_sync(text):
    # 先清洗
    text = clean_text(text)
    
    if not text: return ""
    if len(text.split()) < 1 and not len(text) > 10: 
        return text
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
        
        # 行距修复
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
            # 链接预览等非 Rich Embed，只提取图片
            if embed.image:
                parts['image_urls'].append(embed.image.url)
            elif embed.thumbnail:
                parts['image_urls'].append(embed.thumbnail.url)

    return parts

def apply_output_style(parts, style):
    if style == 'auto':
        return parts 

    if style == 'flat':
        # 强制扁平化
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
        # 强制卡片化
        # 仅当没有 Embed 时创建，或者将现有内容合并
        if not parts['embeds'] and (parts['content'] or parts['image_urls']):
            new_embed = {
                'title': "",
                'description': parts['content'],
                'color': 0x2b2d31, 
                'url': None,
                'timestamp': None,
                'author': {'name': None, 'icon_url': None},
                'footer': {'text': None, 'icon_url': None},
                'image': None,
                'thumbnail': None,
                'fields': []
            }
            
            # 【修复】将第一张图设为 Embed 主图，避免裸链接出现在文字中
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
        if ed['author']['name']:
            embed.set_author(name=ed['author']['name'], icon_url=ed['author']['icon_url'])
        if ed['footer']['text']:
            embed.set_footer(text=ed['footer']['text'], icon_url=ed['footer']['icon_url'])
        if ed['image']:
            embed.set_image(url=ed['image'])
        if ed['thumbnail']:
            embed.set_thumbnail(url=ed['thumbnail'])
        for f in ed['fields']:
            embed.add_field(name=f['name'], value=f['value'], inline=f['inline'])
        embeds.append(embed)
    return embeds

async def get_webhook(channel):
    if channel.id in webhook_cache:
        return webhook_cache[channel.id]
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
    
    # 拼接剩余的图片链接
    if parts['image_urls']:
        if final_content: final_content += "\n"
        final_content += "\n".join(parts['image_urls'])
    
    embeds_obj = rebuild_embeds(parts['embeds'])
    
    if final_content or embeds_obj:
        try:
            await webhook.send(content=final_content, embeds=embeds_obj, **send_kwargs)
        except Exception as e:
            print(f"❌ 发送失败: {e}")

# ==================== 事件处理 ====================

@bot.event
async def on_ready():
    print(f'🚀 {bot.user} 已上线！')
    load_config() 
    try:
        await bot.tree.sync()
        print(f'✅ 命令已同步')
    except Exception as e:
        print(f'❌ 命令同步失败: {e}')

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    if not isinstance(message.channel, discord.TextChannel): return

    # 🛑 死循环防御
    if message.webhook_id:
        current_wh = await get_webhook(message.channel)
        if current_wh and message.webhook_id == current_wh.id:
            return

    cid = str(message.channel.id)
    uid = str(message.author.id)
    name = message.author.display_name 
    
    channel_mappings = global_config["bot_mappings"].get(cid, {})
    target_config = channel_mappings.get(uid) or channel_mappings.get(name)
    
    channel_mode = global_config["channel_modes"].get(cid, 'off')
    output_style = global_config["output_styles"].get(cid, 'auto')

    if not target_config and channel_mode == 'off':
        await bot.process_commands(message)
        return

    try:
        parts = await process_message_content(message)
        parts = apply_output_style(parts, output_style)
    except Exception as e:
        print(f"❌ 处理错误: {e}")
        return
    
    should_send = False
    
    if target_config:
        should_send = True
    else:
        if parts['content'] or parts['embeds'] or parts['image_urls']:
             raw_clean = clean_text(message.content or "")
             trans_clean = (parts['content'] or "").strip()
             if not message.embeds and not message.attachments and raw_clean == trans_clean:
                 should_send = False
             else:
                 should_send = True

    if not should_send:
        await bot.process_commands(message)
        return

    match_type = f"ID: {uid}" if channel_mappings.get(uid) else f"Name: {name}" if target_config else "Global Mode"
    log(f"⚡ 转发消息: [{message.author.display_name}] (Matched by {match_type})")
    
    webhook = await get_webhook(message.channel)
    if webhook:
        if target_config:
            send_name = target_config['name']
            send_avatar = target_config['avatar']
            try: await message.delete()
            except: pass
        else:
            send_name = message.author.display_name
            send_avatar = message.author.avatar.url if message.author.avatar else None
            if channel_mode == 'replace':
                try: await message.delete()
                except: pass

        await send_translated_content(webhook, parts, send_name, send_avatar)
    else:
        if target_config or channel_mode == 'replace':
            try: await message.delete()
            except: pass
            
        name_prefix = target_config['name'] if target_config else message.author.display_name
        final_text = f"**[{name_prefix}]**: {parts['content']}"
        if parts['image_urls']: final_text += "\n" + "\n".join(parts['image_urls'])
        embeds_obj = rebuild_embeds(parts['embeds'])
        await message.channel.send(content=final_text, embeds=embeds_obj)

    await bot.process_commands(message)

# ==================== Slash 命令 ====================

@bot.tree.command(name='translation_status', description='查看当前所有频道的翻译设置状态')
async def translation_status(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 自动翻译配置状态", color=0x3498db)
    all_cids = set(global_config["channel_modes"].keys()) | \
               set(global_config["bot_mappings"].keys()) | \
               set(global_config["output_styles"].keys())
    
    if not all_cids:
        await interaction.response.send_message("💤 当前没有任何频道开启翻译或设置规则。", ephemeral=True)
        return

    for cid in all_cids:
        channel = bot.get_channel(int(cid))
        channel_name = channel.mention if channel else f"Unknown ({cid})"
        mode = global_config["channel_modes"].get(cid, "Off")
        style = global_config["output_styles"].get(cid, "Auto")
        mappings = global_config["bot_mappings"].get(cid, {})
        
        status_text = f"**模式**: {mode}\n**样式**: {style}\n"
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

# ----------------- 右键菜单 (Context Menu) -----------------
@bot.tree.context_menu(name='翻译此消息')
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)
    try:
        parts = await process_message_content(message)
        
        # 【右键翻译逻辑更新】
        # 强制使用 Embed 样式，确保“图片”被包含在 Embed 里，而不是裸露的 URL
        # 同时清洗掉 []() 等残留
        parts = apply_output_style(parts, 'embed')
        
        # 提取结果
        final_text = parts['content']
        # 理论上 Embed 模式下 image_urls 会被移入 embed.image，这里检查是否还有剩余的
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

async def main():
    if not TOKEN:
        print('❌ 错误: 未设置 DISCORD_TOKEN')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
