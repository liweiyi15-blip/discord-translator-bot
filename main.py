import discord
from discord.ext import commands
import asyncio
import os
import requests  # REST API
import re

# 配置
TOKEN = os.getenv('DISCORD_TOKEN')
GOOGLE_KEY = os.getenv('GOOGLE_TRANSLATE_API_KEY')  # 你的API Key
MIN_WORDS = 5
DELETE_MODE = True

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

async def translate_text(text):
    if len(text.split()) < MIN_WORDS or re.search(r'[\u4e00-\u9fff]', text):
        return text
    
    url = f'https://translation.googleapis.com/language/translate/v2?key={GOOGLE_KEY}'
    data = {
        'q': text,
        'source': 'en',
        'target': 'zh-CN',
        'format': 'text'
    }
    
    try:
        response = requests.post(url, json=data)
        if response.status_code == 200:
            result = response.json()
            translated = result['data']['translations'][0]['translatedText']
            if translated == text:
                return text
            return translated
        else:
            print(f'Google API错误: {response.status_code}')
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
