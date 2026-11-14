# -*- coding: utf-8 -*-
"""Лёгкий профайлер боя: сколько мс/с съедают ИИ ботов, физика ботов, сглаживание и лучи (wg_collideSegment).

Раз в REPORT секунд пишет в лог строку [PERF] (средний FPS, худший кадр, мс в секунду по секциям, число лучей в секунду).
Нужен, чтобы по логу видеть, что именно тормозит. Накладные расходы - порядка микросекунды на вызов.
"""
import time

import BigWorld

from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_NOTE

ENABLED = True
REPORT = 10.0

try:
    _clock = time.clock
except AttributeError:
    _clock = time.time

_ACC = {}                  # секция -> [секунд, вызовов]
_RAYS = [0]
_ST = {'on': False, 'token': 0, 'last': 0.0, 'frames': 0, 'worst': 0.0, 'lastFrame': 0.0, 'start': 0.0}
_ORIG = {}


def timed(name):
    """Декоратор: копит время и число вызовов секции."""
    def deco(fn):
        if not ENABLED:
            return fn

        def wrapper(*a, **k):
            t0 = _clock()
            try:
                return fn(*a, **k)
            finally:
                r = _ACC.get(name)
                if r is None:
                    r = _ACC[name] = [0.0, 0]
                r[0] += _clock() - t0
                r[1] += 1
        wrapper.__name__ = getattr(fn, '__name__', name)
        return wrapper
    return deco


def add(name, seconds, calls=1):
    r = _ACC.get(name)
    if r is None:
        r = _ACC[name] = [0.0, 0]
    r[0] += seconds
    r[1] += calls


def clock():
    return _clock()


import sys as _sys
_RC = {}


def _hookRays():
    if _ORIG or not hasattr(BigWorld, 'wg_collideSegment'):
        return
    orig = BigWorld.wg_collideSegment
    _ORIG['ray'] = orig

    def counted(*a, **k):
        _RAYS[0] += 1
        try:
            f = _sys._getframe(1).f_code
            key = f.co_name
            _RC[key] = _RC.get(key, 0) + 1
        except Exception:
            pass
        return orig(*a, **k)
    try:
        BigWorld.wg_collideSegment = counted
    except Exception:
        _ORIG.clear()


def _unhookRays():
    orig = _ORIG.pop('ray', None)
    if orig is not None:
        try:
            BigWorld.wg_collideSegment = orig
        except Exception:
            pass


def frame():
    """Вызывать каждый кадр (из сглаживания ботов): считает FPS и худший кадр."""
    st = _ST
    if not st['on']:
        return
    now = _clock()
    if st['lastFrame']:
        d = now - st['lastFrame']
        if d > st['worst']:
            st['worst'] = d
    st['lastFrame'] = now
    st['frames'] += 1


def _report(token):
    st = _ST
    if not st['on'] or token != st['token']:
        return
    try:
        now = _clock()
        span = max(0.001, now - st['start'])
        parts = []
        for name in sorted(_ACC):
            sec, calls = _ACC[name]
            parts.append('%s=%.1fms/s (%d/s, %.2fms each)' % (name, sec * 1000.0 / span, calls / span, sec * 1000.0 / max(1, calls)))
        mapName = '?'
        try:
            mapName = BigWorld.player().arena.arenaType.geometryName
        except Exception:
            pass
        LOG_NOTE('[PERF] map=%s fps=%.0f worstFrame=%.0fms rays=%.0f/s %s' % (
            mapName, st['frames'] / span, st['worst'] * 1000.0, _RAYS[0] / span, ' '.join(parts)))
        try:
            top = sorted(_RC.items(), key=lambda kv: -kv[1])[:7]
            LOG_NOTE('[PERF] rays by caller: %s' % ' '.join('%s=%.0f/s' % (k, v / span) for k, v in top))
        except Exception:
            pass
        _RC.clear()
        _ACC.clear()
        _RAYS[0] = 0
        st['frames'] = 0
        st['worst'] = 0.0
        st['start'] = now
        BigWorld.callback(REPORT, lambda: _report(token))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def start():
    if not ENABLED:
        return
    st = _ST
    st['token'] += 1
    st['on'] = True
    st['start'] = _clock()
    st['lastFrame'] = 0.0
    st['frames'] = 0
    st['worst'] = 0.0
    _ACC.clear()
    _RAYS[0] = 0
    _hookRays()
    token = st['token']
    BigWorld.callback(REPORT, lambda: _report(token))


def stop():
    _ST['on'] = False
    _ST['token'] += 1
    _unhookRays()
