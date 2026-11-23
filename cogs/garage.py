# -*- coding: utf-8 -*-
"""Автопарк семьи (Фаза 5). Модуль «garage» в /настройки.
• Таблицы vehicles (машина: модель, номер, у кого, заметка, статус ok/repair/gone) и vehicle_log (история).
  user_id NULL — машина общая (в гараже семьи).
• Ведут GARAGE (руководство + старший состав + казначей), смотрят все в семье.
• /автопарк список|добавить|передать|статус|списать|история — машину выбирают из подсказок (автодополнение).
• Канал «🚗・автопарк» (досоздаётся сам) — закреплённая панель с живым списком, туда же пишется журнал.
• Речь: car_add / car_give / car_status / car_remove (с подтверждением), car_list (справка)."""
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.botcore import answer_after, staff_only

log = logging.getLogger("family-bot.garage")
GARAGE = config.LEAD + ["senior", "treasurer"]
STATUS = {"ok": "🟢 в строю", "repair": "🔧 в ремонте", "gone": "⚫ списана"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER, model TEXT NOT NULL,
    plate TEXT, note TEXT, status TEXT DEFAULT 'ok', ts INTEGER, updated INTEGER);
CREATE INDEX IF NOT EXISTS ix_vehicles_g ON vehicles(guild_id, status);
CREATE TABLE IF NOT EXISTS vehicle_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, vehicle_id INTEGER, actor_id INTEGER,
    action TEXT, details TEXT, ts INTEGER);
"""


LATIN_TWINS = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")  # на номерах РФ эти буквы выглядят одинаково


def norm_plate(p):
    return "".join(str(p or "").upper().split()).translate(LATIN_TWINS)[:12] or None


def label(v):
    return f"№{v['id']} {v['model']}" + (f" [{v['plate']}]" if v["plate"] else "")


class Garage(commands.Cog):
    group = app_commands.Group(name="автопарк", description="Машины семьи: у кого какая, история", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.db.c.executescript(SCHEMA)
        bot.db.c.commit()

    # ---------- данные ----------
    def cars(self, guild, user_id=None, all_=False):
        q = "SELECT * FROM vehicles WHERE guild_id=?" + ("" if all_ else " AND status<>'gone'")
        args = [guild.id]
        if user_id is not None:
            q += " AND user_id=?"
            args.append(user_id)
        return self.bot.db.all(q + " ORDER BY model, id", *args)

    def get(self, guild, vid):
        return self.bot.db.one("SELECT * FROM vehicles WHERE guild_id=? AND id=?", guild.id, vid)

    def find(self, guild, text):
        """Машина по номеру, «№id» или модели (если такая одна)."""
        t = str(text or "").strip()
        if not t:
            return None
        digits = t.lstrip("№#").strip()
        if digits.isdigit():
            v = self.get(guild, int(digits))
            if v:
                return v
        p = norm_plate(t)
        rows = self.cars(guild)
        for v in rows:
            if v["plate"] and v["plate"] == p:
                return v
        same = [v for v in rows if t.lower() in v["model"].lower()]
        return same[0] if len(same) == 1 else None

    def owner(self, guild, v):
        if not v["user_id"]:
            return "🏠 общая"
        m = guild.get_member(v["user_id"])
        return m.mention if m else f"<@{v['user_id']}>"

    def line(self, guild, v):
        st = "" if v["status"] == "ok" else f" · {STATUS.get(v['status'], v['status'])}"
        return f"`№{v['id']}` **{v['model']}**" + (f" `{v['plate']}`" if v["plate"] else "") + \
            f" — {self.owner(guild, v)}{st}" + (f" · _{v['note'][:60]}_" if v["note"] else "")

    def summary(self, guild, limit=25):
        rows = self.cars(guild)
        if not rows:
            return "🚗 В автопарке пусто. Добавить: кнопка «Добавить машину» или «Швейцар, запиши Elegy у Пети, номер А123»."
        out = [f"🚗 Машин в автопарке: **{len(rows)}** (в ремонте: {sum(v['status'] == 'repair' for v in rows)})"]
        out += [self.line(guild, v) for v in rows[:limit]]
        if len(rows) > limit:
            out.append(f"…и ещё {len(rows) - limit} — `/автопарк список`")
        return "\n".join(out)

    async def journal(self, guild, actor, vid, action, details):
        self.bot.db.q("INSERT INTO vehicle_log(guild_id, vehicle_id, actor_id, action, details, ts) VALUES(?,?,?,?,?,?)",
                      guild.id, vid, getattr(actor, "id", None), action, details[:300], int(time.time()))
        key = "garage" if self.bot.ch(guild, "garage") else "hrlog"
        await self.bot.log(guild, f"🚗 {details} — {getattr(actor, 'mention', 'бот')}", key, 0x546E7A)
        hub = self.bot.get_cog("Hub")
        if hub:
            await hub.refresh_channel(guild, "garage")

    # ---------- действия (общие для кнопок, команд и речи) ----------
    async def add(self, guild, actor, model, plate=None, member=None, note=None):
        model = " ".join(str(model or "").split())[:60]
        if not model:
            return "❌ Какая машина? Укажи модель."
        plate = norm_plate(plate)
        if plate and any(v["plate"] == plate for v in self.cars(guild)):
            return f"❌ Машина с номером `{plate}` уже есть в автопарке."
        now_ = int(time.time())
        vid = self.bot.db.q("INSERT INTO vehicles(guild_id, user_id, model, plate, note, ts, updated) VALUES(?,?,?,?,?,?,?)",
                            guild.id, member.id if member else None, model, plate, (note or "")[:200] or None, now_,
                            now_).lastrowid
        v = self.get(guild, vid)
        await self.journal(guild, actor, vid, "add", f"Добавлена {label(v)} — {self.owner(guild, v)}")
        return f"✅ В автопарке: {self.line(guild, v)}"

    async def give(self, guild, actor, v, member=None):
        if not v or v["status"] == "gone":
            return "❌ Не нашёл такую машину."
        self.bot.db.q("UPDATE vehicles SET user_id=?, updated=? WHERE id=?", member.id if member else None,
                      int(time.time()), v["id"])
        was = self.owner(guild, v)
        v = self.get(guild, v["id"])
        await self.journal(guild, actor, v["id"], "give", f"{label(v)}: {was} → {self.owner(guild, v)}")
        return f"🔁 {label(v)} теперь — {self.owner(guild, v)}"

    async def set_status(self, guild, actor, v, status, note=None, cost=None):
        """cost — сколько стоил ремонт («50к»): если указан и у автора есть доступ к казне — расход из казны."""
        if not v or v["status"] == "gone":
            return "❌ Не нашёл такую машину."
        if status not in STATUS:
            return "❌ Статус: в строю, в ремонте или списана."
        self.bot.db.q("UPDATE vehicles SET status=?, note=COALESCE(?, note), updated=? WHERE id=?", status,
                      (note or "")[:200] or None, int(time.time()), v["id"])
        await self.journal(guild, actor, v["id"], status, f"{label(v)}: {STATUS[status]}" + (f" ({note})" if note else "")
                           + (f" · ремонт {cost}" if cost else ""))
        text = f"{STATUS[status].split()[0]} {label(v)} — {STATUS[status]}"
        if cost:
            text += "\n" + await self.repair_cost(guild, actor, v, cost)
        return text

    async def repair_cost(self, guild, actor, v, cost):
        """Ремонт машины → расход из казны с пометкой машины (только тем, кто ведёт казну)."""
        from core.parse import parse_amount
        tr = self.bot.get_cog("Treasury")
        amount = parse_amount(cost)
        if not amount:
            return f"⚠️ Сумму ремонта «{cost}» не понял — в казну не записал."
        if not tr or not self.bot.feature(guild, "treasury"):
            return "💰 Казна выключена — расход на ремонт не записан."
        if not tr.allowed(actor):
            return "💰 Расход на ремонт в казну проводит казначей или руководство — передай им сумму."
        return await tr.spend(guild, actor, amount, f"ремонт: {label(v)}")

    # ---------- автодополнение ----------
    async def car_auto(self, inter: discord.Interaction, current: str):
        cur = (current or "").lower()
        out = []
        for v in self.cars(inter.guild):
            name = f"{label(v)} — " + ("общая" if not v["user_id"] else
                                       getattr(inter.guild.get_member(v["user_id"]), "display_name", "?"))
            if cur in name.lower():
                out.append(app_commands.Choice(name=name[:100], value=str(v["id"])))
            if len(out) >= 25:
                break
        return out

    async def _car(self, inter, value):
        v = self.find(inter.guild, value)
        if not v:
            await inter.response.send_message("❌ Не нашёл такую машину — выбери из подсказок.", ephemeral=True)
        return v

    async def gate(self, inter):
        if self.bot.feature(inter.guild, "garage"):
            return True
        await inter.response.send_message("⚫ Автопарк у семьи выключен — `/настройки` → «Автопарк».", ephemeral=True)
        return False

    # ---------- команды ----------
    @group.command(name="список", description="Машины семьи (или конкретного участника)")
    @app_commands.rename(member="участник")
    async def c_list(self, inter: discord.Interaction, member: discord.Member = None):
        if not await self.gate(inter):
            return
        if not self.bot.has(inter.user, config.FAMILY_PLUS):
            return await inter.response.send_message("Это для семьи.", ephemeral=True)
        if member:
            rows = self.cars(inter.guild, member.id)
            text = (f"🚗 У {member.mention}: " + (", ".join(label(v) for v in rows) if rows else "машин семьи нет"))
        else:
            text = self.summary(inter.guild, 40)
        await inter.response.send_message(text[:1990], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="добавить", description="Записать машину в автопарк")
    @app_commands.rename(model="модель", plate="номер", member="у_кого", note="заметка")
    @app_commands.describe(member="У кого машина (пусто — общая, в гараже семьи)")
    @staff_only(GARAGE)
    async def c_add(self, inter: discord.Interaction, model: str, plate: str = None, member: discord.Member = None,
                    note: str = None):
        if not await self.gate(inter):
            return
        await answer_after(inter, self.add(inter.guild, inter.user, model, plate, member, note),
                                          ephemeral=True)

    @group.command(name="передать", description="Передать машину участнику (или вернуть в общий гараж)")
    @app_commands.rename(car="машина", member="кому")
    @app_commands.describe(member="Кому (пусто — вернуть в общий гараж)")
    @app_commands.autocomplete(car=car_auto)
    @staff_only(GARAGE)
    async def c_give(self, inter: discord.Interaction, car: str, member: discord.Member = None):
        if not await self.gate(inter):
            return
        v = await self._car(inter, car)
        if v:
            await answer_after(inter, self.give(inter.guild, inter.user, v, member), ephemeral=True)

    @group.command(name="статус", description="Машина в строю или в ремонте (можно сразу списать ремонт из казны)")
    @app_commands.rename(car="машина", status="статус", note="заметка", cost="ремонт_стоил")
    @app_commands.describe(cost="Сумма ремонта, например 50к — запишется расходом казны")
    @app_commands.choices(status=[app_commands.Choice(name="в строю", value="ok"),
                                  app_commands.Choice(name="в ремонте", value="repair")])
    @app_commands.autocomplete(car=car_auto)
    @staff_only(GARAGE)
    async def c_status(self, inter: discord.Interaction, car: str, status: app_commands.Choice[str], note: str = None,
                       cost: str = None):
        if not await self.gate(inter):
            return
        v = await self._car(inter, car)
        if v:
            await answer_after(inter, self.set_status(inter.guild, inter.user, v, status.value, note, cost),
                                              ephemeral=True)

    @group.command(name="списать", description="Убрать машину из автопарка (продана, разбита…)")
    @app_commands.rename(car="машина", reason="причина")
    @app_commands.autocomplete(car=car_auto)
    @staff_only(GARAGE)
    async def c_remove(self, inter: discord.Interaction, car: str, reason: str = None):
        if not await self.gate(inter):
            return
        v = await self._car(inter, car)
        if v:
            await answer_after(inter, self.set_status(inter.guild, inter.user, v, "gone", reason),
                                              ephemeral=True)

    @group.command(name="история", description="Кто и когда ездил на машине")
    @app_commands.rename(car="машина")
    @app_commands.autocomplete(car=car_auto)
    async def c_hist(self, inter: discord.Interaction, car: str):
        if not await self.gate(inter):
            return
        if not self.bot.has(inter.user, config.FAMILY_PLUS):
            return await inter.response.send_message("Это для семьи.", ephemeral=True)
        v = await self._car(inter, car)
        if not v:
            return
        rows = self.bot.db.all("SELECT * FROM vehicle_log WHERE guild_id=? AND vehicle_id=? ORDER BY id DESC LIMIT 20",
                               inter.guild.id, v["id"])
        lines = [f"<t:{r['ts']}:d> · {r['details']}" + (f" — <@{r['actor_id']}>" if r["actor_id"] else "")
                 for r in rows]
        await inter.response.send_message(f"🚗 **{label(v)}** — {self.owner(inter.guild, v)}\n" +
                                          ("\n".join(lines) or "Истории нет."), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())

    # ---------- речь (assistant.py) ----------
    async def speech(self, guild, actor, a, member=None):
        t = a.get("type")
        if not self.bot.feature(guild, "garage"):
            return "🚗 Автопарк у семьи выключен (`/настройки` → «Автопарк»)."
        if t == "car_list":
            if member:
                rows = self.cars(guild, member.id)
                return f"🚗 У {member.display_name}: " + (", ".join(label(v) for v in rows) if rows else "машин семьи нет")
            return self.summary(guild)
        if t == "car_add":
            return await self.add(guild, actor, a.get("model"), a.get("plate"), member, a.get("note"))
        v = self.find(guild, a.get("plate") or a.get("car") or a.get("model"))
        if not v:
            return "❌ Не нашёл такую машину — назови номер или «№» из списка."
        if t == "car_give":
            return await self.give(guild, actor, v, member)
        if t == "car_status":
            return await self.set_status(guild, actor, v, str(a.get("status") or "ok"), a.get("note"), a.get("cost"))
        if t == "car_remove":
            return await self.set_status(guild, actor, v, "gone", a.get("reason"))
        return ""


async def setup(bot):
    await bot.add_cog(Garage(bot))
