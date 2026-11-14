# -*- coding: utf-8 -*-
"""Обратная связь боя, как в оригинальной игре:

* индикатор обстрела (красная дуга вокруг прицела, откуда стреляют);
* озвучка экипажа: результаты выстрелов игрока (попал / пробил / не пробил / рикошет),
  гибель машины, обнаружение противника;
* подсветка (контур) вражеской машины под прицелом;
* индикатор брони: цвет цели в прицеле - зелёный (пробьёте), жёлтый (на грани), красный (не пробьёте).
"""
import math

import BigWorld
import Math

from constants import VEHICLE_HIT_FLAGS, DAMAGE_INFO_INDICES
from debug_utils import LOG_CURRENT_EXCEPTION

from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE
from gui.mods.offhangar import battle as off_battle
from gui.mods.offhangar import shooting as off_shooting

TICK = 0.1
ARMOR_INDICATOR = True       # цвет цели по пробитию
EDGE_HIGHLIGHT = True        # контур вражеской машины под прицелом
SPOTTED_RANGE = 350.0        # "Противник обнаружен", когда враг ближе, м
SPOTTED_COOLDOWN = 20.0

COLOR_PIERCE = 0x60FF00
COLOR_MARGINAL = 0xFFB400
COLOR_BOUNCE = 0xFF1E00

_REGISTERED = False
_ACTIVE = False
_TOKEN = 0
_STATE = {'focus': None, 'cat': None, 'override': None, 'internal': False,
          'spottedAt': -999.0, 'ricochetHint': None}


# ============================================================
# Голос экипажа
# ============================================================
CREW_VOICE = True            # озвучка экипажа (выключатель)
_VOICE = {'diag': False, 'fails': 0, 'recreated': False, 'names': None, 'resolved': {}, 'logged': 0}

# запасные имена реплик: родные названия в клиенте могут отличаться, поэтому ищем среди реально существующих
_EVENT_HINTS = {
    'enemy_killed': (('enemy', 'kill'), ('target', 'destroy')),
    'start_battle': (('start', 'battle'), ('battle', 'start')),
    'enemy_sighted': (('enemy', 'sight'), ('enemy', 'spot'), ('enemy', 'detect')),
    'vehicle_destroyed': (('vehicle', 'destroy'), ('vehicle', 'killed'), ('player', 'dead')),
}


def _voiceDiag(av):
    """Один раз пишет в лог, что есть у аватара для озвучки (строки [SOUND]) - по ним видно, почему экипаж молчит."""
    if _VOICE['diag']:
        return
    _VOICE['diag'] = True
    LOG_DEBUG('[SOUND] SoundGroups.g_instance = %s' % (getattr(off_battle.SoundGroups, 'g_instance', None),))
    for attr in ('soundNotifications', 'complexSoundNotifications'):
        obj = getattr(av, attr, None)
        if obj is None:
            LOG_DEBUG('[SOUND] avatar.%s is None' % attr)
        else:
            names = [n for n in dir(obj) if not n.startswith('__')]
            LOG_DEBUG('[SOUND] avatar.%s = %s, members: %s' % (attr, type(obj).__name__, names[:60]))


def _collectEventNames(sn):
    """Названия реплик, известные объекту озвучки (ключи словарей внутри него) - нужны и для лога, и для подбора имени."""
    names = set()
    try:
        for n in dir(sn):
            if n.startswith('__'):
                continue
            try:
                v = getattr(sn, n)
            except Exception:
                continue
            if isinstance(v, dict):
                for k in v.keys():
                    if isinstance(k, basestring):
                        names.add(k)
    except Exception:
        pass
    return names


def _resolveEvent(sn, event):
    """Возвращает имя реплики, которое реально есть у объекта озвучки (или исходное, если список неизвестен)."""
    if _VOICE['names'] is None:
        _VOICE['names'] = _collectEventNames(sn)
        LOG_DEBUG('[SOUND] known sound events (%d): %s' % (len(_VOICE['names']), sorted(_VOICE['names'])[:80]))
    names = _VOICE['names']
    if not names or event in names:
        return event
    if event in _VOICE['resolved']:
        return _VOICE['resolved'][event]
    found = event
    for tokens in _EVENT_HINTS.get(event, ()):
        for n in sorted(names):
            low = n.lower()
            if all(t in low for t in tokens):
                found = n
                break
        if found != event:
            break
    _VOICE['resolved'][event] = found
    LOG_DEBUG('[SOUND] event %r resolved to %r' % (event, found))
    return found


def _ensureVoice(av, force=False):
    """Если у аватара нет объекта озвучки - создаём (классы из клиента 0.9.0).
    В офлайне SoundGroups.g_instance может отсутствовать: тогда родной объект озвучки создался без звуков,
    поэтому один раз за бой пересоздаём его, когда SoundGroups уже есть."""
    try:
        off_battle._ensureSoundGroupsInstance()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if not force and getattr(av, 'soundNotifications', None) is not None:
        return True
    try:
        from SoundNotifications import SoundNotifications
        av.soundNotifications = SoundNotifications()
        _VOICE['names'] = None
        _VOICE['resolved'] = {}
        LOG_DEBUG('[SOUND] soundNotifications %s' % ('re-created' if force else 'created manually'))
    except Exception as e:
        if _VOICE['fails'] < 3:
            LOG_DEBUG('[SOUND] cannot create soundNotifications: %s' % e)
        _VOICE['fails'] += 1
        return getattr(av, 'soundNotifications', None) is not None
    if force or getattr(av, 'complexSoundNotifications', None) is None:
        try:
            from ComplexSoundNotifications import ComplexSoundNotifications
            av.complexSoundNotifications = ComplexSoundNotifications(av.soundNotifications)
        except Exception as e:
            LOG_DEBUG('[SOUND] cannot create complexSoundNotifications: %s' % e)
    return True


def applyBattleVolumes(reason=''):
    """Клиент перед боем глушит звуки арены (enableArenaSounds(False)), а включает их заново только родной путь входа в бой,
    которого в офлайне нет, - поэтому экипаж/двигатель молчали, пока не откроешь настройки звука (они выставляют громкости).
    Делаем то же, что настройки: громкости арены из сохранённых предпочтений + применить."""
    try:
        sg = off_battle._ensureSoundGroupsInstance()
        if sg is None:
            return
        sg.enableArenaSounds(True)
        sg.enableVoiceSounds(True)
        sg.applyPreferences()
        LOG_DEBUG('[SOUND] battle volumes applied (%s): voice=%.2f vehicles=%.2f effects=%.2f master=%.2f' % (
            reason, sg.getVolume('voice'), sg.getVolume('vehicles'), sg.getVolume('effects'), sg.getMasterVolume()))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def prepareVoice(av):
    """Вызывается в начале боя: пересоздаём объект озвучки один раз (см. _ensureVoice)."""
    if _VOICE['recreated'] or not CREW_VOICE:
        return
    _VOICE['recreated'] = True
    applyBattleVolumes('battle start')
    _voiceDiag(av)
    _ensureVoice(av, force=True)


def _say(av, event):
    """Реплика экипажа. Возвращает True, если вызов прошёл без ошибки."""
    if not CREW_VOICE:
        return False
    try:
        _voiceDiag(av)
        if not _ensureVoice(av):
            return False
        sn = av.soundNotifications
        name = _resolveEvent(sn, event)
        res = None
        try:
            res = sn.play(name)
        except TypeError:
            res = sn.play(name, _playerVid(av))
        if _VOICE['logged'] < 40:
            _VOICE['logged'] += 1
            LOG_DEBUG('[SOUND] crew voice: %s (as %r) -> %r' % (event, name, res))
        return True
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return False


def _sayEnemySpotted(av):
    """\"Противник обнаружен\": родной комплексный объект, а если его нет - обычная реплика."""
    try:
        cn = getattr(av, 'complexSoundNotifications', None)
        if cn is not None:
            cn.notifyEnemySpotted(True)
            LOG_DEBUG('[SOUND] crew voice: enemy spotted (complex)')
            return
    except Exception:
        LOG_CURRENT_EXCEPTION()
    _say(av, 'enemy_sighted')


# ============================================================
# Результаты выстрелов
# ============================================================
def _playerVid(av):
    return getattr(av, 'playerVehicleID', 0)


def onShot(shooterID, target, result):
    """Вызывается после каждого попадания по машине (shooting._applyDamage вернул result)."""
    if result is None or not _ACTIVE:
        return
    av = BigWorld.player()
    if av is None:
        return
    pvid = _playerVid(av)

    if shooterID == pvid:
        _playerShotResult(av, target, result)
    elif target.id == pvid:
        _playerWasHit(av, shooterID, target, result)


def _playerShotResult(av, target, result):
    VHF = VEHICLE_HIT_FLAGS
    flags = 0
    if result == 'friendly':
        flags = VHF.ATTACK_IS_DIRECT_PROJECTILE | VHF.MATERIAL_WITH_POSITIVE_DF_NOT_PIERCED_BY_PROJECTILE
    elif result == 'dead':
        flags = VHF.VEHICLE_WAS_DEAD_BEFORE_ATTACK
    elif result == 'kill':
        flags = VHF.VEHICLE_KILLED | VHF.ATTACK_IS_DIRECT_PROJECTILE | VHF.MATERIAL_WITH_POSITIVE_DF_PIERCED_BY_PROJECTILE
    elif result == 'pierce':
        flags = VHF.ATTACK_IS_DIRECT_PROJECTILE | VHF.MATERIAL_WITH_POSITIVE_DF_PIERCED_BY_PROJECTILE
    elif result == 'ricochet':
        flags = VHF.ATTACK_IS_DIRECT_PROJECTILE | VHF.RICOCHET
    else:
        flags = VHF.ATTACK_IS_DIRECT_PROJECTILE | VHF.MATERIAL_WITH_POSITIVE_DF_NOT_PIERCED_BY_PROJECTILE
    try:
        av.showShotResults([int(target.id) | (int(flags) << 32)])
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if result == 'kill':
        _say(av, 'enemy_killed')


def _playerWasHit(av, shooterID, target, result):
    if result in ('friendly', 'dead'):
        return
    # индикатор обстрела
    try:
        shooter = BigWorld.entity(shooterID)
        if shooter is not None:
            sp = _pos(shooter)
            tp = _pos(target)
            # wg_globalYaw индикатора - направление ПОЛЁТА снаряда (от стрелявшего к игроку), а не на стрелявшего
            yaw = math.atan2(tp.x - sp.x, tp.z - sp.z)
            av.showOwnVehicleHitDirection(yaw, result in ('pierce', 'kill'))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    # сообщение о гибели
    if result == 'kill':
        try:
            av.showVehicleDamageInfo(_playerVid(av), DAMAGE_INFO_INDICES['DEATH_FROM_SHOT'], 0, shooterID)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        _say(av, 'vehicle_destroyed')


def _pos(vehicle):
    try:
        sp = off_battle._simplePhysicsByVehID.get(vehicle.id)
        if sp is not None:
            return sp.pos
    except Exception:
        pass
    return vehicle.position


# ============================================================
# Прицел: цель, подсветка, индикатор брони
# ============================================================
def _aimAt(av, pv):
    """Что сейчас под прицелом: (вражеская машина, результат попадания, категория пробития) или (None, None, None)."""
    vd = pv.typeDescriptor
    shot = vd.gun['shots'][vd.activeGunShotIndex]
    maxDistance = shot['maxDistance']
    origin, direction = off_shooting._shotOrigin(pv)
    end = origin + direction * maxDistance

    hit = off_shooting._findHitVehicle(pv, origin, end)
    if hit is None:
        return None, None, None
    target, res = hit
    try:
        static = off_shooting._findStaticHit(pv, origin, end)
        if static is not None and static[0] < res.dist - 0.5:
            return None, None, None
    except Exception:
        pass
    try:
        if target.health <= 0:
            return None, None, None
        if target.publicInfo['team'] == pv.publicInfo['team']:
            return target, res, 'ally'      # союзник под прицелом - зелёный контур (без индикатора брони)
    except Exception:
        return None, None, None

    kind, pierce, eff = off_shooting.pierceInfo(
        shot['shell'], shot['piercingPower'], res.hitAngleCos, res.armor, res.dist, maxDistance)
    if kind == 'ricochet':
        cat = 'bounce'
    else:
        r = float(shot['shell'].get('piercingPowerRandomization', 0.25))
        if pierce * (1.0 - r) >= eff:
            cat = 'pierce'          # пробьёт при любом броске
        elif pierce * (1.0 + r) >= eff:
            cat = 'marginal'        # зависит от разброса пробития
        else:
            cat = 'bounce'
    return target, res, cat


def _focus(av, entity):
    _STATE['internal'] = True
    try:
        av.targetFocus(entity)
    finally:
        _STATE['internal'] = False


def _blur(av, entity):
    _STATE['internal'] = True
    try:
        av.targetBlur(entity)
    finally:
        _STATE['internal'] = False


def _applyColor(cat):
    _STATE['override'] = {'pierce': COLOR_PIERCE, 'marginal': COLOR_MARGINAL,
                          'bounce': COLOR_BOUNCE}.get(cat) if ARMOR_INDICATOR else None


def _aimTick(av):
    pv = BigWorld.entity(_playerVid(av))
    if pv is None or not getattr(pv, 'isStarted', False):
        return
    if pv.health <= 0:
        _dropFocus(av)
        return
    try:
        target, res, cat = _aimAt(av, pv)
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return

    newID = target.id if target is not None else None
    oldID = _STATE['focus']

    if newID != oldID:
        if oldID is not None:
            old = BigWorld.entity(oldID)
            if old is not None:
                _blur(av, old)
        _STATE['focus'] = newID
        _STATE['cat'] = cat
        _applyColor(cat)
        if target is not None:
            _focus(av, target)
    elif target is not None and cat != _STATE['cat']:
        _STATE['cat'] = cat
        _applyColor(cat)
        try:
            aim = av.inputHandler.aim
            if aim is not None:
                aim.setTarget(target)       # перерисует подпись цели новым цветом
        except Exception:
            LOG_CURRENT_EXCEPTION()


def _dropFocus(av):
    oldID = _STATE['focus']
    if oldID is not None:
        old = BigWorld.entity(oldID)
        if old is not None:
            try:
                _blur(av, old)
            except Exception:
                pass
    _STATE['focus'] = None
    _STATE['cat'] = None
    _STATE['override'] = None


# ============================================================
# Голос: "противник обнаружен"
# ============================================================
def _spottedTick(av):
    now = BigWorld.time()
    if now - _STATE['spottedAt'] < SPOTTED_COOLDOWN or off_battle.isPrebattle():
        return
    pv = BigWorld.entity(_playerVid(av))
    if pv is None:
        return
    pp = _pos(pv)
    try:
        from gui.mods.offhangar import bots
        for ai in list(bots._BOTS.values()):
            if not ai.alive or ai.team == pv.publicInfo['team']:
                continue
            v = ai.vehicle()
            if v is None or not getattr(v, 'isStarted', False):
                continue
            p = _pos(v)
            try:
                from gui.mods.offhangar import spotting as _sp
                _seenNow = _sp.isSpottedBy(pv.publicInfo['team'], v.id) if _sp.isEnabled() else \
                    math.hypot(p.x - pp.x, p.z - pp.z) < SPOTTED_RANGE
            except Exception:
                _seenNow = math.hypot(p.x - pp.x, p.z - pp.z) < SPOTTED_RANGE
            if _seenNow and getattr(ai, '_spotted', False) is False:
                ai._spotted = True
                _STATE['spottedAt'] = now
                _sayEnemySpotted(av)
                return
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Цикл
# ============================================================
def _tick(token):
    if token != _TOKEN or not _ACTIVE:
        return
    BigWorld.callback(TICK, lambda: _tick(token))
    try:
        if not off_battle._active():
            return
        av = BigWorld.player()
        if av is None or not hasattr(av, 'playerVehicleID'):
            return
        if not _VOICE['recreated']:
            prepareVoice(av)
        if not _STATE.get('startSaid') and not off_battle.isPrebattle():
            _STATE['startSaid'] = True
            applyBattleVolumes('countdown end')
            _say(av, 'start_battle')
        _aimTick(av)
        _spottedTick(av)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def start():
    global _ACTIVE, _TOKEN
    _TOKEN += 1
    _ACTIVE = True
    _STATE.update({'focus': None, 'cat': None, 'override': None, 'internal': False, 'spottedAt': -999.0,
                   'startSaid': False})
    _VOICE['recreated'] = False
    BigWorld.callback(1.0, lambda t=_TOKEN: _tick(t))
    LOG_NOTE('[FEEDBACK] started')


def stop():
    global _ACTIVE, _TOKEN
    _TOKEN += 1
    _ACTIVE = False
    try:
        av = BigWorld.player()
        if av is not None and hasattr(av, 'playerVehicleID') and _STATE['focus'] is not None:
            _dropFocus(av)
    except Exception:
        pass
    _STATE['focus'] = None
    _STATE['override'] = None


# ============================================================
# Патчи
# ============================================================
def register():
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # цвет цели в прицеле
    try:
        from AvatarInputHandler import aims
        origColor = aims.Aim.getTargetColor

        def getTargetColor(self, isFriend, orig=origColor):
            c = _STATE.get('override')
            if _ACTIVE and c is not None and not isFriend:
                return c
            return orig(self, isFriend)

        aims.Aim.getTargetColor = getTargetColor
        LOG_NOTE('[FEEDBACK] aim colour patch installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()

    # фокус цели ведём сами (по лучу орудия), родные вызовы движка глушим, чтобы не мешали
    try:
        import Avatar as _A
        cls = _A.PlayerAvatar
        origFocus = cls.targetFocus
        origBlur = cls.targetBlur

        def targetFocus(self, entity, orig=origFocus):
            if _ACTIVE and not _STATE['internal'] and off_battle._active():
                return
            return orig(self, entity)

        def targetBlur(self, prevEntity, orig=origBlur):
            if _ACTIVE and not _STATE['internal'] and off_battle._active():
                return
            return orig(self, prevEntity)

        cls.targetFocus = targetFocus
        cls.targetBlur = targetBlur
        LOG_NOTE('[FEEDBACK] target focus patch installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()
