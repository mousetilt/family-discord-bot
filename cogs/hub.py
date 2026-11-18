# -*- coding: utf-8 -*-
"""Упрощение UX (Фаза 1, docs/КАРТА_УПРОЩЕНИЯ_КОМАНД.md).
/помощь — интерактивная «что я умею»: что сказать Швейцару, какую кнопку нажать, статус ИИ и голоса.
/панель — панель руководства (разделы с кнопками); копия закреплена в канале бот-команд.
Панель участника — закреплена в канале «🎛・панель» (видит только семья) (ставится при сборке сервера).
Команды помнить не нужно: почти всё — речью («Швейцар, …») или кнопками панелей.

Кнопка панели вызывает СУЩЕСТВУЮЩУЮ слэш-команду (её же проверки прав). Что спросить у человека, раннер понимает из
сигнатуры команды: участник → выбор участника, варианты → список, текст/число → модалка. Старые команды живут как были."""
import inspect
import logging
import typing

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.family import MODULES, module_on, action_on

log = logging.getLogger("family-bot.hub")

STAFF = config.LEAD + ["hr_head", "treasurer", "recruiter", "diplomat", "events", "mentor", "senior"]

# раздел помощи: (эмодзи, название, только для руководства?, [строки])
HELP = {
    "music": ("🎵", "Музыка", False, [
        "🗣 «Швейцар, включи Кино — Группа крови» · «пауза» · «дальше» · «громкость 60» · «что играет?» · «выключи музыку»",
        "⌨️ Ярлыки: `/играть`, `/радио`, `/музыка`",
    ]),
    "voice": ("🎙", "Голос и войс", False, [
        "🗣 «Швейцар, зайди и слушай» — дальше можно командовать голосом, начиная со слова «Швейцар»",
        "🗣 «позови Дашу» · «зайди к Пете и спроси, как дела» · «выйди из войса»",
        "🗣 Руководство: «перенеси нас всех в балкон» · «выключи всем микрофоны» · «объяви сбор голосом»",
    ]),
    "market": ("🛒", "Рынок", False, [
        "🗣 «Швейцар, что есть на рынке по Элегии?»",
        "🔘 Панель участника → **Рынок**: поиск, мои объявления, продать, узнать цену",
    ]),
    "me": ("👤", "Я и мой профиль", False, [
        "🔘 Панель участника → **Моё**: профиль, что Швейцар о тебе помнит, мои выговоры",
        "🔘 Панель участника → **Отпуск**: уйти в отпуск / вернуться (в отпуске не считаешься неактивным)",
        "🗣 «Швейцар, забудь меня» — стереть досье · «напомни через 20 минут зайти в игру» · «брось кубик»",
    ]),
    "rooms": ("🔊", "Комнаты, эфиры, бусты", False, [
        "🔘 Панель участника → **Моя комната**: открыть, закрыть, имя, лимит, выгнать, передать",
        "🔘 Панель участника → **Эфиры и бусты**: объявить свой эфир, кто сейчас в эфире, бусты",
    ]),
    "law": ("⚖️", "Юрист и правила", False, [
        "🗣 «Швейцар, какой штраф за езду без прав?» — ответит по законам и правилам проекта",
        "✍️ Или просто напиши вопрос в канал юриста",
        "🔘 Панель участника → **Обучение**: найти в базе знаний, тренировка экзамена",
    ]),
    "staff": ("🎖", "Ранги и выговоры", True, [
        "🗣 «выговор Пете за опоздание» · «сними выговоры Пете» · «назначь Петю рекрутёром» · «сделай Петю старшим»",
        "🗣 «мут Пете на 30 минут за флуд» · «исключи Петю из семьи» (с подтверждением)",
        "🔘 `/панель` → **Кадры**",
    ]),
    "apps": ("📋", "Заявки и экзамен", True, [
        "Заявки подаются кнопкой в канале заявок — бот сам проводит экзамен и зовёт на собеседование",
        "🔘 `/панель` → **Заявки**: открытые заявки, обращения, наставники, вопросы экзамена",
    ]),
    "activity": ("📊", "Активность и сборы", True, [
        "🗣 «кто неактив за неделю?» · «топ активности» · «сводка» · «объяви сбор на 20:00 у банка»",
        "🗣 «опрос: во сколько едем? 19:00, 20:00» · «напиши всем в ЛС, что сегодня сбор»",
        "🔘 `/панель` → **Активность**",
    ]),
    "money": ("💰", "Казна и взносы", True, [
        "Включается в `/настройки` → «Казна». Ведут: Глава, зам, совет, казначей, старший состав. Суммы — вирты.",
        "🗣 «внеси в казну 50к от Пети за сделку» · «расход 20к на тачку» · «Петя сдал взнос» · «сколько в казне?»",
        "🗣 «кто не сдал взнос?» · Глава/зам: «взнос 100к в неделю» · «ставка рекрутёрам 30к»",
        "🗣 «начисли зарплаты по ставкам» · «выплати организаторам по 50к за ивент» · «заплати Пете 30к»",
        "🗣 «штраф Пете 30к за опоздание» · «выговор Пете за неявку и штраф 50к» · «кто со штрафами?» · «Петя оплатил штраф»",
        "🔘 `/панель` → **Казна**: баланс, внести, расход, взнос, должники, история, отчёт · **Зарплаты** · **Штрафы**",
    ]),
    "guard": ("🛡", "Модерация и ЧС", True, [
        "Бот сам ловит спам, рекламу, скам и сделки за реал — решения приходят в канал модерации с кнопками",
        "🗣 «пробей Петю по ЧС» · «внеси Петю в ЧС за кидок» · «включи режим рейда»",
        "🔘 `/панель` → **Модерация**: защита, рейд, ЧС",
    ]),
    "diplo": ("🤝", "Дипломатия и альянс", True, [
        "🔘 `/панель` → **Дипломатия**: договоры, отношения, позвать на переговоры",
        "🔘 `/панель` → **Альянс**: состав, устав, летопись, тревога",
    ]),
    "setup": ("⚙️", "Настройка бота", True, [
        "🧙 `/настройка` — мастер по шагам: семья → роли → каналы → модули → ИИ → собрать сервер",
        "⚙️ `/настройки` — что включено: каждый модуль и его отдельные действия",
        "🔘 `/панель` — все кнопки руководства в одном месте",
    ]),
}


def is_staff(bot, member):
    return isinstance(member, discord.Member) and (member.id == member.guild.owner_id or bot.has(member, STAFF))


def status_lines(bot, guild):
    """Короткий статус: ИИ, голос, модули."""
    ai = bot.ai_for(guild)
    out = [f"🤖 ИИ: **{ai.name}**" if ai.on else "🤖 ИИ: **выключен** — руководство может подключить бесплатный ключ "
                                                  "(`/настройка` → шаг «ИИ»)"]
    jv = bot.get_cog("Jarvis")
    if jv:
        try:
            voice = bool(jv.whisper_key(guild))
        except Exception:
            voice = False
        here = guild.id in getattr(jv, "listeners", {})
        out.append(f"🎙 Голосовые команды: **{'работают' if voice else 'нужен ключ Groq'}**"
                   + (" · Швейцар сейчас в войсе" if here else ""))
    total = sum(len(a) for _, a in MODULES.values())
    on = sum(1 for m, (_, acts) in MODULES.items() if module_on(bot.db, guild.id, m)
             for a in acts if action_on(bot.db, guild.id, m, a))
    out.append(f"⚙️ Включено функций: **{on}/{total}**")
    return out


def help_home(bot, member):
    g = member.guild
    lines = ["Ничего учить не надо — есть три способа:",
             "🗣 **Сказать** — напиши в чат «**Швейцар, …**» (или упомяни меня) и попроси своими словами. "
             "В войсе — то же самое голосом.",
             "🔘 **Нажать** — панель участника в канале «🎛・панель»" + (", для руководства — `/панель`"
                                                                       if is_staff(bot, member) else "") + ".",
             "⌨️ **Команда** — только для редкого; набери `/` и увидишь, что тебе доступно.", "",
             *status_lines(bot, g), "", "Выбери раздел ниже, чтобы увидеть примеры."]
    mt = bot.get_cog("Maintenance")
    if mt and is_staff(bot, member):  # руководству — состояние бота от сторожа
        lines.insert(-2, mt.health_line())
    e = bot.embed("🎩 Что я умею", "\n".join(lines), config.COLOR, g)
    return e


def help_section(bot, member, key):
    emoji, title, lead, lines = HELP[key]
    e = bot.embed(f"{emoji} {title}" + (" · руководство" if lead else ""), "\n".join(lines), config.COLOR,
                  member.guild)
    return e


class HelpSelect(discord.ui.Select):
    def __init__(self, bot, member, current=None):
        self.bot = bot
        staff = is_staff(bot, member)
        opts = [discord.SelectOption(label="Главное", value="home", emoji="🏠", default=current is None)]
        for key, (emoji, title, lead, _) in HELP.items():
            if lead and not staff:
                continue
            opts.append(discord.SelectOption(label=title, value=key, emoji=emoji, default=current == key))
        super().__init__(placeholder="Раздел помощи…", options=opts[:25])

    async def callback(self, inter: discord.Interaction):
        key = self.values[0]
        e = help_home(self.bot, inter.user) if key == "home" else help_section(self.bot, inter.user, key)
        await inter.response.edit_message(embed=e, view=HelpView(self.bot, inter.user, None if key == "home" else key))


class HelpView(discord.ui.View):
    def __init__(self, bot, member, current=None):
        super().__init__(timeout=600)
        self.add_item(HelpSelect(bot, member, current))


# ═══════════════ панели ═══════════════
# действие: ключ → (эмодзи, подпись, команда, фиксированные аргументы). Команда None — особое действие (см. SPECIAL).
ACTIONS = {
    # заявки
    "apps": ("📥", "Открытые заявки", "заявки", {}),
    "appeals": ("📬", "Обращения", "обращения", {}),
    "m_assign": ("🧑‍🏫", "Назначить наставника", "наставник назначить", {}),
    "m_list": ("📋", "Наставники", "наставник список", {}),
    "m_top": ("🏆", "Рейтинг наставников", "наставник рейтинг", {}),
    "exam_q": ("❓", "Вопросы экзамена", "экзамен вопросы", {}),
    "manual": ("🛂", "Принять вручную", "принять", {}),
    "rec_stats": ("📊", "Статистика рекрутёров", "рекрутёры статистика", {}),
    "rec_rate": ("✏️", "Ставка рекрутёрам", "рекрутёры ставка", {}),
    "rec_pay": ("💸", "Выплатить рекрутёрам", "рекрутёры выплатить", {}),
    "m_days": ("⏳", "Срок испытательного", "наставник срок", {}),
    "exam_pass": ("📝", "Сдать экзамен (новичкам)", "экзамен сдать", {}),
    # кадры
    "rank": ("🔁", "Ранг", "ранг", {}),
    "post": ("🎖", "Должность", "должность", {}),
    "warn": ("⚖️", "Выговор", "выговор", {}),
    "warns": ("📄", "Выговоры участника", "выговоры", {}),
    "unwarn": ("⚪", "Снять выговор", "снять-выговор", {}),
    "mute": ("🔇", "Мут", "мут", {}),
    "expel": ("🚪", "Исключить", "исключить", {}),
    "nick_tpl": ("✏️", "Шаблон ника", "ник шаблон", {}),
    "nick_check": ("🔎", "Кто с ником не по форме", "ник проверка", {}),
    # активность
    "report": ("📊", "Отчёт", "отчёт", {}),
    "top": ("🏆", "Топ", "топ", {}),
    "inactive": ("💤", "Неактив", "неактив", {}),
    "digest": ("🗞", "Сводка", "сводка", {}),
    "gather": ("📣", "Объявить сбор", "сбор", {}),
    "announce": ("📢", "Объявление", "объявление", {}),
    "draft": ("📝", "ИИ-черновик", "черновик", {}),
    "sched": ("⏰", "Объявление на время", "запланировать", {}),
    "sched_list": ("🗓", "Запланированные", "запланированные", {}),
    "relay_set": ("📰", "Ретранслятор новостей", "ретранслятор настроить", {}),
    "missed": ("🕰", "Что я пропустил", "пропустил", {}),
    "search": ("🔎", "Поиск в интернете", "поиск", {}),
    "chron": ("📖", "Глава летописи", "летопись глава", {}),
    # модерация
    "sec": ("🛡", "Защита: статус", "защита статус", {}),
    "raid_on": ("🚨", "Режим рейда: вкл", "защита рейд", {"on": True}),
    "raid_off": ("✅", "Режим рейда: выкл", "защита рейд", {"on": False}),
    "bl_list": ("⛔", "ЧС: список", "чс список", {}),
    "bl_check": ("🔎", "ЧС: проверить", "чс проверить", {}),
    "bl_add": ("➕", "ЧС: добавить", "чс добавить", {}),
    # дипломатия
    "treaties": ("📜", "Договоры", "договор список", {}),
    "nego": ("🕊", "Позвать на переговоры", "переговоры пригласить", {}),
    "rel_set": ("🌐", "Отношения с семьёй", "отношения установить", {}),
    "treaty_add": ("✍️", "Заключить договор", "договор заключить", {}),
    # альянс
    "al_roster": ("🏰", "Состав альянса", "альянс состав", {}),
    # автопарк
    "car_list": ("🚗", "Машины семьи", "автопарк список", {}),
    "car_add": ("➕", "Добавить машину", "автопарк добавить", {}),
    "car_give": ("🔁", "Передать машину", "автопарк передать", {}),
    "car_status": ("🔧", "Ремонт / в строю", "автопарк статус", {}),
    "car_remove": ("🗑", "Списать машину", "автопарк списать", {}),
    "car_hist": ("📜", "История машины", "автопарк история", {}),
    "al_charter": ("📜", "Устав", "альянс устав", {}),
    "al_chron": ("📖", "Летопись альянса", "альянс летопись", {}),
    "al_alarm": ("🚨", "Тревога альянсу", "альянс тревога", {}),
    # база знаний
    "kb_list": ("📚", "Документы", "база список", {}),
    "kb_find": ("🔎", "Найти в базе", "база найти", {}),
    "kb_text": ("📝", "Добавить текст", "база текст", {}),
    "kb_link": ("🔗", "Добавить ссылку", "база ссылка", {}),
    "kb_rules": ("📜", "Правила проекта", "база правила-проекта", {}),
    "kb_update": ("🔄", "Обновить ссылки", "база обновить", {}),
    # казна
    "tr_balance": ("💰", "Баланс", "казна баланс", {}),
    "tr_in": ("➕", "Внести", "казна внести", {}),
    "tr_out": ("➖", "Расход", "казна расход", {}),
    "tr_due": ("✅", "Взнос сдан", "казна взнос", {}),
    "tr_debtors": ("📋", "Кто должен", "казна должники", {}),
    "tr_history": ("🧾", "История", "казна история", {}),
    "tr_report": ("📊", "Отчёт", "казна отчёт", {}),
    "tr_dues": ("⚙️", "Настроить взнос", "казна настроить-взнос", {}),
    # штрафы
    "fine_add": ("🧾", "Выписать штраф", "казна штраф", {}),
    "fine_list": ("📋", "Кто не оплатил", "казна штрафы", {}),
    "fine_paid": ("✅", "Отметить оплату", "казна штраф-оплачен", {}),
    "fine_cancel": ("↩️", "Отменить штраф", "казна штраф-отменить", {}),
    # зарплаты
    "sal_rates": ("📋", "Ставки", "казна ставки", {}),
    "sal_rate": ("✏️", "Задать ставку", "казна ставка", {}),
    "sal_all": ("💸", "Выплатить всем по ставкам", "казна зарплата", {}),
    "sal_pay": ("🎉", "Выплата за ивент / разовая", "казна выплатить", {}),
    "sal_role": ("👥", "Выплатить роли", "казна выплатить-роли", {}),
    # настройка
    "wizard": ("🧙", "Мастер настройки", None, {}),
    "modules": ("⚙️", "Модули", "настройки", {}),
    "ai": ("🤖", "Статус ИИ", "ии статус", {}),
    "perms": ("🔐", "Права по каналам", "права", {}),
    # участник
    "profile": ("🪪", "Мой профиль", "профиль", {"member": None}),
    "dossier": ("🧠", "Что Швейцар помнит", "досье", {}),
    "forget": ("🧹", "Забудь меня", "забудь_меня", {}),
    "my_warns": ("⚖️", "Мои выговоры", "выговоры", {"member": None}),
    "vac": ("🌴", "Уйти в отпуск", "отпуск", {}),
    "back": ("👋", "Я вернулся", "вернулся", {}),
    "r_open": ("🔓", "Открыть", "комната открыть", {}),
    "r_close": ("🔒", "Закрыть", "комната закрыть", {}),
    "r_name": ("✏️", "Название", "комната имя", {}),
    "r_limit": ("👥", "Лимит", "комната лимит", {}),
    "r_kick": ("👢", "Выгнать", "комната выгнать", {}),
    "r_give": ("🤝", "Передать", "комната передать", {}),
    "mk_search": ("🔎", "Поиск", "рынок поиск", {}),
    "mk_mine": ("📦", "Мои объявления", "рынок мои", {}),
    "mk_sell": ("💰", "Продать", "продать", {}),
    "mk_price": ("🏷", "Узнать цену", "цена", {}),
    "stream": ("🔴", "Я в эфире", "эфир", {}),
    "streams": ("📺", "Кто в эфире", "эфиры", {}),
    "boost": ("💎", "Бусты", "буст прогресс", {}),
    "boost_role": ("🎨", "Роль за буст", "буст роль", {}),
    "exam_try": ("📝", "Тренировка экзамена", "экзамен тренировка", {}),
    "lawyer": ("⚖️", "Спросить юриста", "юрист", {}),
    "help": ("❔", "Помощь", None, {}),
    "paid": ("🧾", "Сдал взнос / штраф", "сдал", {}),
    "promote_nb": ("🎓", "Новичков — в члены", "перевести-новичков", {}),
    "who": ("👥", "Кто… (списки)", "кто", {}),
    "export": ("📤", "Выгрузить в Excel", "выгрузка", {}),
}
SETUP_ROLES = ["leader", "deputy"]
# раздел: ключ → (эмодзи, название, кому виден (None — всем в семье), [действия])
LEAD_SECTIONS = {
    "apps": ("📋", "Заявки", config.HR + ["mentor"], ["apps", "manual", "appeals", "m_assign", "m_list", "m_days", "m_top",
                                                     "exam_q", "rec_stats", "rec_rate"]),
    "staff": ("🎖", "Кадры", config.LEAD + ["hr_head", "senior"],
              ["rank", "post", "warn", "warns", "unwarn", "mute", "expel", "promote_nb", "who", "nick_tpl", "nick_check"]),
    "activity": ("📊", "Активность", STAFF, ["report", "top", "inactive", "export", "digest", "gather", "announce", "draft", "sched",
                                         "sched_list", "relay_set", "chron"]),
    "mod": ("🛡", "Модерация", config.LEAD + ["hr_head", "senior"],
            ["sec", "raid_on", "raid_off", "bl_list", "bl_check", "bl_add"]),
    "diplo": ("🤝", "Дипломатия", config.DIPLO, ["treaties", "nego", "rel_set", "treaty_add"]),
    "alliance": ("🏰", "Альянс", config.LEAD + ["diplomat"], ["al_roster", "al_charter", "al_chron", "al_alarm"]),
    "treasury": ("💰", "Казна", config.TREASURY, ["tr_balance", "tr_in", "tr_out", "tr_due", "tr_debtors",
                                               "tr_history", "tr_report", "tr_dues"]),
    "salary": ("💸", "Зарплаты", config.TREASURY, ["sal_rates", "sal_rate", "sal_all", "sal_pay", "sal_role",
                                                         "rec_pay"]),
    "fines": ("🧾", "Штрафы", config.TREASURY + ["hr_head"], ["fine_add", "fine_list", "fine_paid", "fine_cancel"]),
    "kb": ("📚", "База знаний", config.LEAD + ["hr_head", "mentor"],
           ["kb_list", "kb_find", "kb_text", "kb_link", "kb_rules", "kb_update"]),
    "garage": ("🚗", "Автопарк", config.LEAD + ["senior", "treasurer"],
               ["car_list", "car_add", "car_give", "car_status", "car_remove", "car_hist"]),
    "setup": ("⚙️", "Настройка", SETUP_ROLES, ["wizard", "modules", "ai", "perms"]),
}
MEMBER_SECTIONS = {
    "me": ("👤", "Моё", config.FAMILY_PLUS, ["profile", "paid", "missed", "search", "dossier", "my_warns", "forget"]),
    "vac": ("🌴", "Отпуск", config.FAMILY_PLUS, ["vac", "back"]),
    "room": ("🔊", "Моя комната", config.FAMILY_PLUS, ["r_open", "r_close", "r_name", "r_limit", "r_kick", "r_give"]),
    "market": ("🛒", "Рынок", config.FAMILY_PLUS, ["mk_search", "mk_mine", "mk_sell", "mk_price"]),
    "fun": ("🔴", "Эфиры и бусты", config.FAMILY_PLUS, ["stream", "streams", "boost", "boost_role"]),
    "learn": ("🎓", "Обучение", config.FAMILY_PLUS, ["exam_try", "exam_pass", "lawyer", "kb_find"]),
    "cars": ("🚗", "Автопарк", config.FAMILY_PLUS, ["car_list", "car_hist"]),
}
SECTIONS = {**LEAD_SECTIONS, **MEMBER_SECTIONS}
# Команды, скрытые из списка «/» у рядовых участников (default_permissions) — они заменены кнопками и речью.
# Право Discord у них только прячет команду; сама команда проверяет доступ внутри, поэтому кнопки панели работают.
# Админ сервера может вернуть любую из них ролям: Настройки сервера → Интеграции → бот.
HIDDEN_FOR_MEMBERS = {"отпуск", "вернулся", "выговоры", "профиль", "топ", "досье", "забудь_меня", "любимчики",
                      "эфир", "эфиры", "цена", "продать", "юрист", "спросить", "прозвище", "экзамен", "буст",
                      "комната", "казна", "база", "журнал", "кто", "перевести-новичков", "выгрузка"}
TEXTY = {"text", "question", "note", "reason", "what"}  # такие поля — многострочные


def can_see(bot, member, need):
    if not isinstance(member, discord.Member):
        return False
    if need is None:
        return True
    if need is SETUP_ROLES and (member.id == member.guild.owner_id or member.guild_permissions.administrator):
        return True
    return member.id == member.guild.owner_id or bot.has(member, need)


def find_command(bot, qname):
    for c in bot.tree.walk_commands():
        if isinstance(c, app_commands.Command) and c.qualified_name == qname:
            return c
    return None


def wants_choice_obj(cmd, name):
    """Аргумент объявлен как app_commands.Choice[...] (а не просто str с вариантами)?"""
    try:
        ann = inspect.signature(cmd.callback).parameters[name].annotation
    except (KeyError, TypeError, ValueError):
        return False
    return ann is app_commands.Choice or typing.get_origin(ann) is app_commands.Choice


class Plan:
    """Что спросить у человека перед вызовом команды."""

    def __init__(self, cmd, fixed):
        self.cmd, self.fixed = cmd, dict(fixed)
        self.users, self.choices, self.texts, self.missing = [], [], [], []
        T = discord.AppCommandOptionType
        for p in cmd.parameters:
            if p.name in self.fixed:
                continue
            if p.choices:
                self.choices.append(p)
            elif p.type == T.user:
                self.users.append(p)
            elif p.type in (T.string, T.integer, T.number):
                self.texts.append(p)
            elif p.type == T.boolean:
                if p.required:
                    self.choices.append(p)  # Да/Нет
            elif p.required:
                self.missing.append(p)  # файл, роль, канал — кнопкой не спросить
        # в модалке максимум 5 полей: лишние необязательные отбрасываем
        while len(self.texts) > 5:
            opt = next((p for p in reversed(self.texts) if not p.required), None)
            if not opt:
                self.missing.append(self.texts.pop())
                continue
            self.texts.remove(opt)
        while len(self.users) + len(self.choices) > 4:
            opt = next((p for p in reversed(self.choices + self.users) if not p.required), None)
            if not opt:
                break
            (self.choices if opt in self.choices else self.users).remove(opt)

    def choice_value(self, p, value):
        if p.type == discord.AppCommandOptionType.boolean and not p.choices:
            return value == "1"
        ch = next((c for c in p.choices if str(c.value) == value), None)
        if ch is None:
            return None
        return ch if wants_choice_obj(self.cmd, p.name) else ch.value


def plabel(p):
    name = (p.display_name or p.name).replace("_", " ")
    return name[:1].upper() + name[1:]


async def deny(inter, text="⛔ Недостаточно прав."):
    if not inter.response.is_done():
        await inter.response.send_message(text, ephemeral=True)


async def can_run(bot, inter, cmd):
    """Проверки команды (как при обычном вызове) + права Discord, если своих проверок у команды нет."""
    if not inter.guild or not bot.is_family(inter.guild):
        await deny(inter, "Семья на этом сервере не подключена.")
        return False
    root = cmd.root_parent or cmd
    perms = cmd.default_permissions or (cmd.root_parent.default_permissions if cmd.root_parent else None)
    if perms and not cmd.checks and root.name not in HIDDEN_FOR_MEMBERS \
            and not perms.is_subset(inter.user.guild_permissions):
        await deny(inter)
        return False
    try:
        ok = await cmd._check_can_run(inter)
    except app_commands.AppCommandError:
        ok = False
    if not ok:
        await deny(inter)
    return ok


async def execute(bot, inter, cmd, kwargs):
    if not await can_run(bot, inter, cmd):
        return
    log.info("Панель: %s → /%s", inter.user, cmd.qualified_name)
    try:
        await cmd.callback(cmd.binding, inter, **kwargs)
    except Exception as e:
        log.exception("Панель: /%s упала", cmd.qualified_name)
        text = f"❌ Не получилось: {str(e)[:200] or type(e).__name__}"
        try:
            if inter.response.is_done():
                await inter.followup.send(text, ephemeral=True)
            else:
                await inter.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class ArgModal(discord.ui.Modal):
    def __init__(self, bot, plan, values, title):
        super().__init__(title=title[:45])
        self.bot, self.plan, self.values = bot, plan, values
        self.inputs = {}
        for p in plan.texts:
            hint = (p.description or "")
            if p.type != discord.AppCommandOptionType.string:
                lo, hi = p.min_value, p.max_value
                hint = f"число{f' от {lo}' if lo is not None else ''}{f' до {hi}' if hi is not None else ''}. " + hint
            ti = discord.ui.TextInput(label=plabel(p)[:45], required=p.required, placeholder=hint[:100] or None,
                                      style=discord.TextStyle.paragraph if p.name in TEXTY else discord.TextStyle.short,
                                      max_length=1500 if p.name in TEXTY else 200)
            self.inputs[p.name] = (p, ti)
            self.add_item(ti)

    async def on_submit(self, inter: discord.Interaction):
        kwargs = dict(self.values)
        for name, (p, ti) in self.inputs.items():
            ok, val = parse_text(p, ti.value)
            if not ok:
                return await inter.response.send_message(f"❌ «{plabel(p)}» — нужно число.", ephemeral=True)
            if val is not None:
                kwargs[name] = val
        await execute(self.bot, inter, self.plan.cmd, kwargs)


def parse_text(p, raw):
    """Значение из текстового поля → (ок, значение|None). Числа — с ограничениями команды."""
    T = discord.AppCommandOptionType
    raw = (raw or "").strip()
    if not raw:
        return True, None
    if p.type == T.string:
        return True, raw
    try:
        num = int(raw.replace(" ", "")) if p.type == T.integer else float(raw.replace(",", "."))
    except ValueError:
        return False, None
    if p.min_value is not None:
        num = max(p.min_value, num)
    if p.max_value is not None:
        num = min(p.max_value, num)
    return True, num


class OneModal(discord.ui.Modal):
    """Всё в одном окне: выбор участника, варианты и текст (Discord разрешает выпадающие списки в модалке).
    До 5 полей; больше — старый путь «выбор → Дальше → модалка» (ArgView)."""

    def __init__(self, bot, plan, title):
        super().__init__(title=title[:45])
        self.bot, self.plan, self.fields = bot, plan, {}
        for p in plan.users:
            sel = discord.ui.UserSelect(min_values=1 if p.required else 0, max_values=1, required=p.required)
            self.fields[p.name] = ("user", p, sel)
            self.add_item(discord.ui.Label(text=plabel(p)[:45], component=sel))
        for p in plan.choices:
            opts = ([discord.SelectOption(label=str(c.name)[:100], value=str(c.value)) for c in p.choices[:25]]
                    if p.choices else [discord.SelectOption(label="Да", value="1"),
                                       discord.SelectOption(label="Нет", value="0")])
            sel = discord.ui.Select(options=opts, min_values=1 if p.required else 0, max_values=1, required=p.required)
            self.fields[p.name] = ("choice", p, sel)
            self.add_item(discord.ui.Label(text=plabel(p)[:45], component=sel))
        for p in plan.texts:
            hint = p.description or ""
            if p.type != discord.AppCommandOptionType.string:
                lo, hi = p.min_value, p.max_value
                hint = f"число{f' от {lo}' if lo is not None else ''}{f' до {hi}' if hi is not None else ''}. " + hint
            ti = discord.ui.TextInput(required=p.required, placeholder=hint[:100] or None,
                                      style=discord.TextStyle.paragraph if p.name in TEXTY else discord.TextStyle.short,
                                      max_length=1500 if p.name in TEXTY else 200)
            self.fields[p.name] = ("text", p, ti)
            self.add_item(discord.ui.Label(text=plabel(p)[:45], component=ti))

    async def on_submit(self, inter: discord.Interaction):
        kwargs = dict(self.plan.fixed)
        for name, (kind, p, c) in self.fields.items():
            if kind == "text":
                ok, val = parse_text(p, c.value)
                if not ok:
                    return await inter.response.send_message(f"❌ «{plabel(p)}» — нужно число.", ephemeral=True)
            elif kind == "user":
                val = c.values[0] if c.values else None
            else:
                val = self.plan.choice_value(p, c.values[0]) if c.values else None
            if val is None and p.required:
                return await inter.response.send_message(f"❌ Заполни «{plabel(p)}».", ephemeral=True)
            if val is not None:
                kwargs[name] = val
        await execute(self.bot, inter, self.plan.cmd, kwargs)


class ArgUser(discord.ui.UserSelect):
    def __init__(self, p, row):
        self.p = p
        super().__init__(placeholder=f"{plabel(p)}{'' if p.required else ' (необязательно)'}"[:150],
                         min_values=1 if p.required else 0, max_values=1, row=row)

    async def callback(self, inter: discord.Interaction):
        self.view.values[self.p.name] = self.values[0] if self.values else None
        await inter.response.defer()


class ArgChoice(discord.ui.Select):
    def __init__(self, plan, p, row):
        self.plan, self.p = plan, p
        if p.choices:
            opts = [discord.SelectOption(label=str(c.name)[:100], value=str(c.value)) for c in p.choices[:25]]
        else:
            opts = [discord.SelectOption(label="Да", value="1"), discord.SelectOption(label="Нет", value="0")]
        super().__init__(placeholder=f"{plabel(p)}{'' if p.required else ' (необязательно)'}"[:150],
                         min_values=1 if p.required else 0, max_values=1, options=opts, row=row)

    async def callback(self, inter: discord.Interaction):
        self.view.values[self.p.name] = self.plan.choice_value(self.p, self.values[0]) if self.values else None
        await inter.response.defer()


class ArgView(discord.ui.View):
    def __init__(self, bot, plan, title):
        super().__init__(timeout=600)
        self.bot, self.plan, self.title = bot, plan, title
        self.values = dict(plan.fixed)
        row = 0
        for p in plan.users:
            self.add_item(ArgUser(p, row))
            row += 1
        for p in plan.choices:
            self.add_item(ArgChoice(plan, p, row))
            row += 1
        go = discord.ui.Button(label="Дальше" if plan.texts else "Готово", emoji="▶️",
                               style=discord.ButtonStyle.success, row=4)
        go.callback = self._go
        self.add_item(go)

    async def _go(self, inter: discord.Interaction):
        lack = [plabel(p) for p in self.plan.users + self.plan.choices
                if p.required and self.values.get(p.name) is None]
        if lack:
            return await inter.response.send_message("Сначала выбери: " + ", ".join(lack), ephemeral=True)
        kwargs = {k: v for k, v in self.values.items() if v is not None or k in self.plan.fixed}
        if self.plan.texts:
            return await inter.response.send_modal(ArgModal(self.bot, self.plan, kwargs, self.title))
        await execute(self.bot, inter, self.plan.cmd, kwargs)


async def start_action(bot, inter: discord.Interaction, key):
    if key not in ACTIONS:
        return await deny(inter, "Эта кнопка устарела — открой панель заново.")
    emoji, label, qname, fixed = ACTIONS[key]
    if key == "help":
        return await bot.get_cog("Hub").send_help(inter)
    if key == "wizard":
        ss = bot.get_cog("ServerSetup")
        return await (ss.open_wizard(inter) if ss else deny(inter, "Модуль настройки не загружен."))
    cmd = find_command(bot, qname)
    if not cmd:
        return await deny(inter, "Эта функция сейчас недоступна (модуль не загружен).")
    plan = Plan(cmd, fixed)
    if plan.missing:
        return await deny(inter, f"Для этого нужен файл или выбор, который кнопкой не передать — набери `/{qname}`.")
    if not await can_run(bot, inter, cmd):
        return
    title = f"{emoji} {label}"
    if (plan.users or plan.choices) and len(plan.users) + len(plan.choices) + len(plan.texts) <= 5:
        try:  # одно окно вместо «выбор → Дальше → модалка»
            return await inter.response.send_modal(OneModal(bot, plan, title))
        except (discord.HTTPException, TypeError, ValueError) as e:
            log.info("Панель: одно окно не вышло (%s) — пошагово", e)
            if inter.response.is_done():
                return
    if plan.users or plan.choices:
        return await inter.response.send_message(f"**{title}**", view=ArgView(bot, plan, title), ephemeral=True)
    if plan.texts:
        return await inter.response.send_modal(ArgModal(bot, plan, dict(plan.fixed), title))
    await execute(bot, inter, cmd, dict(plan.fixed))


def section_view(bot, sec):
    v = discord.ui.View(timeout=None)
    for i, key in enumerate(SECTIONS[sec][3][:25]):
        v.add_item(ActionButton(key, row=i // 4))
    return v


def panel_view(sections, member=None, bot=None, extra_help=False):
    v = discord.ui.View(timeout=None)
    keys = [k for k, s in sections.items() if member is None or can_see(bot, member, s[2])]
    for i, k in enumerate(keys[:20]):
        v.add_item(SectionButton(k, row=i // 4))
    if extra_help:
        v.add_item(ActionButton("help", row=min(4, len(keys) // 4 + 1)))
    return v


class SectionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"hub:s:(?P<k>\w+)"):
    def __init__(self, k, row=None):
        emoji, title, *_ = SECTIONS.get(k, ("❔", k, None, []))
        super().__init__(discord.ui.Button(label=title, emoji=emoji, style=discord.ButtonStyle.primary,
                                           custom_id=f"hub:s:{k}", row=row))
        self.k = k

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["k"])

    async def callback(self, inter: discord.Interaction):
        bot = inter.client
        if self.k not in SECTIONS:
            return await deny(inter, "Эта кнопка устарела — открой панель заново.")
        if not inter.guild or not bot.is_family(inter.guild):
            return await deny(inter, "Семья на этом сервере не подключена.")
        emoji, title, need, _ = SECTIONS[self.k]
        if not can_see(bot, inter.user, need):
            return await deny(inter, "⛔ Этот раздел — для руководства." if self.k in LEAD_SECTIONS
                              else "Этот раздел — для семьи.")
        await inter.response.send_message(f"**{emoji} {title}** — выбери действие:", view=section_view(bot, self.k),
                                          ephemeral=True)


class ActionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"hub:a:(?P<k>\w+)"):
    def __init__(self, k, row=None):
        emoji, label, *_ = ACTIONS.get(k, ("❔", k, None, {}))
        super().__init__(discord.ui.Button(label=label[:80], emoji=emoji, style=discord.ButtonStyle.secondary,
                                           custom_id=f"hub:a:{k}", row=row))
        self.k = k

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["k"])

    async def callback(self, inter: discord.Interaction):
        await start_action(inter.client, inter, self.k)


# Панели в каналах модулей: канал → (заголовок, разделы, модуль-рубильник). Кнопки — сразу действия разделов,
# сверху — живая сводка (status). Права проверяет сама команда, так что кнопки безопасны в любом канале.
CHANNEL_PANELS = {
    "treasury_log": ("💰 Казна семьи", ["treasury", "salary", "fines"], "treasury"),
    "review": ("📋 Набор в семью", ["apps"], None),
    "warns": ("🎖 Кадры", ["staff"], None),
    "modqueue": ("🛡 Модерация", ["mod"], None),
    "diplo_archive": ("🤝 Дипломатия", ["diplo"], None),
    "garage": ("🚗 Автопарк семьи", ["garage"], "garage"),
}


def channel_status(bot, guild, key):
    """Живая сводка для панели канала (или None)."""
    try:
        if key == "treasury_log":
            tr = bot.get_cog("Treasury")
            return tr.digest_line(guild) if tr else None
        if key == "garage":
            gar = bot.get_cog("Garage")
            return gar.summary(guild, 15) if gar else None
        if key == "review":
            db = bot.db
            apps = db.one("SELECT COUNT(*) n FROM applications WHERE guild_id=? AND status IN ('new','taken','interview')",
                          guild.id)["n"]
            prob = db.one("SELECT COUNT(*) n FROM mentorships WHERE guild_id=? AND status='active'", guild.id)["n"]
            return f"📥 Открытых заявок: **{apps}** · 🎓 на испытательном сроке: **{prob}**"
    except Exception:
        log.exception("Сводка панели %s", key)
    return None


def channel_panel(bot, guild, key):
    title, secs, _ = CHANNEL_PANELS[key]
    lines = []
    status = channel_status(bot, guild, key)
    if status:
        lines += [status, ""]
    v, n = discord.ui.View(timeout=None), 0
    for sec in secs:
        e, t, _, acts = SECTIONS[sec]
        lines.append(f"{e} **{t}** — " + ", ".join(ACTIONS[a][1] for a in acts))
        for a in acts:
            if n < 25:
                v.add_item(ActionButton(a, row=n // 5))
                n += 1
    lines += ["", "🗣 Или скажи: «Швейцар, …» — своими словами."]
    emb = bot.embed(title, "\n".join(lines), config.GOLD if key == "treasury_log" else config.COLOR, guild)
    emb.set_footer(text="Панель обновляется сама · " + bot.brand(guild, config.FOOTER))
    return {"embed": emb, "view": v}


def lead_embed(bot, guild):
    lines = ["Всё управление — кнопками. Нажми раздел, потом действие: бот сам спросит, кого и что.",
             "🗣 А можно просто сказать: «Швейцар, выговор Пете за опоздание».", ""]
    lines += [f"{e} **{t}** — " + ", ".join(ACTIONS[a][1] for a in acts[:4]) + ("…" if len(acts) > 4 else "")
              for e, t, _, acts in LEAD_SECTIONS.values()]
    return bot.embed("🛡 Панель руководства", "\n".join(lines), config.COLOR, guild)


def member_embed(bot, guild):
    lines = ["Нажми раздел — бот покажет кнопки. Команды учить не надо.",
             "🗣 Или просто напиши в чат: «**Швейцар, …**» и попроси своими словами.", ""]
    lines += [f"{e} **{t}** — " + ", ".join(ACTIONS[a][1] for a in acts) for e, t, _, acts in MEMBER_SECTIONS.values()]
    return bot.embed("👤 Панель участника", "\n".join(lines), config.GOLD, guild)


def emoji_png(path):
    """Картинка → PNG 128×128 (лимит Discord на эмодзи — 256 КБ, герб в assets весит больше мегабайта)."""
    import io as _io
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    im.thumbnail((128, 128))
    buf = _io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class Hub(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.emblem = None  # эмодзи приложения с гербом — работает на всех серверах семей без загрузки на каждый

    @commands.Cog.listener()
    async def on_ready(self):
        """Эмодзи приложения (Discord хранит их у бота, а не у сервера): загружаем герб один раз."""
        if self.emblem:
            return
        try:
            have = {e.name: e for e in await self.bot.fetch_application_emojis()}
            e = have.get("family_emblem")
            if e is None:
                e = await self.bot.create_application_emoji(name="family_emblem", image=emoji_png(config.ASSETS / "emblem.png"))
            self.emblem = str(e)
        except (discord.HTTPException, OSError, AttributeError) as ex:
            log.info("Эмодзи приложения не загружено: %s", ex)

    async def send_help(self, inter: discord.Interaction):
        e = help_home(self.bot, inter.user)
        if self.emblem and e.title and self.bot.is_home(inter.guild):  # герб — бренд домашней семьи
            e.title = f"{self.emblem} {e.title}"
        await inter.response.send_message(embed=e, view=HelpView(self.bot, inter.user), ephemeral=True)

    @app_commands.command(name="помощь", description="Что умеет бот: что сказать, куда нажать")
    @app_commands.guild_only()
    async def help_cmd(self, inter: discord.Interaction):
        await self.send_help(inter)

    @app_commands.command(name="панель", description="Панель руководства: заявки, кадры, активность, модерация, настройка")
    @app_commands.guild_only()
    async def panel_cmd(self, inter: discord.Interaction):
        if not is_staff(self.bot, inter.user) and not inter.user.guild_permissions.administrator:
            return await inter.response.send_message(
                "Панель руководства — для должностных лиц. Тебе — панель участника в канале «🎛・панель» и `/помощь`.",
                ephemeral=True)
        await inter.response.send_message(embed=lead_embed(self.bot, inter.guild),
                                          view=panel_view(LEAD_SECTIONS, inter.user, self.bot), ephemeral=True)

    async def post_panels(self, guild, repost):
        """Вызывается из сборки сервера (/настройка): панель участника и закреплённая панель руководства."""
        bot = self.bot

        async def member():
            return {"embed": member_embed(bot, guild), "view": panel_view(MEMBER_SECTIONS, extra_help=True)}

        async def lead():
            return {"embed": lead_embed(bot, guild), "view": panel_view(LEAD_SECTIONS)}

        if int(bot.db.gget(guild, "panelv:hub_member") or 0) < 2 and bot.ch(guild, "panel"):
            # v1 стояла в «о семье», которую видят гости — убираем её оттуда
            old, c = bot.db.gget(guild, "panel:hub_member"), bot.ch(guild, "welcome")
            if old and c:
                try:
                    await (await c.fetch_message(int(old))).delete()
                except discord.HTTPException:
                    pass
            bot.db.gdel(guild, "panel:hub_member")
        await bot.panel_once(guild, "hub_member", "panel", repost, member, version=7)  # v2: канал только для семьи, v3: сдать экзамен, v7: без уровней/магазина/др
        await bot.panel_once(guild, "hub_lead", "botcmd", repost, lead, version=5)  # v3: ручной приём, срок
        for key, (_, _, mod) in CHANNEL_PANELS.items():
            if mod and not bot.feature(guild, mod):
                continue
            old = bot.db.gget(guild, f"panel:ch_{key}")
            msg = await bot.panel_once(guild, f"ch_{key}", key, repost,
                                       lambda k=key: _wrap(channel_panel(bot, guild, k)), version=1)
            if msg and str(msg.id) != str(old or ""):
                try:
                    await msg.pin(reason="Панель модуля")
                except discord.HTTPException:
                    pass

    async def refresh_channel(self, guild, key):
        """Обновить сводку на панели канала (после операции в казне, новой заявки и т.п.)."""
        mid, c = self.bot.db.gget(guild, f"panel:ch_{key}"), self.bot.ch(guild, key)
        if not mid or not c or key not in CHANNEL_PANELS:
            return
        try:
            await c.get_partial_message(int(mid)).edit(**channel_panel(self.bot, guild, key))
        except discord.HTTPException:
            pass


async def _wrap(x):
    return x


async def setup(bot):
    bot.add_dynamic_items(SectionButton, ActionButton)
    await bot.add_cog(Hub(bot))
