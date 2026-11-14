# -*- coding: utf-8 -*-
"""Повреждение модулей и экипажа, пожар, ремонт, расходники и оборудование (по мотивам офлайн-мода 0.8.2).

Работает для игрока и ботов. Точка входа - shooting._applyDamage:
  * пробитие -> onPenetration(): по зоне попадания (башня / корпус спереди, сбоку, сзади) бросаются спасброски
    модулей (двигатель, боеукладка, топливо, рация, башенный привод, прибор наблюдения, орудие) и членов экипажа;
  * любое попадание -> onHit(): шанс повредить гусеницу со стороны стрелявшего.
Модуль может быть "повреждён" (critical, жёлтый), "уничтожен" (destroyed, красный) - экипаж чинит уничтоженный
модуль до половины прочности (авторемонт), ремкомплект чинит полностью. Член экипажа выбывает (аптечка возвращает).
Следствия (см. _recompute): разбитая гусеница или двигатель - машина стоит; повреждённый двигатель - слабее разгон;
орудие разбито - стрелять нельзя; привод башни - башня крутится медленнее/не крутится; приборы наблюдения -
меньше радиус обзора; заряжающий - дольше перезарядка; наводчик - медленнее башня; механик-водитель - хуже ход;
командир - меньше обзор. Разбитая боеукладка - взрыв (шанс растёт с остатком снарядов). Пробитое топливо/двигатель
могут поджечь машину; огонь жжёт прочность, тушится огнетушителем или гаснет сам.
Оборудование игрока: рамер, вентиляция, оптика, бинокль, турбонаддув/форсаж, набор инструментов и др. - см. _OPT_RULES.
Расходники игрока: ремкомплект, аптечка, огнетушитель (клавиши 4-6, клик по слоту и по иконке повреждённого модуля).
Выключатели: moduledamage=0 / crewdamage=0 / fire=0 / equipment=0 в offline_user.txt. Константы - ниже.
"""
import math
import random

import BigWorld

from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE

ENABLED = True
TICK = 0.25                  # период обработки (пожар, авторемонт), с

# --- шансы попадания по модулю/члену экипажа при пробитии в его зоне (как в игре: 0.45 / 0.27 / 0.33)
CHANCE = {'engine': 0.45, 'ammoBay': 0.27, 'fuelTank': 0.27, 'radio': 0.27, 'turretRotator': 0.27,
          'surveyingDevice': 0.27, 'gun': 0.27, 'leftTrack': 0.45, 'rightTrack': 0.45}
CREW_CHANCE = 0.33
AMMOBAY_RELOAD_CRITICAL = 1.2   # множитель времени перезарядки: боеукладка повреждена (жёлтая)
AMMOBAY_RELOAD_DESTROYED = 1.4  # ... и разбита (красная; раньше штрафа у разбитой не было вообще)
CRIT_SCALE_VS_PLAYER = 0.3      # боты бьют по ИГРОКУ: все шансы критов (модули, экипаж, гусеницы) умножаются на это (1.0 - как у остальных)
CRIT_COOLDOWN_VS_PLAYER = 12.0  # после нового крита по игроку столько секунд боты не наносят ему новых критов (0 - без паузы)
MAX_CRITS_VS_PLAYER = 1      # от одного пробития по ИГРОКУ - не больше стольких критов
MAX_CRITS_PER_SHOT = 3       # сколько критов максимум от одного пробития
TRACK_HIT_CHANCE = 0.12      # шанс повредить гусеницу при любом попадании сбоку/спереди/сзади
HE_TRACK_HIT_CHANCE = 0.2
TURRET_ZONE_SHARE = 0.45     # доля пробитий, пришедшихся в башню
AMMO_DETONATE_BASE = 0.3     # взрыв разбитой боеукладки: BASE + (1-BASE) * доля оставшихся снарядов
FIRE_SECONDS = 15.0          # сколько горит машина, если не потушить
FIRE_DPS_FRACTION = 0.014    # потеря прочности в секунду при пожаре (доля максимальной)
FIRE_FROM_FUEL_HOLE = 0.15   # шанс пожара при простом повреждении топливного бака
ENGINE_FIRE_CHANCE = 0.15    # шанс пожара при уничтожении двигателя
MIN_FIRE_DAMAGE = 21.0       # минимальный урон модулю, способный зажечь
REPAIR_TO = 0.5              # до какой доли прочности экипаж чинит уничтоженный модуль
# базовое время ремонта уничтоженного модуля (с, при экипаже 100%)
REPAIR_SECONDS = {'engine': 12.0, 'ammoBay': 12.0, 'radio': 8.0, 'turretRotator': 10.0, 'surveyingDevice': 8.0,
                  'gun': 12.0, 'leftTrack': 10.0, 'rightTrack': 10.0}
# запасная прочность модулей, если не удалось прочитать из описания машины
FALLBACK_HP = {'engine': 150.0, 'ammoBay': 120.0, 'fuelTank': 120.0, 'radio': 100.0, 'turretRotator': 120.0,
               'surveyingDevice': 100.0, 'gun': 200.0, 'leftTrack': 250.0, 'rightTrack': 250.0}

DEVICES = ('engine', 'ammoBay', 'fuelTank', 'radio', 'turretRotator', 'surveyingDevice', 'gun',
           'leftTrack', 'rightTrack')
TURRET_DEVICES = ('turretRotator', 'surveyingDevice', 'gun')

_ACTIVE = False
_TOKEN = 0
_ON_MODULES = True
_ON_CREW = True
_ON_FIRE = True
_ON_EQUIP = True
_STATE = {}                  # vehicleID -> _VS
_PLAYER = {'eq': [], 'skill': 100.0, 'cmdSkill': 100.0, 'stim': 0.0, 'bia': 0.0, 'power': 1.0, 'turretRot': 1.0,
           'fireFactor': 1.0, 'stereoscope': None, 'rpm': {'on': False, 'power': 1.1, 'loss': 1.5, 'e': None}, 'perks': set()}
_LAST_PRESS = {}


# ------------------------------------------------------------
# Состояние машины
# ------------------------------------------------------------
class _VS(object):
    def __init__(self, vehicle):
        self.vid = vehicle.id
        self.td = getattr(vehicle, 'typeDescriptor', None)
        self.isPlayer = _isPlayer(vehicle)
        self.maxHP = {}
        self.hp = {}
        self.destroyed = set()
        self.announced = {}           # имя -> последнее озвученное состояние
        self.roster = _roster(self.td)
        self.roles = _crewRoles(self.td, self.roster)
        self.ko = set()
        self.fireUntil = 0.0
        self.fireBy = 0
        self.rpmWear = 0.0            # сколько прочности двигатель потерял от снятого ограничителя оборотов (не считается повреждением)
        self.fireStarted = False
        self.factors = None
        self.dirty = True
        self.repairUI = {}
        self.lastTick = BigWorld.time()
        self.stillSince = None
        self.zero = False
        self.regenFrac = {}
        self.critUntil = 0.0          # до какого момента новые криты по этой машине запрещены (только для игрока)


def _isPlayer(vehicle):
    try:
        return getattr(BigWorld.player(), 'playerVehicleID', -1) == vehicle.id
    except Exception:
        return False


def _roster(td):
    """Имена членов экипажа: commander, driver, gunner1, loader1, radioman1..."""
    names = []
    try:
        counters = {'gunner': 1, 'loader': 1, 'radioman': 1}
        for roles in getattr(getattr(td, 'type', None), 'crewRoles', []):
            main = roles[0]
            if main in counters:
                names.append(main + str(counters[main]))
                counters[main] += 1
            else:
                names.append(main)
    except Exception:
        pass
    if not names:
        names = ['commander', 'driver', 'gunner1', 'loader1', 'radioman1']
    return names


def _crewRoles(td, roster):
    """Имя члена экипажа -> множество ролей, которые он закрывает."""
    res = {}
    try:
        crewRoles = list(getattr(getattr(td, 'type', None), 'crewRoles', []))
    except Exception:
        crewRoles = []
    for i, name in enumerate(roster):
        if i < len(crewRoles):
            res[name] = set(crewRoles[i])
        else:
            res[name] = set([_baseRole(name)])
    return res


def _baseRole(name):
    return name.rstrip('0123456789')


def _get(vehicle, create=True):
    st = _STATE.get(vehicle.id)
    if st is None and create:
        st = _VS(vehicle)
        _STATE[vehicle.id] = st
    return st


# ------------------------------------------------------------
# Прочность модулей
# ------------------------------------------------------------
def _num(v):
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, long, float)):
            return float(v)
        mh = getattr(v, 'maxHealth', None)
        if mh is not None:
            return float(mh)
        if isinstance(v, dict):
            for k in ('maxHealth', 'health'):
                if k in v:
                    return float(v[k])
        if isinstance(v, (tuple, list)) and v:
            return _num(v[0])
    except Exception:
        pass
    return None


_HP_DIAG = [0]


def _paramsOf(td, name):
    """Словарь параметров модуля из описания машины 0.9.0: {'maxHealth', 'maxRegenHealth', ...} или None."""
    try:
        if name == 'engine':
            return td.engine
        if name == 'fuelTank':
            return td.fuelTank
        if name == 'radio':
            return td.radio
        if name == 'gun':
            return td.gun
        if name in ('leftTrack', 'rightTrack'):
            return td.chassis
        if name == 'ammoBay':
            return td.hull['ammoBayHealth']
        if name == 'turretRotator':
            return td.turret['turretRotatorHealth']
        if name == 'surveyingDevice':
            return td.turret['surveyingDeviceHealth']
    except Exception:
        pass
    return None


_HEALTH_FACTOR = {'engine': 'engineHealthFactor', 'ammoBay': 'ammoBayHealthFactor', 'fuelTank': 'fuelTankHealthFactor',
                  'leftTrack': 'chassisHealthFactor', 'rightTrack': 'chassisHealthFactor'}


def _misc(st):
    try:
        return st.td.miscAttrs or {}
    except Exception:
        return {}


def _probeMaxHP(td, name):
    """Читает прочность модуля из описания машины (vehicles.py 0.9.0), иначе None."""
    p = _paramsOf(td, name)
    try:
        v = _num(p['maxHealth'])
        if v:
            return v
    except Exception:
        pass
    try:
        ex = getattr(td, 'extrasDict', {}).get(name + 'Health')
        for attr in ('maxHealth', 'health'):
            v = _num(getattr(ex, attr, None))
            if v:
                return v
    except Exception:
        pass
    return None


def _maxHP(st, name):
    v = st.maxHP.get(name)
    if v is None:
        v = _probeMaxHP(st.td, name) if st.td is not None else None
        src = 'descriptor'
        if not v or v < 20.0 or v > 5000.0:
            v = FALLBACK_HP.get(name, 150.0)
            src = 'fallback'
        else:
            try:
                v *= float(_misc(st).get(_HEALTH_FACTOR.get(name, ''), 1.0))      # оборудование (усиленные узлы и т.п.)
            except Exception:
                pass
        st.maxHP[name] = v
        try:
            p = _paramsOf(st.td, name)
            regen = float(p['maxRegenHealth'])
            if regen > 0.0:
                st.regenFrac[name] = max(0.1, min(1.0, regen / float(p['maxHealth'])))
        except Exception:
            pass
        if _HP_DIAG[0] < 12:
            _HP_DIAG[0] += 1
            LOG_DEBUG('[DEV] %s max health %.0f (%s, repair up to %.0f%%)' % (
                name, v, src, 100.0 * st.regenFrac.get(name, REPAIR_TO)))
    return v


def _repairCap(st, name):
    """До какой прочности экипаж чинит уничтоженный модуль (maxRegenHealth из описания)."""
    mx = _maxHP(st, name)
    cap = mx * st.regenFrac.get(name, REPAIR_TO)
    if name == 'engine' and st.rpmWear > 0.0:
        cap = min(cap, max(0.0, mx - st.rpmWear))     # износ от снятого ограничителя оборотов экипаж не чинит - только ремкомплект
    return cap


RPM_WEAR_CRITICAL = 0.5        # двигатель на повышенных оборотах становится повреждённым, когда прочность упала ниже этой доли


def _hp(st, name):
    return st.hp.get(name, _maxHP(st, name))


def _state(st, name):
    if name in st.destroyed:
        return 'destroyed'
    mx = _maxHP(st, name)
    hp = _hp(st, name)
    if name == 'engine' and st.rpmWear > 0.0:
        # Износ от снятого ограничителя: двигатель становится "повреждённым" (жёлтым), только когда износ достиг RPM_WEAR_CRITICAL
        # от прочности, а не от первой же потери HP. Повреждения от снарядов считаются отдельно.
        st.rpmWear = min(st.rpmWear, max(0.0, mx - hp))
        shellDamage = (mx - hp) - st.rpmWear
        if shellDamage > 0.5 or hp < mx * RPM_WEAR_CRITICAL:
            return 'critical'
        return 'normal'
    if hp < mx - 0.5:
        return 'critical'
    return 'normal'


# ------------------------------------------------------------
# Следствия (множители)
# ------------------------------------------------------------
def _skillFactor(eff):
    """Множитель времени/разброса от навыка: 1.0 при экипаже 100% (+10% командира)."""
    return 1.05 / (0.5 + 0.005 * max(0.0, eff))


def _roleDead(st, role):
    """Роль не закрывает ни один живой член экипажа."""
    covered = False
    for name in st.roster:
        if role in st.roles.get(name, ()):
            covered = True
            if name not in st.ko:
                return False
    return covered


def _crewSkills(st):
    """Навык экипажа и командира (%): уровни из ангара + вентиляция/стимуляторы/боевое братство (td.miscAttrs)."""
    if not st.isPlayer:
        return 100.0, 100.0
    try:
        inc = float(_misc(st).get('crewLevelIncrease', 0.0) or 0.0)
    except Exception:
        inc = 0.0
    inc += _PLAYER.get('stim', 0.0) + _PLAYER.get('bia', 0.0)
    if not _ON_EQUIP:
        inc = _PLAYER.get('bia', 0.0)
    return _PLAYER['skill'] + inc, _PLAYER['cmdSkill'] + inc


def _enginePower(st):
    """Множитель мощности двигателя: газолин (всегда) и снятый ограничитель оборотов (пока включён)."""
    if not st.isPlayer or not _ON_EQUIP:
        return 1.0
    k = _PLAYER.get('power', 1.0)
    if _PLAYER['rpm']['on']:
        k *= _PLAYER['rpm']['power']
    return k


PERKS_ENABLED = True


def hasPerk(name):
    """Есть ли у экипажа игрока навык (по части имени, без регистра: 'sixthsense', 'camouflage' ...)."""
    if not (_ACTIVE and PERKS_ENABLED):
        return False
    n = name.lower()
    for p in _PLAYER.get('perks', ()):
        if n in p:
            return True
    return False


def isPlayerVid(vid):
    st = _STATE.get(vid)
    return st is not None and st.isPlayer


def _hpFrac(st):
    try:
        v = BigWorld.entity(st.vid)
        return max(0.0, min(1.0, float(v.health) / float(v.typeDescriptor.maxHealth)))
    except Exception:
        return 1.0


def _applyPerks(st, f):
    """Навыки экипажа игрока (приближённо к игре 0.9.0)."""
    if not (PERKS_ENABLED and _PLAYER.get('perks')):
        return
    if hasPerk('desperado'):
        f['reload'] /= 1.0 + 0.2 * (1.0 - _hpFrac(st))      # чем меньше HP, тем быстрее перезарядка (до +20%)
    if hasPerk('eagleeye'):
        f['view'] *= 1.04                                     # орлиный глаз
    if hasPerk('smoothdriving'):
        f['disp'] *= 0.95
    if hasPerk('smoothturret'):
        f['disp'] *= 0.95
    if hasPerk('sniper'):
        f['disp'] *= 0.97
    if hasPerk('gunsmith'):
        f['disp'] *= 0.95
    if hasPerk('virtuoso'):
        f['rot'] *= 1.08


def _recompute(st):
    f = {'move': True, 'fwd': 1.0, 'back': 1.0, 'accel': 1.0, 'rot': 1.0, 'turret': 1.0, 'reload': 1.0,
         'view': 1.0, 'gun': True, 'aim': 1.0, 'disp': 1.0}
    isP = st.isPlayer
    skill, cmd = _crewSkills(st)
    m = _misc(st)

    # --- модули
    if 'engine' in st.destroyed:
        f['move'] = False
    elif _state(st, 'engine') == 'critical':
        f['accel'] *= 0.5
        f['fwd'] *= 0.8
    if 'leftTrack' in st.destroyed or 'rightTrack' in st.destroyed:
        f['move'] = False
    if 'gun' in st.destroyed:
        f['gun'] = False
    elif _state(st, 'gun') == 'critical':
        f['disp'] *= 1.25
        f['aim'] *= 1.25
    if 'turretRotator' in st.destroyed:
        f['turret'] = 0.0
    elif _state(st, 'turretRotator') == 'critical':
        f['turret'] *= 0.5
    if 'surveyingDevice' in st.destroyed:
        f['view'] *= 0.5
    elif _state(st, 'surveyingDevice') == 'critical':
        f['view'] *= 0.75
    if 'ammoBay' in st.destroyed:
        f['reload'] *= AMMOBAY_RELOAD_DESTROYED
    elif _state(st, 'ammoBay') == 'critical':
        f['reload'] *= AMMOBAY_RELOAD_CRITICAL

    # --- экипаж (навык роли = 0, если все, кто её закрывает, выбыли; командир добавляет 10%)
    cmdEff = 0.0 if _roleDead(st, 'commander') else cmd
    loaderEff = (0.0 if _roleDead(st, 'loader') else skill) + 0.1 * cmdEff
    gunnerEff = (0.0 if _roleDead(st, 'gunner') else skill) + 0.1 * cmdEff
    f['reload'] *= _skillFactor(loaderEff)
    f['aim'] *= _skillFactor(gunnerEff)
    f['disp'] *= _skillFactor(gunnerEff)
    if _roleDead(st, 'gunner'):
        f['turret'] *= 0.6
    if _roleDead(st, 'driver'):
        f['fwd'] *= 0.8
        f['back'] *= 0.8
        f['accel'] *= 0.7
        f['rot'] *= 0.7
    if _roleDead(st, 'commander'):
        f['view'] *= 0.8

    # --- оборудование и стимуляторы (td.miscAttrs уже содержит эффекты установленного оборудования)
    try:
        f['reload'] *= float(m.get('gunReloadTimeFactor', 1.0) or 1.0)       # досылатель
        f['aim'] *= float(m.get('gunAimingTimeFactor', 1.0) or 1.0)          # улучшенные приводы наводки
        f['view'] *= float(m.get('circularVisionRadiusFactor', 1.0) or 1.0)  # просветлённая оптика
    except Exception:
        pass
    if isP and _ON_EQUIP:
        power = _enginePower(st)
        f['accel'] *= power
        f['fwd'] *= 1.0 + (power - 1.0) * 0.5
        f['back'] *= 1.0 + (power - 1.0) * 0.5
        f['rot'] *= 1.0 + (power - 1.0) * 0.5
        f['turret'] *= _PLAYER.get('turretRot', 1.0)
    if isP:
        _applyPerks(st, f)
    oldReload = st.factors['reload'] if st.factors else 1.0
    st.factors = f
    st.dirty = False
    if abs(f['reload'] - oldReload) > 1e-3:
        # штраф/бонус к перезарядке действует сразу и на ту, что уже идёт (остаток пересчитывается)
        try:
            from gui.mods.offhangar import shooting
            shooting.rescaleReload(st.vid, oldReload, f['reload'])
        except Exception:
            LOG_CURRENT_EXCEPTION()
    if isP:
        try:
            key = tuple(sorted((k, round(float(v), 3)) for k, v in f.items()))
            if getattr(st, '_loggedKey', None) != key:
                st._loggedKey = key
                LOG_DEBUG('[DEV] player factors: ' + ' '.join('%s=%s' % (k, round(float(v), 2)) for k, v in sorted(f.items())))
        except Exception:
            pass
    return f


def getFactors(vid):
    """Множители для физики/башни/стрельбы/обзора; None - нет данных (всё как обычно)."""
    if not _ACTIVE:
        return None
    st = _STATE.get(vid)
    if st is None:
        return None
    if st.dirty or st.factors is None:
        return _recompute(st)
    return st.factors


def canShoot(vid):
    f = getFactors(vid)
    return True if f is None else f['gun']


def reloadFactor(vid):
    f = getFactors(vid)
    return 1.0 if f is None else f['reload']


def aimFactors(vid):
    """(множитель времени сведения, множитель разброса): экипаж, повреждённое орудие, приводы наводки."""
    f = getFactors(vid)
    if f is None:
        return 1.0, 1.0
    return f['aim'], f['disp']


def viewFactor(vid):
    f = getFactors(vid)
    if f is None:
        return 1.0
    v = f['view']
    st = _STATE.get(vid)
    ster = _PLAYER.get('stereoscope')
    if st is not None and st.isPlayer and _ON_EQUIP and ster and st.stillSince is not None:
        if BigWorld.time() - st.stillSince >= ster[0]:
            v *= ster[1]                  # бинокулярный прибор: радиус обзора растёт, если машина стоит
    return v


# ------------------------------------------------------------
# Интерфейс (панель повреждений, голос)
# ------------------------------------------------------------
def _battleWindow():
    try:
        from gui.WindowsManager import g_windowsManager
        return g_windowsManager.battleWindow
    except Exception:
        return None


def _panel():
    bw = _battleWindow()
    return getattr(bw, 'damagePanel', None) if bw is not None else None


def _pushState(st, name, state):
    """Состояние модуля/члена экипажа на панель повреждений (только для игрока)."""
    if not st.isPlayer:
        return
    try:
        p = _panel()
        if p is not None:
            p.updateState(name, state)
        bw = _battleWindow()
        cp = getattr(bw, 'consumablesPanel', None) if bw is not None else None
        if cp is not None:
            cp.updateExpandedEquipmentSlot(name, state)     # открытая панель выбора модуля/члена экипажа
    except Exception:
        LOG_DEBUG('[DEV] updateState(%s, %s) failed' % (name, state))


def _voice(st, name, key, crew=False):
    """Реплика экипажа о модуле (имя события берётся из extrasDict клиента)."""
    if not st.isPlayer:
        return None
    try:
        td = BigWorld.player().vehicleTypeDescriptor
        ex = td.extrasDict.get(name + 'Health')
        snd = getattr(ex, 'sounds', {}).get(key)
        return snd
    except Exception:
        return None


def _say(snds):
    if not snds:
        return
    try:
        from gui.mods.offhangar import feedback
        feedback._say(BigWorld.player(), snds[0])
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _voiceRank(key):
    return {'destroyed': 3, 'critical': 2, 'functional': 1, 'fixed': 0}.get(key, 0)


# ------------------------------------------------------------
# Удар по модулю / члену экипажа
# ------------------------------------------------------------
def _who(vid):
    """Подпись для лога: игрок / союзник / враг (по команде бота)."""
    try:
        from gui.mods.offhangar import bots
        e = BigWorld.entity(vid)
        if _isPlayer(e):
            return 'player'
        return 'ally' if bots.getTeam(e) == bots.TEAM_ALLY else 'enemy'
    except Exception:
        return '?'


def _setState(st, name, hp, voices, shooter=0, reason=''):
    """Меняет прочность модуля, обновляет панель. voices - список (приоритет, событие)."""
    mx = _maxHP(st, name)
    hp = max(0.0, min(mx, hp))
    st.hp[name] = hp
    if hp <= 0.0:
        st.destroyed.add(name)
    st.dirty = True
    cur = _state(st, name)
    _pushState(st, name, cur)
    last = st.announced.get(name, 'normal')
    rank = {'normal': 0, 'critical': 1, 'destroyed': 2}
    if rank[cur] > rank.get(last, 0):
        st.announced[name] = cur
        snd = _voice(st, name, cur)
        if snd:
            voices.append((_voiceRank(cur), snd))
    elif cur == 'normal':
        st.announced[name] = 'normal'
    LOG_DEBUG('[DEV] vehicle %s (%s) %s -> %s (hp %.0f/%.0f)%s by %s' % (
        st.vid, _who(st.vid), name, cur, hp, mx, (' ' + reason) if reason else '', shooter or '-'))
    return cur


def _knockOut(st, name, voices):
    if name in st.ko:
        return False
    st.ko.add(name)
    st.dirty = True
    _pushState(st, name, 'destroyed')
    snd = _voice(st, name, 'destroyed')
    if snd:
        voices.append((4, snd))
    LOG_DEBUG('[DEV] vehicle %s crew %s knocked out' % (st.vid, name))
    return True


def _bearing(shooter, target):
    """Откуда стреляли: угол между носом цели и направлением на стрелка (рад, -pi..pi, + = справа)."""
    try:
        from gui.mods.offhangar import bots, battle
        sp = bots._posOf(shooter)
        tp = bots._posOf(target)
        phys = battle._simplePhysicsByVehID.get(target.id)
        yaw = phys.yaw if phys is not None else target.model.yaw
        dx, dz = sp.x - tp.x, sp.z - tp.z
        ang = math.atan2(dx, dz) - yaw
        while ang > math.pi:
            ang -= 2.0 * math.pi
        while ang < -math.pi:
            ang += 2.0 * math.pi
        return ang
    except Exception:
        return random.uniform(-math.pi, math.pi)


def _candidates(st, ang):
    """Что может задеть снаряд, пробивший зону: список (имя, это экипаж?)."""
    crew = lambda role: [n for n in st.roster if role in (st.roles.get(n) or ()) and _baseRole(n) == role]
    a = abs(ang)
    cands = []
    if random.random() < TURRET_ZONE_SHARE:
        cands += [(n, True) for n in crew('gunner')]
        cands += [(n, True) for n in crew('loader')]
        cands += [(n, True) for n in crew('commander')]
        cands += [('turretRotator', False), ('surveyingDevice', False), ('gun', False), ('ammoBay', False)]
        if random.random() < 0.5:
            cands += [('radio', False)]
    elif a < math.radians(50.0):
        cands += [(n, True) for n in crew('driver')]
        cands += [(n, True) for n in crew('radioman')]
        cands += [('ammoBay', False), ('engine', False), ('radio', False)]
    elif a > math.radians(130.0):
        cands += [('engine', False), ('fuelTank', False), ('ammoBay', False)]
        cands += [(n, True) for n in crew('loader')]
    else:
        cands += [('fuelTank', False), ('ammoBay', False), ('engine', False)]
        cands += [(n, True) for n in crew('driver')]
        cands += [(n, True) for n in crew('loader')]
    random.shuffle(cands)
    return cands


def _crewEvade(st):
    """Шанс поразить члена экипажа: противоосколочный подкладной слой снижает его (crewChanceToHitFactor)."""
    try:
        return max(0.1, float(_misc(st).get('crewChanceToHitFactor', 1.0) or 1.0))
    except Exception:
        return 1.0


def _deviceDamage(shellDescr):
    try:
        d = shellDescr['damage']
        v = float(d[1]) if len(d) > 1 else float(d[0])
        if v <= 0:
            v = float(d[0])
        return v
    except Exception:
        return 100.0


def _damageModule(st, name, dmg, voices, shooter, reason):
    """Урон модулю. Возвращает True, если модуль только что уничтожен."""
    if not _ON_MODULES:
        return False
    was = name in st.destroyed
    hp = _hp(st, name) - dmg * random.uniform(0.8, 1.0)
    cur = _setState(st, name, hp, voices, shooter, reason)
    if cur == 'destroyed' and not was:
        return True
    return False


def onPenetration(shooterID, target, shellDescr, hitAngleCos, dmg):
    """Вызывается из shooting._applyDamage после пробития (машина ещё жива). True - машина погибла от крита."""
    if not _ACTIVE:
        return False
    try:
        if target.health <= 0:
            return False
        st = _get(target)
        shooter = BigWorld.entity(shooterID)
        ang = _bearing(shooter, target) if shooter is not None else 0.0
        devDmg = _deviceDamage(shellDescr)
        voices = []
        crits = 0
        killed = False
        critScale = 1.0
        if st.isPlayer:
            critScale = 0.0 if BigWorld.time() < st.critUntil else CRIT_SCALE_VS_PLAYER
        maxCrits = min(MAX_CRITS_PER_SHOT, MAX_CRITS_VS_PLAYER) if st.isPlayer else MAX_CRITS_PER_SHOT
        for name, isCrew in _candidates(st, ang):
            if crits >= maxCrits:
                break
            if isCrew:
                if not _ON_CREW or name in st.ko:
                    continue
                if random.random() < CREW_CHANCE * _crewEvade(st) * critScale:
                    if _knockOut(st, name, voices):
                        crits += 1
                continue
            if not _ON_MODULES:
                continue
            if random.random() >= CHANCE.get(name, 0.27) * critScale:
                continue
            crits += 1
            if _hitModule(st, target, name, devDmg, voices, shooterID, 'penetration'):
                killed = True
                break
        if crits and st.isPlayer and CRIT_COOLDOWN_VS_PLAYER > 0.0:
            st.critUntil = BigWorld.time() + CRIT_COOLDOWN_VS_PLAYER
        if not killed and _ON_CREW and len(st.ko) >= len(st.roster):
            LOG_DEBUG('[DEV] vehicle %s: whole crew knocked out' % st.vid)
            killed = True
            _killTarget(target, shooterID, 'crew')
        _flushVoices(st, voices)
        if st.dirty:
            _recompute(st)
        if killed:
            return True
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return False


def _hitModule(st, target, name, devDmg, voices, shooterID, reason):
    """Результат удара по одному модулю (взрыв боеукладки, пожар). True - машина погибла."""
    prevDestroyed = name in st.destroyed
    newly = _damageModule(st, name, devDmg, voices, shooterID, reason)
    if name == 'ammoBay' and newly:
        frac = _ammoFraction(target)
        if random.random() < AMMO_DETONATE_BASE + (1.0 - AMMO_DETONATE_BASE) * frac:
            LOG_DEBUG('[DEV] vehicle %s: AMMO RACK DETONATION (ammo %.0f%%)' % (st.vid, frac * 100.0))
            _killTarget(target, shooterID, 'ammo')
            return True
    if name == 'fuelTank' and _ON_FIRE:
        if newly:
            _ignite(st, target, shooterID, 'fuel tank destroyed', voices)
        elif devDmg >= MIN_FIRE_DAMAGE and random.random() < FIRE_FROM_FUEL_HOLE * (_PLAYER.get('fireFactor', 1.0) if st.isPlayer and _ON_EQUIP else 1.0):
            _ignite(st, target, shooterID, 'fuel tank holed', voices)
    if name == 'engine' and newly and _ON_FIRE:
        if devDmg >= MIN_FIRE_DAMAGE and random.random() < _engineFireChance(st):
            _ignite(st, target, shooterID, 'engine destroyed', voices)
    return False


def _engineFireChance(st):
    c = ENGINE_FIRE_CHANCE
    try:
        c = float(st.td.engine.get('fireStartingChance', c))
    except Exception:
        pass
    if st.isPlayer and _ON_EQUIP:
        c *= _PLAYER.get('fireFactor', 1.0)
    return c


def _ammoFraction(target):
    """Доля оставшихся снарядов от начального запаса."""
    try:
        from gui.mods.offhangar import shooting
        s = shooting._getState(target)
        left = sum(s['ammo'].values())
        full = float(target.typeDescriptor.gun.get('maxAmmo', 0) or 0)
        if full <= 0:
            full = max(left, 1)
        return max(0.0, min(1.0, left / full))
    except Exception:
        return 1.0


def onHit(shooterID, target, kind, shellDescr, hitAngleCos):
    """Любое попадание по корпусу (кроме рикошета): шанс повредить гусеницу."""
    if not _ACTIVE or not _ON_MODULES:
        return
    try:
        if target.health <= 0:
            return
        st = _get(target)
        shooter = BigWorld.entity(shooterID)
        ang = _bearing(shooter, target) if shooter is not None else 0.0
        he = False
        try:
            he = str(shellDescr.get('kind', '')).upper().startswith('HIGH_EXPLOSIVE')
        except Exception:
            pass
        trackChance = HE_TRACK_HIT_CHANCE if he else TRACK_HIT_CHANCE
        if st.isPlayer:
            if BigWorld.time() < st.critUntil:
                return
            trackChance *= CRIT_SCALE_VS_PLAYER
        if random.random() >= trackChance:
            return
        a = abs(ang)
        if a < math.radians(50.0) or a > math.radians(130.0):
            name = random.choice(('leftTrack', 'rightTrack'))
        else:
            name = 'rightTrack' if ang > 0 else 'leftTrack'
        voices = []
        _hitModule(st, target, name, _deviceDamage(shellDescr), voices, shooterID, 'hit')
        if st.isPlayer and CRIT_COOLDOWN_VS_PLAYER > 0.0:
            st.critUntil = BigWorld.time() + CRIT_COOLDOWN_VS_PLAYER
        _flushVoices(st, voices)
        if st.dirty:
            _recompute(st)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _flushVoices(st, voices):
    if not voices or not st.isPlayer:
        return
    voices.sort(key=lambda v: -v[0])
    _say([voices[0][1]])


# ------------------------------------------------------------
# Гибель от крита / огня
# ------------------------------------------------------------
def _killTarget(target, shooterID, reason):
    """Тот же путь, что и при обычной гибели от выстрела (shooting._applyDamage)."""
    try:
        if target.health <= 0:
            return
        try:
            from gui.mods.offhangar import battle as off_battle
            off_battle.freezeCorpse(target)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        prev = target.health
        target.health = 0
        try:
            target.set_health(prev)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            target.onHealthChanged(0, shooterID, 0)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            av = BigWorld.player()
            if getattr(av, 'playerVehicleID', None) == target.id:
                av.updateVehicleHealth(0, True)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            from gui.mods.offhangar import bots as off_bots
            off_bots.onVehicleKilled(target, shooterID)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        LOG_DEBUG('[DEV] vehicle %s destroyed (%s)' % (target.id, reason))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _burn(target, st, dmg, shooterID):
    """Потеря прочности от огня."""
    try:
        prev = target.health
        if prev <= 0:
            return
        newHealth = max(0, int(round(prev - dmg)))
        if newHealth >= prev:
            newHealth = prev - 1 if prev > 1 else 0
        if newHealth <= 0:
            _killTarget(target, shooterID, 'fire')
            return
        target.health = newHealth
        try:
            target.set_health(prev)
        except Exception:
            pass
        try:
            target.onHealthChanged(newHealth, shooterID, 0)
        except Exception:
            pass
        if st.isPlayer:
            try:
                BigWorld.player().updateVehicleHealth(newHealth, True)
            except Exception:
                pass
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ------------------------------------------------------------
# Пожар
# ------------------------------------------------------------
def _ignite(st, target, shooterID, reason, voices=None):
    if st.fireUntil > BigWorld.time():
        return
    # автоматический огнетушитель срабатывает сам
    if st.isPlayer:
        c = _findConsumable('extinguisher', auto=True)
        if c is not None:
            LOG_DEBUG('[DEV] auto extinguisher put out the fire at once')
            _consume(c)
            _voiceFire(False)
            return
    st.fireUntil = BigWorld.time() + FIRE_SECONDS
    st.fireBy = shooterID
    LOG_DEBUG('[DEV] vehicle %s on FIRE (%s)' % (st.vid, reason))
    if st.isPlayer:
        try:
            p = _panel()
            if p is not None:
                p.onFireInVehicle(True)
        except Exception:
            LOG_DEBUG('[DEV] onFireInVehicle failed')
        if voices is not None:
            voices.append((5, _fireSound(True)))


def _fireSound(started):
    key = 'critical' if started else 'fixed'
    try:
        td = BigWorld.player().vehicleTypeDescriptor
        snd = getattr(td.extrasDict.get('fire'), 'sounds', {}).get(key)
        if snd:
            return snd
    except Exception:
        pass
    return 'fire_started' if started else 'fire_stopped'


def _voiceFire(started):
    _say([_fireSound(started)])


def _extinguish(st, reason):
    if st.fireUntil <= 0.0:
        return
    st.fireUntil = 0.0
    LOG_DEBUG('[DEV] vehicle %s fire out (%s)' % (st.vid, reason))
    if st.isPlayer:
        try:
            p = _panel()
            if p is not None:
                p.onFireInVehicle(False)
        except Exception:
            pass
        _voiceFire(False)
    # топливный бак после пожара возвращается в строй
    if 'fuelTank' in st.destroyed:
        st.destroyed.discard('fuelTank')
        _maxHP(st, 'fuelTank')
        st.hp['fuelTank'] = max(_hp(st, 'fuelTank'), _repairCap(st, 'fuelTank'))
        st.dirty = True
        _pushState(st, 'fuelTank', _state(st, 'fuelTank'))


# ------------------------------------------------------------
# Периодический процесс: огонь, авторемонт, обзор
# ------------------------------------------------------------
def _repairSkillFactor(st):
    """Множитель времени ремонта: навык экипажа и ящик с инструментами (repairSpeedFactor)."""
    skill, _ = _crewSkills(st)
    f = (0.5 + 0.005 * 100.0) / (0.5 + 0.005 * skill)      # экипаж 100% -> 1.0
    if st.isPlayer and hasPerk('repair'):
        f /= 1.25                                             # навык «ремонт»
    try:
        k = float(_misc(st).get('repairSpeedFactor', 1.0) or 1.0)
        if k > 0.0:
            f /= k
    except Exception:
        pass
    return f


VISUAL_DAMAGE = True            # повреждения видны на модели танка (сбитая гусеница, огонь) - у ботов и у игрока


def _visualExtra(v, td, name, want):
    """Включает/выключает родной эффект машины (extra клиента): сбитая гусеница, пожар. Идемпотентно."""
    ex = td.extrasDict.get(name)
    if ex is None:
        return
    running = ex.index in getattr(v, 'extras', {})
    if want and not running:
        ex.startFor(v)
    elif (not want) and running:
        data = v.extras.get(ex.index)
        if data is not None:
            data['extra'].stop(data)


def _tickVisual(st, v):
    """Приводит видимые повреждения модели (гусеницы, пожар) в соответствие с состоянием модулей."""
    if not VISUAL_DAMAGE or not getattr(v, 'isStarted', False) or getattr(v, 'appearance', None) is None:
        return
    td = v.typeDescriptor
    for name in ('leftTrack', 'rightTrack'):
        try:
            _visualExtra(v, td, name + 'Health', name in st.destroyed)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    try:
        _visualExtra(v, td, 'fire', st.fireUntil > BigWorld.time())
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _tickPerks(st, v, now):
    try:
        q = int(_hpFrac(st) * 20)
        if q != getattr(st, '_hpQ', None):
            st._hpQ = q
            if hasPerk('desperado'):
                st.dirty = True
        if not hasPerk('sixthsense'):
            return
        from gui.mods.offhangar import spotting
        if not spotting.isEnabled():
            return
        enemy = 3 - spotting._playerTeam()
        if spotting.isSpottedBy(enemy, v.id):
            t0 = getattr(st, '_seenSince', None)
            if t0 is None:
                st._seenSince = now
            elif now - t0 >= 3.0 and not getattr(st, '_lampShown', False):
                st._lampShown = True                # «лампочка»: враг видит нас дольше 3 секунд
                from gui.WindowsManager import g_windowsManager
                bw = g_windowsManager.battleWindow
                if bw is not None:
                    bw.showSixthSenseIndicator(True)
                LOG_NOTE('[PERKS] sixth sense lamp')
        else:
            st._seenSince = None
            st._lampShown = False
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _tickVehicle(st, v, now, dt):
    if st.dirty:
        _recompute(st)                    # изменения применяются сразу, не дожидаясь чьего-то запроса множителей
    if v.health <= 0:
        return
    _tickVisual(st, v)
    if st.isPlayer and PERKS_ENABLED:
        _tickPerks(st, v, now)
    # огонь
    if st.fireUntil > 0.0:
        if now >= st.fireUntil:
            _extinguish(st, 'burned out')
        else:
            try:
                mh = float(v.typeDescriptor.maxHealth)
            except Exception:
                mh = 1000.0
            _burn(v, st, max(1.0, mh * FIRE_DPS_FRACTION) * dt, st.fireBy)
            if v.health <= 0:
                return
    _tickRpm(st, dt)
    # авторемонт
    repair = _repairSkillFactor(st)
    for name in list(st.hp.keys()):
        if name == 'engine' and st.isPlayer and _PLAYER['rpm']['on']:
            continue                      # двигатель на повышенных оборотах не ремонтируется
        mx = _maxHP(st, name)
        hp = st.hp[name]
        cap = _repairCap(st, name)
        if name == 'fuelTank':
            continue                      # чинится только окончанием пожара
        if hp >= cap:
            continue
        secs = REPAIR_SECONDS.get(name, 10.0) * repair
        hp = min(cap, hp + (cap / max(1.0, secs)) * dt)
        st.hp[name] = hp
        if name in st.destroyed:
            if st.isPlayer:
                _pushRepair(st, name, hp / cap, secs * (1.0 - hp / cap))
            if hp >= cap - 1e-6:
                st.destroyed.discard(name)
                st.dirty = True
                if st.isPlayer:
                    _pushRepair(st, name, 0.0, 0.0, done=True)
                _pushState(st, name, 'repaired')
                cur = _state(st, name)
                _pushState(st, name, cur)
                snd = _voice(st, name, 'functional')
                if st.isPlayer and snd:
                    _say([snd])
                st.announced[name] = 'normal' if cur == 'normal' else 'critical'
                LOG_DEBUG('[DEV] vehicle %s %s repaired by crew (hp %.0f)' % (st.vid, name, hp))
        elif hp >= cap and st.announced.get(name) == 'critical':
            pass
    # неподвижность (для бинокля)
    try:
        from gui.mods.offhangar import battle as off_battle
        ph = off_battle._simplePhysicsByVehID.get(v.id)
        if ph is not None:
            if abs(getattr(ph, '_curSpeed', 0.0)) < 0.1 and abs(getattr(ph, '_curRotSpeed', 0.0)) < 0.05:
                if st.stillSince is None:
                    st.stillSince = now
            else:
                st.stillSince = None
    except Exception:
        pass


def _pushRepair(st, name, frac, secsLeft, done=False):
    pct = int(round(100.0 * max(0.0, min(1.0, frac))))
    if not done and st.repairUI.get(name) == pct:
        return
    st.repairUI[name] = pct
    try:
        p = _panel()
        if p is not None:
            if done:
                p.updateModuleRepair(name, 0, 0.0)
            else:
                p.updateModuleRepair(name, pct, secsLeft)
    except Exception:
        pass


# ------------------------------------------------------------
# Повреждения ДРУГИХ машин на экране (родная панель "damageInfoPanel", как у игры при наведении на цель)
# ------------------------------------------------------------
SHOW_TARGET_DEVICES = True      # при наведении на танк (врага или союзника) показывать его повреждённые модули/экипаж
_TARGET_PANEL = {'vid': None, 'key': None, 'logged': 0}


def _extraIdx(td, name):
    try:
        e = td.extrasDict.get(name)
        return None if e is None else e.index
    except Exception:
        return None


def _targetDevices(st, v):
    """(индексы повреждённых extras, индексы уничтоженных) для родной панели."""
    td = getattr(v, 'typeDescriptor', None)
    damaged, destroyed = [], []
    if td is None:
        return damaged, destroyed
    for name in DEVICES:
        idx = _extraIdx(td, name + 'Health')
        if idx is None:
            continue
        s = _state(st, name)
        if s == 'destroyed':
            destroyed.append(idx)
        elif s == 'critical':
            damaged.append(idx)
    for name in st.roster:
        idx = _extraIdx(td, name + 'Health')
        if idx is None:
            continue
        if name in st.ko:
            destroyed.append(idx)
    if st.fireUntil > BigWorld.time():
        idx = _extraIdx(td, 'fire')
        if idx is not None:
            damaged.append(idx)
    return damaged, destroyed


def _tickTargetPanel():
    """Раз в тик: если игрок целится в чужую машину, показать панель её повреждённых модулей; иначе скрыть."""
    if not SHOW_TARGET_DEVICES or not _ON_MODULES:
        return
    bw = _battleWindow()
    panel = getattr(bw, 'damageInfoPanel', None) if bw is not None else None
    if panel is None:
        return
    p = _TARGET_PANEL
    tgt = None
    try:
        tgt = BigWorld.target()
        pid = getattr(BigWorld.player(), 'playerVehicleID', 0)
        if tgt is not None and (not hasattr(tgt, 'typeDescriptor') or not hasattr(tgt, 'publicInfo') or tgt.id == pid):
            tgt = None
    except Exception:
        tgt = None
    if tgt is None:
        if p['vid'] is not None:
            p['vid'] = None
            p['key'] = None
            try:
                panel.hide()
            except Exception:
                pass
        return
    st = _STATE.get(tgt.id)
    damaged, destroyed = ([], []) if st is None else _targetDevices(st, tgt)
    key = (tgt.id, tuple(damaged), tuple(destroyed))
    if key == p['key']:
        return
    p['key'] = key
    p['vid'] = tgt.id
    try:
        if damaged or destroyed:
            panel.show(tgt.id, damaged, destroyed)
            if p['logged'] < 5:
                p['logged'] += 1
                LOG_DEBUG('[DEV] target %s devices shown: damaged=%s destroyed=%s' % (tgt.id, damaged, destroyed))
        else:
            panel.hide()
    except Exception:
        if p['logged'] < 5:
            p['logged'] += 1
            LOG_CURRENT_EXCEPTION()


def _tick(token):
    if token != _TOKEN or not _ACTIVE:
        return
    BigWorld.callback(TICK, lambda: _tick(token))
    try:
        now = BigWorld.time()
        pv = _player()
        if pv is not None and pv.id not in _STATE:
            _get(pv)                      # состояние игрока нужно сразу: оборудование действует и без повреждений
        for vid, st in list(_STATE.items()):
            v = BigWorld.entity(vid)
            if v is None:
                continue
            dt = max(0.0, min(0.5, now - st.lastTick))
            st.lastTick = now
            _tickVehicle(st, v, now, dt)
        _tickTargetPanel()
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ------------------------------------------------------------
# Оборудование и расходники игрока
# ------------------------------------------------------------
def _itemDescr(item):
    """Описание предмета из кэша клиента (artefacts.Equipment / OptionalDevice) по compactDescr."""
    try:
        from gui.mods.offhangar import shooting
        from items import vehicles as items_vehicles
        cd = shooting._getItemCompactDescr(item)
        if cd:
            return cd, items_vehicles.getDictDescr(cd)
    except Exception:
        pass
    return None, None


def _float(d, key, default):
    try:
        v = d.get(key)
        return float(v) if v is not None else default
    except Exception:
        return default


def _readLoadout():
    """Оборудование, расходники и навыки экипажа выбранной в ангаре машины."""
    _PLAYER.update({'eq': [], 'skill': 100.0, 'cmdSkill': 100.0, 'stim': 0.0, 'bia': 0.0, 'power': 1.0,
                    'turretRot': 1.0, 'fireFactor': 1.0, 'stereoscope': None, 'slotCds': None, 'perks': set()})
    _PLAYER['rpm'] = {'on': False, 'power': 1.1, 'loss': 1.5, 'e': None}
    try:
        from gui.mods.offhangar import shooting
        hv = shooting._resolveHangarVehicle()
    except Exception:
        hv = None
    if hv is None:
        LOG_DEBUG('[DEV] hangar vehicle not found: no equipment')
        return
    optNames = []
    try:
        for d in (getattr(hv, 'optDevices', None) or []):
            if not d:
                continue
            cd, descr = _itemDescr(d)
            name = str(getattr(descr, 'name', '') or getattr(d, 'name', '') or '')
            optNames.append(name)
            # бинокулярный прибор: обзор растёт, когда машина стоит (маскировочная сеть тоже имеет activateWhenStillSec)
            if descr is not None and descr.get('circularVisionRadiusFactor') is not None and descr.get('activateWhenStillSec') is not None:
                _PLAYER['stereoscope'] = (_float(descr, 'activateWhenStillSec', 3.0), _float(descr, 'circularVisionRadiusFactor', 1.25))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    items = []
    try:
        for eq in (getattr(hv, 'eqs', None) or []):
            if not eq:
                continue
            cd, descr = _itemDescr(eq)
            if cd and descr is not None:
                items.append((cd, descr))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    eqs, eqNames = _applyEquipment(items)
    # навыки экипажа из ангара
    levels = []
    cmdLevel = None
    bia = True
    try:
        for item in (getattr(hv, 'crew', None) or []):
            tman = item[1] if isinstance(item, tuple) and len(item) == 2 else item
            if tman is None:
                bia = False
                continue
            lvl = None
            for attr in ('efficiencyRoleLevel', 'roleLevel'):
                val = getattr(tman, attr, None)
                if val is not None:
                    try:
                        lvl = float(val)
                        break
                    except Exception:
                        pass
            if lvl is None:
                lvl = 100.0
            levels.append(lvl)
            role = str(getattr(tman, 'role', '') or '')
            if role == 'commander' or (cmdLevel is None and len(levels) == 1):
                cmdLevel = lvl
            skills = [str(getattr(s, 'name', s)).lower() for s in (getattr(tman, 'skills', None) or [])]
            if lvl >= 99.5:
                _PLAYER['perks'].update(skills)          # навык работает только при 100% основной специальности
            if 'brotherhood' not in skills:
                bia = False
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if not levels:
        bia = False
    if bia:
        _PLAYER['bia'] = 5.0
    base = (sum(levels) / len(levels)) if levels else 100.0
    _PLAYER['skill'] = base
    _PLAYER['cmdSkill'] = cmdLevel if cmdLevel is not None else base
    _PLAYER['eq'] = eqs
    LOG_NOTE('[PERKS] crew perks: %s' % (sorted(_PLAYER['perks']),))
    LOG_NOTE('[DEV] loadout: optional=%s equipment=%s consumables=%s crew level %.0f/%.0f (+%.0f stimulators%s) '
             'engine power x%.2f turret x%.2f binocular=%s' % (
                 optNames, eqNames, [(e['tag'], e['name']) for e in eqs], base, _PLAYER['cmdSkill'], _PLAYER['stim'],
                 ', BIA' if bia else '', _PLAYER['power'], _PLAYER['turretRot'], _PLAYER['stereoscope']))


def _applyEquipment(items):
    """items: [(compactDescr, описание)]. Сбрасывает и заново считает пассивные эффекты (газолин, стимуляторы, огнетушитель, ограничитель
    оборотов) и возвращает (список расходников игрока, имена)."""
    _PLAYER.update({'power': 1.0, 'turretRot': 1.0, 'stim': 0.0, 'fireFactor': 1.0})
    _PLAYER['rpm'] = {'on': False, 'power': 1.1, 'loss': 1.5, 'e': None}
    eqs = []
    eqNames = []
    for cd, descr in items:
        try:
            tags = set(getattr(descr, 'tags', None) or ())
            name = str(getattr(descr, 'name', ''))
            eqNames.append(name)
            if 'fuel' in tags:                     # газолин: всегда включён
                _PLAYER['power'] *= _float(descr, 'enginePowerFactor', 1.0)
                _PLAYER['turretRot'] *= _float(descr, 'turretRotationSpeedFactor', 1.0)
                continue
            if 'stimulator' in tags:               # кола, шоколад, пайки: +уровень экипажа
                _PLAYER['stim'] += _float(descr, 'crewLevelIncrease', 10.0)
                continue
            tag = ''
            for t in ('extinguisher', 'medkit', 'repairkit', 'trigger'):
                if t in tags:
                    tag = t
                    break
            if not tag:
                continue
            e = {'tag': tag, 'name': name, 'big': bool(descr.get('repairAll')), 'auto': bool(descr.get('autoactivate')),
                 'used': False, 'cd': cd, 'on': False}
            if tag == 'extinguisher':
                _PLAYER['fireFactor'] = min(_PLAYER['fireFactor'], _float(descr, 'fireStartingChanceFactor', 1.0))
            if tag == 'trigger':                   # снятый ограничитель оборотов двигателя
                _PLAYER['rpm'] = {'on': False, 'power': _float(descr, 'enginePowerFactor', 1.1),
                                  'loss': _float(descr, 'engineHpLossPerSecond', 1.5), 'e': e}
            eqs.append(e)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    return eqs, eqNames


def _syncFromSlots(force=False):
    """Оборудование берём из того, что реально лежит в слотах панели боя (родной словарь аватара): список, прочитанный из ангара при старте,
    может не совпадать (устаревший кэш машины). Возвращает True, если список расходников изменился."""
    try:
        slots = BigWorld.player()._PlayerAvatar__equipment
    except Exception:
        return False
    cds = []
    for idx in sorted(slots.keys()):
        cd = slots[idx][0]
        if cd:
            cds.append(cd)
    have = [e['cd'] for e in _PLAYER['eq']]
    passives = _PLAYER.get('slotCds')
    if not force and passives == cds:
        return False
    _PLAYER['slotCds'] = cds
    items = []
    try:
        from items import vehicles as items_vehicles
        for cd in cds:
            descr = items_vehicles.getDictDescr(cd)
            if descr is not None:
                items.append((cd, descr))
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return False
    old = dict((e['cd'], e) for e in _PLAYER['eq'])
    eqs, names = _applyEquipment(items)
    for e in eqs:
        o = old.get(e['cd'])
        if o is not None and o['used']:
            e['used'] = True
    _PLAYER['eq'] = eqs
    LOG_NOTE('[DEV] equipment from panel slots: cds=%s names=%s consumables=%s (hangar list was %s) engine power x%.2f turret x%.2f stimulators +%.0f' % (
        cds, names, [(e['tag'], e['name']) for e in eqs], have, _PLAYER['power'], _PLAYER['turretRot'], _PLAYER['stim']))
    for st in _STATE.values():
        st.dirty = True
    return True


def _slotCD(idx):
    """compactDescr предмета в слоте панели расходников (родной словарь аватара), None - слот неизвестен."""
    try:
        slot = BigWorld.player()._PlayerAvatar__equipment.get(idx)
        if slot is not None:
            return slot[0] or 0
    except Exception:
        pass
    return None


def _eqBySlot(idx):
    cd = _slotCD(idx)
    eqs = _PLAYER['eq']
    if cd is not None:
        for e in eqs:
            if e['cd'] == cd:
                return e
        return None
    i = idx - 3 if idx >= 3 else idx
    return eqs[i] if 0 <= i < len(eqs) else None


def _findConsumable(tag, auto=None):
    for e in _PLAYER['eq']:
        if e['tag'] == tag and not e['used'] and (auto is None or e['auto'] == auto):
            return e
    return None


def _consume(e):
    e['used'] = True
    try:
        av = BigWorld.player()
        if e.get('cd'):
            av.updateVehicleAmmo(e['cd'], 0, 0, 0)
    except Exception:
        LOG_DEBUG('[DEV] cannot push empty consumable slot')


def _player():
    try:
        return BigWorld.entity(getattr(BigWorld.player(), 'playerVehicleID', 0))
    except Exception:
        return None


def _error(msg):
    try:
        bw = _battleWindow()
        if bw is not None and hasattr(bw, 'vErrorsPanel'):
            bw.vErrorsPanel.showMessage(msg)
    except Exception:
        pass


def _repairDevice(st, name):
    """Полный ремонт модуля. True - что-то починили."""
    changed = False
    if name in st.destroyed or _hp(st, name) < _maxHP(st, name) - 0.5:
        changed = True
    if name == 'engine':
        st.rpmWear = 0.0
    st.hp[name] = _maxHP(st, name)
    st.destroyed.discard(name)
    st.announced[name] = 'normal'
    st.dirty = True
    if changed:
        _pushRepair(st, name, 0.0, 0.0, done=True)
        _pushState(st, name, 'normal')
        snd = _voice(st, name, 'fixed')
        if snd:
            _say([snd])
    return changed


def _damagedNames(st):
    res = [n for n in DEVICES if n in st.destroyed or _hp(st, n) < _maxHP(st, n) - 0.5]
    res.sort(key=lambda n: (0 if n in st.destroyed else 1, _hp(st, n) / _maxHP(st, n)))
    return res


_UI_TO_DEVS = {'chassis': ('leftTrack', 'rightTrack'), 'track': ('leftTrack', 'rightTrack')}


def activate(idx, deviceName=None):
    """Использовать расходник игрока. idx - номер слота панели расходников (родной onEquipmentButtonPressed)."""
    if not _ACTIVE or not _ON_EQUIP:
        return
    try:
        now = BigWorld.time()
        key = (idx, deviceName)
        if now - _LAST_PRESS.get(key, -9.0) < 0.25:
            return                        # повтор нажатия
        _LAST_PRESS[key] = now
        e = _eqBySlot(idx)
        if e is None and _syncFromSlots():
            e = _eqBySlot(idx)
        if e is None:
            LOG_DEBUG('[DEV] equipment slot %s: no usable consumable (passive item or empty); slot cd=%s, known=%s' % (
                idx, _slotCD(idx), [(x['tag'], x['cd']) for x in _PLAYER['eq']]))
            return
        v = _player()
        if v is None or v.health <= 0:
            return
        st = _get(v)
        tag = e['tag']
        if e['used'] and tag != 'trigger':
            return
        LOG_DEBUG('[DEV] use equipment %s (%s) slot=%s device=%s' % (tag, e['name'], idx, deviceName))
        if tag == 'extinguisher':
            if st.fireUntil > BigWorld.time():
                _extinguish(st, 'extinguisher')
                _consume(e)
            else:
                _error('extinguisherDoesNotActivated')
        elif tag == 'repairkit':
            _useRepairKit(st, e, idx, deviceName)
        elif tag == 'medkit':
            _useMedkit(st, e, idx, deviceName)
        elif tag == 'trigger':
            _toggleRpm(st, e, idx)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _collapse(idx):
    try:
        bw = _battleWindow()
        panel = getattr(bw, 'consumablesPanel', None)
        if panel is not None:
            panel.collapseEquipmentSlot(idx)
            try:
                panel._ConsumablesPanel__removeExpandEquipment(idx)
            except Exception:
                pass
    except Exception:
        pass


def _toggleRpm(st, e, idx):
    """Снятый ограничитель оборотов: больше мощности, но двигатель теряет прочность."""
    r = _PLAYER['rpm']
    if 'engine' in st.destroyed and not r['on']:
        _error('equipmentIsNotAvailable')
        return
    r['on'] = not r['on']
    e['on'] = r['on']
    st.dirty = True
    LOG_DEBUG('[DEV] removed RPM limiter %s (power x%.2f, engine loses %.1f hp/s)' % (
        'ON' if r['on'] else 'off', r['power'], r['loss']))
    try:
        bw = _battleWindow()
        panel = getattr(bw, 'consumablesPanel', None)
        if panel is not None:
            panel.setCoolDownTime(idx, -1 if r['on'] else 0)
    except Exception:
        pass


def _tickRpm(st, dt):
    """Пока ограничитель оборотов снят, двигатель теряет прочность; разрушенный двигатель выключает его."""
    r = _PLAYER['rpm']
    if not r['on'] or not st.isPlayer:
        return
    voices = []
    prev = _state(st, 'engine')
    hp = _hp(st, 'engine') - r['loss'] * dt
    st.rpmWear += r['loss'] * dt
    cur = _setStateQuiet(st, 'engine', hp)
    if cur != prev:
        key = 'critical' if cur == 'critical' else 'destroyed'
        snd = _voice(st, 'engine', key)
        if snd:
            voices.append((_voiceRank(key), snd))
        _flushVoices(st, voices)
    if cur == 'destroyed':
        r['on'] = False
        if r['e'] is not None:
            r['e']['on'] = False
        try:
            bw = _battleWindow()
            panel = getattr(bw, 'consumablesPanel', None)
            slot = None
            for i in range(3, 9):
                if _slotCD(i) == r['e']['cd']:
                    slot = i
            if panel is not None and slot is not None:
                panel.setCoolDownTime(slot, 0)
        except Exception:
            pass
        st.dirty = True


def _setStateQuiet(st, name, hp):
    mx = _maxHP(st, name)
    hp = max(0.0, min(mx, hp))
    st.hp[name] = hp
    if hp <= 0.0:
        st.destroyed.add(name)
    st.dirty = True
    cur = _state(st, name)
    if st.announced.get(name) != cur:
        st.announced[name] = cur
        _pushState(st, name, cur)
    return cur


def _useRepairKit(st, e, idx, deviceName):
    if deviceName is not None:
        names = _UI_TO_DEVS.get(str(deviceName), (str(deviceName),))
        if any(_repairDevice(st, n) for n in names if n in DEVICES):
            _consume(e)
        _collapse(idx)
        return
    damaged = _damagedNames(st)
    if not damaged:
        _error('repairkitAllDevicesAreNotDamaged')
        return
    if e['big']:
        for n in damaged:
            _repairDevice(st, n)
        _consume(e)
        return
    # маленький ремкомплект чинит один модуль: даём выбрать, если панель умеет, иначе чиним самый тяжёлый
    try:
        bw = _battleWindow()
        panel = getattr(bw, 'consumablesPanel', None)
        states = {}
        for n in DEVICES:
            states[n] = _state(st, n) if _state(st, n) != 'normal' else None
        panel.expandEquipmentSlot(idx, 'repairkit', states)
        return
    except Exception:
        LOG_DEBUG('[DEV] expandEquipmentSlot(repairkit) failed, repairing the worst module')
    if _repairDevice(st, damaged[0]):
        _consume(e)


def _reviveCrew(st, name):
    if name not in st.ko:
        return False
    st.ko.discard(name)
    st.dirty = True
    _pushState(st, name, 'normal')
    return True


def _useMedkit(st, e, idx, deviceName):
    if deviceName is not None:
        if _reviveCrew(st, str(deviceName)):
            _consume(e)
        _collapse(idx)
        return
    if not st.ko:
        _error('medkitAllTankmenAreSafe')
        return
    if e['big']:
        for n in list(st.ko):
            _reviveCrew(st, n)
        _consume(e)
        return
    try:
        bw = _battleWindow()
        panel = getattr(bw, 'consumablesPanel', None)
        states = {}
        for n in st.roster:
            states[n] = 'destroyed' if n in st.ko else None
        panel.expandEquipmentSlot(idx, 'medkit', states)
        return
    except Exception:
        LOG_DEBUG('[DEV] expandEquipmentSlot(medkit) failed, reviving the first crew member')
    n = sorted(st.ko)[0]
    if _reviveCrew(st, n):
        _consume(e)


# ------------------------------------------------------------
# Подключение к игроку (клавиши и клики обрабатывает родная панель расходников и вызывает avatar.onEquipmentButtonPressed;
# onDamageIconButtonPressed родного аватара тоже сводится к нему, поэтому подменяем только его)
# ------------------------------------------------------------
def _wirePlayer():
    try:
        av = BigWorld.player()
        av.onEquipmentButtonPressed = lambda idx, deviceName=None: activate(idx, deviceName)
        LOG_DEBUG('[DEV] equipment handler wired (slots: %s)' % (
            sorted(getattr(av, '_PlayerAvatar__equipment', {}).keys()),))
        _syncFromSlots(True)
    except Exception:
        LOG_DEBUG('[DEV] cannot wire equipment handler on the avatar')


# ------------------------------------------------------------
# Жизненный цикл
# ------------------------------------------------------------
def _setting(name):
    try:
        from gui.mods.offhangar import _constants as c
        val = str(c.readOfflineSettings().get(name, '1')).strip().lower()
        return val not in ('0', 'off', 'false', 'no', 'нет', 'выкл')
    except Exception:
        return True


def start():
    global _ACTIVE, _TOKEN, _ON_MODULES, _ON_CREW, _ON_FIRE, _ON_EQUIP
    _TOKEN += 1
    _STATE.clear()
    _LAST_PRESS.clear()
    _HP_DIAG[0] = 0
    _TARGET_PANEL.update({'vid': None, 'key': None, 'logged': 0})
    _ON_MODULES = ENABLED and _setting('moduledamage')
    _ON_CREW = ENABLED and _setting('crewdamage')
    _ON_FIRE = ENABLED and _setting('fire')
    _ON_EQUIP = ENABLED and _setting('equipment')
    _ACTIVE = ENABLED
    _readLoadout()
    LOG_NOTE('[DEV] modules=%s crew=%s fire=%s equipment=%s' % (_ON_MODULES, _ON_CREW, _ON_FIRE, _ON_EQUIP))
    BigWorld.callback(TICK, lambda t=_TOKEN: _tick(t))
    BigWorld.callback(1.0, lambda t=_TOKEN: _wireLater(t))


def _wireLater(token):
    if token != _TOKEN or not _ACTIVE:
        return
    _wirePlayer()


def stop():
    global _ACTIVE, _TOKEN
    _ACTIVE = False
    _TOKEN += 1
    _STATE.clear()
    _LAST_PRESS.clear()
