"""Telegram class-group connection handling."""
from telegram import Update
from telegram.ext import ContextTypes
from src.access_control import is_user_group_admin, set_group_authorization


async def connect_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /connect or /authorize in a Telegram class group."""
    chat = update.effective_chat
    user = update.effective_user

    if not await is_user_group_admin(update):
        await update.message.reply_text(
            "🔒 Access Denied (AC5): Only class representatives or group admins can authorize @jumnuybot."
        )
        return

    set_group_authorization(
        group_id=chat.id,
        group_title=chat.title or f"PrivateChat-{user.id}",
        group_username=chat.username or "",
        authorized=True,
        admin_id=user.id,
    )

    await update.message.reply_text(
        f"✅ Class Group Connected!**\n"
        f"• **Group: {chat.title or 'Direct Chat'}\n"
        f"• Group ID: `{chat.id}`\n"
        f"• Status: Authorized for Academic Group Memory (F01).",
        parse_mode="Markdown",
    )


async def revoke_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /revoke to pause indexing in a class group (AC5 Privacy)."""
    chat = update.effective_chat
    user = update.effective_user

    if not await is_user_group_admin(update):
        await update.message.reply_text("🔒 Only group admins can revoke bot access.")
        return

    set_group_authorization(
        group_id=chat.id,
        group_title=chat.title or "Chat",
        group_username=chat.username or "",
        authorized=False,
        admin_id=user.id,
    )
    await update.message.reply_text("⏸️ Group indexing has been paused for privacy (AC5).")