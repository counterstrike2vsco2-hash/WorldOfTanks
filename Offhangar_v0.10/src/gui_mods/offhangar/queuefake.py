# -*- coding: utf-8 -*-
"""Живая статистика очереди для окна "Ожидание боя".

Настоящий сервер раз в несколько секунд присылает клиенту состав очереди: сколько игроков ждёт бой и сколько из них
на лёгких, средних, тяжёлых танках, САУ и ПТ. В офлайне это никто не присылает, и окно показывает нули/пустоту.
Здесь числа правдоподобно "дышат": общее число плавно гуляет вокруг среднего значения, доли классов слегка меняются.

Экран ожидания (BattleQueue) открывает enterQueue(): events_dispatcher.loadBattleQueue(), через QUEUE_WAIT секунд запускается бой.
Кнопка "Выйти из очереди" на экране отменяет старт и возвращает в ангар.
Клиенту отправляется словарь {'players': N, 'classes': [лёгкие, средние, тяжёлые, САУ, ПТ]} (порядок VEHICLE_CLASS_INDICES).
Отправка идёт по таймеру и в ответ на запрос окна (CMD_REQ_QUEUE_INFO). Само останавливается, когда игрок перестаёт быть
Account (бой начался) или через MAX_SECONDS.
"""
import random

import BigWorld

from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_DEBUG

ENABLED = True
PLAYERS_MIN = 1200            # границы общего числа игроков в очереди
PLAYERS_MAX = 4200
QUEUE_WAIT = (4.0, 8.0)       # сколько секунд показывать экран ожидания до старта боя
INTERVAL = (0.8, 1.8)         # пауза между обновлениями, с
MAX_SECONDS = 180.0
# доли классов: лёгкие, средние, тяжёлые, САУ, ПТ (в порядке VEHICLE_CLASS_INDICES)
BASE_WEIGHTS = (0.15, 0.33, 0.30, 0.08, 0.14)

_S = {'pending': None, 'on': False, 'token': 0, 'players': 0, 'target': 0, 'w': list(BASE_WEIGHTS), 'since': 0.0, 'mode': None}


def _classOrder():
    """Порядок классов в массиве 'classes' (берём из клиента, если есть)."""
    try:
        from constants import VEHICLE_CLASS_INDICES
        idx = [VEHICLE_CLASS_INDICES.get(k) for k in ('lightTank', 'mediumTank', 'heavyTank', 'SPG', 'AT-SPG')]
        if None not in idx and sorted(idx) == list(range(5)):
            return idx
    except Exception:
        pass
    return [0, 1, 2, 3, 4]


def _step():
    """Один шаг случайного блуждания: общее число и доли классов."""
    st = _S
    if st['players'] <= 0:
        st['players'] = random.randint(PLAYERS_MIN, PLAYERS_MAX)
        st['target'] = st['players']
    # цель иногда смещается (как будто к очереди подключилась/отключилась волна игроков)
    if random.random() < 0.06:
        st['target'] = int(max(PLAYERS_MIN, min(PLAYERS_MAX, st['target'] * random.uniform(0.85, 1.18))))
    p = st['players']
    p += random.gauss(0.0, 0.012 * p) + 0.06 * (st['target'] - p)
    st['players'] = int(max(PLAYERS_MIN * 0.6, min(PLAYERS_MAX * 1.2, p)))
    w = [max(0.01, x * (1.0 + random.gauss(0.0, 0.012))) for x in st['w']]
    # притяжение к базовым долям, чтобы доли не уплывали
    w = [0.9 * x + 0.1 * b for x, b in zip(w, BASE_WEIGHTS)]
    s = sum(w)
    st['w'] = [x / s for x in w]


def _snapshot():
    st = _S
    n = st['players']
    counts = [int(round(n * x)) for x in st['w']]
    counts[2] += n - sum(counts)            # поправка округления: сумма классов = общему числу
    order = _classOrder()
    classes = [0] * 5
    for i, c in enumerate(counts):
        classes[order[i]] = max(0, c)
    return {'players': sum(classes), 'classes': classes}


def _deliver(info):
    """Отдать данные окну ожидания: BattleQueue слушает g_playerEvents.onQueueInfoReceived(randoms, companies, historical)."""
    from PlayerEvents import g_playerEvents
    g_playerEvents.onQueueInfoReceived(info, {}, {})
    if _S['mode'] != 'events':
        _S['mode'] = 'events'
        LOG_DEBUG('[QUEUE] queue info delivered via g_playerEvents.onQueueInfoReceived: %s' % (info,))
    return True


def _isLobbyPlayer():
    p = BigWorld.player()
    return p is not None and hasattr(p, 'requestQueueInfo')


def pushNow():
    """Один шаг + отправка (вызывается по таймеру и по запросу окна)."""
    if not ENABLED or not _S['on']:
        return
    try:
        _step()
        _deliver(_snapshot())
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _tick(token):
    st = _S
    if not st['on'] or token != st['token']:
        return
    try:
        if not _isLobbyPlayer() or BigWorld.time() - st['since'] > MAX_SECONDS:
            stop()
            return
        pushNow()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        BigWorld.callback(random.uniform(*INTERVAL), lambda t=token: _tick(t))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def start():
    if not ENABLED:
        return
    st = _S
    st['token'] += 1
    st['on'] = True
    st['since'] = BigWorld.time()
    st['players'] = 0
    st['w'] = list(BASE_WEIGHTS)
    st['mode'] = None
    token = st['token']
    LOG_DEBUG('[QUEUE] live queue info started')
    try:
        BigWorld.callback(0.2, lambda t=token: _tick(t))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def stop():
    if _S['on']:
        LOG_DEBUG('[QUEUE] live queue info stopped')
    _S['on'] = False
    _S['token'] += 1


_HOOKED = [False]


def _hookExit():
    """Кнопка "Выйти из очереди" на экране ожидания: пока идёт офлайн-ожидание, отменяем старт и возвращаемся в ангар."""
    if _HOOKED[0]:
        return
    try:
        from gui.Scaleform.daapi.view.lobby.BattleQueue import BattleQueue
        orig = BattleQueue.exitClick

        def exitClick(self):
            if _S['pending'] is not None:
                cancel()
                return
            return orig(self)
        BattleQueue.exitClick = exitClick
        _HOOKED[0] = True
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _waitThen(fn):
    """Подождать QUEUE_WAIT секунд (экран ожидания открыт) и вызвать fn; кнопка "Выйти" это отменяет."""
    _hookExit()
    start()
    token = _S['token']
    _S['pending'] = token
    wait = random.uniform(*QUEUE_WAIT)
    LOG_DEBUG('[QUEUE] waiting %.1fs on the queue screen before the battle' % wait)

    def _go():
        if _S['pending'] != token:
            return                      # отменили кнопкой "Выйти"
        _S['pending'] = None
        stop()
        fn()
    BigWorld.callback(wait, _go)


def waitThen(fn):
    """Для пути через CMD_ENQUEUE_RANDOM (экран уже открыт родным onEnqueued)."""
    if not ENABLED:
        fn()
        return
    try:
        _waitThen(fn)
    except Exception:
        LOG_CURRENT_EXCEPTION()
        fn()


def enterQueue(startFn):
    """Кнопка "В бой!": открыть экран ожидания и через QUEUE_WAIT секунд вызвать startFn (старт боя)."""
    if not ENABLED:
        startFn()
        return
    try:
        from gui.prb_control import events_dispatcher
        events_dispatcher.loadBattleQueue()
        LOG_DEBUG('[QUEUE] battle queue screen requested')
        _waitThen(startFn)
    except Exception:
        LOG_CURRENT_EXCEPTION()
        _S['pending'] = None
        stop()
        startFn()                           # экран не открылся - не блокируем бой


def cancel():
    _S['pending'] = None
    stop()
    try:
        from gui.prb_control import events_dispatcher
        events_dispatcher.loadHangar()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        p = BigWorld.player()
        if getattr(p, 'isInHistoricalQueue', False):
            from constants import QUEUE_TYPE
            p.onDequeued(QUEUE_TYPE.HISTORICAL)         # выходим из очереди исторических боёв
    except Exception:
        LOG_CURRENT_EXCEPTION()
    LOG_DEBUG('[QUEUE] cancelled by player, back to hangar')
