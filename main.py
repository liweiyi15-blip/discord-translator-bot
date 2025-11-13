import discord
from discord.ext import commands
import asyncio
import os
import json
from google.cloud import translate_v2 as translate  # 官方SDK
from google.oauth2 import service_account  # 认证
import re

# 配置
TOKEN = os.getenv('DISCORD_TOKEN')
MIN_WORDS = 5  # 少于5字不翻译
DELETE_MODE = True  # 默认开启删除原始消息（/toggle切换）

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 初始化SDK（从Railway Variables加载JSON内容）
json_key = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
if json_key:
    credentials = service_account.Credentials.from_service_account_info(json.loads(json_key))
    client = translate.Client(credentials=credentials)
else:
    raise ValueError('GOOGLE_APPLICATION_CREDENTIALS 未设置！')

async def translate_text(text):
    if len(text.split()) < MIN_WORDS or re.search(r'[\u4e00-\u9fff]', text):
        return text
    
    try:
        # 检测语言
        detection = client.detect_language(text)
        detected_lang = detection['language']
        
        if detected_lang.startswith('zh'):
            return text
        
        # 翻译英→简中
        if detected_lang == 'en':
            result = client.translate(text, target_language='zh-CN')
            translated = result['translatedText']
            if translated == text:
                return text
            return translated
        else:
            return text
    except Exception as e:
        print(f'翻译异常: {e}')
        return text

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if isinstance(message.channel, discord.TextChannel):
        translated = await translate_text(message.content)
        if translated and translated != message.content:
            try:
                webhook = await message.channel.create_webhook(name=message.author.display_name)
                try:
                    if DELETE_MODE:
                        await message.delete()
                        await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url if message.author.avatar else None)
                    else:
                        await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url if message.author.avatar else None)
                finally:
                    await webhook.delete()
            except discord.Forbidden:
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

@bot.tree.command(name='toggle', description='切换删除原始消息模式')
async def toggle(interaction: discord.Interaction):
    global DELETE_MODE
    DELETE_MODE = not DELETE_MODE
    status = '开启' if DELETE_MODE else '关闭'
    await interaction.response.send_message(f'删除原始消息模式已{status}（关闭时会在原消息下回复翻译）', ephemeral=True)

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
