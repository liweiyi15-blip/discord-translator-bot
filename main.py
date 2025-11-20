import discord
from discord.ext import commands
import asyncio
import os
import json
from google.cloud import translate_v2 as translate
from google.oauth2 import service_account
import re
import functools

# ==================== 配置 ====================
TOKEN = os.getenv('DISCORD_TOKEN')
MIN_WORDS = 5

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ==================== Google SDK 初始化 ====================
json_key = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
if json_key:
    try:
        credentials = service_account.Credentials.from_service_account_info(json.loads(json_key))
        client = translate.Client(credentials=credentials)
        print('Google Translate SDK 初始化成功')
    except Exception as e:
        print(f'SDK 初始化失败: {e}')
        client = None
else:
    print('JSON Key 未设置')
    client = None

# ==================== 状态存储 ====================
channel_modes = {}
webhook_cache = {}  # Webhook 缓存，防止重复创建

# ==================== 核心功能函数 ====================

def translate_text_sync(text):
    """
    同步翻译核心逻辑（运行在线程池中）。
    包含 @提及 保护和 Google API 调用。
    """
    # 1. 基础过滤：字数太少或已包含中文则不翻译
    if len(text.split()) < MIN_WORDS or re.search(r'[\u4e00-\u9fff]', text):
        return text
    
    # 2. 保护 @everyone, @here 和 <@用户ID>
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

    # 3. 调用 Google 翻译
    try:
        if not client:
            return text
            
        detection = client.detect_language(text)
        lang = detection['language']
        if lang.startswith('zh'): 
            return text
            
        # format_='text' 自动处理 HTML 转义
        result = client.translate(
            text, 
            source_language='en', 
            target_language='zh-CN', 
            format_='text'
        )['translatedText']
        
    except Exception as e:
        print(f'翻译异常: {e}')
        return text

    # 4. 还原 @提及
    for placeholder, original in mention_placeholders.items():
        result = result.replace(placeholder, original)

    return result

async def async_translate_text(text):
    """异步包装器：防止 Google API 阻塞 Bot 主循环"""
    if not text:
        return ""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(translate_text_sync, text))

async def extract_and_translate_parts(message):
    """提取并翻译消息内容和 Embed"""
    parts = {'content': message.content or "", 'embeds': []}
    
    if parts['content']:
        parts['content'] = await async_translate_text(parts['content'])
    
    for embed in message.embeds:
        embed_data = {
            'title': await async_translate_text(embed.title) if embed.title else "",
            'description': await async_translate_text(embed.description) if embed.description else "",
            'color': embed.color.value if embed.color else None,
            # 作者名不翻译，保留原汁原味
            'author': {
                'name': embed.author.name if embed.author else None,
                'icon_url': embed.author.icon_url if embed.author else None
            },
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

async def get_webhook(channel):
    """获取或创建 Webhook（带缓存机制）"""
    if channel.id in webhook_cache:
        return webhook_cache[channel.id]

    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.token: # 找到本 Bot 可控的 webhook
                webhook_cache[channel.id] = wh
                return wh
        
        # 创建新的
        new_wh = await channel.create_webhook(name="Translation Hook")
        webhook_cache[channel.id] = new_wh
        return new_wh
    except Exception as e:
        print(f"Webhook 获取/创建失败: {e}")
        return None

async def send_translated_content(webhook, parts, author, mode, original_message):
    """使用 Webhook 发送，模仿原作者头像和昵称"""
    send_kwargs = {
        'username': author.display_name,
        'avatar_url': author.avatar.url if author.avatar else None,
        'wait': True
    }

    content = parts['content']
    embeds = []

    # 重建 Embed 对象
    if parts['embeds']:
        for ed in parts['embeds']:
            embed = discord.Embed(title=ed['title'], description=ed['description'], color=ed['color'])
            if ed['author']['name']:
                embed.set_author(name=ed['author']['name'], icon_url=ed['author']['icon_url'])
            for f in ed['fields']:
                embed.add_field(name=f['name'], value=f['value'], inline=f['inline'])
            embeds.append(embed)
        
        # 如果同时有正文和 Embed，把正文拼接到第一个 Embed 的描述中（你的原有逻辑）
        if content and embeds:
            desc = embeds[0].description or ""
            embeds[0].description = desc + ("\n\n" + content if desc else content)
            content = None 
    
    try:
        if embeds:
            await webhook.send(content=content, embeds=embeds, **send_kwargs)
        elif content:
            await webhook.send(content=content, **send_kwargs)
    except discord.NotFound:
        # Webhook 如果被手动删除，清除缓存
        if original_message.channel.id in webhook_cache:
            del webhook_cache[original_message.channel.id]

# ==================== 事件处理 ====================

@bot.event
async def on_ready():
    print(f'{bot.user} 已上线！')
    synced = await bot.tree.sync()
    print(f'同步了 {len(synced)} 个命令')

@bot.event
async def on_message(message):
    # 1. 忽略自己和 Webhook 消息（防止死循环）
    if message.author == bot.user or message.webhook_id:
        return

    # 2. 检查频道模式
    channel_id = message.channel.id
    mode = channel_modes.get(channel_id, 'replace') # 默认为 replace
    if mode == 'off':
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    # 3. 异步提取和翻译
    parts = await extract_and_translate_parts(message)
    
    # 4. 检查是否发生变化（无变化则不处理）
    content_changed = parts['content'] != (message.content or "")
    embed_changed = False
    if parts['embeds']:
        # 简单判断：如果翻译后的第一个 Embed 标题或描述变了
        orig_embed = message.embeds[0]
        trans_embed = parts['embeds'][0]
        if (trans_embed['title'] != (orig_embed.title or "")) or \
           (trans_embed['description'] != (orig_embed.description or "")):
            embed_changed = True

    if not content_changed and not embed_changed:
        await bot.process_commands(message)
        return

    # 5. 获取 Webhook 并发送
    webhook = await get_webhook(message.channel)
    
    try:
        if webhook:
            if mode == 'replace':
                await message.delete()
            await send_translated_content(webhook, parts, message.author, mode, message)
        else:
            # 如果无法创建 Webhook，回退到普通回复
            if mode == 'replace':
                await message.delete()
            await message.channel.send(f"**[{message.author.display_name}]**: {parts['content']}")
            
    except discord.Forbidden:
        print(f"在频道 {message.channel.name} 缺少权限 (Manage Webhooks / Manage Messages)")
    except Exception as e:
        print(f"处理消息异常: {e}")

    await bot.process_commands(message)

# ==================== Slash 命令 ====================

@bot.tree.command(name='reply_mode', description='在此频道设置回复翻译模式')
async def reply_mode(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'reply'
    await interaction.response.send_message('已设为回复模式（保留原消息，在下方回复翻译）', ephemeral=True)

@bot.tree.command(name='replace_mode', description='在此频道设置删除+代替模式')
async def replace_mode(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'replace'
    await interaction.response.send_message('已设为删除+代替模式（删除原消息，用同名头像发出翻译）', ephemeral=True)

@bot.tree.command(name='off_mode', description='关闭本频道自动翻译')
async def off_mode(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'off'
    await interaction.response.send_message('本频道自动翻译已关闭', ephemeral=True)

@bot.tree.context_menu(name='翻译此消息')
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    if message.author.bot:
        await interaction.response.send_message('机器人消息不翻译', ephemeral=True)
        return
    
    # 复用异步翻译逻辑
    parts = await extract_and_translate_parts(message)
    await interaction.response.send_message(f"翻译结果：\n{parts['content']}", ephemeral=True)

# ==================== 启动 ====================

async def main():
    if not TOKEN:
        print('错误: 未设置 DISCORD_TOKEN')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
