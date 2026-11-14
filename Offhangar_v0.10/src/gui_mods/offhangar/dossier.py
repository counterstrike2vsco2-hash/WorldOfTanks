# -*- coding: utf-8 -*-
"""Офлайн-досье: вкладка "Достижения" (сводка, награды, статистика, техника).

Досье хранится в кэше мода (как остальные офлайн-данные) и обновляется после каждого боя теми же
функциями, что использует настоящий сервер (dossiers2.custom.battle_results_processors).

  * досье аккаунта отдаётся клиенту в stats['dossier'];
  * досье машин - через CMD_SYNC_DOSSIERS (requests.syncDossiers).
"""
import time

import BigWorld

from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE

_CACHE_NAME = 'dossiers'
_REV_BASE = 1000
_STATE = None


# ============================================================
# Хранилище
# ============================================================
def _load():
    global _STATE
    if _STATE is not None:
        return _STATE
    st = None
    try:
        from gui.mods.offhangar import data as off_data
        st = off_data._loadOfflineCache(_CACHE_NAME)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if not isinstance(st, dict):
        st = {}
    st.setdefault('account', '')
    st.setdefault('vehicles', {})
    st.setdefault('rev', _REV_BASE)
    _STATE = st
    return st


def _save():
    try:
        from gui.mods.offhangar import data as off_data
        off_data._saveOfflineCache(_CACHE_NAME, _load())
    except Exception:
        LOG_CURRENT_EXCEPTION()


def getAccountCompDescr():
    return _load()['account']


def getRevision():
    return _load()['rev']


def getVehicleList():
    """[(vehTypeCD, changeTime, compDescr)] - формат, который ждёт DossierCache."""
    return [(cd, t, d) for cd, (t, d) in _load()['vehicles'].items()]


# ============================================================
# Обновление после боя
# ============================================================
def _ensureInit():
    import dossiers2
    try:
        from dossiers2.custom.cache import getCache
        getCache()
    except Exception:
        dossiers2.init()
    return dossiers2


def _recordId(block, name):
    from dossiers2.custom import records
    return records.RECORD_DB_IDS.get((block, name))


def computeAchievements(stats, level, enemyHealthTotal):
    """Награды (медали), которые можно честно посчитать по нашей статистике боя. Возвращает список DB-идентификаторов."""
    names = []
    kills = int(stats.get('kills', 0))
    shots = int(stats.get('shots', 0))
    hits = int(stats.get('directHits', 0))
    dmg = int(stats.get('damageDealt', 0))

    if kills >= 6:
        names.append('warrior')
    if level >= 5:
        if 8 <= kills <= 9:
            names.append('medalRadleyWalters')
        elif 10 <= kills <= 13:
            names.append('medalLafayettePool')
    if kills >= 14:
        names.append('heroesOfRassenay')
    if shots >= 10 and dmg >= 1000 and float(hits) / max(1, shots) >= 0.85:
        names.append('sniper')
    if dmg >= 1000 and enemyHealthTotal > 0 and float(dmg) / enemyHealthTotal >= 0.2 and kills >= 1:
        names.append('mainGun')

    ids = []
    for n in names:
        rid = _recordId('achievements', n)
        if rid is not None:
            ids.append(rid)
    return ids


def _blankResults():
    res = {}
    for k in ('shots', 'directHits', 'spotted', 'damageDealt', 'damageReceived', 'capturePoints',
              'droppedCapturePoints', 'kills', 'damageAssistedTrack', 'damageAssistedRadio',
              'directHitsReceived', 'noDamageDirectHitsReceived', 'piercingsReceived',
              'explosionHitsReceived', 'explosionHits', 'piercings', 'potentialDamageReceived',
              'damageBlockedByArmor', 'lifeTime', 'mileage', 'originalXP', 'markOfMastery',
              'movingAvgDamage', 'marksOnGun', 'killerID'):
        res[k] = 0
    res.update({'kill_list': [], 'achievements': [], 'protoAchievements': [], 'destroyedObjects': {},
                'series': {}})
    return res


def recordBattle(personal, common, ctx, data):
    """Вносит результаты боя в досье аккаунта и машины. Возвращает {'dossier': compDescr} для пуша клиенту."""
    try:
        d2 = _ensureInit()
        from constants import ARENA_BONUS_TYPE
        st = _load()
        vehCD = int(data.get('vehTypeCD') or 0)
        if not vehCD:
            return {}

        results = _blankResults()
        for k in list(results.keys()):
            if k in personal and isinstance(results[k], (int, long, float)):
                results[k] = personal[k]
        results['bonusType'] = common.get('bonusType', ARENA_BONUS_TYPE.REGULAR)
        results['winnerTeam'] = common.get('winnerTeam', 0)
        results['team'] = 1
        results['killerID'] = int(data.get('killerID') or 0) if not data.get('playerAlive', True) else 0
        results['lifeTime'] = int(common.get('duration', 0))
        results['originalXP'] = int(data.get('xp', 0))
        results['achievements'] = list(data.get('achievements', []))
        results['spotted'] = int(ctx['stats'].get('kills', 0))

        kill_list = []
        for vid in ctx.get('killed', ()):
            info = ctx['enemies'].get(vid)
            if info:
                kill_list.append((vid, int(info.get('compDescr', 0)), 0))
        results['kill_list'] = kill_list

        dossierXP = int(data.get('xp', 0))

        from dossiers2.custom.battle_results_processors import updateVehicleDossier, updateAccountDossier

        vehDescr = d2.getVehicleDossierDescr(st['vehicles'].get(vehCD, (0, ''))[1])
        try:
            old = vehDescr['achievements']['movingAvgDamage']
            results['movingAvgDamage'] = int(old * 0.9 + results['damageDealt'] * 0.1)
        except Exception:
            pass
        updateVehicleDossier(vehDescr, results, dossierXP, vehCD)
        vehComp = vehDescr.makeCompDescr()

        accDescr = d2.getAccountDossierDescr(st['account'])
        updateAccountDossier(int(data.get('dbid', 1)), accDescr, results, dossierXP, vehCD, vehDescr)
        accComp = accDescr.makeCompDescr()

        st['vehicles'][vehCD] = (int(time.time()), vehComp)
        st['account'] = accComp
        st['rev'] = int(st['rev']) + 1
        _save()
        LOG_NOTE('[DOSSIER] battle recorded: veh=%s account=%d bytes, vehicle=%d bytes, rev=%s, medals=%s' % (
            vehCD, len(accComp), len(vehComp), st['rev'], results['achievements']))
        return {'dossier': accComp}
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return {}


def resyncClient():
    """Просим клиент заново запросить досье машин (CMD_SYNC_DOSSIERS) - обновится вкладка 'Техника'."""
    try:
        acc = BigWorld.player()
        cache = getattr(acc, 'dossierCache', None)
        if cache is not None:
            cache.resynchronize()
            LOG_DEBUG('[DOSSIER] client dossier resync requested')
    except Exception:
        LOG_CURRENT_EXCEPTION()
