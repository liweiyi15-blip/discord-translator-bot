import discord
from discord.ext import commands
import aiohttp
import asyncio
import requests
import os  # 用环境变量安全读取Token/Key

# 你的配置（用环境变量，Railway设置）
TOKEN = os.getenv('DISCORD_TOKEN')  # 从Railway Variables读取
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')  # 从Railway Variables读取
MODEL = 'deepseek/deepseek-chat:free'  # 免费DeepSeek最高版
MIN_WORDS = 5  # 少于5字不翻译
DELETE_MODE = True  # 默认开启删除原始消息（/toggle命令切换）

# Bot设置： intents是权限
intents = discord.Intents.default()
intents.message_content = True  # 读消息内容
bot = commands.Bot(command_prefix='!', intents=intents)

# API调用辅助函数
async def call_openrouter(prompt):
    headers = {
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'Content-Type': 'application/json'
    }
    data = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': prompt}]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post('https://openrouter.ai/api/v1/chat/completions', headers=headers, json=data) as resp:
            if resp.status == 200:
                result = await resp.json()
                return result['choices'][0]['message']['content'].strip()
            else:
                return None

# 翻译函数：用OpenRouter API（用DeepSeek检测语言）
async def translate_text(text):
    if len(text.split()) < MIN_WORDS:  # 少于5字返回原文本
        return text
    
    try:
        # 用DeepSeek检测语言
        lang_prompt = f"Detect the language of this text. Respond only with 'EN' if English, or 'ZH' if Chinese (simplified or traditional). Do not add extra text. Text: {text}"
        lang_response = await call_openrouter(lang_prompt)
        
        if lang_response != 'EN':  # 非英文不翻译
            return text
        
        # 英文翻译成中文
        translate_prompt = f"Translate the following English text to Chinese (Simplified). Respond only with the translation, no extra text: {text}"
        translated = await call_openrouter(translate_prompt)
        
        return translated if translated else f"翻译失败: {text}"
        
    except Exception:
        return text  # 出错返回原文本

# 自动翻译：监听消息
@bot.event
async def on_message(message):
    if message.author == bot.user:  # 忽略自己消息
        return
    
    # 只在文本频道
    if isinstance(message.channel, discord.TextChannel):
        translated = await translate_text(message.content)
        
        if translated != message.content:  # 需要翻译
            # 用Webhook模拟原作者头像/ID发送
            webhook = await message.channel.create_webhook(name=message.author.display_name)
            try:
                if DELETE_MODE:
                    await message.delete()  # 删除原消息
                    await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url if message.author.avatar else None)
                else:
                    await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url if message.author.avatar else None)
            finally:
                await webhook.delete()  # 清理webhook
    
    await bot.process_commands(message)  # 处理命令

# Bot就绪事件：在这里同步命令（修复MissingApplicationID错误）
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
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
