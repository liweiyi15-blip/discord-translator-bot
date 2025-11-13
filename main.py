import discord
from discord.ext import commands
import asyncio
import os  # 用环境变量安全读取Token/Key
from deep_translator import GoogleTranslator  # Google Translate库（兼容3.13）

# 你的配置（用环境变量，Railway设置）
TOKEN = os.getenv('DISCORD_TOKEN')  # 从Railway Variables读取
MIN_WORDS = 5  # 少于5字不翻译
DELETE_MODE = True  # 默认开启删除原始消息（/toggle命令切换）

# Bot设置： intents是权限
intents = discord.Intents.default()
intents.message_content = True  # 读消息内容
bot = commands.Bot(command_prefix='!', intents=intents)

# 初始化翻译器（全局单例，高效）
translator = GoogleTranslator(source='auto', target='zh-CN')  # auto检测，zh-CN=简中（修复语言代码）

# 翻译函数：用deep-translator（自动检测+英翻中）
async def translate_text(text):
    if len(text.split()) < MIN_WORDS:  # 少于5字返回原文本
        return text
    
    try:
        # 先检测语言
        detected = translator.detect(text)
        src_lang = detected.lang
        
        # 如果是中文（zh-CN/zh-TW）不翻译
        if src_lang in ['zh-CN', 'zh-TW', 'zh']:
            return text
        
        # 翻译
        translated = translator.translate(text)
        
        # 如果翻译前后相似，跳过（防误翻）
        if translated == text:
            return text
        
        return translated
        
    except Exception as e:
        print(f'翻译异常: {e}')  # 调试用
        return f"翻译失败: {text}"  # 出错返回原文本

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
