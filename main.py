import os
import runpy

# Change into discord-bot/ so all relative imports (revolver, db, conversation_manager, cogs/)
# resolve correctly, then execute its main.py as __main__ so bot.run() is called.
bot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discord-bot")
os.chdir(bot_dir)
runpy.run_path(os.path.join(bot_dir, "main.py"), run_name="__main__")
