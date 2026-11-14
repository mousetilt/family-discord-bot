# -*- coding: utf-8 -*-
"""Команды через правый клик: по участнику (паспорт, досье, управление, выговор) и по сообщению
(пожаловаться, спросить юриста, в летопись)."""
import datetime as dt
import json

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.botcore import fmt_hours, now, now_iso, parse_dt, safe_dm

STAFF = config.LEAD + ["hr_head"]
CHIEFS = ["leader", "deputy"]


def perms(**kw):
    return discord.Permissions(**kw)


# ─────────────────────────────── модалки ───────────────────────────────
class ReasonModal(discord.ui.Modal):
    reason = discord.ui.TextInput(label="Причина", style=discord.TextStyle.paragraph, max_length=400)

    def __init__(self, title, on_done):
        super().__init__(title=title[:45])
        self.on_done = on_done

    async def on_submit(self, inter: discord.Interaction):
        await self.on_done(inter, self.reason.value)


class ReportModal(discord.ui.Modal, title="Жалоба на сообщение"):
    comment = discord.ui.TextInput(label="Что не так? (необязательно)", style=discord.TextStyle.paragraph,
                                   max_length=400, required=False)

    def __init__(self, message):
        super().__init__()
        self.message = message

    async def on_submit(self, inter: discord.Interaction):
        mod = inter.client.get_cog("Moderation")
        await inter.response.send_message("✅ Жалоба отправлена руководству. Спасибо!", ephemeral=True)
        if mod:
            await mod.flag(self.message, "Жалоба участника", "medium",
                           f"Пожаловался {inter.user.mention}: {self.comment.value or 'без комментария'}", False)


# ─────────────────────────────── меню управления ───────────────────────────────
ACTIONS = [
    # ключ, подпись, эмодзи, кто может
    ("rank:newbie", "Ранг: Новичок", "🆕", STAFF),
    ("rank:member", "Ранг: Член семьи", "👥", STAFF),
    ("rank:senior", "Ранг: Старший состав", "🛡", STAFF),
    ("post:recruiter", "Должность: Рекрутер (вкл/выкл)", "📨", config.POST_GIVERS),
    ("post:mentor", "Должность: Наставник (вкл/выкл)", "🎓", config.POST_GIVERS),
    ("post:events", "Должность: Организатор (вкл/выкл)", "🎉", config.POST_GIVERS),
    ("post:diplomat", "Должность: Дипломат (вкл/выкл)", "🕊", config.POST_GIVERS),
    ("post:hr_head", "Должность: Нач. отдела кадров (вкл/выкл)", "📋", CHIEFS),
    ("mentor:me", "Взять новичка себе в наставники", "🧑‍🏫", config.MENTORS),
    ("mute:10", "Мут 10 минут", "🔇", STAFF),
    ("mute:60", "Мут 1 час", "🔇", STAFF),
    ("unmute", "Снять мут", "🔊", STAFF),
    ("warn", "Выговор…", "⚖️", STAFF),
    ("blacklist", "В чёрный список…", "⛔", STAFF),
    ("expel", "Исключить из семьи…", "🚪", CHIEFS),
]


class ManageSelect(discord.ui.Select):
    def __init__(self, cog, target, options):
        super().__init__(placeholder="Выбери действие…", options=options, min_values=1, max_values=1)
        self.cog, self.target = cog, target

    async def callback(self, inter: discord.Interaction):
        await self.cog.do_action(inter, self.target, self.values[0])


class ContextMenus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.menus = []
        spec = [
            ("🪪 Паспорт", self.m_passport, None, discord.Member),
            ("📋 Досье", self.m_dossier, perms(move_members=True), discord.Member),
            ("🛠 Управление", self.m_manage, perms(move_members=True), discord.Member),
            ("⚖️ Выговор", self.m_warn, perms(moderate_members=True), discord.Member),
            ("🛡 Пожаловаться", self.m_report, None, discord.Message),
            ("⚖️ Спросить юриста", self.m_lawyer, None, discord.Message),
            ("📖 В летопись", self.m_chronicle, perms(create_events=True), discord.Message),
        ]
        for name, cb, dp, _ in spec:
            menu = app_commands.ContextMenu(name=name, callback=cb)
            if dp is not None:
                menu.default_permissions = dp
            self.menus.append(menu)

    async def cog_load(self):
        for m in self.menus:
            self.bot.tree.add_command(m)

    async def cog_unload(self):
        for m in self.menus:
            self.bot.tree.remove_command(m.name, type=m.type)

    # ---------- по участнику ----------
    async def m_passport(self, inter: discord.Interaction, member: discord.Member):
        act = self.bot.get_cog("Activity")
        await act.profile.callback(act, inter, member)

    async def m_dossier(self, inter: discord.Interaction, member: discord.Member):
        bot, db = self.bot, self.bot.db
        if not bot.has(inter.user, config.HR + config.MENTORS + config.DIPLO):
            return await inter.response.send_message("⛔ Досье видит только состав с должностью.", ephemeral=True)
        apps = db.all("SELECT * FROM applications WHERE user_id=? ORDER BY id DESC", member.id)
        last = apps[0] if apps else None
        a = db.one("SELECT * FROM activity WHERE user_id=?", member.id)
        warns = db.all("SELECT * FROM warns WHERE user_id=? AND active=1 ORDER BY id DESC", member.id)
        ms = db.one("SELECT * FROM mentorships WHERE newbie_id=? ORDER BY id DESC", member.id)
        vac = db.one("SELECT * FROM vacations WHERE user_id=?", member.id)
        bl = bot.blacklist_find(discord_id=member.id, static=last["static_id"] if last else None,
                                nick=last["nick"] if last else None)
        e = bot.embed(f"📋 Досье: {member.display_name}", None, config.COLOR, inter.guild)
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="Ранг", value=bot.rank_name(member), inline=True)
        e.add_field(name="Аккаунт Discord", value=f"{(now() - member.created_at).days} дн.", inline=True)
        e.add_field(name="На сервере", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "—",
                    inline=True)
        if last:
            exam = f"{last['exam_score']}/{last['exam_total']}" if last["exam_total"] else "—"
            e.add_field(name="Игровой ник · Static", value=f"{last['nick']} · `{last['static_id']}`", inline=True)
            e.add_field(name="Заявки", value=f"{len(apps)} шт., последняя №{last['id']} — {last['status']}", inline=True)
            e.add_field(name="Тест", value=exam, inline=True)
        if ms:
            done = sum(1 for x in json.loads(ms["checklist"] or "[]") if x)
            e.add_field(name="Наставничество", value=f"<@{ms['mentor_id']}> · {ms['status']} · чек-лист {done}/"
                                                    f"{len(config.MENTOR_CHECKLIST)}", inline=False)
        e.add_field(name=f"⚖️ Выговоры ({len(warns)}/3)",
                    value="\n".join(f"№{w['id']} · {w['created_at'][:10]} · {w['reason'][:60]}" for w in warns[:3]) or "нет",
                    inline=False)
        if a:
            last_seen = discord.utils.format_dt(parse_dt(a["last_seen"]), "R") if a["last_seen"] else "—"
            e.add_field(name="Активность", value=f"🎙 {fmt_hours(a['voice_seconds'])} (неделя {fmt_hours(a['week_voice'])})"
                                                 f" · 💬 {a['messages']} · был {last_seen}", inline=False)
        if vac:
            e.add_field(name="🌙 Отпуск", value=f"до {discord.utils.format_dt(parse_dt(vac['until']), 'D')}: {vac['reason']}",
                        inline=False)
        e.add_field(name="⛔ Чёрный список", value=f"**ДА** — №{bl['id']}: {bl['reason']}" if bl else "чисто", inline=False)
        await inter.response.send_message(embed=e, ephemeral=True)

    async def m_manage(self, inter: discord.Interaction, member: discord.Member):
        if member.bot:
            return await inter.response.send_message("Это бот.", ephemeral=True)
        allowed = [(k, label, emoji) for k, label, emoji, who in ACTIONS if self.bot.has(inter.user, who)]
        if member.id == inter.user.id:
            allowed = [x for x in allowed if x[0] == "mentor:me"] and []
        if not allowed:
            return await inter.response.send_message("⛔ Нет доступных действий.", ephemeral=True)
        opts = [discord.SelectOption(label=label, value=k, emoji=emoji) for k, label, emoji in allowed][:25]
        v = discord.ui.View(timeout=300)
        v.add_item(ManageSelect(self, member, opts))
        e = self.bot.embed(f"🛠 Управление: {member.display_name}", f"Сейчас: **{self.bot.rank_name(member)}**",
                           config.COLOR, inter.guild)
        await inter.response.send_message(embed=e, view=v, ephemeral=True)

    async def do_action(self, inter: discord.Interaction, target: discord.Member, key: str):
        bot, g = self.bot, inter.guild
        kind, _, arg = key.partition(":")
        rule = next((who for k, _, _, who in ACTIONS if k == key), None)
        if rule is None or not bot.has(inter.user, rule):
            return await inter.response.send_message("⛔ Нет прав.", ephemeral=True)
        if kind == "post":
            ok, why = bot.can_post(inter.user, target, arg)
            if not ok:
                return await inter.response.send_message(f"⛔ {why}", ephemeral=True)
        elif kind != "mentor" and not bot.outranks(inter.user, target):
            return await inter.response.send_message("⛔ Нельзя управлять тем, кто не ниже тебя по иерархии семьи.",
                                                     ephemeral=True)
        if kind == "rank":
            await inter.response.defer(ephemeral=True)
            await bot.set_rank(target, arg, inter.user)
            return await inter.followup.send(f"✅ {target.mention} → {config.BASE_RANKS[arg]}", ephemeral=True)
        if kind == "post":
            role = bot.role(g, arg)
            if not role:
                return await inter.response.send_message("Роль не найдена — запусти /настройка.", ephemeral=True)
            await inter.response.defer(ephemeral=True)
            added = role not in target.roles
            await (target.add_roles(role) if added else target.remove_roles(role))
            await bot.log(g, f"🎖 {target.mention} — {'назначен' if added else 'снята должность'} **{role.name}** "
                             f"({inter.user.mention})", "hrlog", 0x8E24AA)
            bot.emit(g, "post", member=target, post=arg, added=added, by=inter.user)
            return await inter.followup.send(f"✅ {role.name}: {'выдана' if added else 'снята'}", ephemeral=True)
        if kind == "mentor":
            ms = bot.get_cog("Mentorship")
            row = bot.db.one("SELECT * FROM mentorships WHERE newbie_id=? AND status='active'", target.id)
            await inter.response.defer(ephemeral=True)
            if row:
                bot.db.q("UPDATE mentorships SET mentor_id=? WHERE id=?", inter.user.id, row["id"])
                th = await ms.get_thread(g, row["thread_id"])
                if th:
                    try:
                        await th.add_user(inter.user)
                        await th.send(f"🔁 Новый наставник: {inter.user.mention}")
                    except discord.HTTPException:
                        pass
            else:
                await ms.assign(g, target, inter.user, by=inter.user)
            return await inter.followup.send(f"✅ Ты наставник {target.mention}.", ephemeral=True)
        if kind in ("mute", "unmute"):
            try:
                await target.timeout(dt.timedelta(minutes=int(arg)) if kind == "mute" else None,
                                     reason=f"Мут от {inter.user}")
            except discord.Forbidden:
                return await inter.response.send_message("❌ Discord не даёт мутить администраторов и тех, чья роль "
                                                         "выше роли бота.", ephemeral=True)
            await inter.response.send_message(f"🔇 Мут {arg} мин." if kind == "mute" else "🔊 Мут снят.", ephemeral=True)
            if kind == "mute":
                await bot.log(g, f"🔇 {target.mention} мут на {arg} мин ({inter.user.mention})")
            return
        if kind == "warn":
            async def done(i, reason):
                await i.response.defer(ephemeral=True)
                n = await bot.give_warn(g, target, i.user, reason)
                await i.followup.send(f"✅ Выговор ({n}/3).", ephemeral=True)
            return await inter.response.send_modal(ReasonModal(f"Выговор: {target.display_name}", done))
        if kind == "blacklist":
            async def done(i, reason):
                await i.response.defer(ephemeral=True)
                app = bot.db.one("SELECT static_id, nick FROM applications WHERE user_id=? ORDER BY id DESC", target.id)
                bot.db.q("INSERT INTO blacklist(discord_id, static_id, nick, reason, added_by, created_at) VALUES(?,?,?,?,?,?)",
                         target.id, app["static_id"] if app else None, app["nick"] if app else None, reason, i.user.id,
                         now_iso())
                if bot.in_family(target):
                    await bot.expel(target, i.user, f"Чёрный список: {reason}")
                c = bot.ch(g, "blacklist_ch")
                if c:
                    await c.send(embed=bot.embed("⛔ Внесён в чёрный список", f"{target.mention} — {reason}\n\nВнёс: "
                                                                            f"{i.user.mention}", 0x212121, g))
                await i.followup.send("✅ Внесён в ЧС.", ephemeral=True)
            return await inter.response.send_modal(ReasonModal(f"ЧС: {target.display_name}", done))
        if kind == "expel":
            async def done(i, reason):
                await i.response.defer(ephemeral=True)
                await bot.expel(target, i.user, reason)
                await i.followup.send("✅ Исключён.", ephemeral=True)
            return await inter.response.send_modal(ReasonModal(f"Исключить: {target.display_name}", done))

    async def m_warn(self, inter: discord.Interaction, member: discord.Member):
        if not self.bot.has(inter.user, STAFF):
            return await inter.response.send_message("⛔ Нет прав.", ephemeral=True)
        if member.bot or not self.bot.outranks(inter.user, member):
            return await inter.response.send_message("⛔ Нельзя выдать выговор тому, кто не ниже тебя по иерархии.",
                                                     ephemeral=True)

        async def done(i, reason):
            await i.response.defer(ephemeral=True)
            n = await self.bot.give_warn(i.guild, member, i.user, reason)
            await i.followup.send(f"✅ Выговор {member.mention} ({n}/3).", ephemeral=True)
        await inter.response.send_modal(ReasonModal(f"Выговор: {member.display_name}", done))

    # ---------- по сообщению ----------
    async def m_report(self, inter: discord.Interaction, message: discord.Message):
        if message.author.bot:
            return await inter.response.send_message("На бота жаловаться бесполезно 🙂", ephemeral=True)
        await inter.response.send_modal(ReportModal(message))

    async def m_lawyer(self, inter: discord.Interaction, message: discord.Message):
        law = self.bot.get_cog("Lawyer")
        text = (message.content or "").strip()
        if not text:
            return await inter.response.send_message("В сообщении нет текста.", ephemeral=True)
        left = law.cooldown_left(inter.user.id)
        if left:
            return await inter.response.send_message(f"⏳ Подожди {left} сек.", ephemeral=True)
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            answer, sources, _ = await law.answer(text, "case")
        except Exception as e:
            answer, sources = f"⚠️ {e}", []

        async def send(**kw):
            await inter.followup.send(ephemeral=True, **kw)
        await law.send_answer(send, text[:150], answer, sources, "case", inter.guild)

    async def m_chronicle(self, inter: discord.Interaction, message: discord.Message):
        if not self.bot.has(inter.user, config.LEAD + ["events"]):
            return await inter.response.send_message("⛔ Только руководство и организаторы.", ephemeral=True)
        chron = self.bot.get_cog("Chronicle")
        text = (message.content or "📷 (вложение)")[:900]
        await chron.record(inter.guild, "manual", f"✍️ {text}\n— {message.author.mention} · [оригинал]({message.jump_url})",
                           message.author.id)
        await inter.response.send_message("📖 Записано в летопись.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(ContextMenus(bot))
