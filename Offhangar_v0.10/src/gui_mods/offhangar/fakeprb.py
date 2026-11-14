# -*- coding: utf-8 -*-
"""Игровые режимы из меню "В бой!": Тренировочный бой и Исторический бой - родные окна клиента, сервер подменён.

Тренировочный бой.
  Клиент ничего не рисует сам: окна "Список комнат" и "Комната" работают от объекта player.prebattle (ClientPrebattle).
  Настоящий сервер присылает ему обновления (настройки, составы команд, состояния команд). Здесь мы шлём те же
  обновления сами:
    - создание комнаты (карта, длина боя, комментарий) -> Account.onPrebattleJoined + SETTINGS/ROSTER/TEAM_STATES;
    - в комнате живут "боты-игроки": по умолчанию как в обычном бою, кнопка "Выгнать" убирает бота (это и есть выбор
      числа ботов), "Пригласить" добавляет бота, перетаскивание/смена команд работают как обычно;
    - "Начать бой" (обе команды готовы) -> бой на выбранной карте, число ботов = состав команд;
    - в списке комнат несколько чужих комнат (создатель - бот): в них можно войти, создатель сам начнёт бой,
      когда вы будете готовы.
Исторический бой.
  Окно списка исторических боёв берёт данные из g_eventsCache.getHistoricalBattles(), т.е. из eventsData['historicalBattlesClientData'].
  Подставляем один исторический бой, в котором участвуют все ваши танки (штатная комплектация), карта случайная и
  меняется после каждого боя. Кнопка "В бой" -> экран ожидания -> бой.
"""
import cPickle
import random
import time

import BigWorld
import AccountCommands

from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE
from gui.mods.offhangar._constants import OFFLINE_NICKNAME, OFFLINE_DBID
from gui.mods.offhangar.server import BASE_REQUESTS, FakeServer

ENABLED = True
ROOM_BOTS_PER_TEAM = 14        # сколько ботов в команде при создании комнаты (игрок - ещё один в команде 1)
TEAM_MAX = 15                  # мест в команде
START_COUNTDOWN = 3.0          # пауза между "обе команды готовы" и стартом боя
HOST_START_DELAY = (8.0, 16.0) # через сколько создатель чужой комнаты начнёт бой, когда вы готовы
FAKE_ROOMS = 5

# ключи составов (PREBATTLE_ROSTER)
R_T1, R_T2, R_U1, R_U2 = 1, 2, 17, 18

_T = {'on': False, 'id': 0, 'token': 0, 'host': None, 'nextID': 1, 'settings': None, 'players': {}, 'teams': [None, 1, 1],
      'startToken': 0}
_ROOMS = []
_RETURN = [None]          # снимок комнаты на время тренировочного боя: после боя игрок возвращается в ту же комнату
_HIST = {'id': 100, 'arena': 0, 'data': None}


# ----------------------------------------------------------------------------------------------------------------------
# общее
# ----------------------------------------------------------------------------------------------------------------------
def _player():
    try:
        return BigWorld.player()
    except Exception:
        return None


def _isAccount():
    p = _player()
    return p is not None and hasattr(p, 'prb_createTraining')


def _events():
    from PlayerEvents import g_playerEvents
    return g_playerEvents


def _ok(requestID):
    return (requestID, AccountCommands.RES_SUCCESS, '', None)


def _fail(requestID):
    return (requestID, AccountCommands.RES_FAILURE, '', None)


def _ownVehicle(vehInvID=None):
    """Танк игрока: (invID, compDescr)."""
    try:
        from gui.shared import g_itemsCache
        veh = None
        if vehInvID:
            veh = g_itemsCache.items.getVehicle(vehInvID)
        if veh is None:
            from CurrentVehicle import g_currentVehicle
            if g_currentVehicle.isPresent():
                veh = g_currentVehicle.item
        if veh is not None:
            return veh.invID, veh.descriptor.makeCompactDescr()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 0, ''


def _botNick():
    try:
        from gui.mods.offhangar import bots
        return bots._uniqueNick()
    except Exception:
        return 'Bot%d' % random.randint(1000, 99999)


def _randomVehCD(level=None, exclude=''):
    """compDescr случайного танка (для ботов в комнате). level=None - любой уровень 1-10 (без баланса), иначе около level."""
    try:
        from items import vehicles
        import nations
        from gui.mods.offhangar import matchmaker
        if 'rows' not in _VEH_CACHE:                    # список танков строим один раз (бот меняет танк каждые 5 с)
            names = matchmaker.excludedNames()
            rows = []
            for nat in nations.AVAILABLE_NAMES:
                nid = nations.INDICES[nat]
                for row in vehicles.g_list.getList(nid).values():
                    if 'observer' in row.get('tags', ()) or matchmaker.isExcluded(row['name'], names):
                        continue
                    rows.append((nid, row['id'], row.get('level', 0)))
            _VEH_CACHE['rows'] = rows
        pool = [(n, v) for n, v, lv in _VEH_CACHE['rows'] if level is None or abs(lv - level) <= 1]
        for _ in range(5):
            if not pool:
                break
            nid, vid = random.choice(pool)
            cd = vehicles.VehicleDescr(typeID=(nid, vid)).makeCompactDescr()
            if cd != exclude:
                return cd
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return ''


_VEH_CACHE = {}
ROTATE_INTERVAL = 5.0           # боты в комнате меняют танк каждые N секунд


def _rotateBots(token):
    """Каждые 5 секунд готовые боты комнаты меняют танк на другой случайный (любого уровня)."""
    if _T['token'] != token or not _T['on']:
        return
    try:
        for pid, pl in list(_T['players'].items()):
            if pl.get('bot') and pl.get('veh'):
                cd = _randomVehCD(None, pl['veh'])
                if cd:
                    pl['veh'] = cd
                    _pushPlayerState(pid)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    BigWorld.callback(ROTATE_INTERVAL, lambda: _rotateBots(token))


def _ownLevel():
    try:
        from CurrentVehicle import g_currentVehicle
        if g_currentVehicle.isPresent():
            return g_currentVehicle.item.level
    except Exception:
        pass
    return 5


# ----------------------------------------------------------------------------------------------------------------------
# тренировочная комната: данные
# ----------------------------------------------------------------------------------------------------------------------
def _info(pid):
    pl = _T['players'][pid]
    return (pid, pl['name'], pl['dbID'], pl['roster'], pl['state'], pl['time'], pl['veh'], 0, 0, pl.get('clan', ''))


def _send(updateType, obj):
    p = _player()
    prb = getattr(p, 'prebattle', None)
    if prb is None:
        return
    prb.update(updateType, cPickle.dumps(obj, 2))


def _pushRoster():
    from constants import PREBATTLE_UPDATE
    _send(PREBATTLE_UPDATE.ROSTER, [_info(pid) for pid in _T['players']])


def _pushTeamStates():
    from constants import PREBATTLE_UPDATE
    _send(PREBATTLE_UPDATE.TEAM_STATES, (_T['teams'][1], _T['teams'][2]))


def _pushSetting(name, value):
    from constants import PREBATTLE_UPDATE
    _T['settings'][name] = value
    _send(PREBATTLE_UPDATE.SETTING, (name, value))


def _pushPlayerState(pid):
    from constants import PREBATTLE_UPDATE
    pl = _T['players'][pid]
    _send(PREBATTLE_UPDATE.PLAYER_STATE, (pid, pl['roster'], pl['state'], pl['veh'], 0, 0, pl.get('clan', '')))


def _teamOf(roster):
    return roster & 15


def _assigned(roster):
    return not roster & 240


def _count(roster):
    return len([1 for pl in _T['players'].values() if pl['roster'] == roster])


def _addBot(roster, level=None, ready=True):
    from constants import PREBATTLE_ACCOUNT_STATE
    _T['nextID'] += 1
    pid = 500000 + _T['nextID']
    veh = _randomVehCD(None) if ready else ''
    _T['players'][pid] = {'name': _botNick(), 'dbID': 2000000 + _T['nextID'], 'roster': roster,
                          'state': PREBATTLE_ACCOUNT_STATE.READY if (ready and veh) else PREBATTLE_ACCOUNT_STATE.NOT_READY,
                          'veh': veh if ready else '', 'time': time.time(), 'bot': True}
    return pid


def _ownID():
    return getattr(_player(), 'id', 1)


def _buildSettings(arenaTypeID, roundLen, isOpened, comment, hostName=None, hostDBID=0):
    from constants import PREBATTLE_TYPE, ARENA_GUI_TYPE, PREBATTLE_ROLE
    own = hostName is None
    roles = {}
    roles[OFFLINE_DBID if own else hostDBID] = PREBATTLE_ROLE.TRAINING_CREATOR
    s = {'type': PREBATTLE_TYPE.TRAINING, 'ver': 1, 'arenaGuiType': ARENA_GUI_TYPE.TRAINING,
         'creator': OFFLINE_NICKNAME if own else hostName, 'creatorDBID': OFFLINE_DBID if own else hostDBID,
         'creatorClanDBID': 0, 'creatorClanAbbrev': '', 'creatorIGRType': 0,
         'isOpened': bool(isOpened), 'comment': comment or '', 'arenaTypeID': int(arenaTypeID), 'roundLength': int(roundLen),
         'arenaVoipChannels': 0, 'roles': roles, 'clanRoles': {}, 'defaultRoster': R_U1, 'startTime': int(time.time()),
         'limits': {0: {'maxCount': (TEAM_MAX, TEAM_MAX), 'maxCountTotal': TEAM_MAX * 2, 'minCount': 1}, 1: {}, 2: {}},
         'extraData': {}, 'bonusType': 2}
    return s


def _joinRoom(settings, players, ownRoster):
    """Входим в комнату: Account.onPrebattleJoined, затем (как с настоящего сервера) настройки, свойства, составы."""
    from constants import PREBATTLE_UPDATE, PREBATTLE_ACCOUNT_STATE
    p = _player()
    st = _T
    st['token'] += 1
    st['startToken'] += 1
    st['on'] = True
    st['nextID'] += 1
    st['id'] = 900000 + st['nextID']
    st['settings'] = settings
    st['players'] = players
    st['teams'] = [None, 1, 1]
    vid, vcd = _ownVehicle()
    players[_ownID()] = {'name': OFFLINE_NICKNAME, 'dbID': OFFLINE_DBID, 'roster': ownRoster,
                         'state': PREBATTLE_ACCOUNT_STATE.NOT_READY, 'veh': '', 'time': time.time(), 'bot': False}
    token = st['token']
    p.onPrebattleJoined(st['id'])

    def _feed():
        if _T['token'] != token or not _T['on']:
            return
        try:
            _send(PREBATTLE_UPDATE.SETTINGS, _T['settings'])
            _send(PREBATTLE_UPDATE.PROPERTIES, {'wins': [0, 0, 0], 'battlesCount': 0, 'createTime': int(time.time())})
            _send(PREBATTLE_UPDATE.TEAM_STATES, (_T['teams'][1], _T['teams'][2]))
            _pushRoster()
        except Exception:
            LOG_CURRENT_EXCEPTION()
    BigWorld.callback(0.05, _feed)
    BigWorld.callback(ROTATE_INTERVAL, lambda: _rotateBots(token))
    LOG_DEBUG('[PRB] joined training room id=%s' % st['id'])


def createRoom(arenaTypeID, roundLen, isOpened, comment):
    """Своя комната: игрок в команде 1, по умолчанию боты в обеих командах."""
    if not _isAccount():
        return
    players = {}
    _T['players'] = players
    s = _buildSettings(arenaTypeID, roundLen, isOpened, comment)
    try:
        from gui.mods.offhangar import bots
        enemy, ally = bots.BOT_ENEMY_COUNT, bots.BOT_ALLY_COUNT
    except Exception:
        enemy, ally = ROOM_BOTS_PER_TEAM, ROOM_BOTS_PER_TEAM
    enemy = max(0, min(TEAM_MAX, min(enemy, ROOM_BOTS_PER_TEAM + 1)))
    ally = max(0, min(TEAM_MAX - 1, ally))
    lvl = _ownLevel()
    _T['host'] = None
    _joinRoom(s, players, R_T1)
    for _ in range(ally):
        _addBot(R_T1, lvl)
    for _ in range(enemy):
        _addBot(R_T2, lvl)


def _joinForeign(prbID):
    """Чужая комната из списка: создатель - бот."""
    room = None
    for r in _ROOMS:
        if r[1] == prbID:
            room = r
    if room is None or not _isAccount():
        return False
    info = room[2]
    from constants import PREBATTLE_CACHE_KEY as K
    players = {}
    _T['players'] = players
    hostDB = info.get(K.CREATOR_DB_ID, 3000000)
    s = _buildSettings(info.get(K.ARENA_TYPE_ID, 0), info.get(K.ROUND_LENGTH, 900), True, info.get(K.COMMENT, ''),
                       hostName=info.get(K.CREATOR, 'Host'), hostDBID=hostDB)
    lvl = _ownLevel()
    _joinRoom(s, players, R_U1)
    hostID = _addBot(R_T1, lvl)
    players[hostID]['name'] = info.get(K.CREATOR, 'Host')
    players[hostID]['dbID'] = hostDB
    for _ in range(max(0, int(info.get(K.PLAYER_COUNT, 6)) - 1)):
        _addBot(random.choice((R_T1, R_T2, R_U1)), lvl)
    _T['host'] = hostID
    _scheduleHost()
    return True


def _leaveRoom():
    _RETURN[0] = None
    _T['on'] = False
    _T['token'] += 1
    _T['startToken'] += 1
    _T['host'] = None
    p = _player()
    try:
        if p is not None and getattr(p, 'prebattle', None) is not None:
            p.onPrebattleLeft()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    LOG_DEBUG('[PRB] left training room')


# ----------------------------------------------------------------------------------------------------------------------
# старт боя из комнаты
# ----------------------------------------------------------------------------------------------------------------------
def _ownPlayerRec():
    return _T['players'].get(_ownID())


def _startFromRoom():
    """Обе команды готовы: считаем ботов по составам и запускаем бой."""
    from gui.mods.offhangar import battle as off_battle
    from gui.mods.offhangar import bots
    from gui.mods.offhangar import requests as off_req
    me = _ownPlayerRec()
    if me is None:
        return
    vehInvID, _ = _ownVehicle()
    try:
        if not off_req.checkCrewForBattle(vehInvID)[0]:
            _T['teams'] = [None, 1, 1]
            _pushTeamStates()
            return
    except Exception:
        LOG_CURRENT_EXCEPTION()
    myTeam = _teamOf(me['roster'])
    ally = enemy = 0
    roomRoster = {1: [], 2: []}
    from items import vehicles as _vehs
    for pid, pl in _T['players'].items():
        if pid == _ownID() or not _assigned(pl['roster']):
            continue
        team = 1 if _teamOf(pl['roster']) == myTeam else 2
        if team == 1:
            ally += 1
        else:
            enemy += 1
        try:
            if pl.get('veh'):
                roomRoster[team].append(tuple(_vehs.VehicleDescr(compactDescr=pl['veh']).type.id))
        except Exception:
            LOG_CURRENT_EXCEPTION()
    bots.ROOM_ROSTER_NEXT[0] = roomRoster          # в бою боты на тех же танках, что были в окне комнаты
    bots.COUNT_OVERRIDE = (max(1, enemy), max(0, ally))
    bots.FREE_BALANCE_NEXT[0] = True            # в тренировке уровни не балансируются (1 уровень против 10 и т.д.)
    arenaTypeID = int(_T['settings'].get('arenaTypeID') or 0)
    LOG_NOTE('[PRB] training battle: map=%s allies=%s enemies=%s' % (arenaTypeID, ally, enemy))
    try:
        import copy
        from constants import PREBATTLE_ACCOUNT_STATE as _PAS
        snap = dict((pid, copy.deepcopy(pl)) for pid, pl in _T['players'].items() if pid != _ownID())
        for pl in snap.values():
            # боты остаются «готовыми» с танками (иначе в окне у них «вышел» и пустые танки)
            if pl.get('bot'):
                if pl.get('veh'):
                    pl['state'] = _PAS.READY
            else:
                pl['state'] = _PAS.NOT_READY
        _RETURN[0] = {'settings': copy.deepcopy(_T['settings']), 'players': snap, 'roster': me['roster']}
    except Exception:
        _RETURN[0] = None
        LOG_CURRENT_EXCEPTION()
    _T['on'] = False
    _T['token'] += 1

    def _go():
        try:
            off_battle.setNextBattleKind('training')      # тип боя TRAINING: без опыта, кредитов, статистики и наград
            off_battle._startBattle(vehInvID, arenaTypeID)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    BigWorld.callback(0.2, _go)


def _checkStart():
    """Если обе команды в состоянии READY - стартуем через START_COUNTDOWN секунд."""
    from constants import PREBATTLE_TEAM_STATE
    if _T['teams'][1] == PREBATTLE_TEAM_STATE.READY and _T['teams'][2] == PREBATTLE_TEAM_STATE.READY:
        _T['startToken'] += 1
        tok = _T['startToken']

        def _fire():
            if _T['on'] and _T['startToken'] == tok:
                _startFromRoom()
        BigWorld.callback(START_COUNTDOWN, _fire)


def _scheduleHost():
    token = _T['token']

    def _tick():
        if _T['token'] != token or not _T['on'] or _T['host'] is None:
            return
        from constants import PREBATTLE_ACCOUNT_STATE, PREBATTLE_TEAM_STATE
        me = _ownPlayerRec()
        if me is None or not me['state'] & PREBATTLE_ACCOUNT_STATE.READY:
            BigWorld.callback(5.0, _tick)
            return
        if not _assigned(me['roster']):
            _movePlayer(_ownID(), R_T1 if _count(R_T1) <= _count(R_T2) else R_T2, _T['host'])
        for t in (1, 2):
            _T['teams'][t] = PREBATTLE_TEAM_STATE.READY
        _pushTeamStates()
        _checkStart()
    BigWorld.callback(random.uniform(*HOST_START_DELAY), _tick)


def _movePlayer(pid, roster, actor=0):
    from constants import PREBATTLE_UPDATE
    pl = _T['players'][pid]
    prev = pl['roster']
    pl['roster'] = roster
    _send(PREBATTLE_UPDATE.PLAYER_ROSTER, (pid, prev, roster, actor or _ownID()))


# ----------------------------------------------------------------------------------------------------------------------
# команды клиента (Account.prb_*)
# ----------------------------------------------------------------------------------------------------------------------
def _cmdReady(requestID, vehInvID, a=0, b=0):
    from constants import PREBATTLE_ACCOUNT_STATE
    me = _ownPlayerRec()
    if not _T['on'] or me is None:
        return _fail(requestID)
    _, vcd = _ownVehicle(vehInvID)
    if not vcd:
        return _fail(requestID)
    me['state'] = PREBATTLE_ACCOUNT_STATE.READY
    me['veh'] = vcd
    _pushPlayerState(_ownID())
    return _ok(requestID)


def _cmdNotReady(requestID, state=1, a=0, b=0):
    from constants import PREBATTLE_ACCOUNT_STATE
    me = _ownPlayerRec()
    if not _T['on'] or me is None:
        return _fail(requestID)
    me['state'] = int(state) or PREBATTLE_ACCOUNT_STATE.NOT_READY
    me['veh'] = ''
    _pushPlayerState(_ownID())
    return _ok(requestID)


def _cmdAssign(requestID, playerID, roster, b=0):
    pl = _T['players'].get(playerID)
    if not _T['on'] or pl is None or roster not in (R_T1, R_T2, R_U1, R_U2):
        return _fail(requestID)
    if _assigned(roster) and _count(roster) >= TEAM_MAX and pl['roster'] != roster:
        return _fail(requestID)
    _movePlayer(playerID, roster)
    return _ok(requestID)


def _cmdSwap(requestID, a=0, b=0, c=0):
    if not _T['on']:
        return _fail(requestID)
    swap = {R_T1: R_T2, R_T2: R_T1, R_U1: R_U2, R_U2: R_U1}
    for pl in _T['players'].values():
        pl['roster'] = swap.get(pl['roster'], pl['roster'])
    _pushRoster()
    return _ok(requestID)


def _cmdSetting(name, conv=None):
    def _h(requestID, value, b=0, c=0):
        if not _T['on']:
            return _fail(requestID)
        v = conv(value) if conv else value
        _pushSettingLater(name, v)
        return _ok(requestID)
    return _h


def _pushSettingLater(name, v):
    token = _T['token']

    def _do():
        if _T['on'] and _T['token'] == token:
            _pushSetting(name, v)
    BigWorld.callback(0.05, _do)


def _cmdComment(requestID, comment):
    if not _T['on']:
        return _fail(requestID)
    _pushSettingLater('comment', comment)
    return _ok(requestID)


def _cmdTeamReady(requestID, team, force=0, c=0):
    from constants import PREBATTLE_TEAM_STATE
    if not _T['on'] or team not in (1, 2):
        return _fail(requestID)
    _T['teams'][team] = PREBATTLE_TEAM_STATE.READY
    _pushTeamStatesLater(_checkStart)
    return _ok(requestID)


def _cmdTeamNotReady(requestID, team, b=0, c=0):
    from constants import PREBATTLE_TEAM_STATE
    if not _T['on'] or team not in (1, 2):
        return _fail(requestID)
    _T['teams'][team] = PREBATTLE_TEAM_STATE.NOT_READY
    _T['startToken'] += 1
    _pushTeamStatesLater(None)
    return _ok(requestID)


def _pushTeamStatesLater(after):
    token = _T['token']

    def _do():
        if _T['on'] and _T['token'] == token:
            _pushTeamStates()
            if after is not None:
                after()
    BigWorld.callback(0.05, _do)


def _cmdKick(requestID, playerID, b=0, c=0):
    from constants import PREBATTLE_UPDATE
    pl = _T['players'].get(playerID)
    if not _T['on'] or pl is None or playerID == _ownID():
        return _fail(requestID)
    del _T['players'][playerID]
    _send(PREBATTLE_UPDATE.PLAYER_REMOVED, (playerID, pl['roster']))
    return _ok(requestID)


def afterBattleCleanup():
    """После боя из комнаты клиент всё ещё "в комнате" (Account.prebattle не сброшен), а комнаты у нас уже нет:
    выход из неё давал ошибку. Закрываем состояние комнаты."""
    p = _player()
    try:
        if not (_T['on'] or p is None or getattr(p, 'prebattle', None) is None or not hasattr(p, 'onPrebattleLeft')):
            p.onPrebattleLeft()
            LOG_DEBUG('[PRB] room state closed after battle')
    except Exception:
        LOG_CURRENT_EXCEPTION()
    ret = _RETURN[0]
    _RETURN[0] = None
    if ret is not None and not _T['on']:
        BigWorld.callback(1.2, lambda: _rejoin(ret))


def _rejoin(ret):
    """После тренировочного боя возвращаем игрока в ту же комнату (как в игре): те же настройки, карта, боты."""
    try:
        if _T['on'] or not _isAccount():
            return
        _joinRoom(ret['settings'], ret['players'], ret['roster'])
        LOG_NOTE('[PRB] returned to the training room after battle')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _cmdLeave(requestID, a=0, b=0, c=0):
    if not _T['on']:
        BigWorld.callback(0.05, afterBattleCleanup)       # клиент думает, что в комнате, - закрываем её на его стороне
        return _ok(requestID)
    BigWorld.callback(0.05, _leaveRoom)
    return _ok(requestID)


def _cmdJoin(requestID, prbID, b=0, c=0):
    BigWorld.callback(0.05, lambda: _joinForeign(prbID))
    return _ok(requestID)


def _noop(requestID, *a):
    return _ok(requestID)


# FakeServer.createTraining / sendPrebattleInvites вызываются клиентом напрямую (не через doCmd)
def _fs_createTraining(self, arenaTypeID, roundLength, isOpened, comment):
    BigWorld.callback(0.05, lambda: createRoom(arenaTypeID, roundLength, isOpened, comment))


def _fs_sendInvites(self, accountsToInvite, comment):
    """"Пригласить": в комнату приходит бот (на каждого приглашённого, но минимум один)."""
    if not _T['on']:
        return
    from constants import PREBATTLE_UPDATE
    n = max(1, min(len(accountsToInvite or []), 5))
    for _ in range(n):
        if _count(R_U1) + _count(R_U2) >= TEAM_MAX * 2:
            break
        pid = _addBot(R_U1, _ownLevel())
        _send(PREBATTLE_UPDATE.PLAYER_ADDED, _info(pid))


# ----------------------------------------------------------------------------------------------------------------------
# список комнат
# ----------------------------------------------------------------------------------------------------------------------
def _makeRooms():
    del _ROOMS[:]
    try:
        import ArenaType
        arenas = [tid for tid, at in ArenaType.g_cache.items() if getattr(at, 'gameplayName', '') == 'ctf']
    except Exception:
        arenas = []
    from constants import PREBATTLE_CACHE_KEY as K
    comments = [u'Тренировка', u'Отработка взводов', u'Просто покатаемся', u'Тест танков', u'Заходи, не бойся',
                u'Сыграем 15 на 15', u'']
    now = time.time()
    for i in range(FAKE_ROOMS):
        _T['nextID'] += 1
        prbID = 800000 + _T['nextID']
        info = {K.TYPE: 2, K.IS_OPENED: True, K.PLAYER_COUNT: random.randint(3, 22), K.CREATOR: _botNick(),
                K.CREATE_TIME: now - random.randint(60, 3000), K.COMMENT: random.choice(comments).encode('utf-8'),
                K.ARENA_TYPE_ID: random.choice(arenas) if arenas else 0, K.ROUND_LENGTH: 900,
                K.CREATOR_DB_ID: 3000000 + prbID, K.CREATOR_CLAN_ABBREV: ''}
        _ROOMS.append((info[K.CREATE_TIME], prbID, info))


def _requestPrebattles(orig):
    def _r(self, type, sort_key, idle, start, end):
        if not (ENABLED and getattr(self, 'isOffline', False)):
            return orig(self, type, sort_key, idle, start, end)
        from constants import PREBATTLE_TYPE
        if type != PREBATTLE_TYPE.TRAINING:
            return orig(self, type, sort_key, idle, start, end)
        if not _ROOMS:
            _makeRooms()
        rooms = list(_ROOMS)
        BigWorld.callback(0.1, lambda: _events().onPrebattlesListReceived(type, len(rooms), rooms))
    return _r


# ----------------------------------------------------------------------------------------------------------------------
# исторический бой
# ----------------------------------------------------------------------------------------------------------------------
def _randomArena():
    try:
        import ArenaType
        arenas = [tid for tid, at in ArenaType.g_cache.items() if getattr(at, 'gameplayName', '') == 'ctf']
        if arenas:
            return random.choice(arenas)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 0


def _rebuildHistorical():
    """Данные для g_eventsCache: один бой, в котором можно играть любым своим танком."""
    from items import ITEM_TYPES
    from gui.shared import g_itemsCache
    import ArenaType
    _HIST['id'] += 1
    _HIST['arena'] = _randomArena()
    at = ArenaType.g_cache.get(_HIST['arena'])
    kind = getattr(at, 'vehicleCamouflageKind', 0)
    vehicles_, vehSides = {}, {}
    try:
        for veh in g_itemsCache.items.getVehicles().itervalues():
            try:
                d = veh.descriptor
                turretD, gunD = d.turrets[0]
                modules = {ITEM_TYPES.vehicleChassis: d.chassis['compactDescr'], ITEM_TYPES.vehicleEngine: d.engine['compactDescr'],
                           ITEM_TYPES.vehicleFuelTank: d.fuelTank['compactDescr'], ITEM_TYPES.vehicleRadio: d.radio['compactDescr'],
                           ITEM_TYPES.vehicleTurret: turretD['compactDescr'], ITEM_TYPES.vehicleGun: gunD['compactDescr']}
                ammo = []
                for sh in veh.shells:
                    ammo.extend([sh.intCD, sh.count])
                vd = {'modules': modules, 'ammoList': ammo, 'camouflageID': d.camouflages[kind][0] if d.camouflages else None}
                vehicles_[veh.intCD] = vd
                vehSides[veh.intCD] = 'A'
            except Exception:
                LOG_CURRENT_EXCEPTION()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    now = int(time.time())
    lang = None
    try:
        from helpers import getClientLanguage
        lang = getClientLanguage()
    except Exception:
        lang = 'ru'
    _HIST['data'] = {_HIST['id']: {
        'id': _HIST['id'], 'arenaTypeID': _HIST['arena'], 'arenaTeam1': 'A', 'vehicles': vehicles_, 'vehSides': vehSides,
        'startTime': now - 86400, 'finishTime': now + 86400 * 365, 'gStartTime': now - 86400, 'gFinishTime': now + 86400 * 400,
        'backgroundName': 'default', 'urls': {},
        'localized_data': {
            'title': {lang: u'Исторический бой'},
            'shortDescr': {lang: u'Бой на случайной карте на вашем танке в штатной комплектации.'},
            'longDescr': {lang: u'Случайная карта, состав команд подбирается автоматически.'},
            'sideNames': {lang: {'A': u'Союзники', 'B': u'Противник'}}}}}
    LOG_DEBUG('[HIST] historical battle %s rebuilt: arena=%s vehicles=%s' % (_HIST['id'], _HIST['arena'], len(vehicles_)))


def _patchEventsCache():
    try:
        from gui.shared.server_events.eventscache import _EventsCache
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return
    name = '_EventsCache__getEventsData'
    orig = getattr(_EventsCache, name, None)
    if orig is None or getattr(orig, '_offhangar', False):
        return

    def _get(cls, eventsTypeName):
        if ENABLED and eventsTypeName == 'historicalBattlesClientData':
            p = _player()
            if getattr(p, 'isOffline', False):
                if _HIST['data'] is None:
                    _rebuildHistorical()
                return _HIST['data']
        return orig(eventsTypeName)
    _get._offhangar = True
    setattr(_EventsCache, name, classmethod(_get))


def _cmdEnqueueHistorical(requestID, vehInvID, histBattleID, isCreditsAmmo):
    from constants import QUEUE_TYPE
    from gui.mods.offhangar import battle as off_battle
    from gui.mods.offhangar import requests as off_req
    from gui.mods.offhangar import queuefake
    LOG_DEBUG('[HIST] enqueue vehInvID=%s battle=%s' % (vehInvID, histBattleID))
    try:
        if not off_req.checkCrewForBattle(vehInvID)[0]:
            return _fail(requestID)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    arena = _HIST['arena']
    p = _player()
    p.onEnqueued(QUEUE_TYPE.HISTORICAL)

    def _begin():
        try:
            BigWorld.player().onArenaCreated()
        except Exception:
            LOG_CURRENT_EXCEPTION()
        _HIST['data'] = None            # следующий исторический бой - на новой случайной карте

        def _go():
            off_battle._startBattle(vehInvID, arena)
        BigWorld.callback(0.1, _go)
    queuefake.waitThen(_begin)
    return _ok(requestID)


def _cmdDequeueHistorical(requestID, a=0, b=0, c=0):
    from constants import QUEUE_TYPE
    try:
        from gui.mods.offhangar import queuefake
        queuefake._S['pending'] = None
        queuefake.stop()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    p = _player()
    BigWorld.callback(0.05, lambda: p.onDequeued(QUEUE_TYPE.HISTORICAL))
    return _ok(requestID)


# ----------------------------------------------------------------------------------------------------------------------
# регистрация
# ----------------------------------------------------------------------------------------------------------------------
_REG = [False]


def register():
    if _REG[0] or not ENABLED:
        return
    _REG[0] = True
    C = AccountCommands
    table = {'CMD_PRB_READY': _cmdReady, 'CMD_PRB_NOT_READY': _cmdNotReady, 'CMD_PRB_ASSIGN': _cmdAssign,
             'CMD_PRB_SWAP_TEAM': _cmdSwap, 'CMD_PRB_CH_ARENA': _cmdSetting('arenaTypeID', int),
             'CMD_PRB_CH_ROUND': _cmdSetting('roundLength', int), 'CMD_PRB_OPEN': _cmdSetting('isOpened', bool),
             'CMD_PRB_CH_ARENAVOIP': _cmdSetting('arenaVoipChannels', int), 'CMD_PRB_TEAM_READY': _cmdTeamReady,
             'CMD_PRB_TEAM_NOT_READY': _cmdTeamNotReady, 'CMD_PRB_KICK': _cmdKick, 'CMD_PRB_LEAVE': _cmdLeave,
             'CMD_PRB_JOIN': _cmdJoin, 'CMD_PRB_CH_GAMEPLAYSMASK': _noop, 'CMD_PRB_CH_DIVISION': _noop,
             'CMD_ENQUEUE_HISTORICAL': _cmdEnqueueHistorical, 'CMD_DEQUEUE_HISTORICAL': _cmdDequeueHistorical}
    for name, fn in table.items():
        cmd = getattr(C, name, None)
        if cmd is not None:
            BASE_REQUESTS[cmd] = fn
    cc = getattr(C, 'CMD_PRB_CH_COMMENT', None)
    if cc is not None:
        BASE_REQUESTS[cc] = _cmdComment
    FakeServer.createTraining = _fs_createTraining
    FakeServer.sendPrebattleInvites = _fs_sendInvites
    try:
        import Account
        cls = Account.PlayerAccount
        if not getattr(cls.requestPrebattles, '_offhangar', False):
            fn = _requestPrebattles(cls.requestPrebattles)
            fn._offhangar = True
            cls.requestPrebattles = fn
    except Exception:
        LOG_CURRENT_EXCEPTION()
    _patchEventsCache()
    LOG_NOTE('[PRB] registered (training rooms + historical battles)')


def passthroughAction(name):
    """Действия меню, которые должен обработать родной диспетчер (комнаты/исторические бои), а не прямой старт боя."""
    try:
        from gui.prb_control.settings import PREBATTLE_ACTION_NAME as N
        return name in (N.TRAINING_LIST, N.LEAVE_TRAINING_LIST, N.HISTORICAL, N.PREQUEUE_LEAVE, N.PREBATTLE_LEAVE,
                        N.LEAVE_HISTORICAL)
    except Exception:
        return False
