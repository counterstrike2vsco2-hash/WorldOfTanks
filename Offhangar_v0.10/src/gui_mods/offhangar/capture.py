# -*- coding: utf-8 -*-
"""Захват базы (офлайн). Базы берутся из описания арены (те же точки, что рисует миникарта).

Игрок и союзники в круге вражеской базы без врагов набирают очки захвата (до 3 машин считаются, как в игре);
набрано 100 - победа по захвату. Враги в нашей базе без наших - так же приближают поражение. Пока в базе никого нет,
очки медленно спадают; если в базе есть и те и другие - захват стоит. Шкала захвата показывается родной панелью клиента
(arena.onTeamBasePointsUpdate / onTeamBaseCaptured), как в настоящем бою.
"""
import BigWorld
from debug_utils import LOG_CURRENT_EXCEPTION

from gui.mods.offhangar.logging import LOG_NOTE

TICK = 1.0
BASE_RADIUS = 50.0
CAPTURE_POINTS = 100.0
DECAY = 1.0                 # очков в секунду, когда в базе никого нет
END_DELAY = 3.0             # пауза между «база захвачена» и экраном результатов

_ACTIVE = False
_TOKEN = 0
_BASES = []                 # [{'key': 'enemy'|'own', 'id': baseID, 'x', 'z', 'pts', 'stopped'}]
_UI = {}                    # последнее отправленное в клиент состояние: (key, id) -> (points, stopped)


def _arenaType(av):
    try:
        import ArenaType
        return ArenaType.g_cache.get(av.arenaTypeID)
    except Exception:
        return None


def _loadBases(at):
    """Список баз из описания арены. Игрок - команда 1 (индекс 0)."""
    res = []
    try:
        pos = getattr(at, 'teamBasePositions', None)
        if pos:
            for idx, key in ((0, 'own'), (1, 'enemy')):
                for bid, v in pos[idx].items():
                    res.append({'key': key, 'id': int(bid), 'x': float(v[0]), 'z': float(v[1]), 'pts': 0.0, 'stopped': True})
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return res


def targetFor(team, x, z):
    """База, которую захватывает команда team (для ботов-захватчиков): ближайшая из баз противника. (x, z) или None."""
    key = 'enemy' if team == 1 else 'own'
    best = None
    for b in _BASES:
        if b['key'] != key:
            continue
        d = (b['x'] - x) ** 2 + (b['z'] - z) ** 2
        if best is None or d < best[0]:
            best = (d, b['x'], b['z'])
    return (best[1], best[2]) if best else None


def _vehicles():
    result = []
    try:
        from gui.mods.offhangar import bots
        for ai in list(bots._BOTS.values()):
            if getattr(ai, 'alive', False):
                try:
                    v = BigWorld.entity(ai._vid)
                    if v is not None and getattr(v, 'health', 0) > 0:
                        result.append(v)
                except Exception:
                    pass
        av = BigWorld.player()
        vid = getattr(av, 'playerVehicleID', 0)
        v = BigWorld.entity(vid) if vid else None
        if v is not None and getattr(v, 'health', 0) > 0 and v not in result:
            result.append(v)
    except Exception:
        pass
    return result


def _say(msg, key=None):
    LOG_NOTE('[CAPTURE] ' + msg)
    # Голос экипажа не играем: родная шкала захвата и так озвучивает события,
    # а подходящей реплики в 0.9.0 нет (раньше звучало «бой начинается»).


def _playerTeam():
    try:
        return int(getattr(BigWorld.player(), 'team', 1) or 1)
    except Exception:
        return 1


def _pushUI(b):
    """Родная шкала захвата клиента: team - владелец базы."""
    try:
        arena = BigWorld.player().arena
        pt = _playerTeam()
        team = pt if b['key'] == 'own' else (3 - pt)
        pts = int(b['pts'])
        state = (pts, bool(b['stopped']))
        k = (b['key'], b['id'])
        if _UI.get(k) == state:
            return
        _UI[k] = state
        arena.onTeamBasePointsUpdate(team, b['id'], pts, bool(b['stopped']))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _pushCaptured(b):
    try:
        pt = _playerTeam()
        team = pt if b['key'] == 'own' else (3 - pt)
        BigWorld.player().arena.onTeamBaseCaptured(team, b['id'])
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _finish(win):
    global _ACTIVE
    _ACTIVE = False
    try:
        from gui.mods.offhangar import endbattle
        endbattle._FORCE[0] = 'win' if win else 'lose'
        token = _TOKEN

        def _go():
            if token != _TOKEN:
                return
            endbattle.endBattle()
        BigWorld.callback(END_DELAY, _go)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _tick(token):
    if token != _TOKEN or not _ACTIVE:
        return
    BigWorld.callback(TICK, lambda: _tick(token))
    try:
        from gui.mods.offhangar import battle, bots
        if not battle._active() or battle.isPrebattle():
            return
        vehs = []
        for v in _vehicles():
            try:
                sp = battle._simplePhysicsByVehID.get(v.id)
                p = sp.pos if sp is not None else v.position
                vehs.append((float(p.x), float(p.z), bots.getTeam(v)))
            except Exception:
                pass
        for b in _BASES:
            ours = b['key'] == 'enemy'                 # наш захват вражеской базы
            allies = foes = 0
            for (x, z, team) in vehs:
                if (x - b['x']) ** 2 + (z - b['z']) ** 2 > BASE_RADIUS * BASE_RADIUS:
                    continue
                if team == bots.TEAM_ALLY:
                    allies += 1
                elif team == bots.TEAM_ENEMY:
                    foes += 1
            capturers, defenders = (allies, foes) if ours else (foes, allies)
            before = b['pts']
            if capturers and not defenders:
                b['pts'] = min(CAPTURE_POINTS, b['pts'] + min(3, capturers))
                b['stopped'] = False
            elif not capturers and not defenders:
                b['pts'] = max(0.0, b['pts'] - DECAY)
                b['stopped'] = True
            else:
                b['stopped'] = True                    # в базе и те и другие - захват стоит
            if int(before) // 25 < int(b['pts']) // 25 and b['pts'] < CAPTURE_POINTS:
                _say(('Захват вражеской базы: %d%%' if ours else 'Враги захватывают вашу базу: %d%%') % int(b['pts']))
            _pushUI(b)
            if b['pts'] >= CAPTURE_POINTS:
                _pushCaptured(b)
                _say('База захвачена!' if ours else 'Ваша база захвачена!', 'base_captured')
                _finish(ours)
                return
    except Exception:
        LOG_CURRENT_EXCEPTION()


def start():
    global _ACTIVE, _TOKEN, _BASES
    _TOKEN += 1
    _ACTIVE = True
    _UI.clear()
    _BASES = []
    av = BigWorld.player()
    at = _arenaType(av) if av is not None else None
    _BASES = _loadBases(at) if at is not None else []
    if not _BASES:
        LOG_NOTE('[CAPTURE] no bases in arena description; capture disabled on this arena')
        return
    LOG_NOTE('[CAPTURE] enabled: bases=%s radius=%s' % ([(b['key'], b['id'], round(b['x']), round(b['z'])) for b in _BASES], BASE_RADIUS))
    BigWorld.callback(TICK, lambda t=_TOKEN: _tick(t))


def stop():
    global _ACTIVE, _TOKEN, _BASES
    _ACTIVE = False
    _TOKEN += 1
    _BASES = []
