# -*- coding: utf-8 -*-
"""Обучение из клиента 0.9.0 по клавише (по умолчанию F10 в ангаре).

В клиенте обучение уже есть целиком: подсказки в ангаре (tutorial, "offbattle") и учебный бой (arena.guiType == TUTORIAL).
Мод его отключал (isTutorialEnabled=False, tutorialsCompleted=1). Теперь по клавише:
  1. включаем обучение (isTutorialEnabled=True), сбрасываем "пройдено" и кэш обучения этого аккаунта;
  2. запускаем родной загрузчик (tutorial.loader.g_loader.restart()) - он сам показывает окно обучения и ведёт по главам;
  3. когда обучение просит "учебный бой" (player.enqueueTutorial -> CMD_ENQUEUE_TUTORIAL), показываем очередь и запускаем бой:
     карта "tutorial" (если есть в клиенте, иначе случайная), тип арены TUTORIAL (по нему клиент включает боевые подсказки),
     врагов 2, союзников 0.
Клавиша: tutorial_key=F10 в offline_user.txt (любая F1-F12 или буква; none - выключить).
"""
import random

import BigWorld
import AccountCommands

from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE
from gui.mods.offhangar.server import BASE_REQUESTS

ENABLED = True
DEFAULT_KEY = 'F10'
QUEUE_WAIT = (3.0, 5.0)
TUT_ENEMIES = 3          # учебный бой: три неподвижные мишени (#battle_tutorial:bot_1..3) - на них построены главы обучения
TUT_ALLIES = 0
# Мишени учебного боя (#battle_tutorial:bot_1..3): бот 1 - лёгкий (первый выстрел), бот 2 - бронированный (первый выстрел не пробивает -
# глава "попробуй уничтожить" -> автоприцел -> уничтожь), бот 3 - средний. Подбираются по уровню/классу из списка машин клиента.
TUT_BOTS = {1: ('lightTank', 1, 2), 2: ('heavyTank', 5, 6), 3: ('mediumTank', 3, 4)}
TUT_VEHICLE = 'ussr:T-34-85_training'   # учебная машина (tutorial_vehicle=... в offline_user.txt; none - брать машину из ангара)
# Фиксированная награда за учебный бой (не зависит от урона/фрагов/результата). Значения из сообщения обучения; переопределяются в offline_user.txt:
# tutorial_credits, tutorial_xp, tutorial_free_xp, tutorial_gold.
TUT_REWARD = {'credits': 6000, 'xp': 0, 'free_xp': 300, 'gold': 0}
# Конец учебного боя: после уничтожения последней мишени бой не закрывается, пока диктор обучения говорит.
TUT_END_MIN_DELAY = 6.0     # минимум с момента уничтожения последней мишени, с (обучение успевает начать фразу)
TUT_END_QUIET = 2.0         # столько секунд подряд не должно быть звуков обучения, чтобы закрыть бой, с
TUT_END_MAX_WAIT = 20.0     # дольше этого не ждём, с
TUT_VOICE_FALLBACK = 7.0    # звук без объекта (длину не узнать) считаем играющим столько секунд, с
TUT_VEHICLE_FALLBACK = 'ussr:T-34-85'   # если в клиенте нет учебной машины (в 0.9.0 её может не быть) - обычный Т-34-85
# Учебные машины мишеней (есть в клиенте 0.9.5). Если в клиенте их нет - мишени подбираются по классу/уровню (TUT_BOTS выше).
TUT_BOT_VEHICLES = {1: 'germany:PzIII_training', 2: 'germany:PzVIB_Tiger_II_training', 3: 'germany:PzV_training'}
# Из spaces/00_tank_tutorial/*.chunk: AutoBotSpawnPoint (x, y, z, поворот) и SimplePatrolNode пути 1 (бот 3 ездит между ними).
TUT_MAP_BOTS = {
    '00_tank_tutorial': {
        'points': {1: (-49.700, 2.978, 71.000, 2.6180),      # Pz III, стоит
                   2: (165.190, 8.300, 92.351, -1.4379),     # Tiger II, стоит
                   3: (195.776, 6.336, -269.422, 0.0)},      # Panther, патруль
        'patrol': {3: [(141.210, -267.020), (245.711, -270.662)]},
    },
}

_S = {'key': None, 'hooked': False, 'token': 0, 'last': 0.0}


def _player():
    try:
        return BigWorld.player()
    except Exception:
        return None


def _isLobby():
    p = _player()
    return p is not None and hasattr(p, 'prb_createTraining') and hasattr(p, 'stats')


def _readKey():
    import Keys
    from gui.mods.offhangar import _constants as _c
    raw = str(_c.readOfflineSettings().get('tutorialkey', DEFAULT_KEY) or DEFAULT_KEY).strip().upper()
    if raw in ('NONE', '0', 'OFF', 'NO'):
        return None
    return getattr(Keys, 'KEY_' + raw, None)


def _prepareRun(p):
    """Подготовка к запуску обучения: включить, сбросить 'пройдено' и кэш обучения аккаунта."""
    ss = getattr(p, 'serverSettings', None)
    if isinstance(ss, dict):
        ss['isTutorialEnabled'] = True
    cache = getattr(p.stats, '_Stats__cache', None)
    if isinstance(cache, dict):
        cache['tutorialsCompleted'] = 0          # награды обучения "не получены" - обучение предложит все главы
    from tutorial import TutorialCache
    from tutorial.settings import TUTORIAL_SETTINGS
    c = TutorialCache.TutorialCache(p.name, TUTORIAL_SETTINGS.getSettings(TUTORIAL_SETTINGS.DEFAULT_SETTINGS).space)
    c.setFinished(False)
    c.setAfterBattle(False)
    c.setRefused(True)                            # "отказался" + restart = родной загрузчик сразу запускает обучение
    c.write()


def startTutorial():
    """Запустить родное обучение (вызывается клавишей)."""
    p = _player()
    if not _isLobby():
        return
    LOG_NOTE('[TUTOR] starting the native tutorial')
    try:
        _prepareRun(p)
        from tutorial import loader
        if loader.g_loader is None:
            LOG_NOTE('[TUTOR] tutorial loader is not initialised (IS_TUTORIAL_ENABLED?)')
            return
        loader.g_loader.restart()
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchTutorialButton():
    """Кнопка 'Боевое обучение' в ангаре есть всегда (isTutorialEnabled=True), но обучение само не всплывает при входе
    (OffbattleStartReqs.isEnabled без restart -> False); по кнопке (loader.restart) обучение запускается заново, как по F10."""
    try:
        from tutorial.control.offbattle.context import OffbattleStartReqs
        if not getattr(OffbattleStartReqs, '_oh_btn', False):
            orig = OffbattleStartReqs.isEnabled

            def isEnabled(self, *a, **k):
                try:
                    if not getattr(self._ctx, 'restart', False):
                        return False        # автозапуск при входе в ангар не нужен
                except Exception:
                    pass
                return orig(self, *a, **k)

            OffbattleStartReqs.isEnabled = isEnabled
            OffbattleStartReqs._oh_btn = True
        from tutorial import loader as _ld
        cls = _ld._TutorialLoader
        if not getattr(cls, '_oh_btn', False):
            origRestart = cls.restart

            def restart(self, afterBattle=False):
                try:
                    p = _player()
                    if not afterBattle and _isLobby():
                        LOG_NOTE('[TUTOR] tutorial button pressed: resetting tutorial state')
                        _prepareRun(p)
                except Exception:
                    LOG_CURRENT_EXCEPTION()
                return origRestart(self, afterBattle)

            cls.restart = restart
            cls._oh_btn = True
        LOG_NOTE('[TUTOR] tutorial button patched')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _areaTriggers():
    """{имя: позиция} зон-триггеров карты (player starting point, point 2 ...) из TriggersManager."""
    out = {}
    try:
        import TriggersManager as TM
        trig = getattr(TM.g_manager, '_TriggersManager__autoTriggers', {}) or {}
        for p in trig.values():
            try:
                if p.get('type') == TM.TRIGGER_TYPE.AREA and p.get('name'):
                    out[p['name']] = p.get('position')
            except Exception:
                pass
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return out


class _PosProxy(object):
    """Сущность машины игрока для TriggersManager: позиция берётся из нашей физики, а не из entity.position
    (офлайн entity.position остаётся в точке появления, поэтому зоны 'point 2...' не срабатывали)."""
    def __init__(self, ent, sp):
        self._e = ent
        self._sp = sp

    @property
    def position(self):
        import Math
        p = self._sp.pos
        return Math.Vector3(p.x, p.y, p.z)

    def __getattr__(self, name):
        return getattr(self._e, name)


class _EntitiesProxy(object):
    def __init__(self, real):
        self._r = real

    def get(self, key, default=None):
        ent = self._r.get(key, default)
        try:
            from gui.mods.offhangar import battle as off_battle
            sp = off_battle._simplePhysicsByVehID.get(key)
            if ent is not None and sp is not None and key == getattr(BigWorld.player(), 'playerVehicleID', None):
                return _PosProxy(ent, sp)
        except Exception:
            pass
        return ent

    def __getattr__(self, name):
        return getattr(self._r, name)


class _BigWorldProxy(object):
    def __init__(self, real):
        self._r = real
        self.entities = _EntitiesProxy(real.entities)

    def __getattr__(self, name):
        return getattr(self._r, name)


def _installTriggerPosition():
    """Модуль TriggersManager видит свой BigWorld с подменённым entities.get() (только для машины игрока)."""
    try:
        import TriggersManager as TM
        if not isinstance(TM.BigWorld, _BigWorldProxy):
            TM.BigWorld = _BigWorldProxy(TM.BigWorld)
            LOG_NOTE('[TUTOR] TriggersManager now uses the physics position of the player vehicle')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _alignCameraToHull(vid, why=''):
    """Камера боя стартует не по направлению корпуса (вид сбоку/спереди). Поворачиваем камеру за корпус: корпус уже смотрит
    на 'point 2' (задан при создании), камера встаёт позади танка. Значения пишутся в лог."""
    import math
    from gui.mods.offhangar import battle as off_battle
    try:
        sp = off_battle._simplePhysicsByVehID.get(vid)
        ih = getattr(BigWorld.player(), 'inputHandler', None)
        ctrl = getattr(ih, 'ctrl', None)
        cam = getattr(ctrl, 'camera', None) or getattr(ctrl, '_ArcadeControlMode__cam', None)
        if sp is None or cam is None or not hasattr(cam, 'setYawPitch'):
            LOG_NOTE('[TUTOR] camera align %s skipped: physics=%s control=%s camera=%s' % (
                why, sp is not None, type(ctrl).__name__, type(cam).__name__))
            return
        d = BigWorld.camera().direction
        before = math.atan2(d.x, d.z)
        aiming = getattr(cam, '_ArcadeCamera__aimingSystem', None)
        pitch = getattr(aiming, 'pitch', -0.3)
        cam.setYawPitch(sp.yaw, pitch)
        LOG_NOTE('[TUTOR] camera align %s: hull yaw=%.2f, camera yaw before=%.2f -> %.2f (pitch %.2f)' % (
            why, sp.yaw, before, sp.yaw, pitch))
    except Exception:
        LOG_CURRENT_EXCEPTION()


_BOT_CD = {}


def _compDescrByName(name):
    """compDescr машины по имени 'nation:name' или None (без трейсбека: отсутствие учебной машины - не ошибка)."""
    try:
        from items import vehicles
        try:
            descr = vehicles.VehicleDescr(typeName=name)
        except Exception:
            nationID, innationID = vehicles.g_list.getIDsByName(name)
            descr = vehicles.VehicleDescr(typeID=(nationID, innationID))
        return descr.makeCompactDescr()
    except Exception:
        LOG_NOTE('[TUTOR] vehicle %r not found in this client' % (name,))
    return None


def botVehicle(idx):
    """compDescr мишени учебного боя idx (1..3): машина заданного класса и уровня; одна и та же на весь бой."""
    if idx in _BOT_CD:
        return _BOT_CD[idx]
    cd = None
    try:
        from gui.mods.offhangar import bots as _bt
        cd = _bt.TUT_VEHICLES.get(idx)
    except Exception:
        cd = None
    if cd is not None:
        _BOT_CD[idx] = cd
        return cd
    try:
        import random as _r
        import nations
        from items import vehicles as _veh
        cls, lo, hi = TUT_BOTS.get(idx, ('mediumTank', 3, 4))
        pool = []
        for nid in range(len(nations.NAMES)):
            try:
                lst = _veh.g_list.getList(nid)
            except Exception:
                continue
            for iid, d in lst.items():
                tags = d.get('tags', ())
                if cls in tags and lo <= int(d.get('level', 0)) <= hi and 'observer' not in tags and 'secret' not in tags \
                        and 'premium' not in tags and 'training' not in str(d.get('name', '')).lower():
                    pool.append((nid, iid))
        _r.shuffle(pool)
        from gui.mods.offhangar import bots
        for nid, iid in pool[:12]:
            cd = bots._compDescrFor(nid, iid)
            if cd is not None:
                LOG_NOTE('[TUTOR] target bot_%d vehicle: %s' % (idx, _veh.VehicleDescr(compactDescr=cd).type.name))
                break
    except Exception:
        LOG_CURRENT_EXCEPTION()
    _BOT_CD[idx] = cd
    return cd


def botPlace(idx):
    """(x, z) места мишени idx по зонам карты (None, если зон ещё нет)."""
    try:
        pl = _botPlaces(_areaTriggers()).get(idx)
        return (pl[0], pl[2]) if pl is not None else None      # _botPlaces даёт (x, y, z)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return None


def _botPlaces(areas):
    """Где стоят мишени (по главам обучения): бот 1 - перед 'point 4' в сторону 'point 5', бот 2 - перед 'point 5' в сторону 'point 6',
    бот 3 - за 'point 9' по направлению 8 -> 9. Точные места в оригинале задавал сервер, здесь они подобраны по маршруту."""
    import math

    def along(a, b, dist):
        dx, dz = b[0] - a[0], b[2] - a[2]
        d = math.hypot(dx, dz) or 1.0
        return (a[0] + dx / d * dist, a[1], a[2] + dz / d * dist)

    p4, p5, p6, p8, p9 = [areas.get('point %d' % n) for n in (4, 5, 6, 8, 9)]
    out = {}
    if p4 is not None and p5 is not None:
        out[1] = along(p4, p5, 80.0)
    if p5 is not None and p6 is not None:
        out[2] = along(p5, p6, 70.0)
    if p8 is not None and p9 is not None:
        out[3] = along(p9, (2 * p9[0] - p8[0], p9[1], 2 * p9[2] - p8[2]), 70.0)
    return out


def onBattleReady(avatar):
    """Учебный бой готов: ждём, пока карта создаст зоны (UserDataObject), проверяем место старта и расставляем мишени
    у точек маршрута (см. _botPlaces). Всё пишется в лог [TUTOR]."""
    from gui.mods.offhangar import battle as off_battle
    st = {'n': 0, 'player': False, 'bots': set()}
    _installTriggerPosition()
    LOG_NOTE('[TUTOR] battle ready: guiType=%s period=%s' % (
        getattr(avatar.arena, 'guiType', None), getattr(avatar.arena, 'period', None)))

    def tick():
        st['n'] += 1
        try:
            areas = _areaTriggers()
            p1 = areas.get('player starting point')
            if p1 is not None and not st['player']:
                st['player'] = True
                LOG_NOTE('[TUTOR] area triggers: %s' % sorted(areas.items()))
                av = BigWorld.player()
                sp = off_battle._simplePhysicsByVehID.get(getattr(av, 'playerVehicleID', 0))
                if sp is not None:
                    d = ((sp.pos.x - p1[0]) ** 2 + (sp.pos.z - p1[2]) ** 2) ** 0.5
                    LOG_NOTE('[TUTOR] player at (%.1f, %.1f), start point (%.1f, %.1f), distance %.1f m' % (
                        sp.pos.x, sp.pos.z, p1[0], p1[2], d))
                    _alignCameraToHull(av.playerVehicleID, 'at start')
                    if d > 25.0:        # на случай другой карты/версии: ставим точно в зону
                        off_battle.teleportVehicleByID(av.playerVehicleID, (p1[0], p1[1], p1[2]), None)
            try:
                if not st.get('cam2') and st['player'] and getattr(BigWorld.player().arena, 'period', None) == 3:
                    st['cam2'] = True
                    _alignCameraToHull(BigWorld.player().playerVehicleID, 'at battle start')
            except Exception:
                LOG_CURRENT_EXCEPTION()
            if p1 is not None:
                from gui.mods.offhangar import bots as _bots
                places = {} if all(n in _bots.TUT_POINTS for n in (1, 2, 3)) else _botPlaces(areas)    # точные точки карты уже заданы в bots.TUT_POINTS
                for vid, osp in list(off_battle._simplePhysicsByVehID.items()):
                    if not getattr(osp, 'isBot', False) or vid in st['bots']:
                        continue
                    try:
                        name = BigWorld.entity(vid).publicInfo['name']
                    except Exception:
                        continue
                    if not str(name).startswith('#battle_tutorial:bot_'):
                        continue
                    idx = int(str(name).rsplit('_', 1)[-1])
                    if idx in places:
                        off_battle.teleportVehicleByID(vid, places[idx], None)
                        st['bots'].add(vid)
                        LOG_NOTE('[TUTOR] target %s placed at (%.1f, %.1f, %.1f)' % ((name,) + tuple(places[idx])))
                if (len(st['bots']) >= 3 or not places) and st.get('cam2'):
                    return
            elif st['n'] == 1 or st['n'] % 10 == 0:
                LOG_DEBUG('[TUTOR] waiting for map area triggers (%d): %s' % (st['n'], sorted(areas.keys())))
        except Exception:
            LOG_CURRENT_EXCEPTION()
        if st['n'] < 160:
            BigWorld.callback(0.5, tick)
        else:
            LOG_NOTE('[TUTOR] gave up after 80 s: start point found=%s, targets placed=%d' % (st['player'], len(st['bots'])))

    BigWorld.callback(0.5, tick)


def fixedRewards():
    """(xp, freeXP, credits, gold) фиксированной награды за учебный бой."""
    r = dict(TUT_REWARD)
    try:
        from gui.mods.offhangar import _constants as _c
        r['credits'] = _c.getSettingInt('tutorialcredits', r['credits'], 0, 100000000)
        r['xp'] = _c.getSettingInt('tutorialxp', r['xp'], 0, 100000000)
        r['free_xp'] = _c.getSettingInt('tutorialfreexp', r['free_xp'], 0, 100000000)
        r['gold'] = _c.getSettingInt('tutorialgold', r['gold'], 0, 100000000)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return int(r['xp']), int(r['free_xp']), int(r['credits']), int(r['gold'])


# ---- Диктор обучения: отслеживаем звуки (SoundGroups getSound2D/playSound2D), чтобы не закрыть бой посреди фразы ----
_SND = {'hooked': False, 'on': False, 'list': [], 'logged': 0}


def _noteSound(kind, args, result):
    if not _SND['on'] or not _isTutorialArena():
        return
    name = args[0] if args else None
    _SND['list'].append((BigWorld.time(), kind, name, result))
    if len(_SND['list']) > 80:
        del _SND['list'][:40]
    if _SND['logged'] < 60:
        _SND['logged'] += 1
        LOG_DEBUG('[TUTOR] sound %s %r' % (kind, name))


def _hookSounds():
    """Оборачиваем SoundGroups.getSound2D / playSound2D (возвращаемое значение не меняется)."""
    if _SND['hooked']:
        return
    try:
        import SoundGroups
        cls = SoundGroups.SoundGroups
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return

    def wrap(name, orig):
        def wrapper(self, *args, **kwargs):
            res = orig(self, *args, **kwargs)
            try:
                _noteSound(name, args, res)
            except Exception:
                pass
            return res
        return wrapper

    for name in ('getSound2D', 'playSound2D'):
        orig = getattr(cls, name, None)
        if orig is not None:
            setattr(cls, name, wrap(name, orig))
    _SND['hooked'] = True
    LOG_NOTE('[TUTOR] sound hooks installed')


def _soundIsPlaying(obj):
    for attr in ('isPlaying', 'playing'):
        try:
            v = getattr(obj, attr)
        except Exception:
            continue
        try:
            return bool(v() if callable(v) else v)
        except Exception:
            return False
    return None


_NATIVE = {'player': None, 'tried': 0.0}


def _nativeSpeaking():
    """True/False - говорит ли диктор по данным родного BattleSoundPlayer обучения; None - плеер не найден."""
    try:
        from tutorial.control.battle.context import BattleSoundPlayer
        from tutorial.control import context as _tc
        pl = _NATIVE['player']
        if pl is None:
            now = BigWorld.time()
            if now - _NATIVE['tried'] < 2.0:
                return None
            _NATIVE['tried'] = now
            import gc
            for o in gc.get_objects():
                if isinstance(o, BattleSoundPlayer):
                    pl = o
            _NATIVE['player'] = pl
            if pl is None:
                return None
            LOG_NOTE('[TUTOR] native tutorial sound player found')
        busy = bool(pl.isPlaying(_tc.SOUND_EVENT.SPEAKING))
        if not busy and getattr(pl, '_BattleSoundPlayer__nextSndID', None) is not None:
            busy = True
        return busy
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return None


def _voiceBusy(since):
    nat = _nativeSpeaking()
    if nat is not None:
        return nat
    return _voiceBusyHook(since)


def _voiceBusyHook(since):
    """Говорит ли сейчас диктор: звук, начатый после since, ещё играет (или создан только что)."""
    now = BigWorld.time()
    for (t, kind, name, obj) in _SND['list']:
        if t < since:
            continue
        if now - t < 1.0:
            return True
        if obj is not None:
            if _soundIsPlaying(obj):
                return True
        elif now - t < TUT_VOICE_FALLBACK:
            return True
    return False


def finishWhenDone(finish):
    """Все мишени уничтожены: ждём, пока диктор обучения договорит, и только потом вызываем finish (закрытие боя)."""
    t0 = BigWorld.time()
    _NATIVE['player'] = None
    _NATIVE['tried'] = 0.0
    st = {'quiet': None, 'n': 0}
    LOG_NOTE('[TUTOR] all targets destroyed: waiting for the tutorial voice to finish (min %.0f s, max %.0f s)' % (TUT_END_MIN_DELAY, TUT_END_MAX_WAIT))

    def tick():
        st['n'] += 1
        now = BigWorld.time()
        el = now - t0
        busy = _voiceBusy(t0 - 1.0)
        if busy:
            st['quiet'] = None
        elif st['quiet'] is None:
            st['quiet'] = now
        done = False
        if el >= TUT_END_MAX_WAIT:
            LOG_NOTE('[TUTOR] voice wait timed out after %.0f s - finishing the battle' % el)
            done = True
        elif el >= TUT_END_MIN_DELAY and st['quiet'] is not None and now - st['quiet'] >= TUT_END_QUIET:
            LOG_NOTE('[TUTOR] tutorial voice finished (%.1f s after the last target) - finishing the battle' % el)
            done = True
        if done:
            try:
                finish()
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return
        BigWorld.callback(0.5, tick)

    BigWorld.callback(0.5, tick)


def _tutorialArena():
    """Карта учебного боя: арена, в названии которой есть 'tutorial'. Если не нашли - None (будет случайная)."""
    try:
        import ArenaType
        found = []
        for tid, at in ArenaType.g_cache.items():
            if 'tutorial' in str(getattr(at, 'gameplayName', '')).lower() or 'tutorial' in str(getattr(at, 'geometryName', '')).lower():
                found.append(tid)
        found.sort()
        LOG_DEBUG('[TUTOR] tutorial arena candidates: %s (%s)' % (
            found, [(t, getattr(ArenaType.g_cache[t], 'geometryName', '?'), getattr(ArenaType.g_cache[t], 'gameplayName', '?')) for t in found]))
        if not found:
            return None
        from gui.mods.offhangar import battle as _b
        good = [t for t in found if _b._spawnPos(ArenaType.g_cache[t]) != (0.0, 0.0, 0.0)]
        LOG_DEBUG('[TUTOR] with player position: %s' % good)
        # карту учебного боя берём в любом случае: если позиции в xml нет, структура файла попадёт в лог ([SPAWN])
        return (good or found)[0]
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return None


def _tutorialVehicle():
    """(compDescr, ammoDict) учебной машины (Т-34-85) или None, если не нашли - тогда берётся машина из ангара."""
    try:
        from gui.mods.offhangar import _constants as _c
        name = str(_c.readOfflineSettings().get('tutorialvehicle', TUT_VEHICLE) or TUT_VEHICLE).strip()
    except Exception:
        name = TUT_VEHICLE
    if name.upper() in ('NONE', '0', 'OFF', 'NO', ''):
        return None
    from items import vehicles
    tried = []
    for nm in (name, TUT_VEHICLE_FALLBACK):
        if nm in tried:
            continue
        tried.append(nm)
        try:
            descr = None
            try:
                descr = vehicles.VehicleDescr(typeName=nm)
            except Exception:
                nationID, innationID = vehicles.g_list.getIDsByName(nm)
                descr = vehicles.VehicleDescr(typeID=(nationID, innationID))
            cd = descr.makeCompactDescr()
            LOG_NOTE('[TUTOR] tutorial vehicle: %s (maxHealth=%s)' % (nm, getattr(descr, 'maxHealth', '?')))
            return (cd, None)         # ammo=None -> стандартный боекомплект орудия (getUniformAmmoForGun)
        except Exception:
            LOG_NOTE('[TUTOR] tutorial vehicle %r not found' % (nm,))
    LOG_NOTE('[TUTOR] no tutorial vehicle found (%s) - the hangar vehicle will be used' % (tried,))
    return None


_B = {'token': 0, 'fired': set(), 'logged': 0, 'diag': 0}


def _isTutorialArena():
    p = _player()
    arena = getattr(p, 'arena', None)
    if arena is None:
        return False
    try:
        from constants import ARENA_GUI_TYPE
        return arena.guiType == ARENA_GUI_TYPE.TUTORIAL
    except Exception:
        return False


def _diag():
    """Что есть в бою обучения: зоны/цели карты, машины арены, главы и триггеры сценария."""
    try:
        import TriggersManager as TM
        m = TM.g_manager
        auto = getattr(m, '_TriggersManager__autoTriggers', {}) if m is not None else {}
        LOG_NOTE('[TUTOR] triggers manager: %s enabled=%s map triggers=%d' % (m is not None, m is not None and m.isEnabled(), len(auto)))
        for k, v in sorted(auto.items()):
            LOG_NOTE('[TUTOR]   trigger %s: %s' % (k, dict((a, (tuple(b) if hasattr(b, 'x') else b)) for a, b in v.items())))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        arena = _player().arena
        for vid, v in arena.vehicles.items():
            LOG_NOTE('[TUTOR]   arena vehicle %s: name=%r team=%s alive=%s type=%s' % (
                vid, v.get('name'), v.get('team'), v.get('isAlive'), getattr(v.get('vehicleType'), 'name', None)))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from tutorial import loader
        t = loader.g_loader.tutorial
        d = getattr(t, '_data', None)
        LOG_NOTE('[TUTOR] tutorial: %s data=%s' % (t, d))
        for key, val in sorted(vars(d).items()) if d is not None else []:
            if isinstance(val, dict):
                LOG_NOTE('[TUTOR]   data.%s: %s' % (key, sorted(val.keys())[:60]))
            else:
                LOG_NOTE('[TUTOR]   data.%s: %r' % (key, val))
        try:
            LOG_NOTE('[TUTOR]   vars: %s' % (t.getVars()._Vars__vars if hasattr(t.getVars(), '_Vars__vars') else vars(t.getVars())))
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            bonus = getattr(d, '_Chapter__bonus', None)
            if bonus is not None:
                LOG_NOTE('[TUTOR]   bonus object: %s attrs=%s' % (type(bonus).__name__, dict((k, v) for k, v in vars(bonus).items())))
                LOG_NOTE('[TUTOR]   bonus members: %s' % [m for m in dir(bonus) if not m.startswith('__')])
        except Exception:
            LOG_CURRENT_EXCEPTION()
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _watch(token):
    """Раз в 0.5 с: менеджер триггеров включён; уничтоженные цели -> триггер VEHICLE_DESTROYED."""
    if _B['token'] != token:
        return
    p = _player()
    if p is None or getattr(p, 'arena', None) is None or not _isTutorialArena():
        BigWorld.callback(1.0, lambda: _watch(token) if _B['token'] == token and _player() is not None and hasattr(_player(), 'playerVehicleID') else None)
        return
    BigWorld.callback(0.5, lambda: _watch(token))
    try:
        import TriggersManager as TM
        m = TM.g_manager
        if m is None:
            return
        if not m.isEnabled():
            m.enable(True)
            LOG_NOTE('[TUTOR] triggers manager enabled by the mod')
        pvid = getattr(p, 'playerVehicleID', 0)
        for vid in list(p.arena.vehicles.keys()):
            if vid == pvid or vid in _B['fired']:
                continue
            ent = BigWorld.entities.get(vid)
            if ent is not None and getattr(ent, 'health', 1) <= 0:
                _B['fired'].add(vid)
                m.fireTrigger(TM.TRIGGER_TYPE.VEHICLE_DESTROYED, vehicleId=vid)
                try:
                    p.arena.vehicles[vid]['isAlive'] = False
                except Exception:
                    pass
                LOG_NOTE('[TUTOR] target %s destroyed -> VEHICLE_DESTROYED trigger' % vid)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def startBridge():
    _B['token'] += 1
    _B['fired'] = set()
    token = _B['token']
    BigWorld.callback(2.0, lambda: _watch(token))
    for delay in (10.0, 40.0):
        BigWorld.callback(delay, lambda t=token: _diag() if _B['token'] == t and _isTutorialArena() else None)



def _cmdEnqueue(requestID, a=0, b=0, c=0):
    from gui.mods.offhangar import battle as off_battle
    from gui.mods.offhangar import bots
    from gui.mods.offhangar import requests as off_req
    p = _player()
    vehInvID = 0
    try:
        from CurrentVehicle import g_currentVehicle
        if g_currentVehicle.isPresent():
            vehInvID = g_currentVehicle.invID
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        if not off_req.checkCrewForBattle(vehInvID)[0]:
            return (requestID, AccountCommands.RES_FAILURE, '', None)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    _S['token'] += 1
    token = _S['token']
    BigWorld.callback(0.05, lambda: p.onTutorialEnqueued(1, 1, 5))

    def _go():
        if _S['token'] != token:
            return
        try:
            BigWorld.player().onArenaCreated()
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            _BOT_CD.clear()
            _hookSounds()
            _SND['list'] = []
            _SND['logged'] = 0
            _SND['on'] = True
            try:
                from gui.mods.offhangar import shooting as _sh
                _sh._TUT_BOUNCED[0] = False
            except Exception:
                pass
            off_battle.setNextBattleKind('tutorial')       # arenaGuiType/bonusType = TUTORIAL: по ним клиент включает боевые подсказки
            tv = _tutorialVehicle()
            off_battle.setTutorialVehicle(*tv) if tv else off_battle.setTutorialVehicle(None)
            try:
                from gui.mods.offhangar import _constants as _c
                enemies = _c.getSettingInt('tutorialenemies', TUT_ENEMIES, 0, 10)
            except Exception:
                enemies = TUT_ENEMIES
            bots.COUNT_OVERRIDE = (enemies, TUT_ALLIES)
            try:
                from gui.mods.offhangar import spotting as _spot
                from gui.mods.offhangar import _constants as _c2
                _spot.TUT_REVEAL_RADIUS = float(_c2.getSettingInt('tutorialspotradius', int(_spot.TUT_REVEAL_RADIUS), 50, 445))
                LOG_NOTE('[TUTOR] targets appear at %d m' % int(_spot.TUT_REVEAL_RADIUS))
            except Exception:
                LOG_CURRENT_EXCEPTION()
            arena = _tutorialArena()
            LOG_NOTE('[TUTOR] tutorial battle: arena=%s vehInvID=%s' % (arena, vehInvID))
            try:
                # цели: точки и учебные танки из карты (AutoBotSpawnPoint); нет учебных машин - класс/уровень (botVehicle)
                import ArenaType
                geom = ArenaType.g_cache[arena].geometryName if arena else None
                md = TUT_MAP_BOTS.get(geom) or {}
                bots.TUT_POINTS = dict(md.get('points') or {})
                bots.TUT_PATROL = dict(md.get('patrol') or {})
                tv2 = {}
                for n in (1, 2, 3):
                    cd2 = _compDescrByName(TUT_BOT_VEHICLES[n])
                    if cd2 is not None:
                        tv2[n] = cd2
                bots.TUT_VEHICLES = tv2
                LOG_NOTE('[TUTOR] targets: map=%s training vehicles %s, map points %s, patrol %s' % (
                    geom, sorted(tv2.keys()), sorted(bots.TUT_POINTS.keys()), sorted(bots.TUT_PATROL.keys())))
            except Exception:
                LOG_CURRENT_EXCEPTION()
            off_battle._startBattle(vehInvID, arena or 0)
            startBridge()
        except Exception:
            LOG_CURRENT_EXCEPTION()
    BigWorld.callback(random.uniform(*QUEUE_WAIT), _go)
    return (requestID, AccountCommands.RES_SUCCESS, '', None)


def _cmdDequeue(requestID, a=0, b=0, c=0):
    from constants import QUEUE_TYPE
    _S['token'] += 1
    p = _player()
    BigWorld.callback(0.05, lambda: p.onDequeued(QUEUE_TYPE.TUTORIAL))
    return (requestID, AccountCommands.RES_SUCCESS, '', None)


def _hookKeys():
    if _S['hooked']:
        return
    try:
        import game as _game
        orig = _game.handleKeyEvent

        def handleKeyEvent(event, orig=orig):
            try:
                key = _S['key']
                if key is not None and event.key == key and event.isKeyDown() and not event.isRepeatedEvent() and _isLobby():
                    now = BigWorld.time()
                    if now - _S['last'] > 1.0:
                        _S['last'] = now
                        BigWorld.callback(0.0, startTutorial)
                        return True
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return orig(event)
        _game.handleKeyEvent = handleKeyEvent
        _S['hooked'] = True
    except Exception:
        LOG_CURRENT_EXCEPTION()


def register():
    if not ENABLED:
        return
    try:
        _S['key'] = _readKey()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    C = AccountCommands
    if hasattr(C, 'CMD_ENQUEUE_TUTORIAL'):
        BASE_REQUESTS[C.CMD_ENQUEUE_TUTORIAL] = _cmdEnqueue
    if hasattr(C, 'CMD_DEQUEUE_TUTORIAL'):
        BASE_REQUESTS[C.CMD_DEQUEUE_TUTORIAL] = _cmdDequeue
    _hookKeys()
    _patchTutorialButton()
    LOG_NOTE('[TUTOR] registered, key=%s' % (_S['key'],))
