"""The Telegram bot. Long polling, no webhook, no public IP.

PROMPT.md M8. Two operators share one supergroup, and a group is joinable by
anyone, so **the allowlist is the whole access control**. It is applied to
every update including callback queries -- a button is as much an entry point
as a command, and forgetting the callback path is the classic way an allowlist
turns out to be decorative.

Everything that decides *what* to send lives in `radar/alerts.py` and
everything that decides *how it reads* lives in `radar/card.py`, both testable
without a token or an event loop. What is left here is Telegram itself.

    python -m radar.bot            # long-polls until interrupted
    python -m radar.bot --send     # push any cards due, then keep polling
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import sqlite3
import sys
from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
)
from telegram.ext import filters as tg_filters

from . import alerts as alerts_mod
from . import card as card_mod
from . import config as config_module
from . import db, log

WATCH_PREFIX = "w"
DISMISS_PREFIX = "d"
REASON_PREFIX = "r"
WHY_PREFIX = "y"

REFUSAL = "You are not one of this radar's operators, so I take no instructions from you."

# httpx logs the full request URL at INFO, and for Telegram the token is *in*
# the URL -- so an INFO-level run writes the bot token into every log line and
# into any log file or paste that follows. Nothing downstream needs those
# lines; the bot logs its own outcomes.
NOISY_LOGGERS = ("httpx", "httpcore", "telegram.ext.Updater")


def quieten_http_logging() -> None:
    import logging

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def keyboard(opportunity_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👁 Watch", callback_data=f"{WATCH_PREFIX}:{opportunity_id}"),
                InlineKeyboardButton(
                    "✕ Dismiss", callback_data=f"{DISMISS_PREFIX}:{opportunity_id}"
                ),
                InlineKeyboardButton("📄 Why", callback_data=f"{WHY_PREFIX}:{opportunity_id}"),
            ]
        ]
    )


def reason_keyboard(opportunity_id: int) -> InlineKeyboardMarkup:
    """M9's labels. Every dismissal is a training row, so the reason is asked
    for at the moment the operator actually has one."""
    buttons = [
        InlineKeyboardButton(
            reason.replace("_", " "), callback_data=f"{REASON_PREFIX}:{opportunity_id}:{reason}"
        )
        for reason in alerts_mod.DISMISS_REASONS
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


class Bot:
    def __init__(self, cfg: config_module.Config, conn: sqlite3.Connection) -> None:
        self.cfg = cfg
        self.conn = conn
        self.logger = log.get(__name__)
        self.chat_id = cfg.environ.get("TELEGRAM_CHAT_ID")
        self.horizon = int(cfg.get("scoring.durability_horizon", 60))
        self.decision_mode = cfg.get("telegram.decision_mode", "first_wins")

    # --- access control ---------------------------------------------------

    def actor(self, update: Update) -> tuple[int, str] | None:
        """The operator behind this update, or None for anyone else.

        Covers messages and callback queries alike. `effective_user` is the
        single place Telegram puts the human for both, which is exactly why it
        is the only thing consulted.
        """
        user = update.effective_user
        if user is None:
            return None
        if not self.cfg.is_operator(user.id):
            return None
        return user.id, self.cfg.operator_name(user.id) or (user.first_name or str(user.id))

    async def guard(self, update: Update) -> tuple[int, str] | None:
        who = self.actor(update)
        if who is not None:
            return who

        user = update.effective_user
        self.logger.warning(
            "rejected a non-operator",
            extra={"tg_id": getattr(user, "id", None), "name": getattr(user, "username", None)},
        )
        if update.callback_query is not None:
            await update.callback_query.answer(REFUSAL, show_alert=True)
        elif self.cfg.get("telegram.reject_unknown_users", True) and update.effective_message:
            await update.effective_message.reply_text(REFUSAL)
        return None

    # --- commands ---------------------------------------------------------

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.guard(update) is None:
            return
        due = self.due_cards()
        if not due:
            await update.effective_message.reply_text(
                "Nothing new passes the gate right now. Run a sweep with "
                "`python -m radar --once --compose` first.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        for candidate in due:
            await self.send_card(context.bot, candidate)

    async def cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.guard(update) is None:
            return
        rows = self.conn.execute(
            "SELECT o.id, o.title, d.actor_name, d.ts FROM decisions d "
            "JOIN opportunities o ON o.id = d.opportunity_id "
            "WHERE d.action = ? ORDER BY d.ts DESC LIMIT 20",
            (alerts_mod.WATCH,),
        ).fetchall()
        if not rows:
            await update.effective_message.reply_text("Nothing on the watchlist yet.")
            return
        lines = [
            f"#{r['id']} {r['title']} — {r['actor_name']}, "
            f"{dt.datetime.fromtimestamp(r['ts']):%d %b}"
            for r in rows
        ]
        await update.effective_message.reply_text("👁 Watchlist\n" + "\n".join(lines))

    async def cmd_why(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.guard(update) is None:
            return
        if not context.args:
            await update.effective_message.reply_text("Usage: /why <opportunity id>")
            return
        try:
            opportunity_id = int(context.args[0].lstrip("#"))
        except ValueError:
            await update.effective_message.reply_text("That is not an id.")
            return
        await update.effective_message.reply_text(
            self.why(opportunity_id), parse_mode=ParseMode.HTML
        )

    async def cmd_outcome(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        who = await self.guard(update)
        if who is None:
            return
        usage = "Usage: /outcome <id> <tested yes|no> [spent] [revenue] [notes]"
        if len(context.args) < 2:
            await update.effective_message.reply_text(usage)
            return
        try:
            opportunity_id = int(context.args[0].lstrip("#"))
            tested = context.args[1].lower() in {"yes", "y", "true", "1"}
            spent = float(context.args[2]) if len(context.args) > 2 else None
            revenue = float(context.args[3]) if len(context.args) > 3 else None
        except ValueError:
            await update.effective_message.reply_text(usage)
            return

        notes = " ".join(context.args[4:]) or None
        actor_id, actor_name = who
        self.conn.execute(
            "INSERT INTO outcomes (opportunity_id, tested, spent_usd, revenue_usd, "
            "notes, actor_tg_id, actor_name, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (opportunity_id, int(tested), spent, revenue, notes, actor_id, actor_name, db.now()),
        )
        self.conn.commit()
        await update.effective_message.reply_text(
            f"Recorded outcome for #{opportunity_id}. This is the loop the "
            "predecessor never closed, so thank you."
        )

    # --- callbacks --------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        who = await self.guard(update)
        if who is None:
            return
        query = update.callback_query
        actor_id, actor_name = who
        parts = (query.data or "").split(":")
        kind, opportunity_id = parts[0], int(parts[1])

        if kind == WHY_PREFIX:
            await query.answer()
            await query.message.reply_text(self.why(opportunity_id), parse_mode=ParseMode.HTML)
            return

        if kind == DISMISS_PREFIX:
            await query.answer()
            await query.message.reply_text(
                f"Why dismiss #{opportunity_id}?", reply_markup=reason_keyboard(opportunity_id)
            )
            return

        reason = parts[2] if kind == REASON_PREFIX and len(parts) > 2 else None
        action = alerts_mod.WATCH if kind == WATCH_PREFIX else alerts_mod.DISMISS

        written, message = alerts_mod.record_decision(
            self.conn,
            opportunity_id,
            action,
            actor_id,
            actor_name,
            reason=reason,
            decision_mode=self.decision_mode,
        )
        self.conn.commit()
        await query.answer(message[:200], show_alert=not written)
        if written:
            suffix = f" ({reason.replace('_', ' ')})" if reason else ""
            await query.message.reply_text(f"{actor_name}: {action}{suffix} on #{opportunity_id}")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log operator chatter so it is visible without a second poller."""
        who = self.actor(update)
        if who is None:
            return
        _, name = who
        text = (update.effective_message.text or "").strip()
        self.logger.info("operator said", extra={"who": name, "text": text})

    # --- content ----------------------------------------------------------

    def why(self, opportunity_id: int) -> str:
        """The evidence trail. Says which numbers are measured and which are
        estimated, and names the scorer -- /why exists so a card can never be
        taken on faith."""
        row = self.conn.execute(
            "SELECT o.*, t.term FROM opportunities o JOIN terms t ON t.id = o.term_id "
            "WHERE o.id = ?",
            (opportunity_id,),
        ).fetchone()
        if row is None:
            return f"No opportunity #{opportunity_id}."

        score = self.conn.execute(
            "SELECT * FROM scores WHERE term_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (row["term_id"],),
        ).fetchone()

        lines = [f"<b>#{opportunity_id} {html.escape(row['title'])}</b>",
                 f"term: {html.escape(row['term'])}", ""]
        if score:
            lines += [
                "<b>Measured</b> (from the database)",
                f"· durability +{self.horizon}d: {score[f'durability_{self.horizon}']}",
                f"· scorer: {score['scorer']}",
                f"· saturation: {score['saturation_label']} ({score['saturation_raw']:,} counted)",
                f"· demand growth 7d: {score['demand_growth'] * 100:+.0f}%",
                f"· supply growth: {score['supply_growth'] * 100:+.0f}%",
                f"· composite: {score['composite']:.3f}",
                "",
            ]
            if "momentum_fallback" in (score["scorer"] or ""):
                lines += [
                    "⚠ Durability here is naive momentum, not the model: that "
                    "horizon did not beat momentum in the backtest.",
                    "",
                ]
        else:
            lines += ["No score row for this term yet.", ""]

        lines += [
            "<b>Estimated</b> (LLM, may be wrong)",
            f"· setup: {row['setup_cost_usd']} · price: {row['price_usd']} "
            f"· cost/sale: {row['cost_per_sale_usd']}",
            f"· margin: {row['margin_multiple']} · first dollar: {row['ttfd_days']}d "
            f"· confidence: {row['confidence']}",
            f"· model: {row['llm_model']}",
        ]
        if not row["feasible"]:
            lines += ["", f"<b>Gated:</b> {html.escape(row['feasible_reasons'] or '')}"]

        # The personal model is not active until M9 has labels, and /why is
        # where PROMPT.md says to admit it.
        labels = self.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
        if labels < 100:
            lines += [
                "",
                f"Relevance is rule-based: {labels}/100 decisions recorded, so the "
                "personal model is not active yet.",
            ]
        return "\n".join(lines)

    def due_cards(self, *, limit: int | None = None) -> list[alerts_mod.Candidate]:
        """Candidates that survive suppression, the daily cap and quiet hours."""
        max_per_day = int(self.cfg.get("alerts.max_per_day", 12))
        quiet = self.cfg.get("alerts.quiet_hours", [23, 8]) or [23, 8]
        midnight = int(
            dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
        )
        already = alerts_mod.sent_today(self.conn, since_ts=midnight)
        room = max(0, max_per_day - already)
        if room == 0:
            self.logger.info("daily alert cap reached", extra={"sent": already})
            return []
        if alerts_mod.in_quiet_hours(dt.datetime.now().hour, quiet):
            self.logger.info("inside quiet hours, holding cards")
            return []

        due = []
        for candidate in alerts_mod.candidates(self.conn):
            verdict = alerts_mod.should_send(
                self.conn,
                candidate,
                rescore_delta=float(self.cfg.get("alerts.rescore_delta", 0.15)),
                min_composite=float(self.cfg.get("alerts.min_composite", 0.0)),
            )
            if verdict.send:
                due.append(candidate)
            if len(due) >= min(room, limit or room):
                break
        return due

    async def send_card(self, tg_bot, candidate: alerts_mod.Candidate) -> None:
        card = card_mod.build(self.conn, candidate.opportunity_id, horizon=self.horizon)
        if card is None:
            return
        await tg_bot.send_message(
            chat_id=self.chat_id,
            text=card.render(),
            reply_markup=keyboard(candidate.opportunity_id),
            disable_web_page_preview=True,
        )
        alerts_mod.record_alert(self.conn, candidate)
        self.conn.commit()
        self.logger.info(
            "card sent",
            extra={"opportunity_id": candidate.opportunity_id, "composite": candidate.composite},
        )

    async def push_due(self, tg_bot) -> None:
        """Send whatever is due. Takes the Telegram bot rather than a context
        so it can run from post_init as well as from a handler -- PTB's
        JobQueue is an optional extra and this needs no scheduler."""
        due = self.due_cards()
        self.logger.info("pushing due cards", extra={"count": len(due)})
        for candidate in due:
            await self.send_card(tg_bot, candidate)


def build_application(
    cfg: config_module.Config, conn: sqlite3.Connection, *, send_on_start: bool = False
) -> Application:
    token = cfg.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise config_module.ConfigError("TELEGRAM_BOT_TOKEN is not set; the bot cannot start.")

    bot = Bot(cfg, conn)
    builder = Application.builder().token(token)
    if send_on_start:
        async def _push(application: Application) -> None:
            await application.bot_data["radar_bot"].push_due(application.bot)

        builder = builder.post_init(_push)
    app = builder.build()
    app.bot_data["radar_bot"] = bot

    app.add_handler(CommandHandler("scan", bot.cmd_scan))
    app.add_handler(CommandHandler("watchlist", bot.cmd_watchlist))
    app.add_handler(CommandHandler("why", bot.cmd_why))
    app.add_handler(CommandHandler("outcome", bot.cmd_outcome))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    # Anything else an operator says is logged, not answered. The bot is the
    # only process allowed to long-poll -- Telegram serves getUpdates to one
    # consumer and 409s the rest -- so while it runs it is also the only way
    # to see what the operators wrote.
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, bot.on_message))
    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m radar.bot")
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--plain-logs", action="store_true")
    parser.add_argument(
        "--send",
        action="store_true",
        help="push any cards that are due at startup, then keep polling",
    )
    args = parser.parse_args(argv)

    log.setup(args.log_level, json_output=not args.plain_logs)
    quieten_http_logging()
    logger = log.get(__name__)
    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        logger.error("config error", extra={"error": str(exc)})
        return 2

    conn = db.connect(cfg.db_path)
    try:
        app = build_application(cfg, conn, send_on_start=args.send)
    except config_module.ConfigError as exc:
        logger.error("cannot start", extra={"error": str(exc)})
        conn.close()
        return 2

    logger.info(
        "bot starting", extra={"operators": [op.name for op in cfg.operators]}
    )
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
