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

# 核心修改：检测环境变量，决定配置文件存在哪
# 如果在 Railway 上配置了 Volume 挂载到 /data，我们就存在那里
# 否则（本地开发）存在当前目录
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
channel_modes = {}
webhook_cache = {}
bot_mappings = {} 

def load_config():
    """从持久化文件加载配置"""
    global bot_mappings
    
    # 确保目录存在（如果是 /data 这种挂载目录，通常已存在，但为了保险）
    if DATA_DIR != '.' and not os.path.exists(DATA_DIR):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            print(f"📂 创建数据目录: {DATA_DIR}")
        except Exception as e:
            print(f"❌ 无法创建数据目录: {e}")

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                bot_mappings = json.load(f)
            print(f"📂 已加载配置文件: {CONFIG_FILE} (包含 {len(bot_mappings)} 个频道设定)")
        except Exception as e:
            print(f"❌ 加载配置文件失败: {e}")
            bot_mappings = {}
    else:
        print(f"📂 未找到配置文件 {CONFIG_FILE}，将在首次保存时创建")

def save_config():
    """保存配置到持久化文件"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(bot_mappings, f, ensure_ascii=False, indent=4)
        print(f"💾 配置已保存至 {CONFIG_FILE}")
    except Exception as e:
        print(f"❌ 保存配置文件失败: {e}")

# ==================== 核心功能函数 ====================

def log(message):
    if DEBUG:
        print(message)

def translate_text_sync(text):
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
        if not has_text and embed.image:
            parts['image_urls'].append(embed.image.url)
            should_rebuild_embed = False

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

    cid = str(message.channel.id)
    uid = str(message.author.id)
    
    target_config = bot_mappings.get(cid, {}).get(uid)
    channel_mode = channel_modes.get(message.channel.id, 'off')

    if not target_config and channel_mode == 'off':
        await bot.process_commands(message)
        return

    try:
        parts = await process_message_content(message)
    except Exception as e:
        print(f"❌ 提取错误: {e}")
        return
    
    should_send = False
    if target_config:
        should_send = True
    else:
        original_text = (message.content or "").strip()
        translated_text = (parts['content'] or "").strip()
        if original_text != translated_text:
            should_send = True
        
        if not should_send and message.embeds and parts['embeds']:
            orig_embed = message.embeds[0]
            trans_embed = parts['embeds'][0]
            if ((orig_embed.title or "") != (trans_embed['title'] or "")) or \
               ((orig_embed.description or "") != (trans_embed['description'] or "")):
                should_send = True

    if not should_send:
        await bot.process_commands(message)
        return

    log(f"⚡ 处理消息: [{message.author.display_name}]")
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
        # 无 Webhook 降级
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

@bot.tree.command(name='setup_bot_translator', description='设定：自动翻译指定机器人，并使用自定义头像和名字发布')
async def setup_bot_translator(interaction: discord.Interaction, target: discord.User, name: str, avatar: discord.Attachment):
    cid = str(interaction.channel.id)
    uid = str(target.id)
    
    if cid not in bot_mappings:
        bot_mappings[cid] = {}
        
    bot_mappings[cid][uid] = {
        'name': name,
        'avatar': avatar.url 
    }
    save_config()
    await interaction.response.send_message(f"✅ 设定成功！(已保存到持久化存储)\n🎯 监听: {target.mention}\n🎭 新名: {name}", ephemeral=True)

@bot.tree.command(name='clear_bot_translator', description='清除当前频道对指定机器人的翻译设定')
async def clear_bot_translator(interaction: discord.Interaction, target: discord.User):
    cid = str(interaction.channel.id)
    uid = str(target.id)
    if cid in bot_mappings and uid in bot_mappings[cid]:
        del bot_mappings[cid][uid]
        save_config()
        await interaction.response.send_message(f"🗑️ 已移除对 {target.mention} 的特殊设定。", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ 未找到设定。", ephemeral=True)

@bot.tree.command(name='start_translate', description='开启本频道全员自动翻译 (不换皮)')
async def start_translate(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'replace'
    await interaction.response.send_message('✅ 已开启全频道自动翻译', ephemeral=True)

@bot.tree.command(name='off_mode', description='关闭本频道自动翻译')
async def off_mode(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'off'
    await interaction.response.send_message('🛑 全频道自动翻译已关闭', ephemeral=True)

@bot.tree.context_menu(name='翻译此消息')
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)
    try:
        parts = await process_message_content(message)
        final_text = parts['content']
        if parts['image_urls']: final_text += "\n" + "\n".join(parts['image_urls'])
        embeds_obj = rebuild_embeds(parts['embeds'])
        if not final_text and not embeds_obj:
            await interaction.followup.send("⚠️ 消息为空", ephemeral=True)
            return
        await interaction.followup.send(content=final_text, embeds=embeds_obj, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 错误: {e}", ephemeral=True)

# ==================== 启动 ====================

async def main():
    if not TOKEN:
        print('❌ 错误: 未设置 DISCORD_TOKEN')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
