# -- coding: utf-8 --
import random
import cPickle
import re
import weakref
import math
import Math
import BigWorld
import AccountCommands
import ArenaType
import ResMgr
import AreaDestructibles
import SoundGroups
import DestructiblesCache
import AvatarInputHandler
from constants import (
    DESTRUCTIBLE_MATKIND,
    QUEUE_TYPE,
    ARENA_UPDATE,
    ARENA_GUI_TYPE,
    ARENA_BONUS_TYPE,
)
from items import ITEM_TYPE_INDICES
from debug_utils import LOG_CURRENT_EXCEPTION

from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE, LOG_ERROR
from gui.mods.offhangar import perf as _perf
from gui.mods.offhangar.server import BASE_REQUESTS, FakeServer
from gui.mods.offhangar._constants import OFFLINE_NICKNAME, OFFLINE_DBID


# ============================================================
# Offline battle state
# ============================================================
_REGISTERED = False
_BATTLE_ACTIVE = False

_ORIG_INIT_STEP = None
_ORIG_ON_ENTER_WORLD = None
_ORIG_ON_LEAVE_WORLD = None

_PENDING_CTX = None

_AVATAR_FAKE_SERVER = FakeServer('OfflineAvatarServer', True)

_OFFLINE_ATTR_DEFAULTS = {
    'isGunLocked': False,
}

_simplePhysicsByVehID = {}
_simpleTurretByVehID = {}

# Уже сломанные разрушаемые объекты боя: (chunkID, itemIndex). Общий для игрока и ботов: их коллизия
# игнорируется физикой (движок мог не убрать её после "поломки", и тогда танк упирался бы в невидимый забор).
_BROKEN_ITEMS = set()
_DEST_ITEM_CACHE = {}       # (chunkID, itemIndex) -> True/False: действительно ли это разрушаемый объект из кэша клиента


def _probeDestructibleAt(spaceID, segStart, hitPt):
    """Перпендикулярный зонд (как _try_destroy_solid_hit в 0.8.2): отрезок идёт сквозь поверхность вдоль направления луча,
    от hit+3 м до hit-2 м. Возвращает (chunkID, itemIndex, matKind, fname) или None."""
    try:
        d = hitPt - segStart
        if d.length < 0.001:
            return None
        d.normalise()
        a = hitPt + d.scale(3.0)
        b = hitPt - d.scale(2.0)
        mi = BigWorld.wg_getMatInfoNearPoint(spaceID, a, b, hitPt, lambda *args: False)
        if mi is None or len(mi) < 6:
            return None
        return (mi[2], mi[3], mi[4], mi[5])
    except Exception:
        return None


_SOLID_PROBE_LOG = {'ok': 0, 'none': 0}


def _isDestructibleItem(spaceID, chunkID, itemIndex, matKind):
    """Попадание луча с маской 128 бывает и в обычную статику - разрушаемым считаем только объект, у которого
    есть описание в кэше разрушаемых (как в _tryDestroyDestructible)."""
    try:
        key = (int(chunkID), int(itemIndex))
        if key[1] < 0 or key[1] > 0x7FFFFFFE or key[0] < 0 or key[0] > 0x7FFFFFFE:
            return False
        if key in _BROKEN_ITEMS:
            return True
        v = _DEST_ITEM_CACHE.get(key)
        if v is not None:
            return v
        ok = False
        if int(matKind) <= max(int(DESTRUCTIBLE_MATKIND.MAX), STP_DESTR_MATKIND_WIDE_MAX):
            fn = BigWorld.wg_getDestructibleFilename(spaceID, key[0], key[1])
            if fn:
                import re
                fn = re.sub('/lod./', '/lod0/', fn)
                lowName = fn.lower()
                # Малые ограждения/ворота считаем разрушаемыми даже если
                # кэш клиента не вернул обычный descriptor.
                fenceLike = any(word in lowName for word in ('fence', 'gate', 'zabor', 'barrier', 'rail'))
                ok = fenceLike or AreaDestructibles.g_cache.getDescByFilename(fn) is not None
        _DEST_ITEM_CACHE[key] = ok
        return ok
    except Exception:
        return False


def _ensureSoundGroupsInstance():
    try:
        sg = getattr(SoundGroups, 'g_instance', None)
        if sg is None:
            sg = SoundGroups.SoundGroups()
            SoundGroups.g_instance = sg
        return sg
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return None


ENGINE_SOUND_ENABLED = True       # звук двигателя танка игрока
ENGINE_SOUND_RETRY_GAP = 1.0      # пауза между повторными попытками запуска звука, с
ENGINE_SOUND_MAX_TRIES = 25
BOT_ENGINE_SOUND = False          # звук двигателя у ботов: 29 лишних звуков вытесняли звук игрока (пропадал через несколько секунд)
ENGINE_SOUND_WATCHDOG_GAP = 0.2   # как часто проверять, что звук двигателя игрока ещё играет


def _engineEventName(vehicle):
    td = getattr(vehicle, 'typeDescriptor', None)
    if td is None:
        return None
    eng = td.engine
    try:
        if getattr(vehicle, 'isPlayer', False):
            return eng.get('soundPC') or eng.get('sound') or eng.get('soundNPC')
        return eng.get('soundNPC') or eng.get('sound') or eng.get('soundPC')
    except Exception:
        return None


def _startVehicleEngineSound(vehicle):
    """Создаёт и запускает звук двигателя. Возвращает звук или None (причина пишется в лог, строки [SOUND])."""
    try:
        if not ENGINE_SOUND_ENABLED:
            return None
        ap = getattr(vehicle, 'appearance', None)
        if ap is None:
            return None

        sound = getattr(vehicle, '_offhangar_engineSound', None)
        if sound is None:
            sound = getattr(ap, '_VehicleAppearance__engineSound', None)
        if sound is not None:
            return sound

        sg = _ensureSoundGroupsInstance()
        if sg is None:
            LOG_DEBUG('[SOUND] engine: SoundGroups instance unavailable')
            return None

        event = _engineEventName(vehicle)
        if not event:
            LOG_DEBUG('[SOUND] engine: no sound event in typeDescriptor.engine, keys=%s' % (
                list(getattr(vehicle.typeDescriptor, 'engine', {}).keys()),))
            return None

        # Сначала модель корпуса (звук едет вместе с танком), затем "пустая" модель.
        models = []
        try:
            hull = ap.modelsDesc.get('hull', {}).get('model')
            if hull is not None:
                models.append(('hull', hull))
        except Exception:
            pass
        try:
            models.append(('fake', BigWorld.player().newFakeModel()))
        except Exception:
            pass

        sound = None
        how = None
        for label, model in models:
            for fname in ('getSound', 'getSound3D'):
                fn = getattr(sg, fname, None)
                if fn is None:
                    continue
                try:
                    sound = fn(model, event)
                except Exception:
                    sound = None
                if sound is not None:
                    how = '%s(%s model)' % (fname, label)
                    break
            if sound is not None:
                break

        if sound is None:
            for label, model in models:
                try:
                    sound = _playSoundWithFallback(model, event, None)
                except Exception:
                    sound = None
                if sound is not None:
                    how = 'playSound with fallback (%s model)' % label
                    break

        if sound is None:
            # запасной вариант: обычный (не позиционный) звук - слышен всегда
            try:
                sound = sg.FMODgetSound(event)
                how = 'FMODgetSound (2D)'
            except Exception:
                sound = None

        if sound is None:
            LOG_DEBUG('[SOUND] engine: could not create sound for event %r (models tried: %s)' % (
                event, [m[0] for m in models]))
            return None

        try:
            sound.play()
        except Exception:
            LOG_CURRENT_EXCEPTION()

        LOG_DEBUG('[SOUND] engine sound started: event=%r via %s, vehicle %s' % (event, how, getattr(vehicle, 'id', '?')))

        try:
            vehicle._offhangar_engineSound = sound
        except Exception:
            pass

        try:
            setattr(ap, '_VehicleAppearance__engineSound', sound)
        except Exception:
            pass

        return sound
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return None


def _stopVehicleEngineSound(vehicle):
    try:
        sound = getattr(vehicle, '_offhangar_engineSound', None)
        if sound is None:
            return
        try:
            vehicle._offhangar_engineSound = None
            vehicle._offhangar_engineTries = ENGINE_SOUND_MAX_TRIES   # больше не перезапускать
        except Exception:
            pass
        for name in ('stop', 'release'):
            try:
                fn = getattr(sound, name, None)
                if fn is not None:
                    fn()
            except Exception:
                pass
        try:
            ap = getattr(vehicle, 'appearance', None)
            if ap is not None and getattr(ap, '_VehicleAppearance__engineSound', None) is sound:
                setattr(ap, '_VehicleAppearance__engineSound', None)
        except Exception:
            pass
        LOG_DEBUG('[SOUND] engine sound stopped, vehicle %s' % getattr(vehicle, 'id', '?'))
    except Exception:
        LOG_CURRENT_EXCEPTION()


_ENGINE_RPM_UP = 22.0        # как быстро RPM звука догоняет расчётный (1/с): плавный набор оборотов
_ENGINE_RPM_DOWN = 22.0


def _approachValue(cur, target, rate, dt):
    k = min(1.0, rate * dt)
    return cur + (target - cur) * k


def _setSoundParam(sound, name, value):
    try:
        if sound.hasParam(name):
            sound.param(name).value = value
    except Exception:
        pass


def _applyEngineParams(vehicle, sound):
    """Параметры звука двигателя/гусениц (те же, что считает родной VehicleAppearance.__newEngine, но по нашей физике:
    родной код читает filter.speedInfo, который в офлайне всегда нулевой, и каждые 0.1 с обнулял обороты - звук рвался)."""
    try:
        now = BigWorld.time()
        sp = _simplePhysicsByVehID.get(vehicle.id)
        speed = float(sp._curSpeed) if sp is not None else 0.0
        rots = float(getattr(sp, '_curRotSpeed', 0.0)) if sp is not None else 0.0
        flags = getattr(sp, 'flags', 0) if sp is not None else 0
        if isPrebattle():
            flags = 0

        try:
            phys = vehicle.typeDescriptor.physics
            speedMax = float(phys['speedLimits'][0])
            speedBack = float(phys['speedLimits'][1])
            rotLimit = float(phys.get('rotationSpeedLimit', 1.0)) or 1.0
        except Exception:
            speedMax, speedBack, rotLimit = 15.0, 5.0, 1.0
        speedRange = speedMax + speedBack

        last = getattr(vehicle, '_offhangar_engT', None)
        dt = (now - last) if last is not None else 0.0
        if dt <= 0.0:
            dt = 0.0
        if dt > 0.5:
            dt = 0.5
        vehicle._offhangar_engT = now

        # обороты: как в родном коде (по передачам), но со сглаживанием
        srg = speedRange / 3.0
        gearNum = int(math.ceil(math.floor(abs(speed) * 50.0) / 50.0 / srg)) if srg > 0.0 else 0
        rpmT = abs(1.0 + (abs(speed) - gearNum * srg) / srg) if gearNum != 0 else 0.0
        rpmT = max(0.0, min(1.0, rpmT))
        rpm = getattr(vehicle, '_offhangar_rpm', 0.0)
        rpm = _approachValue(rpm, rpmT, _ENGINE_RPM_UP if rpmT > rpm else _ENGINE_RPM_DOWN, dt)
        vehicle._offhangar_rpm = rpm

        # ускорение (сглаженное, -1.5..1.5)
        prevSpeed = getattr(vehicle, '_offhangar_engPrevSpeed', None)
        acc = getattr(vehicle, '_offhangar_engAcc', 0.0)
        if prevSpeed is not None and dt > 1e-3:
            raw = max(-1.5, min(1.5, (speed - prevSpeed) / dt))
            acc = _approachValue(acc, raw, 6.0, dt)
        vehicle._offhangar_engPrevSpeed = speed
        vehicle._offhangar_engAcc = acc

        # engine_load как в родном клиенте: 1 - двигатель на холостых/по инерции, 2 - игрок жмёт педаль (режим 3 -> 2).
        # Двигатель живого танка работает всегда, поэтому на месте это 1, а не 0 (0 - заглушен).
        engineLoad = 2 if (flags & 3) else 1

        speedRel = speed / speedMax if speed > 0.0 else (speed / speedBack if speedBack > 0.0 else 0.0)
        _setSoundParam(sound, 'speed_rel', speedRel)
        _setSoundParam(sound, 'speed_abs', speed)
        _setSoundParam(sound, 'rot_speed_abs', rots)
        _setSoundParam(sound, 'rot_speed_rel', rots / rotLimit)
        _setSoundParam(sound, 'gear_num', float(gearNum))
        _setSoundParam(sound, 'RPM', rpm)
        _setSoundParam(sound, 'acc_abs', acc)
        _setSoundParam(sound, 'engine_load', engineLoad)

        # гусеницы
        try:
            ap = getattr(vehicle, 'appearance', None)
            mv = getattr(ap, '_VehicleAppearance__movementSound', None)
            if mv is not None:
                _setSoundParam(mv, 'speed_abs', speed)
                _setSoundParam(mv, 'rot_speed_rel', rots / rotLimit)
                _setSoundParam(mv, 'speed', min(1.0, abs(speed) / speedMax) if speedMax > 0.0 else 0.0)
                _setSoundParam(mv, 'flying', 0.0)
                if getattr(vehicle, 'isPlayer', False):
                    _setSoundParam(mv, 'friction', min(1.0, abs(rots) * 0.8))
                    _setSoundParam(mv, 'ground', 1.0)
        except Exception:
            pass
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _updateVehicleEngineSound(vehicle):
    try:
        isPlayerVeh = bool(getattr(vehicle, 'isPlayer', False))
        sound = getattr(vehicle, '_offhangar_engineSound', None)
        if sound is None:
            sound = getattr(getattr(vehicle, 'appearance', None), '_VehicleAppearance__engineSound', None)
        if not isPlayerVeh:
            # боты: звук создаёт родной VehicleAppearance, мы только обновляем его параметры (v87: не чаще 6 раз/с на бота)
            if sound is not None:
                tnow = BigWorld.time()
                if tnow - getattr(vehicle, '_offhangar_engBotAt', -1.0) >= BOT_ENGINE_SOUND_GAP:
                    vehicle._offhangar_engBotAt = tnow
                    _applyEngineParams(vehicle, sound)
            return

        if sound is None:
            # звук ещё не создан (модель машины могла быть не готова) - пробуем снова, но не чаще раза в секунду
            if not ENGINE_SOUND_ENABLED:
                return
            tries = getattr(vehicle, '_offhangar_engineTries', 0)
            now = BigWorld.time()
            if tries >= ENGINE_SOUND_MAX_TRIES or now - getattr(vehicle, '_offhangar_engineTryAt', -999.0) < ENGINE_SOUND_RETRY_GAP:
                return
            try:
                vehicle._offhangar_engineTries = tries + 1
                vehicle._offhangar_engineTryAt = now
            except Exception:
                return
            sound = _startVehicleEngineSound(vehicle)
            if sound is None:
                return

        # погибшая машина не должна "тарахтеть"
        try:
            if vehicle.health <= 0:
                _stopVehicleEngineSound(vehicle)
                return
        except Exception:
            pass

        # сторож: если движок звуков остановил наш звук (нехватка голосов, смена модели) - запускаем снова
        try:
            now = BigWorld.time()
            if now - getattr(vehicle, '_offhangar_engineWatchAt', -999.0) >= ENGINE_SOUND_WATCHDOG_GAP:
                vehicle._offhangar_engineWatchAt = now
                playing = getattr(sound, 'isPlaying', None)
                if playing is not None and not playing:
                    sound.play()
                    LOG_DEBUG('[SOUND] engine sound was not playing - restarted, vehicle %s' % getattr(vehicle, 'id', '?'))
        except Exception:
            pass

        _applyEngineParams(vehicle, sound)
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Трупы: уничтоженная машина остаётся там, где погибла
# ============================================================
# После гибели родной код клиента подменяет модель/мотор машины, и труп уезжает на базу (позиция сущности)
# или проваливается под карту / улетает вверх. Запоминаем положение в момент гибели и держим его.
CORPSE_FREEZE = True        # False - вернуть прежнее поведение
CORPSE_HOLD_INTERVAL = 0.05
_CORPSES = {}               # vehicle.id -> {'mtx': Matrix, 'pos': Vector3, 'servo': Servo, 'model': модель}


def freezeCorpse(vehicle):
    """Вызывать ДО того, как машина получила 0 здоровья (пока физика ещё знает настоящее положение)."""
    if not CORPSE_FREEZE:
        return
    try:
        vid = vehicle.id
        if vid in _CORPSES:
            return
        sp = _simplePhysicsByVehID.get(vid)
        if sp is not None:
            try:
                sp.releaseTracks('(before death)')       # защита от вылета (v55): не писать в гусеницы умирающей машины
            except Exception:
                LOG_CURRENT_EXCEPTION()
        mtx = Math.Matrix()
        if sp is not None:
            vp = -sp.pitch if STP_INVERT_VISUAL_PITCH else sp.pitch
            vr = -sp.roll if STP_INVERT_VISUAL_ROLL else sp.roll
            mtx.setRotateYPR((sp.yaw, vp, vr))
            mtx.translation = Math.Vector3(sp.pos)
        else:
            mtx = Math.Matrix(vehicle.model.matrix)
        _CORPSES[vid] = {'mtx': mtx, 'pos': Math.Vector3(mtx.translation), 'servo': None, 'model': None}
        LOG_DEBUG('[CORPSE] vehicle %s frozen at (%.1f, %.1f, %.1f)' % (
            vid, mtx.translation.x, mtx.translation.y, mtx.translation.z))
        BigWorld.callback(0.0, lambda: _holdCorpse(vid))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _holdCorpse(vid):
    st = _CORPSES.get(vid)
    if st is None:
        return
    if not _BATTLE_ACTIVE:
        _CORPSES.pop(vid, None)
        return
    try:
        ent = BigWorld.entity(vid)
        mdl = getattr(ent, 'model', None) if ent is not None else None
        if mdl is not None:
            need = mdl is not st['model']
            if not need:
                try:
                    need = len(mdl.motors) != 1 or (Math.Vector3(mdl.position) - st['pos']).length > 0.05
                except Exception:
                    need = True
            if need:
                try:
                    for m in list(mdl.motors):
                        mdl.delMotor(m)
                except Exception:
                    pass
                servo = BigWorld.Servo(st['mtx'])
                mdl.addMotor(servo)
                st['servo'] = servo
                st['model'] = mdl
    except Exception:
        LOG_CURRENT_EXCEPTION()
    BigWorld.callback(CORPSE_HOLD_INTERVAL, lambda: _holdCorpse(vid))


def clearCorpses():
    _CORPSES.clear()


# ============================================================
# Simple physics constants
# ============================================================
STP_GROUND_RAY_UP = 3.0
STP_GROUND_RAY_DOWN = 10.0
STP_GROUND_RAY_WIDE_UP = 500.0
STP_GROUND_RAY_WIDE_DOWN = 500.0

STP_GROUND_RESOLVE_MAX_TRIES = 50
STP_GROUND_RESOLVE_DELAY = 0.1

STP_COLLIDE_MARGIN = 0.15
STP_COLLIDE_HEIGHTS = (0.05, 0.35, 0.75, 1.3, 1.9)
STP_COLLIDE_HEIGHTS_NEAR = (0.2, 0.75, 1.5)       # ближние боты: три высоты лучей
STP_COLLIDE_HEIGHTS_LITE = (0.35, 1.3)       # далёкие боты: две высоты лучей
STP_MAX_SLOPE = 0.70               # макс. подъём (tg угла, ~32 град): круче - танк не заедет (горы, склоны, стены)
STP_JAM_TIME = 0.8                 # столько секунд танк жмёт газ и не двигается - считаем, что он застрял (забор, столб, бордюр)
STP_JAM_MOVE = 0.35                # сдвиг меньше этого (м) за STP_JAM_TIME = застрял
STP_JAM_PASS_TIME = 1.6            # после застревания столько секунд проезжаем разрушаемые объекты и низкие препятствия насквозь
STP_SLOPE_GRAVITY = 0.40           # доля g, тормозящая танк на подъёме (физика склонов из 0.8.2): слабый движок глохнет на крутом
STP_SLIDE_GRADE = 0.50             # круче этого (tg, ~27 град) стоящий танк не удерживается и сползает вниз
STP_SLOPE_STEP_GRAD = 1.15         # резкий уступ на любом из 3 отрезков (скала, обрыв, стена) - как гора
STP_SLOPE_SAMPLES = 3
STP_DEFLECT_ANGLES = (0.55, -0.55, 1.0, -1.0)   # если упёрлись, пробуем скользить вдоль препятствия под этими углами (рад)
STP_SLOPE_LOOKAHEAD_MIN = 2.2      # на каком расстоянии впереди меряем уклон, м
# Перенесено из WoT-0.8.2-offline-Battles (GPLv3): прямой луч wg_collideSegment скользит по "коже" коллизии забора
# (matKind 101/109, пустое имя файла) и не показывает разрушаемый объект, поэтому забор считался стеной. Зонд, который
# пересекает поверхность перпендикулярно (wg_getMatInfoNearPoint), находит настоящий объект: chunk, индекс, matKind, имя.
STP_SOLID_PROBE = True             # при упоре в статику искать разрушаемый объект перпендикулярным зондом
STP_DESTR_MATKIND_WIDE_MAX = 130   # разрушаемые модели бывают с matKind выше 100 (в 0.8.2 принимают 71..130); окончательный фильтр - описание в кэше
STP_RAM_BREAK_ALWAYS = True        # заборы/ящики/деревья ломаются при ЛЮБОМ касании, а не только с разгона (> 1 м/с)
STP_DEST_NEAR_EPS = 0.3            # разрушаемый объект считается "тем самым", если он не дальше статики больше чем на столько, м
STP_GROUND_SKIP_DESTRUCTIBLES = True   # верх забора/ящика/дерева не считается землёй (иначе танк "въезжает" на забор или считает его склоном)
STP_STUCK_FORCE_TIME = 0.30        # столько секунд упора в НИЗКОЕ препятствие (забор, куст, камень) - и танк его проезжает
STP_LOW_OBSTACLE_H = 1.35          # препятствие считается низким, если луч на высоте выше этого его не задевает
STP_LOW_PASS_TIME = 1.4            # сколько секунд после срабатывания низкие препятствия не держат танк
STP_EMBED_GUARD = True             # не давать корпусу оказаться внутри статичной геометрии (дома, заборы)
STP_EMBED_HEIGHT = 0.9             # высота лучей проверки внутри корпуса, м
STP_COLLIDE_BASE_LIFT_MIN = 0.25
STP_COLLIDE_GROUND_NORMAL_IGNORE = 0.45

STP_GROUND_FOLLOW_RATE = 18.0
STP_USE_SMOOTH_GROUND = True
STP_DRIVE_GUARD = True             # как в 0.8.2: если земля по ходу плавно поднимается (холм, склон, насыпь) - это не препятствие, лучи стен не считаем
STP_DRIVE_RISE = 1.28              # макс. подъём на 1 м хода, всё что круче - уступ/стена (~52 град)
STP_LOW_SOFT_H = 0.8               # препятствие, задетое только нижними лучами (h <= этого, ~1 м над землёй), - низкое: проезжаем сразу (камни, бордюры, основания заборов)
STP_STEP_CLIMB = True               # низкий уступ (доски переезда, бордюр, рельс, кочка) танк заезжает, а не упирается как в стену
STP_STEP_MAX = 0.65                # макс. высота уступа, который танк берёт с хода, м
STP_RAY_TILT = True                # высота лучей коллизии учитывает наклон корпуса (на подъёме передние лучи не стартуют под землёй)
STP_RAM_FRONT = True                # разрушаемые объекты (заборы, ящики) ломаются, когда их задел нос/передние углы корпуса, а не только центр
STP_RAM_FRONT_EXTRA = 0.3          # вынос проверки вперёд за нос корпуса, м
STP_SUPPORT_POINTS = True          # высота корпуса не ниже хорды по опорным точкам (перёд/зад/борта): гусеницы не уходят под землю на кочках и в ложбинах
STP_SUPPORT_MARGIN = 0.03          # запас над землёй, м

STP_PITCH_RESPONSE = 12.0
STP_ROLL_RESPONSE = 14.0
STP_MAX_PITCH = 0.6
STP_MAX_ROLL = 0.45

STP_INVERT_VISUAL_PITCH = True
STP_INVERT_VISUAL_ROLL = True


# ============================================================
# Basic helpers
# ============================================================
def _active():
    return _BATTLE_ACTIVE


def _clampValue(minValue, maxValue, value):
    if value < minValue:
        return minValue
    if value > maxValue:
        return maxValue
    return value


def _isOffline():
    try:
        p = BigWorld.player()
    except Exception:
        return False

    return bool(getattr(p, 'isOffline', False)) or hasattr(p, 'fakeServer')


# ============================================================
# Destructibles helpers
# ============================================================
_DESTR_DESC_PATCHED = False


def _patchDestructibleDescLookup():
    global _DESTR_DESC_PATCHED

    if _DESTR_DESC_PATCHED:
        return

    _DESTR_DESC_PATCHED = True

    try:
        origGetDescByFilename = DestructiblesCache.DestructiblesCache.getDescByFilename

        def _patchedGetDescByFilename(self, filename):
            if not filename:
                return origGetDescByFilename(self, filename)

            try:
                f = filename.strip()
            except Exception:
                f = filename

            f = f.replace('\\', '/')
            f = re.sub('/lod./', '/lod0/', f)

            desc = origGetDescByFilename(self, f)
            if desc is not None:
                return desc

            low = f.lower()

            desc = origGetDescByFilename(self, low)
            if desc is not None:
                return desc

            if low.startswith('content/'):
                desc = origGetDescByFilename(self, f[8:])
                if desc is None:
                    desc = origGetDescByFilename(self, low[8:])
                if desc is not None:
                    return desc
            else:
                desc = origGetDescByFilename(self, 'content/' + f)
                if desc is None:
                    desc = origGetDescByFilename(self, 'content/' + low)
                if desc is not None:
                    return desc

            if not hasattr(self, '_offhangar_descAltMap'):
                alt = {}

                for d in getattr(self, '_DestructiblesCache__descs', []):
                    fn = d.get('filename', '')

                    try:
                        fn = fn.strip()
                    except Exception:
                        pass

                    fn = fn.replace('\\', '/').lower()
                    if not fn:
                        continue

                    alt[fn] = d

                    base = fn.rsplit('/', 1)[-1]
                    alt[base] = d

                    noext = base.rsplit('.', 1)[0]
                    alt[noext] = d

                self._offhangar_descAltMap = alt

            base = f.rsplit('/', 1)[-1].lower()
            noext = base.rsplit('.', 1)[0]

            return self._offhangar_descAltMap.get(base) or self._offhangar_descAltMap.get(noext)

        DestructiblesCache.DestructiblesCache.getDescByFilename = _patchedGetDescByFilename
        LOG_DEBUG('DestructiblesCache desc lookup patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


def _ensureDestructibleChunkLoaded(mgr, spaceID, chunkID):
    if mgr is None:
        return False

    if mgr.isChunkLoaded(chunkID):
        return True

    try:
        filenames = BigWorld.wg_getChunkDestrFilenames(spaceID, chunkID)
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return False

    if filenames is None:
        return False

    try:
        count = len(filenames)
    except Exception:
        try:
            count = int(filenames)
        except Exception:
            return False

    if count <= 0:
        try:
            mgr._DestructiblesManager__loadedChunkIDs[chunkID] = count
        except Exception:
            LOG_CURRENT_EXCEPTION()

        return mgr.isChunkLoaded(chunkID)

    try:
        mgr.onChunkLoad(chunkID, count)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    if mgr.isChunkLoaded(chunkID):
        return True

    try:
        for idx in xrange(count):
            mgr._DestructiblesManager__setDestructibleInitialState(chunkID, idx)

        mgr._DestructiblesManager__loadedChunkIDs[chunkID] = count
    except Exception:
        LOG_CURRENT_EXCEPTION()

    return mgr.isChunkLoaded(chunkID)

# ============================================================
# Turret rotation helper
# ============================================================
def _startTurretRotation(avatar, vehicle):
    try:
        from gui.mods.offhangar import turret as off_turret

        ctrl = off_turret.SimpleTurretRotation()
        ctrl.setup(vehicle, avatar)

        _simpleTurretByVehID[vehicle.id] = ctrl
        ctrl.start()
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Vehicle params
# ============================================================
# Разгон. Раньше танк достигал максимальной скорости за 1-3 секунды.
# Теперь ускорение зависит от удельной мощности и падает по мере набора скорости
# (как у настоящего танка: быстро трогается, дольше добирает максималку).
ACCEL_SCALE = 1.0          # 1.0 - как задумано; 0.5 - вдвое медленнее; 2.0 - вдвое резвее
ACCEL_FALLOFF = 0.6        # 0 - ускорение постоянно; ближе к 1 - сильнее падает у максималки
REVERSE_ACCEL_FACTOR = 0.85


class _VehicleParams(object):
    def __init__(self, vehicle):
        self.maxFwd = 15.0
        self.maxBack = 5.0
        self.accel = 2.2
        self.brake = 4.0
        self.rotRate = 1.2
        self.rotAccel = 4.0
        self.rotDecel = 6.0
        self.massTonnes = 50.0
        self.terrainResistance = 1.0
        self.terrainDrag = 0.03
        self.rotLimit = 1.0

        self.halfLength = 3.0
        self.halfWidth = 1.5
        self.clearance = 0.5

        try:
            td = vehicle.typeDescriptor

            phys = getattr(td, 'physics', None)
            if phys is not None:
                speedLimits = phys.get('speedLimits', (15.0, 5.0))

                self.maxFwd = float(speedLimits[0])
                self.maxBack = float(speedLimits[1])

                self.maxFwd = _clampValue(4.0, 20.0, self.maxFwd)
                self.maxBack = _clampValue(2.0, 10.0, self.maxBack)

                weight = float(phys.get('weight', 50000.0))
                enginePower = float(phys.get('enginePower', 350000.0))

                self.massTonnes = max(1.0, weight * 0.001)

                powerToWeight = enginePower / max(1.0, weight)
                self.accel = _clampValue(1.3, 3.2, 0.9 + powerToWeight * 0.12) * ACCEL_SCALE
                self.brake = _clampValue(2.5, 6.5, self.accel * 1.8)

                self.rotLimit = float(phys.get('rotationSpeedLimit', 1.2) or 1.2)
                self.rotRate = _clampValue(0.45, 2.2, self.rotLimit)

                terr = phys.get('terrainResistance', (1.0, 1.2, 1.5))
                self.terrainResistance = float(terr[0])
                self.terrainDrag = _clampValue(0.0, 0.2, 0.03 * self.terrainResistance)

            chassis = getattr(td, 'chassis', None)
            if chassis is not None:
                hitTester = chassis.get('hitTester', None)
                if hitTester is not None:
                    bbox = getattr(hitTester, 'bbox', None)
                    if bbox is not None:
                        chassisMin = bbox[0]
                        chassisMax = bbox[1]

                        self.halfWidth = max(0.8, (chassisMax[0] - chassisMin[0]) * 0.5)
                        self.halfLength = max(1.5, (chassisMax[2] - chassisMin[2]) * 0.5)

                hullPosition = chassis.get('hullPosition', None)
                if hullPosition is not None and hitTester is not None:
                    bbox = getattr(hitTester, 'bbox', None)
                    if bbox is not None:
                        chassisMin = bbox[0]
                        self.clearance = max(0.2, float(hullPosition.y) + float(chassisMin.y))

        except Exception:
            LOG_CURRENT_EXCEPTION()

        LOG_DEBUG(
            'vehicle params: maxFwd=%.2f maxBack=%.2f accel=%.2f brake=%.2f rot=%.2f '
            'mass=%.2f terr=%.2f halfLen=%.2f halfWidth=%.2f clearance=%.2f' % (
                self.maxFwd,
                self.maxBack,
                self.accel,
                self.brake,
                self.rotRate,
                self.massTonnes,
                self.terrainResistance,
                self.halfLength,
                self.halfWidth,
                self.clearance,
            )
        )


# ============================================================
# SimpleTankPhysics
# ============================================================
# ------------------------------------------------------------
# Анимация гусениц / колёс
# ------------------------------------------------------------
# Клиентский WGVehicleFashion крутит гусеницы по fashion.movementInfo (Vector4: [1] и [2] - движение левой и
# правой гусеницы), а пыль/звук берут скорость из filter.speedInfo ([0] - скорость, [1] - скорость поворота).
# Нативный фильтр в офлайн-бою эти значения не обновляет, поэтому физика мода пишет их сама.
#
# Единицы movementInfo в движке не документированы, поэтому режимы переключаются прямо в бою (пишутся в лог):
#   F6 - единицы: абсолютные м/с  <->  относительные (-1..1)
#   F7 - направление вращения гусениц (вперёд/назад)
#   F8 - направление вращения при повороте на месте (левая/правая)
#   F9 - раскладка Vector4 (0..3): какие компоненты куда пишем (подбор, если гусеницы крутятся не так)
BOT_TRACK_ANIMATION = True   # крутить гусеницы и у ботов (False - только у игрока, если вылетает)
TRACK_UNITS_ABS = True
TRACK_INVERT_SCROLL = False
TRACK_INVERT_TURN = False
TRACK_LAYOUT = 0              # раскладка Vector4: 0=(fwd,левая,правая) 1=(левая,правая) 2=(левая,правая,fwd) 3=(скорость,угловая)
_TRACK_MAX_INIT_TRIES = 400
_TRACK_ATTACH_INTERVAL = 0.3
_TRACK_MAX_ATTACH_TRIES = 40
_TRACK_KEYS_HOOKED = False


def _logTrackMode(why):
    LOG_DEBUG('tracks: MODE (%s) units=%s invertScroll=%s invertTurn=%s layout=%s' % (
        why, 'abs m/s' if TRACK_UNITS_ABS else 'relative -1..1', TRACK_INVERT_SCROLL, TRACK_INVERT_TURN, TRACK_LAYOUT))


def _patchTrackKeys():
    """F6/F7/F8 - переключение режимов анимации гусениц прямо в бою (для подбора единиц и направления)."""
    global _TRACK_KEYS_HOOKED
    if _TRACK_KEYS_HOOKED:
        return
    try:
        import Avatar as _A
        import Keys as _K
        cls = _A.PlayerAvatar
        origHandleKey = cls.handleKey
        kUnits = getattr(_K, 'KEY_F6', None)
        kScroll = getattr(_K, 'KEY_F7', None)
        kTurn = getattr(_K, 'KEY_F8', None)
        kLayout = getattr(_K, 'KEY_F9', None)

        def handleKey(self, isDown, key, mods, orig=origHandleKey):
            global TRACK_UNITS_ABS, TRACK_INVERT_SCROLL, TRACK_INVERT_TURN, TRACK_LAYOUT
            try:
                if isDown and _BATTLE_ACTIVE and key in (kUnits, kScroll, kTurn, kLayout):
                    if key == kLayout:
                        TRACK_LAYOUT = (TRACK_LAYOUT + 1) % 4
                    elif key == kUnits:
                        TRACK_UNITS_ABS = not TRACK_UNITS_ABS
                    elif key == kScroll:
                        TRACK_INVERT_SCROLL = not TRACK_INVERT_SCROLL
                    else:
                        TRACK_INVERT_TURN = not TRACK_INVERT_TURN
                    _logTrackMode('hotkey')
                    return True
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return orig(self, isDown, key, mods)

        cls.handleKey = handleKey
        _TRACK_KEYS_HOOKED = True
        LOG_DEBUG('tracks: hotkeys F6/F7/F8/F9 installed')
        _logTrackMode('initial')
    except Exception:
        LOG_CURRENT_EXCEPTION()


class _V4Writer(object):
    """Единый интерфейс записи Vector4 в провайдер (set(x, y, z, w)), независимо от того, какой класс Math для этого
    нашёлся. В клиенте 0.9.0 в Math нет Vector4Basic/Vector4Provider, поэтому значение пишем по-разному:
      value  - у провайдера есть записываемый .value;
      elem   - провайдер читает матрицу (source), значения кладём в её элементы (setElement);
      trans  - провайдер читает матрицу, значения кладём в translation;
      anim   - Vector4Animation: два одинаковых ключа и время посередине."""

    def __init__(self, provider, mode, matrix=None, mapping=None, attr=None):
        self.provider = provider
        self.mode = mode
        self.matrix = matrix
        self.mapping = mapping
        self.attr = attr

    def set(self, x, y, z=0.0, w=0.0):
        vals = (x, y, z, w)
        mode = self.mode
        if mode == 'value':
            self.provider.value = Math.Vector4(x, y, z, w)
        elif mode == 'elem':
            m = self.matrix
            for k, ab in enumerate(self.mapping):
                if ab is not None:
                    m.setElement(ab[0], ab[1], vals[k])
        elif mode == 'trans':
            p = self.mapping
            self.matrix.translation = Math.Vector3(vals[p[0]], vals[p[1]], vals[p[2]])
        elif mode == 'anim':
            v4 = Math.Vector4(x, y, z, w)
            self.provider.keyframes = [(0.0, v4), (1.0, v4)]
            self.provider.time = 0.5

    def read(self):
        return _readV4(self.provider)


def _readV4(p):
    try:
        v = p.value
        return tuple(float(v[i]) for i in range(4))
    except Exception:
        return None


def _close(a, b):
    return abs(a - b) < 1e-3


_V4_RECIPE = {'tried': False, 'ok': None, 'logged': 0}


def _probeClass(name):
    """Пробует класс Math.<name> как управляемый Vector4-провайдер. Возвращает рецепт или None.
    Рецепт - (имя класса, режим, атрибут источника, раскладка)."""
    cls = getattr(Math, name, None)
    if cls is None:
        return None, 'absent'

    # a) записываемый .value
    try:
        p = cls()
        p.value = Math.Vector4(1.0, 2.0, 3.0, 4.0)
        v = _readV4(p)
        if v is not None and _close(v[0], 1.0) and _close(v[1], 2.0) and _close(v[2], 3.0):
            return (name, 'value', None, None), 'value is writable'
    except Exception:
        pass

    # б) провайдер, читающий матрицу
    for attr in ('source', 'matrix', 'target', 'input', 'm'):
        try:
            p = cls()
            m = Math.Matrix()
            setattr(p, attr, m)
        except Exception:
            continue
        try:
            m.setIdentity()
        except Exception:
            pass
        # б1) значения в элементах матрицы
        try:
            for r in range(4):
                for k in range(4):
                    m.setElement(r, k, 100.0 * (r + 1) + (k + 1))
            v = _readV4(p)
            if v is not None:
                mapping = [None] * 4
                for i in range(4):
                    for r in range(4):
                        for k in range(4):
                            if _close(v[i], 100.0 * (r + 1) + (k + 1)):
                                mapping[i] = (r, k)
                if sum(1 for x in mapping if x is not None) >= 3:
                    return (name, 'elem', attr, mapping), 'matrix elements %s' % (mapping,)
        except Exception:
            pass
        # б2) значения в translation
        try:
            m.setIdentity()
        except Exception:
            pass
        try:
            m.translation = Math.Vector3(11.0, 22.0, 33.0)
            v = _readV4(p)
            if v is not None:
                perm = [None] * 3
                for i, want in enumerate((11.0, 22.0, 33.0)):
                    for k in range(4):
                        if _close(v[k], want):
                            perm[i] = k
                # perm[i] - в какой компоненте вектора оказалась i-я координата; нужен обратный порядок записи
                if None not in perm:
                    inv = [None] * 3
                    for i, k in enumerate(perm):
                        if k < 3:
                            inv[k] = i
                    if None not in inv:
                        return (name, 'trans', attr, tuple(inv)), 'matrix translation %s' % (perm,)
        except Exception:
            pass

    # в) анимация по ключам
    try:
        p = cls()
        v4 = Math.Vector4(1.0, 2.0, 3.0, 4.0)
        p.keyframes = [(0.0, v4), (1.0, v4)]
        p.time = 0.5
        v = _readV4(p)
        if v is not None and _close(v[0], 1.0) and _close(v[1], 2.0) and _close(v[2], 3.0):
            return (name, 'anim', None, None), 'keyframes'
    except Exception:
        pass
    return None, 'no way to write'


def _buildFromRecipe(recipe):
    name, mode, attr, mapping = recipe
    cls = getattr(Math, name)
    p = cls()
    if mode == 'value':
        p.value = Math.Vector4(0.0, 0.0, 0.0, 0.0)
        return _V4Writer(p, 'value')
    if mode in ('elem', 'trans'):
        m = Math.Matrix()
        setattr(p, attr, m)
        try:
            m.setIdentity()
        except Exception:
            pass
        return _V4Writer(p, mode, matrix=m, mapping=mapping, attr=attr)
    if mode == 'anim':
        w = _V4Writer(p, 'anim')
        w.set(0.0, 0.0, 0.0, 0.0)
        return w
    return None


def _newVec4Writer():
    """Новый управляемый Vector4-провайдер (или None). Подбор класса делается один раз на игру, результат - в лог."""
    if _V4_RECIPE['ok'] is None and not _V4_RECIPE['tried']:
        _V4_RECIPE['tried'] = True
        report = []
        order = ('Vector4Basic', 'Vector4Provider', 'Vector4Translation', 'Vector4MatrixAdaptor',
                 'Vector4Animation', 'Vector4Combiner', 'Vector4Swizzle', 'Vector4Product', 'Vector4Morph')
        for name in order:
            try:
                recipe, why = _probeClass(name)
            except Exception:
                recipe, why = None, 'probe raised'
            report.append('%s: %s' % (name, why))
            if recipe is not None:
                _V4_RECIPE['ok'] = recipe
                break
        LOG_DEBUG('tracks: writable Vector4 provider search: %s' % ('; '.join(report),))
        if _V4_RECIPE['ok'] is not None:
            LOG_DEBUG('tracks: USING %s' % (_V4_RECIPE['ok'],))
        else:
            LOG_ERROR('tracks: no writable Vector4 provider found; Math has: %s' % (
                [n for n in dir(Math) if 'Vector4' in n],))
            for n in dir(Math):
                if 'Vector4' in n and n != 'Vector4':
                    try:
                        LOG_DEBUG('tracks: Math.%s attributes: %s' % (
                            n, [a for a in dir(getattr(Math, n)) if not a.startswith('__')]))
                    except Exception:
                        pass
    recipe = _V4_RECIPE['ok']
    if recipe is None:
        return None
    try:
        return _buildFromRecipe(recipe)
    except Exception:
        if _V4_RECIPE['logged'] < 3:
            _V4_RECIPE['logged'] += 1
            LOG_CURRENT_EXCEPTION()
        return None


def _nativeWriter(prov):
    """Если нативный провайдер сам позволяет писать .value - оборачиваем его."""
    try:
        prov.value = Math.Vector4(0.0, 0.0, 0.0, 0.0)
        v = prov.value
        if v is not None and len(v) >= 3:
            return _V4Writer(prov, 'value')
    except Exception:
        pass
    return None


def _initTrackAnimation(vehicle):
    """Находим fashion и провайдеры. Возвращает dict или None, если appearance ещё не готов."""
    app = getattr(vehicle, 'appearance', None)
    if app is None:
        return None
    fashion = getattr(app, 'fashion', None)
    if fashion is None:
        fashion = getattr(app, '_VehicleAppearance__fashion', None)
    if fashion is None:
        return None

    st = {'app': app, 'fashion': fashion, 'movement': None, 'speed': None,
          'attached': False, 'tries': 0, 'next': 0.0}

    try:
        mi = fashion.movementInfo
    except Exception:
        mi = None
    LOG_DEBUG('tracks: fashion=%s movementInfo=%s' % (type(fashion).__name__, type(mi).__name__))

    writer = _nativeWriter(mi) if mi is not None else None
    if writer is not None:
        st['movement'] = writer
        LOG_DEBUG('tracks: using native fashion.movementInfo provider (writable)')
    else:
        own = _newVec4Writer()
        if own is not None:
            try:
                fashion.movementInfo = own.provider
                st['movement'] = own
                LOG_DEBUG('tracks: fashion.movementInfo replaced by %s (%s)' % (type(own.provider).__name__, own.mode))
            except Exception:
                LOG_CURRENT_EXCEPTION()
        else:
            LOG_DEBUG('tracks: no way to drive movementInfo - tracks will stay static')

    LOG_DEBUG('tracks: speedInfo writable=%s' % False)
    return st


def _attachTrackFashion(vehicle, st):
    """Штатное прикрепление fashion к шасси при старте падало ('Cannot attach given WGVehicleFashion ... yet not
    initialized'). Пробуем повторно, с нарастающей подменой параметров; все шаги пишем в лог."""
    now = BigWorld.time()
    if now < st['next']:
        return
    st['next'] = now + _TRACK_ATTACH_INTERVAL
    st['tries'] += 1
    n = st['tries']
    app = st['app']

    try:
        model = app.modelsDesc['chassis']['model']
    except Exception:
        model = getattr(vehicle, 'model', None)
    if model is None:
        return

    try:
        if hasattr(model, 'wg_fashion'):
            st['attached'] = True
            LOG_DEBUG('tracks: fashion already attached to chassis model (try %d)' % n)
            return
    except Exception:
        pass

    # Ступень 2 (после 3 неудач): подменяем physicsInfo и placingCompensationMatrix своими
    if n == 4:
        try:
            own = _newVec4Writer()
            if own is not None:
                st['fashion'].physicsInfo = own.provider
            st['fashion'].placingCompensationMatrix = Math.Matrix()
            LOG_DEBUG('tracks: physicsInfo / placingCompensationMatrix replaced by own providers')
        except Exception:
            LOG_CURRENT_EXCEPTION()

    # Ступень 3 (после 9 неудач): собираем новый fashion без привязки к нативному фильтру
    if n == 10:
        try:
            import VehicleAppearance as _VA
            f2 = BigWorld.WGVehicleFashion()
            vDesc = vehicle.typeDescriptor
            _VA.setupTracksFashion(f2, vDesc)
            f2.maxMovement = vDesc.physics['speedLimits'][0]
            mv = _newVec4Writer()
            if mv is not None:
                f2.movementInfo = mv.provider
                st['movement'] = mv
            ph = _newVec4Writer()
            if ph is not None:
                f2.physicsInfo = ph.provider
            f2.placingCompensationMatrix = Math.Matrix()
            st['fashion'] = f2
            app._VehicleAppearance__fashion = f2
            LOG_DEBUG('tracks: built a NEW fashion (own providers)')
        except Exception:
            LOG_CURRENT_EXCEPTION()

    try:
        model.wg_fashion = st['fashion']
        st['attached'] = True
        LOG_DEBUG('tracks: fashion attached to chassis model (try %d)' % n)
    except Exception:
        import sys as _sys
        if n <= 3 or n % 10 == 0:
            LOG_DEBUG('tracks: attach try %d failed: %s' % (n, _sys.exc_info()[1]))
        if n >= _TRACK_MAX_ATTACH_TRIES:
            st['attached'] = True
            LOG_ERROR('tracks: giving up attaching fashion after %d tries - tracks will stay static' % n)


BOT_ENGINE_SOUND_GAP = 0.16      # звук двигателя бота обновляется не чаще, сек
BOT_SMOOTH = True              # плавное движение ботов: поза считается редко (BOT_PHYSICS_TICK), а между расчётами модель плавно интерполируется каждый кадр
BOT_SMOOTH_NEAR = 650.0        # дальше этого расстояния от игрока интерполяция не нужна (v87: почти вся карта - далёкие боты тоже плавные), поза пишется при расчёте
BOT_SMOOTH_JUMP = 6.0          # скачок позиции больше этого (телепорт/выход из тупика) - не сглаживаем
_SMOOTH_SET = set()            # физики ботов, чья модель сглаживается
_SMOOTH_CB = [None]
_SMOOTH_PP = {'t': 0.0, 'pos': None}


def _smoothPlayerPos(now):
    if now - _SMOOTH_PP['t'] > 0.2:
        _SMOOTH_PP['t'] = now
        pos = None
        try:
            av = BigWorld.player()
            vid = getattr(av, 'playerVehicleID', 0)
            sp = _simplePhysicsByVehID.get(vid) if vid else None
            if sp is not None and not getattr(sp, 'isBot', False):
                pos = sp.pos
            else:
                v = BigWorld.entity(vid) if vid else None
                pos = v.position if v is not None else None
        except Exception:
            pos = None
        _SMOOTH_PP['pos'] = pos
    return _SMOOTH_PP['pos']


def _smoothWrite(ph, pose):
    mtx = ph._mtx
    if mtx is None:
        return
    mtx.setRotateYPR((pose[4], pose[5], pose[6]))
    mtx.translation = Math.Vector3(pose[1], pose[2], pose[3])


def _smoothOne(ph, now, pp):
    if ph._cbID is None:
        _SMOOTH_SET.discard(ph)         # физика остановлена (бот погиб/бой кончился) - модель больше не трогаем
        return
    sn = ph._snb
    if not sn or ph._mtx is None:
        return
    last = sn[-1]
    far = False
    if pp is not None:
        far = abs(last[1] - pp.x) > BOT_SMOOTH_NEAR or abs(last[3] - pp.z) > BOT_SMOOTH_NEAR
    if far or len(sn) < 2:
        if ph._snDone is not last:
            _smoothWrite(ph, last)
            ph._snDone = last
        return
    # задержка показа: модель отстаёт от физики на ~1.5 интервала расчёта, поэтому всегда есть пара поз, между которыми можно плавно перейти.
    # v87: интервал считается по РЕАЛЬНЫМ промежуткам между позами (при 20-30 FPS callback срабатывает раз в 1-2 кадра, интервал плавает),
    # задержка быстро растёт, если не хватило, и медленно падает.
    want = max(ph.tickInterval * 1.5 + 0.02, getattr(ph, '_snInt', 0.0) * 1.4 + 0.025)
    if want > ph._snD:
        ph._snD += (want - ph._snD) * 0.5
    else:
        ph._snD += (want - ph._snD) * 0.04
    tr = now - ph._snD
    if tr >= last[0]:
        over = tr - last[0]
        if len(sn) >= 2 and not last[7] and over < 0.25:
            # новая поза опоздала: не замираем, а продолжаем движение по последней скорости (раньше тут был рывок "стоп-прыжок")
            prev = sn[-2]
            span = last[0] - prev[0]
            if span > 1e-4:
                k = min(over, 0.12) / span
                dy = last[4] - prev[4]
                while dy > math.pi:
                    dy -= 2.0 * math.pi
                while dy < -math.pi:
                    dy += 2.0 * math.pi
                mtx = ph._mtx
                mtx.setRotateYPR((last[4] + dy * k, last[5], last[6]))
                mtx.translation = Math.Vector3(last[1] + (last[1] - prev[1]) * k, last[2] + (last[2] - prev[2]) * k,
                                               last[3] + (last[3] - prev[3]) * k)
                ph._snDone = None
                ph._snD += over * 0.5
                return
        if ph._snDone is not last:
            _smoothWrite(ph, last)
            ph._snDone = last
        return
    if tr <= sn[0][0]:
        pose = sn[0]
        if ph._snDone is not pose:
            _smoothWrite(ph, pose)
            ph._snDone = pose
        return
    i = len(sn) - 1
    while i > 0 and sn[i - 1][0] > tr:
        i -= 1
    s0 = sn[i - 1]
    s1 = sn[i]
    if s1[7]:
        if ph._snDone is not s1:
            _smoothWrite(ph, s1)
            ph._snDone = s1
        return
    span = s1[0] - s0[0]
    a = (tr - s0[0]) / span if span > 1e-4 else 1.0
    dy = s1[4] - s0[4]
    while dy > math.pi:
        dy -= 2.0 * math.pi
    while dy < -math.pi:
        dy += 2.0 * math.pi
    mtx = ph._mtx
    mtx.setRotateYPR((s0[4] + dy * a, s0[5] + (s1[5] - s0[5]) * a, s0[6] + (s1[6] - s0[6]) * a))
    mtx.translation = Math.Vector3(s0[1] + (s1[1] - s0[1]) * a, s0[2] + (s1[2] - s0[2]) * a, s0[3] + (s1[3] - s0[3]) * a)
    ph._snDone = None


def _smoothLoop():
    _SMOOTH_CB[0] = None
    if not _SMOOTH_SET:
        return
    _SMOOTH_CB[0] = BigWorld.callback(0.0, _smoothLoop)
    t0 = _perf.clock()
    _perf.frame()
    try:
        now = BigWorld.time()
        pp = _smoothPlayerPos(now)
        for ph in list(_SMOOTH_SET):
            try:
                _smoothOne(ph, now, pp)
            except Exception:
                LOG_CURRENT_EXCEPTION()
    finally:
        _perf.add('smooth', _perf.clock() - t0)


def _smoothRegister(ph):
    _SMOOTH_SET.add(ph)
    if _SMOOTH_CB[0] is None:
        _SMOOTH_CB[0] = BigWorld.callback(0.0, _smoothLoop)


def _smoothUnregister(ph):
    _SMOOTH_SET.discard(ph)


class SimpleTankPhysics(object):
    def __init__(self):
        self.vehicle = None

        self.pos = Math.Vector3(0.0, 0.0, 0.0)
        self.yaw = 0.0
        self.pitch = 0.0
        self.roll = 0.0

        self.params = None
        self.flags = 0

        self._cbID = None
        self._groundCbID = None

        self._mprov = None
        self._motorRemoved = False
        self._motorRetryNext = 0.0
        self._mtx = None

        self._lastSpeed = 0.0
        self._curSpeed = 0.0
        self._curRotSpeed = 0.0

        self.isBot = False
        self.tickInterval = 0.0
        self._snb = []
        self._snD = 0.1
        self._snDone = None
        self._trk = None
        self._trkTries = 0
        self._trkLog = 0

        self.verticalSpeed = 0.0
        self.lastGroundY = 0.0

        self._lastTime = 0.0
        self._groundReady = False
        self._groundMissCount = 0
        self._tiltLogCount = 0
        self._embedded = False
        self._blockedTime = 0.0
        self._lowPassUntil = 0.0
        self._dtc = 0.0
        self._grade = 0.0
        self._gradeT = -1.0
        self._wantMove = False
        self._jamT = 0.0
        self._jamPos = None
        self._lastBlock = None
        self._jamPassUntil = 0.0

        # Destructibles state
        self._brokenDestructibles = set()
        self._destDbgCount = 0
        self._destNoDescSeen = set()
        self._chunkDestrCache = {}
        self._chunkDestrCacheSpace = None

    # --------------------------------------------------------
    # Setup
    # --------------------------------------------------------
    def setup(self, vehicle):
        self.vehicle = weakref.proxy(vehicle)

        try:
            p = vehicle.model.position
            self.pos = Math.Vector3(float(p.x), float(p.y), float(p.z))
            self.yaw = float(vehicle.model.yaw)
        except Exception:
            self.pos = Math.Vector3(0.0, 0.0, 0.0)
            self.yaw = 0.0

        self.params = _VehicleParams(vehicle)

        self.flags = 0

        self._cbID = None
        self._groundCbID = None

        self._mprov = None if getattr(self, 'isBot', False) else self._resolveMProv()
        self._motorRemoved = False
        self._motorRetryNext = 0.0
        self._mtx = None

        self.pitch = 0.0
        self.roll = 0.0

        self._lastSpeed = 0.0
        self._curSpeed = 0.0
        self._curRotSpeed = 0.0

        self._trk = None
        self._trkTries = 0
        self._trkLog = 0

        self.verticalSpeed = 0.0
        self.lastGroundY = self.pos.y

        self._lastTime = BigWorld.time()
        self._groundReady = False
        self._groundMissCount = 0
        self._tiltLogCount = 0
        self._embedded = False
        self._blockedTime = 0.0
        self._lowPassUntil = 0.0
        self._dtc = 0.0
        self._grade = 0.0
        self._gradeT = -1.0
        self._wantMove = False
        self._jamT = 0.0
        self._jamPos = None
        self._lastBlock = None
        self._jamPassUntil = 0.0

        self._brokenDestructibles = set()
        self._destDbgCount = 0
        self._destNoDescSeen = set()
        self._chunkDestrCache = {}
        self._chunkDestrCacheSpace = None

        self._ensureMotorRemoved(vehicle)

        LOG_DEBUG(
            'SimpleTankPhysics setup id=%s pos=(%.1f,%.1f,%.1f) yaw=%.2f' % (
                getattr(vehicle, 'id', None),
                self.pos.x,
                self.pos.y,
                self.pos.z,
                self.yaw,
            )
        )

        self._resolveGround(vehicle, STP_GROUND_RESOLVE_MAX_TRIES)

    # --------------------------------------------------------
    # Start / stop
    # --------------------------------------------------------
    def start(self):
        if self._cbID is not None:
            return

        self._lastTime = BigWorld.time()
        self._cbID = BigWorld.callback(0.0, self._tick)

        LOG_DEBUG('SimpleTankPhysics started: cbID=%s' % self._cbID)

    def stop(self):
        _smoothUnregister(self)
        if self._groundCbID is not None:
            try:
                BigWorld.cancelCallback(self._groundCbID)
            except Exception:
                pass

            self._groundCbID = None

        if self._cbID is not None:
            try:
                BigWorld.cancelCallback(self._cbID)
            except Exception:
                pass

            self._cbID = None

    def setFlags(self, flags):
        self.flags = flags

    def getSpeed(self):
        return float(self._curSpeed)

    # --------------------------------------------------------
    # Ground resolve
    # --------------------------------------------------------
    def _resolveGround(self, vehicle, triesLeft):
        try:
            spaceID = vehicle.spaceID
        except Exception:
            spaceID = None

        y = None

        if spaceID is not None:
            y = self._groundHeightStrict(spaceID, self.pos.x, self.pos.y, self.pos.z)

        if y is not None:
            self.pos.y = y
            self.lastGroundY = y
            self._groundReady = True

            LOG_DEBUG(
                'SimpleTankPhysics ground resolved id=%s y=%.2f triesLeft=%d' % (
                    getattr(vehicle, 'id', None),
                    y,
                    triesLeft,
                )
            )

            self.start()
            return

        if triesLeft <= 0:
            LOG_ERROR(
                'SimpleTankPhysics ground resolve timeout id=%s pos=(%.1f,%.1f,%.1f)' % (
                    getattr(vehicle, 'id', None),
                    self.pos.x,
                    self.pos.y,
                    self.pos.z,
                )
            )

            self._groundReady = True
            self.start()
            return

        self._groundCbID = BigWorld.callback(
            STP_GROUND_RESOLVE_DELAY,
            lambda: self._resolveGround(vehicle, triesLeft - 1)
        )

    # --------------------------------------------------------
    # Matrix provider / motor
    # --------------------------------------------------------
    def _resolveMProv(self):
        try:
            avatar = BigWorld.player()
        except Exception:
            return None

        if avatar is None:
            return None

        mp = getattr(avatar, '_PlayerAvatar__ownVehicleMProv', None)
        if mp is not None and callable(getattr(mp, 'setStaticTransform', None)):
            try:
                mp.target = None
            except Exception:
                pass

            return mp

        return None

    def _ensureMotorRemoved(self, vehicle):
        if self._motorRemoved:
            return

        now = BigWorld.time()
        if self._motorRetryNext > now:
            return

        try:
            mdl = getattr(vehicle, 'model', None)
            if mdl is None:
                return

            motors = getattr(mdl, 'motors', None)
            if motors:
                mdl.delMotor(motors[0])

            mtx = Math.Matrix()

            visualPitch = -self.pitch if STP_INVERT_VISUAL_PITCH else self.pitch
            visualRoll = -self.roll if STP_INVERT_VISUAL_ROLL else self.roll

            mtx.setRotateYPR((self.yaw, visualPitch, visualRoll))
            mtx.translation = self.pos

            mdl.addMotor(BigWorld.Servo(mtx))

            self._mtx = mtx
            self._motorRemoved = True

        except Exception:
            LOG_CURRENT_EXCEPTION()
            self._motorRetryNext = now + 1.0

    # --------------------------------------------------------
    # Destructibles: debug / chunk / hide helpers
    # --------------------------------------------------------
    def _destDbg(self, msg):
        if getattr(self, '_destDbgCount', 0) < 200:
            self._destDbgCount = getattr(self, '_destDbgCount', 0) + 1
            LOG_DEBUG('[DESTDBG] %s' % msg)

    def _ensureChunkLoaded(self, spaceID, chunkID):
        mgr = AreaDestructibles.g_destructiblesManager

        if mgr is None:
            return False

        if mgr.isChunkLoaded(chunkID):
            return True

        try:
            filenames = BigWorld.wg_getChunkDestrFilenames(spaceID, chunkID)
        except Exception:
            LOG_CURRENT_EXCEPTION()
            return False

        if filenames is None:
            return False

        try:
            count = len(filenames)
        except Exception:
            try:
                count = int(filenames)
            except Exception:
                return False

        try:
            mgr.onChunkLoad(chunkID, count)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        if mgr.isChunkLoaded(chunkID):
            return True

        try:
            for idx in xrange(count):
                mgr._DestructiblesManager__setDestructibleInitialState(chunkID, idx)

            mgr._DestructiblesManager__loadedChunkIDs[chunkID] = count
        except Exception:
            LOG_CURRENT_EXCEPTION()

        return mgr.isChunkLoaded(chunkID)

    def _hideUnknownDestructible(self, spaceID, chunkID, itemIndex):
        try:
            fashion = BigWorld.wg_getChunkModelFashion(spaceID, chunkID, itemIndex)

            if fashion is None:
                fashion = BigWorld.WGMaterialDisabler()
                BigWorld.wg_setChunkModelFashion(spaceID, chunkID, itemIndex, fashion, True)

            try:
                fashion.isDestroyed = True
            except Exception:
                pass

            try:
                fashion.setEnableDrawMergedModel(False)
            except Exception:
                pass

            try:
                fashion.enableModelCompound = False
            except Exception:
                pass

            try:
                fashion.havokRemove()
            except Exception:
                pass

            return True

        except Exception:
            LOG_CURRENT_EXCEPTION()
            return False

    def _forceFragileHidden(self, spaceID, chunkID, itemIndex):
        mgr = AreaDestructibles.g_destructiblesManager
        if mgr is None:
            return

        try:
            fashion = BigWorld.wg_getChunkModelFashion(spaceID, chunkID, itemIndex)
            if fashion is None:
                fashion = BigWorld.WGMaterialDisabler()
                BigWorld.wg_setChunkModelFashion(spaceID, chunkID, itemIndex, fashion, True)

            try:
                fashion.isDestroyed = True
            except Exception:
                pass
            try:
                fashion.setEnableDrawMergedModel(False)
            except Exception:
                pass
            try:
                fashion.havokRemove()
            except Exception:
                pass
            try:
                fashion.havokDestroyAll()
            except Exception:
                pass
        except Exception:
            LOG_CURRENT_EXCEPTION()

        # 2) Штатный путь — для эффектов и зависимостей.
        try:
            # Client build exposes a shorter native signature; the extra
            # trailing flag raises TypeError during destruction handling.
            mgr._DestructiblesManager__setDestructibleState(
                spaceID, chunkID, itemIndex, True, None
            )
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _forceStructureHidden(self, spaceID, chunkID, itemIndex):
        try:
            fashion = BigWorld.wg_getChunkModelFashion(spaceID, chunkID, itemIndex)
            if fashion is None:
                fashion = BigWorld.WGMaterialDisabler()
                BigWorld.wg_setChunkModelFashion(spaceID, chunkID, itemIndex, fashion, True)

            try:
                fashion.isDestroyed = True
            except Exception:
                pass
            try:
                fashion.setEnableDrawMergedModel(False)
            except Exception:
                pass
            try:
                fashion.havokRemove()
            except Exception:
                pass
            try:
                fashion.havokDestroyAll()
            except Exception:
                pass
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _destroyStructureModules(self, spaceID, chunkID, itemIndex, desc, hitMatKind, speed):
        mgr = AreaDestructibles.g_destructiblesManager

        if mgr is None:
            return False

        modules = desc.get('modules') or {}

        if not modules:
            return False

        try:
            hitMatKind = int(hitMatKind)
        except Exception:
            hitMatKind = sorted(modules.keys())[0]

        originalHit = hitMatKind

        if hitMatKind not in modules:
            if DESTRUCTIBLE_MATKIND.DAMAGED_MIN <= hitMatKind <= DESTRUCTIBLE_MATKIND.DAMAGED_MAX:
                normal = hitMatKind - (
                    DESTRUCTIBLE_MATKIND.DAMAGED_MIN - DESTRUCTIBLE_MATKIND.NORMAL_MIN
                )
                if normal in modules:
                    hitMatKind = normal

            if hitMatKind not in modules:
                hitMatKind = sorted(modules.keys())[0]

        filename = (desc.get('filename') or '').lower()

        isFenceLike = any(
            s in filename
            for s in ('fence', 'gate', 'gaf', 'wall', 'zabor')
        )

        destroyAll = (
            isFenceLike
            or len(modules) <= 6
            or speed >= 8.0
            or originalHit not in modules
        )

        if destroyAll:
            matKinds = list(modules.keys())
        else:
            matKinds = [hitMatKind]

        anyDone = False

        for matKind in matKinds:
            try:
                matKind = int(matKind)
            except Exception:
                continue

            key = (
                chunkID,
                itemIndex,
                matKind,
                DestructiblesCache.DESTR_TYPE_STRUCTURE
            )

            if key in self._brokenDestructibles:
                continue

            try:
                data = int(
                    DestructiblesCache.encodeDestructibleModule(
                        itemIndex,
                        matKind,
                        False
                    )
                )

                mgr.orderDestructibleDestroy(
                    chunkID,
                    DestructiblesCache.DESTR_TYPE_STRUCTURE,
                    data,
                    True,
                    syncWithProjectile=False
                )

                self._brokenDestructibles.add(key)
                anyDone = True

            except Exception:
                LOG_CURRENT_EXCEPTION()

        if destroyAll and anyDone:
            self._noteBroken(chunkID, itemIndex)

        if not anyDone:
            allBroken = True
            for m in modules.keys():
                try:
                    mk = int(m)
                except Exception:
                    continue
                key = (
                    chunkID,
                    itemIndex,
                    mk,
                    DestructiblesCache.DESTR_TYPE_STRUCTURE
                )
                if key not in self._brokenDestructibles:
                    allBroken = False
                    break
            if allBroken:
                if destroyAll:
                    self._noteBroken(chunkID, itemIndex)
                return True

        return anyDone

    def _noteBroken(self, chunkID, itemIndex):
        """Запоминаем сломанный объект (для всех машин) и считаем поломки за шаг - танк чуть замедляется."""
        try:
            key = (int(chunkID), int(itemIndex))
            if key not in _BROKEN_ITEMS:
                _BROKEN_ITEMS.add(key)
                self._newBreaks = getattr(self, '_newBreaks', 0) + 1
        except Exception:
            pass

    def _tryDestroyDestructible(self, spaceID, chunkID, itemIndex, matKind, fallYaw, speed, easyOnly=False):
        mgr = AreaDestructibles.g_destructiblesManager
        cache = AreaDestructibles.g_cache

        if mgr is None or cache is None:
            return False

        if speed <= 1.0 and not STP_RAM_BREAK_ALWAYS:
            return False

        try:
            chunkID = int(chunkID)
            itemIndex = int(itemIndex)
            matKind = int(matKind)
        except Exception:
            return False
        if itemIndex < 0 or itemIndex > 0x7FFFFFFE or chunkID < 0 or chunkID > 0x7FFFFFFE:
            return False        # 4294967295 (-1) = "объекта нет": движок бросал TypeError на каждый кадр (24 тыс. раз за бой)

        if not hasattr(self, '_brokenDestructibles'):
            self._brokenDestructibles = set()

        if not hasattr(self, '_destNoDescSeen'):
            self._destNoDescSeen = set()

        # Принудительная регистрация чанка.
        if not mgr.isChunkLoaded(chunkID):
            try:
                filenames = BigWorld.wg_getChunkDestrFilenames(spaceID, chunkID)
                if filenames is not None:
                    mgr.onChunkLoad(chunkID, len(filenames))
            except Exception:
                LOG_CURRENT_EXCEPTION()

        if not mgr.isChunkLoaded(chunkID):
            return False

        # matKind > 100 обычно у статических неразрушаемых моделей, но у разрушаемых "кожа" коллизии тоже бывает 101..130 (0.8.2);
        # окончательный фильтр - описание объекта в кэше разрушаемых (ниже), поэтому диапазон можно расширить.
        if matKind > max(int(DESTRUCTIBLE_MATKIND.MAX), STP_DESTR_MATKIND_WIDE_MAX):
            return False

        filename = None
        desc = None

        try:
            filename = BigWorld.wg_getDestructibleFilename(spaceID, chunkID, itemIndex)
            if filename:
                import re
                filename = re.sub('/lod./', '/lod0/', filename)
            desc = cache.getDescByFilename(filename)
        except Exception:
            LOG_CURRENT_EXCEPTION()
            return False

        if desc is None:
            noDescKey = (chunkID, itemIndex, filename)
            if noDescKey not in self._destNoDescSeen and len(self._destNoDescSeen) < 25:
                self._destNoDescSeen.add(noDescKey)
                LOG_DEBUG(
                    '[DESTDBG] no desc chunk=%s item=%s matKind=%s file=%s' % (
                        chunkID,
                        itemIndex,
                        matKind,
                        filename,
                    )
                )
            return False

        destrType = desc.get('type')

        # ----------------------------------------------------
        # Structures: fences, gates, small buildings
        # ----------------------------------------------------
        if destrType == DestructiblesCache.DESTR_TYPE_STRUCTURE:
            structKey = (chunkID, itemIndex, 'struct-any')

            if structKey in self._brokenDestructibles:
                return True

            # на малой скорости ломаем только заборы и мелкие постройки; дома - только с разгона.
            # easyOnly (контакт, найденный перпендикулярным зондом) - так же на любой скорости: дома зонд не ломает
            if speed <= 1.0 or easyOnly:
                fl = (desc.get('filename') or filename or '').lower()
                # Fence/gate-like objects should not trap offline tanks even
                # when their client descriptor has an unusually large module list.
                # Keep non-fence structures protected at low speed.
                easy = any(w in fl for w in ('fence', 'gate', 'gaf', 'wall', 'zabor'))
                if not easy and len(desc.get('modules') or {}) > 6:
                    return False

            done = self._destroyStructureModules(
                spaceID,
                chunkID,
                itemIndex,
                desc,
                matKind,
                speed
            )

            if done:
                self._brokenDestructibles.add(structKey)
                self._noteBroken(chunkID, itemIndex)
                LOG_DEBUG(
                    '[DEST] ram structure: chunk=%s item=%s matKind=%s spd=%.1f file=%s' % (
                        chunkID,
                        itemIndex,
                        matKind,
                        speed,
                        filename,
                    )
                )

            return done

        # ----------------------------------------------------
        # Fragile: boxes, small fences, crates, barrels, etc.
        # ----------------------------------------------------
        if destrType == DestructiblesCache.DESTR_TYPE_FRAGILE:
            key = (chunkID, itemIndex, destrType)

            if key in self._brokenDestructibles:
                return True

            try:
                data = int(
                    DestructiblesCache.encodeFragile(
                        itemIndex,
                        False
                    )
                )

                mgr.orderDestructibleDestroy(
                    chunkID,
                    destrType,
                    data,
                    True,
                    syncWithProjectile=False
                )

                self._forceFragileHidden(spaceID, chunkID, itemIndex)
                self._brokenDestructibles.add(key)
                self._noteBroken(chunkID, itemIndex)

                LOG_DEBUG(
                    '[DEST] ram fragile: chunk=%s item=%s spd=%.1f file=%s' % (
                        chunkID,
                        itemIndex,
                        speed,
                        filename,
                    )
                )

                return True

            except Exception:
                LOG_CURRENT_EXCEPTION()
                return False

        # ----------------------------------------------------
        # Falling atoms: poles, signs, lamps, etc.
        # ----------------------------------------------------
        if destrType == DestructiblesCache.DESTR_TYPE_FALLING_ATOM:
            key = (chunkID, itemIndex, destrType)

            if key in self._brokenDestructibles:
                return True

            try:
                data = int(
                    DestructiblesCache.encodeFallenColumn(
                        itemIndex,
                        fallYaw,
                        1
                    )
                )

                mgr.orderDestructibleDestroy(
                    chunkID,
                    destrType,
                    data,
                    True,
                    syncWithProjectile=False
                )

                self._brokenDestructibles.add(key)
                self._noteBroken(chunkID, itemIndex)

                LOG_DEBUG(
                    '[DEST] falling atom: chunk=%s item=%s spd=%.1f file=%s' % (
                        chunkID,
                        itemIndex,
                        speed,
                        filename,
                    )
                )

                return True

            except Exception:
                LOG_CURRENT_EXCEPTION()
                return False

        # ----------------------------------------------------
        # Trees
        # ----------------------------------------------------
        if destrType == DestructiblesCache.DESTR_TYPE_TREE:
            key = (chunkID, itemIndex, destrType)

            if key in self._brokenDestructibles:
                return True

            try:
                data = int(
                    DestructiblesCache.encodeFallenTree(
                        itemIndex,
                        fallYaw,
                        math.pi / 2.0,
                        1
                    )
                )

                mgr.orderDestructibleDestroy(
                    chunkID,
                    destrType,
                    data,
                    True,
                    syncWithProjectile=False
                )

                self._brokenDestructibles.add(key)
                self._noteBroken(chunkID, itemIndex)

                LOG_DEBUG(
                    '[DEST] tree: chunk=%s item=%s spd=%.1f file=%s' % (
                        chunkID,
                        itemIndex,
                        speed,
                        filename,
                    )
                )

                return True

            except Exception:
                LOG_CURRENT_EXCEPTION()
                return False

        return False

    # --------------------------------------------------------
    # Destructibles: proximity fallback
    # --------------------------------------------------------
    def _getChunkDestructibles(self, spaceID, chunkID):
        if getattr(self, '_chunkDestrCacheSpace', None) != spaceID:
            self._chunkDestrCache = {}
            self._chunkDestrCacheSpace = spaceID

        if chunkID in self._chunkDestrCache:
            return self._chunkDestrCache[chunkID]

        mgr = AreaDestructibles.g_destructiblesManager
        cache = AreaDestructibles.g_cache

        if mgr is None or cache is None:
            self._chunkDestrCache[chunkID] = []
            return []

        if not self._ensureChunkLoaded(spaceID, chunkID):
            return []

        try:
            filenames = BigWorld.wg_getChunkDestrFilenames(spaceID, chunkID)
        except Exception:
            filenames = None

        if filenames is None:
            self._chunkDestrCache[chunkID] = []
            return []

        try:
            count = len(filenames)
        except Exception:
            try:
                count = int(filenames)
            except Exception:
                count = 0

        if count <= 0:
            self._chunkDestrCache[chunkID] = []
            return []

        try:
            chunkMatrix = BigWorld.wg_getChunkMatrix(spaceID, chunkID)
        except Exception:
            chunkMatrix = None

        if chunkMatrix is None:
            return []

        items = []

        for idx in xrange(count):
            try:
                filename = BigWorld.wg_getDestructibleFilename(spaceID, chunkID, idx)

                if filename:
                    filename = re.sub('/lod./', '/lod0/', filename)

                desc = cache.getDescByFilename(filename)

                m = BigWorld.wg_getDestructibleMatrix(spaceID, chunkID, idx)
                if m is None:
                    continue

                try:
                    pos = chunkMatrix.applyPoint(m.translation)
                except Exception:
                    pos = chunkMatrix.translation + m.translation

                pos = Math.Vector3(pos.x, pos.y, pos.z)

                try:
                    scale = m.applyVector((0.0, 1.0, 0.0)).length
                except Exception:
                    scale = 1.0

                # Unknown destructible: try to hide it later.
                if desc is None:
                    items.append((idx, -1, pos, 1.1, filename))
                    continue

                dtype = desc.get('type')
                fname = (filename or '').lower()

                if dtype == DestructiblesCache.DESTR_TYPE_TREE:
                    radius = max(1.9, 1.2 * scale)

                elif dtype == DestructiblesCache.DESTR_TYPE_FALLING_ATOM:
                    radius = 1.35

                elif dtype == DestructiblesCache.DESTR_TYPE_FRAGILE:
                    radius = 1.45

                elif dtype == DestructiblesCache.DESTR_TYPE_STRUCTURE:
                    # Structures are dangerous to destroy by proximity.
                    # Allow only fence/gate-like objects.
                    if any(s in fname for s in ('fence', 'gate', 'gaf', 'wall', 'zabor')):
                        radius = 2.2
                    else:
                        continue

                else:
                    continue

                items.append((idx, dtype, pos, radius, filename))

            except Exception:
                pass

        self._chunkDestrCache[chunkID] = items
        return items

    @_perf.timed('p.destr')
    def _updateDestructibleProximity(self, vehicle, prevPos, newPos):
        speed = abs(self._curSpeed)

        if speed <= 1.0:
            return

        try:
            spaceID = vehicle.spaceID
        except Exception:
            return

        dx = newPos.x - prevPos.x
        dz = newPos.z - prevPos.z
        dy = newPos.y - prevPos.y

        dist = math.sqrt(dx * dx + dz * dz)

        if dist > 1e-4:
            fallYaw = math.atan2(dx / dist, dz / dist)
        else:
            fallYaw = self.yaw

        samples = max(1, int(dist / 1.0))

        chunkItems = {}

        for i in xrange(samples + 1):
            t = float(i) / float(samples)

            px = prevPos.x + dx * t
            py = prevPos.y + dy * t
            pz = prevPos.z + dz * t

            try:
                chunkID = DestructiblesCache.chunkIDFromPosition((px, py, pz))
            except Exception:
                continue

            if chunkID not in chunkItems:
                chunkItems[chunkID] = self._getChunkDestructibles(spaceID, chunkID)

            items = chunkItems[chunkID]

            if not items:
                continue

            # Не только центр танка: нос и передние углы корпуса (ломаем, когда объект задел передом, а не когда центр доехал)
            if STP_RAM_FRONT:
                hl = self.params.halfLength + STP_RAM_FRONT_EXTRA
                hw = self.params.halfWidth * 0.9
                sgn = 1.0 if self._curSpeed >= 0.0 else -1.0
                fx, fz = math.sin(self.yaw) * sgn, math.cos(self.yaw) * sgn
                rx, rz = fz, -fx
                pts = ((px, pz),
                       (px + fx * hl, pz + fz * hl),
                       (px + fx * hl + rx * hw, pz + fz * hl + rz * hw),
                       (px + fx * hl - rx * hw, pz + fz * hl - rz * hw))
            else:
                pts = ((px, pz),)

            for idx, dtype, pos, radius, filename in items:
                for (qx, qz) in pts:
                    ddx = qx - pos.x
                    ddz = qz - pos.z

                    if ddx * ddx + ddz * ddz <= radius * radius:
                        self._tryDestroyDestructible(
                            spaceID,
                            chunkID,
                            idx,
                            73,
                            fallYaw,
                            speed
                        )
                        break

    # --------------------------------------------------------
    # Main tick
    # --------------------------------------------------------
    def _tick(self):
        t0 = _perf.clock()
        try:
            self._tickImpl()
        finally:
            _perf.add('phys.bot' if self.isBot else 'phys.player', _perf.clock() - t0)

    def _tickImpl(self):
        now = BigWorld.time()
        dt = now - self._lastTime
        self._lastTime = now

        self._cbID = BigWorld.callback(getattr(self, 'tickInterval', 0.0), self._tick)

        if dt <= 0.0:
            return

        capDt = 0.1
        if self.isBot and BOT_SMOOTH:
            capDt = max(0.1, getattr(self, 'tickInterval', 0.0) * 1.6)
        if dt > capDt:
            dt = capDt

        try:
            vehicle = self.vehicle
        except Exception:
            self.stop()
            return

        if vehicle is None or not getattr(vehicle, 'isStarted', False):
            self.stop()
            return

        if not getattr(self, '_groundReady', True):
            return

        prevPos = Math.Vector3(self.pos)
        prevYaw = self.yaw
        prevEmbedded = self._embedded

        self._updateMovement(vehicle, dt)
        self._updateCollision(vehicle, dt)
        self._solidGuard(vehicle, prevPos, prevYaw, prevEmbedded)
        self._updateJam(vehicle, dt)
        self._updateDestructibleProximity(vehicle, prevPos, self.pos)
        self._updateGroundFollow(vehicle, dt)
        self._updateTilt(vehicle, dt)

        self._lastSpeed = self._curSpeed
        self._apply(vehicle)
        _updateVehicleEngineSound(vehicle)

    # --------------------------------------------------------
    # Movement
    # --------------------------------------------------------
    @_perf.timed('p.move')
    def _updateMovement(self, vehicle, dt):
        # во время обратного отсчёта танк стоит на месте (башня/прицел работают)
        flags = 0 if isPrebattle() else self.flags

        moveDir = 0
        if flags & 1:
            moveDir = 1
        elif flags & 2:
            moveDir = -1

        rotDir = 0
        if flags & 4:
            rotDir = -1
        elif flags & 8:
            rotDir = 1

        params = self.params

        # модули, экипаж и оборудование (devices.py): множители хода; разбитая гусеница/двигатель - машина стоит
        maxFwd, maxBack = params.maxFwd, params.maxBack
        accelK, rotK = 1.0, 1.0
        try:
            from gui.mods.offhangar import devices as _dev
            _f = _dev.getFactors(vehicle.id)
            if _f is not None:
                if not _f['move']:
                    moveDir = 0
                    rotDir = 0
                maxFwd *= _f['fwd']
                maxBack *= _f['back']
                accelK = _f['accel']
                rotK = _f['rot']
        except Exception:
            pass

        if moveDir > 0:
            targetSpeed = maxFwd
        elif moveDir < 0:
            targetSpeed = -maxBack
        else:
            targetSpeed = 0.0

        cur = self._curSpeed
        if moveDir == 0:
            accelRate = params.brake
        elif (moveDir > 0 and cur < -0.05) or (moveDir < 0 and cur > 0.05):
            # едем в одну сторону, жмём в другую - сначала тормозим
            accelRate = params.brake
        else:
            ref = maxFwd if moveDir > 0 else maxBack
            frac = min(1.0, abs(cur) / ref) if ref > 0.0 else 1.0
            accelRate = params.accel * accelK * max(0.15, 1.0 - ACCEL_FALLOFF * frac)
            if moveDir < 0:
                accelRate *= REVERSE_ACCEL_FACTOR

        self._wantMove = moveDir != 0
        grade = self._gradeAlong(vehicle)
        steep = abs(grade) >= STP_SLIDE_GRADE
        if moveDir == 0 and steep:
            pass        # на крутом склоне стояночный тормоз не держит - танк сползает (гравитация ниже)
        else:
            self._curSpeed = self._approach(self._curSpeed, targetSpeed, accelRate, dt)
        self._curSpeed -= self._curSpeed * params.terrainDrag * dt
        if abs(grade) > 0.04:
            # гравитация вдоль корпуса: вверх тормозит, вниз разгоняет
            self._curSpeed -= 9.81 * math.sin(math.atan(grade)) * STP_SLOPE_GRAVITY * dt

        if self._curSpeed > 0.0:
            self._curSpeed = min(self._curSpeed, maxFwd)
        elif self._curSpeed < 0.0:
            self._curSpeed = max(self._curSpeed, -maxBack)

        if abs(self._curSpeed) < 1e-3:
            self._curSpeed = 0.0

        targetRot = rotDir * params.rotRate * rotK

        if rotDir != 0:
            rotRate = params.rotAccel
        else:
            rotRate = params.rotDecel

        self._curRotSpeed = self._approach(self._curRotSpeed, targetRot, rotRate, dt)

        if abs(self._curRotSpeed) < 1e-3:
            self._curRotSpeed = 0.0

        self.yaw += self._curRotSpeed * dt

    # --------------------------------------------------------
    # Collision
    # --------------------------------------------------------
    def _circles(self, pos, yaw):
        """Корпус как три окружности вдоль оси - для столкновений машин друг с другом."""
        p = self.params
        r = max(1.0, p.halfWidth * 1.05)
        off = max(0.0, p.halfLength - r)
        fx = math.sin(yaw)
        fz = math.cos(yaw)
        if off < 0.3:
            return [(pos.x, pos.z, r)]
        return [(pos.x + fx * off * k, pos.z + fz * off * k, r) for k in (-1.0, 0.0, 1.0)]

    def _separateFromVehicles(self, vehicle):
        """Машины твёрдые: выталкиваем себя из чужих корпусов и гасим скорость при упоре."""
        try:
            ghost = self.isBot and BigWorld.time() < getattr(self, 'ghostUntil', 0.0)
            others = [(op, vid in _CORPSES) for vid, op in _simplePhysicsByVehID.items()
                      if op is not self and getattr(op, 'pos', None) is not None
                      and not (ghost and (getattr(op, 'isBot', False) or vid in _CORPSES))]
        except Exception:
            return
        if not others:
            return
        pushed = False
        for _ in range(2):
            mine = self._circles(self.pos, self.yaw)
            moveX = moveZ = 0.0
            for op, isWreck in others:
                try:
                    theirs = op._circles(op.pos, op.yaw)
                except Exception:
                    continue
                if abs(op.pos.x - self.pos.x) > 25.0 or abs(op.pos.z - self.pos.z) > 25.0:
                    continue
                for (ax, az, ar) in mine:
                    for (bx, bz, br) in theirs:
                        dx = ax - bx
                        dz = az - bz
                        d = math.sqrt(dx * dx + dz * dz)
                        minD = ar + br
                        if d >= minD:
                            continue
                        if d < 1e-4:
                            dx, dz, d = 1.0, 0.0, 1.0
                        k = (minD - d) / d
                        w = 1.0 if isWreck else 0.5     # остов не двигается - выталкиваем себя целиком, живую машину - пополам
                        moveX += dx * k * w
                        moveZ += dz * k * w
            if abs(moveX) < 1e-4 and abs(moveZ) < 1e-4:
                break
            mv = math.sqrt(moveX * moveX + moveZ * moveZ)
            if mv > 1.2:                      # за шаг не выталкиваем дальше 1.2 м (иначе при касании нескольких кругов машину "выстреливает")
                moveX *= 1.2 / mv
                moveZ *= 1.2 / mv
            self.pos = Math.Vector3(self.pos.x + moveX, self.pos.y, self.pos.z + moveZ)
            pushed = True
        if pushed:
            self._curSpeed *= 0.5

    def _hullPoints(self, pos, yaw):
        """Углы и середины сторон корпуса (x, z)."""
        p = self.params
        hl = p.halfLength
        hw = p.halfWidth
        fx, fz = math.sin(yaw), math.cos(yaw)
        rx, rz = fz, -fx
        res = []
        for lo, la in ((hl, hw), (hl, -hw), (-hl, hw), (-hl, -hw), (hl, 0.0), (-hl, 0.0), (0.0, hw), (0.0, -hw)):
            res.append((pos.x + fx * lo + rx * la, pos.z + fz * lo + rz * la))
        return res

    def _isEmbedded(self, spaceID, pos, yaw):
        """True, если от центра корпуса до его контура лучом находится статичная стена/здание."""
        try:
            g = self._groundHeight(spaceID, pos.x, pos.y, pos.z, pos.y)
            y = g + STP_EMBED_HEIGHT
            start = Math.Vector3(pos.x, y, pos.z)
            for (x, z) in self._hullPoints(pos, yaw):
                res = BigWorld.wg_collideSegment(spaceID, start, Math.Vector3(x, y, z), 0)
                if res is None or res[0] is None:
                    continue
                n = self._asVector3(res[1]) if len(res) > 1 else None
                if n is not None and abs(n.y) > STP_COLLIDE_GROUND_NORMAL_IGNORE:
                    continue
                # забор/ящик/дерево внутри корпуса - не "стена": ломаем его, а не откатываем шаг
                if self._hitIsBreakable(spaceID, start, Math.Vector3(x, y, z), res):
                    continue
                return True
        except Exception:
            LOG_CURRENT_EXCEPTION()
        return False

    def _tryDestroySolidHit(self, spaceID, start, hitPt, yaw):
        """Статичное попадание луча: проверяем перпендикулярным зондом, не забор/ящик/дерево ли это. Если да - ломаем его
        (или он уже сломан) и возвращаем True: такой луч препятствием не считается. Результат кэшируется по ячейке 0.8 м."""
        if not STP_SOLID_PROBE:
            return False
        try:
            cell = (int(round(hitPt.x / 0.8)), int(round(hitPt.y / 0.8)), int(round(hitPt.z / 0.8)))
            cache = getattr(self, '_solidCache', None)
            if cache is None:
                cache = self._solidCache = {}
            now = BigWorld.time()
            ent = cache.get(cell)
            if ent is not None and ent[0] > now:
                return ent[1]

            ok = False
            mi = _probeDestructibleAt(spaceID, start, hitPt)
            if mi is not None:
                chunkID, itemIndex, matKind, fname = mi
                try:
                    key = (int(chunkID), int(itemIndex))
                except Exception:
                    key = None
                if key is not None and key in _BROKEN_ITEMS:
                    ok = True
                elif key is not None:
                    ok = bool(self._tryDestroyDestructible(spaceID, chunkID, itemIndex, matKind, yaw, abs(self._curSpeed), True))
                if _SOLID_PROBE_LOG['ok'] < 30:
                    _SOLID_PROBE_LOG['ok'] += 1
                    LOG_DEBUG('[DEST] solid hit probe: matKind=%s file=%s chunk=%s item=%s -> %s' % (
                        matKind, fname, chunkID, itemIndex, 'broken/ignored' if ok else 'stays solid'))
            elif _SOLID_PROBE_LOG['none'] < 10:
                _SOLID_PROBE_LOG['none'] += 1
                LOG_DEBUG('[DEST] solid hit probe: nothing resolved at (%.1f, %.1f, %.1f)' % (hitPt.x, hitPt.y, hitPt.z))

            cache[cell] = (now + (0.4 if ok else 0.5), ok)
            if len(cache) > 64:
                cache.clear()
            return ok
        except Exception:
            LOG_CURRENT_EXCEPTION()
            return False

    def _hitIsBreakable(self, spaceID, start, end, res):
        """True, если статичное попадание луча принадлежит разрушаемому объекту, который сломан или ломается сейчас."""
        try:
            rd = BigWorld.wg_collideSegment(spaceID, start, end, 128)
            if rd is not None and rd[0] is not None and len(rd) > 5:
                tStat = (res[0] - start).length
                tDest = (rd[0] - start).length
                if tDest <= tStat + STP_DEST_NEAR_EPS:
                    key = (int(rd[5]), int(rd[4]))
                    if key in _BROKEN_ITEMS:
                        return True
                    if self._tryDestroyDestructible(
                            spaceID, rd[5], rd[4], rd[2], self.yaw, abs(self._curSpeed)):
                        return True
            # прямой луч не опознал объект ("кожа" коллизии) - пробуем перпендикулярным зондом, как в 0.8.2
            return self._tryDestroySolidHit(spaceID, start, res[0], self.yaw)
        except Exception:
            LOG_CURRENT_EXCEPTION()
            return False

    @_perf.timed('p.solid')
    def _solidGuard(self, vehicle, prevPos, prevYaw, prevEmbedded):
        """После шага (в т.ч. поворота) корпус не должен оказаться в доме/заборе: откатываем шаг."""
        if not STP_EMBED_GUARD:
            return
        if BigWorld.time() < max(self._lowPassUntil, self._jamPassUntil):
            self._embedded = False
            return
        if abs(self.yaw - prevYaw) < 1e-5 and (self.pos - prevPos).length < 1e-4:
            return
        try:
            spaceID = vehicle.spaceID
        except Exception:
            return
        now = self._isEmbedded(spaceID, self.pos, self.yaw)
        if now and not prevEmbedded:
            # пробуем оставить только движение (без поворота), потом только поворот, иначе откат
            if abs(self.yaw - prevYaw) > 1e-5 and not self._isEmbedded(spaceID, self.pos, prevYaw):
                self.yaw = prevYaw
                self._curRotSpeed = 0.0
                now = False
            elif (self.pos - prevPos).length > 1e-4 and not self._isEmbedded(spaceID, prevPos, self.yaw):
                self.pos = Math.Vector3(prevPos)
                self._curSpeed *= 0.3
                now = False
            else:
                self.pos = Math.Vector3(prevPos)
                self.yaw = prevYaw
                self._curSpeed = 0.0
                self._curRotSpeed = 0.0
                now = False
        self._embedded = now

    @_perf.timed('p.jam')
    def _updateJam(self, vehicle, dt):
        """Антизастревание: газ нажат, а танк не движется (сломанный забор/столб остался твёрдым, бордюр, угол) - ломаем
        то, во что упёрлись, а если это не разрушаемое - на короткое время пропускаем низкие препятствия."""
        if not self._wantMove or self._jamPos is None:
            self._jamT = 0.0
            self._jamPos = Math.Vector3(self.pos)
            return
        self._jamT += dt
        if self._jamT < STP_JAM_TIME:
            return
        dx, dz = self.pos.x - self._jamPos.x, self.pos.z - self._jamPos.z
        moved = math.sqrt(dx * dx + dz * dz)
        self._jamT = 0.0
        self._jamPos = Math.Vector3(self.pos)
        if moved >= STP_JAM_MOVE:
            return
        now = BigWorld.time()
        info = self._lastBlock
        broke = False
        try:
            if info is not None and now - info[0] < 1.0:
                _, chunkID, itemIndex, matKind, hh = info
                if int(matKind) <= DESTRUCTIBLE_MATKIND.MAX or _isDestructibleItem(vehicle.spaceID, chunkID, itemIndex, matKind):
                    try:
                        self._tryDestroyDestructible(vehicle.spaceID, chunkID, itemIndex, matKind, self.yaw, 6.0)
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                    _BROKEN_ITEMS.add((int(chunkID), int(itemIndex)))   # даже если визуально не сломался - больше не держит
                    broke = True
                    self._jamPassUntil = now + STP_JAM_PASS_TIME
                LOG_DEBUG('[JAM] stuck: moved=%.2f m, block chunk=%s item=%s matKind=%s h=%.2f destructible=%s -> %s' % (
                    moved, chunkID, itemIndex, matKind, hh, broke, 'object released' if broke else 'low pass'))
            else:
                LOG_DEBUG('[JAM] stuck: moved=%.2f m, no recorded obstacle (embed/slope?) -> low pass' % moved)
            if not broke:
                geom = getattr(self, '_lastBlockGeom', None)
                if geom is not None and self._tryDestroySolidHit(vehicle.spaceID, geom[0], geom[1], self.yaw):
                    broke = True
                    self._jamPassUntil = now + STP_JAM_PASS_TIME
                    LOG_DEBUG('[JAM] block resolved by the perpendicular probe -> object released')
        except Exception:
            LOG_CURRENT_EXCEPTION()
        if not broke:
            self._lowPassUntil = now + STP_JAM_PASS_TIME
        self._blockedTime = 0.0

    def _gradeAlong(self, vehicle):
        """Уклон вдоль курса корпуса (tg; + = нос выше кормы), сглажен, считается не чаще 10 раз в секунду."""
        now = BigWorld.time()
        if now - self._gradeT < 0.1:
            return self._grade
        self._gradeT = now
        try:
            spaceID = vehicle.spaceID
            hl = max(1.5, self.params.halfLength)
            fx, fz = math.sin(self.yaw) * hl, math.cos(self.yaw) * hl
            py = self.pos.y
            g0 = self._groundHeight(spaceID, self.pos.x, py, self.pos.z, py)
            gf = self._groundHeight(spaceID, self.pos.x + fx, g0, self.pos.z + fz, g0)
            gr = self._groundHeight(spaceID, self.pos.x - fx, g0, self.pos.z - fz, g0)
            raw = max(-1.5, min(1.5, (gf - gr) / (2.0 * hl)))
            self._grade += (raw - self._grade) * 0.5
        except Exception:
            pass
        return self._grade

    def _slopeBlocked(self, spaceID, fwd, sgn):
        """Впереди слишком крутой подъём (гора) или резкий уступ: танк туда не заезжает."""
        L = max(STP_SLOPE_LOOKAHEAD_MIN, self.params.halfLength * 1.1)
        k = 1.25 if BigWorld.time() < self._lowPassUntil else 1.0
        g0 = self._groundHeight(spaceID, self.pos.x, self.pos.y, self.pos.z, self.pos.y)
        prev = g0
        n = max(1, STP_SLOPE_SAMPLES)
        seg = L / n
        for i in xrange(1, n + 1):
            d = seg * i * sgn
            gi = self._groundHeight(spaceID, self.pos.x + fwd.x * d, prev, self.pos.z + fwd.z * d, prev)
            if (gi - prev) / seg > STP_SLOPE_STEP_GRAD * k:
                return True
            prev = gi
        return (prev - g0) / L > STP_MAX_SLOPE * k

    @_perf.timed('p.collide')
    def _updateCollision(self, vehicle, dt):
        if abs(self._curSpeed) <= 1e-4:
            self._blockedTime = 0.0
            self._separateFromVehicles(vehicle)
            return

        fwd = Math.Vector3(
            math.sin(self.yaw),
            0.0,
            math.cos(self.yaw)
        )

        self._dtc = dt
        try:
            if self._slopeBlocked(vehicle.spaceID, fwd, 1.0 if self._curSpeed > 0.0 else -1.0):
                self._curSpeed *= 0.3
                if abs(self._curSpeed) < 0.3:
                    self._curSpeed = 0.0
                self._separateFromVehicles(vehicle)
                return
        except Exception:
            LOG_CURRENT_EXCEPTION()

        candidate = self.pos + fwd * (self._curSpeed * dt)

        try:
            spaceID = vehicle.spaceID
            candidate.y = self._groundHeight(
                spaceID,
                candidate.x,
                self.pos.y,
                candidate.z,
                self.pos.y
            )
        except Exception:
            pass

        self._newBreaks = 0
        self.pos = self._checkCollision(vehicle, candidate)
        if self._newBreaks:
            # проломили забор/ящик - чуть теряем скорость (не больше трёх объектов за шаг)
            self._curSpeed *= 0.94 ** min(self._newBreaks, 3)
            self._newBreaks = 0
        self._separateFromVehicles(vehicle)

    def _driveSmooth(self, spaceID, dirX, dirZ, look, columns):
        """Земля вдоль курса поднимается плавно на всех колонках (центр и борта)? Тогда это холм, а не стена (приём из 0.8.2).
        Шаг по ходу ~look/6, подъём круче STP_DRIVE_RISE на метр или отсутствие земли = не плавно."""
        px, pz, py = self.pos.x, self.pos.z, self.pos.y
        n = 6
        seg = look / n
        rx, rz = dirZ, -dirX
        for lat in columns:
            prev = None
            for i in xrange(n + 1):
                d = seg * i
                x = px + dirX * d + rx * lat
                z = pz + dirZ * d + rz * lat
                c = BigWorld.wg_collideSegment(spaceID, Math.Vector3(x, py + 12.0, z), Math.Vector3(x, py - 8.0, z), 128)
                if c is None or c[0] is None:
                    return False
                y = float(c[0].y)
                if prev is not None and (y - prev) > seg * STP_DRIVE_RISE:
                    return False
                prev = y
        return True

    def _checkCollision(self, vehicle, candidate, depth=0):
        try:
            spaceID = vehicle.spaceID
        except Exception:
            return candidate

        dx = candidate.x - self.pos.x
        dz = candidate.z - self.pos.z

        dist = math.sqrt(dx * dx + dz * dz)
        if dist < 1e-4:
            return candidate

        dirX = dx / dist
        dirZ = dz / dist

        fwd = Math.Vector3(dirX, 0.0, dirZ)
        right = Math.Vector3(dirZ, 0.0, -dirX)

        params = self.params
        probeDist = dist + STP_COLLIDE_MARGIN

        if STP_DRIVE_GUARD and depth == 0 and spaceID is not None and not (self.isBot and getattr(self, 'lite', 0) >= 2):
            try:
                ahead = min(1.2, max(0.4, abs(self._curSpeed) * self._dtc * 2.0))
                look = params.halfLength + ahead
                if self.isBot:
                    cols = (0.0,)
                else:
                    hw = params.halfWidth * 0.9
                    cols = (0.0, -hw, hw)
                if self._driveSmooth(spaceID, dirX, dirZ, look, cols):
                    self._blockedTime = 0.0
                    return candidate
            except Exception:
                LOG_CURRENT_EXCEPTION()

        baseYStart = self.pos.y
        baseYEnd = candidate.y

        if spaceID is not None:
            g0 = self._groundHeight(spaceID, self.pos.x, self.pos.y, self.pos.z, self.pos.y)
            g1 = self._groundHeight(spaceID, candidate.x, g0, candidate.z, g0)

            lift = max(STP_COLLIDE_BASE_LIFT_MIN, params.clearance * 0.65)

            baseYStart = g0 + lift
            baseYEnd = g1 + lift

        offsets = (
            (0.0, 0.0),
            (params.halfLength, 0.0),
            (-params.halfLength, 0.0),
            (0.0, params.halfWidth),
            (0.0, -params.halfWidth),
            (params.halfLength, params.halfWidth),
            (params.halfLength, -params.halfWidth),
            (-params.halfLength, params.halfWidth),
            (-params.halfLength, -params.halfWidth),
        )
        heights = STP_COLLIDE_HEIGHTS
        lite = getattr(self, 'lite', 0) if self.isBot else 0
        if lite >= 2:
            # далёкий бот: только передние точки и две высоты (в 5-10 раз меньше лучей)
            offsets = (
                (params.halfLength, 0.0),
                (params.halfLength, params.halfWidth),
                (params.halfLength, -params.halfWidth),
                (0.0, 0.0),
            )
            heights = STP_COLLIDE_HEIGHTS_LITE
        elif lite == 1:
            # ближний бот: только передние и боковые точки (по ходу движения) и три высоты
            offsets = (
                (params.halfLength, 0.0),
                (0.0, params.halfWidth),
                (0.0, -params.halfWidth),
                (params.halfLength, params.halfWidth),
                (params.halfLength, -params.halfWidth),
            )
            heights = STP_COLLIDE_HEIGHTS_NEAR

        bestT = probeDist
        bestNormal = None
        lowPass = BigWorld.time() < self._lowPassUntil
        jamPass = BigWorld.time() < self._jamPassUntil
        blockHeights = []

        tanP = tanR = 0.0
        if STP_RAY_TILT:
            try:
                tanP = math.tan(max(-0.6, min(0.6, self.pitch)))
                tanR = math.tan(max(-0.45, min(0.45, self.roll)))
            except Exception:
                tanP = tanR = 0.0

        for longOff, latOff in offsets:
            baseX = self.pos.x + fwd.x * longOff + right.x * latOff
            baseZ = self.pos.z + fwd.z * longOff + right.z * latOff
            # земля у этой точки корпуса выше/ниже, чем под центром: поднимаем лучи по наклону корпуса
            # (иначе на подъёме передние нижние лучи стартуют под землёй и "упираются" в склон)
            tiltOff = tanP * longOff - tanR * latOff
            if tiltOff > 1.6:
                tiltOff = 1.6
            elif tiltOff < -1.6:
                tiltOff = -1.6

            for h in heights:
                if lowPass and h <= STP_LOW_OBSTACLE_H:
                    continue
                start = Math.Vector3(baseX, baseYStart + h + tiltOff, baseZ)
                end = Math.Vector3(
                    baseX + dirX * probeDist,
                    baseYEnd + h + tiltOff,
                    baseZ + dirZ * probeDist
                )

                res = None
                try:
                    # Static collision
                    res = BigWorld.wg_collideSegment(spaceID, start, end, 0)

                    # Destructibles collision
                    if lite == 1 and h != 0.75:
                        res_dest = None          # ближние боты: разрушаемые проверяем на одной высоте (лучей вдвое меньше)
                    else:
                        res_dest = BigWorld.wg_collideSegment(spaceID, start, end, 128)

                    tStat = None
                    if res is not None and res[0] is not None:
                        tStat = (res[0].x - start.x) * dirX + (res[0].z - start.z) * dirZ

                    if res_dest is not None and len(res_dest) > 5 and res_dest[0] is not None:
                        tDest = (res_dest[0].x - start.x) * dirX + (res_dest[0].z - start.z) * dirZ

                        # разрушаемый объект учитываем, только если за ним нет более близкой статики
                        # (стена перед забором остаётся стеной)
                        if tStat is None or tDest <= tStat + STP_DEST_NEAR_EPS:
                            destKey = (int(res_dest[5]), int(res_dest[4]))

                            if destKey in _BROKEN_ITEMS or self._tryDestroyDestructible(
                                spaceID,
                                res_dest[5],
                                res_dest[4],
                                res_dest[2],
                                math.atan2(dirX, dirZ),
                                abs(self._curSpeed)
                            ):
                                # сломан (или ломается прямо сейчас) - луч его не учитывает
                                continue

                    # статичное попадание в уже сломанный объект: его коллизия движком могла остаться
                    if res is not None and len(res) > 5:
                        try:
                            if (int(res[5]), int(res[4])) in _BROKEN_ITEMS:
                                continue
                        except Exception:
                            pass

                except Exception:
                    LOG_CURRENT_EXCEPTION()
                    res = None

                if res is None:
                    continue

                hitPoint = res[0]
                if hitPoint is None:
                    continue

                n = None
                if len(res) > 1:
                    n = self._asVector3(res[1])

                if n is not None and abs(n.y) > STP_COLLIDE_GROUND_NORMAL_IGNORE:
                    continue

                if jamPass and len(res) > 5:
                    try:
                        if int(res[2]) <= DESTRUCTIBLE_MATKIND.MAX or (int(res[5]), int(res[4])) in _BROKEN_ITEMS:
                            continue        # застряли на разрушаемом (забор, столб) - не держит
                    except Exception:
                        pass

                hx = hitPoint.x - start.x
                hz = hitPoint.z - start.z

                t = hx * dirX + hz * dirZ
                if t < 0.0:
                    continue

                if t < probeDist and self._tryDestroySolidHit(spaceID, start, hitPoint, math.atan2(dirX, dirZ)):
                    continue          # это забор/ящик/дерево: ломаем (или он уже сломан) - препятствием луч не считается

                if STP_STEP_CLIMB and t < probeDist and spaceID is not None:
                    # низкий уступ: если сверху (в 0.35 м за точкой касания) земля не выше STP_STEP_MAX над землёй под танком -
                    # это бордюр/доски/рельс, танк заезжает (высоту потом подхватывает слежение за землёй)
                    try:
                        groundHere = baseYStart - lift
                        if hitPoint.y - groundHere <= STP_STEP_MAX + 0.25:
                            # луч сверху с большой высоты: у стены/дома первым будет верх (крыша), а не пол внутри
                            tx, tz = hitPoint.x + dirX * 0.35, hitPoint.z + dirZ * 0.35
                            top = self._terrainHit(spaceID, Math.Vector3(tx, hitPoint.y + 12.0, tz), Math.Vector3(tx, hitPoint.y - 2.0, tz))
                            if top is not None and top[0] is not None and float(top[0].y) - groundHere <= STP_STEP_MAX:
                                continue
                    except Exception:
                        LOG_CURRENT_EXCEPTION()

                if t < probeDist:
                    blockHeights.append(h)

                if t < bestT:
                    bestT = t
                    bestNormal = n
                    try:
                        if len(res) > 5:
                            self._lastBlock = (BigWorld.time(), int(res[5]), int(res[4]), int(res[2]), h)
                        self._lastBlockGeom = (Math.Vector3(start), Math.Vector3(hitPoint))
                    except Exception:
                        pass

        if depth == 0 and bestT < dist and blockHeights and max(blockHeights) <= STP_LOW_SOFT_H:
            # как в 0.8.2: задели только нижние лучи (препятствие ниже ~1 м: камень, бордюр, основание забора) - танк его проезжает
            self._blockedTime = 0.0
            return candidate

        if depth == 0:
            if bestT < dist:
                self._blockedTime += self._dtc
                if (not lowPass and self._blockedTime >= STP_STUCK_FORCE_TIME and blockHeights
                        and max(blockHeights) <= STP_LOW_OBSTACLE_H):
                    # упёрлись в низкое препятствие (забор/куст/камень, который не получилось сломать) - проезжаем его
                    self._lowPassUntil = BigWorld.time() + STP_LOW_PASS_TIME
                    self._blockedTime = 0.0
                    LOG_DEBUG('[DEST] low obstacle pass: pos=(%.1f, %.1f) maxHit=%.2f spd=%.1f' % (
                        self.pos.x, self.pos.z, max(blockHeights), self._curSpeed))
                    return candidate
            else:
                self._blockedTime = 0.0

        if bestT >= dist:
            return candidate

        if bestNormal is not None:
            nn = Math.Vector3(bestNormal.x, 0.0, bestNormal.z)
            if nn.length > 1e-5:
                nn.normalise()
                if nn.x * dirX + nn.z * dirZ > 0.0:      # у тонких объектов (заборы) нормаль может смотреть "от нас"
                    nn = nn * -1.0
                bestNormal = nn

        if bestT <= STP_COLLIDE_MARGIN:
            self._curSpeed *= 0.25
            if bestNormal is not None and not depth:
                # скользим вдоль препятствия, а не упираемся намертво
                dotv = dirX * bestNormal.x + dirZ * bestNormal.z
                tx, tz = dirX - bestNormal.x * dotv, dirZ - bestNormal.z * dotv
                tl = math.sqrt(tx * tx + tz * tz)
                if tl > 0.15:
                    step = dist * 0.8
                    cand2 = Math.Vector3(self.pos.x + tx / tl * step, candidate.y, self.pos.z + tz / tl * step)
                    r2 = self._checkCollision(vehicle, cand2, 1)
                    if (r2 - self.pos).length > step * 0.3:
                        return r2
            if not depth:
                # упёрлись в упор / в угол: как в 0.8.2 пробуем скользить под углом к курсу, чтобы не стоять намертво
                base = math.atan2(dirX, dirZ)
                for da in STP_DEFLECT_ANGLES:
                    a = base + da
                    step = dist * 0.7
                    cand2 = Math.Vector3(self.pos.x + math.sin(a) * step, candidate.y, self.pos.z + math.cos(a) * step)
                    r2 = self._checkCollision(vehicle, cand2, 1)
                    if (r2 - self.pos).length > step * 0.5:
                        return r2
            return self.pos

        allowed = max(0.0, bestT - STP_COLLIDE_MARGIN)

        newPos = Math.Vector3(
            self.pos.x + dirX * allowed,
            self.pos.y,
            self.pos.z + dirZ * allowed
        )

        remaining = candidate - newPos

        if bestNormal is not None:
            n = Math.Vector3(bestNormal.x, 0.0, bestNormal.z)

            if n.length > 1e-5:
                n.normalise()

                dot = self._dotV3(remaining, n)
                if dot < 0.0:
                    remaining = remaining - n * dot

                newPos = newPos + remaining * 0.5

            self._curSpeed *= 0.7
        else:
            self._curSpeed *= 0.4

        return newPos

    # --------------------------------------------------------
    # Ground follow
    # --------------------------------------------------------
    @_perf.timed('p.ground')
    def _updateGroundFollow(self, vehicle, dt):
        try:
            spaceID = vehicle.spaceID
        except Exception:
            return

        targetY = self._groundHeight(
            spaceID,
            self.pos.x,
            self.pos.y,
            self.pos.z,
            self.lastGroundY
        )

        self.lastGroundY = targetY

        if STP_SUPPORT_POINTS:
            # центр корпуса не ниже хорды по краям (кочка/ложбина), значения с прошлого кадра (_computeTilt)
            sup = self.__dict__.get('_supportY')
            if sup is not None and sup > targetY and sup - targetY < 1.5:
                targetY = sup

        if STP_USE_SMOOTH_GROUND and targetY <= self.pos.y:
            alpha = 1.0 - math.exp(-STP_GROUND_FOLLOW_RATE * dt)
            self.pos.y += (targetY - self.pos.y) * alpha          # вниз - плавно
        else:
            self.pos.y = targetY                                   # вверх - сразу, иначе корпус уходит в землю на подъёме

        self.verticalSpeed = 0.0

    # --------------------------------------------------------
    # Tilt
    # --------------------------------------------------------
    def _computeTilt(self, vehicle):
        try:
            spaceID = vehicle.spaceID
        except Exception:
            return 0.0, 0.0

        params = self.params

        cosY = math.cos(self.yaw)
        sinY = math.sin(self.yaw)

        fx = sinY * params.halfLength
        fz = cosY * params.halfLength

        rx = cosY * params.halfWidth
        rz = -sinY * params.halfWidth

        px = self.pos.x
        pz = self.pos.z
        py = self.pos.y

        # точки опоры: (a, b, высота земли), a - доля длины (+1 нос), b - доля ширины (-1 левый борт, +1 правый)
        pts = None
        if STP_SUPPORT_POINTS and not (self.isBot and getattr(self, 'lite', 0) >= 1):
            # игрок и ближние боты: вся площадь опоры гусениц - углы, четверти длины и середины бортов
            gh = self._groundHeight
            pts = []
            for a in (1.0, 0.5, 0.0, -0.5, -1.0):
                for b in (-1.0, 1.0):
                    pts.append((a, b, gh(spaceID, px + fx * a + rx * b, py, pz + fz * a + rz * b, py)))
            frontY = (pts[0][2] + pts[1][2]) * 0.5
            rearY = (pts[8][2] + pts[9][2]) * 0.5
            leftY = sum(q[2] * (2.0 if q[0] == 0.0 else 1.0) for q in pts if q[1] < 0) / 6.0
            rightY = sum(q[2] * (2.0 if q[0] == 0.0 else 1.0) for q in pts if q[1] > 0) / 6.0
        else:
            frontY = self._groundHeight(spaceID, px + fx, py, pz + fz, py)
            rearY = self._groundHeight(spaceID, px - fx, py, pz - fz, py)
            leftY = self._groundHeight(spaceID, px - rx, py, pz - rz, py)
            rightY = self._groundHeight(spaceID, px + rx, py, pz + rz, py)

        pitch = math.atan2(frontY - rearY, 2.0 * params.halfLength)
        roll = math.atan2(leftY - rightY, 2.0 * params.halfWidth)

        if STP_SUPPORT_POINTS:
            if pts is None:
                pts = [(1.0, 0.0, frontY), (-1.0, 0.0, rearY), (0.0, -1.0, leftY), (0.0, 1.0, rightY)]
            # Высота центра, при которой каждая точка опоры не ниже земли для плоскости корпуса с наклоном:
            # и ограниченным (STP_MAX_*), и текущим (наклон догоняет цель с задержкой STP_*_RESPONSE).
            need = -1.0e9
            for (cp, cr) in ((self._clamp(-STP_MAX_PITCH, STP_MAX_PITCH, pitch), self._clamp(-STP_MAX_ROLL, STP_MAX_ROLL, roll)),
                             (self.pitch, self.roll)):
                sl = math.tan(cp) * params.halfLength
                sw = math.tan(cr) * params.halfWidth
                for (a, b, y) in pts:
                    v = y - (a * sl - b * sw)
                    if v > need:
                        need = v
            self._supportY = need + STP_SUPPORT_MARGIN

        if self._tiltLogCount < 3:
            self._tiltLogCount += 1
            LOG_DEBUG(
                'SimpleTankPhysics tilt #%d: front=%.2f rear=%.2f left=%.2f right=%.2f pitch=%.3f roll=%.3f' % (
                    self._tiltLogCount,
                    frontY,
                    rearY,
                    leftY,
                    rightY,
                    pitch,
                    roll,
                )
            )

        return pitch, roll

    @_perf.timed('p.tilt')
    def _updateTilt(self, vehicle, dt):
        try:
            targetPitch, targetRoll = self._computeTilt(vehicle)

            targetPitch = self._clamp(-STP_MAX_PITCH, STP_MAX_PITCH, targetPitch)
            targetRoll = self._clamp(-STP_MAX_ROLL, STP_MAX_ROLL, targetRoll)

            if STP_PITCH_RESPONSE <= 0.0:
                self.pitch = targetPitch
            else:
                pitchAlpha = 1.0 - math.exp(-STP_PITCH_RESPONSE * dt)
                self.pitch += (targetPitch - self.pitch) * pitchAlpha

            if STP_ROLL_RESPONSE <= 0.0:
                self.roll = targetRoll
            else:
                rollAlpha = 1.0 - math.exp(-STP_ROLL_RESPONSE * dt)
                self.roll += (targetRoll - self.roll) * rollAlpha

        except Exception:
            LOG_CURRENT_EXCEPTION()

    # --------------------------------------------------------
    # Apply matrix
    # --------------------------------------------------------
    def _apply(self, vehicle):
        self._ensureMotorRemoved(vehicle)

        visualPitch = -self.pitch if STP_INVERT_VISUAL_PITCH else self.pitch
        visualRoll = -self.roll if STP_INVERT_VISUAL_ROLL else self.roll

        if self.isBot and BOT_SMOOTH:
            self._pushSnapshot(visualPitch, visualRoll)
        elif self._mtx is not None:
            self._mtx.setRotateYPR((self.yaw, visualPitch, visualRoll))
            self._mtx.translation = self.pos

        if self._mprov is not None:
            try:
                self._mprov.target = None
            except Exception:
                pass

            try:
                mtx = Math.Matrix()
                mtx.setRotateYPR((self.yaw, visualPitch, visualRoll))
                mtx.translation = self.pos

                self._mprov.setStaticTransform(mtx)

            except Exception:
                self._mprov = None

        try:
            filt = getattr(vehicle, 'filter', None)
            if filt is not None and hasattr(filt, 'setPosition'):
                filt.setPosition(self.pos.x, self.pos.z)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        self._guardTeleport(vehicle)

        self._updateTrackAnimation(vehicle)

    def _pushSnapshot(self, visualPitch, visualRoll):
        """Бот: запоминаем позу (время, x, y, z, yaw, pitch, roll, скачок?). Модель между расчётами двигает _smoothLoop каждый кадр."""
        now = BigWorld.time()
        p = self.pos
        sn = self._snb
        jump = False
        if sn:
            prev = sn[-1]
            jump = abs(p.x - prev[1]) > BOT_SMOOTH_JUMP or abs(p.z - prev[3]) > BOT_SMOOTH_JUMP
        snap = (now, p.x, p.y, p.z, self.yaw, visualPitch, visualRoll, jump)
        if sn:
            gap = min(0.3, max(0.0, now - sn[-1][0]))
            self._snInt = getattr(self, '_snInt', gap) * 0.85 + gap * 0.15
        sn.append(snap)
        if len(sn) > 5:
            del sn[0]
        if len(sn) == 1:
            self._snD = self.tickInterval * 1.5 + 0.02
            self._snDone = None
            _smoothWrite(self, snap)
        if self not in _SMOOTH_SET:
            _smoothRegister(self)

    def _guardTeleport(self, vehicle):
        """Сторож: модель машины должна быть там же, где её физика. Если модель вдруг оказалась далеко
        (мотор физики пропал, и она вернулась к позиции сущности - на базу), возвращаем мотор и пишем в лог.
        Также логируется резкий скачок позиции самой физики."""
        try:
            now = BigWorld.time()

            last = getattr(self, '_wdLastPos', None)
            if last is not None:
                jump = math.hypot(self.pos.x - last[0], self.pos.z - last[1])
                if jump > 20.0:
                    LOG_DEBUG('[JUMP] vehicle %s: physics position jumped %.0f m (%.0f, %.0f) -> (%.0f, %.0f)' % (
                        getattr(vehicle, 'id', '?'), jump, last[0], last[1], self.pos.x, self.pos.z))
            self._wdLastPos = (self.pos.x, self.pos.z)

            if now < getattr(self, '_wdNext', 0.0):
                return
            self._wdNext = now + 1.0

            if not self._motorRemoved or getattr(vehicle, 'health', 1) <= 0:
                return
            mdl = getattr(vehicle, 'model', None)
            if mdl is None:
                return
            mp = mdl.position
            d = math.hypot(mp.x - self.pos.x, mp.z - self.pos.z)
            if d > 8.0 + 30.0 * getattr(self, 'tickInterval', 0.0):       # далёкие боты с редкой физикой законно отстают на несколько метров
                LOG_DEBUG('[TELEPORT] vehicle %s: model at (%.0f, %.0f), physics at (%.0f, %.0f), diff %.0f m - motor re-attached' % (
                    getattr(vehicle, 'id', '?'), mp.x, mp.z, self.pos.x, self.pos.z, d))
                self._motorRemoved = False          # _ensureMotorRemoved вернёт мотор на место
                self._ensureMotorRemoved(vehicle)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def releaseTracks(self, reason=''):
        """Прекратить любые записи в нативный fashion/провайдеры гусениц этой машины (перед её смертью).
        При смерти клиент подменяет модель и разбирает fashion; наши ссылки и записи в них после этого - обращение к
        освобождённым объектам, и игра падала через 1-2 кадра после убийства."""
        if getattr(self, '_trkOff', False):
            return
        self._trkOff = True
        self._trk = None
        self._trkTries = _TRACK_MAX_INIT_TRIES
        try:
            LOG_DEBUG('[DEATH] tracks released for vehicle %s %s' % (getattr(self.vehicle, 'id', '?'), reason))
        except Exception:
            pass

    @_perf.timed('p.tracks')
    def _updateTrackAnimation(self, vehicle):
        if getattr(self, '_trkOff', False):
            return
        if getattr(self, 'isBot', False) and not BOT_TRACK_ANIMATION:
            return
        try:
            if getattr(vehicle, 'health', 1) <= 0:
                self.releaseTracks('(dead)')
                return
        except Exception:
            pass
        try:
            if self._trk is None:
                if self._trkTries >= _TRACK_MAX_INIT_TRIES:
                    return
                self._trkTries += 1
                self._trk = _initTrackAnimation(vehicle)
                if self._trk is None:
                    return

            st = self._trk
            if not st['attached']:
                _attachTrackFashion(vehicle, st)

            v = float(self._curSpeed)
            w = float(self._curRotSpeed)
            hw = float(getattr(self.params, 'halfWidth', 1.5))
            vmax = max(float(getattr(self.params, 'maxFwd', 15.0)), 0.1)

            turn = w * hw
            if TRACK_INVERT_TURN:
                turn = -turn
            left = v + turn
            right = v - turn
            fwd = v
            if not TRACK_UNITS_ABS:
                left = max(-1.0, min(1.0, left / vmax))
                right = max(-1.0, min(1.0, right / vmax))
                fwd = max(-1.0, min(1.0, fwd / vmax))
            if TRACK_INVERT_SCROLL:
                left, right, fwd = -left, -right, -fwd

            mp = st.get('movement')
            if mp is not None:
                layout = TRACK_LAYOUT
                if layout == 0:
                    mp.set(fwd, left, right, 0.0)
                elif layout == 1:
                    mp.set(left, right, 0.0, 0.0)
                elif layout == 2:
                    mp.set(left, right, fwd, 0.0)
                else:
                    mp.set(fwd, (-w if TRACK_INVERT_SCROLL else w) if TRACK_UNITS_ABS else max(-1.0, min(1.0, w / max(
                        float(getattr(self.params, 'rotRate', 1.0)), 0.1))), 0.0, 0.0)
            sp = st.get('speed')
            if sp is not None:
                sp.set(v, w, v, 0.0)

            if self._trkLog < 3 and abs(v) > 0.5:
                self._trkLog += 1
                LOG_DEBUG('tracks: v=%.2f w=%.2f -> movementInfo=(%.2f, %.2f, %.2f) readback=%s mode=%s layout=%s attached=%s' % (
                    v, w, fwd, left, right, mp.read() if mp is not None else None,
                    mp.mode if mp is not None else None, TRACK_LAYOUT, st['attached']))
        except Exception:
            self._trkTries = _TRACK_MAX_INIT_TRIES
            LOG_CURRENT_EXCEPTION()

    # --------------------------------------------------------
    # Ground helpers
    # --------------------------------------------------------
    def _terrainHit(self, spaceID, start, end):
        """Вертикальный луч вниз. Верх разрушаемых объектов (заборы, ящики, деревья, постройки) пропускается:
        это не земля. Иначе забор выглядел как отвесный склон (танк не ехал) или танк въезжал на него."""
        res = BigWorld.wg_collideSegment(spaceID, start, end, 0)
        if res is None or not STP_GROUND_SKIP_DESTRUCTIBLES:
            return res
        if self.isBot and getattr(self, 'lite', 0) >= 2:
            return res                  # далёкие боты: заборы под колёсами не различаем (на 1 луч меньше на каждый запрос)
        for _ in xrange(4):
            if res is None or res[0] is None:
                return res
            try:
                rd = BigWorld.wg_collideSegment(spaceID, start, end, 128)
            except Exception:
                return res
            if rd is None or rd[0] is None or len(rd) <= 5:
                return res
            if abs(float(rd[0].y) - float(res[0].y)) > 0.12:
                return res          # разрушаемый объект лежит не на той поверхности, в которую попал луч
            if not _isDestructibleItem(spaceID, rd[5], rd[4], rd[2]):
                return res          # это обычная статика/земля, а не забор или ящик
            start = Math.Vector3(start.x, float(res[0].y) - 0.05, start.z)
            nxt = BigWorld.wg_collideSegment(spaceID, start, end, 0)
            if nxt is None:
                return res          # под объектом ничего нет - оставляем исходную высоту
            res = nxt
        return res

    def _groundHeight(self, spaceID, x, y, z, fallback):
        """Высота земли. У ботов повторные запросы к той же точке за 0.2 с берутся из кэша (экономит лучи)."""
        if not self.isBot:
            return self._groundHeightRaw(spaceID, x, y, z, fallback)
        memo = self.__dict__.get('_ghMemo')
        if memo is None:
            memo = self._ghMemo = {}
        now = BigWorld.time()
        key = (int(round(x * 4.0)), int(round(z * 4.0)), int(round(y * 2.0)))
        c = memo.get(key)
        if c is not None and now - c[0] < 0.2:
            return c[1]
        h = self._groundHeightRaw(spaceID, x, y, z, fallback)
        if len(memo) > 40:
            memo.clear()
        memo[key] = (now, h)
        return h

    def _groundHeightRaw(self, spaceID, x, y, z, fallback):
        try:
            start = Math.Vector3(x, y + STP_GROUND_RAY_UP, z)
            end = Math.Vector3(x, y - STP_GROUND_RAY_DOWN, z)

            res = self._terrainHit(spaceID, start, end)
            if res is not None:
                return float(res[0].y)

            start = Math.Vector3(x, y + STP_GROUND_RAY_WIDE_UP, z)
            end = Math.Vector3(x, y - STP_GROUND_RAY_WIDE_DOWN, z)

            res = self._terrainHit(spaceID, start, end)
            if res is not None:
                return float(res[0].y)

            if self._groundMissCount < 10:
                self._groundMissCount += 1
                LOG_DEBUG(
                    'SimpleTankPhysics ground miss #%d: spaceID=%s x=%.2f z=%.2f fallback=%.2f' % (
                        self._groundMissCount,
                        spaceID,
                        x,
                        z,
                        fallback,
                    )
                )

        except Exception:
            LOG_CURRENT_EXCEPTION()

        return float(fallback)

    def _groundHeightStrict(self, spaceID, x, y, z):
        try:
            start = Math.Vector3(x, y + STP_GROUND_RAY_UP, z)
            end = Math.Vector3(x, y - STP_GROUND_RAY_DOWN, z)

            res = self._terrainHit(spaceID, start, end)
            if res is not None:
                return float(res[0].y)

            start = Math.Vector3(x, y + STP_GROUND_RAY_WIDE_UP, z)
            end = Math.Vector3(x, y - STP_GROUND_RAY_WIDE_DOWN, z)

            res = self._terrainHit(spaceID, start, end)
            if res is not None:
                return float(res[0].y)

        except Exception:
            LOG_CURRENT_EXCEPTION()

        return None

    # --------------------------------------------------------
    # Math helpers
    # --------------------------------------------------------
    @staticmethod
    def _approach(current, target, rate, dt):
        diff = target - current
        maxStep = rate * dt

        if diff > maxStep:
            return current + maxStep

        if diff < -maxStep:
            return current - maxStep

        return target

    @staticmethod
    def _clamp(minValue, maxValue, value):
        if value < minValue:
            return minValue

        if value > maxValue:
            return maxValue

        return value

    @staticmethod
    def _dotV3(a, b):
        return a.x * b.x + a.y * b.y + a.z * b.z

    @staticmethod
    def _asVector3(value):
        try:
            return Math.Vector3(
                float(value[0]),
                float(value[1]),
                float(value[2])
            )
        except Exception:
            return None


_SimplePhysics = SimpleTankPhysics

# ============================================================
# Arena period / countdown
# ============================================================
# Время до начала боя (обратный отсчёт) и длительность самого боя, сек.
PREBATTLE_LENGTH = 30.0
BATTLE_LENGTH = 15 * 60.0

_PREBATTLE_ACTIVE = False   # True пока идёт обратный отсчёт (стоять и не стрелять)
_COUNTDOWN_TOKEN = 0        # id текущего отсчёта; старые callback-и с другим id игнорируются
_COUNTDOWN_AVATAR = None


def isPrebattle():
    return _BATTLE_ACTIVE and _PREBATTLE_ACTIVE


_SERVER_TIME_PATCHED = False


def _patchServerTime():
    # Оффлайн BigWorld.serverTime() возвращает константу (-1.0), поэтому таймеры боя
    # (periodEndTime - serverTime()) стояли на месте. В бою отдаём BigWorld.time().
    global _SERVER_TIME_PATCHED
    if _SERVER_TIME_PATCHED:
        return
    try:
        orig = BigWorld.serverTime

        def serverTime(orig=orig):
            if _BATTLE_ACTIVE:
                return BigWorld.time()
            return orig()

        BigWorld.serverTime = serverTime
        _SERVER_TIME_PATCHED = True
        LOG_DEBUG('BigWorld.serverTime patched (battle clock runs offline)')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _arenaNow():
    # ВАЖНО: клиентский Battle.__setArenaTime считает остаток времени как
    #   arena.periodEndTime - BigWorld.serverTime()
    # поэтому endTime обязан быть в тех же часах (serverTime), а не BigWorld.time().
    try:
        return BigWorld.serverTime()
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return BigWorld.time()


def _forceArenaPeriod(avatar, period, length, additionalInfo=None):
    try:
        endTime = _arenaNow() + length
        payload = cPickle.dumps((period, endTime, length, additionalInfo))
        avatar.arena.update(ARENA_UPDATE.PERIOD, payload)
        LOG_DEBUG('battle: forced arena period=%s length=%s endTime=%.2f serverTime=%.2f time=%.2f' % (
            period, length, endTime, _arenaNow(), BigWorld.time()))
    except Exception:
        LOG_CURRENT_EXCEPTION()


BATTLE_MUSIC = True        # после отсчёта музыка загрузки боя затихает, в бою играет боевая музыка (как в оригинальной игре)


def _switchToBattleMusic():
    """Музыка боя: после обратного отсчёта убираем музыку начала боя и включаем боевую. Имена событий берём из клиента
    (MusicController), всё пишется в лог строками [MUSIC] - по ним видно, что нашлось в этой сборке клиента."""
    if not BATTLE_MUSIC or not _BATTLE_ACTIVE:
        return
    try:
        import MusicController as MC
        mc = MC.g_musicController
        consts = dict((n, getattr(MC, n)) for n in dir(MC) if n.startswith('MUSIC_EVENT') or n.startswith('AMBIENT_EVENT'))
        cur = getattr(mc, '_MusicController__eventMusicId', None)
        amb = getattr(mc, '_MusicController__eventAmbientId', None)
        LOG_DEBUG('[MUSIC] battle started: music=%s ambient=%s events=%s' % (cur, amb, sorted(consts.items())))
        combat = consts.get('MUSIC_EVENT_COMBAT')
        loading = consts.get('MUSIC_EVENT_COMBAT_LOADING')
        if combat is not None:
            if cur != combat:
                mc.play(combat)
                LOG_DEBUG('[MUSIC] switched to combat music (event %s)' % combat)
            else:
                LOG_DEBUG('[MUSIC] combat music is already playing')
        elif loading is not None and cur == loading:
            mc.stop()
            LOG_DEBUG('[MUSIC] no combat event in this client: loading music stopped')
        else:
            LOG_DEBUG('[MUSIC] nothing to switch (no combat/loading events found)')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _finishPrebattle(avatar, token):
    global _PREBATTLE_ACTIVE
    from constants import ARENA_PERIOD

    # отсчёт устарел (бой закрыли / начали новый) - ничего не делаем
    if token != _COUNTDOWN_TOKEN or not _BATTLE_ACTIVE:
        LOG_DEBUG('battle: stale countdown callback ignored (token=%s)' % token)
        return

    _PREBATTLE_ACTIVE = False
    _forceArenaPeriod(avatar, ARENA_PERIOD.BATTLE, BATTLE_LENGTH)
    try:
        from gui.mods.offhangar import shooting as off_shooting
        off_shooting.startBattleReload()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    LOG_DEBUG('battle: PREBATTLE finished -> BATTLE started')
    try:
        BigWorld.callback(0.4, _switchToBattleMusic)     # даём родным обработчикам смены периода отработать первыми
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _startPrebattleCountdown(avatar):
    global _PREBATTLE_ACTIVE, _COUNTDOWN_TOKEN, _COUNTDOWN_AVATAR
    from constants import ARENA_PERIOD

    # защита от двойного запуска для одного и того же боя
    if _PREBATTLE_ACTIVE and _COUNTDOWN_AVATAR is avatar:
        return

    _COUNTDOWN_TOKEN += 1
    token = _COUNTDOWN_TOKEN
    _COUNTDOWN_AVATAR = avatar
    _PREBATTLE_ACTIVE = True

    _forceArenaPeriod(avatar, ARENA_PERIOD.PREBATTLE, PREBATTLE_LENGTH)
    BigWorld.callback(PREBATTLE_LENGTH, lambda: _finishPrebattle(avatar, token))


def _resetCountdown():
    global _PREBATTLE_ACTIVE, _COUNTDOWN_TOKEN, _COUNTDOWN_AVATAR
    _COUNTDOWN_TOKEN += 1
    _PREBATTLE_ACTIVE = False
    _COUNTDOWN_AVATAR = None

# ============================================================
# Vehicle / avatar patches
# ============================================================
def _patchVehicleNativePhysics():
    try:
        import Vehicle as _Veh

        cls = _Veh.Vehicle

        _orig_startPhysics = cls._Vehicle__startWGPhysics

        def _safe_startPhysics(self, orig=_orig_startPhysics):
            if _BATTLE_ACTIVE:
                LOG_DEBUG(
                    'vehicle native physics skipped (offline), id=%s' % getattr(self, 'id', None)
                )

                self.wgPhysics = None

                try:
                    sp = SimpleTankPhysics()

                    # чужие машины (боты) не должны трогать матрицу/камеру игрока
                    try:
                        ownID = getattr(BigWorld.player(), 'playerVehicleID', 0)
                        isBot = False
                        try:
                            isBot = int(self.publicInfo['accountDBID']) < 0
                        except Exception:
                            isBot = bool(ownID) and self.id != ownID
                        if isBot:
                            sp.isBot = True
                            sp.tickInterval = 0.04
                    except Exception:
                        LOG_CURRENT_EXCEPTION()

                    sp.setup(self)

                    _simplePhysicsByVehID[self.id] = sp
                    sp.start()

                except Exception:
                    LOG_CURRENT_EXCEPTION()

                return

            return orig(self)

        cls._Vehicle__startWGPhysics = _safe_startPhysics

        _orig_stopPhysics = cls._Vehicle__stopWGPhysics

        def _safe_stopPhysics(self, orig=_orig_stopPhysics):
            sp = _simplePhysicsByVehID.pop(getattr(self, 'id', None), None)
            if sp is not None:
                try:
                    sp.stop()
                except Exception:
                    LOG_CURRENT_EXCEPTION()
                _stopVehicleEngineSound(self)

            turretCtrl = _simpleTurretByVehID.pop(getattr(self, 'id', None), None)
            if turretCtrl is not None:
                try:
                    turretCtrl.stop()
                except Exception:
                    LOG_CURRENT_EXCEPTION()

            if _BATTLE_ACTIVE and getattr(self, 'wgPhysics', None) is None:
                return

            return orig(self)

        cls._Vehicle__stopWGPhysics = _safe_stopPhysics

        LOG_DEBUG('Vehicle native physics offline-safe patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()

def _patchAvatarInputHandlerDetach():
    try:
        import AvatarInputHandler

        _orig_detachCursor = AvatarInputHandler.AvatarInputHandler.detachCursor

        def _safe_detachCursor(self, isDetached, enableAiming):
            if _isOffline() and isDetached:
                LOG_DEBUG('detachCursor(True) suppressed (offline)')
                return
            return _orig_detachCursor(self, isDetached, enableAiming)

        AvatarInputHandler.AvatarInputHandler.detachCursor = _safe_detachCursor
        LOG_DEBUG('AvatarInputHandler.detachCursor offline-safe patch installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()

_GOOD_ENGINE_EVENTS = []      # события звука, которые клиент реально смог создать (для запасного выбора)
_BAD_SOUND_EVENTS = set()     # события, которых нет в звуковых банках клиента
_SOUND_FALLBACK = {}          # исходное событие -> найденное запасное
_FALLBACK_MAX_PROBES = 24


def _soundCandidates(eventName):
    """Запасные события: сначала уже сработавшие, затем событие того же класса техники, затем остальные двигатели."""
    cands = []
    for ev in _GOOD_ENGINE_EVENTS:
        if ev != eventName and ev not in cands:
            cands.append(ev)
    try:
        import nations
        from items import vehicles as _veh
        allEv = set()
        for nid in xrange(len(nations.NAMES)):
            for eng in _veh.g_cache.engines(nid).itervalues():
                ev = eng.get('sound')
                if ev and ev != eventName and ev not in _BAD_SOUND_EVENTS:
                    allEv.add(ev)
        parts = eventName.split('/')
        cls = parts[3] if len(parts) > 3 else ''
        same = sorted(e for e in allEv if ('/%s/' % cls) in e)
        rest = sorted(e for e in allEv if e not in same)
        for ev in same + rest:
            if ev not in cands:
                cands.append(ev)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return cands


def _playSoundWithFallback(model, soundName, orig):
    """model.playSound(soundName); если такого события нет в клиенте - берём похожее, чтобы танк не ехал молча."""
    if soundName not in _BAD_SOUND_EVENTS:
        try:
            sound = model.playSound(soundName)
        except Exception:
            sound = None
        if sound is not None:
            if soundName.startswith('/vehicles/tanks/') and soundName not in _GOOD_ENGINE_EVENTS:
                _GOOD_ENGINE_EVENTS.append(soundName)
            return sound
        _BAD_SOUND_EVENTS.add(soundName)
        LOG_DEBUG('[SOUND] event %r is missing in the client sound banks' % (soundName,))
    if not soundName.startswith('/vehicles/'):
        return None
    alt = _SOUND_FALLBACK.get(soundName)
    if alt is not None:
        try:
            return model.playSound(alt)
        except Exception:
            return None
    probes = 0
    for ev in _soundCandidates(soundName):
        if probes >= _FALLBACK_MAX_PROBES:
            break
        if ev in _BAD_SOUND_EVENTS:
            continue
        probes += 1
        try:
            sound = model.playSound(ev)
        except Exception:
            sound = None
        if sound is not None:
            _SOUND_FALLBACK[soundName] = ev
            if ev not in _GOOD_ENGINE_EVENTS:
                _GOOD_ENGINE_EVENTS.append(ev)
            LOG_DEBUG('[SOUND] %r -> fallback %r' % (soundName, ev))
            return sound
        _BAD_SOUND_EVENTS.add(ev)
    LOG_DEBUG('[SOUND] no fallback sound found for %r' % (soundName,))
    return None


def _patchEngineSoundNative():
    """Звук двигателя: родной VehicleAppearance в офлайне рвал звук (читает нулевой filter.speedInfo, обнуляя обороты,
    а общий лимит в 6 проигрываний вытеснял звук игрока ботами). Здесь параметры считаем сами, звук игрока из лимита выводим."""
    try:
        import VehicleAppearance as _VA
        cls = _VA.VehicleAppearance

        cls._VehicleAppearance__newEngine = lambda self: None
        cls._VehicleAppearance__newTrackSounds = lambda self: None
        cls._VehicleAppearance__updateTrackSounds = lambda self: None

        _orig_ums = cls._VehicleAppearance__updateMovementSounds

        def _ums(self, orig=_orig_ums):
            veh = self._VehicleAppearance__vehicle
            if not _BATTLE_ACTIVE or not getattr(veh, 'isPlayer', False):
                return orig(self)
            checker = cls.VehicleSoundsChecker
            for attr in ('_VehicleAppearance__engineSound', '_VehicleAppearance__movementSound'):
                snd = getattr(self, attr, None)
                if snd is None:
                    continue
                try:
                    checker.removeSound(snd)
                    if not snd.isPlaying:
                        snd.play()
                except Exception:
                    pass

        cls._VehicleAppearance__updateMovementSounds = _ums

        _orig_getSound = _VA._getSound

        def _getSound(model, soundName, orig=_orig_getSound):
            try:
                return _playSoundWithFallback(model, soundName, orig)
            except Exception:
                LOG_CURRENT_EXCEPTION()
                return None

        _VA._getSound = _getSound
        try:
            import VehicleGunRotator as _VGR
            cls2 = getattr(_VGR, '_PlayerTurretRotationSoundEffect', None)
            if cls2 is not None and not hasattr(cls2, '_PlayerTurretRotationSoundEffect__manualSound'):
                # в офлайне звук вращения башни мог не создаться, и destroy() при выходе из боя падал с AttributeError
                cls2._PlayerTurretRotationSoundEffect__manualSound = None
        except Exception:
            LOG_CURRENT_EXCEPTION()
        LOG_DEBUG('VehicleAppearance engine sound patch installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchVehicleAppearanceHavok():
    try:
        import VehicleAppearance as _VA

        cls = _VA.VehicleAppearance

        _orig_setupHavok = cls._VehicleAppearance__setupHavok

        def _safe_setupHavok(self, orig=_orig_setupHavok):
            if _BATTLE_ACTIVE:
                LOG_DEBUG('vehicle havok setup skipped (offline)')
                return

            return orig(self)

        cls._VehicleAppearance__setupHavok = _safe_setupHavok

        LOG_DEBUG('VehicleAppearance havok offline-safe patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchVehicleStartVisual():
    try:
        import Vehicle as _Veh

        cls = _Veh.Vehicle

        _orig = cls.startVisual

        def _safe_startVisual(self, orig=_orig):
            if not _BATTLE_ACTIVE:
                return orig(self)

            try:
                from gui.mods.offhangar import bots as off_bots
                off_bots.preStartVisual(self)
            except Exception:
                LOG_CURRENT_EXCEPTION()

            try:
                orig(self)
            except Exception:
                LOG_CURRENT_EXCEPTION()

                # Minimal fallback without native/sound-related calls.
                try:
                    self.appearance.preStart(self.typeDescriptor)
                    self.appearance.start(self, self._Vehicle__prereqs)

                    self._Vehicle__prereqs = None
                    self.appearance.onVehicleHealthChanged()

                    self.isStarted = True
                    self._Vehicle__startWGPhysics()

                except Exception:
                    LOG_CURRENT_EXCEPTION()

            if self.isPlayer:
                ctrl = _simpleTurretByVehID.get(self.id)
                if ctrl is not None:
                    try:
                        self.appearance.setupGunMatrixTargets(ctrl)
                        LOG_DEBUG('turret rotation target attached id=%s' % self.id)
                    except Exception:
                        LOG_CURRENT_EXCEPTION()

        cls.startVisual = _safe_startVisual

        LOG_DEBUG('Vehicle.startVisual offline-safe patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


EDGE_DETECT_ENEMIES = True    # подсветка контура чужих машин под прицелом (False - выключить, если вылетает)


def _patchOwnSpeeds():
    """Спидометр (панель повреждений) и разброс берут скорость из filter.speedInfo, которого в офлайне нет -
    отдаём скорость нашей физики."""
    try:
        import Avatar as _A
        cls = _A.PlayerAvatar
        orig = cls.getOwnVehicleSpeeds

        def getOwnVehicleSpeeds(self, getInstantaneous=False, orig=orig):
            if _BATTLE_ACTIVE:
                try:
                    sp = _simplePhysicsByVehID.get(self.playerVehicleID)
                    if sp is not None:
                        return (float(sp._curSpeed), float(getattr(sp, '_curRotSpeed', 0.0)))
                except Exception:
                    pass
            return orig(self, getInstantaneous)

        cls.getOwnVehicleSpeeds = getOwnVehicleSpeeds
        LOG_DEBUG('Avatar.getOwnVehicleSpeeds offline patch installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchEdgeDetect():
    try:
        _orig_add = BigWorld.wgAddEdgeDetectEntity
        _orig_del = BigWorld.wgDelEdgeDetectEntity

        def _isOwn(args):
            try:
                ent = args[0]
                av = BigWorld.player()
                return bool(getattr(ent, 'isPlayer', False)) or ent.id == getattr(av, 'playerVehicleID', None)
            except Exception:
                return True

        def _safe_add(*args, **kwargs):
            # своя машина - как раньше (в офлайне ломает отрисовку); чужие - подсветка цели под прицелом
            if _BATTLE_ACTIVE and (not EDGE_DETECT_ENEMIES or _isOwn(args)):
                return
            try:
                return _orig_add(*args, **kwargs)
            except Exception:
                LOG_CURRENT_EXCEPTION()

        def _safe_del(*args, **kwargs):
            if _BATTLE_ACTIVE and (not EDGE_DETECT_ENEMIES or _isOwn(args)):
                return
            try:
                return _orig_del(*args, **kwargs)
            except Exception:
                LOG_CURRENT_EXCEPTION()

        BigWorld.wgAddEdgeDetectEntity = _safe_add
        BigWorld.wgDelEdgeDetectEntity = _safe_del

        LOG_DEBUG('EdgeDetect offline-safe patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchSplineTracks():
    try:
        import VehicleAppearance as _VA

        _orig = _VA.setupSplineTracks

        def _safe_setupSplineTracks(fashion, vDesc, chassisModel, prereqs):
            try:
                splineDesc = getattr(vDesc.chassis, 'splineDesc', None)
                # nothing to do if no spline description
                if splineDesc is None:
                    return None

                # need BigWorld helper available
                if not hasattr(BigWorld, 'wg_createSplineTrack'):
                    LOG_DEBUG('wg_createSplineTrack not available, skipping spline tracks')
                    return None

                # prereqs must contain required segment models
                if prereqs is None:
                    LOG_DEBUG('prereqs missing, skipping spline tracks')
                    return None

                leftName = splineDesc.get('segmentModelLeft')
                rightName = splineDesc.get('segmentModelRight')
                if leftName not in prereqs or rightName not in prereqs:
                    LOG_DEBUG('spline segment models not in prereqs, skipping spline tracks')
                    return None

                return _orig(fashion, vDesc, chassisModel, prereqs)
            except Exception:
                LOG_CURRENT_EXCEPTION()
                return None

        _VA.setupSplineTracks = _safe_setupSplineTracks

        LOG_DEBUG('SplineTracks offline-safe patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchAvatarMoveVehicle():
    try:
        import Avatar as _A

        cls = _A.PlayerAvatar

        _orig_moveVehicle = cls.moveVehicle

        def _safe_moveVehicle(self, flags, isKeyDown, orig=_orig_moveVehicle):
            if _BATTLE_ACTIVE:
                flags = int(flags or 0)

                sp = _simplePhysicsByVehID.get(self.playerVehicleID)
                if sp is not None:
                    sp.setFlags(flags)

                try:
                    vehicle = BigWorld.entity(self.playerVehicleID)

                    if vehicle is not None and getattr(vehicle, 'isStarted', False):
                        try:
                            vehicle.showPlayerMovementCommand(flags)
                        except Exception:
                            pass

                        try:
                            movementDir = 1 if flags & 1 else (-1 if flags & 2 else 0)
                            rotationDir = -1 if flags & 4 else (1 if flags & 8 else 0)

                            vehicle.filter.notifyInputKeysDown(movementDir, rotationDir)

                        except Exception:
                            LOG_CURRENT_EXCEPTION()

                except Exception:
                    LOG_CURRENT_EXCEPTION()

                return

            return orig(self, flags, isKeyDown)

        cls.moveVehicle = _safe_moveVehicle

        LOG_DEBUG('Avatar.moveVehicle offline-safe patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchAvatarPositionControl():
    try:
        import AvatarPositionControl as _APC
        cls = _APC.AvatarPositionControl

        _orig_moveTo = cls.moveTo

        def _safe_moveTo(self, pos):
            if _isOffline():
                return
            return _orig_moveTo(self, pos)

        cls.moveTo = _safe_moveTo

        LOG_DEBUG('AvatarPositionControl offline-safe patch installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchAvatarBaseCell():
    try:
        import Avatar as _A

        cls = _A.PlayerAvatar

        origGA = cls.__getattribute__

        def _patchedGA(self, name, orig=origGA):
            if name in ('base', 'cell', 'server') and _BATTLE_ACTIVE:
                return _AVATAR_FAKE_SERVER

            try:
                return orig(self, name)
            except AttributeError:
                if _BATTLE_ACTIVE and name in _OFFLINE_ATTR_DEFAULTS:
                    return _OFFLINE_ATTR_DEFAULTS[name]

                raise

        cls.__getattribute__ = _patchedGA

        LOG_DEBUG('Avatar base/cell offline-safe patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchAvatarOwnVehicleMatrix():
    try:
        import Avatar as _A

        cls = _A.PlayerAvatar

        _orig = cls._PlayerAvatar__setOwnVehicleMatrixCallback

        def _safe_setOwnVehicleMatrixCallback(self, orig=_orig):
            if _BATTLE_ACTIVE:
                mp = getattr(self, '_PlayerAvatar__ownVehicleMProv', None)
                if mp is not None:
                    try:
                        mp.target = None
                    except Exception:
                        pass

                return

            return orig(self)

        cls._PlayerAvatar__setOwnVehicleMatrixCallback = _safe_setOwnVehicleMatrixCallback

        LOG_DEBUG('Avatar.__setOwnVehicleMatrixCallback offline patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Offline battle helpers
# ============================================================
class _PublicInfo(dict):
    def __init__(self, compDescr, team):
        data = {
            'compDescr': compDescr,
            'team': team,
            'isTeamKiller': False,
            'index': 0,
            'name': OFFLINE_NICKNAME,
            'prebattleID': 0,
            'marksOnGun': 0,
            'accountDBID': OFFLINE_DBID,
            'clanAbbrev': '',
            'clanDBID': 0,
            'isPrebattleCreator': False,
            'events': {},
            'igrType': 0,
            'potapovQuestIDs': [],
            'vehicleType': None,
        }

        dict.__init__(self, data)

        for k, v in data.items():
            setattr(self, k, v)


def _resolveRequestedArena(arenaTypeID):
    """Карта, выбранная игроком в окне "Демонстрация карты" (клик по карте -> enqueueRandom(..., arenaTypeID)).
    Мод умеет только обычный бой (ctf): для штурма/встречного боя берём ту же карту в варианте ctf.
    Возвращает id типа арены или None (тогда карта выбирается случайно, как раньше)."""
    try:
        aid = int(arenaTypeID or 0)
    except Exception:
        return None
    if aid <= 0:
        return None
    at = ArenaType.g_cache.get(aid)
    if at is None:
        LOG_DEBUG('[MAP] requested arenaTypeID=%s is unknown, choosing randomly' % aid)
        return None
    if getattr(at, 'gameplayName', '') == 'ctf':
        return aid
    for tid, other in ArenaType.g_cache.items():
        if (getattr(other, 'geometryName', None) == getattr(at, 'geometryName', '')
                and getattr(other, 'gameplayName', '') == 'ctf'):
            LOG_DEBUG('[MAP] %s requested as %s - using the standard (ctf) version %s' % (
                at.geometryName, getattr(at, 'gameplayName', '?'), tid))
            return tid
    LOG_DEBUG('[MAP] %s has no standard (ctf) version, using the requested arena type %s as is' % (at.geometryName, aid))
    return aid


def _chooseArenaTypeID(requested=None):
    chosen = _resolveRequestedArena(requested)
    if chosen is not None:
        LOG_DEBUG('[MAP] using the map chosen by the player: arenaTypeID=%s' % chosen)
        return chosen

    candidates = [
        tid for tid, at in ArenaType.g_cache.items()
        if getattr(at, 'gameplayName', '') == 'ctf'
        and not getattr(at, 'explicitRequestOnly', False)
    ]

    if not candidates:
        candidates = list(ArenaType.g_cache.keys())

    return random.choice(candidates) if candidates else None


def _spawnYaw(at):
    """Face the player's tank toward the opposing team's base, matching ally bots."""
    ts = _tutorialStartPos(at)
    if ts is not None:
        return ts[1]
    try:
        # Prefer the exact direction already calculated for the bot formation.
        from gui.mods.offhangar import bots as off_bots
        axis = getattr(off_bots, '_AXIS', {})
        direction = axis.get('dir') if axis else None
        if direction:
            dx, dz = float(direction[0]), float(direction[1])
            if abs(dx) + abs(dz) > 1e-6:
                return math.atan2(dx, dz)
    except Exception:
        pass

    # The player vehicle is created before bot formation in some startup paths,
    # so derive the same direction from arena team base positions as a fallback.
    try:
        path = 'scripts/arena_defs/%s.xml' % at.geometryName
        section = ResMgr.openSection(path)
        if section is not None:
            root = section['gameplayTypes/ctf/teamBasePositions']
            one = root['team1'] if root is not None else None
            two = root['team2'] if root is not None else None
            p1 = p2 = None
            if one is not None:
                for i in range(1, 9):
                    val = one.readString('position%d' % i, '')
                    if val:
                        q = val.split()
                        if len(q) >= 2:
                            p1 = (float(q[0]), float(q[1]))
                            break
            if two is not None:
                for i in range(1, 9):
                    val = two.readString('position%d' % i, '')
                    if val:
                        q = val.split()
                        if len(q) >= 2:
                            p2 = (float(q[0]), float(q[1]))
                            break
            if p1 and p2:
                return math.atan2(p2[0] - p1[0], p2[1] - p1[1])
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 0.0


def _readSpawnFromSection(section):
    """Первая позиция команды 1 из любого типа игры (ctf, assault, tutorial...): teamBasePositions / teamSpawnPoints."""
    try:
        gt = section['gameplayTypes']
        groups = []
        if gt is not None:
            ctf = gt['ctf']
            if ctf is not None:
                groups.append(ctf)
            for name, sec in gt.items():
                if sec is not None and sec is not ctf:
                    groups.append(sec)
        groups.append(section)
        for g in groups:
            for grp in ('teamBasePositions', 'teamSpawnPoints'):
                team = g[grp + '/team1']
                if team is None:
                    continue
                for key in ('position1', 'position'):
                    posStr = team.readString(key, '')
                    if posStr:
                        parts = posStr.split()
                        if len(parts) >= 2:
                            return (float(parts[0]), float(parts[1]))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return None


def _dumpArenaXml(section, geometryName):
    """Диагностика: структура arena_defs/<карта>.xml, если позицию игрока найти не удалось."""
    try:
        def walk(sec, depth, out):
            if depth > 4:
                return
            for name, sub in sec.items():
                try:
                    val = sub.asString.strip()[:40]
                except Exception:
                    val = ''
                out.append('%s%s %s' % ('  ' * depth, name, val))
                walk(sub, depth + 1, out)
        lines = []
        walk(section, 0, lines)
        LOG_DEBUG('[SPAWN] arena_defs/%s.xml structure (no player position found):\n%s' % (geometryName, '\n'.join(lines[:80])))
    except Exception:
        LOG_CURRENT_EXCEPTION()


# Учебная карта: старт игрока - объект SpawnPoint из spaces/00_tank_tutorial/*.chunk (позиция, поворот в радианах).
# arena_defs для этой карты точку старта не содержит (лежит за границей).
_TUTORIAL_START = {'00_tank_tutorial': ((-131.76, -253.69), 0.6224)}


def _tutorialStartPos(at):
    return _TUTORIAL_START.get(getattr(at, 'geometryName', None))


def _spawnPos(at):
    ts = _tutorialStartPos(at)
    if ts is not None:
        LOG_DEBUG('spawnPos: tutorial start point x=%.2f z=%.2f' % (ts[0][0], ts[0][1]))
        return (ts[0][0], 0.0, ts[0][1])

    path = 'scripts/arena_defs/%s.xml' % at.geometryName

    try:
        section = ResMgr.openSection(path)

        if section is not None:
            pos = _readSpawnFromSection(section)

            if pos is not None:
                LOG_DEBUG('spawnPos from xml: x=%.2f z=%.2f' % (pos[0], pos[1]))

                return (pos[0], 0.0, pos[1])

            _dumpArenaXml(section, at.geometryName)

    except Exception:
        LOG_CURRENT_EXCEPTION()

    return (0.0, 0.0, 0.0)


def _vehInfoTuple(vid, compDescr, team):
    return (
        vid,
        compDescr,
        OFFLINE_NICKNAME,
        team,
        True,
        True,
        False,
        OFFLINE_DBID,
        '',
        0,
        0,
        False,
        {},
        0,
        [],
    )


def _getMaxHealth(compDescr):
    try:
        from items import vehicles as _vehicles_module

        descr = _vehicles_module.VehicleDescr(compactDescr=compDescr)
        return descr.maxHealth

    except Exception:
        LOG_CURRENT_EXCEPTION()

    return 1000


# ============================================================
# Avatar init / world enter / leave
# ============================================================
def teleportVehicleByID(vid, pos, yaw=None):
    """Переставить машину (игрока или бота) в точку pos: физика, скорость, привязка к земле."""
    sp = _simplePhysicsByVehID.get(vid)
    if sp is None:
        return False
    try:
        sp.pos = Math.Vector3(float(pos[0]), float(pos[1]), float(pos[2]))
        if yaw is not None:
            sp.yaw = float(yaw)
        sp._curSpeed = 0.0
        sp._lastSpeed = 0.0
        sp._curRotSpeed = 0.0
        sp.verticalSpeed = 0.0
        sp.lastGroundY = float(pos[1])
        sp._groundReady = False
        LOG_DEBUG('[TELEPORT] vehicle %s -> (%.1f, %.1f, %.1f) yaw=%s' % (vid, pos[0], pos[1], pos[2], yaw))
        return True
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return False


def _patchedTeleportVehicle(self, position, yaw, _orig=None):
    """Родной PlayerAvatar.teleportVehicle зовёт self.base.vehicle_teleport (сервер). Офлайн двигаем свою физику."""
    if _BATTLE_ACTIVE:
        teleportVehicleByID(getattr(self, 'playerVehicleID', 0), position, yaw)
        return
    if _orig is not None:
        return _orig(self, position, yaw)


def _patchLockPrediction():
    """Автоприцел наводит орудие на цель по entity.position/matrix машины, а офлайн они стоят в точке создания
    (у ботов даже y=0, пока земля не подгрузилась; у ездящих - вообще не меняются). В итоге орудие смотрело мимо цели,
    под ней (на холмах до 8-10 м ниже). Теперь берётся позиция и поворот из нашей физики."""
    try:
        from VehicleGunRotator import VehicleGunRotator as _VGR
        _orig = _VGR.predictLockedTargetShotPoint
    except Exception:
        LOG_NOTE('[AUTOAIM] VehicleGunRotator.predictLockedTargetShotPoint not found in this client - patch skipped')
        return

    def predictLockedTargetShotPoint(self):
        try:
            tv = BigWorld.player().autoAimVehicle
            if tv is None:
                return None
            sp = _simplePhysicsByVehID.get(tv.id)
            if sp is None:
                return _orig(self)
            td = tv.typeDescriptor
            hb = td.hull['hitTester'].bbox
            hullPos = Math.Vector3((hb[0][0] + hb[1][0]) / 2.0, td.chassis['hullPosition'][1], (hb[0][2] + hb[1][2]) / 2.0)
            offset = td.hull['turretPositions'][0] / 2.0 + hullPos
            m = Math.Matrix()
            m.setRotateY(sp.yaw)
            return Math.Vector3(sp.pos.x, sp.pos.y, sp.pos.z) + m.applyVector(offset)
        except Exception:
            LOG_CURRENT_EXCEPTION()
            return _orig(self)

    _VGR.predictLockedTargetShotPoint = predictLockedTargetShotPoint
    LOG_NOTE('[AUTOAIM] lock prediction uses the physics position of the target')


def _enableTriggers(avatar):
    """Родной шаг инициализации в конце включает TriggersManager (зоны на карте, прицеливание на цель). Наш шаг его подменял."""
    try:
        import TriggersManager as _TM
        _TM.g_manager.enable(True)
        LOG_DEBUG('[TRIGGERS] TriggersManager enabled')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _offlineInitStep(self, *args, **kwargs):
    if not _BATTLE_ACTIVE:
        if _ORIG_INIT_STEP:
            return _ORIG_INIT_STEP(self, *args, **kwargs)

        return

    steps = getattr(self, '_PlayerAvatar__stepsTillInit', 0)

    if not steps or steps <= 0:
        return

    self._PlayerAvatar__stepsTillInit = steps - 1

    if self._PlayerAvatar__stepsTillInit != 0:
        return

    LOG_DEBUG('battle: init-step finalize begin')

    try:
        self.initSpace()
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        LOG_DEBUG('battle: calling startGUI')
        self._PlayerAvatar__startGUI()
        LOG_DEBUG('battle: startGUI done')
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        import Vehicle as _V

        for v in BigWorld.entities.values():
            if (
                getattr(v, 'inWorld', False)
                and isinstance(v, _V.Vehicle)
                and not v.isStarted
            ):
                LOG_DEBUG('battle: startVisual for vehicle id=%s' % v.id)

                v.startVisual()
                self.onVehicleEnterWorld(v)

    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        from helpers import DecalMap

        DecalMap.g_instance.initGroups(1.0)
        self._PlayerAvatar__setOwnVehicleMatrixCallback()

    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        BigWorld.worldDrawEnabled(True)

        import BattleReplay
        BattleReplay.g_replayCtrl.onClientReady()

    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        self._PlayerAvatar__setIsOnArena(True)

        own = BigWorld.entity(self.playerVehicleID)
        if own is not None:
            self.updateVehicleHealth(own.health, own.isCrewActive)

    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        from Avatar import ClientVisibilityFlags
        ClientVisibilityFlags.updateSpaceVisibility(self.spaceID, 0)

        from PlayerEvents import g_playerEvents
        g_playerEvents.onAvatarReady()

    except Exception:
        LOG_CURRENT_EXCEPTION()

    _enableTriggers(self)

    try:
        from constants import ARENA_GUI_TYPE as _AGT
        if getattr(self.arena, 'guiType', None) == _AGT.TUTORIAL:
            from gui.mods.offhangar import tutor as _tutor
            _tutor.onBattleReady(self)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    LOG_DEBUG('battle: finalize done; userSeesWorld=%s' % self.userSeesWorld())


def _onEnterWorld(self, prereqs):
    global _PENDING_CTX

    if _BATTLE_ACTIVE and _PENDING_CTX is not None:
        props, at, compDescr, spaceID, ammoDict, hangarVehicle, vehInvID = _PENDING_CTX

        if getattr(self, 'spaceID', None) == spaceID:
            for k, v in props.items():
                try:
                    setattr(self, k, v)
                except Exception:
                    pass

            if BigWorld.player() is not self:
                try:
                    BigWorld.player(self)
                except Exception:
                    LOG_CURRENT_EXCEPTION()

    result = _ORIG_ON_ENTER_WORLD(self, prereqs)

    if _BATTLE_ACTIVE and _PENDING_CTX is not None:
        props, at, compDescr, spaceID, ammoDict, hangarVehicle, vehInvID = _PENDING_CTX

        if getattr(self, 'spaceID', None) == spaceID:
            _PENDING_CTX = None
            _continueBattle(self, at, compDescr, spaceID, ammoDict, hangarVehicle=hangarVehicle, vehInvID=vehInvID)

    return result

def _onLeaveWorld(self, *args, **kwargs):
    global _BATTLE_ACTIVE

    # onLeaveWorld Avatar'а вызывается вручную из endbattle (пока Avatar ещё BigWorld.player()),
    # повторный вызов движком при destroyEntity пропускаем
    if getattr(self, '_offhangar_left', False):
        LOG_DEBUG('battle: Avatar.onLeaveWorld skipped (already done)')
        return
    try:
        self._offhangar_left = True
    except Exception:
        pass

    _resetCountdown()

    try:
        from gui.mods.offhangar import bots as off_bots
        off_bots.onBattleEnd()
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        AreaDestructibles.clear()
    except Exception:
        pass

    try:
        if getattr(self, 'terrainEffects', None) is not None:
            self.terrainEffects.destroy()
            self.terrainEffects = None
    except Exception:
        pass

    # _BATTLE_ACTIVE сбрасываем только ПОСЛЕ оригинального onLeaveWorld:
    # ему ещё нужен фейковый base/cell аватара (иначе AttributeError при выходе из боя).
    try:
        return _ORIG_ON_LEAVE_WORLD(self, *args, **kwargs)
    finally:
        _BATTLE_ACTIVE = False
        _BROKEN_ITEMS.clear()
        _DEST_ITEM_CACHE.clear()
        clearCorpses()


# ============================================================
# Battle creation
# ============================================================
def _continueBattle(avatar, at, compDescr, spaceID, ammoDict, hangarVehicle=None, vehInvID=None):
    try:
        import AreaDestructibles

        if AreaDestructibles.g_destructiblesManager is None:
            AreaDestructibles.init()

        AreaDestructibles.g_destructiblesManager.startSpace(spaceID)

        LOG_DEBUG('AreaDestructibles space started: %s' % spaceID)

    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        if getattr(avatar, 'terrainEffects', None) is None:
            from helpers import bound_effects

            avatar.terrainEffects = bound_effects.StaticSceneBoundEffects()

            LOG_DEBUG('terrainEffects created for offline battle')

    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        pos = _spawnPos(at)

        vprops = {
            'publicInfo': _PublicInfo(compDescr, 1),
            'health': _getMaxHealth(compDescr),
            'isCrewActive': True,
            'engineMode': (0, 0),
            'publicStateModifiers': [],
            'damageStickers': [],
            'gunAnglesPacked': 0,
            'isStrafing': False,
        }

        LOG_DEBUG('battle: createEntity Vehicle at (%.1f,%.1f,%.1f) ...' % pos)

        vehicle = BigWorld.createEntity(
            'Vehicle',
            spaceID,
            0,
            pos,
            (0, 0, _spawnYaw(at)),
            vprops
        )

        if isinstance(vehicle, (int, long)):
            vehicle = BigWorld.entity(vehicle)

        if vehicle is not None:
            for k, v in vprops.items():
                try:
                    setattr(vehicle, k, v)
                except Exception:
                    pass

            LOG_DEBUG('battle: vehicle created synchronously id=%s' % getattr(vehicle, 'id', None))

            _finishVehicleSetup(avatar, vehicle, compDescr, ammoDict, hangarVehicle=hangarVehicle, vehInvID=vehInvID)
        else:
            LOG_DEBUG('battle: vehicle creation async, waiting for entity')
            BigWorld.callback(0.1, lambda: _waitForVehicleEntity(avatar, vprops, compDescr, spaceID, 50, ammoDict, hangarVehicle=hangarVehicle, vehInvID=vehInvID))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _waitForVehicleEntity(avatar, vprops, compDescr, spaceID, triesLeft, ammoDict, hangarVehicle=None, vehInvID=None):
    found = None

    try:
        import Vehicle as _V

        for e in BigWorld.entities.values():
            if (
                isinstance(e, _V.Vehicle)
                and getattr(e, 'spaceID', None) == spaceID
                and getattr(e, 'inWorld', False)
            ):
                found = e
                break

    except Exception:
        LOG_CURRENT_EXCEPTION()

    if found is None:
        if triesLeft > 0:
            BigWorld.callback(
                0.1,
                lambda: _waitForVehicleEntity(avatar, vprops, compDescr, spaceID, triesLeft - 1, ammoDict, hangarVehicle=hangarVehicle, vehInvID=vehInvID)
            )
        else:
            LOG_ERROR('battle: vehicle wait timeout')

        return

    for k, v in vprops.items():
        try:
            setattr(found, k, v)
        except Exception:
            pass

    LOG_DEBUG(
        'battle: vehicle appeared async id=%s inWorld=%s' % (
            getattr(found, 'id', None),
            found.inWorld,
        )
    )

    _finishVehicleSetup(avatar, found, compDescr, ammoDict, hangarVehicle=hangarVehicle, vehInvID=vehInvID)


def _finishVehicleSetup(avatar, vehicle, compDescr, ammoDict, hangarVehicle=None, vehInvID=None):
    try:
        vid = getattr(vehicle, 'id', 0)

        if vid:
            try:
                avatar.playerVehicleID = vid
                LOG_DEBUG('battle: playerVehicleID set = %s' % vid)
            except Exception:
                LOG_CURRENT_EXCEPTION()

            try:
                vehicle.isPlayer = True
            except Exception:
                LOG_CURRENT_EXCEPTION()

            try:
                avatar.updateArena(
                    ARENA_UPDATE.VEHICLE_LIST,
                    cPickle.dumps([_vehInfoTuple(vid or 1, compDescr, 1)])
                )
            except Exception:
                LOG_CURRENT_EXCEPTION()

            try:
                _startPrebattleCountdown(avatar)
            except Exception:
                LOG_CURRENT_EXCEPTION()

            try:
                from gui.mods.offhangar import bots as off_bots
                off_bots.onBattleStart(avatar)
            except Exception:
                LOG_CURRENT_EXCEPTION()

            BigWorld.callback(0.3, lambda: _finalize(avatar, 10))

            try:
                # Build prereqs mapping (model name -> BigWorld.Model) so Vehicle.appearance.start
                # can access loaded segment models (spline tracks) in offline mode.
                try:
                    names = []
                    try:
                        names = vehicle.prerequisites() or []
                    except Exception:
                        LOG_CURRENT_EXCEPTION()

                    prereqs_map = {}
                    for nm in set(names):
                        try:
                            if not nm:
                                continue
                            # attempt to load model; skip non-model resource names
                            if isinstance(nm, basestring) or isinstance(nm, str):
                                if nm.endswith('.model') or '/' in nm:
                                    m = BigWorld.Model(nm)
                                    prereqs_map[nm] = m
                        except Exception:
                            LOG_CURRENT_EXCEPTION()

                    try:
                        vehicle._Vehicle__prereqs = prereqs_map
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                except Exception:
                    LOG_CURRENT_EXCEPTION()

                avatar.vehicle_onEnterWorld(vehicle)
            except Exception:
                LOG_CURRENT_EXCEPTION()

            _startTurretRotation(avatar, vehicle)

            try:
                from gui.mods.offhangar import shooting as off_shooting
                if hangarVehicle is None and vehInvID is not None:
                    try:
                        hangarVehicle = off_shooting._resolveHangarVehicle(hangarVehicle=None, vehInvID=vehInvID)
                        if hangarVehicle is not None:
                            LOG_DEBUG('battle: resolved hangarVehicle from vehInvID=%s for consumables sync', vehInvID)
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                off_shooting.initAmmoForVehicle(vehicle, ammoDict)
                try:
                    _deferConsumablesSync(avatar, vehicle, hangarVehicle, vehInvID)
                    try:
                        BigWorld.callback(0.35, lambda: off_shooting.pushInitialAmmoUpdate(avatar, vehicle, hangarVehicle=hangarVehicle, vehInvID=vehInvID))
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                except Exception:
                    LOG_CURRENT_EXCEPTION()
            except Exception:
                LOG_CURRENT_EXCEPTION()

            try:
                _startVehicleEngineSound(vehicle)
            except Exception:
                LOG_CURRENT_EXCEPTION()

            try:
                avatar.updateArena(
                    ARENA_UPDATE.VEHICLE_LIST,
                    cPickle.dumps([_vehInfoTuple(vid or 1, compDescr, 1)])
                )
            except Exception:
                LOG_CURRENT_EXCEPTION()

            BigWorld.callback(0.3, lambda: _finalize(avatar, 10))

    except Exception:
        LOG_CURRENT_EXCEPTION()


def _finalize(avatar, tries):
    if avatar is None or not getattr(avatar, 'inWorld', False):
        if tries > 0:
            BigWorld.callback(0.2, lambda: _finalize(avatar, tries - 1))

        return

    if getattr(avatar, 'gunRotator', None) is None and tries > 0:
        BigWorld.callback(0.2, lambda: _finalize(avatar, tries - 1))
        return

    for _ in range(6):
        if avatar.userSeesWorld():
            break

        try:
            avatar.onSpaceLoaded()
        except Exception:
            LOG_CURRENT_EXCEPTION()

    LOG_DEBUG('battle: finalize userSeesWorld=%s' % avatar.userSeesWorld())

    try:
        avatar._PlayerAvatar__setIsOnArena(True)
        LOG_DEBUG('battle: forced isOnArena=True')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _battleGuiReady():
    try:
        from gui.WindowsManager import g_windowsManager
        w = g_windowsManager.battleWindow
        return w is not None and w.consumablesPanel is not None
    except Exception:
        return False


def _deferConsumablesSync(avatar, vehicle, hangarVehicle, vehInvID, tries=120):
    """Заливаем расходники в боевую панель, когда она создана."""
    if not _BATTLE_ACTIVE:
        return
    if _battleGuiReady():
        try:
            from gui.mods.offhangar import shooting as off_shooting
            off_shooting.syncHangarConsumablesToAvatar(avatar, vehicle, hangarVehicle=hangarVehicle, vehInvID=vehInvID)
            LOG_DEBUG('battle: consumables synced into battle GUI')
        except Exception:
            LOG_CURRENT_EXCEPTION()
        return
    if tries > 0:
        BigWorld.callback(0.25, lambda: _deferConsumablesSync(avatar, vehicle, hangarVehicle, vehInvID, tries - 1))
    else:
        LOG_DEBUG('battle: consumables panel never became ready')


_NEXT_KIND = [None]
_TUT_VEHICLE = [None]      # (compDescr, ammoDict) учебной машины для следующего учебного боя (ставит tutor.py)


def setTutorialVehicle(compDescr, ammoDict=None):
    _TUT_VEHICLE[0] = (compDescr, ammoDict) if compDescr else None


def _trainingExtraData():
    """Подпись на экране загрузки: клиент берёт её из localized_data (как у боёв командных сессий) -> "Тренировочный бой: <название комнаты>"."""
    comment = u'Тренировка'
    try:
        from gui.mods.offhangar import fakeprb
        c = (fakeprb._T.get('settings') or {}).get('comment')
        if c:
            comment = c if isinstance(c, unicode) else c.decode('utf-8', 'replace')
    except Exception:
        LOG_CURRENT_EXCEPTION()
    loc = {'event_name': u'Тренировочный бой', 'session_name': comment, 'desc': u'Тренировочный бой'}
    return {'localized_data': {'ru': dict(loc), 'en': dict(loc)}}


def setNextBattleKind(kind):
    """Следующий бой - особого типа: 'tutorial' (учебный: по нему клиент включает обучение), 'training' (тренировочный: без наград и статистики)."""
    _NEXT_KIND[0] = kind


def _startBattle(vehInvID, arenaTypeID=0):
    global _BATTLE_ACTIVE, _PENDING_CTX

    _BATTLE_ACTIVE = True
    _BROKEN_ITEMS.clear()
    _DEST_ITEM_CACHE.clear()
    _resetCountdown()
    _SMOOTH_SET.clear()

    try:
        from gui.mods.offhangar import endbattle as off_endbattle
        off_endbattle.onBattleStart(vehInvID)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        from CurrentVehicle import g_currentVehicle

        compDescr = None
        ammoDict = None
        hangarVehicle = None

        try:
            if g_currentVehicle.isPresent():
                hangarVehicle = g_currentVehicle.item
                compDescr = hangarVehicle.descriptor.makeCompactDescr()
                ammoDict = dict(((s.intCD, s.count) for s in hangarVehicle.shells))
        except Exception:
            LOG_CURRENT_EXCEPTION()

        if hangarVehicle is None and vehInvID:
            try:
                from gui.shared import g_itemsCache
                hangarVehicle = g_itemsCache.items.getVehicle(vehInvID)
                if hangarVehicle is not None:
                    compDescr = hangarVehicle.descriptor.makeCompactDescr()
                    ammoDict = dict(((s.intCD, s.count) for s in hangarVehicle.shells))
            except Exception:
                LOG_CURRENT_EXCEPTION()

        if hangarVehicle is not None and vehInvID is None:
            try:
                vehInvID = getattr(hangarVehicle, 'invID', None)
                LOG_DEBUG('battle: resolved pending vehInvID=%s from hangarVehicle', vehInvID)
            except Exception:
                LOG_CURRENT_EXCEPTION()

        # учебный бой: вместо выбранной в ангаре машины - учебная (Т-34-85), её снаряжение и экипаж ангарной машины не берём
        try:
            from gui.mods.offhangar import shooting as _sh
            if _NEXT_KIND[0] == 'tutorial' and _TUT_VEHICLE[0] is not None:
                compDescr, ammoDict = _TUT_VEHICLE[0]
                hangarVehicle = None
                vehInvID = None
                _sh.NO_HANGAR[0] = True
                LOG_DEBUG('[TUTOR] tutorial vehicle is used instead of the hangar vehicle')
            else:
                _sh.NO_HANGAR[0] = False
        except Exception:
            LOG_CURRENT_EXCEPTION()

        if compDescr is None:
            try:
                inv = BigWorld.player().inventory._Inventory__cache.get(
                    ITEM_TYPE_INDICES['vehicle'],
                    {}
                )

                cds = inv.get('compDescr', {})

                if cds:
                    compDescr = cds[sorted(cds.keys())[0]]

            except Exception:
                LOG_CURRENT_EXCEPTION()

        if compDescr is None:
            LOG_ERROR('battle: no compDescr, abort')

            _BATTLE_ACTIVE = False
            return

        at_id = _chooseArenaTypeID(arenaTypeID)

        if at_id is None:
            LOG_ERROR('battle: no arena type, abort')

            _BATTLE_ACTIVE = False
            return

        at = ArenaType.g_cache[at_id]

        LOG_DEBUG('battle: arena %s (%s)' % (at.geometryName, at_id))

        pos = _spawnPos(at)

        spaceID = BigWorld.createSpace()
        geom = getattr(at, 'geometry', None) or ('spaces/' + at.geometryName)

        try:
            import AreaDestructibles

            if AreaDestructibles.g_destructiblesManager is None:
                AreaDestructibles.init()
                LOG_DEBUG('AreaDestructibles.init() called before geometry load')

            AreaDestructibles.g_destructiblesManager.startSpace(spaceID)

            LOG_DEBUG('AreaDestructibles.startSpace(%s) called before geometry load' % spaceID)

        except Exception:
            LOG_CURRENT_EXCEPTION()

        BigWorld.addSpaceGeometryMapping(spaceID, None, geom)

        LOG_DEBUG('battle: space=%s geom=%s' % (spaceID, geom))

        _kind = _NEXT_KIND[0]
        _NEXT_KIND[0] = None
        try:
            from gui.mods.offhangar import bots as _off_bots
            _off_bots.TUTORIAL[0] = (_kind == 'tutorial')
        except Exception:
            LOG_CURRENT_EXCEPTION()
        props = {
            'arenaUniqueID': int(BigWorld.time() * 1000) & 0x7fffffff,
            'arenaTypeID': at.id,
            'arenaBonusType': {'tutorial': ARENA_BONUS_TYPE.TUTORIAL, 'training': ARENA_BONUS_TYPE.TRAINING}.get(_kind, ARENA_BONUS_TYPE.REGULAR),
            'arenaGuiType': {'tutorial': ARENA_GUI_TYPE.TUTORIAL, 'training': ARENA_GUI_TYPE.TRAINING}.get(_kind, ARENA_GUI_TYPE.RANDOM),
            'arenaExtraData': _trainingExtraData() if _kind == 'training' else {},
            'weatherPresetID': 0,
            'team': 1,
            'playerVehicleID': 0,
            'name': OFFLINE_NICKNAME,
        }

        _PENDING_CTX = (props, at, compDescr, spaceID, ammoDict, hangarVehicle, vehInvID)

        avatar = BigWorld.createEntity(
            'Avatar',
            spaceID,
            0,
            pos,
            (0, 0, 0),
            props
        )

        if isinstance(avatar, (int, long)):
            avatar = BigWorld.entity(avatar)

        if avatar is not None:
            LOG_DEBUG('battle: avatar created synchronously')
        else:
            LOG_DEBUG('battle: avatar creation async, waiting for onEnterWorld')

    except Exception:
        LOG_CURRENT_EXCEPTION()

        _BATTLE_ACTIVE = False
        _PENDING_CTX = None


def _cmdReqQueueInfo(requestID, *args):
    """Окно ожидания боя запрашивает состав очереди: отвечаем успехом и сразу присылаем свежие числа."""
    try:
        from gui.mods.offhangar import queuefake
        BigWorld.callback(0.05, queuefake.pushNow)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return (requestID, AccountCommands.RES_SUCCESS, '', None)


def _cmdEnqueueRandom(requestID, vehInvID, gameplaysMask, arenaTypeID):
    LOG_DEBUG('battle: CMD_ENQUEUE_RANDOM vehInvID=%s gameplaysMask=%s arenaTypeID=%s' % (
        vehInvID, gameplaysMask, arenaTypeID))

    if not _isOffline():
        return (requestID, AccountCommands.RES_SUCCESS, '', None)

    try:
        from gui.mods.offhangar import requests as _off_req
        if not _off_req.checkCrewForBattle(vehInvID)[0]:
            return (requestID, AccountCommands.RES_FAILURE, '', None)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        p = BigWorld.player()

        p.onEnqueued(QUEUE_TYPE.RANDOMS)          # родной путь открывает экран ожидания боя

        def _begin():
            try:
                BigWorld.player().onArenaCreated()
            except Exception:
                LOG_CURRENT_EXCEPTION()
            BigWorld.callback(0.1, lambda: _startBattle(vehInvID, arenaTypeID))
        from gui.mods.offhangar import queuefake
        queuefake.waitThen(_begin)               # экран ожидания с живыми числами, затем бой
        return (requestID, AccountCommands.RES_SUCCESS, '', None)

    except Exception:
        LOG_CURRENT_EXCEPTION()

    BigWorld.callback(0.1, lambda: _startBattle(vehInvID, arenaTypeID))

    return (requestID, AccountCommands.RES_SUCCESS, '', None)



# ============================================================
# Registration
# ============================================================
def register():
    global _REGISTERED
    global _ORIG_INIT_STEP
    global _ORIG_ON_ENTER_WORLD
    global _ORIG_ON_LEAVE_WORLD

    if _REGISTERED:
        return

    _REGISTERED = True

    BASE_REQUESTS[AccountCommands.CMD_ENQUEUE_RANDOM] = _cmdEnqueueRandom
    _cmdQI = getattr(AccountCommands, 'CMD_REQ_QUEUE_INFO', None)
    if _cmdQI is not None:
        BASE_REQUESTS[_cmdQI] = _cmdReqQueueInfo
    else:
        LOG_DEBUG('[QUEUE] AccountCommands.CMD_REQ_QUEUE_INFO not found - queue info is pushed by timer only')

    _patchDestructibleDescLookup()

    import Avatar as _A

    cls = _A.PlayerAvatar

    _ORIG_INIT_STEP = getattr(cls, '_PlayerAvatar__onInitStepCompleted', None)
    cls._PlayerAvatar__onInitStepCompleted = _offlineInitStep

    _patchLockPrediction()
    _orig_teleport = getattr(cls, 'teleportVehicle', None)
    cls.teleportVehicle = lambda self, position, yaw, _o=_orig_teleport: _patchedTeleportVehicle(self, position, yaw, _o)

    _ORIG_ON_ENTER_WORLD = cls.onEnterWorld
    cls.onEnterWorld = _onEnterWorld

    _ORIG_ON_LEAVE_WORLD = cls.onLeaveWorld
    cls.onLeaveWorld = _onLeaveWorld

    _patchVehicleStartVisual()
    _patchEdgeDetect()
    _patchOwnSpeeds()
    _patchSplineTracks()
    _patchAvatarPositionControl()
    _patchAvatarBaseCell()
    _patchVehicleNativePhysics()
    _patchVehicleAppearanceHavok()
    _patchEngineSoundNative()
    _patchAvatarMoveVehicle()
    _patchAvatarOwnVehicleMatrix()
    _patchAvatarInputHandlerDetach() 

    _patchServerTime()
    _patchTrackKeys()

    try:
        from gui.mods.offhangar import endbattle as off_endbattle
        off_endbattle.register()
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        from gui.mods.offhangar import bots as off_bots
        off_bots.register()
    except Exception:
        LOG_CURRENT_EXCEPTION()

    LOG_NOTE('offline battle module registered')