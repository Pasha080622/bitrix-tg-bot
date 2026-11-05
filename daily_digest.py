#!/usr/bin/env python3
"""Daily Telegram digest for the webhook owner's personal ПФ planner column."""
import argparse
import datetime as dt
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request

MSK = dt.timezone(dt.timedelta(hours=3))
MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')
BOARD = 'https://nebar.bitrix24.ru/company/personal/user/58/tasks/?F_STATE=sV180'
BASE = 'https://nebar.bitrix24.ru/company/personal/user/58/tasks/task/view/'


def post(url, payload, retry=False):
    attempts = 3 if retry else 1
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, json.dumps(payload).encode(),
                                         {'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.load(response)
            if data.get('error') or data.get('ok') is False:
                raise RuntimeError('Remote API rejected the request')
            return data
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt + 1 == attempts:
                raise RuntimeError('Remote API connection failed') from None
            time.sleep(2 * (attempt + 1))


def field(task, name, default=None):
    return task.get(name, task.get(name.upper(), default))


def deadline(task):
    raw = field(task, 'deadline')
    if not raw:
        return None
    value = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    return (value.replace(tzinfo=MSK) if value.tzinfo is None else value).astimezone(MSK)


def date_label(value):
    return f'{value.day} {MONTHS[value.month - 1]}'


def task_line(task, now, overdue):
    title = html.escape(str(field(task, 'title', 'Без названия'))[:500])
    task_id = int(field(task, 'id'))
    due = deadline(task)
    if overdue:
        days = (now.date() - due.date()).days
        age = f'{days} дн.' if days else 'сегодня'
        timing = f'просрочено {age} (срок был {date_label(due)}, {due:%H:%M})'
    else:
        label = 'сегодня' if due.date() == now.date() else 'завтра'
        timing = f'{label}, {due:%H:%M}'
    creator = field(task, 'creator', {}) or {}
    name = creator.get('name') or f'ID {field(task, "createdBy", "—")}'
    return f'— <a href="{BASE}{task_id}/">{title}</a> — {timing}\nПостановщик: {html.escape(str(name))}'


def build_messages(tasks, now):
    active = [t for t in tasks if str(field(t, 'status')) not in ('5', '7')]
    late, soon, other = [], [], []
    tomorrow = now.date() + dt.timedelta(days=1)
    for task in active:
        due = deadline(task)
        if due and due < now:
            late.append(task)
        elif due and due.date() <= tomorrow:
            soon.append(task)
        else:
            other.append(task)
    key = lambda t: (deadline(t), int(field(t, 'id')))
    late.sort(key=key)
    soon.sort(key=key)
    blocks = [f'📋 <b>Сводка по колонке ПФ на {date_label(now)} {now.year}</b>']
    for heading, group, overdue in [('🔴 Просрочено', late, True), ('🟡 Сегодня / завтра', soon, False)]:
        blocks.append(f'<b>{heading} ({len(group)}):</b>')
        blocks.extend(task_line(t, now, overdue) for t in group)
        if not group:
            blocks.append('Нет задач.')
    blocks.extend([f'⚪ Остальные: {len(other)} задач', f'Всего в ПФ: {len(active)} задач',
                   f'📌 <a href="{BOARD}">Открыть доску ПФ</a>'])
    chunks, current = [], ''
    for block in blocks:
        candidate = (current + '\n\n' + block).strip()
        if len(candidate) > 3800 and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks, {'total': len(active), 'overdue': len(late), 'soon': len(soon), 'other': len(other)}


def collect(webhook):
    def bx(method, payload):
        return post(webhook.rstrip('/') + '/' + method + '.json', payload, retry=True)
    result = bx('task.stages.get', {'entityId': 0})['result']
    stages = list(result.values()) if isinstance(result, dict) else result
    matches = [s for s in stages if str(s.get('TITLE', s.get('title', ''))).strip().upper() == 'ПФ']
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one personal planner column named ПФ')
    stage_id = int(matches[0].get('ID', matches[0].get('id')))
    tasks, start, seen = [], 0, set()
    while True:
        if start in seen:
            raise RuntimeError('Repeated pagination offset')
        seen.add(start)
        response = bx('tasks.task.list', {
            'filter': {'STAGE_ID': stage_id, '!REAL_STATUS': [5, 7]},
            'select': ['ID', 'TITLE', 'DEADLINE', 'CREATED_BY', 'STATUS'],
            'order': {'ID': 'ASC'}, 'start': start,
        })
        page = response.get('result', {}).get('tasks', [])
        tasks.extend(page)
        if response.get('next') is None:
            break
        start = int(response['next'])
    if len({str(field(t, 'id')) for t in tasks}) != len(tasks):
        raise RuntimeError('Duplicate task IDs in API results')
    return stage_id, tasks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    stage_id, tasks = collect(os.environ['BITRIX_WEBHOOK'])
    messages, counts = build_messages(tasks, dt.datetime.now(MSK))
    # Logs contain counts and IDs for verification, never task titles, tokens or messages.
    print(json.dumps({'stage_id': stage_id, 'counts': counts,
                      'task_ids': [int(field(t, 'id')) for t in tasks],
                      'message_count': len(messages)}, ensure_ascii=False))
    if args.send:
        url = 'https://api.telegram.org/bot' + os.environ['TG_TOKEN'] + '/sendMessage'
        for text in messages:
            post(url, {'chat_id': int(os.environ['CHAT_ID']), 'text': text,
                       'parse_mode': 'HTML', 'disable_web_page_preview': True})
        print('DIGEST sent successfully')
    else:
        print('DIGEST preview only; no Telegram messages sent')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('DIGEST failed; no success confirmation. Check API access and planner column.', file=sys.stderr)
        sys.exit(1)
