#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автономный Telegram-бот постановки задач в Битрикс24.
Работает на GitHub Actions (без браузера, без открытого Claude).
Ходит в Битрикс через входящий вебхук, в Telegram — через Bot API.

Гарантия «без потерь»: сообщение подтверждается в очереди Telegram
(getUpdates?offset=id+1) ТОЛЬКО после того, как по нему создан объект
в Битриксе / задан вопрос с кнопками / отправлена явная ошибка.

Секреты берутся из переменных окружения:
  BITRIX_WEBHOOK  — https://nebar.bitrix24.ru/rest/58/xxxx/
  TG_TOKEN        — токен Telegram-бота
  CHAT_ID         — 254803890
"""
import os, re, sys, json, time, base64, datetime, urllib.request, urllib.parse

BITRIX = os.environ["BITRIX_WEBHOOK"].rstrip("/") + "/"
TG_TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = int(os.environ.get("CHAT_ID", "254803890"))
TG = "https://api.telegram.org/bot" + TG_TOKEN + "/"

OWNER = 58            # Павел
DEPT = "635"          # отдел "Концерты" (подчинённые)
DISK_FOLDER = 104     # папка на Диске для вложений
CAL_SECTION = 120     # основной календарь
TZ = "+03:00"
BASE = "https://nebar.bitrix24.ru/company/personal/user/58/tasks/task/view/"

MONTHS = {"январ":1,"феврал":2,"март":3,"апрел":4,"ма":5,"май":5,"мая":5,"июн":6,"июл":7,
          "август":8,"сентябр":9,"октябр":10,"ноябр":11,"декабр":12}
WEEKDAYS = {"пн":0,"понедельник":0,"вт":1,"вторник":1,"ср":2,"среда":2,"среду":2,
            "чт":3,"четверг":3,"пт":4,"пятниц":4,"сб":5,"суббот":5,"вс":6,"воскресен":6}
# короткое имя -> варианты в именительном
NAME_VARIANTS = {
    "ане":["анна","аня"],"аня":["анна","аня"],"анне":["анна"],
    "насте":["анастасия"],"настю":["анастасия"],"анастасии":["анастасия"],
    "юле":["юлия"],"юлю":["юлия"],"юлии":["юлия"],
    "ксюше":["ксения"],"ксюшу":["ксения"],"ксении":["ксения"],
    "кате":["екатерина"],"катю":["екатерина"],"екатерине":["екатерина"],"екатерину":["екатерина"],
    "ларисе":["лариса"],"ларису":["лариса"],
    "владу":["владимир","владислав"],"владиславу":["владислав"],"владимиру":["владимир"],
    "лёше":["алексей"],"леше":["алексей"],
    "саше":["александр","александра"],
    "гоше":["георгий","гоша"],"николаю":["николай"],"артёму":["артём","артем"],
    "семёну":["семен","семён"],"антону":["антон"],"альбине":["альбина"],"елене":["елена"],
}

# ------------------------------------------------------------------ HTTP
def http(url, data=None, headers=None):
    if data is not None and not isinstance(data, (bytes, str)):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if data is not None and "Content-Type" not in (headers or {}):
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def bx(method, params=None):
    """Вызов Битрикс REST через вебхук."""
    body = json.dumps(params or {}).encode()
    req = urllib.request.Request(BITRIX + method + ".json", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def bx_list(method, params):
    """Постраничный сбор (user.get / tasks.task.list)."""
    out, start = [], 0
    for _ in range(25):
        p = dict(params); p["start"] = start
        res = bx(method, p)
        chunk = res.get("result", [])
        if isinstance(chunk, dict) and "tasks" in chunk:
            chunk = chunk["tasks"]
        if not chunk:
            break
        out.extend(chunk)
        nxt = res.get("next")
        if nxt is None:
            break
        start = nxt
    return out

# ------------------------------------------------------------------ Telegram
def tg(method, payload):
    return http(TG + method, payload)

def send(text, reply_markup=None):
    p = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return tg("sendMessage", p)

def confirm(update_id):
    """Подтвердить обработку апдейта (снять из очереди)."""
    http(TG + "getUpdates?offset=%d&timeout=0&allowed_updates=%s"
         % (update_id + 1, urllib.parse.quote('["message","callback_query"]')))

def answer_cb(cb_id, text="Принято ✅"):
    tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

def edit(msg_id, text):
    tg("editMessageText", {"chat_id": CHAT_ID, "message_id": msg_id, "text": text})

def task_link(tid):
    return BASE + str(tid) + "/"

# ------------------------------------------------------------------ users
_USERS = {"all": [], "team": []}
def load_users():
    rows = bx_list("user.get", {"FILTER": {"ACTIVE": "Y"}})
    people = []
    for u in rows:
        dep = [str(x) for x in (u.get("UF_DEPARTMENT") or [])]
        people.append({
            "id": u["ID"],
            "first": (u.get("NAME") or "").strip(),
            "last": (u.get("LAST_NAME") or "").strip(),
            "name": ((u.get("NAME") or "") + " " + (u.get("LAST_NAME") or "")).strip(),
            "pos": u.get("WORK_POSITION") or "",
            "dep": dep,
        })
    _USERS["all"] = people
    _USERS["team"] = [p for p in people if DEPT in p["dep"]]

def nominatives(word):
    w = word.lower().strip(" ,.")
    if w in NAME_VARIANTS:
        return NAME_VARIANTS[w]
    # грубое снятие дательного падежа
    cands = {w}
    for suf, rep in (("е",""),("у",""),("ю",""),("ы",""),("и","")):
        if w.endswith(suf):
            cands.add(w[:-1])
            cands.add(w[:-1] + "а")
            cands.add(w[:-1] + "я")
    return list(cands)

def candidates(name_word):
    names = [n.lower() for n in nominatives(name_word)]
    match = lambda p: p["first"].lower() in names
    pool = [p for p in _USERS["team"] if match(p)]
    if not pool:
        pool = [p for p in _USERS["all"] if match(p)]
    return pool

def apply_hint(pool, hint):
    if not hint:
        return pool
    h = hint.lower().strip(" ()")
    sub = [p for p in pool if p["last"].lower().startswith(h)
           or h in p["pos"].lower() or h in p["name"].lower()]
    return sub or pool

def fuzzy(word):
    q = word.lower(); p3 = q[:3]
    return [p for p in _USERS["all"]
            if p["first"].lower().startswith(p3) or q in p["name"].lower()
            or q in p["pos"].lower()][:16]

# ------------------------------------------------------------------ dates
def now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))

def parse_deadline(text):
    """Вернёт (iso_or_None, human_text)."""
    t = text.lower()
    hh, mm = 18, 0
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)   # время только с двоеточием
    if m and 0 <= int(m.group(1)) <= 23:
        hh, mm = int(m.group(1)), int(m.group(2))
    base = now()
    target = None
    # dd.mm(.yyyy)
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else base.year
        if y < 100: y += 2000
        try: target = base.replace(year=y, month=mo, day=d)
        except ValueError: target = None
    # "до N <месяц>" / "N <месяц>"
    if target is None:
        m = re.search(r"(\d{1,2})\s*([а-я]{3,})", t)
        if m:
            d = int(m.group(1)); mon_word = m.group(2)
            for k, v in MONTHS.items():
                if mon_word.startswith(k):
                    y = base.year
                    try:
                        target = base.replace(month=v, day=d)
                        if target.date() < base.date():
                            target = target.replace(year=y+1)
                    except ValueError: pass
                    break
    # день недели
    if target is None:
        for k, wd in sorted(WEEKDAYS.items(), key=lambda x:-len(x[0])):
            if k in t:
                delta = (wd - base.weekday()) % 7
                if delta == 0: delta = 7
                target = base + datetime.timedelta(days=delta)
                break
    if target is None and "завтра" in t:
        target = base + datetime.timedelta(days=1)
    if target is None and "сегодня" in t:
        target = base
    if target is None and ("конец недел" in t or "конце недел" in t):
        delta = (4 - base.weekday()) % 7  # ближайшая пятница
        target = base + datetime.timedelta(days=delta)
    if target is None:
        return None, ""
    target = target.replace(hour=hh, minute=mm, second=0, microsecond=0)
    iso = target.strftime("%Y-%m-%dT%H:%M:00") + TZ
    human = target.strftime("%d.%m.%Y %H:%M")
    return iso, human

# ------------------------------------------------------------------ parsing message
ADDR_PREFIX = re.compile(r"^\s*(задача|поставь задачу|поставь|задачу)\b[:\s]*", re.I)
SELF = {"мне","себе","я","мной"}

def looks_like_addressee(line):
    w = line.strip().strip(":").split()
    return 1 <= len(w) <= 4 and re.match(r"^[А-ЯЁ][а-яё]", line.strip())

def parse_task(text):
    """Грубый разбор задачи: -> dict(title, addressee, hint, deadline_iso, deadline_h, desc)."""
    raw = [l.strip() for l in text.split("\n") if l.strip()]
    if not raw:
        return None
    addressee, hint = None, None
    lines = list(raw)

    def extract_hint(s):
        h = None
        mm = re.search(r"\(([^)]+)\)", s)
        if mm:
            h = mm.group(1); s = re.sub(r"\([^)]*\)", "", s).strip()
        toks = s.split()
        first = toks[0] if toks else ""
        # доп. уточнение — второе слово (фамилия/инициал)
        if h is None and len(toks) >= 2:
            h = " ".join(toks[1:])
        return first, h

    # 1) инлайн "Адресат: остальное" в первой строке
    m0 = re.match(r"^\s*([^:\n]{1,40}?)\s*:\s*(.+)$", raw[0])
    if m0:
        left = ADDR_PREFIX.sub("", m0.group(1)).strip()
        low = left.lower().strip(" ,.")
        words = left.split()
        if low in SELF:
            addressee = "self"; lines[0] = m0.group(2).strip()
        elif 1 <= len(words) <= 3 and re.match(r"^[А-ЯЁ][а-яё]", left) \
                and not re.match(r"^(описание|desc|ддл|дедлайн|срок)$", low):
            addressee, hint = extract_hint(left); lines[0] = m0.group(2).strip()

    # 2) отдельная строка-адресат
    if addressee is None:
        for i, l in enumerate(lines):
            s = ADDR_PREFIX.sub("", l).strip()
            low = s.lower().strip(" ,.")
            if low in SELF:
                addressee = "self"; lines.pop(i); break
            s2 = re.sub(r"\([^)]*\)", "", s).strip()
            if re.match(r"^[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яёА-ЯЁ.]*){0,2}$", s2) and len(s2.split()) <= 3:
                addressee, hint = extract_hint(s); lines.pop(i); break
    # дедлайн
    deadline_iso, deadline_h = None, ""
    dl_line_idx, dl_dedicated = None, False
    for i, l in enumerate(lines):
        if re.match(r"^\s*(ддл|дедлайн|срок)\b", l, re.I):
            deadline_iso, deadline_h = parse_deadline(l)
            dl_line_idx, dl_dedicated = i, True
            break
    if deadline_iso is None:
        # дата внутри содержательной строки — извлекаем, но строку НЕ удаляем
        for i, l in enumerate(lines):
            iso, h = parse_deadline(l)
            if iso:
                deadline_iso, deadline_h, dl_line_idx = iso, h, i
                break
    if dl_line_idx is not None and dl_dedicated:
        lines.pop(dl_line_idx)
    # описание: строка-метка "Описание:" и всё, что после неё
    desc_parts, keep, in_desc = [], [], False
    for l in lines:
        if not in_desc and re.match(r"^\s*(описание задачи|описание|desc)\b", l, re.I):
            rest = re.sub(r"^\s*(описание задачи|описание|desc)\s*[:\-]?\s*", "", l, flags=re.I)
            if rest: desc_parts.append(rest)
            in_desc = True
        elif in_desc:
            desc_parts.append(l)
        else:
            keep.append(l)
    desc = "\n".join(desc_parts).strip()
    title = " / ".join(keep) if keep else (lines[0] if lines else raw[0])
    title = title.strip(" /")
    return {"addressee": addressee, "hint": hint, "title": title,
            "deadline_iso": deadline_iso, "deadline_h": deadline_h, "desc": desc}

# ------------------------------------------------------------------ actions
def dedup(title, resp):
    try:
        res = bx("tasks.task.list", {"filter": {"%TITLE": title, "RESPONSIBLE_ID": resp},
                                     "order": {"ID": "DESC"}, "select": ["ID", "CREATED_DATE"]})
        for t in res.get("result", {}).get("tasks", []):
            return t["id"]
    except Exception:
        pass
    return None

def create_task(title, resp, who, deadline_iso, deadline_h, desc=""):
    dup = dedup(title, resp)
    if dup:
        tid = dup
    else:
        fields = {"TITLE": title, "RESPONSIBLE_ID": resp, "CREATED_BY": OWNER}
        if desc: fields["DESCRIPTION"] = desc
        if deadline_iso: fields["DEADLINE"] = deadline_iso
        res = bx("tasks.task.add", {"fields": fields})
        tid = res["result"]["task"]["id"]
    send("✅ Задача создана: %s\nИсполнитель: %s\nДедлайн: %s\n%s"
         % (title, who, deadline_h or "—", task_link(tid)))
    return tid

def question(name_word, cands, payload, update_id):
    kb = {"inline_keyboard":
          [[{"text": c["name"] + (" — " + c["pos"] if c["pos"] else ""),
             "callback_data": "u:" + str(c["id"])}] for c in cands[:16]]
          + [[{"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    body = ("\n\n---PAYLOAD---\nTITLE: %s\nDEADLINE: %s\nDESC: %s"
            % (payload["title"], payload.get("deadline_iso") or "-", payload.get("desc") or "-"))
    send("❓ Кого имел в виду под «%s»? Нажми нужного (или ответь reply с фамилией):%s"
         % (name_word, body), reply_markup=kb)
    confirm(update_id)

def resolve_and_create(p, update_id):
    """p = результат parse_task. Создаёт задачу или задаёт вопрос."""
    if p["addressee"] == "self":
        create_task(p["title"], OWNER, "Мне (Паша Ф.)", p["deadline_iso"], p["deadline_h"], p["desc"])
        confirm(update_id); return
    if not p["addressee"]:
        # исполнитель НЕ распознан — НЕ ставим наугад, спрашиваем
        send("❓ Кому поставить задачу «%s»? Ответь на это сообщение (reply) именем исполнителя "
             "(или напиши «мне»).\n\n---PAYLOAD---\nTITLE: %s\nDEADLINE: %s\nDESC: %s"
             % (p["title"], p["title"], p["deadline_iso"] or "-", p["desc"] or "-"))
        confirm(update_id); return
    pool = apply_hint(candidates(p["addressee"]), p["hint"])
    if len(pool) == 1:
        c = pool[0]
        create_task(p["title"], c["id"], c["name"], p["deadline_iso"], p["deadline_h"], p["desc"])
        confirm(update_id)
    elif len(pool) > 1:
        question(p["addressee"], pool, p, update_id)
    else:
        fz = fuzzy(p["addressee"])
        if fz:
            question(p["addressee"], fz, p, update_id)
        else:
            send("❓ Не нашёл «%s». Ответь на это сообщение (reply) правильным именем/фамилией.\n\n"
                 "---PAYLOAD---\nTITLE: %s\nDEADLINE: %s\nDESC: %s"
                 % (p["addressee"], p["title"], p["deadline_iso"] or "-", p["desc"] or "-"))
            confirm(update_id)

# ------------------------------------------------------------------ callbacks & replies
def parse_payload(src):
    d = {}
    m = re.search(r"---PAYLOAD---(.*)$", src, re.S)
    if not m:
        return None
    for line in m.group(1).strip().split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip().upper()] = v.strip()
    return d

def handle_callback(cb):
    data = cb.get("data", "")
    message = cb.get("message") or {}
    msg_id = message.get("message_id")
    src = message.get("text", "") or ""
    try:
        answer_cb(cb["id"])
    except Exception:
        pass
    if data == "cancel":
        edit(msg_id, "✖️ Отменено")
        confirm(cb["update_id"]); return
    uid = int(data.split(":", 1)[1])
    who = next((p["name"] for p in _USERS["all"] if str(p["id"]) == str(uid)), "выбранный")
    edit(msg_id, "✅ Выбран: %s\nСоздаю…" % who)
    pay = parse_payload(src) or {}
    title = pay.get("TITLE", "Задача")
    dl = pay.get("DEADLINE"); dl = None if dl in (None, "-", "") else dl
    dl_h = ""
    if dl:
        try: dl_h = datetime.datetime.fromisoformat(dl).strftime("%d.%m.%Y %H:%M")
        except Exception: dl_h = dl
    desc = pay.get("DESC"); desc = "" if desc in (None, "-", "") else desc
    create_task(title, uid, who, dl, dl_h, desc)
    confirm(cb["update_id"])

def handle_reply(msg):
    """Ответ reply на вопрос: msg['reply_to_message']['text'] содержит PAYLOAD."""
    src = msg["reply_to_message"]["text"]
    pay = parse_payload(src) or {}
    name_word = msg.get("text", "").split()[0] if msg.get("text") else ""
    pool = apply_hint(candidates(name_word), None)
    # уточнение фамилией/должностью из полного текста ответа
    pool = apply_hint(pool, " ".join(msg.get("text", "").split()[1:]) or None)
    if len(pool) == 1:
        c = pool[0]
        title = pay.get("TITLE", "Задача")
        dl = pay.get("DEADLINE"); dl = None if dl in (None, "-", "") else dl
        dl_h = ""
        if dl:
            try: dl_h = datetime.datetime.fromisoformat(dl).strftime("%d.%m.%Y %H:%M")
            except Exception: dl_h = dl
        desc = pay.get("DESC"); desc = "" if desc in (None, "-", "") else desc
        create_task(title, c["id"], c["name"], dl, dl_h, desc)
        confirm(msg["update_id"])
    else:
        p = {"addressee": name_word, "hint": None, "title": pay.get("TITLE", "Задача"),
             "deadline_iso": pay.get("DEADLINE") if pay.get("DEADLINE") not in (None,"-","") else None,
             "deadline_h": "", "desc": pay.get("DESC") if pay.get("DESC") not in (None,"-","") else ""}
        cands = pool if pool else fuzzy(name_word)
        if cands:
            question(name_word, cands, p, msg["update_id"])
        else:
            send("❓ Всё ещё не нашёл. Пришли фамилию точнее.")
            confirm(msg["update_id"])

# ------------------------------------------------------------------ main loop
def main():
    load_users()
    res = http(TG + "getUpdates?timeout=0&allowed_updates=%s"
               % urllib.parse.quote('["message","callback_query"]'))
    ups = res.get("result", [])
    print("BOT updates=%d types=%s" % (len(ups),
          [ ("cb" if "callback_query" in u else "msg") for u in ups ]), file=sys.stderr)
    ups.sort(key=lambda x: x["update_id"])
    for up in ups:
        try:
            if "callback_query" in up:
                cb = up["callback_query"]; cb["update_id"] = up["update_id"]
                cb_chat = (cb.get("message", {}) or {}).get("chat", {}).get("id")
                print("BOT callback update=%s data=%s from=%s chat=%s" %
                      (up["update_id"], cb.get("data"),
                       (cb.get("from") or {}).get("id"), cb_chat), file=sys.stderr)
                if cb_chat is not None and cb_chat != CHAT_ID:
                    confirm(up["update_id"]); continue
                handle_callback(cb)
                continue
            msg = up.get("message")
            if not msg or msg.get("chat", {}).get("id") != CHAT_ID:
                confirm(up["update_id"]); continue
            msg["update_id"] = up["update_id"]
            text = msg.get("text") or msg.get("caption") or ""
            if msg.get("reply_to_message") and "---PAYLOAD---" in (msg["reply_to_message"].get("text") or ""):
                handle_reply(msg); continue
            if not text or text.strip().startswith("/"):
                send("Не понял. Напиши: кому и что сделать, к какому сроку.")
                confirm(up["update_id"]); continue
            p = parse_task(text)
            if not p or not p["title"]:
                send("Не понял. Напиши: кому и что сделать, к какому сроку.")
                confirm(up["update_id"]); continue
            resolve_and_create(p, up["update_id"])
        except Exception as e:
            try:
                send("⚠️ Не смог обработать: «%s». Поставь вручную или повтори."
                     % (str((up.get("message", {}) or {}).get("text", ""))[:80]))
                confirm(up["update_id"])
            except Exception:
                # даже ошибку не отправили — НЕ подтверждаем, сообщение останется
                print("FATAL for update", up["update_id"], repr(e), file=sys.stderr)

if __name__ == "__main__":
    main()
