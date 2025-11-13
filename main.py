import discord
from discord.ext import commands
import aiohttp
import asyncio
import requests
from langdetect import detect, LangDetectException

# 你的配置
TOKEN = 'YOUR_TOKEN'  # 从步骤1复制
OPENROUTER_API_KEY = 'YOUR_OPENROUTER_API_KEY'  # OpenRouter的API Key
MODEL = 'deepseek/deepseek-chat:free'  # 免费DeepSeek最高版
MIN_WORDS = 5  # 少于5字不翻译
DELETE_MODE = True  # 默认开启删除原始消息（/toggle命令切换）

# Bot设置： intents是权限
intents = discord.Intents.default()
intents.message_content = True  # 读消息内容
bot = commands.Bot(command_prefix='!', intents=intents)

# 翻译函数：用OpenRouter API
async def translate_text(text):
    if len(text.split()) < MIN_WORDS:  # 少于5字返回原文本
        return text
    
    try:
        # 检测语言
        lang = detect(text)
        if lang in ['zh-cn', 'zh-tw', 'zh']:  # 中文/繁体不翻译
            return text
        
        # 英文翻译成中文
        prompt = f"Translate the following English text to Chinese (Simplified): {text}"
        
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
                    return f"翻译失败: {text}"  # 出错返回原文本
    except LangDetectException:
        return text  # 检测失败不翻译

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
                    await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url)
                else:
                    await webhook.send(translated, username=message.author.display_name, avatar_url=message.author.avatar.url)
            finally:
                await webhook.delete()  # 清理webhook
    
    await bot.process_commands(message)  # 处理命令

# /toggle命令：开关删除模式
@bot.tree.command(name='toggle', description='切换删除原始消息模式')
async def toggle(interaction: discord.Interaction):
    global DELETE_MODE
    DELETE_MODE = not DELETE_MODE
    status = '开启' if DELETE_MODE else '关闭'
    await interaction.response.send_message(f'删除原始消息模式已{status}（关闭时会在原消息下回复翻译）', ephemeral=True)

# 右键菜单：主动翻译（只操作者可见）
@bot.tree.command(name='translate_message', description='翻译此消息')  # 改成slash子命令，避免context_menu复杂
@app.commands.describe(message='要翻译的消息')
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    if message.author == bot.user:
        await interaction.response.send_message('不能翻译Bot消息', ephemeral=True)
        return
    
    translated = await translate_text(message.content)
    if translated == message.content:
        await interaction.response.send_message('无需翻译（已是中文或太短）', ephemeral=True)
    else:
        await interaction.response.send_message(f'翻译：{translated}', ephemeral=True)  # 只自己看到

# 注意：右键功能用slash reply消息实现（/translate_message @消息），简单版；想纯右键后问我优化

# 启动Bot
async def main():
    async with bot:
        synced = await bot.tree.sync()  # 同步命令
        print(f'同步了 {len(synced)} 命令')
        await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())