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
        
    # 这里的正则检测中文，如果已包含中文则直接返回
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
        
        # ========== 修复行距逻辑 ==========
        result = result.replace(' \n', '\n').replace('\n ', '\n')
        orig_double_newlines = text.count('\n\n')
        trans_double_newlines = result.count('\n\n')
        if trans_double_newlines > orig_double_newlines:
             result = re.sub(r'\n+', '\n', result)
        # ================================
        
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
    智能处理消息结构
    """
    parts = {
        'content': message.content or "", 
        'embeds': [],     
        'image_urls': []  
    }

    # 1. 翻译正文
    if parts['content']:
        parts['content'] = await async_translate_text(parts['content'])

    # 2. 处理附件
    if message.attachments:
        for attachment in message.attachments:
            parts['image_urls'].append(attachment.url)

    # 3. 处理 Embeds
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
    if parts['image_urls']:
        if final_content:
            final_content += "\n"
        final_content += "\n".join(parts['image_urls'])

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

    # 防死循环第二道防线：如果消息来自 webhook，且频道模式不是 off，
    # 且消息内容本身就是中文，下面的逻辑会检测到内容无变化从而停止。
    
    channel_id = message.channel.id
    current_mode = channel_modes.get(channel_id, 'off') 

    if current_mode == 'off':
        await bot.process_commands(message)
        return

    if not isinstance(message.channel, discord.TextChannel):
        return

    try:
        parts = await process_message_content(message)
    except Exception as e:
        print(f"❌ 处理失败: {e}")
        return
    
    # ========== 核心修复：死循环防御逻辑 ==========
    should_send = False
    
    # 1. 检查文字是否发生了变化 (去空格对比)
    original_text = (message.content or "").strip()
    translated_text = (parts['content'] or "").strip()
    if original_text != translated_text:
        should_send = True
        
    # 2. 检查 Embed 是否发生了变化
    if not should_send and message.embeds and parts['embeds']:
        # 简单比对第一个 Embed 的标题或描述
        orig_embed = message.embeds[0]
        trans_embed = parts['embeds'][0]
        
        orig_title = (orig_embed.title or "").strip()
        trans_title = (trans_embed['title'] or "").strip()
        
        orig_desc = (orig_embed.description or "").strip()
        trans_desc = (trans_embed['description'] or "").strip()
        
        if (orig_title != trans_title) or (orig_desc != trans_desc):
            should_send = True
            
    # 3. 检查是否有附件需要搬运 (仅在 Replace 模式下)
    # 如果原消息有附件，且模式是 Replace，因为我们会删除原消息，所以必须发送新消息(哪怕文字没变)
    # 但是！如果原消息已经是中文（文字没变），我们通常不想删它。
    # 这里做一个权衡：如果文字没变，且是 replace 模式，且有附件 -> 不删，不发（避免重复）
    # 只有当文字变了，才进行替换。
    # 修正：如果文字没变，但我们处于 replace 模式，我们应该什么都不做（保留原样），不要删除原消息。
    
    # 总结判断：只有当【内容确实被翻译了】才发送。
    if not should_send:
        # 如果内容没变，直接跳过，不要删除原消息，也不要发新消息
        # 这样就能完美解决中文消息无限重复的问题
        # log(f"⏭️ 内容未变 (可能是中文)，跳过")
        await bot.process_commands(message)
        return

    # ==========================================

    log(f"⚡ 检测到内容变化，执行翻译转发...")
    webhook = await get_webhook(message.channel)
    
    try:
        if webhook:
            if current_mode == 'replace':
                try: await message.delete()
                except: pass 
            
            await send_translated_content(webhook, parts, message.author, current_mode)
        else:
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
