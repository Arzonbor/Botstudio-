"""
Разбор сценария TajStudio. Не зависит ни от aiogram, ни от Blender — можно проверять отдельно:
    python scenario.py            (самопроверка)

Что делает:
  * parse(text)        -> (scenes, notes): сцены с репликами и описания персонажей
  * build_cast(...)    -> {имя: описание 3D-героя}: пол, возраст, животное, цвета, одежда, причёска…
                          (это то, по чему Blender сам «рисует» персонажа, если картинка не загружена)
  * infer_env(text)    -> место / время суток / погода для декораций
  * emotion_of(...)    -> эмоция реплики (из скобок или по тексту), жесты
  * pick_voice(...)    -> подбор голоса по полу героя, чтобы разные герои звучали по-разному

Формат сценария:
    [Персонаж: Маша — девочка 8 лет, рыжие косички, красное платье]     (необязательно)
    [Персонаж: Барсик — рыжий кот]
    [Сцена: лес, утро]
    Маша: (радостно, машет рукой) Привет, Барсик!
    Рассказчик: Так началось их приключение.
"""
import hashlib
import re

NARRATORS = {"рассказчик", "автор", "диктор", "за кадром", "narrator", "ровӣ", "ровй"}

# ───────────────────────────── разбор ─────────────────────────────
_SCENE_RE = re.compile(r"^\[?\s*(?:сцена|scene|локация|место|саҳна)\s*[:\-–—]\s*(.+?)\]?$", re.I)
_CHAR_RE = re.compile(r"^\[?\s*(?:персонаж|герой|character|шахс)\s*[:]\s*([^\-–—=:\]]{1,30}?)\s*[\-–—=:]\s*(.+?)\]?$", re.I)
_LINE_RE = re.compile(r"^([^:\[\]()]{1,30}):\s*(?:\(([^)]*)\))?\s*(.+)$")


def _clean_who(s: str) -> str:
    s = re.sub(r"[.$#\[\]/\\\n\r\t]", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:30]


def parse(text: str) -> tuple[list[dict], dict]:
    """Возвращает (scenes, notes).
    scenes = [{"setting": str, "env": {...}, "lines": [{who, emotion, mood, actions, energy, text, narrator}]}]
    notes  = {имя персонажа: свободное описание из «[Персонаж: …]»}
    Имена приводятся к первой встретившейся записи (Маша/маша/МАША -> Маша)."""
    scenes, notes, cur = [], {}, None
    canon: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        m = _SCENE_RE.match(line)
        if m:
            cur = {"setting": m.group(1).strip(), "lines": []}
            scenes.append(cur)
            continue
        m = _CHAR_RE.match(line)
        if m:
            who = _clean_who(m.group(1))
            if who:
                canon.setdefault(who.casefold(), who)
                notes[canon[who.casefold()]] = m.group(2).strip()
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        who = _clean_who(m.group(1))
        if not who:
            continue
        narrator = who.casefold() in NARRATORS
        who = canon.setdefault(who.casefold(), who)
        if cur is None:
            cur = {"setting": "студия", "lines": []}
            scenes.append(cur)
        tag, body = (m.group(2) or "").strip(), m.group(3).strip()
        mood, actions, energy = emotion_of(tag, body)
        cur["lines"].append({"who": who, "emotion": tag or "neutral", "mood": mood, "actions": actions,
                             "energy": energy, "text": body, "narrator": narrator})
    for sc in scenes:
        sc["env"] = infer_env(sc["setting"])
    # описания, привязанные не к тому регистру имени
    notes = {canon.get(k.casefold(), k): v for k, v in notes.items()}
    return scenes, notes


# ───────────────────────────── эмоции и жесты ─────────────────────────────
_MOODS = [
    ("laugh", r"смеётся|смеется|хохоч|ха-?ха|ахах|смех|laugh|ҳа-?ҳа|механдад"),
    ("joy", r"радост|весел|счастлив|ура\b|здорово|круто|отлично|прекрасн|замечательн|улыб|спасибо|люблю|"
            r"happy|great|yay|joy|хурсанд|хуш"),
    ("sad", r"груст|печал|плач|плак|жаль|прости|к сожалению|скучаю|одинок|sad|cry|sorry|ғамгин|гирья"),
    ("angry", r"зл[ойаяы]|сердит|ярост|гнев|бесит|ненавиж|злюсь|angry|mad\b|furious|хашм|ғазаб"),
    ("surprise", r"удив|ого\b|ничего себе|неужели|ух ты|вот это да|wow|surpris|ҳайрон|аҷоб"),
    ("fear", r"страшн|испуг|боюсь|помогите|ужас|scared|afraid|fear|тарс"),
    ("think", r"думаю|хм+|размышл|интересно|задумч|think|hmm|фикр"),
    ("ask", r"вопрос|спрашива|curious|любопытн"),
]
_ACTIONS = [
    ("wave", r"машет|помахал|приветств|wave|даст мерзад|дасти"),
    ("nod", r"кивает|кивнул|nod"),
    ("shake", r"качает головой|мотает головой|shake"),
    ("point", r"показыва|указыва|point|нишон"),
    ("jump", r"прыга|подпрыгив|jump|ҷаҳид"),
    ("bow", r"кланя|поклон|bow"),
    ("dance", r"танцу|dance|рақс"),
    ("walk_in", r"входит|заходит|подходит|идёт|идет|бежит|walks|runs|меояд"),
    ("walk_out", r"уходит|убегает|leaves|меравад"),
]
_GREET = re.compile(r"^(привет|здравствуй|здорово|добрый|доброе|hello|hi\b|hey|салом|assalom|ассалом)", re.I)
_BYE = re.compile(r"^(пока\b|до свидания|до встречи|прощай|bye|хайр|худо ҳофиз)", re.I)


def emotion_of(tag: str, text: str) -> tuple[str, list[str], float]:
    """(настроение, жесты, энергия 0..1). Скобки автора важнее, чем догадка по тексту."""
    tag_l, text_l = (tag or "").lower(), (text or "").lower()
    mood = next((k for k, rx in _MOODS if re.search(rx, tag_l)), None)
    if mood is None:
        mood = next((k for k, rx in _MOODS if k != "ask" and re.search(rx, text_l)), None)
    if mood is None:
        t = text.strip()
        mood = "ask" if t.endswith("?") else ("joy" if t.endswith("!") and len(t) < 60 and not tag_l else "neutral")
    actions = [k for k, rx in _ACTIONS if re.search(rx, tag_l)]
    if not actions:
        if _GREET.search(text_l) or _BYE.search(text_l):
            actions.append("wave")
        elif re.match(r"^(да|конечно|согласен|yes|ҳа)\b", text_l):
            actions.append("nod")
        elif re.match(r"^(нет|не хочу|no\b|не)\b", text_l):
            actions.append("shake")
        elif re.search(r"\b(смотри|посмотри|вот|look|ана)\b", text_l):
            actions.append("point")
    ex = text.count("!")
    energy = min(1.0, 0.35 + 0.25 * ex + (0.25 if mood in ("joy", "laugh", "angry", "fear", "surprise") else 0)
                 - (0.2 if mood in ("sad", "think") else 0))
    return mood, actions, round(max(0.15, energy), 2)


# ───────────────────────────── место действия ─────────────────────────────
_PLACES = [
    ("space", r"космос|планет|звёзд|звезд|ракет|space|луна\b|кайҳон"),
    ("beach", r"пляж|берег|море|океан|beach|sea|ocean|соҳил|баҳр"),
    ("snow", r"зим|снег|сугроб|снеж|winter|snow|зимистон|барф"),
    ("desert", r"пустын|дюн|desert|биёбон"),
    ("mountain", r"гор[аыуе]\b|горах|горн|скал|mountain|кӯҳ"),
    ("castle", r"замок|дворец|корол|castle|palace|қалъа"),
    ("school", r"школ|класс|урок|school|мактаб"),
    ("kitchen", r"кухн|kitchen|ошхона"),
    ("office", r"офис|office|контор"),
    ("room", r"дом[еа]?\b|комнат|квартир|спальн|гостин|room|house|home|хона|ҳуҷра|больниц|кабинет"),
    ("city", r"город|улиц|площад|магазин|рынок|city|street|town|шаҳр|кӯча|бозор"),
    ("village", r"деревн|село|кишлак|village|деҳа"),
    ("farm", r"ферм|двор|огород|farm|ҳавлӣ"),
    ("forest", r"лес|дерев|чащ|тропинк|forest|wood|ҷангал"),
    ("park", r"парк|сад|поляна|луг|поле|park|garden|meadow|боғ|марғзор"),
]
_TIMES = [
    ("night", r"ноч|полночь|night|шаб"),
    ("evening", r"вечер|закат|сумерк|evening|sunset|бегоҳ"),
    ("morning", r"утр|рассвет|заря|morning|sunrise|субҳ|саҳар"),
    ("day", r"день|днём|днем|полдень|day|noon|рӯз"),
]


def infer_env(setting: str) -> dict:
    s = (setting or "").lower()
    place = next((k for k, rx in _PLACES if re.search(rx, s)), "park")
    time_ = next((k for k, rx in _TIMES if re.search(rx, s)), "night" if place == "space" else "day")
    weather = "snow" if place == "snow" or re.search(r"снег|snow|барф", s) else (
        "rain" if re.search(r"дожд|ливень|rain|борон", s) else "clear")
    return {"place": place, "time": time_, "weather": weather}


# ───────────────────────────── герои ─────────────────────────────
MALE_NAMES = {
    "рустам", "фарход", "сино", "далер", "бахром", "эмомали", "джамшед", "умар", "алишер", "саид", "мирзо",
    "фируз", "комрон", "зафар", "шохрух", "нодир", "парвиз", "бобур", "дониёр", "абдулло", "хасан", "хусейн",
    "хусаин", "мухаммад", "исмоил", "сухроб", "рахмон", "достон", "азиз", "анвар", "алик", "тимур", "амир",
    "саша", "женя", "миша", "никита", "илья", "лёва", "лева", "гоша", "дима", "коля", "вася", "петя", "ваня",
    "алёша", "алеша", "серёжа", "сережа", "андрюша", "слава", "митя", "гена", "паша", "лёша", "леша", "костя",
    "жора", "боря", "толя", "юра", "стёпа", "степа", "данила", "кирилл", "артём", "артем", "максим", "иван",
    "алик", "алекс", "том", "джон", "майк", "пётр", "петр", "сергей", "андрей", "дмитрий", "олег", "игорь",
}
FEMALE_NAMES = {
    "мадина", "зарина", "нигина", "фарангис", "гулнора", "парвина", "дилрабо", "ситора", "лола", "малика",
    "рухшона", "нилуфар", "шахноза", "мехрангез", "зулфия", "мохира", "сабрина", "гулрухсор", "мунира",
    "азиза", "сарвиноз", "дилноза", "ширин", "мафтуна", "любовь", "любовь", "ольга", "ляйсан", "алиса",
    "мария", "анна", "лиза", "елена", "наташа", "катя", "оля", "таня", "света", "юля", "маша", "даша",
    "соня", "ира", "аня", "настя", "вера", "надежда", "аленка", "алёнка", "золушка", "белоснежка",
}
_MALE_END = ("бек", "джон", "шо", "мир", "дин", "ддин", "ёр", "ер", "ов", "ев", "ин", "ой", "ий", "ей", "ан", "ил", "ик")
_FEMALE_END = ("а", "я", "ия", "ова", "ева", "ина", "ая", "ская", "хон", "ой")

# животные: слово (корень) -> (вид, пол)
_ANIMALS = [
    (r"кошк|киск|кошеч", "cat", "f"), (r"\bкот\b|котен|котён|котик|барсик|мурзик|\bcat\b|гурба", "cat", "m"),
    (r"собак|щен|пёс\b|\bпес\b|пёсик|шарик|бобик|dog\b|сагча", "dog", "m"),
    (r"зайц|заяц|кролик|крольч|rabbit|bunny|харгӯш", "rabbit", "m"),
    (r"медвед|мишк|bear|хирс", "bear", "m"),
    (r"лис[аыу]\b|лисиц|лисён|fox|рӯбоҳ", "fox", "f"),
    (r"волк|wolf|гург", "wolf", "m"),
    (r"мыш|mouse|муш", "mouse", "m"),
    (r"свин|поросён|поросен|хрюш|pig|хук", "pig", "m"),
    (r"лев\b|львён|lion|шер\b", "lion", "m"),
    (r"лягуш|жаб|frog|қурбоққа", "frog", "f"),
]
# профессии и роли -> переопределения облика
_ROLES = [
    (r"дед мороз|дедушка мороз", {"age": "elder", "gender": "m", "top": (0.75, 0.05, 0.05), "beard": "long",
                                  "hat": "santa", "hair_color": (0.95, 0.95, 0.95), "outfit": "robe"}),
    (r"волшебник|маг\b|чародей|wizard", {"hat": "wizard", "beard": "long", "outfit": "robe", "top": (0.25, 0.2, 0.6),
                                          "age": "elder", "gender": "m", "hair_color": (0.85, 0.85, 0.9)}),
    (r"фея|fairy", {"gender": "f", "outfit": "dress", "top": (0.8, 0.5, 0.9), "wings": True}),
    (r"принцесс|princess", {"gender": "f", "outfit": "dress", "top": (0.9, 0.45, 0.65), "hat": "crown"}),
    (r"принц\b|prince", {"gender": "m", "hat": "crown", "top": (0.2, 0.3, 0.7)}),
    (r"король|king", {"gender": "m", "hat": "crown", "beard": "short", "outfit": "robe", "top": (0.6, 0.1, 0.15),
                      "age": "adult"}),
    (r"королев|queen", {"gender": "f", "hat": "crown", "outfit": "dress", "top": (0.55, 0.1, 0.5)}),
    (r"доктор|врач|медсестр|doctor|nurse|духтур", {"outfit": "coat", "top": (0.95, 0.95, 0.97), "accessory": "stethoscope"}),
    (r"учител|преподавател|teacher|муаллим", {"accessory": "glasses", "outfit": "suit", "top": (0.35, 0.3, 0.3)}),
    (r"повар|шеф-повар|chef|cook", {"hat": "chef", "outfit": "coat", "top": (0.97, 0.97, 0.97)}),
    (r"полицейск|милиционер|police", {"hat": "cap", "top": (0.12, 0.2, 0.45), "outfit": "suit"}),
    (r"пират|pirate", {"hat": "pirate", "beard": "short", "top": (0.6, 0.15, 0.1), "accessory": "eyepatch"}),
    (r"космонавт|астронавт|astronaut", {"hat": "helmet", "outfit": "suit", "top": (0.95, 0.95, 0.95)}),
    (r"солдат|воин|рыцарь|knight|soldier", {"hat": "helmet", "outfit": "suit", "top": (0.45, 0.5, 0.55)}),
    (r"строител|рабочий|builder", {"hat": "hardhat", "top": (0.95, 0.6, 0.1)}),
    (r"пожарн|fireman", {"hat": "hardhat", "top": (0.8, 0.12, 0.1), "outfit": "coat"}),
    (r"бабушк|grandma|модарбузург", {"age": "elder", "gender": "f", "hair_color": (0.85, 0.85, 0.88),
                                       "hair_style": "bun", "accessory": "glasses", "outfit": "dress"}),
    (r"дедушк|дед\b|grandpa|падарбузург", {"age": "elder", "gender": "m", "hair_color": (0.85, 0.85, 0.88),
                                             "beard": "short", "accessory": "glasses"}),
    (r"мама|мать|мамочк|mother|модар", {"gender": "f", "age": "adult"}),
    (r"папа|отец|папочк|father|падар", {"gender": "m", "age": "adult"}),
    (r"мальчик|boy\b|писар", {"gender": "m", "age": "child"}),
    (r"девочк|girl\b|духтарак", {"gender": "f", "age": "child"}),
    (r"малыш|ребёнок|ребенок|baby|кудак", {"age": "child"}),
    (r"подросток|teen", {"age": "teen"}),
    (r"старик|старый|старушк|пожил|elder", {"age": "elder"}),
    (r"робот|robot", {"robot": True}),
]
_HAIR_WORDS = [
    (r"рыж|ginger|red hair", (0.78, 0.3, 0.1)), (r"блонд|светлые волосы|светловолос|русые|blond", (0.9, 0.75, 0.35)),
    (r"чёрные волосы|черные волосы|темноволос|брюнет|black hair|брюнетк", (0.04, 0.03, 0.03)),
    (r"седые|седой|седина|gray hair|grey hair", (0.85, 0.85, 0.88)),
    (r"каштанов|шатен|brown hair|тёмно-русые", (0.3, 0.17, 0.08)),
    (r"розов", (0.95, 0.45, 0.7)), (r"синие волосы|голубые волосы|blue hair", (0.25, 0.5, 0.9)),
]
_STYLE_WORDS = [
    (r"косичк|косы|коса\b|braid", "braids"), (r"хвост|ponytail", "ponytail"), (r"пучок|bun\b", "bun"),
    (r"кудряв|курчав|curly|афро", "curly"), (r"лыс|bald", "bald"), (r"длинные волосы|длинноволос|long hair", "long"),
    (r"короткие волосы|стрижк|short hair|ёжик|ежик", "short"),
]
_COLORS = [
    (r"красн|red", (0.85, 0.08, 0.08)), (r"син|blue", (0.1, 0.25, 0.8)), (r"голуб", (0.4, 0.7, 0.95)),
    (r"зелён|зелен|green", (0.12, 0.6, 0.2)), (r"жёлт|желт|yellow", (0.98, 0.82, 0.12)),
    (r"оранж|orange", (0.98, 0.5, 0.08)), (r"фиолет|purple", (0.5, 0.2, 0.7)), (r"розов|pink", (0.95, 0.45, 0.7)),
    (r"чёрн|черн|black", (0.05, 0.05, 0.06)), (r"бел[ыоаяе]|white", (0.96, 0.96, 0.96)),
    (r"серый|серая|серое|gray|grey", (0.5, 0.5, 0.52)), (r"коричнев|brown", (0.4, 0.22, 0.1)),
    (r"бирюз", (0.1, 0.75, 0.75)),
]
_FUR_COLORS = [
    (r"рыж", (0.88, 0.45, 0.12)), (r"бел[ыоаяе]|white|снежн", (0.95, 0.95, 0.95)),
    (r"чёрн|черн|black", (0.07, 0.07, 0.08)), (r"сер[ыоаяе]|gr[ae]y", (0.55, 0.55, 0.58)),
    (r"бур[ыоаяе]|коричнев|brown", (0.4, 0.24, 0.12)), (r"жёлт|желт|золот|yellow", (0.95, 0.75, 0.25)),
    (r"розов|pink", (0.95, 0.65, 0.75)), (r"пёстр|пестр|полосат|tabby", (0.65, 0.45, 0.25)),
]
_FUR_DEFAULT = {"cat": (0.85, 0.5, 0.2), "dog": (0.7, 0.5, 0.28), "rabbit": (0.88, 0.88, 0.9),
                "bear": (0.42, 0.26, 0.13), "fox": (0.9, 0.42, 0.1), "wolf": (0.5, 0.5, 0.55),
                "mouse": (0.6, 0.6, 0.62), "pig": (0.95, 0.65, 0.7), "lion": (0.88, 0.65, 0.25),
                "frog": (0.3, 0.7, 0.25)}

SKINS = [(0.96, 0.78, 0.65), (0.88, 0.66, 0.5), (0.78, 0.55, 0.4), (0.62, 0.42, 0.3), (0.45, 0.29, 0.2)]
HAIRS = [(0.05, 0.03, 0.03), (0.25, 0.14, 0.07), (0.45, 0.28, 0.12), (0.8, 0.6, 0.25), (0.7, 0.28, 0.1)]
TOPS = [(0.2, 0.45, 0.8), (0.85, 0.25, 0.25), (0.25, 0.65, 0.4), (0.95, 0.7, 0.15), (0.6, 0.35, 0.75),
        (0.95, 0.5, 0.2), (0.2, 0.65, 0.7)]
BOTTOMS = [(0.15, 0.2, 0.35), (0.25, 0.22, 0.2), (0.3, 0.3, 0.38), (0.4, 0.28, 0.18)]
EYES = [(0.25, 0.15, 0.08), (0.2, 0.4, 0.7), (0.2, 0.5, 0.3), (0.1, 0.08, 0.06)]


def _h(name: str, salt: str = "") -> int:
    return int(hashlib.md5((name.casefold() + salt).encode()).hexdigest()[:8], 16)


def _first(rows, text, default=None):
    for rx, val in rows:
        if re.search(rx, text):
            return val
    return default


def guess_gender(name: str, text: str = "") -> str:
    n = name.casefold().strip()
    t = text.casefold()
    if re.search(r"\b(он|мальчик|мужчина|парень|дед|дедушка|папа|отец|брат|сын|король|принц|\bhe\b|boy|man)\b", t):
        return "m"
    if re.search(r"\b(она|девочка|женщина|девушка|бабушка|мама|мать|сестра|дочь|королева|принцесса|she|girl|woman)\b", t):
        return "f"
    words = n.split()
    for w in words:
        if w in MALE_NAMES:
            return "m"
        if w in FEMALE_NAMES:
            return "f"
    last = words[-1] if words else n
    if last.endswith(_MALE_END[:6]) or last.endswith(("ик", "ан", "ил", "ей", "ий")):
        return "m"
    if last.endswith(("а", "я")) and not last.endswith(("ча", "да")):
        return "f"
    if last.endswith(_FEMALE_END[2:]):
        return "f"
    return "m" if _h(n, "g") % 2 == 0 else "f"


def _color_near(text: str, noun_rx: str, rows=None):
    """Цвет, названный рядом с существительным: «красное платье», «платье красное»."""
    rows = rows or _COLORS
    for rx, val in rows:
        if re.search(rf"(?:{rx})\w*\s+(?:{noun_rx})|(?:{noun_rx})\w*\s+(?:{rx})", text):
            return val
    return None


def infer_avatar(name: str, note: str = "", spoken: str = "") -> dict:
    """Описание героя. note — из «[Персонаж: …]», spoken — все реплики героя (для подсказок вроде «я — доктор»)."""
    nm = name.casefold()
    text = f"{nm}. {note}".casefold()
    sp = spoken.casefold()
    spec = {"name": name, "species": "human", "gender": None, "age": "adult", "height": 1.0}

    # животное
    for rx, sp_name, g in _ANIMALS:
        if re.search(rx, text):
            spec["species"], spec["gender"] = sp_name, g
            if sp_name in ("cat", "fox", "frog") and re.search(r"\bкот\b|котик|барсик|мурзик|лис(?!иц)\w*\b", text):
                spec["gender"] = "m"
            break
    animal = spec["species"] != "human"

    # роли
    over = {}
    for rx, o in _ROLES:
        if re.search(rx, text) or (not animal and re.search(rf"(?:я|мы|меня зовут)\s+(?:{rx})", sp)):
            for k, v in o.items():
                over.setdefault(k, v)
    for k in ("gender", "age"):
        if k in over:
            spec[k] = over[k]
    if spec["gender"] is None:
        spec["gender"] = guess_gender(name, note)
    if re.search(r"\b(\d{1,2})\s*(?:лет|года|год)\b", text):
        years = int(re.search(r"\b(\d{1,2})\s*(?:лет|года|год)\b", text).group(1))
        spec["age"] = "child" if years < 12 else "teen" if years < 18 else "elder" if years >= 60 else "adult"
    age = spec["age"]
    spec["height"] = {"child": 0.72, "teen": 0.88, "adult": 1.0, "elder": 0.95}[age] * (0.97 + (_h(nm, "h") % 7) / 100)
    if animal:
        spec["height"] *= {"mouse": 0.6, "frog": 0.7, "rabbit": 0.85, "cat": 0.85, "bear": 1.15, "lion": 1.1,
                           "wolf": 1.05, "dog": 0.9}.get(spec["species"], 0.95)

    # цвета и внешность (детерминированно по имени, если не заданы явно)
    skin = SKINS[_h(nm, "s") % 3]
    if re.search(r"смугл|тёмная кожа|темная кожа|dark skin", text):
        skin = SKINS[3]
    spec["skin"] = skin
    spec["eye_color"] = EYES[_h(nm, "e") % len(EYES)]
    spec["hair_color"] = over.get("hair_color") or _first(_HAIR_WORDS, text) or HAIRS[_h(nm, "c") % len(HAIRS)]
    if age == "elder" and "hair_color" not in over and not _first(_HAIR_WORDS, text):
        spec["hair_color"] = (0.82, 0.82, 0.85)
    style = _first(_STYLE_WORDS, text)
    if not style:
        style = (("braids", "ponytail", "long", "bun") if spec["gender"] == "f" and age != "elder" else
                 ("short", "short", "curly", "short") if age != "elder" else ("short",))[_h(nm, "st") % (4 if age != "elder" and spec["gender"] == "f" else 3 if age != "elder" else 1)]
        if age == "child" and spec["gender"] == "f":
            style = ("braids", "ponytail", "long")[_h(nm, "st") % 3]
    spec["hair_style"] = over.get("hair_style") or style
    if spec["gender"] == "m" and age != "child" and re.search(r"бород|beard", text):
        over.setdefault("beard", "short")
    if re.search(r"усы|усат|mustache", text):
        over.setdefault("beard", "mustache")
    if re.search(r"очки|очкарик|glasses", text):
        over.setdefault("accessory", "glasses")
    if re.search(r"шляп|hat\b", text):
        over.setdefault("hat", "tophat")
    if re.search(r"кепк|cap\b|бейсболк", text):
        over.setdefault("hat", "cap")
    if re.search(r"корон|crown", text):
        over.setdefault("hat", "crown")
    spec["beard"] = over.get("beard", "none")
    spec["hat"] = over.get("hat", "none")
    spec["accessory"] = over.get("accessory", "none")
    spec["wings"] = bool(over.get("wings"))
    spec["robot"] = bool(over.get("robot"))

    # одежда
    outfit = over.get("outfit") or ("dress" if spec["gender"] == "f" and age != "elder" and
                                    _h(nm, "o") % 3 != 0 else "casual")
    if re.search(r"платье|юбк|dress|skirt", text):
        outfit = "dress"
    elif re.search(r"костюм|пиджак|suit", text):
        outfit = "suit"
    elif re.search(r"куртк|пальто|coat|jacket", text):
        outfit = "coat"
    spec["outfit"] = outfit
    top = _color_near(text, r"платье|рубашк|футболк|куртк|костюм|пальто|кофт|свитер|майк|форм|dress|shirt|jacket|suit|coat")
    spec["top"] = over.get("top") if "top" in over else (top or TOPS[_h(nm, "t") % len(TOPS)])
    if top and "top" in over:
        spec["top"] = top
    spec["bottom"] = _color_near(text, r"штаны|брюки|джинс|юбк|шорты|pants|jeans|skirt") or BOTTOMS[_h(nm, "b") % len(BOTTOMS)]
    spec["shoes"] = (0.12, 0.08, 0.06)

    # шерсть
    if animal:
        fur = _first(_FUR_COLORS, re.sub(r"\b(кот|кошка|собака|лиса|медведь|заяц)\w*", "", text)) \
            or _FUR_DEFAULT[spec["species"]]
        spec["fur"] = fur
        spec["outfit"] = over.get("outfit", "casual") if _h(nm, "dr") % 2 else "casual"
        spec["hair_style"] = "bald"
        spec["beard"] = "none"
    return _jsonable(spec)


def _jsonable(o):
    if isinstance(o, tuple):
        return [round(float(x), 3) if isinstance(x, float) else x for x in o]
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    return o


def build_cast(scenes: list[dict], notes: dict | None = None) -> dict:
    """{имя: описание героя} для всех говорящих персонажей (рассказчик — без тела)."""
    notes = notes or {}
    spoken: dict[str, list[str]] = {}
    for sc in scenes:
        for ln in sc["lines"]:
            if not ln.get("narrator"):
                spoken.setdefault(ln["who"], []).append(ln["text"])
    cast = {}
    for who, texts in spoken.items():
        note = next((v for k, v in notes.items() if k.casefold() == who.casefold()), "")
        cast[who] = infer_avatar(who, note, " ".join(texts)[:600])
    # два героя не должны выглядеть как близнецы: если цвета одежды совпали — сдвигаем
    seen = set()
    for who, spec in cast.items():
        key = tuple(spec["top"])
        if key in seen:
            spec["top"] = list(TOPS[(TOPS.index(tuple(key)) + 2) % len(TOPS)]) if tuple(key) in TOPS else spec["top"]
        seen.add(tuple(spec["top"]))
    return cast


# ───────────────────────────── голоса ─────────────────────────────
def pick_voice(spec: dict, voices: list[dict], taken: list[str], cyrillic: bool = True) -> str:
    """Выбирает ключ голоса по полу героя; старается не повторять голоса, уже занятые другими героями.
    voices — список из tts.voice_catalog: [{key, name, desc}]. Пусто — вернёт ''."""
    if not voices:
        return ""
    want_f = spec.get("gender") == "f"
    want = ("женск", "female") if want_f else ("мужск", "male")

    def lang_ok(v):
        k = v["key"]
        if k.startswith("piper:"):
            return k.split(":", 1)[1].startswith("ru_") == cyrillic
        return True

    pool = [v for v in voices if lang_ok(v)] or voices
    gendered = [v for v in pool if any(w in (v.get("desc") or "").lower() for w in want)] or pool
    free = [v for v in gendered if v["key"] not in taken] or [v for v in pool if v["key"] not in taken] or gendered
    return free[_h(spec.get("name", ""), "v") % len(free)]["key"]


def is_cyrillic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return not letters or sum("а" <= c.lower() <= "я" or c.lower() in "ёғӣқӯҳҷ" for c in letters) / len(letters) > 0.5


# ───────────────────────────── самопроверка ─────────────────────────────
if __name__ == "__main__":
    demo = """# пример
[Персонаж: Маша — девочка 8 лет, рыжие косички, красное платье]
[Персонаж: Барсик — рыжий кот]
[Сцена: лес, утро]
Маша: (радостно, машет рукой) Привет, Барсик!
маша: Куда ты идёшь?
Барсик: Мяу! Ничего себе, как тут красиво!
Рассказчик: Так началось их приключение.
[Сцена: дом, вечер]
Дед Рустам: (грустно) Я так скучаю по вам.
"""
    sc, nt = parse(demo)
    assert len(sc) == 2 and sc[0]["env"] == {"place": "forest", "time": "morning", "weather": "clear"}, sc[0]["env"]
    assert sc[1]["env"]["place"] == "room" and sc[1]["env"]["time"] == "evening"
    assert [l["who"] for l in sc[0]["lines"]] == ["Маша", "Маша", "Барсик", "Рассказчик"]
    assert sc[0]["lines"][0]["mood"] == "joy" and "wave" in sc[0]["lines"][0]["actions"]
    assert sc[0]["lines"][1]["mood"] == "ask"
    assert sc[0]["lines"][2]["mood"] == "surprise"
    assert sc[0]["lines"][3]["narrator"] and sc[1]["lines"][0]["mood"] == "sad"
    assert set(nt) == {"Маша", "Барсик"}
    cast = build_cast(sc, nt)
    assert set(cast) == {"Маша", "Барсик", "Дед Рустам"}, cast.keys()
    m, b, d = cast["Маша"], cast["Барсик"], cast["Дед Рустам"]
    assert m["gender"] == "f" and m["age"] == "child" and m["hair_style"] == "braids" and m["outfit"] == "dress"
    assert m["hair_color"][0] > 0.7 and m["top"][0] > 0.8 and m["top"][1] < 0.2, m
    assert b["species"] == "cat" and b["gender"] == "m" and b["fur"][0] > 0.8
    assert d["age"] == "elder" and d["gender"] == "m" and d["beard"] != "none"
    print("scenario.py: OK")
    for k, v in cast.items():
        print(k, v)
