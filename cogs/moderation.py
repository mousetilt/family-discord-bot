# -*- coding: utf-8 -*-
"""Модерация. Работает и без ИИ (правила: чужие инвайты, скам, продажа за реальные деньги).
С бесплатным ИИ дополнительно проверяет сообщения пачками раз в несколько минут — экономит лимит."""
import datetime as dt
import logging
import re

import discord
from discord.ext import commands, tasks

from core import config
from core.botcore import guarded, now, now_iso, safe_dm

log = logging.getLogger("family-bot.mod")

RULES = [  # (флаг в /настройки, категория, тяжесть, шаблон)
    ("invites", "Реклама другого сервера", "medium",
     re.compile(r"(discord\.gg/|discord(?:app)?\.com/invite/)\S+", re.I)),
    ("scam", "Скам / фишинг", "high",
     re.compile(r"(free\s*nitro|бесплатн\w*\s*нитро|steamcommunity\.(?!com)\w+|dlscord|disc0rd|discorcl)", re.I)),
    ("realmoney", "Сделка за реальные деньги (PermBan на проекте!)", "medium",
     re.compile(r"(прода[мю]|куплю|покупаю|продаю|обменяю)[^\n]{0,40}(вирт|валют|акк|аккаунт|кк\b|лям)[^\n]{0,60}"
                r"(руб|₽|реал|карт[уыа]|сбер|тиньк|киви|qiwi|usdt|крипт|донат\s*за)", re.I)),
]
INVITE_RX = re.compile(r"(?:discord\.gg/|discord(?:app)?\.com/invite/)([\w-]+)", re.I)
SEVERITY_COLOR = {"low": 0xFDD835, "medium": 0xFB8C00, "high": 0xE53935}


class FlagAction(discord.ui.DynamicItem[discord.ui.Button], template=r"flag:(?P<a>[a-z]+):(?P<id>\d+)"):
    LABELS = {"del": ("Удалить", "🗑", discord.ButtonStyle.danger),
              "warn": ("Удалить + выговор", "⚖️", discord.ButtonStyle.danger),
              "mute": ("Мут 30 мин", "🔇", discord.ButtonStyle.secondary),
              "ok": ("Ложная тревога", "👌", discord.ButtonStyle.success)}

    def __init__(self, a, fid):
        label, emoji, style = self.LABELS[a]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"flag:{a}:{fid}"))
        self.a, self.fid = a, fid

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["a"], int(match["id"]))

    async def callback(self, inter: discord.Interaction):
        bot = inter.client
        if not bot.has(inter.user, config.LEAD + ["hr_head"]):
            return await inter.response.send_message("⛔ Нет прав.", ephemeral=True)
        f = bot.db.one("SELECT * FROM flags WHERE id=?", self.fid)
        if not f or f["status"] != "open":
            return await inter.response.send_message("Уже обработано.", ephemeral=True)
        member = inter.guild.get_member(f["user_id"])
        if self.a in ("warn", "mute") and member and (member.id == inter.user.id or not bot.outranks(inter.user, member)):
            return await inter.response.send_message("⛔ Наказывать можно только тех, кто ниже тебя по иерархии семьи — "
                                                     "реши с Главой или заместителем.", ephemeral=True)
        if self.a in ("del", "warn"):
            ch = inter.guild.get_channel(f["channel_id"])
            if ch:
                try:
                    await (await ch.fetch_message(f["message_id"])).delete()
                except discord.HTTPException:
                    pass
        if self.a == "warn" and member:
            await bot.give_warn(inter.guild, member, inter.user, f"Нарушение в чате: {f['reason']}")
        if self.a == "mute" and member:
            try:
                await member.timeout(dt.timedelta(minutes=30), reason=f["reason"])
            except discord.HTTPException:
                pass
        result = {"del": "🗑 Удалено", "warn": "⚖️ Удалено + выговор", "mute": "🔇 Мут 30 мин",
                  "ok": "👌 Ложная тревога"}[self.a]
        bot.db.q("UPDATE flags SET status=? WHERE id=?", self.a, self.fid)
        e = inter.message.embeds[0]
        e.color = config.GREEN if self.a == "ok" else 0x757575
        e.add_field(name="Решение", value=f"{result} — {inter.user.mention}", inline=False)
        await inter.response.edit_message(embed=e, view=None)


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.buffer = {}  # guild_id -> [сообщения]: у каждой семьи своя очередь, свой ключ ИИ и свой лимит
        self.ai_loop.change_interval(seconds=max(60, config.AI_MOD_INTERVAL))
        self.ai_loop.start()

    def cog_unload(self):
        self.ai_loop.cancel()

    def skip(self, message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return True
        skip_ids = {getattr(self.bot.ch(message.guild, k), "id", 0) for k in ("lawyer", "botcmd", "modqueue")}
        return message.channel.id in skip_ids

    async def foreign_invite(self, message):
        """Есть ли в сообщении приглашение на ЧУЖОЙ сервер. Свои ссылки (наш сервер, семьи и штаб альянса) — не реклама."""
        own = {message.guild.id}
        ally = self.bot.get_cog("Alliance")
        if ally:
            own |= {r["guild_id"] for r in ally.nodes()}
        for code in INVITE_RX.findall(message.content or "")[:3]:
            try:
                inv = await self.bot.fetch_invite(code, with_counts=False)
            except discord.NotFound:
                continue  # мёртвая ссылка — не реклама
            except discord.HTTPException:
                return True
            if not inv.guild or inv.guild.id not in own:
                return True
        return False

    async def flag(self, message, category, severity, reason, by_ai):
        bot = self.bot
        cur = bot.db.q("INSERT INTO flags(guild_id, user_id, channel_id, message_id, content, category, reason, created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", message.guild.id, message.author.id, message.channel.id, message.id,
                       message.content[:1500], category, reason, now_iso())
        fid = cur.lastrowid
        auto = ""
        lead = bot.has(message.author, config.MOD_EXEMPT)
        if severity == "high" and not lead and bot.feature(message.guild, "moderation:autopunish"):
            try:
                await message.delete()
            except discord.HTTPException:
                pass  # уже удалено — мут всё равно выдаём
            try:
                await message.author.timeout(dt.timedelta(minutes=10), reason=f"Автомодерация: {category}")
                auto = "\n🤖 **Удалено автоматически, мут 10 мин** (тяжёлое нарушение)"
            except discord.HTTPException:
                pass
        if severity in ("medium", "high") and not lead:
            try:
                await message.channel.send(f"⚠️ {message.author.mention}, полегче: {reason or category}. "
                                           "Руководство получило уведомление.", delete_after=30)
            except discord.HTTPException:
                pass
            await safe_dm(message.author, f"⚠️ Предупреждение на сервере семьи {config.FAMILY}: {reason or category}.\n"
                                          f"Сообщение: «{message.content[:200]}»\nПовторные нарушения — выговор.")
        e = bot.embed(f"🛡 {'ИИ-модерация' if by_ai else 'Модерация'} · {category}",
                      f"**Автор:** {message.author.mention}\n**Канал:** {message.channel.mention} · "
                      f"[перейти]({message.jump_url})\n**Тяжесть:** {severity}\n**Почему:** {reason}{auto}"
                      f"{chr(10) + '🎖 Старший состав — без автонаказания, решение за руководством' if lead else ''}\n\n"
                      f">>> {message.content[:1500]}", SEVERITY_COLOR.get(severity, 0xFB8C00), message.guild)
        e.timestamp = now()
        v = discord.ui.View(timeout=None)
        for a in ("del", "warn", "mute", "ok"):
            if auto and a == "del":
                continue
            v.add_item(FlagAction(a, fid))
        q = bot.ch(message.guild, "modqueue")
        if q:
            try:
                await q.send(embed=e, view=v)
            except discord.HTTPException as err:
                log.warning("Очередь модерации: %s", err)

    @commands.Cog.listener()
    async def on_message(self, message):
        if self.skip(message):
            return
        lead = self.bot.has(message.author, config.MOD_EXEMPT)
        for key, category, severity, rx in RULES:
            if not lead and self.bot.feature(message.guild, f"moderation:{key}") and rx.search(message.content or ""):
                if category.startswith("Реклама") and not await self.foreign_invite(message):
                    continue
                await self.flag(message, category, severity, "Сработало правило автомодерации", False)
                if "реальные деньги" in category:
                    await safe_dm(message.author, "⚠️ Осторожно: сделки за реальные деньги на проекте «Россия Онлайн» "
                                                  "караются PermBan (п. 6.21 правил). Сообщение передано руководству семьи.")
                return
        if config.AI_MODERATION and len(message.content or "") >= 6 and self.bot.feature(message.guild, "moderation:ai") \
                and self.bot.ai_for(message.guild).on:
            buf = self.buffer.setdefault(message.guild.id, [])
            buf.append(message)
            if len(buf) > 200:
                del buf[:-200]

    @tasks.loop(seconds=180)
    @guarded
    async def ai_loop(self):
        # каждой семье — своя пачка за проход (раньше одна семья за раз, остальные ждали и теряли сообщения)
        for gid in list(self.buffer):
            batch, self.buffer[gid] = self.buffer[gid][:40], self.buffer[gid][40:]
            if not self.buffer[gid]:
                self.buffer.pop(gid, None)
            if batch:
                try:
                    await self.check_batch(batch[0].guild, batch)
                except Exception:
                    log.exception("ИИ-модерация «%s»", batch[0].guild.name)

    async def check_batch(self, g, batch):
        ai = self.bot.ai_for(g)
        if not ai.on:
            return
        lines = [f"[{i}] #{m.channel.name} | {m.author.display_name}: {m.content[:400]}" for i, m in enumerate(batch)]
        system = self.bot.brand(g, f"Ты модератор Discord-сервера RP-семьи «{config.FAMILY}» (игра «Россия Онлайн»). Ниже пронумерованные "
                  "сообщения. Мат в дружеском тоне, шутки, сленг и RP-отыгрыш — НЕ нарушение. Нарушения: прямые "
                  "оскорбления и травля участника, оскорбление родных, дискриминация (нация, религия), угрозы в "
                  "реальной жизни, реклама других серверов/семей, слив чужих личных данных, NSFW, скам, призывы к "
                  "читам или сделкам за реальные деньги. Ссылка-приглашение на этот же сервер или в альянс — не реклама. "
                  'Верни {"violations":[{"i":номер,"severity":"low"|"medium"|"high","category":"кратко",'
                  '"reason":"1 предложение"}]}; если нарушений нет — {"violations":[]}.')
        try:
            data = await ai.ask_json(system, "\n".join(lines), max_tokens=700, light=True)
        except Exception as e:
            log.warning("ИИ-модерация: %s", e)
            return
        for v in (data or {}).get("violations", []) if isinstance(data, dict) else []:
            try:
                m = batch[int(v.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            await self.flag(m, str(v.get("category", "нарушение"))[:60], str(v.get("severity", "low")),
                            str(v.get("reason", ""))[:300], True)

    @ai_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    bot.add_dynamic_items(FlagAction)
    await bot.add_cog(Moderation(bot))
