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

async def translate_text(text):
    if len(text.split()) < MIN_WORDS or re.search(r'[\u4e00-\u9fff]', text):
        print(f'跳过翻译: 短句或含中文 - {text}')
        return text
    
    if not client:
        print('SDK未初始化，跳过翻译')
        return text
    
    try:
        print(f'翻译调用: {text}')
        detection = client.detect_language(text)
        detected_lang = detection['language']
        print(f'检测语言: {detected_lang}')
        
        if detected_lang.startswith('zh'):
            print('检测为中文，跳过')
            return text
        
        if detected_lang == 'en':
            result = client.translate(text, source_language='en', target_language='zh-CN', format_='html')  # format_='html'保持粗体/Markdown/换行
            translated = result['translatedText']
            print(f'翻译结果: {translated}')
            if translated == text:
                print('翻译与原文相同，跳过')
                return text
            return translated
        else:
            print('非英文，跳过')
            return text
    except Exception as e:
        print(f'翻译异常详情: {e}')
        return text

@bot.event
async def on_message(message):
    if message.author == bot.user or message.webhook_id:
        return
    
    channel_id = message.channel.id
    mode = channel_modes.get(channel_id, 'replace')  # 默认replace
    
    if mode == 'off':
        return  # 关闭自动翻译
    
    if isinstance(message.channel, discord.TextChannel):
        translated = await translate_text(message.content)
        print(f'准备发送翻译: {translated}')
        if translated and translated != message.content:
            try:
                webhook = await message.channel.create_webhook(name=message.author.display_name)
                try:
                    if mode == 'replace':
                        try:
                            await message.delete()
                            print('原消息删除成功')
                        except Exception as e:
                            print(f'删除异常: {e}')
                        await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url if message.author.avatar else None)
                        print('Webhook代替发送成功')
                    elif mode == 'reply':
                        # 直接发送翻译内容作为回复（不带Bot名，纯translated，保持格式）
                        await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url if message.author.avatar else None, reference=message)
                        print('Webhook回复发送成功 (纯翻译内容)')
                finally:
                    await webhook.delete()
            except discord.Forbidden as e:
                print(f'Webhook权限失败: {e}，Fallback发送')
                if mode == 'replace':
                    try:
                        await message.delete()
                    except Exception as e:
                        print(f'Fallback删除异常: {e}')
                if mode == 'reply':
                    await message.reply(translated)  # 直接reply translated，保持格式
                else:
                    await message.channel.send(f"**[{message.author.display_name}]** {translated}")
            except Exception as e:
                print(f'Webhook异常: {e}，Fallback发送')
                if mode == 'replace':
                    try:
                        await message.delete()
                    except Exception as e:
                        print(f'Fallback删除异常: {e}')
                if mode == 'reply':
                    await message.reply(translated)
                else:
                    await message.channel.send(f"**[{message.author.display_name}]** {translated}")
    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f'{bot.user} 已上线！')
    try:
        synced = await bot.tree.sync()
        print(f'同步了 {len(synced)} 命令')
    except Exception as e:
        print(f'命令同步失败: {e}')

# 三个独立命令
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
    translated = await translate_text(message.content)
    if translated == message.content:
        await interaction.response.send_message('无需翻译（已是中文或太短）', ephemeral=True)
    else:
        await interaction.response.send_message(f'翻译：{translated}', ephemeral=True)

async def main():
    if not TOKEN:
        print('错误: DISCORD_TOKEN 未设置！')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
