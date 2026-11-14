# -*- coding: utf-8 -*-
"""Простые боты для офлайн-боя.

* При старте боя (пока идёт обратный отсчёт) сами появляются BOT_ENEMY_COUNT врагов
  (команда 2) и BOT_ALLY_COUNT союзников (команда 1, рядом с игроком).
* Враги едут к ближайшей цели (игрок или союзный бот), останавливаются на
  дистанции BOT_MIN_RANGE..BOT_MAX_RANGE, объезжают препятствия и стреляют.
* Союзники атакуют вражеских ботов, а если врагов не осталось - держатся рядом с игроком.
* Урон по своей команде не проходит (см. shooting._applyDamage).

Как это встроено в мод:
  - battle._finishVehicleSetup -> bots.onBattleStart(avatar)
  - battle._onLeaveWorld / endbattle._teardown -> bots.onBattleEnd()
  - battle.register -> bots.register() (ставит хук Vehicle.onEnterWorld)
  - Хук Vehicle.onEnterWorld добавляет бота в список машин арены ДО старта визуала:
    иначе VehicleAppearance падает с KeyError в __createStickers.
"""
import math
import random
import weakref
import cPickle

import BigWorld
import Math
import ResMgr

from constants import ARENA_UPDATE
from debug_utils import LOG_CURRENT_EXCEPTION
from gun_rotation_shared import calcPitchLimitsFromDesc
from projectile_trajectory import getShotAngles

from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE, LOG_ERROR
from gui.mods.offhangar import battle as off_battle
from gui.mods.offhangar import shooting as off_shooting
from gui.mods.offhangar import turret as off_turret
from gui.mods.offhangar import spotting as off_spotting
from gui.mods.offhangar import perf as _perf
from gui.mods.offhangar import nav as off_nav

# ============================================================
# Config
# ============================================================
BOT_ENEMY_COUNT = 15           # врагов (как в настоящем бою 15 на 15)
BOT_ALLY_COUNT = 14            # союзников (кроме игрока)
_DEFAULT_ENEMY = BOT_ENEMY_COUNT
_DEFAULT_ALLY = BOT_ALLY_COUNT
BOT_TIER_SPREAD = 2            # уровень техники ботов: уровень игрока +/- 2
BOT_USE_MATCHMAKER = True       # состав команд подбирает матчмейкер (mm_core.py, конфиг offhangar_user/balancer.json)
BOT_USE_ROUTES = True          # боты едут по маршрутам (фланги/центр по классу техники), а не все сразу на ближайшего врага
BOT_SPOT_RANGE = 230.0         # пока едет по маршруту, бот замечает врагов только ближе этой дистанции, м
BOT_ROUTE_REACH = 20.0         # точка маршрута считается достигнутой на этом расстоянии, м
BOT_PUSH_AFTER = 0.0         # ПТ сразу выходят на маршрут; САУ остаются на позиции
BOT_AUTO_END = True            # бой заканчивается сам, когда уничтожена одна из команд
BOT_AUTO_END_DELAY = 4.0       # пауза перед экраном результатов, сек

BOT_SPAWN_DELAY = 0.0          # пауза после старта отсчёта до первого бота, сек (0 - боты появляются сразу, за экраном загрузки)
BOT_SPAWN_INTERVAL = 0.02      # пауза между ботами, сек (0.02 - все 29 за ~0.6 с; поставь больше, если вылетает)
BOT_SPAWN_CHECK = True         # проверять точку спавна: не в стене/доме, не под крышей и мостом, не на крутом склоне, не вплотную к другой машине
BOT_SPAWN_SEARCH = (0.0, 6.0, 12.0, 18.0, 26.0, 36.0, 48.0)   # радиусы поиска свободной точки вокруг запланированной, м
BOT_SPAWN_GAP = 8.0            # минимальное расстояние между центрами заспавненных машин, м
BOT_SPAWN_MAX_SLOPE = 0.45     # максимальный уклон под корпусом (tg)
BOT_SPAWN_RING = 16.0          # союзники появляются в этом радиусе от игрока, м

BOT_AI_TICK = 0.1              # частота "мышления" бота, сек
BOT_AI_TICK_FAR = 0.2          # ... для далёких от игрока ботов
BOT_PHYSICS_TICK = 0.08        # частота физики бота (у игрока - каждый кадр), сек; плавность даёт интерполяция (battle.BOT_SMOOTH)
BOT_PHYSICS_TICK_FAR = 0.16    # ... для далёких от игрока ботов
BOT_LOD_DIST = 130.0           # "далёкий" бот: дальше этого расстояния от игрока, м
BOT_LOD_DIST2 = 250.0          # "очень далёкий" бот: ещё реже (v87)
BOT_PHYSICS_TICK_FAR2 = 0.22
BOT_AI_TICK_FAR2 = 0.3
BOT_AVOID_CACHE_FAR = 0.6      # для далёких ботов объезд пересчитывается ещё реже
BOT_AVOID_CACHE = 0.25         # объезд препятствий пересчитывается не чаще, сек (пока курс почти не изменился)
BOT_START_WAIT = 15.0          # сколько ждать старта визуала машины бота, сек
BOT_ENTITY_WAIT_STEP = 0.1
BOT_ENTITY_WAIT_TRIES = 150

BOT_MIN_RANGE = 35.0           # ближе этой дистанции враг пятится
BOT_MAX_RANGE = 70.0           # дальше этой дистанции враг едет к цели
BOT_FOLLOW_MIN = 18.0          # союзник без врагов: держится в этом кольце вокруг игрока
BOT_FOLLOW_MAX = 40.0
BOT_SHOOT_DISTANCE = 220.0
BOT_ALLOW_SPG = True           # артиллерия среди ботов (при подборе без матчмейкера; матчмейкер: ALLOW_SPG в matchmaker.py)
BOT_SPG_RANGE = 350.0          # САУ-бот бьёт по врагу в этом радиусе (навесом, без прямой видимости), м
BOT_SPG_MIN_RANGE = 110.0      # ближе этого САУ-бот пятится
BOT_SPG_AIM_TOL = 0.08         # допуск наводки САУ по азимуту, рад (угол возвышения не важен - огонь навесом)
BOT_SPG_HIT_NEAR = 0.5        # шанс попадания САУ-бота вблизи
BOT_SPG_HIT_FAR = 0.14         # ... и на предельной дальности
BOT_RETARGET_INTERVAL = 2.0
BOT_AIM_TOLERANCE = 0.05       # допустимая ошибка наводки, рад
BOT_INACCURACY_BASE = 0.5      # разброс точки прицеливания: база + за метр дистанции, м
BOT_INACCURACY_PER_METER = 0.012
BOT_RELOAD_FACTOR = (1.0, 1.5) # боты перезаряжаются медленнее игрока

BOT_PROBE_LENGTH_MIN = 18.0    # дальность "усов" для объезда препятствий, м
BOT_AVOID_ANGLES = (0.0, 0.35, -0.35, 0.7, -0.7, 1.05, -1.05, 1.4, -1.4, 1.75, -1.75, 2.1, -2.1)
BOT_SPG_RETARGET = (6.0, 14.0)  # арта держит выбранную цель столько секунд, потом выбирает новую случайно среди засвеченных (не только игрока)
BOT_SPG_THREAT_CHANCE = 0.3     # шанс, что САУ ответит тому, кто по ней попал (иначе продолжает свою цель)
BOT_SPG_DAMAGE_MULT = 0.7      # урон снаряда САУ-бота относительно паспортного
BOT_SPG_DAMAGE_CAP = 0.5       # и не больше этой доли максимального здоровья цели (нет ваншотов с полного HP)
BOT_SPG_RELOAD_MULT = 1.3      # САУ-боты перезаряжаются дольше
BOT_CAPTURERS_PER_TEAM = 2         # сколько ботов в команде едут захватывать базу противника
BOT_CAPTURER_CHANCE = 0.3
_CAPTURERS = {}
BOT_SPG_HARASS = (14.0, 28.0)  # пока цели нет, САУ раз в столько секунд кидает снаряд по карте (около врагов, с промахом)
BOT_SPG_COVER = True           # снаряд не долетает до цели под крышей/кроной и за домом на траектории падения
BOT_SPG_DANGER = 140.0         # враг (засвеченный) ближе этого - арта отъезжает к своей базе
BOT_SPG_FIGHT = 70.0           # враг ближе этого - арта останавливается, разворачивается и бьёт прямой наводкой
BOT_SPG_HOME_RADIUS = 9.0      # САУ держится возле стартовой позиции, м
BOT_STUCK_TIME = 1.0           # столько стоим на месте при движении -> считаем, что застряли
BOT_AVOID_VEHICLES = True      # лучи объезда учитывают чужие танки и остовы (не только статику)
BOT_AVOID_HYSTERESIS = 4.0     # бонус к выбранной стороне объезда, чтобы бот не метался влево-вправо
BOT_AVOID_KEEP = 1.5           # сколько секунд держим выбранную сторону объезда
BOT_DETOUR_DIST = 14.0         # после застревания бот едет к боковой точке на таком расстоянии, м
BOT_DETOUR_TIME = 4.0          # ... но не дольше стольких секунд
BOT_STUCK_MIN_MOVE = 0.35      # смещение за 0.5 сек, меньше которого считаем, что не едем

# Отладочные переключатели (если игра падает - выключай по одному и смотри, что виновато)
BOT_DEBUG_NO_MOVE = False      # боты не ездят
BOT_DEBUG_NO_TURRET = False    # башни ботов не вращаются
BOT_DEBUG_NO_FIRE = False      # боты не стреляют

TEAM_ALLY = 1
TEAM_ENEMY = 2

# ============================================================
# State
# ============================================================
_REGISTERED = False
_HOOK_INSTALLED = False

_BOTS = {}              # botID -> BotAI (мёртвые тоже остаются до конца боя)
_PENDING_BOTS = []      # записи ботов, чьи Vehicle-сущности ещё создаются
_BOT_ID_COUNTER = 0
_SPAWN_TOKEN = 0        # id текущего боя; отложенные callback-и с другим id игнорируются
_SPAWNED_FOR = None     # avatar, для которого уже запланирован спавн
_PLAN = {}              # team -> [(x, z), ...] точки спавна
_REGISTERED_IDS = set() # vehicle.id, добавленные в список машин арены
_KILLED_IDS = set()     # vehicle.id, по которым отправлен VEHICLE_KILLED
_FRAGS = {}             # vehicle.id -> число фрагов
_CHECK_END_AFTER = 0.0  # раньше этого времени конец боя не проверяем (боты ещё появляются)
_END_SCHEDULED = False
_PENDING_WAIT_ACTIVE = False
_TRACER_BROKEN = [False]


# ============================================================
# Helpers
# ============================================================
def _active():
    try:
        return bool(off_battle._active())
    except Exception:
        return False


def _wrap(a):
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


def _getPlayerVehicle():
    try:
        avatar = BigWorld.player()
        vid = getattr(avatar, 'playerVehicleID', 0)
        if vid:
            return BigWorld.entity(vid)
    except Exception:
        pass
    return None


def isAlive(vehicle):
    try:
        return bool(getattr(vehicle, 'isStarted', False)) and getattr(vehicle, 'health', 0) > 0
    except Exception:
        return False


def _posOf(vehicle):
    """Положение машины. Физика мода двигает модель сама, поэтому берём позицию из неё."""
    try:
        sp = off_battle._simplePhysicsByVehID.get(vehicle.id)
        if sp is not None:
            return sp.pos
    except Exception:
        pass
    return vehicle.position


def getTeam(vehicle):
    try:
        return int(vehicle.publicInfo['team'])
    except Exception:
        return 0


def isBotVehicle(vehicle):
    try:
        return int(vehicle.publicInfo['accountDBID']) < 0
    except Exception:
        return False


def _updateArena(avatar, updateType, payload):
    try:
        avatar.updateArena(updateType, payload)
    except Exception:
        LOG_CURRENT_EXCEPTION()
        try:
            avatar.arena.update(updateType, payload)
        except Exception:
            LOG_CURRENT_EXCEPTION()


_ROSTER = {}            # team -> [(nationID, vehID), ...] состав от матчмейкера
_ROSTER_INFO = ['']


def _resourcesOk(descr):
    """Есть ли у машины файлы моделей в клиенте (иначе она "прозрачная": не рисуется и не получает урон)."""
    try:
        import ResMgr
        for comp in (descr.chassis, descr.hull, descr.turret, descr.gun):
            models = comp.get('models')
            if not models:
                continue
            path = models.get('undamaged')
            if not path:
                continue
            ok = True
            try:
                ok = bool(ResMgr.isFile(path))
            except Exception:
                try:
                    ok = ResMgr.openSection(path) is not None
                except Exception:
                    ok = True
            if not ok:
                return False, path
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return True, None


BOT_MODULES = [4]               # 0 - все в стоке, 1 - случайные модули, 2 - у всех топ, 3 - у всех предтоп, 4 - микс: сток/предтоп/топ на каждую машину (по умолчанию); offline_user.txt: bot_modules=
MIX_STOCK = 0.25                # доля стоковых ботов в режиме 4
MIX_PRETOP = 0.45               # доля предтоповых (остальное - топовые)


def _pickModules(descr, mode):
    """Меняет модули машины бота: шасси, двигатель, рация, башня+орудие. mode 1 - у каждого модуля случайная модель из доступных
    (уклон к более новым), 2 - самая новая. Каждая замена проверяется движком (вес, совместимость); при ошибке модуль остаётся."""
    t = descr.type

    def pick(lst):
        if not lst:
            return None
        if mode == 2:
            return lst[-1]
        if mode >= 3:
            # предтоп: второй с конца (если модулей всего один-два - он же, иногда на ступень ниже)
            k = max(0, len(lst) - 2)
            if k > 0 and random.random() < 0.25:
                k -= 1
            return lst[k]
        # уклон к более новым: берём лучшее из двух случайных
        i, j = random.randrange(len(lst)), random.randrange(len(lst))
        return lst[max(i, j)]

    for lst in (t.chassis, t.engines, t.radios):
        try:
            c = pick(lst)
            if c is None:
                continue
            cd = c['compactDescr']
            if descr.mayInstallComponent(cd)[0]:
                descr.installComponent(cd)
        except Exception:
            pass
    try:
        turrets = t.turrets[0]
        tur = pick(turrets)
        if tur is not None:
            gun = pick(tur['guns'])
            if gun is not None and descr.mayInstallTurret(tur['compactDescr'], gun['compactDescr'])[0]:
                descr.installTurret(tur['compactDescr'], gun['compactDescr'])
    except Exception:
        pass


def _compDescrFor(nationID, vehID):
    from items import vehicles as _veh
    descr = _veh.VehicleDescr(typeID=(nationID, vehID))
    stockCD = descr.makeCompactDescr()
    mode = BOT_MODULES[0]
    if mode >= 4:
        r = random.random()
        mode = 0 if r < MIX_STOCK else (3 if r < MIX_STOCK + MIX_PRETOP else 2)
    if mode > 0 and 'SPG' not in descr.type.tags:
        try:
            _pickModules(descr, mode)
            ok, path = _resourcesOk(descr)
            if not ok:
                descr = _veh.VehicleDescr(compactDescr=stockCD)
        except Exception:
            LOG_CURRENT_EXCEPTION()
            descr = _veh.VehicleDescr(compactDescr=stockCD)
    ok, path = _resourcesOk(descr)
    if not ok:
        LOG_ERROR('[BOTS] vehicle %s skipped: model file missing in client: %s' % (descr.type.name, path))
        return None
    return descr.makeCompactDescr()


def _rosterCompDescr(team, idx):
    lst = _ROSTER.get(team)
    if not lst or idx >= len(lst):
        return None
    try:
        return _compDescrFor(*lst[idx])
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return None


FREE_BALANCE = [False]          # тренировочный бой: без балансировки по уровням (1 и 10 в одном бою)
FREE_BALANCE_NEXT = [False]     # выставляет комната; переходит в FREE_BALANCE при старте боя
FREE_LEVELS = (1, 10)
ROOM_ROSTER = [None]            # {1: [(nationID, vehID)] союзники, 2: [...] враги} - танки из окна тренировочной комнаты
ROOM_ROSTER_NEXT = [None]


def _makeFreeRoster():
    """Любые танки любых уровней (по FREE_LEVELS), не больше 2 САУ на команду, без машин из excluded_vehicles.txt."""
    try:
        from gui.mods.offhangar import matchmaker
        rows, ids = matchmaker._catalogRows()
        names = matchmaker.excludedNames()
        pool = {}
        for row in rows:
            lv = row.get('level', 0)
            if lv < FREE_LEVELS[0] or lv > FREE_LEVELS[1] or 'observer' in row.get('tags', ()):
                continue
            if matchmaker.isExcluded(row['name'], names) or row['name'] not in ids:
                continue
            pool.setdefault(row['vehicle_class'], []).append(row['name'])
        allNames = [n for lst in pool.values() for n in lst]
        if not allNames:
            return
        spgNames = pool.get('SPG', [])
        other = [n for n in allNames if n not in spgNames]
        for team, count in ((TEAM_ALLY, BOT_ALLY_COUNT), (TEAM_ENEMY, BOT_ENEMY_COUNT)):
            out, spg = [], 0
            while len(out) < count:
                n = random.choice(allNames)
                if n in spgNames:
                    if spg >= 2:
                        n = random.choice(other)
                    else:
                        spg += 1
                out.append(ids[n])
            _ROSTER[team] = out
        _ROSTER_INFO[0] = 'free balance (levels %s-%s)' % FREE_LEVELS
        LOG_NOTE('[BOTS] training battle: free balance, no level limits')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _applyRoomRoster():
    """Танки из окна комнаты - первые в составе каждой команды (остальные - случайные, если комната дала меньше)."""
    room = ROOM_ROSTER[0]
    if not room:
        return
    for team, lst in room.items():
        cur = _ROSTER.setdefault(team, [])
        for i, item in enumerate(lst):
            if i < len(cur):
                cur[i] = item
            else:
                cur.append(item)
    LOG_NOTE('[BOTS] training battle: vehicles taken from the room (allies %d, enemies %d)' % (
        len(room.get(TEAM_ALLY, ())), len(room.get(TEAM_ENEMY, ()))))


def _makeRoster(pv):
    """Состав обеих команд по правилам матчмейкера. При неудаче _ROSTER пуст и боты берутся случайно."""
    _ROSTER.clear()
    _ROSTER_INFO[0] = ''
    if FREE_BALANCE[0]:
        _makeFreeRoster()
        _applyRoomRoster()
        return
    if not BOT_USE_MATCHMAKER:
        return
    try:
        from gui.mods.offhangar import matchmaker
        res = matchmaker.buildRoster(pv.typeDescriptor.type.name, BOT_ALLY_COUNT, BOT_ENEMY_COUNT)
        if res:
            _ROSTER.update(res[0])
            _ROSTER_INFO[0] = res[1]
    except Exception:
        LOG_CURRENT_EXCEPTION()


_AXIS = {}              # team centers, common attack direction and arena bounds


def _vehicleRole(compDescr):
    """'spg' | 'td' | 'light' | 'heavy' | 'medium'"""
    try:
        from items import vehicles as _veh
        tags = _veh.getVehicleType(compDescr).tags
        if 'SPG' in tags:
            return 'spg'
        if 'AT-SPG' in tags:
            return 'td'
        if 'lightTank' in tags:
            return 'light'
        if 'heavyTank' in tags:
            return 'heavy'
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 'medium'


_LANES = {}             # team -> оставшиеся "полосы" карты (равномерно по всей ширине, перемешаны)
LANE_FACTOR = {'light': 1.0, 'medium': 1.0, 'heavy': 0.85, 'td': 0.7}


def _mapName():
    try:
        return BigWorld.player().arena.arenaType.geometryName
    except Exception:
        return '?'


def _takeLane(team):
    """Каждый бот команды получает свою полосу: от левого до правого края карты, без повторов (иначе кучи)."""
    lst = _LANES.get(team)
    if not lst:
        n = max(1, (BOT_ALLY_COUNT if team == TEAM_ALLY else BOT_ENEMY_COUNT))
        lst = [-1.0 + 2.0 * (i + 0.5) / n for i in range(n)]
        random.shuffle(lst)
        _LANES[team] = lst
    return lst.pop()


def _buildRoute(team, role):
    """Список точек (x, z): каждый бот идёт в своей полосе по всей ширине карты, затем меняет фланг (не копится в куче)."""
    a, e = _AXIS.get('ally'), _AXIS.get('enemy')
    if not a or not e:
        return []
    own, foe = (a, e) if team == TEAM_ALLY else (e, a)
    dx, dz = foe[0] - own[0], foe[1] - own[1]
    length = math.hypot(dx, dz) or 1.0
    fx, fz = dx / length, dz / length
    rx, rz = fz, -fx
    bbox = _AXIS.get('bbox')

    if bbox:
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        half = 0.5 * (abs(rx) * bw + abs(rz) * bh) - 60.0        # половина ширины карты поперёк оси атаки
        width = max(120.0, min(420.0, half))
    else:
        width = min(170.0, 0.26 * length)

    if role == 'spg':
        lane = random.choice((-0.5, 0.0, 0.5))
        width = min(width, 170.0)
        stages = (0.18,)
    else:
        lane = _takeLane(team) * LANE_FACTOR.get(role, 1.0)
        stages = (0.22, 0.42, 0.62, 0.82)

    route = []

    def add(prog, lat):
        px = own[0] + fx * length * prog + rx * lat
        pz = own[1] + fz * length * prog + rz * lat
        if bbox:
            px = min(max(px, bbox[0] + 40.0), bbox[2] - 40.0)
            pz = min(max(pz, bbox[1] + 40.0), bbox[3] - 40.0)
        route.append((px, pz))

    for prog in stages:
        add(prog, lane * width + random.uniform(-25.0, 25.0))
    if role != 'spg':
        # до вражеской стороны в своей полосе, потом разворот через центр на противоположный фланг и снова вперёд:
        # бот объезжает всю карту, а не сидит на базе врага
        add(0.95, lane * width * 0.8 + random.uniform(-25.0, 25.0))
        add(0.55, -lane * width * 0.9 + random.uniform(-25.0, 25.0))
        add(0.90, -lane * width * 0.6 + random.uniform(-25.0, 25.0))
        add(0.30, lane * width * 0.7 + random.uniform(-25.0, 25.0))
    return route


_POOL = {}
_SPG_SLOT_BY_TEAM = {}


def _randomSpgCompDescr():
    """Choose a compatible SPG for the player's tier, if the client has one."""
    try:
        import nations
        from items import vehicles as _veh
        level = int(_getPlayerVehicle().typeDescriptor.type.level)
        pool = []
        for nationID in range(len(nations.NAMES)):
            try:
                lst = _veh.g_list.getList(nationID)
            except Exception:
                continue
            for innationID, descr in lst.items():
                try:
                    tags = descr.get('tags', ())
                    if 'SPG' not in tags or 'observer' in tags or 'secret' in tags:
                        continue
                    if abs(int(descr.get('level', 0)) - level) <= BOT_TIER_SPREAD:
                        pool.append((nationID, innationID))
                except Exception:
                    pass
        random.shuffle(pool)
        for nationID, innationID in pool:
            try:
                cd = _compDescrFor(nationID, innationID)
                if cd is not None:
                    return cd
            except Exception:
                pass
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return None


def _vehiclePool(level):
    """Все танки игры подходящего уровня (кроме исключённых в excluded_vehicles.txt): список (nationID, innationID)."""
    key = (level, BOT_TIER_SPREAD)
    pool = _POOL.get(key)
    if pool is not None:
        return pool
    pool = []
    allPool = []
    try:
        from items import vehicles as _veh
        import nations
        try:
            from gui.mods.offhangar import matchmaker as _mm
            exclNames = _mm.excludedNames()
        except Exception:
            _mm = None
            exclNames = set()
        for nationID in range(len(nations.NAMES)):
            try:
                lst = _veh.g_list.getList(nationID)
            except Exception:
                continue
            for innationID, d in lst.items():
                try:
                    tags = d.get('tags', ())
                    if ('SPG' in tags and not BOT_ALLOW_SPG) or 'observer' in tags or 'secret' in tags:
                        continue
                    if _mm is not None and _mm.isExcluded(d.get('name', ''), exclNames):
                        continue
                    allPool.append((nationID, innationID))
                    lv = d.get('level', 0)
                    if level is None or abs(int(lv) - int(level)) <= BOT_TIER_SPREAD:
                        pool.append((nationID, innationID))
                except Exception:
                    pass
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if not pool:
        pool = allPool
    LOG_DEBUG('[BOTS] vehicle pool for tier %s: %s tanks' % (level, len(pool)))
    _POOL[key] = pool
    return pool


def _randomCompDescr():
    """Случайная машина из всех танков игры, по уровню близкая к машине игрока."""
    try:
        from items import vehicles as _veh
        level = None
        try:
            level = int(_getPlayerVehicle().typeDescriptor.type.level)
        except Exception:
            pass
        pool = _vehiclePool(level)
        for _ in range(8):
            if not pool:
                break
            nationID, innationID = random.choice(pool)
            try:
                cd = _compDescrFor(nationID, innationID)
                if cd is not None:
                    return cd
            except Exception:
                LOG_CURRENT_EXCEPTION()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return None


def _groundY(spaceID, x, z):
    try:
        res = BigWorld.wg_collideSegment(
            spaceID, Math.Vector3(x, 600.0, z), Math.Vector3(x, -100.0, z), 0)
        if res is not None and res[0] is not None:
            return float(res[0].y) + 0.3
    except Exception:
        pass
    return 0.0


_SPAWN_TAKEN = []      # (x, z) уже занятые точки спавна в этом бою


def _spawnClear(spaceID, x, z, yaw, maxSlope=None):
    """Можно ли поставить танк в (x, z)? Возвращает высоту поверхности или None. Проверки: под лучом не крыша/мост/крона (под верхней
    поверхностью нет второй ниже 2.5 м), поверхность не отвесная, уклон под корпусом небольшой, внутри контура корпуса нет статики."""
    slope = BOT_SPAWN_MAX_SLOPE if maxSlope is None else maxSlope
    try:
        top = Math.Vector3(x, 600.0, z)
        res = BigWorld.wg_collideSegment(spaceID, top, Math.Vector3(x, -100.0, z), 0)
        if res is None or res[0] is None:
            return None
        y0 = float(res[0].y)
        try:
            if len(res) > 1 and abs(float(res[1][1])) < 0.6:
                return None                     # отвесная поверхность (стена, склон)
        except Exception:
            pass
        # под верхней поверхностью есть ещё одна заметно ниже -> мы на крыше/мосту/объекте, а не на земле
        below = BigWorld.wg_collideSegment(spaceID, Math.Vector3(x, y0 - 0.1, z), Math.Vector3(x, y0 - 40.0, z), 0)
        if below is not None and below[0] is not None and y0 - float(below[0].y) > 2.5:
            return None
        # над поверхностью нет потолка (мост, навес) в пределах высоты танка
        sx, sz = math.sin(yaw), math.cos(yaw)
        rx, rz = sz, -sx
        L, W = 4.6, 2.2
        pts = [(x + sx * L, z + sz * L), (x - sx * L, z - sz * L), (x + rx * W, z + rz * W), (x - rx * W, z - rz * W),
               (x + sx * L + rx * W, z + sz * L + rz * W), (x + sx * L - rx * W, z + sz * L - rz * W),
               (x - sx * L + rx * W, z - sz * L + rz * W), (x - sx * L - rx * W, z - sz * L - rz * W)]
        # уклон: высоты под углами корпуса
        for (px, pz) in pts:
            r2 = BigWorld.wg_collideSegment(spaceID, Math.Vector3(px, y0 + 6.0, pz), Math.Vector3(px, y0 - 6.0, pz), 0)
            if r2 is None or r2[0] is None:
                return None
            if abs(float(r2[0].y) - y0) > slope * 4.6 + 0.3:
                return None
        # статика внутри контура: горизонтальные лучи от центра к контуру на двух высотах
        for h in (0.9, 1.9):
            start = Math.Vector3(x, y0 + h, z)
            for (px, pz) in pts:
                r3 = BigWorld.wg_collideSegment(spaceID, start, Math.Vector3(px, y0 + h, pz), 0)
                if r3 is None or r3[0] is None:
                    continue
                try:
                    if len(r3) > 1 and abs(float(r3[1][1])) > 0.45:
                        continue                # пологая поверхность - земля
                except Exception:
                    pass
                return None
        up = BigWorld.wg_collideSegment(spaceID, Math.Vector3(x, y0 + 0.3, z), Math.Vector3(x, y0 + 4.0, z), 0)
        if up is not None and up[0] is not None:
            return None
        return y0
    except Exception:
        return None


def _safeSpawnPoint(spaceID, x, z, yaw):
    """(x, z) из плана, если там свободно, иначе ближайшая свободная точка по расширяющимся кольцам. Занятые точки учитываются."""
    if not BOT_SPAWN_CHECK:
        return x, z
    def free(px, pz):
        for (tx, tz) in _SPAWN_TAKEN:
            if (tx - px) ** 2 + (tz - pz) ** 2 < BOT_SPAWN_GAP ** 2:
                return False
        return _spawnClear(spaceID, px, pz, yaw) is not None
    for r in BOT_SPAWN_SEARCH:
        n = 1 if r == 0.0 else max(8, int(r * 0.7))
        off = random.uniform(0.0, 6.283)
        for k in range(n):
            a = off + 6.283 * k / n
            px, pz = x + math.cos(a) * r, z + math.sin(a) * r
            if free(px, pz):
                if r > 0.0:
                    LOG_DEBUG('[BOTS] spawn point (%.0f, %.0f) is blocked -> moved %.0f m to (%.0f, %.0f)' % (x, z, r, px, pz))
                _SPAWN_TAKEN.append((px, pz))
                return px, pz
    LOG_DEBUG('[BOTS] no free spawn point near (%.0f, %.0f), using the planned one' % (x, z))
    _SPAWN_TAKEN.append((x, z))
    return x, z


# ============================================================
# Spawn points
# ============================================================
def _arenaType(avatar):
    try:
        import ArenaType
        return ArenaType.g_cache.get(avatar.arenaTypeID)
    except Exception:
        return None


def _readXmlPoints(geometryName, group, team):
    pts = []
    try:
        section = ResMgr.openSection('scripts/arena_defs/%s.xml' % geometryName)
        if section is None:
            return pts
        sub = section['gameplayTypes/ctf/%s/team%d' % (group, team)]
        if sub is None:
            return pts
        for i in range(1, 16):
            s = sub.readString('position%d' % i, '')
            if not s:
                if i > 1:
                    break
                continue
            parts = s.split()
            if len(parts) >= 2:
                pts.append((float(parts[0]), float(parts[1])))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return pts


def _enemyPoints(avatar, playerPos):
    at = _arenaType(avatar)
    if at is not None:
        geometryName = getattr(at, 'geometryName', None)
        if geometryName:
            for group in ('teamSpawnPoints', 'teamBasePositions'):
                pts = _readXmlPoints(geometryName, group, TEAM_ENEMY)
                if pts:
                    LOG_DEBUG('[BOTS] enemy spawn points from xml (%s): %s' % (group, pts))
                    return pts
        # запасной вариант: зеркальная точка относительно центра арены
        try:
            bb = getattr(at, 'boundingBox', None)
            if bb is not None:
                cx = (float(bb[0][0]) + float(bb[1][0])) * 0.5
                cz = (float(bb[0][1]) + float(bb[1][1])) * 0.5
                p = (2.0 * cx - playerPos.x, 2.0 * cz - playerPos.z)
                LOG_DEBUG('[BOTS] enemy spawn point mirrored via bounding box: %s' % (p,))
                return [p]
        except Exception:
            LOG_CURRENT_EXCEPTION()
    p = (playerPos.x + 300.0, playerPos.z)
    LOG_ERROR('[BOTS] no enemy spawn data, fallback point %s' % (p,))
    return [p]


def _formation(cx, cz, dirX, dirZ, count, skipCenter):
    """Расставляет count машин рядами поперёк направления (dirX, dirZ), ряд - 8 машин, шаг 9 м."""
    rightX, rightZ = dirZ, -dirX
    pts = []
    k = 0
    while len(pts) < count:
        row = k // 8
        col = k % 8
        k += 1
        lat = (col - 3.5) * 9.0
        if skipCenter and row == 0 and abs(lat) < 5.0:
            continue
        back = row * 14.0
        pts.append((cx + rightX * lat - dirX * back, cz + rightZ * lat - dirZ * back))
    return pts


def _makePlan(avatar):
    """Точки спавна для каждой команды (один раз на бой)."""
    pv = _getPlayerVehicle()
    if pv is None:
        return False
    playerPos = _posOf(pv)

    plan = {TEAM_ALLY: [], TEAM_ENEMY: []}

    ex, ez = _enemyPoints(avatar, playerPos)[0]
    dx, dz = ex - playerPos.x, ez - playerPos.z
    d = math.hypot(dx, dz) or 1.0
    dirX, dirZ = dx / d, dz / d          # от игрока к базе врагов

    # союзники: ряды вокруг игрока (позади него), враги: ряды у вражеской базы (позади, от центра)
    plan[TEAM_ALLY] = _formation(playerPos.x, playerPos.z, dirX, dirZ, BOT_ALLY_COUNT, True)
    plan[TEAM_ENEMY] = _formation(ex, ez, -dirX, -dirZ, BOT_ENEMY_COUNT, False)
    if TUTORIAL[0]:
        _TUT_BOT_IDX[0] = 0
        # учебный бой: мишени в 130 м впереди игрока (а не на вражеской базе)
        tx, tz = playerPos.x + dirX * 130.0, playerPos.z + dirZ * 130.0
        plan[TEAM_ENEMY] = _formation(tx, tz, -dirX, -dirZ, BOT_ENEMY_COUNT, False)
        plan[TEAM_ALLY] = []
        if all(n in TUT_POINTS for n in (1, 2, 3)):
            plan[TEAM_ENEMY] = [(TUT_POINTS[n][0], TUT_POINTS[n][2]) for n in (1, 2, 3)][:BOT_ENEMY_COUNT]

    _PLAN.clear()
    _PLAN.update(plan)
    _LANES.clear()
    del _SPAWN_TAKEN[:]
    _SPAWN_TAKEN.append((playerPos.x, playerPos.z))
    _AXIS.clear()
    _AXIS['ally'] = (playerPos.x, playerPos.z)
    _AXIS['enemy'] = (ex, ez)
    _AXIS['dir'] = (dirX, dirZ)
    _AXIS['bbox'] = None
    try:
        bb = getattr(_arenaType(avatar), 'boundingBox', None)
        if bb is not None:
            _AXIS['bbox'] = (float(bb[0][0]), float(bb[0][1]), float(bb[1][0]), float(bb[1][1]))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    LOG_DEBUG('[BOTS] spawn plan: %s allies, %s enemies' % (len(plan[TEAM_ALLY]), len(plan[TEAM_ENEMY])))
    _startNav(avatar)
    return True


NAV_ENABLED = True
NAV_MAX_SLOPE = 0.58
NAV_START_DELAY = 4.0     # пауза перед построением: дать карте подгрузить геометрию


def _navProbe(spaceID):
    def probe(x, z):
        try:
            g = BigWorld.wg_collideSegment(spaceID, Math.Vector3(x, 600.0, z), Math.Vector3(x, -100.0, z), 0)
            if g is None or g[0] is None:
                return None                     # геометрия тут ещё не загружена - клетка неизвестна (считается свободной)
        except Exception:
            return None
        y0 = _spawnClear(spaceID, x, z, 0.0, NAV_MAX_SLOPE)
        if y0 is None:
            return False
        if _spawnClear(spaceID, x, z, math.pi * 0.5, NAV_MAX_SLOPE) is None:
            return False
        try:
            d = BigWorld.wg_collideWater(Math.Vector3(x, y0 - 0.3, z), Math.Vector3(x, y0 + 14.0, z))
            if d is not None and d >= 0.0 and d - 0.3 > off_nav.WATER_MAX_DEPTH:
                return False
        except Exception:
            pass
        return True
    return probe


def _startNav(avatar):
    """Строит карту проходимости (в фоне, маленькими шагами), по ней боты объезжают дома, стены и глубокую воду."""
    off_nav.NAV.reset()
    bbox = _AXIS.get('bbox')
    if not NAV_ENABLED or bbox is None:
        return
    try:
        sid = avatar.spaceID
        BigWorld.callback(NAV_START_DELAY, lambda t=off_nav.NAV.token: (
            off_nav.NAV.token == t and off_nav.NAV.start(bbox, _navProbe(sid), BigWorld.callback, LOG_NOTE)))
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Arena registration
# ============================================================
_NAME_WORDS = (
    'Jove', 'Kolya', 'Vasya', 'Sanya', 'Dimon', 'Tolik', 'Zhenya', 'Max', 'Alex', 'Ivan', 'Gena', 'Stas',
    'Wolf', 'Bear', 'Tiger', 'Falcon', 'Raven', 'Viper', 'Cobra', 'Shark', 'Hunter', 'Sniper', 'Ghost',
    'Shadow', 'Storm', 'Thunder', 'Blitz', 'Panzer', 'Tanker', 'Steel', 'Iron', 'Rusty', 'Fury', 'Titan',
    'Nomad', 'Rogue', 'Saber', 'Comet', 'Frost', 'Blaze', 'Crow', 'Lynx', 'Orion', 'Zeus', 'Ares', 'Ninja',
    'Pro100', 'Killer', 'Dozer', 'Mauser', 'Tiger1', 'Hellcat', 'Boris', 'Dmitry', 'Sergey', 'Artem',
    'Kirill', 'Oleg', 'Pavel', 'Roman', 'Egor', 'Igor', 'Misha', 'Fedya', 'Lesha', 'Danil', 'Tema',
    'Hans', 'Klaus', 'Fritz', 'Otto', 'John', 'Mike', 'Tom', 'Jack', 'Luke', 'Mark', 'Pierre', 'Jean',
    'Marco', 'Luca', 'Jan', 'Piotr', 'Tomek', 'Kuba', 'Mateusz', 'Yuri', 'Anton', 'Victor', 'Vlad',
)
_NAME_SUFFIX = ('_rus', '_ru', '_ua', '_pro', '_wot', '_tank', '_pl', '_de', '_fr', '_cz', '_by', '_kz',
                'TV', 'HD', 'XXL', '_off', '_1337', '_2000', '_007', '_vs', 'Boy', 'Man', 'ski', 'off')
_NAME_PREFIX = ('xX', 'The', 'Mr', 'Dr', 'Big', 'Mad', 'Evil', 'Dark', 'Red', 'Old', 'Iron', 'Pro', 'Super',
                'Lord', 'Ded', 'Bad', 'Cool')
_USED_NAMES = set()
_BOT_NAME_BY_ID = {}      # botID -> ник (нужен и экрану результатов боя)
_BOT_CLANS = ('CCCP', 'USSR', 'RUS', 'T-34', 'RED', 'WOT', 'ARMY', 'KV', 'RAT', '404',
              'URAL', 'SIB', 'VODKA', 'TANK', '1337', 'RU-7', 'ZOV', 'BARS', 'BOOM')


def _randomNick():
    """Случайный ник в стиле WoT: Jove, Kolya22_rus, xXWolfXx, Mad_Tiger77 и т.п."""
    r = random.random
    w = random.choice(_NAME_WORDS)
    style = r()
    if style < 0.18:
        nick = w                                                    # Jove
    elif style < 0.45:
        nick = '%s%d' % (w, random.choice((random.randint(1, 99), random.randint(1980, 2009),
                                           random.randint(100, 999))))     # Kolya22
    elif style < 0.62:
        nick = '%s%s' % (w, random.choice(_NAME_SUFFIX))            # Kolya_rus
    elif style < 0.78:
        nick = '%s%d%s' % (w, random.randint(1, 99), random.choice(_NAME_SUFFIX))   # Kolya22_rus
    elif style < 0.88:
        nick = '%s_%s' % (random.choice(_NAME_PREFIX), w)           # Mad_Tiger
        if r() < 0.5:
            nick += str(random.randint(1, 99))
    elif style < 0.94:
        nick = 'xX%sXx' % w                                         # xXWolfXx
    else:
        w2 = random.choice(_NAME_WORDS)
        nick = '%s%s' % (w, w2) if r() < 0.5 else '%s_%s' % (w, w2)  # WolfRaven / Wolf_Raven
    if r() < 0.08:
        nick = nick.lower()
    elif r() < 0.05:
        nick = nick.upper()
    return nick[:24]


def _uniqueNick():
    own = getattr(off_battle, 'OFFLINE_NICKNAME', None)
    for _ in range(80):
        base = _randomNick()
        # Only some players belong to clans, like a normal random battle.
        if random.random() < 0.42:
            clan = random.choice(_BOT_CLANS)
            room = max(3, 24 - len(clan) - 3)
            n = '%s [%s]' % (base[:room], clan)
        else:
            n = base[:24]
        if n not in _USED_NAMES and n != own and len(n) >= 3:
            _USED_NAMES.add(n)
            return n
    n = '%s%d' % (random.choice(_NAME_WORDS)[:10], random.randint(1000, 99999))
    n = n[:24]
    _USED_NAMES.add(n)
    return n


def botName(botID):
    """Ник бота (для экрана результатов)."""
    return _BOT_NAME_BY_ID.get(botID)


_TUT_BOT_IDX = [0]


def _tutorialBotName():
    """Учебный бой: враги называются #battle_tutorial:bot_1..3 - по этим именам обучение находит своих ботов (vehOnArena)."""
    _TUT_BOT_IDX[0] += 1
    if _TUT_BOT_IDX[0] <= 3:
        return '#battle_tutorial:bot_%d' % _TUT_BOT_IDX[0]
    return None


def _makePublicInfo(compDescr, team, botID):
    info = off_battle._PublicInfo(compDescr, team)
    name = None
    if TUTORIAL[0] and team == TEAM_ENEMY:
        name = _tutorialBotName()
    if name is None:
        name = _uniqueNick()
    _BOT_NAME_BY_ID[botID] = name
    dbid = -botID
    info['name'] = name
    info['accountDBID'] = dbid
    try:
        info.name = name
        info.accountDBID = dbid
    except Exception:
        pass
    return info


def _arenaTuple(vid, info):
    t = list(off_battle._vehInfoTuple(vid, info['compDescr'], info['team']))
    t[2] = info['name']
    t[7] = info['accountDBID']
    return tuple(t)


def _bindPending(vehicle):
    """Сопоставляет только что появившуюся Vehicle-сущность с записью о боте.
    Порядок: уже привязана по id -> по publicInfo.accountDBID -> по близости к точке спавна."""
    for rec in _PENDING_BOTS:
        if rec.get('vid') == vehicle.id:
            return rec

    dbid = None
    try:
        dbid = int(vehicle.publicInfo['accountDBID'])
    except Exception:
        pass
    if dbid is not None and dbid < 0:
        for rec in _PENDING_BOTS:
            if rec['botID'] == -dbid and rec.get('vid') is None:
                rec['vid'] = vehicle.id
                return rec

    try:
        p = vehicle.position
        best = None
        for rec in _PENDING_BOTS:
            if rec.get('vid') is not None:
                continue
            d = math.hypot(p.x - rec['x'], p.z - rec['z'])
            if d < 6.0 and (best is None or d < best[0]):
                best = (d, rec)
        if best is not None:
            best[1]['vid'] = vehicle.id
            return best[1]
    except Exception:
        pass
    return None


def _registerArenaVehicle(vehicle, rec=None):
    """Добавляет бота в список машин арены (один раз). Безопасно вызывать повторно."""
    try:
        vid = vehicle.id
        if vid in _REGISTERED_IDS:
            return
        if rec is not None:
            info = rec['vprops']['publicInfo']
        else:
            info = vehicle.publicInfo
            if int(info['accountDBID']) >= 0:
                return
        avatar = BigWorld.player()
        if avatar is None:
            return
        _REGISTERED_IDS.add(vid)
        _updateArena(avatar, ARENA_UPDATE.VEHICLE_ADDED, cPickle.dumps(_arenaTuple(vid, info)))
        LOG_DEBUG('[BOTS] vehicle %s (%s, team %s) added to arena' % (vid, info['name'], info['team']))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def preStartVisual(vehicle):
    """Вызывается из battle._safe_startVisual ДО Vehicle.startVisual: без записи в списке машин арены
    VehicleAppearance падает с KeyError в __createStickers."""
    try:
        if not _active():
            return
        avatar = BigWorld.player()
        if vehicle.id == getattr(avatar, 'playerVehicleID', 0):
            return
        rec = _bindPending(vehicle)
        if rec is None and not isBotVehicle(vehicle):
            return
        LOG_DEBUG('[BOTS] preStartVisual: entity %s matched to bot %s' % (
            vehicle.id, rec['botID'] if rec else '(by publicInfo)'))
        _registerArenaVehicle(vehicle, rec)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchVehicleOnEnterWorld():
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        import Vehicle as _V
        orig = _V.Vehicle.onEnterWorld

        def onEnterWorld(self, prereqs, orig=orig):
            try:
                if _active():
                    preStartVisual(self)
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return orig(self, prereqs)

        _V.Vehicle.onEnterWorld = onEnterWorld
        _HOOK_INSTALLED = True
        LOG_NOTE('[BOTS] Vehicle.onEnterWorld hook installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def onVehicleKilled(vehicle, killerID):
    """Вызывается из shooting._applyDamage, когда здоровье машины упало до нуля."""
    try:
        vid = vehicle.id
        if vid in _KILLED_IDS:
            return
        _KILLED_IDS.add(vid)

        for ai in list(_BOTS.values()):
            if ai._vid == vid:
                ai.die()

        avatar = BigWorld.player()
        if avatar is not None:
            _updateArena(avatar, ARENA_UPDATE.VEHICLE_KILLED, cPickle.dumps((vid, killerID, 0)))
            # счётчик фрагов в панелях команд ("уши")
            try:
                killer = BigWorld.entity(killerID)
                if killer is not None and getTeam(killer) != getTeam(vehicle):
                    _FRAGS[killerID] = _FRAGS.get(killerID, 0) + 1
                    _updateArena(avatar, ARENA_UPDATE.VEHICLE_STATISTICS,
                                 cPickle.dumps((killerID, _FRAGS[killerID])))
            except Exception:
                LOG_CURRENT_EXCEPTION()
        LOG_DEBUG('[BOTS] vehicle %s destroyed by %s' % (vid, killerID))
        _checkBattleEnd()
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _checkBattleEnd():
    """Бой заканчивается сам: все враги уничтожены - победа; игрок и все союзники уничтожены - поражение."""
    global _END_SCHEDULED
    if not BOT_AUTO_END or _END_SCHEDULED or not _active():
        return
    if BigWorld.time() < _CHECK_END_AFTER or _PENDING_BOTS:
        return
    enemiesAlive = sum(1 for b in _BOTS.values() if b.team == TEAM_ENEMY and b.alive)
    alliesAlive = sum(1 for b in _BOTS.values() if b.team == TEAM_ALLY and b.alive)
    pv = _getPlayerVehicle()
    playerAlive = pv is not None and getattr(pv, 'health', 0) > 0
    total = sum(1 for b in _BOTS.values() if b.team == TEAM_ENEMY)
    if total == 0:
        return
    if enemiesAlive == 0 or (not playerAlive and alliesAlive == 0):
        _END_SCHEDULED = True
        LOG_NOTE('[BOTS] battle over: enemiesAlive=%s alliesAlive=%s playerAlive=%s -> results in %.0fs' % (
            enemiesAlive, alliesAlive, playerAlive, BOT_AUTO_END_DELAY))
        token = _SPAWN_TOKEN

        def _finish():
            if token != _SPAWN_TOKEN or not _active():
                return
            try:
                from gui.mods.offhangar import endbattle
                endbattle.endBattle()
            except Exception:
                LOG_CURRENT_EXCEPTION()

        if TUTORIAL[0] and enemiesAlive == 0:
            # учебный бой: не закрываем бой, пока диктор обучения не договорит (tutor.finishWhenDone)
            try:
                from gui.mods.offhangar import tutor as _tut
                _tut.finishWhenDone(_finish)
                return
            except Exception:
                LOG_CURRENT_EXCEPTION()
        BigWorld.callback(BOT_AUTO_END_DELAY, _finish)


# ============================================================
# Turret
# ============================================================
class BotTurret(object):
    """Наводка башни и орудия бота.

    Башня крутится так же, как у игрока: свои матрицы (_RotAnimator) подставляются как target в
    appearance.turretMatrix / appearance.gunMatrix. Нативный filter.syncGunAngles не используется:
    при отключённой в офлайне нативной физике он валил игру сразу после старта боя."""

    TICK = 0.05

    def __init__(self):
        self._vehicle = None
        self._descr = None
        self.yaw = 0.0
        self.pitch = 0.0
        self.errYaw = 9.9
        self.errPitch = 9.9
        self._point = None
        self._pointSetAt = 0.0
        self._maxTurretSpeed = 0.5
        self._maxGunSpeed = 0.5
        self._yawLimits = None
        self._cbID = None
        self._lastTime = 0.0
        self._turretAnim = off_turret._RotAnimator()
        self._gunAnim = off_turret._RotAnimator()
        self._firstUpdateLogged = False

    def setup(self, vehicle):
        self._vehicle = weakref.proxy(vehicle)
        self._descr = vehicle.typeDescriptor
        self._maxTurretSpeed = self._descr.turret['rotationSpeed']
        self._maxGunSpeed = self._descr.gun['rotationSpeed']
        self._yawLimits = self._descr.gun['turretYawLimits']

        self._applyMatrices(0.0)
        try:
            app = vehicle.appearance
            app.turretMatrix.target = self._turretAnim.matrix
            app.gunMatrix.target = self._gunAnim.matrix
            LOG_DEBUG('[BOTS] turret matrices attached for vehicle %s' % vehicle.id)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def setTarget(self, point):
        if point is None:
            self._point = None
            self.errYaw = self.errPitch = 9.9
            return
        if self._point is None:
            self._pointSetAt = BigWorld.time()
        self._point = point

    def isAimed(self):
        if self._point is None:
            return False
        if BigWorld.time() - self._pointSetAt < 0.3:
            return False
        return abs(self.errYaw) < BOT_AIM_TOLERANCE and abs(self.errPitch) < BOT_AIM_TOLERANCE

    def start(self):
        if self._cbID is not None:
            return
        self._lastTime = BigWorld.time()
        self._cbID = BigWorld.callback(0.0, self._tick)

    def stop(self):
        if self._cbID is not None:
            try:
                BigWorld.cancelCallback(self._cbID)
            except Exception:
                pass
            self._cbID = None

    def _tick(self):
        now = BigWorld.time()
        dt = now - self._lastTime
        self._lastTime = now
        self._cbID = BigWorld.callback(self.TICK, self._tick)
        if dt <= 0.0:
            return
        dt = min(dt, 0.2)
        try:
            vehicle = self._vehicle
            if not getattr(vehicle, 'isStarted', False):
                return
            if off_battle.isPrebattle() or BOT_DEBUG_NO_TURRET:
                return
            self._update(vehicle, dt)
        except ReferenceError:
            self.stop()
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _update(self, vehicle, dt):
        point = self._point
        if point is None:
            return
        descr = self._descr

        if not self._firstUpdateLogged:
            self._firstUpdateLogged = True
            LOG_DEBUG('[BOTS] turret first update, vehicle %s' % vehicle.id)

        vehMat = Math.Matrix(vehicle.model.matrix)

        shotYaw, shotPitch = getShotAngles(descr, vehMat, (self.yaw, self.pitch), point)

        turretK = 1.0
        try:
            from gui.mods.offhangar import devices as _dev
            _f = _dev.getFactors(vehicle.id)
            if _f is not None:
                turretK = _f['turret']
        except Exception:
            pass
        self.yaw = off_turret._stepTurretYaw(
            self.yaw, shotYaw, self._maxTurretSpeed * dt * turretK, self._yawLimits)
        limits = calcPitchLimitsFromDesc(self.yaw, descr.gun['pitchLimits'])
        self.pitch = off_turret._stepGunPitch(
            self.pitch, shotPitch, self._maxGunSpeed * dt, limits)

        self.errYaw = _wrap(shotYaw - self.yaw)
        self.errPitch = shotPitch - self.pitch

        self._applyMatrices(dt)

    def _applyMatrices(self, dt):
        turretMat = Math.Matrix()
        turretMat.setRotateY(self.yaw)
        self._turretAnim.update(turretMat, dt)

        gunMat = Math.Matrix()
        gunMat.setRotateX(self.pitch)
        self._gunAnim.update(gunMat, dt)


# ============================================================
# Bot AI
# ============================================================
class BotAI(object):
    def __init__(self, vehicle, botID, team, role='medium'):
        self.role = role
        self._route = None
        self._routeIdx = 0
        self._battleStart = BigWorld.time()
        self._vehicle = weakref.proxy(vehicle)
        self._vid = vehicle.id
        self.botID = botID
        self.team = team
        self.alive = True

        self._physics = None
        self._turret = None
        self._spgHome = None
        self._ready = False
        self._kicked = False
        self._waited = 0.0

        self._cbID = None
        self._lastTime = 0.0
        self._far = False
        self._far2 = False
        self._lodNext = 0.0
        self._avoidCache = None

        self._targetID = None
        self._follow = False
        self._retarget = 0.0
        self._aimPoint = None
        self._aimOffset = (0.0, 0.0, 0.0)

        self._holdMode = 0
        self._holdUntil = 0.0

        self._lastPos = None
        self._lastCheck = 0.0
        self._stuckTime = 0.0
        self._moveDir = 0
        self._unstuckUntil = 0.0
        self._unstuckMove = 0
        self._unstuckTurn = 1
        self._stuckCount = 0           # подряд застреваний за короткое время (растёт длительность и дальность манёвра)
        self._lastStuckAt = -99.0
        self._detour = None            # (x, z, until) - боковая точка объезда после застревания
        self._avoidAng = 0.0
        self._avoidUntil = 0.0
        self._avoidLogN = 0

        self._shootCd = random.uniform(1.0, 3.0)
        self._thinkLogged = False
        self._fireDiag = False
        self._threatID = None
        self._threatUntil = 0.0
        self._ramCooldowns = {}

    # --------------------------------------------------------
    def start(self):
        if self._cbID is not None:
            return
        self._lastTime = BigWorld.time()
        # фазы ИИ разнесены по времени: 29 ботов не думают в один и тот же кадр (иначе раз в 0.1 с - провал FPS)
        self._cbID = BigWorld.callback(random.uniform(0.0, BOT_AI_TICK), self._tick)

    def stop(self):
        if self._cbID is not None:
            try:
                BigWorld.cancelCallback(self._cbID)
            except Exception:
                pass
            self._cbID = None
        if self._turret is not None:
            self._turret.stop()
        if self._physics is not None:
            try:
                self._physics.stop()
            except Exception:
                pass
            try:
                off_battle._simplePhysicsByVehID.pop(self._vid, None)
            except Exception:
                pass

    def die(self):
        if not self.alive:
            return
        self.alive = False
        # ИИ и башню останавливаем, а физику оставляем: она держит остов на месте
        # (если её остановить, модель возвращается к позиции сущности - на базу)
        if self._cbID is not None:
            try:
                BigWorld.cancelCallback(self._cbID)
            except Exception:
                pass
            self._cbID = None
        if self._turret is not None:
            self._turret.stop()
        if self._physics is not None:
            try:
                self._physics.setFlags(0)
                self._physics._curSpeed = 0.0
            except Exception:
                pass
        LOG_DEBUG('[BOTS] bot %s (team %s) is dead' % (self.botID, self.team))

    def vehicle(self):
        """Настоящая сущность (не weakref-proxy): нужна, чтобы `e is shooter` в поиске попаданий работало."""
        return BigWorld.entity(self._vid)

    # --------------------------------------------------------
    def _tick(self):
        t0 = _perf.clock()
        try:
            self._tickImpl()
        finally:
            _perf.add('ai', _perf.clock() - t0)

    def _updateLod(self, now):
        """Далёкие от игрока боты думают и считают физику реже (интерполяция модели скрывает разницу)."""
        if now < self._lodNext:
            return
        self._lodNext = now + 0.5 + random.uniform(0.0, 0.2)
        pp = off_battle._smoothPlayerPos(now)
        phys = self._physics
        if pp is None or phys is None:
            self._far = False
            self._far2 = False
            return
        p = phys.pos
        self._far = abs(p.x - pp.x) > BOT_LOD_DIST or abs(p.z - pp.z) > BOT_LOD_DIST
        self._far2 = abs(p.x - pp.x) > BOT_LOD_DIST2 or abs(p.z - pp.z) > BOT_LOD_DIST2
        phys.lite = 2 if self._far else 1
        phys.tickInterval = BOT_PHYSICS_TICK_FAR2 if self._far2 else (BOT_PHYSICS_TICK_FAR if self._far else BOT_PHYSICS_TICK)

    def _tickImpl(self):
        self._cbID = BigWorld.callback(BOT_AI_TICK_FAR2 if getattr(self, '_far2', False) else (BOT_AI_TICK_FAR if self._far else BOT_AI_TICK), self._tick)

        now = BigWorld.time()
        dt = now - self._lastTime
        self._lastTime = now
        if dt <= 0.0:
            return
        dt = min(dt, 0.25)

        if not _active():
            self.stop()
            return

        veh = self.vehicle()
        if veh is None:
            self.stop()
            return

        if getattr(veh, 'health', 1) <= 0:
            self.die()
            return

        if not self._ready:
            if getattr(veh, 'isStarted', False):
                self._setupControllers(veh)
            else:
                self._waited += dt
                if self._waited > 3.0 and not self._kicked:
                    # естественный запуск визуала не сработал - запускаем как для чужой машины игрока
                    self._kicked = True
                    try:
                        BigWorld.player().vehicle_onEnterWorld(veh)
                        LOG_DEBUG('[BOTS] bot %s: visual start kicked' % self.botID)
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                if self._waited > BOT_START_WAIT:
                    LOG_ERROR('[BOTS] bot %s: vehicle visual never started, giving up' % self.botID)
                    self.die()
            return

        self._updateLod(now)
        try:
            self._think(veh, dt)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _setupControllers(self, veh):
        phys = off_battle._simplePhysicsByVehID.get(veh.id)
        if phys is None:
            phys = off_battle.SimpleTankPhysics()
            phys.isBot = True            # не трогать матрицу/камеру игрока
            phys.tickInterval = BOT_PHYSICS_TICK
            phys.setup(veh)
            off_battle._simplePhysicsByVehID[veh.id] = phys
            phys.start()
        else:
            # физику уже запустил патч Vehicle.__startWGPhysics (для ботов - с isBot=True)
            phys.isBot = True
            phys.tickInterval = BOT_PHYSICS_TICK

        turret = BotTurret()
        turret.setup(veh)
        turret.start()

        self._physics = phys
        self._turret = turret
        if self.role == 'spg':
            self._spgHome = (phys.pos.x, phys.pos.z)
            LOG_DEBUG('[BOTS] SPG %s home position set: (%.1f, %.1f)' % (self.botID, phys.pos.x, phys.pos.z))
        self._ready = True
        self._fixVisibility(veh)
        LOG_DEBUG('[BOTS] bot %s (team %s) ready, vehicle id=%s' % (self.botID, self.team, veh.id))

    def _fixVisibility(self, veh):
        """Бот не должен быть "призраком": сбрасываем stipple (полупрозрачность появления),
        включаем targetCaps и пишем в лог состояние для диагностики."""
        try:
            veh.targetCaps = [1]
        except Exception:
            pass
        try:
            if veh.model is not None:
                veh.model.stipple = False
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            BigWorld.callback(2.5, lambda vid=self._vid: _clearStipple(vid))
        except Exception:
            pass
        try:
            st = off_shooting._getState(veh)
            LOG_DEBUG('[BOTS] bot %s diag: health=%s model=%s inWorld=%s pos=%s modelPos=%s ammo=%s' % (
                self.botID, getattr(veh, 'health', '?'), veh.model is not None, getattr(veh, 'inWorld', '?'),
                veh.position, veh.model.position if veh.model is not None else None, st.get('ammo')))
        except Exception:
            LOG_CURRENT_EXCEPTION()

    # --------------------------------------------------------
    # Targets
    # --------------------------------------------------------
    def _enemies(self, spottedOnly=True):
        """Живые враги. spottedOnly: только те, кого видит команда бота (засвет, spotting.py)."""
        res = []
        if self.team == TEAM_ENEMY:
            pv = _getPlayerVehicle()
            if pv is not None and isAlive(pv):
                res.append(pv)
        for b in _BOTS.values():
            if b is self or b.team == self.team or not b.alive:
                continue
            v = b.vehicle()
            if v is not None and isAlive(v):
                res.append(v)
        if spottedOnly:
            res = [v for v in res if off_spotting.isSpottedBy(self.team, v.id)]
        return res

    def _routeOn(self):
        return BOT_USE_ROUTES and bool(_AXIS)

    def _spotLimit(self):
        """Дальность обнаружения врагов, пока бот занят маршрутом/обороной; None - атакует любого."""
        if self.role == 'spg':
            return BOT_SPG_RANGE          # САУ видит врагов по всей дальности огня
        if not self._routeOn():
            return None
        if self._route is None:
            self._route = _buildRoute(self.team, self.role)
            self._routeIdx = 0
        if self._routeIdx < len(self._route):
            return BOT_SPOT_RANGE
        if self.role in ('spg', 'td') and BigWorld.time() - self._battleStart < BOT_PUSH_AFTER:
            return BOT_SPOT_RANGE * 1.3
        return None

    def _searchGoal(self, pos):
        if self.role == 'spg' or not self._routeOn():
            return None
        best = None
        for v in self._enemies(False):
            p = _posOf(v)
            d = (p.x - pos.x) ** 2 + (p.z - pos.z) ** 2
            if best is None or d < best[0]:
                best = (d, p.x, p.z)
        return (best[1], best[2]) if best else None

    def _spreadGoal(self, goal):
        """Каждый бот идёт не в центр цели, а в свою точку рядом с ней: боты не сбиваются в одну кучу."""
        off = getattr(self, '_goalOff', None)
        if off is None:
            a = random.uniform(0.0, 6.283)
            r = random.uniform(25.0, 70.0)
            off = self._goalOff = (math.cos(a) * r, math.sin(a) * r)
        return goal[0] + off[0], goal[1] + off[1]

    _retreatUntil = 0.0
    _retreatDone = False
    capturer = False

    def _captureGoal(self, pos):
        """Точка базы противника для бота-захватчика (None - баз нет)."""
        try:
            from gui.mods.offhangar import capture
            return capture.targetFor(self.team, pos.x, pos.z)
        except Exception:
            return None

    def _retreating(self, veh, pos, now):
        """Бот с малым запасом прочности (<30%) один раз за бой отходит к базе на ~12 с (если рядом не его база)."""
        if self.role == 'spg' or self._follow:
            return False
        if now < self._retreatUntil:
            return True
        if self._retreatDone:
            return False
        try:
            frac = float(veh.health) / float(veh.typeDescriptor.maxHealth)
        except Exception:
            return False
        if frac < 0.30:
            self._retreatDone = True
            if random.random() < 0.75:
                self._retreatUntil = now + random.uniform(10.0, 15.0)
                LOG_DEBUG('[BOTS] bot %s retreats (hp %.0f%%)' % (self.botID, frac * 100.0))
                return True
        return False

    _losCache = (0.0, True, None)

    def _seesTarget(self, pos, tpos, target):
        """Есть ли прямая видимость до цели (статика: дома, стены). Кэш 0.5 с на цель."""
        now = BigWorld.time()
        c = self._losCache
        if c[2] == target.id and now - c[0] < 0.5:
            return c[1]
        ok = True
        try:
            sid = BigWorld.player().spaceID
            r = BigWorld.wg_collideSegment(sid, Math.Vector3(pos.x, pos.y + 2.2, pos.z),
                                           Math.Vector3(tpos.x, tpos.y + 1.8, tpos.z), 0)
            ok = r is None or r[0] is None
        except Exception:
            ok = True
        self._losCache = (now, ok, target.id)
        return ok

    def _navNext(self, pos, gx, gz, now, direct=25.0):
        """Следующая точка пути к цели в обход домов/воды; при близкой цели или без карты - сама цель."""
        nav = off_nav.NAV
        if not nav.ready or math.hypot(gx - pos.x, gz - pos.z) < direct:
            self._navPath = None
            return gx, gz
        path = getattr(self, '_navPath', None)
        goal = getattr(self, '_navGoal', None)
        due = (path is None or not path or goal is None or math.hypot(goal[0] - gx, goal[1] - gz) > 30.0
               or now >= getattr(self, '_navReplanAt', 0.0) or getattr(self, '_navForce', False))
        if due and nav.canPlan(now):
            self._navForce = False
            self._navGoal = (gx, gz)
            far = math.hypot(gx - pos.x, gz - pos.z) > 200.0
            self._navReplanAt = now + (30.0 if far else 18.0) + random.uniform(0.0, 6.0)
            try:
                p = nav.plan(pos.x, pos.z, gx, gz, now, self.botID)
            except Exception:
                LOG_CURRENT_EXCEPTION()
                p = None
            self._navPath = list(p) if p else None
            path = self._navPath
        if not path:
            return gx, gz
        while len(path) > 1 and math.hypot(path[0][0] - pos.x, path[0][1] - pos.z) < 10.0:
            path.pop(0)
        return path[0]

    def _waypoint(self, pos):
        if not self._routeOn() or not self._route:
            return None
        while self._routeIdx < len(self._route):
            wx, wz = self._route[self._routeIdx]
            if math.hypot(wx - pos.x, wz - pos.z) <= BOT_ROUTE_REACH:
                self._routeIdx += 1
                continue
            return wx, wz
        return None

    def _currentTarget(self):
        if self._targetID is None:
            return None
        v = BigWorld.entity(self._targetID)
        if v is not None and isAlive(v):
            return v
        return None

    def _pickTarget(self, pos, dt):
        self._retarget -= dt
        # Получив урон, бот немедленно отвечает стрелявшему, даже если тот ещё далеко от маршрута.
        if self._threatID is not None and BigWorld.time() < self._threatUntil:
            threat = BigWorld.entity(self._threatID)
            if threat is not None and isAlive(threat) and getTeam(threat) != self.team:
                self._targetID = threat.id
                self._follow = False
                self._retarget = BOT_RETARGET_INTERVAL
                return threat
        cur = self._currentTarget()
        if cur is not None and self._retarget > 0.0:
            return cur
        self._retarget = BOT_RETARGET_INTERVAL

        def dist(v):
            p = _posOf(v)
            return math.hypot(p.x - pos.x, p.z - pos.z)

        self._follow = False
        cands = self._enemies()
        limit = self._spotLimit()
        if limit is not None:
            cands = [c for c in cands if dist(c) <= limit]
            if not cands:
                self._targetID = None
                return None
        if cands and self.role == 'spg':
            # Арта выбирает цель случайно среди ВСЕХ засвеченных врагов (игрок и союзные боты), а не ближайшую = обычно игрока.
            now = BigWorld.time()
            if cur is not None and any(c.id == cur.id for c in cands) and now < getattr(self, '_spgHold', 0.0):
                best = cur
            else:
                others = [c for c in cands if cur is None or c.id != cur.id] or cands      # новая цель - не та же самая
                best = random.choice(others)
                self._spgHold = now + random.uniform(*BOT_SPG_RETARGET)
                LOG_DEBUG('[BOTS] SPG %s new target %s (of %d spotted)' % (self.botID, best.id, len(cands)))
            self._targetID = best.id
            return best
        if cands:
            best = min(cands, key=dist)
            if cur is not None and any(c.id == cur.id for c in cands):
                if dist(cur) <= dist(best) * 1.3 + 15.0:
                    best = cur
            self._targetID = best.id
            return best

        if self.team == TEAM_ALLY and not self._enemies(False):
            pv = _getPlayerVehicle()
            if pv is not None and isAlive(pv):
                self._follow = True
                self._targetID = pv.id
                return pv

        self._targetID = None
        return None

    # --------------------------------------------------------
    # Think
    # --------------------------------------------------------
    def _think(self, veh, dt):
        phys = self._physics
        turret = self._turret

        if off_battle.isPrebattle():
            phys.setFlags(0)
            return

        if TUTORIAL[0]:
            path = None
            try:
                nm = str(veh.publicInfo['name'])
                if nm.startswith('#battle_tutorial:bot_'):
                    path = TUT_PATROL.get(int(nm.rsplit('_', 1)[-1]))
            except Exception:
                path = None
            if path:
                # мишень-патруль (бот 3): едет между точками пути карты туда-обратно
                i = getattr(self, '_patIdx', 0) % len(path)
                dxp, dzp = path[i][0] - phys.pos.x, path[i][1] - phys.pos.z
                dp = math.hypot(dxp, dzp)
                if dp < 6.0:
                    self._patIdx = (i + 1) % len(path)
                else:
                    phys.setFlags(self._steer(dt, phys.pos, dp, math.atan2(dxp, dzp), 1.0, 6.0))
            else:
                phys.setFlags(0)              # учебный бой: остальные боты - неподвижные мишени
            try:
                turret.setTarget(None)
            except Exception:
                pass
            return

        pos = phys.pos
        self._ramDamageTick(veh)
        if self.role == 'spg':
            try:
                if self._spgDanger(veh, pos, dt):
                    return
            except Exception:
                LOG_CURRENT_EXCEPTION()
        target = self._pickTarget(pos, dt)
        if target is not None and self.capturer and not self._follow:
            tp0 = _posOf(target)
            if math.hypot(tp0.x - pos.x, tp0.z - pos.z) > 90.0:
                target = None                    # захватчик не гоняется за далёкими целями - ему на базу
        if target is None:
            if self.capturer and not self._retreating(veh, pos, now=BigWorld.time()):
                cg = self._captureGoal(pos)
                if cg is not None:
                    turret.setTarget(None)
                    self._aimPoint = None
                    nx, nz = self._navNext(pos, cg[0], cg[1], BigWorld.time(), 30.0)
                    phys.setFlags(self._steer(dt, pos, math.hypot(cg[0] - pos.x, cg[1] - pos.z),
                                              math.atan2(nx - pos.x, nz - pos.z), 5.0, 22.0))
                    return
            if self.role == 'spg' and self._spgHome is not None:
                hx, hz = self._spgHome
                dxh, dzh = hx - pos.x, hz - pos.z
                dh = math.hypot(dxh, dzh)
                phys.setFlags(0 if dh <= BOT_SPG_HOME_RADIUS else self._steer(dt, pos, dh, math.atan2(dxh, dzh), 3.0, BOT_SPG_HOME_RADIUS))
                try:
                    self._spgHarassTick(veh, pos, dt)
                except Exception:
                    LOG_CURRENT_EXCEPTION()
                turret.setTarget(None)
                self._aimPoint = None
                return
            if self._retreating(veh, pos, now=BigWorld.time()):
                base = _AXIS.get('ally' if self.team == TEAM_ALLY else 'enemy')
                if base:
                    nx, nz = self._navNext(pos, base[0], base[1], BigWorld.time(), 30.0)
                    turret.setTarget(None)
                    self._aimPoint = None
                    phys.setFlags(self._steer(dt, pos, max(25.0, math.hypot(base[0] - pos.x, base[1] - pos.z)),
                                              math.atan2(nx - pos.x, nz - pos.z), 20.0, 40.0))
                    return
            wp = self._waypoint(pos)
            if wp is not None:
                wp = self._navNext(pos, wp[0], wp[1], BigWorld.time())
                dxw, dzw = wp[0] - pos.x, wp[1] - pos.z
                turret.setTarget(None)
                self._aimPoint = None
                phys.setFlags(self._steer(dt, pos, math.hypot(dxw, dzw), math.atan2(dxw, dzw), 1.0, 6.0))
                if not self._thinkLogged:
                    self._thinkLogged = True
                    LOG_DEBUG('[BOTS] bot %s route (%s): %s' % (self.botID, self.role, self._route))
                return
            goal = self._searchGoal(pos)
            if goal is not None:
                # никого не видно и маршрут пройден: идём к ближайшему врагу, пока не засветим его
                goal = self._spreadGoal(goal)
                realD = math.hypot(goal[0] - pos.x, goal[1] - pos.z)
                nx, nz = self._navNext(pos, goal[0], goal[1], BigWorld.time())
                dxs, dzs = nx - pos.x, nz - pos.z
                turret.setTarget(None)
                self._aimPoint = None
                phys.setFlags(self._steer(dt, pos, realD, math.atan2(dxs, dzs), BOT_MIN_RANGE, BOT_MAX_RANGE))
                return
            phys.setFlags(0)
            turret.setTarget(None)
            self._aimPoint = None
            return

        tpos = _posOf(target)
        dx = tpos.x - pos.x
        dz = tpos.z - pos.z
        dist = math.hypot(dx, dz)
        desiredYaw = math.atan2(dx, dz)

        # башня
        if self._aimPoint is None:
            self._rollAimPoint(tpos, dist)
        else:
            self._aimPoint = Math.Vector3(
                tpos.x + self._aimOffset[0], tpos.y + 1.3 + self._aimOffset[1], tpos.z + self._aimOffset[2])
        turret.setTarget(self._aimPoint)

        # движение
        if self._follow:
            near, far = BOT_FOLLOW_MIN, BOT_FOLLOW_MAX
        elif self.role == 'spg':
            near, far = BOT_SPG_MIN_RANGE, BOT_SPG_RANGE * 0.92     # держит дистанцию, на месте не мечется
        else:
            near, far = BOT_MIN_RANGE, BOT_MAX_RANGE
        if BOT_DEBUG_NO_MOVE:
            phys.setFlags(0)
        elif self.role == 'spg' and self._spgHome is not None:
            # Арта остаётся в тылу: возвращается к стартовой позиции, если её сдвинули/она уехала.
            hx, hz = self._spgHome
            hdx, hdz = hx - pos.x, hz - pos.z
            homeDist = math.hypot(hdx, hdz)
            if homeDist > BOT_SPG_HOME_RADIUS:
                phys.setFlags(self._steer(dt, pos, homeDist, math.atan2(hdx, hdz), 3.0, BOT_SPG_HOME_RADIUS))
            else:
                phys.setFlags(0)
        elif self._retreating(veh, pos, now=BigWorld.time()):
            # раненый бот отходит к своей базе, продолжая отстреливаться, если цель видна
            self._seesTarget(pos, tpos, target)            # обновляем видимость цели: отходя, бот отстреливается
            base = _AXIS.get('ally' if self.team == TEAM_ALLY else 'enemy')
            bx, bz = base if base else (pos.x - dx, pos.z - dz)
            nx, nz = self._navNext(pos, bx, bz, BigWorld.time(), 30.0)
            phys.setFlags(self._steer(dt, pos, max(25.0, math.hypot(bx - pos.x, bz - pos.z)),
                                      math.atan2(nx - pos.x, nz - pos.z), 20.0, 40.0))
        else:
            yawGo = desiredYaw
            if not self._follow and self.role != 'spg' and not self._seesTarget(pos, tpos, target):
                # цель за стеной/домом: не стоим и не целимся сквозь стену, а идём к ней в обход (по карте проходимости)
                nx, nz = self._navNext(pos, tpos.x, tpos.z, BigWorld.time(), 8.0)
                yawGo = math.atan2(nx - pos.x, nz - pos.z)
                phys.setFlags(self._steer(dt, pos, max(dist, near + 20.0), yawGo, near, near + 8.0))
            elif dist > far * 1.05 and not self._follow and self.role != 'spg':
                # заходим к цели каждый со своей стороны (фланг), а не толпой по прямой
                fl = getattr(self, '_flank', None)
                if fl is None:
                    fl = self._flank = random.choice((-1.0, 1.0)) * random.uniform(0.35, 1.3)
                th = math.atan2(pos.x - tpos.x, pos.z - tpos.z) + fl * min(1.0, (dist - far) / 120.0)
                R = max(near + 10.0, far * 0.75)
                ax, az = tpos.x + math.sin(th) * R, tpos.z + math.cos(th) * R
                if dist > 70.0:
                    nx, nz = self._navNext(pos, ax, az, BigWorld.time())
                else:
                    nx, nz = ax, az
                yawGo = math.atan2(nx - pos.x, nz - pos.z)
            elif dist > 70.0 and dist > far:
                nx, nz = self._navNext(pos, tpos.x, tpos.z, BigWorld.time())
                yawGo = math.atan2(nx - pos.x, nz - pos.z)
            if not (not self._follow and self.role != 'spg' and not self._losCache[1]):
                phys.setFlags(self._steer(dt, pos, dist, yawGo, near, far))

        if not self._thinkLogged:
            self._thinkLogged = True
            LOG_DEBUG('[BOTS] bot %s first think: target=%s dist=%.0f follow=%s' % (
                self.botID, self._targetID, dist, self._follow))

        # стрельба
        if not self._follow and not BOT_DEBUG_NO_FIRE:
            self._shootCd -= dt
            self._diagT = getattr(self, '_diagT', 0.0) + dt
            if self._diagT > 8.0 and getattr(self, '_diagN', 0) < 3:
                self._diagT = 0.0
                self._diagN = getattr(self, '_diagN', 0) + 1
                LOG_DEBUG('[BOTS] bot %s state: dist=%.0f errYaw=%.3f errPitch=%.3f aimed=%s cd=%.1f canShoot=%s' % (
                    self.botID, dist, turret.errYaw, turret.errPitch, turret.isAimed(), self._shootCd,
                    off_shooting._canShoot(veh)))
            if self.role == 'spg':
                # артиллерия: огонь навесом по врагу в радиусе, прямая видимость не нужна
                # Для САУ не требуем прямой линии прицеливания: она стреляет навесом.
                # Проверяем только, что цель выбрана и находится в рабочем радиусе.
                if self._shootCd <= 0.0 and dist <= BOT_SPG_RANGE:
                    fired = self._fireIndirect(veh, target, dist)
                    self._shootCd = random.uniform(1.2, 2.4) if fired else random.uniform(0.4, 0.8)
            elif self._shootCd <= 0.0 and dist <= BOT_SHOOT_DISTANCE and turret.isAimed() and self._losCache[1]:
                if self._fire(veh, dist):
                    self._rollAimPoint(tpos, dist)
                self._shootCd = random.uniform(0.2, 0.6)

    def onAttacked(self, shooterID):
        try:
            shooter = BigWorld.entity(shooterID)
            if shooter is None or not isAlive(shooter) or getTeam(shooter) == self.team:
                return
            if self.role == 'spg' and random.random() >= BOT_SPG_THREAT_CHANCE:
                return                      # арта не бросает свою цель из-за каждого попадания - иначе все САУ били бы только по игроку
            self._threatID = shooterID
            self._threatUntil = BigWorld.time() + 12.0
            self._targetID = shooterID
            self._retarget = 0.0
            self._follow = False
            LOG_DEBUG('[BOTS] bot %s under fire -> retaliate against %s' % (self.botID, shooterID))
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _ramDamageTick(self, vehicle):
        """Урон при физическом таране; кулдаун на пару машин предотвращает урон каждый тик."""
        if not self.alive or self._physics is None:
            return
        now = BigWorld.time()
        pos = _posOf(vehicle)
        mySpeed = abs(float(getattr(self._physics, '_curSpeed', 0.0)))
        if mySpeed < 2.0:
            return
        try:
            myHalf = float(getattr(self._physics.params, 'halfLength', 4.0))
        except Exception:
            myHalf = 4.0
        entities = []
        try:
            for ovid, oph in list(off_battle._simplePhysicsByVehID.items()):
                if ovid == vehicle.id:
                    continue
                opp = oph.pos
                if abs(opp.x - pos.x) > 9.0 or abs(opp.z - pos.z) > 9.0:
                    continue
                oe = BigWorld.entity(ovid)
                if oe is not None:
                    entities.append(oe)
        except Exception:
            return
        for other in entities:
            try:
                if other is vehicle or not hasattr(other, 'publicInfo') or not isAlive(other):
                    continue
                if getTeam(other) == self.team:
                    continue
                op = _posOf(other)
                dx, dz = op.x - pos.x, op.z - pos.z
                distance = math.hypot(dx, dz)
                try:
                    otherPhys = off_battle._simplePhysicsByVehID.get(other.id)
                    otherHalf = float(getattr(getattr(otherPhys, 'params', None), 'halfLength', 4.0))
                    otherSpeed = abs(float(getattr(otherPhys, '_curSpeed', 0.0))) if otherPhys is not None else 0.0
                except Exception:
                    otherHalf, otherSpeed = 4.0, 0.0
                if distance > max(3.0, min(8.0, (myHalf + otherHalf) * 0.72)):
                    continue
                key = other.id
                if now < self._ramCooldowns.get(key, 0.0):
                    continue
                self._ramCooldowns[key] = now + 1.25
                try:
                    maxHP = float(other.typeDescriptor.maxHealth)
                except Exception:
                    maxHP = 1000.0
                # Урон зависит от скорости сближения и ограничен, чтобы таран не был сильнее снаряда.
                damage = int(max(25.0, min(maxHP * 0.12, (mySpeed + otherSpeed) * 3.0)))
                prev = int(other.health)
                new = max(0, prev - damage)
                if new >= prev:
                    continue
                other.health = new
                try:
                    other.set_health(prev)
                    other.onHealthChanged(new, vehicle.id, 0)
                except Exception:
                    LOG_CURRENT_EXCEPTION()
                try:
                    av = BigWorld.player()
                    if getattr(av, 'playerVehicleID', None) == other.id:
                        av.updateVehicleHealth(new, True)
                except Exception:
                    pass
                LOG_DEBUG('[RAM] %s rammed %s for %s (speed %.1f)' % (vehicle.id, other.id, damage, mySpeed))
                if new <= 0:
                    try:
                        off_battle.freezeCorpse(other)
                    except Exception:
                        pass
                    onVehicleKilled(other, vehicle.id)
            except Exception:
                continue

    def _rollAimPoint(self, tpos, dist):
        r = BOT_INACCURACY_BASE + dist * BOT_INACCURACY_PER_METER
        self._aimOffset = (random.uniform(-r, r), random.uniform(-0.3, 0.3), random.uniform(-r, r))
        self._aimPoint = Math.Vector3(
            tpos.x + self._aimOffset[0], tpos.y + 1.3 + self._aimOffset[1], tpos.z + self._aimOffset[2])

    # --------------------------------------------------------
    # Movement
    # --------------------------------------------------------
    @staticmethod
    def _turnFlag(diff, deadzone=0.12):
        # в физике мода yaw растёт при флаге 8 (вправо) и падает при флаге 4 (влево)
        if diff > deadzone:
            return 8
        if diff < -deadzone:
            return 4
        return 0

    def _steer(self, dt, pos, dist, desiredYaw, near, far):
        phys = self._physics
        yaw = phys.yaw
        now = BigWorld.time()

        # режим "выбираемся из тупика"
        if now < self._unstuckUntil:
            flags = 2 if self._unstuckMove > 0 else 1
            flags |= 8 if self._unstuckTurn > 0 else 4
            self._moveDir = 0
            return flags

        # боковая точка объезда после застревания: временно едем к ней, а не к цели
        det = self._detour
        if det is not None:
            ddx, ddz = det[0] - pos.x, det[1] - pos.z
            dd = math.hypot(ddx, ddz)
            if now >= det[2] or dd < 4.0:
                self._detour = None
            else:
                desiredYaw = math.atan2(ddx, ddz)
                dist = max(dist, far + 1.0)

        # выбор режима
        if dist > far:
            mode = 1
        elif dist < near:
            mode = -1
        else:
            if self.role == 'spg':
                mode = 0
            else:
                if now > self._holdUntil:
                    self._holdMode = random.choice((0, 0, 0, 1, -1))
                    self._holdUntil = now + random.uniform(1.5, 4.0)
                mode = self._holdMode

        flags = 0
        if mode == 1:
            steerYaw = self._avoid(pos, desiredYaw)
            diff = _wrap(steerYaw - yaw)
            if abs(diff) < 1.1:
                flags |= 1
            flags |= self._turnFlag(diff)
        elif mode == -1:
            diff = _wrap(desiredYaw - yaw)            # корпус смотрит на цель, едем задним ходом
            flags |= self._turnFlag(diff)
            if self._clearDist(pos, yaw + math.pi, 8.0) > 6.0:
                flags |= 2
        else:
            diff = _wrap(desiredYaw - yaw)
            if abs(diff) > 0.35:
                flags |= self._turnFlag(diff)

        # детектор застревания
        moving = 1 if (flags & 1) else (-1 if (flags & 2) else 0)
        self._moveDir = moving
        if moving != 0 and now - self._lastCheck >= 0.5:
            if self._lastPos is not None:
                moved = math.hypot(pos.x - self._lastPos[0], pos.z - self._lastPos[1])
                if moved < BOT_STUCK_MIN_MOVE:
                    self._stuckTime += now - self._lastCheck
                else:
                    self._stuckTime = 0.0
            self._lastPos = (pos.x, pos.z)
            self._lastCheck = now
            if self._stuckTime >= BOT_STUCK_TIME:
                self._stuckTime = 0.0
                self._startUnstuck(pos, yaw, moving, now)
        elif moving == 0:
            self._stuckTime = 0.0
            self._lastPos = (pos.x, pos.z)
            self._lastCheck = now

        return flags

    def _startUnstuck(self, pos, yaw, moving, now):
        """Застряли: сдаём в сторону, где свободнее, разворачиваемся к свободному боку и затем едем в обход по боковой точке."""
        if now - self._lastStuckAt < 12.0:
            self._stuckCount = min(self._stuckCount + 1, 5)
        else:
            self._stuckCount = 1
        self._lastStuckAt = now
        try:
            if moving > 0:
                off_nav.NAV.markStuck(pos.x + math.sin(yaw) * 6.0, pos.z + math.cos(yaw) * 6.0, now)
            self._navForce = True
        except Exception:
            LOG_CURRENT_EXCEPTION()
        circles = self._gatherCircles(pos, 30.0)
        left = self._clearDist(pos, yaw - 1.2, 12.0, circles)
        right = self._clearDist(pos, yaw + 1.2, 12.0, circles)
        front = self._clearDist(pos, yaw, 6.0, circles)
        back = self._clearDist(pos, yaw + math.pi, 6.0, circles)
        # какая сторона свободнее; при повторных застреваниях меняем сторону, чтобы не биться в одно и то же место
        sideSign = 1 if right >= left else -1
        if self._stuckCount % 2 == 0:
            sideSign = -sideSign
        if moving > 0:
            move = -1 if back > 1.5 else 1
        else:
            move = 1 if front > 1.5 else -1
        self._unstuckMove = move
        # в физике мода рыскание не зависит от направления движения: флаг 8 - нос вправо, 4 - влево
        self._unstuckTurn = sideSign
        self._unstuckUntil = now + random.uniform(1.2, 1.8) + 0.4 * self._stuckCount
        # точка объезда сбоку от текущего курса, дальше при повторах
        dyaw = yaw + sideSign * 1.2
        d = BOT_DETOUR_DIST * (1.0 + 0.4 * (self._stuckCount - 1))
        self._detour = (pos.x + math.sin(dyaw) * d, pos.z + math.cos(dyaw) * d, now + BOT_DETOUR_TIME + self._unstuckUntil - now)
        self._avoidAng = 0.0
        self._avoidUntil = 0.0
        try:
            self._escalateStuck(pos, yaw, now)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        LOG_DEBUG('[BOTS] map=%s pos=(%.0f, %.0f) bot %s stuck x%d -> unstuck (move=%d side=%d clear L=%.1f R=%.1f F=%.1f B=%.1f)' % (
            _mapName(), pos.x, pos.z, self.botID, self._stuckCount, move, sideSign, left, right, front, back))

    def _escalateStuck(self, pos, yaw, now):
        """Повторные застревания: проходим сквозь других ботов и остовы, бросаем недостижимую точку маршрута,
        ищем другой путь, а если и это не помогло (дом, куча) - переставляем бота на свободное место."""
        phys = self._physics
        n = self._stuckCount
        if phys is None:
            return
        if n >= 2:
            phys.ghostUntil = now + 5.0 + n            # временно не упираемся в других ботов и остовы (стены остаются)
        if n >= 3 and self._route and self._routeIdx < len(self._route):
            self._routeIdx += 1                         # точка маршрута, видимо, внутри дома/в тупике - к следующей
        if n >= 4:
            ang = random.choice((-1, 1)) * random.uniform(1.6, 2.6)
            d = BOT_DETOUR_DIST * (2.0 + 0.5 * n)
            dyaw = yaw + ang
            self._detour = (pos.x + math.sin(dyaw) * d, pos.z + math.cos(dyaw) * d, now + BOT_DETOUR_TIME + 3.0)
        if n >= 2 and now - getattr(self, '_hopAt', 0.0) > 6.0:
            if self._hop(pos, yaw):
                self._hopAt = now
                return
        if n >= 4:
            self._relocate(pos, yaw)

    def _hop(self, pos, yaw):
        """Короткий «выход» из застревания: бот переставляется на свободное место в 4-14 м (лучше вперёд по ходу). Нужен для столбов,
        бордюров, урн и прочей мелочи, в которую корпус упёрся так, что лучи её не видят. Игрок поблизости - не трогаем."""
        veh = self.vehicle()
        phys = self._physics
        if veh is None or phys is None:
            return False
        now = BigWorld.time()
        pp = off_battle._smoothPlayerPos(now)
        if pp is not None and math.hypot(pos.x - pp.x, pos.z - pp.z) < 35.0:
            return False
        try:
            others = [(op.pos.x, op.pos.z) for op in off_battle._simplePhysicsByVehID.values()
                      if op is not phys and getattr(op, 'pos', None) is not None]
        except Exception:
            others = []
        spaceID = veh.spaceID
        for dist in (5.0, 8.0, 12.0, 16.0):
            for da in (0.0, 0.6, -0.6, 1.2, -1.2, 1.9, -1.9, 3.14):
                a = yaw + da
                x, z = pos.x + math.sin(a) * dist, pos.z + math.cos(a) * dist
                if any(abs(ox - x) < 7.0 and abs(oz - z) < 7.0 for ox, oz in others):
                    continue
                y = _spawnClear(spaceID, x, z, yaw, 0.58)
                if y is None:
                    continue
                phys.pos = Math.Vector3(x, y + 0.3, z)
                phys._curSpeed = 0.0
                self._detour = None
                self._unstuckUntil = 0.0
                LOG_DEBUG('[BOTS] bot %s hopped %.0f m out of a snag' % (self.botID, dist))
                return True
        return False

    def _relocate(self, pos, yaw):
        """Переставить застрявшего бота на ближайшее свободное место (18-50 м), если игрок его не видит рядом."""
        veh = self.vehicle()
        phys = self._physics
        if veh is None or phys is None:
            return False
        now = BigWorld.time()
        pp = off_battle._smoothPlayerPos(now)
        if pp is not None and math.hypot(pos.x - pp.x, pos.z - pp.z) < 45.0:
            return False                                # игрок рядом - не телепортируем на глазах
        try:
            others = [(op.pos.x, op.pos.z) for op in off_battle._simplePhysicsByVehID.values()
                      if op is not phys and getattr(op, 'pos', None) is not None]
        except Exception:
            others = []
        spaceID = veh.spaceID
        tries = 0
        for dist in (18.0, 28.0, 40.0, 50.0):
            for k in range(8):
                tries += 1
                a = yaw + math.pi + k * (math.pi / 4.0) + random.uniform(-0.2, 0.2)
                x, z = pos.x + math.sin(a) * dist, pos.z + math.cos(a) * dist
                if any(abs(ox - x) < 9.0 and abs(oz - z) < 9.0 for ox, oz in others):
                    continue
                y = _spawnClear(spaceID, x, z, yaw)
                if y is None:
                    continue
                phys.pos = Math.Vector3(x, y + 0.3, z)
                phys._curSpeed = 0.0
                self._stuckCount = 1
                self._detour = None
                self._unstuckUntil = 0.0
                LOG_DEBUG('[BOTS] bot %s relocated %.0f m from a dead end' % (self.botID, dist))
                return True
        LOG_DEBUG('[BOTS] bot %s: no free spot to relocate (%d tries)' % (self.botID, tries))
        return False

    def _gatherCircles(self, pos, rng):
        """Окружности корпусов всех ДРУГИХ машин (живых и остовов) в радиусе rng - препятствия для лучей объезда."""
        res = []
        if not BOT_AVOID_VEHICLES:
            return res
        try:
            items = list(off_battle._simplePhysicsByVehID.items())
        except Exception:
            return res
        for vid, op in items:
            if vid == self._vid or op is self._physics:
                continue
            try:
                p = op.pos
                if p is None or abs(p.x - pos.x) > rng or abs(p.z - pos.z) > rng:
                    continue
                res.extend(op._circles(p, op.yaw))
            except Exception:
                continue
        return res

    def _avoid(self, pos, desiredYaw):
        """Ищет направление, ближайшее к желаемому, в котором впереди свободно (статика + чужие машины)."""
        phys = self._physics
        now = BigWorld.time()
        c = self._avoidCache
        if c is not None and now - c[0] < (BOT_AVOID_CACHE_FAR if self._far else BOT_AVOID_CACHE) and abs(_wrap(desiredYaw - c[1])) < 0.12 \
                and abs(pos.x - c[3]) < 1.5 and abs(pos.z - c[4]) < 1.5:
            return desiredYaw + c[2]                 # курс и место почти те же - прошлый ответ ещё верен
        ans = self._avoidCompute(pos, desiredYaw, now)
        self._avoidCache = (now, desiredYaw, _wrap(ans - desiredYaw), pos.x, pos.z)
        return ans

    @_perf.timed('ai.avoid')
    def _avoidCompute(self, pos, desiredYaw, now):
        phys = self._physics
        spd = abs(getattr(phys, '_curSpeed', 0.0))
        length = max(BOT_PROBE_LENGTH_MIN, phys.params.halfLength * 2.5 + spd * 1.5)
        circles = self._gatherCircles(pos, length + 12.0)

        d0 = self._clearDist(pos, desiredYaw, length, circles)
        if d0 >= length * 0.98:
            self._avoidAng = 0.0
            return desiredYaw

        best = None
        keep = now < self._avoidUntil and self._avoidAng != 0.0
        # Сравниваем все направления: выбираем самый свободный коридор, штрафуем резкий уход от маршрута
        # и держим уже выбранную сторону (гистерезис), чтобы бот не метался у углов и зданий.
        fullAt = None
        for ang in BOT_AVOID_ANGLES:
            if fullAt is not None and abs(ang) > fullAt + 0.01:
                break                       # свободный коридор уже найден на меньшем угле: дальние углы не выигрывают (штраф за угол)
            d = d0 if ang == 0.0 else self._clearDist(pos, desiredYaw + ang, length, circles)
            score = d - abs(ang) * 3.0
            if keep and ang * self._avoidAng > 0.0:
                score += BOT_AVOID_HYSTERESIS
            if best is None or score > best[0]:
                best = (score, ang, d)
            if d >= length * 0.98 and fullAt is None:
                fullAt = abs(ang)
        ang = best[1] if best is not None else 0.0
        if ang != 0.0 and (not keep or ang * self._avoidAng <= 0.0):
            self._avoidAng = ang
            self._avoidUntil = now + BOT_AVOID_KEEP
        elif ang != 0.0:
            self._avoidUntil = now + BOT_AVOID_KEEP
        if ang != 0.0 and self._avoidLogN < 3:
            self._avoidLogN += 1
            LOG_DEBUG('[BOTS] bot %s avoid: ahead clear %.1f/%.1f -> turn %.2f rad (clear %.1f)' % (
                self.botID, d0, length, ang, best[2]))
        return desiredYaw + ang

    def _clearDist(self, pos, yaw, length, circles=None):
        """Свободная дистанция вдоль курса yaw: до статики (три луча по ширине корпуса) и до корпусов чужих машин."""
        try:
            spaceID = self._vehicle.spaceID
        except Exception:
            return length

        phys = self._physics
        p = phys.params
        dirX = math.sin(yaw)
        dirZ = math.cos(yaw)
        rightX = dirZ
        rightZ = -dirX

        lift = max(getattr(off_battle, 'STP_COLLIDE_BASE_LIFT_MIN', 0.25),
                   getattr(p, 'clearance', 0.5) * 0.65)
        baseY = phys.pos.y + lift
        heights = getattr(off_battle, 'STP_COLLIDE_HEIGHTS', (0.05, 0.35, 0.75))
        probeHeights = (heights[len(heights) // 2], heights[-1])
        ignoreN = getattr(off_battle, 'STP_COLLIDE_GROUND_NORMAL_IGNORE', 0.45)
        half = p.halfLength
        total = length + half

        best = length

        # чужие машины: окружность корпуса против луча шириной с наш корпус
        if circles is None:
            circles = self._gatherCircles(pos, length + 12.0)
        hw = p.halfWidth * 0.95
        for (cx, cz, cr) in circles:
            rx, rz = cx - pos.x, cz - pos.z
            t = rx * dirX + rz * dirZ
            if t <= 0.0 or t > total + cr:
                continue
            lat = abs(rx * rightX + rz * rightZ)
            R = cr + hw
            if lat >= R:
                continue
            tt = t - math.sqrt(max(0.0, R * R - lat * lat)) - half
            if tt < best:
                best = max(0.0, tt)

        # слишком крутой подъём впереди (гора, склон) - для бота это такая же стена
        try:
            maxSlope = getattr(off_battle, 'STP_MAX_SLOPE', 0.62) * 0.9
            seg = max(3.0, half * 1.6)
            n = int(min(length, 12.0) / seg)
            prevY = phys._groundHeight(spaceID, pos.x, phys.pos.y, pos.z, phys.pos.y)
            for i in xrange(1, n + 1):
                d = half + seg * i
                gy = phys._groundHeight(spaceID, pos.x + dirX * d, prevY, pos.z + dirZ * d, prevY)
                if (gy - prevY) / seg > maxSlope:
                    best = min(best, max(0.0, d - seg - half))
                    break
                prevY = gy
        except Exception:
            pass

        for lat in (0.0, p.halfWidth * 0.9, -p.halfWidth * 0.9):
            bx = pos.x + rightX * lat
            bz = pos.z + rightZ * lat
            for h in probeHeights:
                start = Math.Vector3(bx, baseY + h, bz)
                end = Math.Vector3(bx + dirX * total, baseY + h, bz + dirZ * total)
                try:
                    res = BigWorld.wg_collideSegment(spaceID, start, end, 0)
                except Exception:
                    res = None
                if res is None or res[0] is None:
                    continue
                try:
                    if len(res) > 1 and abs(float(res[1][1])) > ignoreN:
                        continue          # пологий склон/земля - не препятствие
                except Exception:
                    pass
                hp = res[0]
                t = (hp.x - start.x) * dirX + (hp.z - start.z) * dirZ - half
                if t < best:
                    best = max(0.0, t)
        return best

    # --------------------------------------------------------
    # Shooting
    # --------------------------------------------------------
    def _spgMissFx(self, vehicle, shellDescr, x, z, dist, shot):
        """Снаряд САУ упал мимо: взрыв на земле в точке (x, z) через время полёта."""
        sid = BigWorld.player().spaceID
        r = BigWorld.wg_collideSegment(sid, Math.Vector3(x, 600.0, z), Math.Vector3(x, -100.0, z), 0)
        if r is None or r[0] is None:
            return
        end = Math.Vector3(float(r[0].x), float(r[0].y), float(r[0].z))
        flight = max(0.6, min(8.0, float(dist) / max(1.0, float(shot['speed'])) * 1.8))
        shotID = off_shooting._nextShotID()
        ei = shellDescr['effectsIndex']

        def boom():
            try:
                BigWorld.player().explodeProjectile(shotID, ei, 3, end, Math.Vector3(0.0, -1.0, 0.0), None)
            except Exception:
                pass
        BigWorld.callback(flight, boom)

    def _spgHarassTick(self, veh, pos, dt):
        """Беспокоящий огонь: САУ без цели стреляет по карте возле врагов (промахи, без урона)."""
        now = BigWorld.time()
        if now < getattr(self, '_harassAt', 0.0):
            return
        self._harassAt = now + random.uniform(*BOT_SPG_HARASS)
        if not off_shooting._canShoot(veh):
            return
        foes = self._enemies(False)
        if not foes:
            return
        foe = random.choice(foes)
        fp = _posOf(foe)
        dist = math.hypot(fp.x - pos.x, fp.z - pos.z)
        if dist > BOT_SPG_RANGE or dist < 60.0:
            return
        gunDescr = veh.typeDescriptor.gun
        shot = gunDescr['shots'][veh.typeDescriptor.activeGunShotIndex]
        st = off_shooting._getState(veh)
        st['reloadEnd'] = now + gunDescr['reloadTime'] * BOT_SPG_RELOAD_MULT * off_shooting._reloadFactor(veh.id)
        a = random.uniform(0.0, 6.283)
        r = random.uniform(15.0, 55.0)
        self._spgMissFx(veh, shot['shell'], fp.x + math.cos(a) * r, fp.z + math.sin(a) * r, dist, shot)
        try:
            veh.showShooting(gunDescr['burst'][0], True)
        except Exception:
            pass

    def _spgCovered(self, vehicle, target):
        """Цель укрыта от навесного снаряда: над ней крыша/крона/мост либо дом на траектории падения (последние ~40 м к стрелку)."""
        if not BOT_SPG_COVER:
            return False
        try:
            sid = BigWorld.player().spaceID
            tp = _posOf(target)
            top = Math.Vector3(tp.x, tp.y + 1.5, tp.z)
            r = BigWorld.wg_collideSegment(sid, Math.Vector3(tp.x, tp.y + 90.0, tp.z), top, 0)
            if r is not None and r[0] is not None:
                return True
            sp = _posOf(vehicle)
            dx, dz = sp.x - tp.x, sp.z - tp.z
            d = math.hypot(dx, dz) or 1.0
            # снаряд падает под углом ~55 градусов со стороны стрелка: проверяем луч от цели назад-вверх
            ex, ez = tp.x + dx / d * 30.0, tp.z + dz / d * 30.0
            r = BigWorld.wg_collideSegment(sid, Math.Vector3(tp.x, tp.y + 2.0, tp.z), Math.Vector3(ex, tp.y + 2.0 + 42.0, ez), 0)
            if r is not None and r[0] is not None:
                return True
        except Exception:
            LOG_CURRENT_EXCEPTION()
        return False

    def _fireIndirect(self, vehicle, target, dist):
        """Выстрел САУ-бота навесом: цель попадает с вероятностью, падающей с дистанцией (от BOT_SPG_HIT_NEAR до
        BOT_SPG_HIT_FAR); броня считается как сверху (тонкая, ~30 мм). Возвращает True, если выстрел произошёл."""
        if vehicle is None or target is None or not off_shooting._canShoot(vehicle):
            return False
        if getTeam(target) == self.team or not isAlive(target):
            return False

        gunDescr = vehicle.typeDescriptor.gun
        shot = gunDescr['shots'][vehicle.typeDescriptor.activeGunShotIndex]
        shellDescr = shot['shell']
        maxDistance = shot['maxDistance']

        st = off_shooting._getState(vehicle)
        shellCD = off_shooting._currentShellCD(vehicle)
        st['ammo'][shellCD] = max(0, st['ammo'].get(shellCD, 0) - 1)
        if st['ammo'][shellCD] <= 0:
            st['ammo'][shellCD] = 10          # у ботов снаряды не кончаются
        st['reloadEnd'] = BigWorld.time() + gunDescr['reloadTime'] * random.uniform(*BOT_RELOAD_FACTOR) * BOT_SPG_RELOAD_MULT * off_shooting._reloadFactor(vehicle.id)

        t = min(1.0, dist / max(1.0, BOT_SPG_RANGE))
        chance = BOT_SPG_HIT_NEAR + (BOT_SPG_HIT_FAR - BOT_SPG_HIT_NEAR) * t
        covered = self._spgCovered(vehicle, target)
        if covered or random.random() >= chance:
            try:
                tp = _posOf(target)
                a = random.uniform(0.0, 6.283)
                r = random.uniform(5.0, 24.0)
                self._spgMissFx(vehicle, shellDescr, tp.x + math.cos(a) * r, tp.z + math.sin(a) * r, dist, shot)
            except Exception:
                LOG_CURRENT_EXCEPTION()
        if random.random() < chance and not covered:
            # Артиллерийский снаряд бота тоже летит некоторое время до нанесения урона.
            flightTime = max(0.35, min(8.0, float(dist) / max(1.0, float(shot['speed'])) * 1.8))
            shooterID = vehicle.id
            effectsIndex = shellDescr['effectsIndex']

            def _botSPGImpact(tgt=target, sid=shooterID, sh=shellDescr, d=dist,
                              md=maxDistance, ei=effectsIndex):
                try:
                    if tgt is None or not isAlive(tgt):
                        return
                    sh = dict(sh)
                    try:
                        dm = list(sh['damage'])
                        cap = float(tgt.typeDescriptor.maxHealth) * BOT_SPG_DAMAGE_CAP
                        dm[0] = max(1.0, min(float(dm[0]) * BOT_SPG_DAMAGE_MULT, cap))
                        sh['damage'] = tuple(dm)
                    except Exception:
                        pass
                    result = off_shooting._applyDamage(sid, tgt, sh, 0.85, 30.0, d, md)
                    off_shooting._feedback(sid, tgt, result)
                    try:
                        if getattr(tgt, 'health', 0) > 0:          # по только что убитой машине эффект попадания не показываем
                            tgt.showDamageFromShot(sid, [], ei)
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                except Exception:
                    LOG_CURRENT_EXCEPTION()

            BigWorld.callback(flightTime, _botSPGImpact)

        try:
            vehicle.showShooting(gunDescr['burst'][0], True)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        return True

    def _spgDanger(self, veh, pos, dt):
        """Арта, к которой подобрался враг: далеко - отъезжает к своей базе, вплотную - разворачивается и стреляет
        прямой наводкой. Возвращает True, если управление взято (обычная логика САУ пропускается)."""
        phys = self._physics
        now = BigWorld.time()
        near, nd = None, 1e9
        for e in self._enemies():
            ep = _posOf(e)
            d = math.hypot(ep.x - pos.x, ep.z - pos.z)
            if d < nd:
                near, nd = e, d
        if near is not None and nd <= BOT_SPG_DANGER:
            self._dangerUntil = now + 3.0
            self._dangerID = near.id
        elif now >= getattr(self, '_dangerUntil', 0.0):
            if getattr(self, '_dangerOn', False):
                self._dangerOn = False
                self._spgHome = (pos.x, pos.z)         # новая позиция в тылу: обратно на старую (к врагу) не едет
            return False
        else:
            near = BigWorld.entity(getattr(self, '_dangerID', 0))
            if near is None or not isAlive(near):
                self._dangerUntil = 0.0
                return False
            ep = _posOf(near)
            nd = math.hypot(ep.x - pos.x, ep.z - pos.z)
        if not getattr(self, '_dangerOn', False):
            self._dangerOn = True
            LOG_DEBUG('[BOTS] SPG %s: enemy %s at %.0f m, evading' % (self.botID, near.id, nd))
        tp = _posOf(near)
        dx, dz = tp.x - pos.x, tp.z - pos.z
        toYaw = math.atan2(dx, dz)
        turret = self._turret
        self._shootCd -= dt
        cornered = nd <= BOT_SPG_FIGHT or now < self._unstuckUntil + 0.0 and self._stuckCount >= 3
        if cornered:
            # вплотную: останавливаемся, поворачиваем корпус к врагу и бьём прямой наводкой
            diff = _wrap(toYaw - phys.yaw)
            phys.setFlags(self._turnFlag(diff) if abs(diff) > 0.12 else 0)
            self._aimPoint = Math.Vector3(tp.x, tp.y + 1.3, tp.z)
            turret.setTarget(self._aimPoint)
            self._seesTarget(pos, tp, near)
            if self._shootCd <= 0.0 and turret.isAimed() and self._losCache[1] and abs(diff) < 0.2:
                if self._fire(veh, nd):
                    self._shootCd = random.uniform(1.5, 3.0)
                else:
                    self._shootCd = 0.5
            return True
        # дальше: отъезжаем от врага в сторону своей базы
        ax, az = -dx, -dz
        al = max(1.0, math.hypot(ax, az))
        ax, az = ax / al, az / al
        base = _AXIS.get('ally' if self.team == TEAM_ALLY else 'enemy')
        if base:
            bx, bz = base[0] - pos.x, base[1] - pos.z
            bl = max(1.0, math.hypot(bx, bz))
            ax, az = ax + 0.8 * bx / bl, az + 0.8 * bz / bl
            al = max(0.01, math.hypot(ax, az))
            ax, az = ax / al, az / al
        fx, fz = pos.x + ax * 70.0, pos.z + az * 70.0
        nx, nz = self._navNext(pos, fx, fz, now, 30.0)
        turret.setTarget(None)
        self._aimPoint = None
        phys.setFlags(self._steer(dt, pos, 100.0, math.atan2(nx - pos.x, nz - pos.z), 20.0, 40.0))
        return True

    def _fire(self, vehicle, dist):
        """Выстрел бота. Возвращает True, если выстрел произошёл."""
        if vehicle is None or not off_shooting._canShoot(vehicle):
            return False

        origin, direction = off_shooting._shotOrigin(vehicle)
        gunDescr = vehicle.typeDescriptor.gun
        shot = gunDescr['shots'][vehicle.typeDescriptor.activeGunShotIndex]
        shellDescr = shot['shell']
        maxDistance = shot['maxDistance']
        end = origin + direction * maxDistance

        hit = off_shooting._findHitVehicle(vehicle, origin, end)
        staticHit = off_shooting._findStaticHit(vehicle, origin, end)

        hitTarget = None
        hitDist = maxDistance
        endPoint = end
        kind = 'none'
        normal = None

        if hit is not None and (staticHit is None or hit[1].dist <= staticHit[0]):
            hitTarget = hit
            hitDist = hit[1].dist
            endPoint = origin + direction * hitDist
            kind = 'vehicle'
        elif staticHit is not None:
            hitDist, endPoint, normal = staticHit
            kind = 'static'

        # стреляем только по вражеским машинам, в стены/союзников не тратим снаряды
        if kind != 'vehicle':
            if not self._fireDiag:
                self._fireDiag = True
                LOG_DEBUG('[BOTS] bot %s aimed but ray hits %s (hit=%s static=%s)' % (
                    self.botID, kind, hit is not None, staticHit is not None))
            return False
        if getTeam(hitTarget[0]) == self.team or not isAlive(hitTarget[0]):
            return False

        avatar = BigWorld.player()

        # боезапас и перезарядка
        st = off_shooting._getState(vehicle)
        shellCD = off_shooting._currentShellCD(vehicle)
        st['ammo'][shellCD] = max(0, st['ammo'].get(shellCD, 0) - 1)
        if st['ammo'][shellCD] <= 0:
            st['ammo'][shellCD] = 10          # у ботов снаряды не кончаются
        st['reloadEnd'] = BigWorld.time() + gunDescr['reloadTime'] * random.uniform(*BOT_RELOAD_FACTOR) * off_shooting._reloadFactor(vehicle.id)

        target, res = hitTarget
        # Bot shells also travel before damage is applied; use the actual gun's
        # shell speed so slow guns do not behave like instant-hit weapons.
        flight = max(0.08, min(8.0, float(hitDist) / max(1.0, float(shot['speed']))))
        shooterID = vehicle.id
        hitCos, hitArmor, hitDistance = res.hitAngleCos, res.armor, res.dist
        effectsIndex = shellDescr['effectsIndex']

        def _botTankImpact(tgt=target, sid=shooterID, sh=shellDescr,
                           hc=hitCos, ar=hitArmor, hd=hitDistance,
                           md=maxDistance, ei=effectsIndex):
            try:
                if tgt is None or not isAlive(tgt):
                    return
                result = off_shooting._applyDamage(sid, tgt, sh, hc, ar, hd, md)
                off_shooting._feedback(sid, tgt, result)
                try:
                    if getattr(tgt, 'health', 0) > 0:              # по только что убитой машине эффект попадания не показываем
                        tgt.showDamageFromShot(sid, [], ei)
                except Exception:
                    LOG_CURRENT_EXCEPTION()
            except Exception:
                LOG_CURRENT_EXCEPTION()

        BigWorld.callback(flight, _botTankImpact)

        # дульная вспышка / отдача
        try:
            vehicle.showShooting(gunDescr['burst'][0], True)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        # трассер (если клиент его не принял - один раз пишем в лог и больше не пытаемся)
        if not _TRACER_BROKEN[0]:
            try:
                shotID = off_shooting._nextShotID()
                velocity = direction * shot['speed']
                avatar.showTracer(vehicle.id, shotID, shellDescr['effectsIndex'],
                                  origin, velocity, shot['gravity'], maxDistance)
                flight = max(0.01, hitDist / max(1.0, shot['speed']))
                BigWorld.callback(flight, lambda sid=shotID, ep=endPoint, ei=shellDescr['effectsIndex'], d=direction: _stopTracer(sid, ep, ei, d))
            except Exception:
                _TRACER_BROKEN[0] = True
                LOG_ERROR('[BOTS] showTracer failed, bot tracers disabled')
                LOG_CURRENT_EXCEPTION()
        return True


def _clearStipple(vid):
    try:
        e = BigWorld.entity(vid)
        if e is not None and getattr(e, 'model', None) is not None and e.model.stipple:
            e.model.stipple = False
            LOG_DEBUG('[BOTS] stipple cleared for vehicle %s' % vid)
    except Exception:
        pass


def _stopTracer(shotID, endPoint, effectsIndex=None, direction=None):
    try:
        av = BigWorld.player()
        if av is not None:
            if effectsIndex is not None:
                try:
                    av.explodeProjectile(shotID, effectsIndex, 3, endPoint, direction, None)
                    return
                except Exception:
                    pass
            av.stopTracer(shotID, endPoint)
    except Exception:
        pass


# ============================================================
# Spawn
# ============================================================
def _completeBotSpawn(avatar, vehicle, rec):
    botID = rec['botID']
    try:
        for k, v in rec['vprops'].items():
            try:
                setattr(vehicle, k, v)
            except Exception:
                pass

        _registerArenaVehicle(vehicle, rec)     # на случай, если до старта визуала не успели (идемпотентно)

        ai = BotAI(vehicle, botID, rec['team'], _vehicleRole(rec['compDescr']))
        if ai.role in ('light', 'medium', 'heavy') and _CAPTURERS.get(rec['team'], 0) < BOT_CAPTURERS_PER_TEAM \
                and random.random() < BOT_CAPTURER_CHANCE:
            ai.capturer = True                       # этот бот едет захватывать базу противника
            _CAPTURERS[rec['team']] = _CAPTURERS.get(rec['team'], 0) + 1
            LOG_DEBUG('[BOTS] bot %s (team %s) is a base capturer' % (botID, rec['team']))
        _BOTS[botID] = ai
        ai.start()
        off_spotting.onBotReady(vehicle)
        LOG_DEBUG('[BOTS] bot %s created: team=%s entityID=%s' % (botID, rec['team'], vehicle.id))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _findEntityFor(rec, ownID, known):
    import Vehicle as _V
    vid = rec.get('vid')
    if vid is not None:
        e = BigWorld.entity(vid)
        if e is not None:
            return e
    for e in list(BigWorld.entities.values()):
        if not isinstance(e, _V.Vehicle) or e.id == ownID or e.id in known:
            continue
        try:
            if int(e.publicInfo['accountDBID']) == -rec['botID']:
                rec['vid'] = e.id
                return e
        except Exception:
            pass
        try:
            p = e.position
            if math.hypot(p.x - rec['x'], p.z - rec['z']) < 6.0:
                rec['vid'] = e.id
                return e
        except Exception:
            pass
    return None


def _dumpVehicles(ownID):
    try:
        import Vehicle as _V
        for e in list(BigWorld.entities.values()):
            if isinstance(e, _V.Vehicle):
                try:
                    dbid = e.publicInfo['accountDBID']
                except Exception:
                    dbid = '?'
                LOG_DEBUG('[BOTS]   Vehicle entity id=%s own=%s inWorld=%s pos=%s dbid=%s started=%s' % (
                    e.id, e.id == ownID, getattr(e, 'inWorld', '?'), e.position, dbid,
                    getattr(e, 'isStarted', '?')))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _waitForBotEntity(token, triesLeft):
    global _PENDING_WAIT_ACTIVE

    if token != _SPAWN_TOKEN or not _active():
        _PENDING_WAIT_ACTIVE = False
        del _PENDING_BOTS[:]
        return

    try:
        avatar = BigWorld.player()
        ownID = getattr(avatar, 'playerVehicleID', 0)
        known = set(ai._vid for ai in _BOTS.values())

        for rec in list(_PENDING_BOTS):
            e = _findEntityFor(rec, ownID, known)
            if e is None or not getattr(e, 'inWorld', True):
                continue
            _PENDING_BOTS.remove(rec)
            known.add(e.id)
            _completeBotSpawn(avatar, e, rec)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    if _PENDING_BOTS:
        if triesLeft > 0:
            BigWorld.callback(BOT_ENTITY_WAIT_STEP, lambda: _waitForBotEntity(token, triesLeft - 1))
        else:
            _PENDING_WAIT_ACTIVE = False
            LOG_ERROR('[BOTS] wait timeout, %s bot(s) still pending: %s' % (
                len(_PENDING_BOTS), [(r['botID'], r['team'], r.get('vid')) for r in _PENDING_BOTS]))
            _dumpVehicles(getattr(BigWorld.player(), 'playerVehicleID', 0))
            del _PENDING_BOTS[:]
    else:
        _PENDING_WAIT_ACTIVE = False


def _spawnOne(token, team, idx):
    global _BOT_ID_COUNTER, _PENDING_WAIT_ACTIVE

    if token != _SPAWN_TOKEN or not _active():
        return

    avatar = BigWorld.player()
    points = _PLAN.get(team) or []
    if avatar is None or idx >= len(points):
        return

    compDescr = None
    tutPlace = None
    if TUTORIAL[0] and team == TEAM_ENEMY:
        try:
            from gui.mods.offhangar import tutor as _tut
            compDescr = TUT_VEHICLES.get(idx + 1) or _tut.botVehicle(idx + 1)     # учебные машины клиента (0.9.5) или подбор по классу/уровню
            if not TUT_POINTS.get(idx + 1):
                tutPlace = _tut.botPlace(idx + 1)                                  # нет точек карты - расчётное место (x, z)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    if compDescr is None and idx in _SPG_SLOT_BY_TEAM.get(team, set()):
        compDescr = _randomSpgCompDescr()
        if compDescr is not None:
            LOG_DEBUG('[BOTS] selected SPG for team=%s slot=%s' % (team, idx))
    if compDescr is None:
        compDescr = _rosterCompDescr(team, idx) or _randomCompDescr()
    if compDescr is None:
        LOG_ERROR('[BOTS] spawn skipped: no vehicle for bot')
        return

    _BOT_ID_COUNTER += 1
    botID = _BOT_ID_COUNTER

    x, z = points[idx]
    if tutPlace is not None:
        x, z = tutPlace[0], tutPlace[1]       # botPlace() уже отдаёт (x, z)

    # Все машины одной команды ориентированы одинаково, вдоль оси боя,
    # а не каждая на свою отдельную точку игрока.
    yaw = 0.0
    try:
        dx, dz = _AXIS.get('dir', (0.0, 1.0))
        if team == TEAM_ENEMY:
            dx, dz = -dx, -dz
        yaw = math.atan2(dx, dz)
    except Exception:
        pass
    tp = TUT_POINTS.get(idx + 1) if (TUTORIAL[0] and team == TEAM_ENEMY) else None
    if tp is not None:
        x, z, yaw = tp[0], tp[2], tp[3]               # учебный бой: ровно в точке карты и с её поворотом
    else:
        if TUTORIAL[0] and team == TEAM_ENEMY:
            try:
                pv = _getPlayerVehicle()
                yaw = math.atan2(pv.position.x - x, pv.position.z - z)        # расчётное место: мишень смотрит на игрока
            except Exception:
                pass
        x, z = _safeSpawnPoint(avatar.spaceID, x, z, yaw)       # не в текстурах: стены, крыши, крутые склоны, чужие машины
    y = _groundY(avatar.spaceID, x, z)

    vprops = {
        'publicInfo': _makePublicInfo(compDescr, team, botID),
        'health': off_battle._getMaxHealth(compDescr),
        'isCrewActive': True,
        'engineMode': (0, 0),
        'publicStateModifiers': [],
        'damageStickers': [],
        'gunAnglesPacked': 0,
        'isStrafing': False,
    }
    rec = {'botID': botID, 'team': team, 'compDescr': compDescr, 'vprops': vprops,
           'x': x, 'z': z, 'vid': None}

    # запись нужна ДО createEntity: визуал может стартовать прямо внутри этого вызова
    _PENDING_BOTS.append(rec)

    LOG_DEBUG('[BOTS] spawning bot %s team=%s at (%.1f, %.1f, %.1f)' % (botID, team, x, y, z))

    try:
        result = BigWorld.createEntity('Vehicle', avatar.spaceID, 0, (x, y, z), (0.0, 0.0, yaw), vprops)
        LOG_DEBUG('[BOTS] createEntity -> %r' % (result,))
        if result is not None and rec.get('vid') is None:
            try:
                rec['vid'] = result if isinstance(result, (int, long)) else result.id
            except Exception:
                pass
    except Exception:
        LOG_CURRENT_EXCEPTION()
        try:
            _PENDING_BOTS.remove(rec)
        except ValueError:
            pass
        return

    if not _PENDING_WAIT_ACTIVE:
        _PENDING_WAIT_ACTIVE = True
        BigWorld.callback(BOT_ENTITY_WAIT_STEP, lambda: _waitForBotEntity(token, BOT_ENTITY_WAIT_TRIES))


def _beginSpawning(token, triesLeft=100):
    if token != _SPAWN_TOKEN or not _active():
        return
    avatar = BigWorld.player()
    if avatar is None or not _makePlan(avatar):
        # машина игрока ещё не готова - пробуем снова каждые 0.1 с (раньше план строился один раз и боты могли не появиться)
        if triesLeft > 0:
            BigWorld.callback(0.1, lambda: _beginSpawning(token, triesLeft - 1))
        else:
            LOG_ERROR('[BOTS] cannot make spawn plan (player vehicle not ready)')
        return

    _makeRoster(_getPlayerVehicle())

    # Pick a stable random count of SPGs for each team in this battle.
    global _SPG_SLOT_BY_TEAM
    _SPG_SLOT_BY_TEAM = {}
    for team, count in ((TEAM_ENEMY, BOT_ENEMY_COUNT), (TEAM_ALLY, BOT_ALLY_COUNT)):
        # До четырёх случайных слотов плюс максимум одна САУ из состава матчмейкера = не более пяти на команду.
        spgCount = random.randint(1, min(4, count)) if count >= 1 else 0
        if TUTORIAL[0]:
            spgCount = 0
        _SPG_SLOT_BY_TEAM[team] = set(random.sample(range(count), spgCount)) if spgCount else set()
    LOG_DEBUG('[BOTS] random SPG slots: %s' % _SPG_SLOT_BY_TEAM)

    queue = []
    for i in range(max(BOT_ENEMY_COUNT, BOT_ALLY_COUNT)):
        if i < BOT_ENEMY_COUNT:
            queue.append((TEAM_ENEMY, i))
        if i < BOT_ALLY_COUNT:
            queue.append((TEAM_ALLY, i))

    global _CHECK_END_AFTER
    _CHECK_END_AFTER = BigWorld.time() + len(queue) * BOT_SPAWN_INTERVAL + 8.0

    for k, (team, idx) in enumerate(queue):
        BigWorld.callback(k * BOT_SPAWN_INTERVAL,
                          lambda t=team, i=idx: _spawnOne(token, t, i))


# ============================================================
# Battle hooks (вызываются из battle.py / endbattle.py)
# ============================================================
TUT_POINTS = {}                  # {1: (x, y, z, yaw), ...} - точные места мишеней учебного боя (AutoBotSpawnPoint карты); пусто - расчётные места
TUT_PATROL = {}                  # {3: [(x, z), (x, z)]} - мишень, которая ездит между точками (SimplePatrolNode карты)
TUT_VEHICLES = {}                # {1: compDescr, 2: ..., 3: ...} - учебные машины мишеней (ставит tutor.py); пусто - подбор по классу/уровню
TUTORIAL = [False]              # учебный бой: враги стоят близко впереди и не двигаются (мишени)
COUNT_OVERRIDE = None           # (враги, союзники) из тренировочной комнаты, используется один раз


def _loadBotCounts():
    """Количество ботов из offline_user.txt (enemy_bots / ally_bots), читается в начале каждого боя."""
    global BOT_ENEMY_COUNT, BOT_ALLY_COUNT, COUNT_OVERRIDE
    try:
        from gui.mods.offhangar import _constants as _c0
        BOT_MODULES[0] = _c0.getSettingInt('botmodules', 4, 0, 4)
    except Exception:
        pass
    FREE_BALANCE[0] = FREE_BALANCE_NEXT[0]
    ROOM_ROSTER[0] = ROOM_ROSTER_NEXT[0]
    ROOM_ROSTER_NEXT[0] = None
    FREE_BALANCE_NEXT[0] = False
    if COUNT_OVERRIDE is not None:
        BOT_ENEMY_COUNT, BOT_ALLY_COUNT = COUNT_OVERRIDE
        COUNT_OVERRIDE = None
        return
    try:
        from gui.mods.offhangar import _constants as _c
        BOT_ENEMY_COUNT = _c.getSettingInt('enemybots', _DEFAULT_ENEMY, 1, 29)
        BOT_ALLY_COUNT = _c.getSettingInt('allybots', _DEFAULT_ALLY, 0, 29)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def onBattleStart(avatar):
    global _SPAWN_TOKEN, _SPAWNED_FOR

    if _SPAWNED_FOR is avatar:
        return
    onBattleEnd()                       # на всякий случай чистим остатки прошлого боя
    _USED_NAMES.clear()                 # ники прошлого боя нужны были только экрану результатов
    _CAPTURERS.clear()
    _BOT_NAME_BY_ID.clear()
    _SPAWNED_FOR = avatar
    token = _SPAWN_TOKEN
    _loadBotCounts()
    try:
        from gui.mods.offhangar import feedback
        feedback.start()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        off_spotting.start()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from gui.mods.offhangar import devices
        devices.start()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from gui.mods.offhangar import drown
        drown.start()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from gui.mods.offhangar import capture
        if not TUTORIAL[0]:
            capture.start()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        _perf.start()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    LOG_NOTE('[BOTS] battle started: %s enemies + %s allies in %.1fs' % (
        BOT_ENEMY_COUNT, BOT_ALLY_COUNT, BOT_SPAWN_DELAY))
    BigWorld.callback(BOT_SPAWN_DELAY, lambda: _beginSpawning(token))


def onBattleEnd():
    global _SPAWN_TOKEN, _SPAWNED_FOR, _PENDING_WAIT_ACTIVE
    try:
        _perf.stop()
    except Exception:
        pass
    try:
        off_spotting.stop()
    except Exception:
        pass
    try:
        from gui.mods.offhangar import devices
        devices.stop()
    except Exception:
        pass
    try:
        from gui.mods.offhangar import drown
        drown.stop()
    except Exception:
        pass
    try:
        off_nav.NAV.reset()
    except Exception:
        pass
    try:
        from gui.mods.offhangar import capture
        capture.stop()
    except Exception:
        pass

    _SPAWN_TOKEN += 1
    _SPAWNED_FOR = None
    _PENDING_WAIT_ACTIVE = False
    try:
        from gui.mods.offhangar import feedback
        feedback.stop()
    except Exception:
        pass

    for ai in list(_BOTS.values()):
        try:
            ai.stop()
        except Exception:
            LOG_CURRENT_EXCEPTION()
    _BOTS.clear()
    del _PENDING_BOTS[:]
    _PLAN.clear()
    _AXIS.clear()
    _REGISTERED_IDS.clear()
    _KILLED_IDS.clear()
    _FRAGS.clear()
    global _END_SCHEDULED
    _END_SCHEDULED = False


def onVehicleDamaged(target, shooterID):
    """Вызывается системой выстрелов после попадания: бот запоминает атакующего."""
    try:
        bot = _BOTS.get(target.id)
        if bot is not None and bot.alive:
            bot.onAttacked(shooterID)
        shooter = BigWorld.entity(shooterID)
        if shooter is not None:
            off_spotting.onShot(shooter, target)
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Registration
# ============================================================
def register():
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    _patchVehicleOnEnterWorld()
    try:
        from gui.mods.offhangar import feedback
        feedback.register()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    LOG_NOTE('[BOTS] registered (enemies=%s, allies=%s)' % (BOT_ENEMY_COUNT, BOT_ALLY_COUNT))


def fini():
    global _REGISTERED
    _REGISTERED = False
    onBattleEnd()
