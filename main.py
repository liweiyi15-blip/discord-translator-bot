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
    """同步翻译核心逻辑"""
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

async def extract_and_translate_parts(message):
    """
    提取并翻译消息内容
    修改：智能识别纯图片Embed，将其降级为普通图片链接，避免出现Embed边框
    """
    parts = {
        'content': message.content or "", 
        'embeds': [], 
        'attachment_urls': [] 
    }

    # 1. 翻译正文
    if parts['content']:
        parts['content'] = await async_translate_text(parts['content'])
    
    # 2. 提取原生附件
    if message.attachments:
        for attachment in message.attachments:
            parts['attachment_urls'].append(attachment.url)

    # 3. 处理 Embeds
    for embed in message.embeds:
        # 核心修改：检查这个 Embed 是否只是一个“图片容器”
        # 如果 Embed 没有标题、描述、字段，且有图片，则视为纯图片
        has_text_content = bool(embed.title or embed.description or embed.fields or (embed.footer and embed.footer.text) or (embed.author and embed.author.name))
        
        if not has_text_content and embed.image:
            # 这是一个纯图片 Embed，提取图片 URL，不要作为 Embed 发送
            if embed.image.url not in parts['attachment_urls']:
                parts['attachment_urls'].append(embed.image.url)
            # 跳过后续 Embed 构建
            continue

        # 如果有文字内容，或者是真正的信息卡片，则正常处理
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
    """
    重建 Embed 对象
    """
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

async def send_translated_content(webhook, parts, author, mode, original_message):
    send_kwargs = {
        'username': author.display_name,
        'avatar_url': author.avatar.url if author.avatar else None,
        'wait': True
    }
    
    final_content = parts['content']
    
    # 拼接图片 URL 到正文
    if parts['attachment_urls']:
        if final_content:
            final_content += "\n" 
        final_content += "\n".join(parts['attachment_urls'])

    embeds = rebuild_embeds(parts['embeds'])

    if final_content or embeds:
        try:
            await webhook.send(content=final_content, embeds=embeds, **send_kwargs)
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

    snippet = message.content[:30].replace('\n', ' ') + '...' if message.content else '[Embed/图片]'
    log(f"🔎 收到 [{message.channel.name}] {message.author.name}: {snippet}")

    try:
        parts = await extract_and_translate_parts(message)
    except Exception as e:
        print(f"❌ 提取失败: {e}")
        return
    
    content_changed = parts['content'] != (message.content or "")
    
    should_send = False
    if content_changed:
        should_send = True
    elif parts['embeds']:
        orig_embed = message.embeds[0] if message.embeds else None
        trans_embed = parts['embeds'][0]
        if orig_embed:
            if (trans_embed['title'] != (orig_embed.title or "")) or \
               (trans_embed['description'] != (orig_embed.description or "")):
                should_send = True
        else:
            should_send = True
    elif parts['attachment_urls']:
        if current_mode == 'replace':
            should_send = True

    if not should_send:
        log(f"⏭️ 内容未变或无需翻译，跳过")
        await bot.process_commands(message)
        return

    log(f"⚡ 检测到需要翻译，正在处理...")

    webhook = await get_webhook(message.channel)
    
    try:
        if webhook:
            if current_mode == 'replace':
                try:
                    await message.delete()
                except: pass 
            
            await send_translated_content(webhook, parts, message.author, current_mode, message)
            log(f"✅ 转发成功 (Webhook)")
        else:
            if current_mode == 'replace':
                try: await message.delete()
                except: pass
            
            embeds = rebuild_embeds(parts['embeds'])
            final_text = f"**[{message.author.display_name}]**: {parts['content']}"
            if parts['attachment_urls']:
                final_text += "\n" + "\n".join(parts['attachment_urls'])

            await message.channel.send(content=final_text, embeds=embeds)
            log(f"✅ 转发成功 (普通消息)")
            
    except discord.Forbidden:
        print(f"❌ 权限不足 (Missing Permissions)")
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
        parts = await extract_and_translate_parts(message)
        
        embeds_to_send = rebuild_embeds(parts['embeds'])
        content_to_send = parts['content']

        # 处理图片链接：将其拼接到正文中，这样 Discord 会自动显示大图而不是 Embed 框
        if parts['attachment_urls']:
            if content_to_send:
                content_to_send += "\n"
            content_to_send += "\n".join(parts['attachment_urls'])
        
        if not content_to_send and not embeds_to_send:
            await interaction.followup.send("⚠️ 消息为空或无需翻译", ephemeral=True)
            return

        await interaction.followup.send(content=content_to_send, embeds=embeds_to_send, ephemeral=True)
        
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
