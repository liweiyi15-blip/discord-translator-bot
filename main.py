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
DEBUG = True  # 开启详细日志

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

# ==================== 状态存储 ====================
channel_modes = {}
webhook_cache = {}

# ==================== 核心功能函数 ====================

def log(message):
    if DEBUG:
        print(message)

def translate_text_sync(text):
    """同步翻译核心逻辑（含智能换行修正）"""
    if not text: return ""
    # 如果只有链接或数字，不翻译
    if len(text.split()) < 1 and not len(text) > 10: 
        return text
        
    if re.search(r'[\u4e00-\u9fff]', text):
        return text
    
    # 保护 @提及
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
            text, 
            source_language='en', 
            target_language='zh-CN', 
            format_='text'
        )['translatedText']
        
        # ========== 修复行距的核心逻辑 ==========
        # 1. 去除行尾多余的空格（谷歌翻译常在 \n 前加空格）
        result = result.replace(' \n', '\n').replace('\n ', '\n')
        
        # 2. 智能压缩：如果原文是紧凑列表（没有双换行），但译文出现了双换行，强制压回单换行
        # 这样可以解决 "行距多空一行" 的问题
        orig_double_newlines = text.count('\n\n')
        trans_double_newlines = result.count('\n\n')
        
        if trans_double_newlines > orig_double_newlines:
             # 将连续的换行符替换为单个换行符
             result = re.sub(r'\n+', '\n', result)
        # =====================================
        
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
    """
    智能处理消息结构：
    1. 翻译正文
    2. 如果有 Embed (Rich类型)，翻译并保留结构
    3. 如果有附件，提取链接拼接到正文
    """
    parts = {
        'content': message.content or "", 
        'embeds': [],     # 存放翻译后的 Embed 对象数据
        'image_urls': []  # 存放纯图片链接
    }

    # 1. 翻译正文
    if parts['content']:
        parts['content'] = await async_translate_text(parts['content'])

    # 2. 处理附件 (Attachments) -> 视为纯图片链接，不放入 Embed
    if message.attachments:
        for attachment in message.attachments:
            parts['image_urls'].append(attachment.url)

    # 3. 处理原有的 Embeds
    for embed in message.embeds:
        # 核心判断：只有 rich (富文本卡片) 或 article 类型的 Embed 我们才当做“卡片”处理
        # image/video/link 类型的 Embed 通常是 Discord 根据链接自动生成的预览，我们不需要手动重建它们
        
        should_rebuild_embed = False
        if embed.type in ['rich', 'article']:
            should_rebuild_embed = True
        
        # 特殊情况：如果一个 Embed 只有图片，没有标题没有描述，那它本质上就是个图片
        # 这种情况下我们把它降级为 URL，避免出现空框
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
    """重建 Embed 对象列表"""
    embeds = []
    for ed in embed_data_list:
        embed = discord.Embed(
            title=ed['title'], 
            description=ed['description'], 
            color=ed['color'],
            url=ed['url'],
            timestamp=ed['timestamp']
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
        print(f"🆕 为频道 {channel.name} 创建了新 Webhook")
        return new_wh
    except Exception as e:
        print(f"❌ Webhook 获取失败: {e}")
        return None

async def send_translated_content(webhook, parts, author, mode):
    send_kwargs = {
        'username': author.display_name,
        'avatar_url': author.avatar.url if author.avatar else None,
        'wait': True
    }
    
    final_content = parts['content']
    
    # 拼接纯图片链接到正文 (为了不带框)
    if parts['image_urls']:
        if final_content:
            final_content += "\n"
        final_content += "\n".join(parts['image_urls'])

    # 重建 Rich Embeds
    embeds_obj = rebuild_embeds(parts['embeds'])

    if final_content or embeds_obj:
        try:
            await webhook.send(content=final_content, embeds=embeds_obj, **send_kwargs)
        except Exception as e:
            print(f"❌ 发送具体内容失败: {e}")

# ==================== 事件处理 ====================

@bot.event
async def on_ready():
    print(f'🚀 {bot.user} 已上线！等待消息中...')
    try:
        synced = await bot.tree.sync()
        print(f'✅ 同步了 {len(synced)} 个命令')
    except Exception as e:
        print(f'❌ 同步命令失败: {e}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    channel_id = message.channel.id
    current_mode = channel_modes.get(channel_id, 'off') 

    if current_mode == 'off':
        await bot.process_commands(message)
        return

    if not isinstance(message.channel, discord.TextChannel):
        return

    # log(f"🔎 收到消息: {message.content[:20]}...") 

    try:
        parts = await process_message_content(message)
    except Exception as e:
        print(f"❌ 处理失败: {e}")
        return
    
    should_send = False
    if parts['content'] or parts['embeds'] or parts['image_urls']:
         should_send = True

    if not should_send:
        await bot.process_commands(message)
        return

    log(f"⚡ 正在发送翻译结果...")
    webhook = await get_webhook(message.channel)
    
    try:
        if webhook:
            if current_mode == 'replace':
                try: await message.delete()
                except: pass 
            
            await send_translated_content(webhook, parts, message.author, current_mode)
        else:
            # 无 Webhook 降级处理
            if current_mode == 'replace':
                try: await message.delete()
                except: pass
            
            final_text = f"**[{message.author.display_name}]**: {parts['content']}"
            if parts['image_urls']:
                final_text += "\n" + "\n".join(parts['image_urls'])
            
            embeds_obj = rebuild_embeds(parts['embeds'])
            await message.channel.send(content=final_text, embeds=embeds_obj)
            
    except discord.Forbidden:
        print(f"❌ 权限不足")
    except Exception as e:
        print(f"❌ 发送异常: {e}")

    await bot.process_commands(message)

# ==================== Slash 命令 ====================

@bot.tree.command(name='start_translate', description='开启本频道自动翻译 (默认替换模式)')
async def start_translate(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'replace'
    await interaction.response.send_message('✅ 已开启自动翻译 (模式: 删除原句+Webhook替换)', ephemeral=True)

@bot.tree.command(name='reply_mode', description='在此频道设置回复翻译模式')
async def reply_mode(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'reply'
    await interaction.response.send_message('✅ 已设为回复模式', ephemeral=True)

@bot.tree.command(name='replace_mode', description='在此频道设置删除+代替模式')
async def replace_mode(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'replace'
    await interaction.response.send_message('✅ 已设为替换模式', ephemeral=True)

@bot.tree.command(name='off_mode', description='关闭本频道自动翻译')
async def off_mode(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'off'
    await interaction.response.send_message('🛑 本频道自动翻译已关闭', ephemeral=True)

@bot.tree.context_menu(name='翻译此消息')
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    """
    右键菜单翻译
    """
    await interaction.response.defer(ephemeral=True)
    try:
        parts = await process_message_content(message)
        
        final_text = parts['content']
        if parts['image_urls']:
            final_text += "\n" + "\n".join(parts['image_urls'])
        
        embeds_obj = rebuild_embeds(parts['embeds'])
        
        if not final_text and not embeds_obj:
            await interaction.followup.send("⚠️ 消息为空或无需翻译", ephemeral=True)
            return

        await interaction.followup.send(content=final_text, embeds=embeds_obj, ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ 翻译失败: {e}", ephemeral=True)

# ==================== 启动 ====================

async def main():
    if not TOKEN:
        print('❌ 错误: 未设置 DISCORD_TOKEN')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
