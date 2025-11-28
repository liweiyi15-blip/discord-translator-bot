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
    修改：增加 attachment_urls 字段，用于处理纯图片附件，避免强制转为 Embed
    """
    parts = {
        'content': message.content or "", 
        'embeds': [], 
        'attachment_urls': [] # 新增：附件链接
    }

    # 1. 翻译正文
    if parts['content']:
        parts['content'] = await async_translate_text(parts['content'])
    
    # 2. 提取附件 (保留原图格式的关键)
    if message.attachments:
        for attachment in message.attachments:
            parts['attachment_urls'].append(attachment.url)

    # 3. 处理 Embeds (如果原消息本身就是 Embed)
    for embed in message.embeds:
        # 有些简单的 Link Preview 也是 Embed，如果不需要处理可以加判断，但这里保留以兼容复杂消息
        if embed.type == 'rich' or embed.type == 'article' or embed.type == 'image': 
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
    
    # 核心逻辑修改：如果原消息有附件，直接把链接拼接到正文后面
    # Discord 会自动把这个链接渲染成一张大图，且没有 Embed 的边框
    if parts['attachment_urls']:
        # 加换行符确保图片在文字下方
        if final_content:
            final_content += "\n" 
        final_content += "\n".join(parts['attachment_urls'])

    embeds = rebuild_embeds(parts['embeds'])

    # 只有当有内容或有 Embed 时才发送
    if final_content or embeds:
        try:
            # 这里的 content 包含了文字 + 图片链接
            # embeds 包含了原有的 Rich Embed (如果有的话)
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
    # 1. 忽略自己
    if message.author == bot.user:
        return

    # 2. 检查模式
    channel_id = message.channel.id
    current_mode = channel_modes.get(channel_id, 'off') # 默认关闭，需手动开启

    if current_mode == 'off':
        await bot.process_commands(message)
        return

    if not isinstance(message.channel, discord.TextChannel):
        return

    # 调试打印：收到消息
    snippet = message.content[:30].replace('\n', ' ') + '...' if message.content else '[Embed/图片]'
    log(f"🔎 收到 [{message.channel.name}] {message.author.name}: {snippet}")

    try:
        parts = await extract_and_translate_parts(message)
    except Exception as e:
        print(f"❌ 提取失败: {e}")
        return
    
    # 3. 变动检测 
    # 如果内容变了，或者原消息有 Embed 且被修改了，则视为需要发送
    content_changed = parts['content'] != (message.content or "")
    
    # 如果原消息只是纯图片（无文字），翻译后文字依然为空，但我们需要转发图片
    # 所以只要是 replace 模式，即使文字没变（空的），我们也得转发过去，否则原图会被删掉只剩个寂寞
    # 但为了防止文字没变时的死循环（如果是 Reply 模式），我们需要小心
    
    should_send = False
    if content_changed:
        should_send = True
    elif parts['embeds']:
        # 简单检查 Embed 是否变化
        orig_embed = message.embeds[0]
        trans_embed = parts['embeds'][0]
        if (trans_embed['title'] != (orig_embed.title or "")) or \
           (trans_embed['description'] != (orig_embed.description or "")):
            should_send = True
    elif parts['attachment_urls']:
        # 如果有附件，且是 Replace 模式，必须转发，因为原消息会被删
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
            # 降级处理 (没有 Webhook)
            if current_mode == 'replace':
                try: await message.delete()
                except: pass
            
            # 普通发送
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

        # 处理图片链接
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
