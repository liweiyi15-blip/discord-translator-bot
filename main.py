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

# 初始化SDK
json_key = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
if json_key:
    try:
        credentials = service_account.Credentials.from_service_account_info(json.loads(json_key))
        client = translate.Client(credentials=credentials)
        print('SDK初始化成功')
    except Exception as e:
        print(f'SDK初始化失败: {e}')
        client = None
else:
    print('JSON Key 未设置')
    client = None

# per-channel模式: 'reply' (回复翻译), 'replace' (删除+代替), 'off' (关闭)
channel_modes = {}

def extract_and_translate_parts(message):
    """提取消息的各个部分，并分别翻译文本部分。返回翻译后的字典（用于重建）"""
    parts = {
        'content': message.content or "",
        'embeds': []
    }
    
    # 翻译content（现在用preserve_structure）
    if parts['content']:
        parts['content'] = translate_text(parts['content'])
    
    # 处理每个embed（不变）
    for embed in message.embeds:
        embed_data = {
            'title': embed.title or "",
            'description': embed.description or "",
            'color': embed.color.value if embed.color else None,
            'author': {'name': embed.author.name if embed.author else None, 'icon_url': embed.author.icon_url if embed.author else None},
            'fields': []
        }
        
        # 翻译title
        if embed_data['title']:
            embed_data['title'] = translate_text(embed_data['title'])
        
        # 翻译description（用新函数，保持结构）
        if embed_data['description']:
            embed_data['description'] = translate_text(embed_data['description'])
        
        # 翻译每个field的name和value
        for field in embed.fields:
            field_data = {
                'name': translate_text(field.name) if field.name else "",
                'value': translate_text(field.value) if field.value else "",
                'inline': field.inline
            }
            embed_data['fields'].append(field_data)
        
        parts['embeds'].append(embed_data)
    
    return parts

def translate_text(text):
    """翻译单个文本片段（支持HTML格式），新增: 保持结构（换行、bullet）"""
    if len(text.split()) < MIN_WORDS or re.search(r'[\u4e00-\u9fff]', text):
        print(f'跳过翻译: 短句或含中文 - {text[:50]}...')
        return text
    
    if not client:
        print('SDK未初始化，跳过翻译')
        return text
    
    # 新增: 保护结构 - 分割段落和行，分别翻译
    paragraphs = re.split(r'\n\s*\n', text)  # 按双换行分割段落（忽略多余空格）
    translated_paragraphs = []
    
    for para in paragraphs:
        if not para.strip():
            translated_paragraphs.append(para)
            continue
        
        # 段落内按单换行分割行
        lines = para.split('\n')
        translated_lines = []
        
        for line in lines:
            line = line.rstrip()  # 去除行尾空格
            if not line:
                translated_lines.append(line)
                continue
            
            # 检测bullet行（• 或 -），只翻译内容部分
            bullet_prefix = ''
            if re.match(r'^[•\-\*]\s', line):
                bullet_prefix = line[:2]  # 取• + 空格
                content = line[2:].strip()
                if len(content.split()) < MIN_WORDS:
                    translated_lines.append(line)
                    continue
                translated_content = _raw_translate(content)
                translated_line = bullet_prefix + translated_content
            else:
                # 普通行
                if len(line.split()) < MIN_WORDS:
                    translated_lines.append(line)
                    continue
                translated_line = _raw_translate(line)
            
            translated_lines.append(translated_line)
        
        translated_para = '\n'.join(translated_lines)
        translated_paragraphs.append(translated_para)
    
    result = '\n\n'.join(translated_paragraphs)
    
    # 如果结果与原文相同，跳过
    if result == text:
        print('翻译与原文相同，跳过')
        return text
    
    return result

def _raw_translate(text):
    """底层翻译调用（不保护结构，用于行级）"""
    try:
        print(f'翻译调用: {text[:50]}...')
        detection = client.detect_language(text)
        detected_lang = detection['language']
        print(f'检测语言: {detected_lang}')
        
        if detected_lang.startswith('zh'):
            print('检测为中文，跳过')
            return text
        
        if detected_lang == 'en':
            result = client.translate(text, source_language='en', target_language='zh-CN', format_='html')
            translated = result['translatedText']
            print(f'翻译结果: {translated[:50]}...')
            return translated
        else:
            print('非英文，跳过')
            return text
    except Exception as e:
        print(f'翻译异常详情: {e}')
        return text

async def send_translated_content(webhook, parts, author, mode, original_message=None):
    """使用翻译后的parts重建并发送内容（支持Embed）"""
    try:
        if parts['embeds']:
            # 有Embeds：重建每个Embed并发送（不变）
            for i, embed_data in enumerate(parts['embeds']):
                new_embed = discord.Embed(
                    title=embed_data['title'],
                    description=embed_data['description'],
                    color=embed_data['color']
                )
                
                # 添加作者
                if embed_data['author']['name']:
                    new_embed.set_author(
                        name=embed_data['author']['name'],
                        icon_url=embed_data['author']['icon_url']
                    )
                
                # 添加字段
                for field in embed_data['fields']:
                    new_embed.add_field(
                        name=field['name'],
                        value=field['value'],
                        inline=field['inline']
                    )
                
                # 如果是第一个Embed，且有content，添加到description
                if i == 0 and parts['content']:
                    if new_embed.description:
                        new_embed.description += f"\n\n{parts['content']}"
                    else:
                        new_embed.description = parts['content']
                
                await webhook.send(embed=new_embed, username=author.display_name, avatar_url=author.avatar.url if author.avatar else None)
        else:
            # 无Embeds：直接发送翻译content（Discord会渲染\n为换行）
            await webhook.send(
                parts['content'],
                username=author.display_name,
                avatar_url=author.avatar.url if author.avatar else None
            )
        
        # reply模式：Webhook不支持reference，这里用send（Discord会显示为普通消息；如需严格reply，可fallback用bot.reply）
        if mode == 'reply' and original_message:
            pass  # 可选优化：后续用bot.send(content, reference=original_message)
    except Exception as e:
        print(f'发送Embed异常: {e}')
        # 回退：发送合并文本（但用结构化translated content）
        full_text = parts['content']
        for embed in parts['embeds']:
            full_text += f"\n\n{embed['title']}\n{embed['description']}"
            for field in embed['fields']:
                full_text += f"\n**{field['name']}**: {field['value']}"
        await webhook.send(full_text, username=author.display_name, avatar_url=author.avatar.url if author.avatar else None)

# on_message 事件（不变，但用新parts）
@bot.event
async def on_message(message):
    print(f'收到消息: {message.content[:50]}... (频道: {message.channel.name}, 作者: {message.author.display_name}, 是Bot: {message.author.bot})')
    
    if message.author == bot.user:
        return
    
    channel_id = message.channel.id
    mode = channel_modes.get(channel_id, 'replace')
    
    if mode == 'off':
        return
    
    if isinstance(message.channel, discord.TextChannel):
        parts = extract_and_translate_parts(message)
        print(f'翻译后内容预览: {parts["content"][:100]}...')
        
        # 检查变化（简化）
        if parts['content'] == message.content and not parts['embeds']:
            print('无翻译变化，跳过')
            return
        
        try:
            webhook = await message.channel.create_webhook(name=message.author.display_name)
            try:
                if mode == 'replace':
                    await message.delete()
                    print('原消息删除成功')
                await send_translated_content(webhook, parts, message.author, mode, message if mode == 'reply' else None)
                print('Webhook发送成功')
            finally:
                await webhook.delete()
        except discord.Forbidden as e:
            print(f'Webhook权限失败: {e}，Fallback发送')
            if mode == 'replace':
                await message.delete()
            # Fallback: 用bot发送（支持reference）
            if parts['embeds']:
                for embed_data in parts['embeds']:
                    new_embed = discord.Embed(title=embed_data['title'], description=embed_data['description'], color=embed_data['color'])
                    if embed_data['author']['name']:
                        new_embed.set_author(name=embed_data['author']['name'], icon_url=embed_data['author']['icon_url'])
                    for field in embed_data['fields']:
                        new_embed.add_field(name=field['name'], value=field['value'], inline=field['inline'])
                    if parts['content']:
                        new_embed.description += f"\n\n{parts['content']}"
                    await message.channel.send(embed=new_embed, reference=message if mode == 'reply' else None)
            else:
                await message.channel.send(parts['content'], reference=message if mode == 'reply' else None)
        except Exception as e:
            print(f'异常: {e}，Fallback发送')
            if mode == 'replace':
                await message.delete()
            await message.channel.send(f"**[{message.author.display_name}]** {parts['content']}", reference=message if mode == 'reply' else None)
    await bot.process_commands(message)

# 其他事件和命令不变（on_ready, slash commands, context menu）
@bot.event
async def on_ready():
    print(f'{bot.user} 已上线！')
    try:
        synced = await bot.tree.sync()
        print(f'同步了 {len(synced)} 命令')
    except Exception as e:
        print(f'命令同步失败: {e}')

@bot.tree.command(name='reply_mode', description='在此频道设置回复翻译模式 (原下回复翻译)')
async def reply_mode(interaction: discord.Interaction):
    channel_id = interaction.channel.id
    channel_modes[channel_id] = 'reply'
    await interaction.response.send_message('频道翻译模式设为回复模式 (原下回复翻译)，仅此频道生效', ephemeral=True)

@bot.tree.command(name='replace_mode', description='在此频道设置删除代替翻译模式 (删原+发新，头像/ID一样)')
async def replace_mode(interaction: discord.Interaction):
    channel_id = interaction.channel.id
    channel_modes[channel_id] = 'replace'
    await interaction.response.send_message('频道翻译模式设为删除代替模式 (删原+发新)，仅此频道生效', ephemeral=True)

@bot.tree.command(name='off_mode', description='在此频道关闭自动翻译')
async def off_mode(interaction: discord.Interaction):
    channel_id = interaction.channel.id
    channel_modes[channel_id] = 'off'
    await interaction.response.send_message('频道自动翻译已关闭，仅此频道生效', ephemeral=True)

@bot.tree.context_menu(name='翻译此消息')
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    if message.author == bot.user:
        await interaction.response.send_message('不能翻译Bot消息', ephemeral=True)
        return
    parts = extract_and_translate_parts(message)
    if parts['content'] == message.content and not parts['embeds']:
        await interaction.response.send_message('无需翻译（已是中文或太短）', ephemeral=True)
    else:
        full_text = parts['content']
        for embed in parts['embeds']:
            full_text += f"\n\n**{embed['title']}**\n{embed['description']}"
            for field in embed['fields']:
                full_text += f"\n**{field['name']}**: {field['value']}"
        await interaction.response.send_message(f'翻译：\n{full_text}', ephemeral=True)

async def main():
    if not TOKEN:
        print('错误: DISCORD_TOKEN 未设置！')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
