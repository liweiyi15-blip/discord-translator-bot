import discord
from discord.ext import commands
import asyncio
import os
import json
from google.cloud import translate_v2 as translate
from google.oauth2 import service_account
import re

# 配置
TOKEN = os.getenv('DISCORD_TOKEN')
MIN_WORDS = 5

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 初始化 Google Translate SDK
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

# 每个频道的翻译模式
channel_modes = {}

def extract_and_translate_parts(message):
    parts = {'content': message.content or "", 'embeds': []}
    
    if parts['content']:
        parts['content'] = translate_text(parts['content'])
    
    for embed in message.embeds:
        embed_data = {
            'title': translate_text(embed.title) if embed.title else "",
            'description': translate_text(embed.description) if embed.description else "",
            'color': embed.color.value if embed.color else None,
            'author': {'name': embed.author.name if embed.author else None,
                       'icon_url': embed.author.icon_url if embed.author else None},
            'fields': []
        }
        for field in embed.fields:
            embed_data['fields'].append({
                'name': translate_text(field.name) if field.name else "",
                'value': translate_text(field.value) if field.value else "",
                'inline': field.inline
            })
        parts['embeds'].append(embed_data)
    
    return parts

def translate_text(text):
    """核心翻译函数：先保护所有 @ 提到 → 翻译 → 再完整还原"""
    if len(text.split()) < MIN_WORDS or re.search(r'[\u4e00-\u9fff]', text):
        return text
    
    if not client:
        return text

    # ==================== 1. 保护所有 @ 提到 ====================
    mention_placeholders = {}
    counter = 0

    # 保护 @everyone 和 @here（防止被翻译成 @所有人）
    for mention in ['@everyone', '@here']:
        placeholder = f"@@PROTECTED_MENTION_{counter}@@"
        text = text.replace(mention, placeholder)
        mention_placeholders[placeholder] = mention
        counter += 1

    # 保护用户/角色提到 <@123> <@!123> <@&123>
    def protect_mention(match):
        nonlocal counter
        placeholder = f"@@PROTECTED_MENTION_{counter}@@"
        mention_placeholders[placeholder] = match.group(0)
        counter += 1
        return placeholder

    text = re.sub(r'<@!?&?\d+>', protect_mention, text)
    # ===========================================================

    # ==================== 2. 原有翻译逻辑（保持结构） ====================
    paragraphs = re.split(r'\n\s*\n', text)
    translated_paragraphs = []

    for para in paragraphs:
        if not para.strip():
            translated_paragraphs.append(para)
            continue

        lines = para.split('\n')
        translated_lines = []

        for line in lines:
            line = line.rstrip()
            if not line:
                translated_lines.append(line)
                continue

            # bullet 行单独处理
            if re.match(r'^[•\-\*]\s', line):
                prefix = line[:2]
                content = line[2:].strip()
                if len(content.split()) >= MIN_WORDS:
                    translated = _raw_translate(content)
                    translated_lines.append(prefix + translated)
                else:
                    translated_lines.append(line)
            else:
                if len(line.split()) >= MIN_WORDS:
                    translated_lines.append(_raw_translate(line))
                else:
                    translated_lines.append(line)

        translated_paragraphs.append('\n'.join(translated_lines))

    result = '\n\n'.join(translated_paragraphs)
    # ===========================================================

    # ==================== 3. 还原所有被保护的 @ 提到 ====================
    for placeholder, original in mention_placeholders.items():
        result = result.replace(placeholder, original)
    # ===========================================================

    return result if result != text else text

def _raw_translate(text):
    """底层单行翻译（只负责调用 Google API）"""
    try:
        detection = client.detect_language(text)
        lang = detection['language']
        if lang.startswith('zh') or lang != 'en':
            return text
        translated = client.translate(text, source_language='en', target_language='zh-CN', format_='html')['translatedText']
        return translated
    except Exception as e:
        print(f'翻译异常: {e}')
        return text

async def send_translated_content(webhook, parts, author, mode, original_message=None):
    try:
        if parts['embeds']:
            for i, ed in enumerate(parts['embeds']):
                embed = discord.Embed(title=ed['title'], description=ed['description'], color=ed['color'])
                if ed['author']['name']:
                    embed.set_author(name=ed['author']['name'], icon_url=ed['author']['icon_url'])
                for f in ed['fields']:
                    embed.add_field(name=f['name'], value=f['value'], inline=f['inline'])
                if i == 0 and parts['content']:
                    desc = embed.description or ""
                    embed.description = desc + ("\n\n" + parts['content'] if desc else parts['content'])
                await webhook.send(embed=embed, username=author.display_name,
                                 avatar_url=author.avatar.url if author.avatar else None)
        else:
            await webhook.send(parts['content'], username=author.display_name,
                             avatar_url=author.avatar.url if author.avatar else None)
    except Exception as e:
        print(f'发送失败: {e}')
        await webhook.send(parts['content'], username=author.display_name,
                         avatar_url=author.avatar.url if author.avatar else None)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # 已允许其他 webhook 消息（美股仙人等）
    # if message.webhook_id is not None:
    #     return

    channel_id = message.channel.id
    mode = channel_modes.get(channel_id, 'replace')
    if mode == 'off':
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    parts = extract_and_translate_parts(message)
    content_changed = parts['content'] != message.content or any(
        e['title'] != (message.embeds[i].title or "") or
        e['description'] != (message.embeds[i].description or "")
        for i, e in enumerate(parts['embeds'])
    )

    if not content_changed:
        await bot.process_commands(message)
        return

    try:
        webhook = await message.channel.create_webhook(name=message.author.display_name)
        try:
            if mode == 'replace':
                await message.delete()
            await send_translated_content(webhook, parts, message.author, mode, message)
        finally:
            await webhook.delete()
    except discord.Forbidden:
        if mode == 'replace':
            await message.delete()
        await message.channel.send(parts['content'], reference=message if mode == 'reply' else None)
    except Exception as e:
        print(f'异常: {e}')
        if mode == 'replace':
            await message.delete()
        await message.channel.send(f"**[{message.author.display_name}]** {parts['content']}",
                                 reference=message if mode == 'reply' else None)

    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f'{bot.user} 已上线！')
    synced = await bot.tree.sync()
    print(f'同步了 {len(synced)} 个斜杠命令')

# 下面四个命令保持不变
@bot.tree.command(name='reply_mode', description='在此频道设置回复翻译模式')
async def reply_mode(interaction: discord.Interaction):
    channel_modes[interaction.channel.id] = 'reply'
    await interaction.response.send_message('已设为回复模式（保留原消息，在下方回复翻译）', ephemeral=True)

@bot.tree.command(name='replace_mode', description='在此频道设置删除+代替模式（最像原作者发言）')
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
        await interaction.response.send_message('机器人消息不翻译', ephemeral=True); return
    parts = extract_and_translate_parts(message)
    await interaction.response.send_message(f"翻译结果：\n{parts['content']}", ephemeral=True)

async def main():
    if not TOKEN:
        print('错误: 未设置 DISCORD_TOKEN')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
