# -*- coding: utf-8 -*-
"""Офлайн-завершение боя.

Клавиша K -> бой заканчивается, считается результат (победа / поражение /
ничья), начисляются опыт / свободный опыт / кредиты, игрок возвращается в
ангар и видит стандартное окно результатов боя.

Как это работает:
  1. Во время боя собираем статистику (выстрелы, попадания, урон, фраги) -
     оборачиваем shooting._applyDamage / shooting._resolveShot.
  2. По K собираем результат в "полной форме" (то, что отдаёт
     BattleResultsCache.convertToFullForm) и кладём в _RESULTS.
     BattleResultsCache.get() перехватывается и отдаёт наш результат.
  3. Награды записываются в офлайн-кэш статистики (data.py) и потом
     пушатся в клиент через requests._applyStatsDiff.
  4. Чистим бой (боты, танки), возвращаем игроком Account, удаляем Avatar.
     Клиент сам закрывает боевой GUI и показывает ангар; окно результатов
     открывается через g_battleContext.lastArenaUniqueID (штатный путь
     LobbyView), а если он не сработал - вручную через ShowWindowEvent.
"""
import time
import random

import BigWorld
import AccountCommands
from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE, LOG_ERROR


def LOG_CURRENT_EXCEPTION():
    # полный трейс сразу в offhangar_debug.log
    try:
        import traceback
        LOG_ERROR('endbattle EXC:\n' + traceback.format_exc())
    except Exception:
        pass
from gui.mods.offhangar._constants import OFFLINE_NICKNAME, OFFLINE_DBID

# ============================================================
# Config
# ============================================================
try:
    import Keys as _Keys
    END_KEY = _Keys.KEY_K          # клавиша K
except Exception:
    END_KEY = 0x25                 # DIK_K


def _isKeyDown(key):
    # в клиенте 0.9.0 есть BigWorld.isKeyDown (wg_isKeyDown там нет - из-за этого K не срабатывала)
    try:
        return bool(BigWorld.isKeyDown(key))
    except Exception:
        return False


RESULT_VICTORY = 'victory'
RESULT_DEFEAT = 'defeat'
RESULT_DRAW = 'draw'

# Базовая награда за результат: (опыт, кредиты)
BASE_REWARD = {
    RESULT_VICTORY: (250, 9000),
    RESULT_DRAW: (120, 4500),
    RESULT_DEFEAT: (80, 3000),
}
XP_PER_DAMAGE = 0.4
CREDITS_PER_DAMAGE = 5.0
XP_PER_KILL = 90
CREDITS_PER_KILL = 600
FREE_XP_RATIO = 0.1            # свободный опыт = 10% от опыта

# Показывать на вкладке "Эффективность" урон по каждому противнику.
# Если окно результатов вдруг не открывается - поставь False.
INCLUDE_DETAILS = True

ENEMY_DBID_START = 2000000

# ============================================================
# State
# ============================================================
_REGISTERED = False
_KEY_PRESSED = False
_ENDING = False
_CTX = None            # контекст текущего боя
_FORCE = [None]        # 'win' / 'lose' - бой закончен захватом базы (capture.py)
_ACCOUNT = None        # Account-сущность (вернём её игроком)
_RESULTS = {}          # arenaUniqueID -> результаты в полной форме
_SHOOTING_PATCHED = False
_CACHE_PATCHED = False


def _active():
    from gui.mods.offhangar import battle as off_battle
    return off_battle._active()


# ============================================================
# Battle start hook (вызывается из battle._startBattle)
# ============================================================
def _nextKind():
    """'training' / 'tutorial' / None - тип боя, выставленный перед стартом (battle.setNextBattleKind)."""
    try:
        from gui.mods.offhangar import battle as _b
        return _b._NEXT_KIND[0]
    except Exception:
        return None


def _isTraining():
    return bool(_CTX) and _CTX.get('kind') == 'training'


def _isTutorial():
    return bool(_CTX) and _CTX.get('kind') == 'tutorial'


def onBattleStart(vehInvID=None):
    global _CTX, _ACCOUNT, _ENDING
    _FORCE[0] = None

    _ENDING = False
    _CTX = {
        'startTime': BigWorld.time(),
        'createTime': int(time.time()),
        'vehInvID': vehInvID,
        'kind': _nextKind(),
        'ended': False,
        'stats': {'shots': 0, 'directHits': 0, 'piercings': 0,
                  'damageDealt': 0, 'kills': 0},
        'dmgTo': {},          # vehicleID врага -> нанесённый урон
        'hitsTo': {},         # vehicleID врага -> попадания
        'pierceTo': {},       # vehicleID врага -> пробития
        'killed': set(),
        'botStats': {},       # vehicleID бота -> {'damageDealt', 'kills', 'directHits', 'piercings'}
        'botKilled': set(),   # кого уже засчитали как убитого ботом
        'enemies': {},        # vehicleID -> инфо о боте
    }

    try:
        p = BigWorld.player()
        if p is not None and hasattr(p, 'battleResultsCache'):
            _ACCOUNT = p
            LOG_DEBUG('endbattle: account entity remembered id=%s' % getattr(p, 'id', None))
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        BigWorld.callback(0.5, _trackTick)
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Stats collection
# ============================================================
def _playerVehicleID():
    try:
        return int(BigWorld.player().playerVehicleID)
    except Exception:
        return 0


def _vehTypeCD(vehicle):
    try:
        return int(vehicle.typeDescriptor.type.compactDescr)
    except Exception:
        return 0


def _trackTick():
    """Раз в полсекунды запоминаем ботов (они пропадают из bots._BOTS после смерти)."""
    ctx = _CTX
    if ctx is None or ctx['ended'] or not _active():
        return

    try:
        from gui.mods.offhangar import bots as off_bots
        for botID, botAI in list(off_bots._BOTS.items()):
            try:
                veh = botAI._vehicle
                vid = int(veh.id)
                info = ctx['enemies'].get(vid)
                if info is None:
                    info = {'botID': botID, 'compDescr': _vehTypeCD(veh),
                            'team': int(getattr(botAI, 'team', 2)),
                            'maxHealth': int(getattr(veh, 'health', 0)) or 1}
                    ctx['enemies'][vid] = info
                info['health'] = int(getattr(veh, 'health', 0))
            except Exception:
                pass
    except Exception:
        pass

    BigWorld.callback(0.5, _trackTick)


def _patchShooting():
    global _SHOOTING_PATCHED
    if _SHOOTING_PATCHED:
        return
    _SHOOTING_PATCHED = True

    try:
        from gui.mods.offhangar import shooting as sh
        from gui.mods.offhangar import battle as off_battle

        origApply = sh._applyDamage
        origResolve = sh._resolveShot

        def applyDamage(shooterID, target, shellDescr, hitAngleCos, armor, dist,
                        maxDistance, orig=origApply):
            # во время обратного отсчёта урон не наносится (ни игроком, ни ботами)
            try:
                if off_battle.isPrebattle():
                    return
            except Exception:
                pass

            prev = int(getattr(target, 'health', 0))
            result = orig(shooterID, target, shellDescr, hitAngleCos, armor, dist, maxDistance)
            try:
                _recordHit(shooterID, target, prev, int(getattr(target, 'health', prev)))
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return result

        def resolveShot(avatar, vehicle, orig=origResolve):
            try:
                ctx = _CTX
                if ctx is not None and not ctx['ended'] and vehicle.id == _playerVehicleID():
                    ctx['stats']['shots'] += 1
            except Exception:
                pass
            return orig(avatar, vehicle)

        sh._applyDamage = applyDamage
        sh._resolveShot = resolveShot
        LOG_NOTE('[ENDBATTLE] shooting stats hooks installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _recordBotHit(ctx, shooterID, target, tid, prev, new):
    """Статистика ботов для таблицы "Командный результат": урон по противникам, фраги, попадания."""
    try:
        from gui.mods.offhangar import bots as _b
        shooter = BigWorld.entity(shooterID)
        if shooter is not None and _b.getTeam(shooter) == _b.getTeam(target):
            return                          # урон по своим не засчитывается
    except Exception:
        pass
    bs = ctx['botStats'].setdefault(shooterID, {'damageDealt': 0, 'kills': 0, 'directHits': 0, 'piercings': 0, 'damaged': set()})
    bs['directHits'] += 1
    dmg = max(0, prev - new)
    if dmg > 0:
        bs['piercings'] += 1
        bs['damageDealt'] += dmg
        bs['damaged'].add(tid)
        if prev > 0 and new <= 0 and tid not in ctx['botKilled'] and tid not in ctx['killed']:
            ctx['botKilled'].add(tid)
            bs['kills'] += 1


def _botXP(result, team, damageDealt, kills):
    """Опыт бота по той же формуле, что и у игрока (без множителей игрока); результат - с точки зрения команды бота."""
    if result == RESULT_DRAW:
        res = RESULT_DRAW
    elif (result == RESULT_VICTORY) == (team == 1):
        res = RESULT_VICTORY
    else:
        res = RESULT_DEFEAT
    baseXP, baseCredits = BASE_REWARD[res]
    xp = int(baseXP + damageDealt * XP_PER_DAMAGE + kills * XP_PER_KILL)
    credits = int(baseCredits + damageDealt * CREDITS_PER_DAMAGE + kills * CREDITS_PER_KILL)
    return xp, credits


def _recordHit(shooterID, target, prev, new):
    ctx = _CTX
    if ctx is None or ctx['ended']:
        return

    pid = _playerVehicleID()
    tid = int(getattr(target, 'id', 0))
    if shooterID != pid and shooterID in ctx['enemies']:
        _recordBotHit(ctx, shooterID, target, tid, prev, new)
        return
    if shooterID != pid or tid == pid:
        return

    st = ctx['stats']
    st['directHits'] += 1
    ctx['hitsTo'][tid] = ctx['hitsTo'].get(tid, 0) + 1

    dmg = max(0, prev - new)
    if dmg > 0:
        st['piercings'] += 1
        st['damageDealt'] += dmg
        ctx['pierceTo'][tid] = ctx['pierceTo'].get(tid, 0) + 1
        ctx['dmgTo'][tid] = ctx['dmgTo'].get(tid, 0) + dmg
        if prev > 0 and new <= 0 and tid not in ctx['killed']:
            ctx['killed'].add(tid)
            st['kills'] += 1


# ============================================================
# Result / rewards
# ============================================================
def _decideResult(playerAlive, enemiesAlive, kills, enemyTotal=0):
    # победа: враги были и все уничтожены (команда победила, даже если добивал союзник)
    if enemyTotal > 0 and enemiesAlive == 0:
        return RESULT_VICTORY
    if playerAlive and enemiesAlive == 0 and kills > 0:
        return RESULT_VICTORY
    if (not playerAlive) and enemiesAlive > 0:
        return RESULT_DEFEAT
    return RESULT_DRAW


def _calcRewards(result, damageDealt, kills):
    baseXP, baseCredits = BASE_REWARD[result]
    xp = int(baseXP + damageDealt * XP_PER_DAMAGE + kills * XP_PER_KILL)
    credits = int(baseCredits + damageDealt * CREDITS_PER_DAMAGE + kills * CREDITS_PER_KILL)
    freeXP = int(xp * FREE_XP_RATIO)
    try:
        from gui.mods.offhangar import _constants as _c
        allM = _c.getSettingFloat('rewardmultiplier', 1.0)
        xp = int(xp * allM * _c.getSettingFloat('xpmultiplier', 1.0))
        credits = int(credits * allM * _c.getSettingFloat('creditsmultiplier', 1.0))
        freeXP = int(freeXP * allM * _c.getSettingFloat('freexpmultiplier', 1.0))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return xp, freeXP, credits


def _persistRewards(vehTypeCD, xp, freeXP, credits, gold=0):
    """Пишем награду в офлайн-кэш статистики. Возвращает полные значения для пуша в клиент."""
    from gui.mods.offhangar import data as off_data

    cur = off_data.getOfflineStats().get('stats', {})
    newCredits = int(cur.get('credits', 0)) + int(credits)
    newFreeXP = int(cur.get('freeXP', 0)) + int(freeXP)
    newVehXP = dict(cur.get('vehTypeXP', {}))
    if vehTypeCD:
        newVehXP[vehTypeCD] = int(newVehXP.get(vehTypeCD, 0)) + int(xp)

    stats = off_data._loadOfflineCache('stats')
    if stats is None:
        stats = off_data.getOfflineStats()
    s = stats.setdefault('stats', {})
    s['credits'] = newCredits
    s['freeXP'] = newFreeXP
    s['vehTypeXP'] = newVehXP
    pushed = {'credits': newCredits, 'freeXP': newFreeXP, 'vehTypeXP': newVehXP}
    if int(gold) > 0:
        newGold = int(cur.get('gold', 0)) + int(gold)
        s['gold'] = newGold
        pushed['gold'] = newGold
    off_data._saveOfflineCache('stats', stats)

    LOG_DEBUG('endbattle: rewards saved credits=%s freeXP=%s gold+=%s vehXP[%s]=%s' % (
        newCredits, newFreeXP, gold, vehTypeCD, newVehXP.get(vehTypeCD)))
    return pushed


_PENDING_CREW = {}          # tankman inv ID -> новый compDescr (после боя, пушится в ангар вместе с наградой)
CREW_XP_RATIO = 1.0         # доля опыта боя, которую получает каждый член экипажа (как в игре - весь опыт боя)
CREW_XP_MIN = 10            # минимум опыта экипажу за бой (даже за поражение с нулевым уроном)
CREW_LEVEL_GAIN = (10, 30)  # на сколько процентов за бой растёт уровень специальности / текущего перка у каждого члена экипажа (случайно из диапазона);
                            # None - старый режим: настоящий боевой опыт (battleXpGain)


def _stageOf(d):
    """Что сейчас качается у танкиста: (текущий уровень, номер навыка для стоимости) или None, если всё уже 100%."""
    if d.roleLevel < 100:
        return d.roleLevel, 0
    if d.skills and d.lastSkillLevel < 100:
        return d.lastSkillLevel, len(d.skills)
    return None


def _levelBy(d, percent):
    """Поднять уровень танкиста примерно на percent процентов: сначала специальность, потом последний перк; когда всё 100% - копим опыт на новый перк.
    Возвращает потраченный опыт."""
    from items import tankmen
    cost = tankmen.TankmanDescr.levelUpXpCost
    spent = 0
    gained = 0
    guard = 0
    while gained < percent and guard < 200:
        guard += 1
        st = _stageOf(d)
        if st is None:
            # всё прокачано: остаток идёт в свободный опыт танкиста - прогресс к новому перку (стоимость уровней нового перка)
            need = sum(cost(i, len(d.skills)) for i in range(gained, percent))
            d.addXP(need)
            spent += need
            break
        lvl, seq = st
        xp = max(1, cost(lvl, seq))
        d.addXP(xp)
        spent += xp
        gained += 1
    return spent


def _awardCrewXP(vehInvID, veh, xp, survived):
    """Экипаж играемой машины получает боевой опыт (за победу и за поражение): растут уровень специальности и перки.
    Пишет новые compDescr в офлайн-инвентарь и возвращает {tankmanInvID: compDescr} для пуша в ангар."""
    from items import tankmen
    from gui.mods.offhangar import requests as off_requests
    from items import ITEM_TYPE_INDICES, vehicles as _vehs
    if not vehInvID:
        LOG_DEBUG('[CREW] no vehInvID/vehicle - crew xp skipped')
        return {}
    root = off_requests._getOfflineInventoryRoot().get('inventory', {})
    crew = root.get(ITEM_TYPE_INDICES['vehicle'], {}).get('crew', {}).get(vehInvID) or []
    tmanCDs = root.setdefault(ITEM_TYPE_INDICES['tankman'], {}).setdefault('compDescr', {})
    vehType = None
    try:
        vehType = veh.typeDescriptor.type
    except Exception:
        try:
            vehType = _vehs.VehicleDescr(compactDescr=off_requests._getCurrentVehicleCompDescr(vehInvID)).type
        except Exception:
            LOG_CURRENT_EXCEPTION()
    if vehType is None:
        return {}
    battleXP = max(CREW_XP_MIN, int(xp * CREW_XP_RATIO))

    descrs = {}
    for tid in crew:
        cd = tmanCDs.get(tid) if tid else None
        if cd:
            try:
                descrs[tid] = tankmen.TankmanDescr(cd)
            except Exception:
                LOG_CURRENT_EXCEPTION()
    if not descrs:
        LOG_DEBUG('[CREW] vehInvID=%s: no crew in the tank - nothing to level' % vehInvID)
        return {}
    factor = 1.0
    try:
        factor = tankmen.commanderTutorXpBonusFactorForCrew(descrs.values())
    except Exception:
        pass

    result = {}
    for tid, d in descrs.items():
        try:
            before = (d.roleLevel, len(d.skills), d.lastSkillLevel, d.freeXP)
            if CREW_LEVEL_GAIN:
                pct = random.randint(int(CREW_LEVEL_GAIN[0]), int(CREW_LEVEL_GAIN[1]))
                gain = _levelBy(d, pct)
            else:
                gain = d.battleXpGain(battleXP, vehType, bool(survived), factor)
                if isinstance(gain, (tuple, list)):
                    gain = gain[0]
                gain = int(gain)
                d.addXP(gain)
            newCD = d.makeCompactDescr()
            tmanCDs[tid] = newCD
            result[tid] = newCD
            LOG_NOTE('[CREW] tankman %s (%s): +%s xp, role %s->%s, skills %s->%s, lastSkill %s->%s, free xp %s->%s' % (
                tid, d.role, gain, before[0], d.roleLevel, before[1], len(d.skills), before[2], d.lastSkillLevel, before[3], d.freeXP))
        except Exception:
            LOG_CURRENT_EXCEPTION()
    if result:
        off_requests._markInventoryDirty()
    return result


def _pushCrewToClient(crewCDs):
    try:
        from gui.mods.offhangar import requests as off_requests
        off_requests._commitTankmanChanges({tid: {'compDescr': cd} for tid, cd in crewCDs.items()})
        LOG_DEBUG('[CREW] crew levels pushed to client (%d tankmen)' % len(crewCDs))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _pushRewardsToClient(fields):
    """Обновляем уже запущенный ангар (после возврата Account игроком)."""
    try:
        from gui.mods.offhangar import requests as off_requests
        off_requests._applyStatsDiff(fields)
        LOG_DEBUG('endbattle: rewards pushed to client')
        try:
            from gui.mods.offhangar import dossier as off_dossier
            off_dossier.resyncClient()
        except Exception:
            LOG_CURRENT_EXCEPTION()
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Build battle results in "full form"
# ============================================================
_LIST_KEYS = ('achievements', 'protoAchievements', 'spot_list', 'damage_list',
              'kill_list', 'ammo', 'crewActivityFlags', 'dossierPopUps')
_DICT_KEYS = ('details', 'misc', 'series', 'destroyedObjects', 'questsProgress',
              'eventIndices', 'cybersportRatingDeltas', 'tkillLog')
_TUPLE2_KEYS = ('autoLoadCost', 'autoEquipCost', 'histAmmoCost')
_BOOL_KEYS = ('isPremium', 'isEnemyBaseCaptured', 'isTeamKiller', 'isPrematureLeave')


def _blank(keys):
    d = {}
    for k in keys:
        if k in _LIST_KEYS:
            d[k] = []
        elif k in _DICT_KEYS:
            d[k] = {}
        elif k in _TUPLE2_KEYS:
            d[k] = (0, 0)
        elif k in _BOOL_KEYS:
            d[k] = False
        elif k in ('deathReason',):
            d[k] = -1
        else:
            d[k] = 0
    return d


def _sharedKeys():
    import battle_results_shared as brs
    return brs.VEH_FULL_RESULTS, brs.VEH_PUBLIC_RESULTS, brs.PLAYER_INFO, brs.COMMON_RESULTS


def _const(name, group, default):
    try:
        import constants
        return int(getattr(getattr(constants, group), name))
    except Exception:
        return default


def _buildFullResults(avatar, ctx, data):
    fullKeys, pubKeys, playerKeys, commonKeys = _sharedKeys()

    pid = data['playerVehID']
    now = BigWorld.time()
    duration = int(now - ctx['startTime'])
    result = data['result']

    winnerTeam = {RESULT_VICTORY: 1, RESULT_DEFEAT: 2, RESULT_DRAW: 0}[result]
    if _FORCE[0] in ('win', 'lose'):
        finishReason = _const('BASE', 'FINISH_REASON', 2)
    elif result == RESULT_DRAW:
        finishReason = _const('TIMEOUT', 'FINISH_REASON', 3)
    elif (result == RESULT_VICTORY and data['enemiesAlive'] == 0) or \
            (result == RESULT_DEFEAT and not data['playerAlive']):
        finishReason = _const('EXTERMINATION', 'FINISH_REASON', 1)
    else:
        finishReason = _const('TIMEOUT', 'FINISH_REASON', 3)

    st = ctx['stats']

    # ---- common ----
    common = _blank(commonKeys)
    common.update({
        'arenaTypeID': int(getattr(avatar, 'arenaTypeID', 0) or 0),
        'arenaCreateTime': ctx['createTime'],
        'winnerTeam': winnerTeam,
        'finishReason': finishReason,
        'duration': duration,
        'bonusType': _const('TRAINING', 'ARENA_BONUS_TYPE', 2) if _isTraining() else _const('REGULAR', 'ARENA_BONUS_TYPE', 1),
        'guiType': _const('TRAINING', 'ARENA_GUI_TYPE', 2) if _isTraining() else _const('RANDOM', 'ARENA_GUI_TYPE', 1),
        'vehLockMode': 0,
    })

    # ---- players ----
    players = {}
    players[OFFLINE_DBID] = {'name': OFFLINE_NICKNAME, 'clanDBID': 0, 'clanAbbrev': '',
                             'prebattleID': 0, 'team': 1, 'igrType': 0}

    # ---- vehicles (public part) ----
    vehicles = {}
    playerPub = _blank(pubKeys)
    playerPub.update({
        'accountDBID': OFFLINE_DBID, 'team': 1, 'typeCompDescr': data['vehTypeCD'],
        'health': data['health'], 'credits': data['credits'], 'xp': data['xp'],
        'shots': st['shots'], 'directHits': st['directHits'], 'piercings': st['piercings'],
        'damageDealt': st['damageDealt'], 'kills': st['kills'],
        'damageReceived': data['damageReceived'], 'lifeTime': duration,
        'deathReason': data['deathReason'], 'killerID': data['killerID'],
        'spotted': st['kills'], 'damaged': len(ctx['dmgTo']),
    })
    vehicles[pid] = playerPub

    enemyDBIDs = {}
    n = 0
    for vid, info in sorted(ctx['enemies'].items()):
        n += 1
        dbid = ENEMY_DBID_START + n
        enemyDBIDs[vid] = dbid
        botTeam = info.get('team', 2)
        _botNick = None
        try:
            from gui.mods.offhangar import bots as _off_bots
            _botNick = _off_bots.botName(info.get('botID'))
        except Exception:
            pass
        players[dbid] = {'name': _botNick or ('Bot%d' % n), 'clanDBID': 0, 'clanAbbrev': '',
                         'prebattleID': 0, 'team': botTeam, 'igrType': 0}
        pub = _blank(pubKeys)
        alive = info.get('health', 0) > 0
        bs = ctx['botStats'].get(vid) or {}
        bdmg = int(bs.get('damageDealt', 0))
        bkills = int(bs.get('kills', 0))
        bxp, bcredits = _botXP(result, botTeam, bdmg, bkills)
        pub.update({
            'accountDBID': dbid, 'team': botTeam, 'typeCompDescr': info['compDescr'],
            'health': max(0, info.get('health', 0)),
            'damageReceived': ctx['dmgTo'].get(vid, 0),
            'lifeTime': duration,
            'deathReason': -1 if alive else 0,
            'killerID': pid if (not alive and botTeam == 2) else 0,
            'damageDealt': bdmg, 'kills': bkills, 'xp': bxp, 'credits': bcredits,
            'directHits': int(bs.get('directHits', 0)), 'piercings': int(bs.get('piercings', 0)),
            'damaged': len(bs.get('damaged', ())),
        })
        vehicles[vid] = pub

    # ---- personal ----
    personal = _blank(fullKeys)
    personal.update(playerPub)
    personal.update({
        'achievements': list(data.get('achievements', [])),
        # окно итогов боя рисует медали личного результата из dossierPopUps [(id записи досье, значение)], а не из achievements
        'dossierPopUps': [(int(a), 1) for a in data.get('achievements', [])],
        'xp': data['xp'], 'originalXP': data['xp'],
        'freeXP': data['freeXP'], 'originalFreeXP': data['freeXP'],
        'credits': data['credits'], 'originalCredits': data['credits'],
        'premiumXPFactor10': 15, 'premiumCreditsFactor10': 15,
        'dailyXPFactor10': 10, 'igrXPFactor10': 10, 'aogasFactor10': 10,
        'isPremium': False,
    })

    if INCLUDE_DETAILS:
        details = {}
        for vid, dmg in ctx['dmgTo'].items():
            details[vid] = {
                'spotted': 0,
                'deathReason': 0 if vid in ctx['killed'] else -1,
                'directHits': ctx['hitsTo'].get(vid, 0),
                'explosionHits': 0,
                'piercings': ctx['pierceTo'].get(vid, 0),
                'damageDealt': dmg,
                'damageAssistedTrack': 0,
                'damageAssistedRadio': 0,
                'crits': 0,
                'fire': 0,
            }
        personal['details'] = details

    return {
        'arenaUniqueID': data['arenaUniqueID'],
        'personal': personal,
        'common': common,
        'players': players,
        'vehicles': vehicles,
    }


# ============================================================
# BattleResultsCache.get override: отдаём наши результаты
# ============================================================
def _patchResultsCache():
    global _CACHE_PATCHED
    if _CACHE_PATCHED:
        return
    _CACHE_PATCHED = True

    try:
        from account_helpers.BattleResultsCache import BattleResultsCache
        origGet = BattleResultsCache.get

        def get(self, arenaUniqueID, callback, orig=origGet):
            full = _RESULTS.get(arenaUniqueID)
            if full is None:
                try:
                    full = _RESULTS.get(int(arenaUniqueID))
                except Exception:
                    full = None
            if full is not None:
                LOG_DEBUG('endbattle: serving offline battle results for arena %s' % arenaUniqueID)
                if callback is not None:
                    callback(AccountCommands.RES_CACHE, full)
                return
            return orig(self, arenaUniqueID, callback)

        BattleResultsCache.get = get
        LOG_NOTE('[ENDBATTLE] BattleResultsCache.get patched')
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Ending the battle
# ============================================================
def endBattle():
    global _ENDING

    ctx = _CTX
    if ctx is None or _ENDING or ctx['ended'] or not _active():
        return
    _ENDING = True

    try:
        avatar = BigWorld.player()
        pid = _playerVehicleID()
        veh = BigWorld.entity(pid) if pid else None

        health = int(getattr(veh, 'health', 0)) if veh is not None else 0
        playerAlive = health > 0

        # актуализируем здоровье врагов
        enemiesAlive = 0
        enemyTotal = 0
        for vid, info in ctx['enemies'].items():
            e = BigWorld.entity(vid)
            if e is not None:
                info['health'] = int(getattr(e, 'health', 0))
            if info.get('team', 2) != 2:
                continue            # союзный бот
            enemyTotal += 1
            if info.get('health', 0) > 0:
                enemiesAlive += 1

        st = ctx['stats']
        result = _decideResult(playerAlive, enemiesAlive, st['kills'], enemyTotal)
        if _FORCE[0] == 'win':
            result = RESULT_VICTORY
        elif _FORCE[0] == 'lose':
            result = RESULT_DEFEAT
        global _LAST_RESULT
        _LAST_RESULT = result
        xp, freeXP, credits = _calcRewards(result, st['damageDealt'], st['kills'])
        training = _isTraining()
        tutorial = _isTutorial()
        tutGold = 0
        if training:
            xp = freeXP = credits = 0               # тренировочный бой: наград нет
        elif tutorial:
            # учебный бой: награда фиксированная (tutor.fixedRewards), от урона/фрагов/результата не зависит
            try:
                from gui.mods.offhangar import tutor as _tut
                xp, freeXP, credits, tutGold = _tut.fixedRewards()
            except Exception:
                LOG_CURRENT_EXCEPTION()
                xp = freeXP = credits = 0

        maxHealth = 0
        try:
            maxHealth = int(veh.typeDescriptor.maxHealth)
        except Exception:
            pass
        damageReceived = max(0, maxHealth - health) if maxHealth else 0

        killerID = 0
        deathReason = -1
        if not playerAlive:
            deathReason = 0
            for vid in sorted(ctx['enemies'].keys()):
                if ctx['enemies'][vid].get('team', 2) != 2:
                    continue
                killerID = vid
                break

        arenaUniqueID = getattr(avatar, 'arenaUniqueID', None) or int(BigWorld.time() * 1000) & 0x7fffffff
        vehTypeCD = _vehTypeCD(veh) if veh is not None else 0

        try:
            from gui.mods.offhangar import dossier as off_dossier
            _lvl = int(veh.typeDescriptor.type.level) if veh is not None else 0
            _enemyHP = sum(int(i.get('maxHealth', 0)) for i in ctx['enemies'].values() if i.get('team', 2) == 2)
            _medals = off_dossier.computeAchievements(st, _lvl, _enemyHP)
            if training or tutorial:
                _medals = []
        except Exception:
            LOG_CURRENT_EXCEPTION()
            _medals = []

        data = {
            'achievements': _medals,
            'result': result, 'playerAlive': playerAlive, 'enemiesAlive': enemiesAlive,
            'health': health, 'xp': xp, 'freeXP': freeXP, 'credits': credits,
            'damageReceived': damageReceived, 'killerID': killerID, 'deathReason': deathReason,
            'playerVehID': pid, 'vehTypeCD': vehTypeCD, 'arenaUniqueID': arenaUniqueID,
        }

        LOG_NOTE('[ENDBATTLE] result=%s xp=%s freeXP=%s credits=%s dmg=%s kills=%s shots=%s' % (
            result, xp, freeXP, credits, st['damageDealt'], st['kills'], st['shots']))

        full = _buildFullResults(avatar, ctx, data)
        _RESULTS[arenaUniqueID] = full
        _patchResultsCache()

        ctx['ended'] = True

        pushFields = None
        _PENDING_CREW.clear()
        if training:
            LOG_NOTE('[ENDBATTLE] training battle: no XP, credits, crew XP, dossier or statistics')
        elif tutorial:
            # учебный бой: только фиксированная награда; опыт экипажа, досье и статистика не затрагиваются
            LOG_NOTE('[ENDBATTLE] tutorial battle: fixed reward xp=%s freeXP=%s credits=%s gold=%s; no crew XP, dossier or statistics' % (
                xp, freeXP, credits, tutGold))
            try:
                pushFields = _persistRewards(0, xp, freeXP, credits, tutGold)
            except Exception:
                LOG_CURRENT_EXCEPTION()
        else:
            try:
                pushFields = _persistRewards(vehTypeCD, xp, freeXP, credits)
            except Exception:
                LOG_CURRENT_EXCEPTION()
            try:
                _PENDING_CREW.update(_awardCrewXP(ctx.get('vehInvID'), veh, xp, playerAlive))
            except Exception:
                LOG_CURRENT_EXCEPTION()
            try:
                from gui.mods.offhangar import dossier as off_dossier
                extra = off_dossier.recordBattle(full['personal'], full['common'], ctx, data)
                if extra:
                    if pushFields is None:
                        pushFields = {}
                    pushFields.update(extra)
            except Exception:
                LOG_CURRENT_EXCEPTION()

        _teardown(avatar, arenaUniqueID, pushFields)
    except Exception:
        LOG_CURRENT_EXCEPTION()
        _ENDING = False


def _step(name, fn):
    LOG_DEBUG('endbattle: >> %s' % name)
    try:
        r = fn()
        LOG_DEBUG('endbattle: << %s ok' % name)
        return r
    except Exception:
        LOG_ERROR('endbattle: << %s FAILED' % name)
        LOG_CURRENT_EXCEPTION()
        return None


def _teardown(avatar, arenaUniqueID, pushFields):
    """Шаг 1: гасим ботов/физику и удаляем танки (Avatar ещё игрок)."""
    from gui.mods.offhangar import battle as off_battle

    _step('resetCountdown', off_battle._resetCountdown)

    spaceID = getattr(avatar, 'spaceID', None)

    def stopBots():
        from gui.mods.offhangar import bots as off_bots
        off_bots.onBattleEnd()      # стопорит ботов, отменяет отложенный спавн, чистит списки
    _step('stopBots', stopBots)

    def stopControllers():
        for storeName in ('_simplePhysicsByVehID', '_simpleTurretByVehID'):
            store = getattr(off_battle, storeName, None)
            if store:
                for ctrl in list(store.values()):
                    try:
                        ctrl.stop()
                    except Exception:
                        pass
                store.clear()
    _step('stopControllers', stopControllers)

    def destroyVehicles():
        import Vehicle as _V
        for ent in list(BigWorld.entities.values()):
            try:
                if isinstance(ent, _V.Vehicle) and getattr(ent, 'spaceID', None) == spaceID:
                    BigWorld.destroyEntity(ent.id)
            except Exception:
                LOG_CURRENT_EXCEPTION()
    _step('destroyVehicles', destroyVehicles)

    BigWorld.callback(0.3, lambda: _switchToHangar(avatar, spaceID, arenaUniqueID, pushFields))


_LAST_RESULT = None


def _prepareResultMusic():
    """В настоящей игре в конце боя MusicController (по смене периода арены) выставляет джингл
    победы/поражения/ничьей, который играет при первом play(MUSIC_EVENT_LOBBY) из Personality.onAccountShowGUI.
    Здесь арена закрывается принудительно, поэтому выставляем то же самое вручную."""
    import MusicController as MC
    mc = MC.g_musicController
    ev = {'victory': MC.MUSIC_EVENT_COMBAT_VICTORY,
          'defeat': MC.MUSIC_EVENT_COMBAT_LOSE,
          'draw': MC.MUSIC_EVENT_COMBAT_DRAW}.get(_LAST_RESULT)
    mc.stopAmbient()
    if ev is not None:
        mc._MusicController__lastBattleResultEventId = ev
    LOG_DEBUG('endbattle: result music prepared result=%s event=%s' % (_LAST_RESULT, ev))


def _ensureLobbyMusic():
    """Страховка: если после возврата никакая лобби/итоговая музыка не играет - запускаем лобби-музыку."""
    try:
        import MusicController as MC
        mc = MC.g_musicController
        cur = mc._MusicController__eventMusicId
        ok = (MC.MUSIC_EVENT_LOBBY, MC.MUSIC_EVENT_COMBAT_VICTORY, MC.MUSIC_EVENT_COMBAT_LOSE, MC.MUSIC_EVENT_COMBAT_DRAW)
        LOG_DEBUG('endbattle: music check: eventMusicId=%s ambientId=%s' % (cur, mc._MusicController__eventAmbientId))
        if cur not in ok:
            LOG_ERROR('endbattle: lobby music was not started by the client -> starting it')
            mc.play(MC.MUSIC_EVENT_LOBBY)
            mc.play(MC.AMBIENT_EVENT_LOBBY)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _restoreAccountProxies(account):
    """Avatar.onBecomeNonPlayer делает intUserSettings.setProxy(None, None), а объект общий с Account.
    В настоящей игре после боя создаётся НОВЫЙ Account и его __init__ возвращает прокси.
    Здесь Account тот же, поэтому возвращаем прокси вручную (иначе синхронизация ангара падает
    на IntUserSettings.getCache: <lambda>() takes exactly 2 arguments)."""
    account.intUserSettings.setProxy(account, account.syncData)
    LOG_DEBUG('endbattle: account proxies restored (intUserSettings)')


def _guiState(tag):
    try:
        from gui.WindowsManager import g_windowsManager
        pl = BigWorld.player()
        LOG_DEBUG('endbattle: STATE[%s] player=%s battleWindow=%s account_is_player=%s' % (
            tag, type(pl).__name__, g_windowsManager.battleWindow, pl is _ACCOUNT))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _switchToHangar(avatar, spaceID, arenaUniqueID, pushFields):
    """Шаг 2: Avatar -> Account, удаляем Avatar (его onLeaveWorld закрывает боевой GUI), открываем результаты."""
    global _ENDING, _CTX

    _guiState('before')

    # штатный выход клиента (останавливает реплей, рассылает onPlayerEntityChanging)
    _step('avatar.leaveArena', lambda: avatar.leaveArena())

    def findAccount():
        account = _ACCOUNT
        if account is None:
            for ent in BigWorld.entities.values():
                if hasattr(ent, 'battleResultsCache'):
                    account = ent
                    break
        return account
    account = _step('findAccount', findAccount)

    # 1) Avatar уходит, пока он ещё игрок: клиентский onLeaveWorld рассчитывает на BigWorld.player().arena
    #    (MusicController.onLeaveArena) и закрывает боевой GUI, камеру и звук. Как в настоящей игре.
    _step('avatar.onLeaveWorld (avatar is still player)', lambda: avatar.onLeaveWorld())
    _guiState('after avatar.onLeaveWorld')

    _step('prepareResultMusic', _prepareResultMusic)

    # 2) Только теперь игроком становится Account
    if account is None:
        LOG_ERROR('endbattle: Account entity not found, cannot return to hangar')
    else:
        _step('BigWorld.player(account)', lambda: BigWorld.player(account))
        _step('restoreAccountProxies', lambda: _restoreAccountProxies(account))
    _guiState('after player(account)')

    _step('destroyEntity(avatar)', lambda: BigWorld.destroyEntity(avatar.id))
    _guiState('after destroy avatar')

    def markResults():
        from gui.battlecontext import g_battleContext
        g_battleContext.lastArenaUniqueID = arenaUniqueID
    _step('lastArenaUniqueID', markResults)

    _CTX = None
    _ENDING = False

    BigWorld.callback(1.5, lambda: _ensureHangar(arenaUniqueID))
    BigWorld.callback(4.0, _ensureLobbyMusic)
    if pushFields:
        BigWorld.callback(2.5, lambda: _pushRewardsToClient(pushFields))
    if _PENDING_CREW:
        crewCDs = dict(_PENDING_CREW)
        _PENDING_CREW.clear()
        BigWorld.callback(3.2, lambda: _pushCrewToClient(crewCDs))
    BigWorld.callback(3.0, lambda: _releaseSpace(spaceID))
    try:
        from gui.mods.offhangar import fakeprb as _fprb
        BigWorld.callback(2.0, _fprb.afterBattleCleanup)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    BigWorld.callback(7.0, lambda: _ensureResultsShown(arenaUniqueID))


def _ensureHangar(arenaUniqueID):
    """Страховка: если боевой GUI не закрылся сам - закрываем вручную и пишем состояние в лог."""
    _guiState('ensure')
    try:
        from gui.WindowsManager import g_windowsManager
        if g_windowsManager.battleWindow is not None:
            LOG_ERROR('endbattle: battle GUI is still alive -> forcing destroyBattle()')
            try:
                from gui.battlecontext import g_battleContext
                g_battleContext.stop()
            except Exception:
                LOG_CURRENT_EXCEPTION()
            _step('forced destroyBattle', g_windowsManager.destroyBattle)
            _guiState('after forced destroyBattle')
        if _ACCOUNT is not None and BigWorld.player() is not _ACCOUNT:
            LOG_ERROR('endbattle: player is not the Account -> BigWorld.player(account)')
            _step('BigWorld.player(account) #2', lambda: BigWorld.player(_ACCOUNT))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _releaseSpace(spaceID):
    if spaceID is None:
        return
    try:
        BigWorld.releaseSpace(spaceID)
        LOG_DEBUG('endbattle: battle space %s released' % spaceID)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _ensureResultsShown(arenaUniqueID):
    """Если LobbyView сам не открыл окно результатов - открываем вручную."""
    try:
        from gui.battlecontext import g_battleContext
        if g_battleContext.lastArenaUniqueID != arenaUniqueID:
            return          # уже показано штатным путём
        g_battleContext.lastArenaUniqueID = None

        from gui.shared import g_eventBus, events
        g_eventBus.handleEvent(events.ShowWindowEvent(
            events.ShowWindowEvent.SHOW_BATTLE_RESULTS, {'arenaUniqueID': arenaUniqueID}))
        LOG_DEBUG('endbattle: battle results window opened manually')
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Key handling
# ============================================================
def _keyTick():
    global _KEY_PRESSED

    try:
        if _active() and _CTX is not None and not _ENDING:
            try:
                isDown = _isKeyDown(END_KEY)
            except Exception:
                isDown = False

            if isDown and not _KEY_PRESSED:
                _KEY_PRESSED = True
                LOG_DEBUG('[ENDBATTLE] key K pressed -> end battle')
                endBattle()
            elif not isDown and _KEY_PRESSED:
                _KEY_PRESSED = False
        else:
            try:
                if not _isKeyDown(END_KEY):
                    _KEY_PRESSED = False
            except Exception:
                _KEY_PRESSED = False
    except Exception:
        LOG_CURRENT_EXCEPTION()

    BigWorld.callback(0.05, _keyTick)


_KEY_HOOKED = False


def _patchKeyHandler():
    """K через Avatar.handleKey - тот же путь, по которому идут WASD."""
    global _KEY_HOOKED
    if _KEY_HOOKED:
        return
    try:
        import Avatar as _A
        cls = _A.PlayerAvatar
        origHandleKey = cls.handleKey

        def handleKey(self, isDown, key, mods, orig=origHandleKey):
            try:
                if isDown and key == END_KEY and _CTX is not None and not _ENDING and _active():
                    LOG_DEBUG('[ENDBATTLE] key K pressed (handleKey) -> end battle')
                    BigWorld.callback(0.0, endBattle)
                    return True
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return orig(self, isDown, key, mods)

        cls.handleKey = handleKey
        _KEY_HOOKED = True
        LOG_NOTE('[ENDBATTLE] Avatar.handleKey hooked, END_KEY=%s' % (END_KEY,))
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Кнопка "Выйти из боя" в меню (и "Выход" в окне после гибели)
# ============================================================
_LEAVE_HOOKED = False


def _patchLeaveArena():
    """Меню боя и окно после гибели зовут Avatar.leaveArena() - это команда серверу, которого в офлайне нет: аватар
    переходил в "смену сущности", бой зависал и кнопка не работала. Теперь выход из боя завершает его обычным путём
    (экран результатов -> ангар). Сам endBattle() вызывает родной leaveArena (флаг _ENDING)."""
    global _LEAVE_HOOKED
    if _LEAVE_HOOKED:
        return
    try:
        import Avatar as _A
        cls = _A.PlayerAvatar
        origLeave = cls.leaveArena

        def leaveArena(self, *args, **kwargs):
            try:
                if _CTX is not None and not _ENDING and not _CTX.get('ended') and _active():
                    LOG_DEBUG('[ENDBATTLE] leaveArena from menu -> end battle')
                    BigWorld.callback(0.0, endBattle)
                    return
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return origLeave(self, *args, **kwargs)

        cls.leaveArena = leaveArena
        _LEAVE_HOOKED = True
        LOG_NOTE('[ENDBATTLE] Avatar.leaveArena hooked (menu "leave battle" works offline)')
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

    _patchShooting()
    _patchResultsCache()
    _patchKeyHandler()
    _patchLeaveArena()
    BigWorld.callback(0.1, _keyTick)
    LOG_NOTE('[ENDBATTLE] registered (press K in battle to finish it)')
