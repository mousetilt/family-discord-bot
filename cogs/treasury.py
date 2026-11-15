# -*- coding: utf-8 -*-
"""Казна семьи (Фаза 2, docs/СПЕЦ_КАЗНА.md). Внутриигровые вирты, НЕ реальные деньги.
• Баланс = приходы + взносы − расходы (таблица treasury_tx, всё по guild_id).
• Членские взносы: сумма и период (неделя/месяц) в guild_settings dues:amount / dues:period; кто сдал, кто должен.
  Освобождены: в отпуске и новички на испытательном.
• Вести и видеть казну — config.TREASURY (Глава, зам, совет, казначей, старший состав). Взнос настраивают Глава/зам.
• Управление: речью («Швейцар, внеси в казну 50к от Пети») и кнопками /панель → Казна. /казна — ярлык, скрыт у рядовых.
• Каждая операция — в канал казны (ch:treasury_log, иначе кадровый журнал).
Модуль «treasury» в /настройки, по умолчанию включён."""
import datetime as dt
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, safe_dm

log = logging.getLogger("family-bot.treasury")
TREASURY = config.TREASURY
DUES_SETTERS = ["leader", "deputy"]  # взнос и ставки зарплат настраивают Глава и заместитель
FINERS = TREASURY + ["hr_head"]  # штраф выписывают ещё и кадры (вместе с выговором); оплату отмечает казна
RATE_ROLES = list(config.ROSTER_ORDER)  # роли, на которые можно поставить ставку зарплаты
ROLE_CHOICES = [app_commands.Choice(name=config.ROLE_NAMES[k], value=k) for k in RATE_ROLES][:25]
MAX_AMOUNT = 10 ** 12
PERIODS = {"week": "неделю", "month": "месяц"}
_MULT = [("ккк", 10 ** 9), ("kkk", 10 ** 9), ("млрд", 10 ** 9), ("кк", 10 ** 6), ("kk", 10 ** 6), ("млн", 10 ** 6),
         ("лям", 10 ** 6), ("м", 10 ** 6), ("m", 10 ** 6), ("к", 10 ** 3), ("k", 10 ** 3), ("тыс", 10 ** 3)]


def parse_amount(s):
    """«50к» → 50000, «1.2кк» / «1,2 ляма» → 1200000, «2м» → 2000000, «1 200 000» → 1200000. Мусор / ≤0 → None."""
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        n = int(s)
        return n if 0 < n <= MAX_AMOUNT else None
    t = str(s or "").lower().replace("ё", "е")
    t = re.sub(r"(вирт\w*|руб\w*|\$|₽)", "", t)
    t = t.replace(" ", "").replace(" ", "").replace("_", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)*)([a-zа-я]*)", t)
    if not m:
        return None
    num, suf = m.groups()
    mult = 1
    if suf:
        for name, k in _MULT:
            if suf.startswith(name):
                mult = k
                break
        else:
            return None
        try:
            val = float(num.replace(",", "."))
        except ValueError:
            return None
    else:  # без суффикса точки/запятые — разделители разрядов
        val = float(num.replace(",", "").replace(".", ""))
    n = int(round(val * mult))
    return n if 0 < n <= MAX_AMOUNT else None


def fmt(n):
    return f"{int(n):,}".replace(",", " ") + " вирт"


def msk_now():
    return now().astimezone(dt.timezone(dt.timedelta(hours=config.GATHER_UTC_OFFSET)))


def period_start(period):
    """Начало текущего периода взносов (по Москве) → unix-время."""
    t = msk_now().replace(hour=0, minute=0, second=0, microsecond=0)
    t = t.replace(day=1) if period == "month" else t - dt.timedelta(days=t.weekday())
    return int(t.timestamp())


def clean(s, n=200):
    return " ".join(str(s or "").split())[:n]


class OverdraftView(discord.ui.View):
    """Расход больше баланса — только после явного подтверждения."""

    def __init__(self, cog, author_id, amount, reason):
        super().__init__(timeout=180)
        self.cog, self.author_id, self.amount, self.reason = cog, author_id, amount, reason

    async def interaction_check(self, inter):
        if inter.user.id != self.author_id:
            await inter.response.send_message("Подтвердить может только тот, кто проводит расход.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Да, провести (казна уйдёт в минус)", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def yes(self, inter, _):
        self.stop()
        text = await self.cog.spend(inter.guild, inter.user, self.amount, self.reason)
        await inter.response.edit_message(content=text, view=None)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def no(self, inter, _):
        self.stop()
        await inter.response.edit_message(content="Отменено. Казна не тронута.", view=None)


class PayView(discord.ui.View):
    """Подтверждение выплаты зарплат (показывает список и сумму)."""

    def __init__(self, cog, author_id, pairs, reason, ranks=False):
        super().__init__(timeout=300)
        self.cog, self.author_id, self.pairs, self.reason, self.ranks = cog, author_id, pairs, reason, ranks

    async def interaction_check(self, inter):
        if inter.user.id != self.author_id:
            await inter.response.send_message("Подтвердить может только тот, кто проводит выплату.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Выплатить", style=discord.ButtonStyle.success, emoji="💸")
    async def yes(self, inter, _):
        self.stop()
        await inter.response.edit_message(content="💸 Выплачиваю…", view=None)
        text = await self.cog.payout(inter.guild, inter.user, self.pairs, self.reason, self.ranks)
        await inter.edit_original_response(content=text)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def no(self, inter, _):
        self.stop()
        await inter.response.edit_message(content="Отменено. Казна не тронута.", view=None)


def treasury_only(keys=None):
    async def pred(inter: discord.Interaction):
        cog = inter.client.get_cog("Treasury")
        return bool(cog) and await cog.gate(inter, None, keys or TREASURY)
    return app_commands.check(pred)


class Treasury(commands.Cog):
    group = app_commands.Group(name="казна", description="Казна семьи: баланс, приход, расход, взносы",
                               default_permissions=discord.Permissions(manage_messages=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.remind_loop.start()

    def cog_unload(self):
        self.remind_loop.cancel()

    # ---------- доступ ----------
    def allowed(self, member, keys=TREASURY):
        return isinstance(member, discord.Member) and (member.id == member.guild.owner_id or self.bot.has(member, keys))

    def why_not(self, guild, member, act=None, keys=TREASURY):
        """Причина отказа (или None)."""
        if not self.bot.feature(guild, "treasury"):
            return "💰 Казна у семьи выключена — Глава может включить: `/настройки` → «Казна»."
        if act and not self.bot.feature(guild, f"treasury:{act}"):
            return "💰 Эта часть казны выключена в `/настройки` → «Казна»."
        if not self.allowed(member, keys):
            return "⛔ Казна — не твоя забота: её ведут Глава, заместитель, совет, казначей и старший состав."
        return None

    async def gate(self, inter, act=None, keys=TREASURY):
        why = self.why_not(inter.guild, inter.user, act, keys)
        if why and not inter.response.is_done():
            await inter.response.send_message(why, ephemeral=True)
        return why is None

    # ---------- данные ----------
    def balance(self, gid):
        r = self.bot.db.one("SELECT COALESCE(SUM(CASE WHEN kind='out' THEN -amount ELSE amount END), 0) b "
                            "FROM treasury_tx WHERE guild_id=?", gid)
        return int(r["b"] or 0)

    def dues(self, guild):
        db = self.bot.db
        amount = int(db.gget(guild, "dues:amount") or 0)
        period = db.gget(guild, "dues:period") or "week"
        return amount, (period if period in PERIODS else "week")

    async def tx(self, guild, kind, amount, actor, user_id=None, reason="", quiet=False):
        reason = clean(reason, 200)
        cur = self.bot.db.q("INSERT INTO treasury_tx(guild_id, user_id, actor_id, amount, kind, reason, ts) "
                            "VALUES(?,?,?,?,?,?,?)", guild.id, user_id, actor.id, int(amount), kind, reason,
                            int(now().timestamp()))
        if quiet:  # пачку (зарплаты) пишем в журнал одной записью
            return cur.lastrowid
        sign = {"in": "➕ Приход", "out": "➖ Расход", "due": "✅ Взнос"}[kind]
        who = f" от <@{user_id}>" if user_id and kind != "out" else ""
        text = (f"{sign} **{fmt(amount)}**{who}" + (f" — {reason}" if reason else "")
                + f"\nПровёл: {actor.mention} · баланс: **{fmt(self.balance(guild.id))}**")
        await self.journal(guild, text, config.GREEN if kind != "out" else 0xFB8C00)
        log.info("Казна «%s»: %s %s (%s)", guild.name, kind, amount, actor)
        return cur.lastrowid

    async def journal(self, guild, text, color=config.GOLD):
        key = "treasury_log" if self.bot.ch(guild, "treasury_log") else "hrlog"
        await self.bot.log(guild, text, key, color)

    def debtors(self, guild):
        """[(участник, сдал)] — кто не сдал взнос за текущий период полностью. Без взноса — пусто."""
        need, period = self.dues(guild)
        if need <= 0:
            return []
        since = period_start(period)
        paid = {r["user_id"]: int(r["s"]) for r in self.bot.db.all(
            "SELECT user_id, SUM(amount) s FROM treasury_tx WHERE guild_id=? AND kind='due' AND ts>=? GROUP BY user_id",
            guild.id, since)}
        free = [r for r in (self.bot.role(guild, "vacation"), self.bot.role(guild, "newbie")) if r]
        out = []
        for m in self.bot.family_members(guild):
            if m.bot or any(r in m.roles for r in free):
                continue
            got = paid.get(m.id, 0)
            if got < need:
                out.append((m, got))
        return out

    # ---------- действия (общие для команд, кнопок и речи) ----------
    async def deposit(self, guild, actor, amount, reason, from_member=None):
        await self.tx(guild, "in", amount, actor, from_member.id if from_member else None, reason)
        return (f"➕ В казну **{fmt(amount)}**" + (f" от {from_member.display_name}" if from_member else "")
                + (f" — {clean(reason)}" if reason else "") + f". Баланс: **{fmt(self.balance(guild.id))}**.")

    async def spend(self, guild, actor, amount, reason):
        await self.tx(guild, "out", amount, actor, None, reason)
        bal = self.balance(guild.id)
        return (f"➖ Из казны **{fmt(amount)}**" + (f" — {clean(reason)}" if reason else "")
                + f". Баланс: **{fmt(bal)}**" + (" ⚠️ казна в минусе!" if bal < 0 else "."))

    async def take_due(self, guild, actor, member, amount=None):
        need, period = self.dues(guild)
        amount = amount or need
        if not amount:
            return "❌ Сумма взноса не настроена — укажи сумму или настрой взнос (Глава/зам)."
        await self.tx(guild, "due", amount, actor, member.id, f"членский взнос за {PERIODS[period]}")
        got = sum(int(r["amount"]) for r in self.bot.db.all(
            "SELECT amount FROM treasury_tx WHERE guild_id=? AND kind='due' AND user_id=? AND ts>=?",
            guild.id, member.id, period_start(period)))
        tail = (f" Сдано за {PERIODS[period]}: {fmt(got)} из {fmt(need)}." if need and got < need
                else f" Взнос за {PERIODS[period]} закрыт ✅." if need else "")
        return f"✅ Взнос {member.display_name}: **{fmt(amount)}**.{tail}"

    def set_dues(self, guild, amount, period):
        self.bot.db.gset(guild, "dues:amount", int(amount))
        self.bot.db.gset(guild, "dues:period", period)
        self.bot.db.gset(guild, "dues:since", int(now().timestamp()))
        return f"⚙️ Членский взнос: **{fmt(amount)}** в {PERIODS[period]}. Прошлые периоды не меняются."

    def balance_text(self, guild):
        need, period = self.dues(guild)
        text = f"💰 В казне: **{fmt(self.balance(guild.id))}**"
        if need and self.bot.feature(guild, "treasury:dues"):
            text += f"\nВзнос: {fmt(need)} в {PERIODS[period]} · должников: **{len(self.debtors(guild))}**"
        return text

    def debtors_text(self, guild):
        need, period = self.dues(guild)
        if not need:
            return "Членский взнос не настроен. Глава или заместитель: «Швейцар, взнос 100к в неделю» или кнопка панели."
        rows = self.debtors(guild)
        if not rows:
            return f"✅ Взнос за {PERIODS[period]} ({fmt(need)}) сдали все. Красавцы."
        lines = [f"📋 Не сдали взнос за {PERIODS[period]} ({fmt(need)}) — {len(rows)} чел.:"]
        for m, got in rows[:40]:
            lines.append(f"• {m.mention}" + (f" — сдал {fmt(got)} из {fmt(need)}" if got else ""))
        if len(rows) > 40:
            lines.append(f"…и ещё {len(rows) - 40}")
        return "\n".join(lines)

    def history_text(self, guild, limit=15):
        rows = self.bot.db.all("SELECT * FROM treasury_tx WHERE guild_id=? ORDER BY id DESC LIMIT ?", guild.id, limit)
        if not rows:
            return "🧾 Операций пока не было."
        icon = {"in": "➕", "out": "➖", "due": "✅"}
        lines = ["🧾 Последние операции:"]
        for r in rows:
            who = f" · <@{r['user_id']}>" if r["user_id"] else ""
            lines.append(f"{icon.get(r['kind'], '•')} <t:{r['ts']}:d> **{fmt(r['amount'])}**{who}"
                         f"{' — ' + r['reason'] if r['reason'] else ''} (провёл <@{r['actor_id']}>)")
        return "\n".join(lines)[:1900]

    def report_text(self, guild, period="month"):
        since = period_start(period) if period in PERIODS else 0
        db, gid = self.bot.db, guild.id
        sums = {r["kind"]: int(r["s"]) for r in db.all(
            "SELECT kind, SUM(amount) s FROM treasury_tx WHERE guild_id=? AND ts>=? GROUP BY kind", gid, since)}
        top = db.all("SELECT user_id, SUM(amount) s FROM treasury_tx WHERE guild_id=? AND ts>=? AND kind IN ('in','due') "
                     "AND user_id IS NOT NULL GROUP BY user_id ORDER BY s DESC LIMIT 5", gid, since)
        name = {"week": "эту неделю", "month": "этот месяц"}.get(period, "всё время")
        lines = [f"📊 Казна за {name}",
                 f"Баланс сейчас: **{fmt(self.balance(gid))}**",
                 f"➕ Приход: {fmt(sums.get('in', 0))} · ✅ Взносы: {fmt(sums.get('due', 0))} · "
                 f"➖ Расход: {fmt(sums.get('out', 0))}"]
        if top:
            lines.append("🏅 Больше всех внесли: " + ", ".join(f"<@{r['user_id']}> ({fmt(r['s'])})" for r in top))
        if self.dues(guild)[0] and self.bot.feature(guild, "treasury:dues"):
            lines.append(f"📋 Должников по взносу сейчас: {len(self.debtors(guild))}")
        return "\n".join(lines)

    def digest_line(self, guild):
        """Строка для сводки (или None, если казна выключена)."""
        if not self.bot.feature(guild, "treasury"):
            return None
        line = f"💰 Казна: {fmt(self.balance(guild.id))}"
        if self.dues(guild)[0] and self.bot.feature(guild, "treasury:dues"):
            line += f" · должников по взносу: {len(self.debtors(guild))}"
        if self.bot.feature(guild, "treasury:fines"):
            rows = self.unpaid(guild)
            if rows:
                line += (f"\n🧾 Неоплаченных штрафов: {len({r['user_id'] for r in rows})} чел. на "
                         f"{fmt(sum(int(r['amount']) for r in rows))}")
        return line

    # ---------- зарплаты ----------
    def has_role(self, member, key):
        """Реально ли у человека роль key (без «админ видит всё», как в bot.has)."""
        ids = self.bot.role_ids(member.guild, [key])
        return any(r.id in ids for r in member.roles)

    def rates(self, guild):
        out = {}
        for k in RATE_ROLES:
            try:
                v = int(self.bot.db.gget(guild, f"salary:{k}") or 0)
            except ValueError:
                v = 0
            if v > 0:
                out[k] = v
        return out

    def set_rate(self, guild, key, amount):
        if amount:
            self.bot.db.gset(guild, f"salary:{key}", int(amount))
            return f"💸 Ставка «{config.ROLE_NAMES[key]}»: **{fmt(amount)}**."
        self.bot.db.gdel(guild, f"salary:{key}")
        return f"💸 Ставка «{config.ROLE_NAMES[key]}» убрана."

    def payroll(self, guild):
        """Зарплата по ставкам: каждому — наибольшая ставка из его ролей. Отпускники не получают."""
        rates, vac = self.rates(guild), self.bot.role(guild, "vacation")
        out = []
        for m in self.bot.family_members(guild):
            if vac and vac in m.roles:
                continue
            best = max((v for k, v in rates.items() if self.has_role(m, k)), default=0)
            if best:
                out.append((m, best))
        return out

    def role_members(self, guild, key):
        vac = self.bot.role(guild, "vacation")
        return [m for m in self.bot.family_members(guild) if self.has_role(m, key) and not (vac and vac in m.roles)]

    def rates_text(self, guild):
        rates = self.rates(guild)
        if not rates:
            return ("💸 Ставки не заданы. Глава/зам: кнопка «Ставка» или «Швейцар, ставка рекрутёрам 30к». "
                    "Каждый получает наибольшую ставку из своих ролей.")
        pay = self.payroll(guild)
        lines = ["💸 Ставки (каждому — наибольшая из его ролей):"]
        lines += [f"• {config.ROLE_NAMES[k]} — {fmt(v)}" for k, v in rates.items()]
        lines.append(f"Выплата по ставкам сейчас: {len(pay)} чел. на **{fmt(sum(a for _, a in pay))}** · "
                     f"в казне {fmt(self.balance(guild.id))}")
        last = self.bot.db.gget(guild, "salary:last")
        if last:
            lines.append(f"Последняя выплата по ставкам: <t:{last}:R>")
        return "\n".join(lines)

    def pay_preview(self, guild, pairs, reason):
        total = sum(a for _, a in pairs)
        bal = self.balance(guild.id)
        names = ", ".join(f"{m.display_name} ({fmt(a)})" for m, a in pairs[:20]) + ("…" if len(pairs) > 20 else "")
        text = f"💸 Выплата «{clean(reason, 80)}»: {len(pairs)} чел. на **{fmt(total)}**\n{names}"
        if total > bal:
            text += f"\n⚠️ В казне только {fmt(bal)} — казна уйдёт в минус."
        return text

    async def payout(self, guild, actor, pairs, reason, ranks=False):
        """Выплаты из казны: каждому — расход с пометкой «зарплата», уведомление в ЛС, одна запись в журнал."""
        reason = clean(reason, 120) or "зарплата"
        pairs = [(m, int(a)) for m, a in pairs if m and a and a > 0]
        if not pairs:
            return "❌ Некому платить: никого не нашёл или ставки не заданы."
        for m, a in pairs:
            await self.tx(guild, "out", a, actor, m.id, f"зарплата: {reason}", quiet=True)
        total = sum(a for _, a in pairs)
        if ranks:
            self.bot.db.gset(guild, "salary:last", int(now().timestamp()))
        fam = self.bot.family(guild)["name"]
        for m, a in pairs:
            await safe_dm(m, f"💸 Тебе выплата из казны семьи {fam}: **{fmt(a)}** — {reason}.")
        bal = self.balance(guild.id)
        await self.journal(guild, f"💸 Выплата «{reason}»: {len(pairs)} чел. на **{fmt(total)}**\n"
                                  + ", ".join(f"{m.mention} {fmt(a)}" for m, a in pairs[:30])
                                  + f"\nПровёл: {actor.mention} · баланс: **{fmt(bal)}**", 0xFB8C00)
        log.info("Казна «%s»: выплата %s × %d = %s (%s)", guild.name, reason, len(pairs), total, actor)
        return (f"💸 Выплачено {len(pairs)} чел. на **{fmt(total)}** — {reason}. Баланс: **{fmt(bal)}**"
                + (" ⚠️ казна в минусе!" if bal < 0 else "."))

    # ---------- штрафы ----------
    def unpaid(self, guild, user_id=None):
        if user_id:
            return self.bot.db.all("SELECT * FROM fines WHERE guild_id=? AND user_id=? AND paid=0 ORDER BY id",
                                   guild.id, user_id)
        return self.bot.db.all("SELECT * FROM fines WHERE guild_id=? AND paid=0 ORDER BY user_id, id", guild.id)

    async def fine(self, guild, actor, member, amount, reason):
        reason = clean(reason, 200) or "без причины"
        cur = self.bot.db.q("INSERT INTO fines(guild_id, user_id, actor_id, amount, reason, ts) VALUES(?,?,?,?,?,?)",
                            guild.id, member.id, actor.id, int(amount), reason, int(now().timestamp()))
        fid = cur.lastrowid
        total = sum(int(r["amount"]) for r in self.unpaid(guild, member.id))
        await safe_dm(member, f"💸 Тебе штраф в семье {self.bot.family(guild)['name']}: **{fmt(amount)}** — {reason} "
                              f"(№{fid}).\nВсего неоплачено: {fmt(total)}. Сдай казначею или руководству.")
        await self.journal(guild, f"🧾 Штраф №{fid}: {member.mention} — **{fmt(amount)}** · {reason}\n"
                                  f"Выписал: {actor.mention} · всего на нём: {fmt(total)}", 0xE53935)
        log.info("Казна «%s»: штраф %s %s (%s)", guild.name, member, amount, actor)
        return f"🧾 Штраф №{fid} {member.display_name}: **{fmt(amount)}** — {reason}. Неоплачено у него: {fmt(total)}."

    async def fine_paid(self, guild, actor, member, fid=None):
        rows = self.unpaid(guild, member.id)
        if fid:
            rows = [r for r in rows if r["id"] == int(fid)]
        if not rows:
            return f"У {member.display_name} нет неоплаченных штрафов" + (f" с №{fid}" if fid else "") + "."
        ts = int(now().timestamp())
        for r in rows:
            self.bot.db.q("UPDATE fines SET paid=1, paid_ts=? WHERE id=?", ts, r["id"])
            await self.tx(guild, "in", r["amount"], actor, member.id, f"штраф №{r['id']}: {r['reason']}", quiet=True)
        total = sum(int(r["amount"]) for r in rows)
        nums = ", ".join(f"№{r['id']}" for r in rows)
        await self.journal(guild, f"✅ Оплачены штрафы {member.mention} ({nums}): **{fmt(total)}** → в казну\n"
                                  f"Отметил: {actor.mention} · баланс: **{fmt(self.balance(guild.id))}**", config.GREEN)
        return f"✅ {member.display_name} оплатил {nums} — **{fmt(total)}** ушло в казну."

    async def fine_cancel(self, guild, actor, fid):
        r = self.bot.db.one("SELECT * FROM fines WHERE guild_id=? AND id=? AND paid=0", guild.id, int(fid))
        if not r:
            return f"❌ Неоплаченного штрафа №{fid} нет."
        self.bot.db.q("UPDATE fines SET paid=-1, paid_ts=? WHERE id=?", int(now().timestamp()), r["id"])
        await self.journal(guild, f"↩️ Штраф №{r['id']} (<@{r['user_id']}>, {fmt(r['amount'])}) отменён — "
                                  f"{actor.mention}")
        return f"↩️ Штраф №{r['id']} отменён."

    def fines_text(self, guild):
        rows = self.unpaid(guild)
        if not rows:
            return "✅ Неоплаченных штрафов нет."
        per = {}
        for r in rows:
            per.setdefault(r["user_id"], []).append(r)
        total = sum(int(r["amount"]) for r in rows)
        lines = [f"🧾 Неоплаченные штрафы: {len(per)} чел. на **{fmt(total)}**"]
        for uid, rs in list(per.items())[:25]:
            lines.append(f"• <@{uid}> — {fmt(sum(int(r['amount']) for r in rs))}: "
                         + "; ".join(f"№{r['id']} {fmt(r['amount'])} {r['reason'][:40]}" for r in rs[:4]))
        return "\n".join(lines)[:1900]

    @tasks.loop(minutes=30)
    @guarded
    async def remind_loop(self):
        """Раз в день (после 12:00 МСК) — ЛС тем, у кого неоплаченные штрафы."""
        t = msk_now()
        if t.hour < 12:
            return
        day = t.strftime("%Y-%m-%d")
        for g in self.bot.home_guilds:
            if not self.bot.feature(g, "treasury:fines") or not self.bot.feature(g, "treasury:remind"):
                continue
            if self.bot.db.gget(g, "fines:reminded") == day:
                continue
            self.bot.db.gset(g, "fines:reminded", day)
            per = {}
            for r in self.unpaid(g):
                per.setdefault(r["user_id"], []).append(r)
            fam = self.bot.family(g)["name"]
            for uid, rs in per.items():
                m = g.get_member(uid)
                if not m:
                    continue
                await safe_dm(m, f"⏰ Напоминаю: на тебе неоплаченные штрафы в семье {fam} — "
                                 f"**{fmt(sum(int(r['amount']) for r in rs))}** ("
                                 + "; ".join(f"№{r['id']} {r['reason'][:60]}" for r in rs[:5])
                                 + "). Сдай казначею или руководству.")

    @remind_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- речь (вызывает assistant.py) ----------
    def describe(self, guild, a, who=None):
        """Строка для окна «Подтверди»."""
        t = a.get("type")
        amount = parse_amount(a.get("amount"))
        s = fmt(amount) if amount else "❓сумма"
        if t == "treasury_in":
            return f"➕ В казну {s}" + (f" от {who.display_name}" if who else "") + f" — {clean(a.get('reason'), 100)}"
        if t == "treasury_out":
            warn = " ⚠️ больше баланса — казна уйдёт в минус" if amount and amount > self.balance(guild.id) else ""
            return f"➖ Расход из казны {s} — {clean(a.get('reason'), 100)}{warn}"
        if t == "treasury_due":
            return f"✅ Взнос {who.display_name if who else '❓кто'}: {s if amount else 'сумма взноса по умолчанию'}"
        if t == "dues_set":
            return f"⚙️ Взнос {s} в {PERIODS.get(a.get('period'), 'неделю')}"
        if t == "fine_add":
            return f"🧾 Штраф {who.display_name if who else '❓кому'}: {s} — {clean(a.get('reason'), 100)}"
        if t == "fine_paid":
            return f"✅ {who.display_name if who else '❓кто'} оплатил штраф" + (f" №{a['id']}" if a.get("id") else "ы")
        if t == "salary_rate_set":
            key = a.get("role")
            return f"💸 Ставка «{config.ROLE_NAMES.get(key, '❓роль')}»: {s if amount else 'убрать'}"
        if t == "salary_ranks":
            return self.pay_preview(guild, self.payroll(guild), "по ставкам")
        if t == "salary_role":
            key = a.get("role")
            if key not in RATE_ROLES or not amount:
                return f"💸 Выплата роли — {config.ROLE_NAMES.get(key, '❓роль')} по {s}"
            return self.pay_preview(guild, [(m, amount) for m in self.role_members(guild, key)],
                                    a.get("reason") or config.ROLE_NAMES[key])
        if t == "salary_pay":
            people = a.get("_members") or ([who] if who else [])
            if not people or not amount:
                return f"💸 Выплата {', '.join(m.display_name for m in people) or '❓кому'} по {s}"
            return self.pay_preview(guild, [(m, amount) for m in people], a.get("reason") or "разовая выплата")
        return t

    async def speech(self, guild, actor, a, who=None):
        """Выполняет действие казны из речи. Возвращает строку-итог."""
        t = a.get("type")
        act = {"treasury_due": "dues", "dues_set": "dues", "treasury_debtors": "dues"}.get(t)
        if t.startswith("salary_"):
            act = "salary"
        if t.startswith("fine_"):
            act = "fines"
        keys = DUES_SETTERS if t in ("dues_set", "salary_rate_set") else FINERS if t in ("fine_add", "fine_list") \
            else TREASURY
        why = self.why_not(guild, actor, act, keys)
        if why:
            return why
        if t == "fine_list":
            return self.fines_text(guild)
        if t == "fine_paid":
            if not who:
                return "❌ Не понял, кто оплатил штраф — назови ник точнее."
            fid = a.get("id")
            try:
                fid = int(fid) if fid else None
            except (TypeError, ValueError):
                fid = None
            return await self.fine_paid(guild, actor, who, fid)
        if t == "fine_add":
            amount = parse_amount(a.get("amount"))
            if not who or who.bot:
                return "❌ Не понял, кому штраф — назови ник точнее."
            if not amount:
                return "❌ Не понял сумму штрафа. Пиши как в игре: 30к, 1кк."
            return await self.fine(guild, actor, who, amount, a.get("reason") or "")
        if t == "treasury_balance":
            return self.balance_text(guild)
        if t == "treasury_debtors":
            return self.debtors_text(guild)
        if t == "salary_rates":
            return self.rates_text(guild)
        amount = parse_amount(a.get("amount"))
        if t == "salary_ranks":
            return await self.payout(guild, actor, self.payroll(guild), clean(a.get("reason"), 80) or "по ставкам",
                                     ranks=True)
        if t == "salary_rate_set":
            if a.get("role") not in RATE_ROLES:
                return "❌ Не понял, для какой роли ставка."
            return self.set_rate(guild, a["role"], amount)
        if t in ("salary_role", "salary_pay") and not amount:
            return "❌ Не понял сумму на человека. Пиши как в игре: 50к, 1.5кк."
        if t == "salary_role":
            if a.get("role") not in RATE_ROLES:
                return "❌ Не понял, какой роли платить."
            return await self.payout(guild, actor, [(m, amount) for m in self.role_members(guild, a["role"])],
                                     a.get("reason") or config.ROLE_NAMES[a["role"]])
        if t == "salary_pay":
            people = a.get("_members") or ([who] if who else [])
            if not people:
                return "❌ Не понял, кому платить — назови ник точнее."
            return await self.payout(guild, actor, [(m, amount) for m in people], a.get("reason") or "разовая выплата")
        if t == "treasury_due":
            if not who:
                return "❌ Не понял, кто сдал взнос — назови ник точнее."
            return await self.take_due(guild, actor, who, amount)
        if not amount:
            return "❌ Не понял сумму. Пиши как в игре: 50к, 1.5кк, 200000."
        if t == "treasury_in":
            return await self.deposit(guild, actor, amount, a.get("reason") or "", who)
        if t == "treasury_out":
            return await self.spend(guild, actor, amount, a.get("reason") or "")
        if t == "dues_set":
            period = a.get("period") if a.get("period") in PERIODS else "week"
            return self.set_dues(guild, amount, period)
        return "❌ Не понял, что сделать с казной."

    # ---------- команды (ярлык; в основном — кнопки панели) ----------
    async def _amount(self, inter, raw):
        n = parse_amount(raw)
        if not n:
            await inter.response.send_message("❌ Сумма непонятная. Пиши как в игре: `50к`, `1.5кк`, `200000`.",
                                              ephemeral=True)
        return n

    @group.command(name="баланс", description="Сколько в казне и сколько должников")
    @treasury_only()
    async def c_balance(self, inter: discord.Interaction):
        await inter.response.send_message(self.balance_text(inter.guild), ephemeral=True)

    @group.command(name="внести", description="Приход в казну (вирты)")
    @app_commands.rename(amount="сумма", reason="за_что", member="от_кого")
    @app_commands.describe(amount="Например 50к, 1.5кк, 200000")
    @treasury_only()
    async def c_in(self, inter: discord.Interaction, amount: str, reason: str, member: discord.Member = None):
        n = await self._amount(inter, amount)
        if n:
            await inter.response.send_message(await self.deposit(inter.guild, inter.user, n, reason, member),
                                              ephemeral=True)

    @group.command(name="расход", description="Расход из казны (вирты)")
    @app_commands.rename(amount="сумма", reason="на_что")
    @app_commands.describe(amount="Например 20к, 1кк")
    @treasury_only()
    async def c_out(self, inter: discord.Interaction, amount: str, reason: str):
        n = await self._amount(inter, amount)
        if not n:
            return
        bal = self.balance(inter.guild.id)
        if n > bal:
            return await inter.response.send_message(
                f"⚠️ В казне {fmt(bal)}, а расход {fmt(n)} — казна уйдёт в минус. Точно?",
                view=OverdraftView(self, inter.user.id, n, reason), ephemeral=True)
        await inter.response.send_message(await self.spend(inter.guild, inter.user, n, reason), ephemeral=True)

    @group.command(name="взнос", description="Отметить, что участник сдал членский взнос")
    @app_commands.rename(member="участник", amount="сумма")
    @app_commands.describe(amount="Пусто — сумма взноса по умолчанию")
    @treasury_only()
    async def c_due(self, inter: discord.Interaction, member: discord.Member, amount: str = None):
        if not await self.gate(inter, "dues"):
            return
        n = None
        if amount:
            n = await self._amount(inter, amount)
            if not n:
                return
        await inter.response.send_message(await self.take_due(inter.guild, inter.user, member, n), ephemeral=True)

    @group.command(name="должники", description="Кто не сдал членский взнос за текущий период")
    @treasury_only()
    async def c_debtors(self, inter: discord.Interaction):
        if not await self.gate(inter, "dues"):
            return
        await inter.response.send_message(self.debtors_text(inter.guild), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="история", description="Последние 15 операций казны")
    @treasury_only()
    async def c_history(self, inter: discord.Interaction):
        await inter.response.send_message(self.history_text(inter.guild), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="отчёт", description="Приход, расход, взносы и топ вкладчиков за период")
    @app_commands.rename(period="период")
    @app_commands.choices(period=[app_commands.Choice(name="Эта неделя", value="week"),
                                  app_commands.Choice(name="Этот месяц", value="month"),
                                  app_commands.Choice(name="Всё время", value="all")])
    @treasury_only()
    async def c_report(self, inter: discord.Interaction, period: app_commands.Choice[str] = None):
        await inter.response.send_message(self.report_text(inter.guild, period.value if period else "month"),
                                          ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="настроить-взнос", description="Сумма членского взноса и период (Глава/зам)")
    @app_commands.rename(amount="сумма", period="период")
    @app_commands.choices(period=[app_commands.Choice(name="В неделю", value="week"),
                                  app_commands.Choice(name="В месяц", value="month")])
    @treasury_only()
    async def c_dues(self, inter: discord.Interaction, amount: str, period: app_commands.Choice[str]):
        if not await self.gate(inter, "dues", DUES_SETTERS):
            return
        n = await self._amount(inter, amount)
        if n:
            text = self.set_dues(inter.guild, n, period.value)
            await self.journal(inter.guild, f"{text}\nНастроил: {inter.user.mention}")
            await inter.response.send_message(text, ephemeral=True)

    # ---------- штрафы ----------
    @group.command(name="штраф", description="Выписать штраф участнику (в ЛС придёт уведомление)")
    @app_commands.rename(member="участник", amount="сумма", reason="за_что")
    @treasury_only(FINERS)
    async def c_fine(self, inter: discord.Interaction, member: discord.Member, amount: str, reason: str):
        if not await self.gate(inter, "fines", FINERS):
            return
        if member.bot:
            return await inter.response.send_message("Боту штраф не выписать 🙂", ephemeral=True)
        n = await self._amount(inter, amount)
        if n:
            await inter.response.send_message(await self.fine(inter.guild, inter.user, member, n, reason),
                                              ephemeral=True)

    @group.command(name="штрафы", description="Кто не оплатил штрафы")
    @treasury_only(FINERS)
    async def c_fines(self, inter: discord.Interaction):
        if await self.gate(inter, "fines", FINERS):
            await inter.response.send_message(self.fines_text(inter.guild), ephemeral=True,
                                              allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="штраф-оплачен", description="Отметить оплату штрафа — деньги уходят в казну")
    @app_commands.rename(member="участник", fid="номер")
    @app_commands.describe(fid="Номер штрафа; пусто — все неоплаченные штрафы участника")
    @treasury_only()
    async def c_fine_paid(self, inter: discord.Interaction, member: discord.Member, fid: int = None):
        if await self.gate(inter, "fines"):
            await inter.response.send_message(await self.fine_paid(inter.guild, inter.user, member, fid),
                                              ephemeral=True)

    @group.command(name="штраф-отменить", description="Отменить ошибочный штраф по номеру")
    @app_commands.rename(fid="номер")
    @treasury_only()
    async def c_fine_cancel(self, inter: discord.Interaction, fid: int):
        if await self.gate(inter, "fines"):
            await inter.response.send_message(await self.fine_cancel(inter.guild, inter.user, fid), ephemeral=True)

    # ---------- зарплаты ----------
    @group.command(name="ставки", description="Ставки зарплат по ролям и сколько уйдёт на выплату")
    @treasury_only()
    async def c_rates(self, inter: discord.Interaction):
        if await self.gate(inter, "salary"):
            await inter.response.send_message(self.rates_text(inter.guild), ephemeral=True)

    @group.command(name="ставка", description="Ставка зарплаты для роли (Глава/зам). 0 — убрать")
    @app_commands.rename(role="роль", amount="сумма")
    @app_commands.choices(role=ROLE_CHOICES)
    @treasury_only()
    async def c_rate(self, inter: discord.Interaction, role: app_commands.Choice[str], amount: str):
        if not await self.gate(inter, "salary", DUES_SETTERS):
            return
        n = 0 if str(amount).strip() in ("0", "-") else await self._amount(inter, amount)
        if n is None:
            return
        text = self.set_rate(inter.guild, role.value, n)
        await self.journal(inter.guild, f"{text}\nНастроил: {inter.user.mention}")
        await inter.response.send_message(text, ephemeral=True)

    @group.command(name="зарплата", description="Выплатить зарплату всем по ставкам (с подтверждением)")
    @treasury_only()
    async def c_payroll(self, inter: discord.Interaction):
        if not await self.gate(inter, "salary"):
            return
        pairs = self.payroll(inter.guild)
        if not pairs:
            return await inter.response.send_message(self.rates_text(inter.guild), ephemeral=True)
        text = self.pay_preview(inter.guild, pairs, "по ставкам")
        last = self.bot.db.gget(inter.guild, "salary:last")
        if last and int(now().timestamp()) - int(last) < 6 * 86400:
            text += f"\n⚠️ По ставкам уже платили <t:{last}:R> — не задвоить бы."
        await inter.response.send_message(text[:1900], view=PayView(self, inter.user.id, pairs, "по ставкам", True),
                                          ephemeral=True)

    @group.command(name="выплатить", description="Разовая выплата участнику: за ивент, за работу…")
    @app_commands.rename(member="участник", amount="сумма", reason="за_что")
    @treasury_only()
    async def c_pay(self, inter: discord.Interaction, member: discord.Member, amount: str, reason: str):
        if not await self.gate(inter, "salary"):
            return
        n = await self._amount(inter, amount)
        if not n:
            return
        pairs = [(member, n)]
        if n > self.balance(inter.guild.id):
            return await inter.response.send_message(self.pay_preview(inter.guild, pairs, reason),
                                                     view=PayView(self, inter.user.id, pairs, reason), ephemeral=True)
        await inter.response.send_message(await self.payout(inter.guild, inter.user, pairs, reason), ephemeral=True)

    @group.command(name="выплатить-роли", description="Выплатить всем с ролью по одной сумме (организаторам, рекрутерам…)")
    @app_commands.rename(role="роль", amount="сумма", reason="за_что")
    @app_commands.choices(role=ROLE_CHOICES)
    @treasury_only()
    async def c_pay_role(self, inter: discord.Interaction, role: app_commands.Choice[str], amount: str, reason: str):
        if not await self.gate(inter, "salary"):
            return
        n = await self._amount(inter, amount)
        if not n:
            return
        pairs = [(m, n) for m in self.role_members(inter.guild, role.value)]
        if not pairs:
            return await inter.response.send_message(f"❌ С ролью «{role.name}» в семье никого нет.", ephemeral=True)
        await inter.response.send_message(self.pay_preview(inter.guild, pairs, reason)[:1900],
                                          view=PayView(self, inter.user.id, pairs, reason), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Treasury(bot))
