import discord
from discord.ext import commands
import asyncio
import os  # 用环境变量安全读取Token/Key
import aiohttp  # 用aiohttp异步请求Google Translate
import json  # 解析JSON响应

# 你的配置（用环境变量，Railway设置）
TOKEN = os.getenv('DISCORD_TOKEN')  # 从Railway Variables读取
MIN_WORDS = 5  # 少于5字不翻译
DELETE_MODE = True  # 默认开启删除原始消息（/toggle命令切换）

# Bot设置： intents是权限
intents = discord.Intents.default()
intents.message_content = True  # 读消息内容
bot = commands.Bot(command_prefix='!', intents=intents)

# 翻译函数：用aiohttp刮取Google Translate API（免费、无额外库）
async def translate_text(text):
    if len(text.split()) < MIN_WORDS:  # 少于5字返回原文本
        return text
    
    url = 'https://translate.google.com/translate_a/single'
    params = {
        'client': 'gtx',
        'sl': 'auto',
        'tl': 'zh-CN',  # 简体中文
        'dt': 't',
        'q': text
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    translated = data[0][0][0]
                    detected_lang = data[2]  # 检测语言代码，如 'en' 或 'zh'
                    
                    # 如果是中文（zh包括简/繁）或翻译==原文本，不翻译
                    if detected_lang.startswith('zh') or translated == text:
                        return text
                    
                    return translated
                else:
                    print(f'Google API错误: {response.status}')  # 调试用
                    return text
    except Exception as e:
        print(f'翻译异常: {e}')  # 调试用
        return text  # 出错返回原文本

# 自动翻译：监听消息
@bot.event
async def on_message(message):
    if message.author == bot.user:  # 忽略自己消息
        return
    
    # 只在文本频道
    if isinstance(message.channel, discord.TextChannel):
        translated = await translate_text(message.content)
        
        if translated and translated != message.content:  # 需要翻译
            # 用Webhook模拟原作者头像/ID发送
            try:
                webhook = await message.channel.create_webhook(name=message.author.display_name)
                try:
                    if DELETE_MODE:
                        await message.delete()  # 删除原消息
                        await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url if message.author.avatar else None)
                    else:
                        await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url if message.author.avatar else None)
                finally:
                    await webhook.delete()  # 清理webhook
            except discord.Forbidden:
                print('权限不足: 无法创建webhook或删除消息')  # 调试用
                # 备选：直接Bot发，但头像不对
                await message.channel.send(f"**[{message.author.display_name}]** {translated}")
    
    await bot.process_commands(message)  # 处理命令

# Bot就绪事件：在这里同步命令
@bot.event
async def on_ready():
    print(f'{bot.user} 已上线！')
    try:
        synced = await bot.tree.sync()
        print(f'同步了 {len(synced)} 命令')
    except Exception as e:
        print(f'命令同步失败: {e}')

# /toggle命令：开关删除模式
@bot.tree.command(name='toggle', description='切换删除原始消息模式')
async def toggle(interaction: discord.Interaction):
    global DELETE_MODE
    DELETE_MODE = not DELETE_MODE
    status = '开启' if DELETE_MODE else '关闭'
    await interaction.response.send_message(f'删除原始消息模式已{status}（关闭时会在原消息下回复翻译）', ephemeral=True)

# 右键菜单：主动翻译（只操作者可见）
@bot.tree.context_menu(name='翻译此消息')
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    if message.author == bot.user:
        await interaction.response.send_message('不能翻译Bot消息', ephemeral=True)
        return
    
    translated = await translate_text(message.content)
    if translated == message.content:
        await interaction.response.send_message('无需翻译（已是中文或太短）', ephemeral=True)
    else:
        await interaction.response.send_message(f'翻译：{translated}', ephemeral=True)  # 只自己看到

# 启动Bot
async def main():
    if not TOKEN:
        print('错误: DISCORD_TOKEN 未设置！')
        return
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
