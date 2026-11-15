# -*- coding: utf-8 -*-
"""Статистика рекрутёров и оплата им из казны (Фаза 3).
• Каждое решение по заявке пишется в hr_actions: кто (reviewer_id), исход (interview/accepted/denied), когда (ts).
  Один исход на пару «заявка + рекрутёр» (UNIQUE) — повторный вызов на собеседование не задваивает.
  Источники: кнопки карточки заявки, ручной приём, перенос кандидата ботом в комнату собеседования.
• /рекрутёры статистика — за неделю / месяц / N дней: обзвонов, принято, отклонено (+ к выплате по ставкам).
• /рекрутёры ставка — сколько платить за обзвон / за принятого (recruit:rate:<исход>, Глава/зам).
• /рекрутёры выплатить — всё, что не оплачено с прошлой выплаты (recruit:paid_ts), через казну с подтверждением.
• Речь: recruit_stats {"days":7}. Кнопки: панель → Заявки."""
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.botcore import staff_only

log = logging.getLogger("family-bot.recruit")
OUTCOMES = {"interview": "🎤 обзвон", "accepted": "✅ принят", "denied": "❌ отклонён"}
PAYABLE = {"interview": "за обзвон (собеседование)", "accepted": "за принятого в семью"}
RATE_SETTERS = ["leader", "deputy"]
PERIODS = [app_commands.Choice(name="неделя", value=7), app_commands.Choice(name="месяц", value=30),
           app_commands.Choice(name="3 месяца", value=90), app_commands.Choice(name="всё время", value=0)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_actions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, app_id INTEGER, reviewer_id INTEGER NOT NULL,
    outcome TEXT NOT NULL, ts INTEGER NOT NULL, UNIQUE(guild_id, app_id, reviewer_id, outcome));
CREATE INDEX IF NOT EXISTS ix_hr_actions_g ON hr_actions(guild_id, ts);
"""


class RecruitPayView(discord.ui.View):
    def __init__(self, cog, author_id, pairs, until):
        super().__init__(timeout=300)
        self.cog, self.author_id, self.pairs, self.until = cog, author_id, pairs, until

    async def interaction_check(self, inter):
        if inter.user.id != self.author_id:
            await inter.response.send_message("Подтвердить может только тот, кто проводит выплату.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Выплатить", style=discord.ButtonStyle.success, emoji="💸")
    async def yes(self, inter, _):
        self.stop()
        tr = inter.client.get_cog("Treasury")
        if not tr:
            return await inter.response.edit_message(content="Казна не загружена.", view=None)
        await inter.response.edit_message(content="💸 Выплачиваю…", view=None)
        text = await tr.payout(inter.guild, inter.user, self.pairs, "рекрутёрам по статистике")
        if text.startswith("💸"):
            inter.client.db.gset(inter.guild, "recruit:paid_ts", self.until)
        await inter.edit_original_response(content=text)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def no(self, inter, _):
        self.stop()
        await inter.response.edit_message(content="Отменено. Казна не тронута.", view=None)


class Recruit(commands.Cog):
    group = app_commands.Group(name="рекрутёры", description="Статистика рекрутёров и оплата им",
                               default_permissions=discord.Permissions(move_members=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)
        bot.db.c.commit()

    # ---------- запись ----------
    def record(self, guild, app_id, reviewer_id, outcome):
        if not reviewer_id or outcome not in OUTCOMES:
            return
        try:
            self.bot.db.q("INSERT OR IGNORE INTO hr_actions(guild_id, app_id, reviewer_id, outcome, ts) "
                          "VALUES(?,?,?,?,?)", getattr(guild, "id", guild), app_id, reviewer_id, outcome,
                          int(time.time()))
        except Exception:
            log.exception("Статистика рекрутёров: запись")

    # ---------- отчёт ----------
    def rates(self, guild):
        out = {}
        for k in PAYABLE:
            try:
                out[k] = max(0, int(self.bot.db.gget(guild, f"recruit:rate:{k}") or 0))
            except (TypeError, ValueError):
                out[k] = 0
        return out

    def stats(self, guild, since_ts=0, until_ts=None):
        """{reviewer_id: {outcome: n}} за период."""
        until_ts = until_ts or int(time.time()) + 1
        out = {}
        for r in self.bot.db.all("SELECT reviewer_id, outcome, COUNT(*) n FROM hr_actions WHERE guild_id=? AND ts>? "
                                 "AND ts<=? GROUP BY reviewer_id, outcome", guild.id, since_ts, until_ts):
            out.setdefault(r["reviewer_id"], {})[r["outcome"]] = r["n"]
        return out

    def owed(self, stats, rates):
        return {uid: sum(rates.get(k, 0) * n for k, n in st.items()) for uid, st in stats.items()}

    def report_text(self, guild, days=7):
        days = max(0, min(3650, int(days or 0)))
        since = int(time.time()) - days * 86400 if days else 0
        st = self.stats(guild, since)
        period = f"за {days} дн." if days else "за всё время"
        if not st:
            return f"📊 Рекрутёры {period}: решений по заявкам не было."
        rates = self.rates(guild)
        pay = self.owed(st, rates)
        rows = sorted(st.items(), key=lambda kv: (kv[1].get("accepted", 0), kv[1].get("interview", 0)), reverse=True)
        lines = [f"📊 **Рекрутёры {period}** — 🎤 обзвонов · ✅ принято · ❌ отклонено"]
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, s) in enumerate(rows[:20]):
            m = guild.get_member(uid)
            name = m.mention if m else f"<@{uid}>"
            money = f" · 💸 {self._fmt(pay[uid])}" if any(rates.values()) and pay.get(uid) else ""
            lines.append(f"{medals[i] if i < 3 else f'`{i + 1}.`'} {name} — 🎤 {s.get('interview', 0)} · "
                         f"✅ {s.get('accepted', 0)} · ❌ {s.get('denied', 0)}{money}")
        tot = {k: sum(s.get(k, 0) for s in st.values()) for k in OUTCOMES}
        lines.append(f"\nВсего: 🎤 {tot['interview']} · ✅ {tot['accepted']} · ❌ {tot['denied']}")
        if any(rates.values()):
            lines.append("Ставки: " + ", ".join(f"{PAYABLE[k]} — {self._fmt(v)}" for k, v in rates.items() if v))
            paid = self.bot.db.gget(guild, "recruit:paid_ts")
            lines.append(f"Последняя выплата рекрутёрам: <t:{paid}:R>" if paid else "Рекрутёрам ещё не платили.")
        return "\n".join(lines)

    def _fmt(self, n):
        tr = self.bot.get_cog("Treasury")
        if tr:
            from cogs.treasury import fmt
            return fmt(n)
        return f"{n:,}".replace(",", " ")

    # ---------- команды ----------
    @group.command(name="статистика", description="Обзвоны, принятые и отклонённые по каждому рекрутёру за период")
    @app_commands.rename(period="период", days="дней")
    @app_commands.describe(period="неделя / месяц / …", days="Или своё число дней (1–3650) — важнее периода")
    @app_commands.choices(period=PERIODS)
    @staff_only(config.HR + ["treasurer"])
    async def c_stats(self, inter: discord.Interaction, period: app_commands.Choice[int] = None,
                      days: app_commands.Range[int, 1, 3650] = None):
        d = days if days else (period.value if period else 7)
        await inter.response.send_message(embed=self.bot.embed("📊 Статистика рекрутёров",
                                                               self.report_text(inter.guild, d)[:4000],
                                                               config.COLOR, inter.guild), ephemeral=True)

    @group.command(name="ставка", description="Сколько платить рекрутёру: за обзвон или за принятого (0 — не платить)")
    @app_commands.rename(kind="за_что", amount="сумма")
    @app_commands.choices(kind=[app_commands.Choice(name=v, value=k) for k, v in PAYABLE.items()])
    @staff_only(RATE_SETTERS)
    async def c_rate(self, inter: discord.Interaction, kind: app_commands.Choice[str], amount: str):
        from cogs.treasury import parse_amount
        raw = str(amount).strip()
        n = 0 if raw in ("0", "-") else parse_amount(raw)
        if n is None or n < 0:
            return await inter.response.send_message("❌ Не понял сумму. Пример: `30к`, `1.5кк`, `200000`.",
                                                     ephemeral=True)
        self.bot.db.gset(inter.guild, f"recruit:rate:{kind.value}", int(n))
        text = (f"✅ Рекрутёрам {kind.name}: **{self._fmt(n)}**" if n else f"✅ {kind.name.capitalize()} — не платим.")
        await self.bot.log(inter.guild, f"⚙️ {text} ({inter.user.mention})", "hrlog")
        await inter.response.send_message(text, ephemeral=True)

    @group.command(name="выплатить", description="Выплатить рекрутёрам из казны всё, что не оплачено с прошлой выплаты")
    @staff_only(config.TREASURY)
    async def c_pay(self, inter: discord.Interaction):
        g, tr = inter.guild, self.bot.get_cog("Treasury")
        if not tr:
            return await inter.response.send_message("Казна не загружена.", ephemeral=True)
        why = tr.why_not(g, inter.user, "salary")
        if why:
            return await inter.response.send_message(why, ephemeral=True)
        rates = self.rates(g)
        if not any(rates.values()):
            return await inter.response.send_message(
                "💸 Ставки рекрутёрам не заданы. Глава/зам: `/рекрутёры ставка` (за обзвон или за принятого).",
                ephemeral=True)
        since = int(self.bot.db.gget(g, "recruit:paid_ts") or 0)
        until = int(time.time())
        owed = self.owed(self.stats(g, since, until), rates)
        pairs = [(g.get_member(uid), a) for uid, a in owed.items() if a > 0]
        gone = [uid for uid, a in owed.items() if a > 0 and not g.get_member(uid)]
        pairs = [(m, a) for m, a in pairs if m]
        if not pairs:
            return await inter.response.send_message(
                "✅ Рекрутёрам платить не за что — новых обзвонов и принятых с прошлой выплаты нет.", ephemeral=True)
        text = tr.pay_preview(g, pairs, "рекрутёрам по статистике")
        text += f"\nПериод: с {f'<t:{since}:f>' if since else 'начала учёта'} по сейчас."
        if gone:
            text += f"\n⚠️ Ушли с сервера, им не платим: {len(gone)} чел."
        await inter.response.send_message(text[:1900], view=RecruitPayView(self, inter.user.id, pairs, until),
                                          ephemeral=True)


async def setup(bot):
    await bot.add_cog(Recruit(bot))
