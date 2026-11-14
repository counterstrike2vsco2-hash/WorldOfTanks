# -*- coding: utf-8 -*-
"""Утопление: танк, полностью ушедший под воду (вода выше башни), через несколько секунд тонет.
Работает для игрока и ботов. Гибель идёт тем же путём, что и от выстрела (причина DROWNING)."""
import BigWorld
import Math
from debug_utils import LOG_CURRENT_EXCEPTION

from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE

ENABLED = True
TICK = 0.5                 # период проверки, сек
DEPTH_FACTOR = 0.75        # тонет, если вода выше этой доли высоты башни (мелкая вода на картах редко выше башни)
DROWN_AFTER = 7.0          # сколько секунд под водой до гибели
ATTACK_REASON_DROWNING = 5

_TOKEN = [0]
_UNDER = {}                # vehicleID -> время, с которого танк под водой
_WARNED = set()
_DIAG = [0]
_PLAYER_LOG = [0.0]


def start():
    _TOKEN[0] += 1
    _UNDER.clear()
    _WARNED.clear()
    _DIAG[0] = 0
    if ENABLED:
        token = _TOKEN[0]
        BigWorld.callback(2.0, lambda: _tick(token))


def stop():
    _TOKEN[0] += 1
    _UNDER.clear()
    _WARNED.clear()


def _waterDepth(v):
    """Глубина воды над точкой опоры танка, м (0 - воды нет)."""
    p = v.position
    d = BigWorld.wg_collideWater(Math.Vector3(p.x, p.y, p.z), Math.Vector3(p.x, p.y + 15.0, p.z))
    if d is None or d < 0.0:
        return 0.0
    return float(d)


def _topHeight(v):
    try:
        td = v.typeDescriptor
        return float(td.chassis['hullPosition'][1] + td.hull['turretPositions'][0][1]) + 0.6
    except Exception:
        return 2.2


def _tick(token):
    if token != _TOKEN[0]:
        return
    BigWorld.callback(TICK, lambda: _tick(token))
    now = BigWorld.time()
    try:
        from gui.mods.offhangar import battle as off_battle
        if off_battle.isPrebattle():
            return
    except Exception:
        pass
    seen = set()
    for e in BigWorld.entities.values():
        try:
            if not hasattr(e, 'typeDescriptor') or not hasattr(e, 'health') or e.health <= 0:
                continue
            if not getattr(e, 'isStarted', True):
                continue
            try:
                depth = _waterDepth(e)
            except Exception:
                if _DIAG[0] < 3:
                    _DIAG[0] += 1
                    LOG_CURRENT_EXCEPTION()
                continue
            vid = e.id
            try:
                if depth > 0.0 and getattr(BigWorld.player(), 'playerVehicleID', None) == vid and now - _PLAYER_LOG[0] > 3.0:
                    _PLAYER_LOG[0] = now
                    LOG_NOTE('[DROWN] player water depth=%.2f limit=%.2f under=%s' % (
                        depth, _topHeight(e) * DEPTH_FACTOR, vid in _UNDER))
            except Exception:
                pass
            if depth > _topHeight(e) * DEPTH_FACTOR:
                seen.add(vid)
                t0 = _UNDER.setdefault(vid, now)
                if vid not in _WARNED:
                    _WARNED.add(vid)
                    _warn(e, True)
                if now - t0 >= DROWN_AFTER:
                    _drown(e)
            elif vid in _UNDER:
                _UNDER.pop(vid, None)
                if vid in _WARNED:
                    _WARNED.discard(vid)
                    _warn(e, False)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    for vid in list(_UNDER):
        if vid not in seen:
            _UNDER.pop(vid, None)
            _WARNED.discard(vid)


def _warn(v, on):
    try:
        av = BigWorld.player()
        if getattr(av, 'playerVehicleID', None) != v.id:
            return
        from constants import VEHICLE_MISC_STATUS, DROWN_WARNING_LEVEL
        level = DROWN_WARNING_LEVEL.DANGER if on else DROWN_WARNING_LEVEL.SAFE
        av.updateVehicleMiscStatus(v.id, VEHICLE_MISC_STATUS.VEHICLE_DROWN_WARNING, level, None)
    except Exception:
        pass


def _drown(v):
    try:
        from gui.mods.offhangar import battle as off_battle
        from gui.mods.offhangar import bots as off_bots
        if v.health <= 0:
            return
        off_battle.freezeCorpse(v)
        prev = v.health
        v.health = 0
        try:
            v.set_health(prev)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            v.onHealthChanged(0, v.id, ATTACK_REASON_DROWNING)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            av = BigWorld.player()
            if getattr(av, 'playerVehicleID', None) == v.id:
                av.updateVehicleHealth(0, True)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            off_bots.onVehicleKilled(v, v.id)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        _UNDER.pop(v.id, None)
        LOG_NOTE('[DROWN] vehicle %s drowned' % v.id)
    except Exception:
        LOG_CURRENT_EXCEPTION()
