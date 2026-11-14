# -*- coding: utf-8 -*-
import math
import BigWorld
import Math

from constants import VEHICLE_SETTING
try:
    from constants import EQUIPMENT_STAGES
except ImportError:
    class EQUIPMENT_STAGES(object):  # нет в 0.9.0
        READY = 1
from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE, LOG_ERROR
from ModelHitTester import SegmentCollisionResult
from helpers.EffectMaterialCalculation import calcSurfaceMaterialNearPoint
import SoundGroups
_REGISTERED = False
_gunState = {}
_SHOT_ID_COUNTER = 0
_LAST_HANGAR_VEHICLE = None
_LAST_HANGAR_VEHICLE_INV_ID = None


def _flashPlayerHitDirection(target, shooterID):
    try:
        av = BigWorld.player()
        if av is None or getattr(av, 'playerVehicleID', None) != getattr(target, 'id', None): return
        shooter = BigWorld.entity(shooterID)
        if shooter is None: return
        sp, tp = shooter.position, target.position
        yaw = math.atan2(tp.x - sp.x, tp.z - sp.z)
        av.showOwnVehicleHitDirection(yaw, True)
        LOG_DEBUG('[HIT-FLASH] native incoming-hit indicator triggered')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _playPlayerHitSound(target):
    """Play a short hit cue only when the player's own vehicle loses HP.

    Sound event names vary between client builds, so try several known-style
    event names and fail silently if this client has none of them.
    """
    try:
        av = BigWorld.player()
        if av is None or getattr(av, 'playerVehicleID', None) != getattr(target, 'id', None):
            return
        sg = getattr(SoundGroups, 'g_instance', None)
        if sg is None:
            return
        for event in ('tank_hit', 'hit_armor', 'vo_enemyShot'):
            try:
                sound = sg.FMODgetSound(event)
                if sound is not None:
                    sound.play()
                    LOG_DEBUG('[SOUND] player hit cue played: %s' % event)
                    return
            except Exception:
                pass
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _nextShotID():
    global _SHOT_ID_COUNTER
    _SHOT_ID_COUNTER += 1
    return _SHOT_ID_COUNTER

def _active():
    from gui.mods.offhangar import battle as off_battle
    return off_battle._active()


def _initVehicleAmmo(vehicle):
    from items import vehicles as items_vehicles

    gunDescr = vehicle.typeDescriptor.gun
    ammo = items_vehicles.getUniformAmmoForGun(gunDescr)
    state = {'ammo': {}, 'reloadEnd': 0.0}
    it = iter(ammo)
    for cd, count in zip(it, it):
        state['ammo'][cd] = count
    _gunState[vehicle.id] = state
    return state


def initAmmoForVehicle(vehicle, ammoDict):
    gunDescr = vehicle.typeDescriptor.gun
    state = {'ammo': {}, 'reloadEnd': 0.0}
    if ammoDict:
        for cd, count in ammoDict.items():
            state['ammo'][cd] = count
    else:
        from items import vehicles as items_vehicles
        ammo = items_vehicles.getUniformAmmoForGun(gunDescr)
        it = iter(ammo)
        for cd, count in zip(it, it):
            state['ammo'][cd] = count
    _gunState[vehicle.id] = state
    return state


def _getItemCompactDescr(item):
    if item is None:
        return None

    try:
        if isinstance(item, (int, long)):
            return item
    except Exception:
        if isinstance(item, int):
            return item

    if isinstance(item, dict):
        return item.get('compactDescr') or item.get('intCD')

    for attr in ('intCD', 'compactDescr'):
        value = getattr(item, attr, None)
        if value:
            return value

    try:
        return item.compactDescr
    except Exception:
        return None


CONSUMABLES_ALWAYS_ACTIVE = True   # ремкомплекты/аптечки/огнетушитель в бою не серые, даже если в ангаре их 0 шт.


def _getHangarConsumables(hangarVehicle):
    result = []
    if hangarVehicle is None:
        return result

    try:
        eqs = getattr(hangarVehicle, 'eqs', None) or []
        for eq in eqs:
            intCD = _getItemCompactDescr(eq)
            if not intCD:
                continue
            quantity = getattr(eq, 'count', None)
            if quantity is None:
                quantity = getattr(eq, 'inventoryCount', None)
            if quantity is None:
                quantity = 1
            quantity = int(quantity)
            if CONSUMABLES_ALWAYS_ACTIVE and quantity < 1:
                quantity = 1          # ноль штук - слот серый; пока показываем как доступный
            result.append((intCD, quantity, EQUIPMENT_STAGES.READY, 0))
    except Exception:
        LOG_CURRENT_EXCEPTION()

    return result


def _getHangarOptionalDevices(hangarVehicle):
    result = []
    if hangarVehicle is None:
        return result

    try:
        optDevices = getattr(hangarVehicle, 'optDevices', None) or []
        if optDevices:
            for device in optDevices:
                intCD = _getItemCompactDescr(device)
                if not intCD:
                    continue
                try:
                    from items import vehicles as items_vehicles
                    deviceID = items_vehicles.parseIntCompactDescr(intCD)[-1]
                except Exception:
                    deviceID = None
                if deviceID is None:
                    continue
                result.append((deviceID, True))
            return result
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        descriptor = getattr(hangarVehicle, 'descriptor', None)
        devices = getattr(descriptor, 'optionalDevices', None) or []
        for device in devices:
            intCD = _getItemCompactDescr(device)
            if not intCD:
                continue
            try:
                from items import vehicles as items_vehicles
                deviceID = items_vehicles.parseIntCompactDescr(intCD)[-1]
            except Exception:
                deviceID = None
            if deviceID is None:
                continue
            result.append((deviceID, True))
    except Exception:
        LOG_CURRENT_EXCEPTION()

    return result


_TUT_BOUNCED = [False]  # учебный бой: первый выстрел по боту 2 уже отскочил
NO_HANGAR = [False]     # учебный бой на учебной машине: снаряжение/экипаж ангарной машины не применяем


def _resolveHangarVehicle(hangarVehicle=None, vehInvID=None):
    global _LAST_HANGAR_VEHICLE, _LAST_HANGAR_VEHICLE_INV_ID

    if NO_HANGAR[0]:
        return None

    if hangarVehicle is not None:
        return hangarVehicle

    if vehInvID is not None:
        try:
            from gui.shared import g_itemsCache
            hangarVehicle = g_itemsCache.items.getVehicle(vehInvID)
            if hangarVehicle is not None:
                return hangarVehicle
        except Exception:
            LOG_CURRENT_EXCEPTION()

    if _LAST_HANGAR_VEHICLE is not None:
        LOG_DEBUG('[OFFHANGAR] _resolveHangarVehicle: using cached hangar vehicle invID=%s', getattr(_LAST_HANGAR_VEHICLE, 'invID', None))
        return _LAST_HANGAR_VEHICLE

    if _LAST_HANGAR_VEHICLE_INV_ID is not None:
        try:
            from gui.shared import g_itemsCache
            hangarVehicle = g_itemsCache.items.getVehicle(_LAST_HANGAR_VEHICLE_INV_ID)
            if hangarVehicle is not None:
                LOG_DEBUG('[OFFHANGAR] _resolveHangarVehicle: using cached hangar invID lookup %s', _LAST_HANGAR_VEHICLE_INV_ID)
                return hangarVehicle
        except Exception:
            LOG_CURRENT_EXCEPTION()

    g_currentVehicle = None
    try:
        from CurrentVehicle import g_currentVehicle
    except Exception:
        g_currentVehicle = None

    if g_currentVehicle is not None:
        try:
            hangarVehicle = getattr(g_currentVehicle, 'item', None)
            if hangarVehicle is not None:
                return hangarVehicle
        except Exception:
            LOG_CURRENT_EXCEPTION()

        try:
            from gui.shared import g_itemsCache
            currentInvID = getattr(g_currentVehicle, 'invID', None)
            if currentInvID:
                hangarVehicle = g_itemsCache.items.getVehicle(currentInvID)
                if hangarVehicle is not None:
                    return hangarVehicle
        except Exception:
            LOG_CURRENT_EXCEPTION()

    return None


def syncHangarConsumablesToAvatar(avatar, vehicle, hangarVehicle=None, vehInvID=None):
    global _LAST_HANGAR_VEHICLE, _LAST_HANGAR_VEHICLE_INV_ID

    if avatar is None or vehicle is None:
        return

    hangarVehicle = _resolveHangarVehicle(hangarVehicle=hangarVehicle, vehInvID=vehInvID)
    if hangarVehicle is not None:
        _LAST_HANGAR_VEHICLE = hangarVehicle
    if vehInvID is not None:
        _LAST_HANGAR_VEHICLE_INV_ID = vehInvID
    elif hangarVehicle is not None:
        _LAST_HANGAR_VEHICLE_INV_ID = getattr(hangarVehicle, 'invID', None)

    LOG_DEBUG('[OFFHANGAR] syncHangarConsumablesToAvatar: avatar=%s vehicle=%s hangarVehicle=%s vehInvID=%s', getattr(avatar, 'id', None), getattr(vehicle, 'id', None), getattr(hangarVehicle, 'invID', None) if hangarVehicle is not None else None, vehInvID)

    for intCD, quantity, stage, timeRemaining in _getHangarConsumables(hangarVehicle):
        try:
            LOG_DEBUG('[OFFHANGAR] syncHangarConsumablesToAvatar shell/equipment push: intCD=%s quantity=%s stage=%s timeRemaining=%s', intCD, quantity, stage, timeRemaining)
            avatar.updateVehicleAmmo(intCD, quantity, stage, timeRemaining)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    for deviceID, isOn in _getHangarOptionalDevices(hangarVehicle):
        try:
            LOG_DEBUG('[OFFHANGAR] syncHangarConsumablesToAvatar optional device push: deviceID=%s isOn=%s', deviceID, isOn)
            avatar.updateVehicleOptionalDeviceStatus(deviceID, isOn)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    return


_CONSUMABLES_PATCHED = False


def initAmmoForVehicle(vehicle, ammoDict):
    gunDescr = vehicle.typeDescriptor.gun
    state = {'ammo': {}, 'reloadEnd': 0.0}
    if ammoDict:
        for cd, count in ammoDict.items():
            state['ammo'][cd] = count
    else:
        from items import vehicles as items_vehicles
        ammo = items_vehicles.getUniformAmmoForGun(gunDescr)
        it = iter(ammo)
        for cd, count in zip(it, it):
            state['ammo'][cd] = count
    _gunState[vehicle.id] = state
    return state


SHELLS_IN_GUN_ORDER = True      # снаряды в панели боя - в порядке орудия (как в игре): 1, 2, 3 = бронебойный, подкалиберный/кумулятивный, фугас


def _shellPushOrder(vehicle, ammo):
    """Порядок, в котором снаряды отправляются в панель боя. Раньше шли в порядке словаря (по хэшу номера снаряда),
    поэтому слоты 1-3 оказывались в разнобой. Панель раздаёт слоты и клавиши по порядку прихода."""
    order = []
    if SHELLS_IN_GUN_ORDER:
        try:
            for shot in vehicle.typeDescriptor.gun['shots']:
                cd = shot['shell']['compactDescr']
                if cd in ammo and cd not in order:
                    order.append(cd)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    for cd in ammo.keys():
        if cd not in order:
            order.append(cd)
    return order


def pushInitialAmmoUpdate(avatar, vehicle, hangarVehicle=None, vehInvID=None):
    _SWITCH['quiet'] += 1
    try:
        _pushInitialAmmoUpdate(avatar, vehicle, hangarVehicle, vehInvID)
    finally:
        _SWITCH['quiet'] -= 1


def _pushInitialAmmoUpdate(avatar, vehicle, hangarVehicle=None, vehInvID=None):
    st = _getState(vehicle)
    gunDescr = vehicle.typeDescriptor.gun

    try:
        LOG_DEBUG('[OFFHANGAR] pushInitialAmmoUpdate: avatar=%s vehicle=%s', getattr(avatar, 'id', None), getattr(vehicle, 'id', None))
        for cd in _shellPushOrder(vehicle, st['ammo']):
            count = st['ammo'][cd]
            LOG_DEBUG('[OFFHANGAR] pushInitialAmmoUpdate shells: cd=%s count=%s', cd, count)
            clipSize, _clipInterval = _clipParams(gunDescr)
            if clipSize > 1:
                st.setdefault('inClip', {})[cd] = min(clipSize, count)
                avatar.updateVehicleAmmo(cd, count, st['inClip'][cd], -1)
            else:
                avatar.updateVehicleAmmo(cd, count, count, -1)

        currentCD = _currentShellCD(vehicle)
        LOG_DEBUG('[OFFHANGAR] pushInitialAmmoUpdate current shell=%s', currentCD)
        avatar.updateVehicleSetting(VEHICLE_SETTING.CURRENT_SHELLS, currentCD)

        avatar.updateVehicleGunReloadTime(vehicle.id, 0.0, gunDescr['reloadTime'])
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        syncHangarConsumablesToAvatar(avatar, vehicle, hangarVehicle=hangarVehicle, vehInvID=vehInvID)
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ============================================================
# Смена снарядов в бою: перезарядка при смене и автопереключение, когда тип снарядов кончился
# ============================================================
SHELL_SWITCH_RELOADS = True      # смена текущего снаряда (двойное нажатие клавиши) перезаряжает орудие заново, как в игре
AUTO_SWITCH_EMPTY_SHELL = True   # когда снаряды текущего типа кончились - орудие само переключается на другой тип (с перезарядкой)
APPLY_NEXT_SHELL = True          # снаряд, выбранный одним нажатием ("следующий"), заряжается после ближайшего выстрела
_SWITCH = {'quiet': 0}


def _avatarAmmo(avatar):
    """{слот панели: (compactDescr, количество, в магазине)} - приватное поле родного Avatar."""
    try:
        return getattr(avatar, '_PlayerAvatar__ammo')
    except Exception:
        return {}


def _beginReload(avatar, vehicle, st, reloadTime):
    """Запустить перезарядку игрока с нуля (после смены снаряда)."""
    vid = vehicle.id
    now = BigWorld.time()
    st['reloadEnd'] = now + reloadTime
    cd = _currentShellCD(vehicle)
    gunDescr = vehicle.typeDescriptor.gun
    clipSize, _ = _clipParams(gunDescr)
    count = st['ammo'].get(cd, 0)
    if clipSize > 1:
        st.setdefault('inClip', {})[cd] = 0
    try:
        avatar.updateVehicleAmmo(cd, count, 0 if clipSize > 1 else count, -1)
        avatar.updateVehicleGunReloadTime(vid, reloadTime, reloadTime)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    expectedEnd = st['reloadEnd']

    def _done():
        try:
            st2 = _gunState.get(vid)
            veh = BigWorld.entity(vid)
            av = BigWorld.player()
            if st2 is None or veh is None or av is None:
                return
            if abs(st2.get('reloadEnd', 0.0) - expectedEnd) > 0.05:
                return                      # за это время началась другая перезарядка
            cdNow = _currentShellCD(veh)
            n = st2['ammo'].get(cdNow, 0)
            cs, _ = _clipParams(veh.typeDescriptor.gun)
            if cs > 1:
                st2.setdefault('inClip', {})[cdNow] = min(cs, n)
            clipN = st2.get('inClip', {}).get(cdNow, n) if cs > 1 else n
            av.updateVehicleAmmo(cdNow, n, clipN, -1)
            av.updateVehicleGunReloadTime(vid, 0.0, reloadTime)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    BigWorld.callback(reloadTime, _done)


def _afterShellSwitch(avatar, beforeCD):
    """CURRENT_SHELLS поменялся (игрок нажал клавишу дважды) - орудие заряжается заново, если было заряжено."""
    if not SHELL_SWITCH_RELOADS or _SWITCH['quiet']:
        return
    try:
        from gui.mods.offhangar import battle as off_battle
        if off_battle.isPrebattle():
            return
    except Exception:
        pass
    vehicle = BigWorld.entity(avatar.playerVehicleID)
    if vehicle is None:
        return
    afterCD = _currentShellCD(vehicle)
    if afterCD == beforeCD:
        return
    st = _getState(vehicle)
    if BigWorld.time() < st['reloadEnd'] - 0.01:
        LOG_DEBUG('[SHELLS] switched during reload: reload continues')
        return                              # орудие и так перезаряжается - выбранный снаряд будет заряжен в конце
    gun = vehicle.typeDescriptor.gun
    rt = max(0.1, float(gun.get('reloadTime', 5.0)) * _reloadFactor(vehicle.id))
    LOG_DEBUG('[SHELLS] shell switched %s -> %s: reload restarted (%.1fs)' % (beforeCD, afterCD, rt))
    _beginReload(avatar, vehicle, st, rt)


def _selectShellAfterShot(avatar, vehicle, st, fullReload):
    """После выстрела: заряжаем выбранный "следующий" снаряд; если снаряды текущего типа кончились - переключаем на другой."""
    if not fullReload:
        return
    cur = _currentShellCD(vehicle)
    want = cur
    nextCD = None
    if APPLY_NEXT_SHELL:
        try:
            idx = getattr(avatar, '_PlayerAvatar__nextShellsIdx')
            nextCD = _avatarAmmo(avatar).get(idx, (None,))[0]
        except Exception:
            nextCD = None
    if nextCD is not None and nextCD != cur and st['ammo'].get(nextCD, 0) > 0:
        want = nextCD
    if AUTO_SWITCH_EMPTY_SHELL and st['ammo'].get(want, 0) <= 0:
        for cd in _shellPushOrder(vehicle, st['ammo']):
            if st['ammo'].get(cd, 0) > 0:
                want = cd
                break
    if want == cur:
        return
    LOG_DEBUG('[SHELLS] after shot: loading %s instead of %s (empty=%s)' % (want, cur, st['ammo'].get(cur, 0) <= 0))
    _SWITCH['quiet'] += 1                   # перезарядка уже идёт - дополнительного сброса не нужно
    try:
        avatar.updateVehicleSetting(VEHICLE_SETTING.CURRENT_SHELLS, want)
        avatar.updateVehicleSetting(VEHICLE_SETTING.NEXT_SHELLS, want)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    finally:
        _SWITCH['quiet'] -= 1


def _patchShellSwitch():
    try:
        import Avatar as _A
        cls = _A.PlayerAvatar
        orig = cls.updateVehicleSetting

        def patched(self, code, value, orig=orig):
            if not _active() or code != VEHICLE_SETTING.CURRENT_SHELLS or _SWITCH['quiet']:
                return orig(self, code, value)
            before = None
            try:
                v = BigWorld.entity(self.playerVehicleID)
                if v is not None:
                    before = _currentShellCD(v)
            except Exception:
                before = None
            res = orig(self, code, value)
            try:
                if before is not None:
                    _afterShellSwitch(self, before)
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return res

        cls.updateVehicleSetting = patched
        LOG_NOTE('[OFFHANGAR] shell switch patch installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _patchConsumablesPanel():
    global _CONSUMABLES_PATCHED

    if _CONSUMABLES_PATCHED:
        return

    _CONSUMABLES_PATCHED = True

    try:
        from gui.Scaleform.daapi.view.battle.ConsumablesPanel import ConsumablesPanel

        _origStart = ConsumablesPanel.start

        def _safeStart(self, orig=_origStart):
            orig(self)

            if _active():
                def _doInitialUpdate():
                    try:
                        avatar = BigWorld.player()
                        if avatar is None:
                            LOG_DEBUG('[OFFHANGAR] ConsumablesPanel start: avatar not ready')
                            BigWorld.callback(0.1, _doInitialUpdate)
                            return
                        vehicle = BigWorld.entity(getattr(avatar, 'playerVehicleID', 0))
                        if vehicle is None:
                            LOG_DEBUG('[OFFHANGAR] ConsumablesPanel start: player vehicle not ready, retry')
                            BigWorld.callback(0.1, _doInitialUpdate)
                            return
                        LOG_DEBUG('[OFFHANGAR] ConsumablesPanel start: updating consumables for vehicle id=%s', vehicle.id)
                        pushInitialAmmoUpdate(avatar, vehicle, hangarVehicle=_LAST_HANGAR_VEHICLE, vehInvID=_LAST_HANGAR_VEHICLE_INV_ID)
                    except Exception:
                        LOG_CURRENT_EXCEPTION()

                try:
                    _doInitialUpdate()
                except Exception:
                    LOG_CURRENT_EXCEPTION()

        ConsumablesPanel.start = _safeStart
        LOG_NOTE('[OFFHANGAR] ConsumablesPanel start patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()


def _getState(vehicle):
    st = _gunState.get(vehicle.id)
    if st is None:
        st = _initVehicleAmmo(vehicle)
    return st


def _currentShellCD(vehicle):
    gunDescr = vehicle.typeDescriptor.gun
    return gunDescr['shots'][vehicle.typeDescriptor.activeGunShotIndex]['shell']['compactDescr']


def _clipParams(gunDescr):
    """(размер магазина, интервал между выстрелами в магазине). Обычное орудие: (1, 0.0)."""
    try:
        clip = gunDescr['clip']
        return int(clip[0]), float(clip[1])
    except Exception:
        return 1, 0.0


def _clipQuantity(st, gunDescr, shellCD):
    """Сколько снарядов сейчас в магазине для данного типа снаряда."""
    clipSize, _ = _clipParams(gunDescr)
    inClip = st.setdefault('inClip', {})
    if shellCD not in inClip:
        inClip[shellCD] = min(clipSize, st['ammo'].get(shellCD, 0))
    return inClip[shellCD]


def _canShoot(vehicle):
    try:
        from gui.mods.offhangar import devices as _dev
        if not _dev.canShoot(vehicle.id):
            return False                    # орудие разбито
    except Exception:
        pass
    st = _getState(vehicle)
    if BigWorld.time() < st['reloadEnd']:
        return False
    return st['ammo'].get(_currentShellCD(vehicle), 0) > 0



def _reloadFactor(vid):
    """Множитель перезарядки от экипажа, модулей и оборудования (devices.py)."""
    try:
        from gui.mods.offhangar import devices as _dev
        return _dev.reloadFactor(vid)
    except Exception:
        return 1.0


def rescaleReload(vid, oldFactor, newFactor):
    """Мгновенно пересчитать ОСТАТОК текущей перезарядки, когда изменился множитель (модуль/экипаж повреждён или починен).
    Пример: орудие заряжается, ещё 3 с; сбили боеукладку (x1.2 -> x1.4): остаток сразу становится 3 * 1.4 / 1.2 = 3.5 с.
    Доля уже выполненной перезарядки сохраняется. Для игрока обновляется шкала перезарядки."""
    try:
        if oldFactor <= 0.0 or newFactor <= 0.0 or abs(newFactor - oldFactor) < 1e-3:
            return
        st = _gunState.get(vid)
        veh = BigWorld.entity(vid)
        if st is None or veh is None:
            return
        now = BigWorld.time()
        remaining = float(st.get('reloadEnd', 0.0)) - now
        if remaining <= 0.05:
            return                                  # орудие не перезаряжается
        gun = veh.typeDescriptor.gun
        clipSize, clipInterval = _clipParams(gun)
        if clipSize > 1 and clipInterval > 0.0 and remaining <= clipInterval * 1.05:
            return                                  # короткий интервал между выстрелами барабана от модулей не зависит
        newRemaining = max(0.05, remaining * newFactor / oldFactor)
        st['reloadEnd'] = now + newRemaining
        expectedEnd = st['reloadEnd']
        total = max(newRemaining, float(gun.get('reloadTime', 5.0)) * newFactor)
        av = BigWorld.player()
        isPlayer = av is not None and getattr(av, 'playerVehicleID', None) == vid
        if isPlayer:
            try:
                av.updateVehicleGunReloadTime(vid, newRemaining, total)
            except Exception:
                LOG_CURRENT_EXCEPTION()

        def _done():
            try:
                st2 = _gunState.get(vid)
                veh2 = BigWorld.entity(vid)
                if st2 is None or veh2 is None:
                    return
                if abs(st2.get('reloadEnd', 0.0) - expectedEnd) > 0.05:
                    return                          # за это время перезарядка пересчитана ещё раз или началась новая
                cdNow = _currentShellCD(veh2)
                n = st2['ammo'].get(cdNow, 0)
                cs, _ = _clipParams(veh2.typeDescriptor.gun)
                if cs > 1:
                    st2.setdefault('inClip', {})[cdNow] = min(cs, n)
                av2 = BigWorld.player()
                if av2 is not None and getattr(av2, 'playerVehicleID', None) == vid:
                    clipN = st2.get('inClip', {}).get(cdNow, n) if cs > 1 else n
                    av2.updateVehicleAmmo(cdNow, n, clipN, -1)
                    av2.updateVehicleGunReloadTime(vid, 0.0, total)
            except Exception:
                LOG_CURRENT_EXCEPTION()

        BigWorld.callback(newRemaining, _done)
        LOG_DEBUG('[RELOAD] vehicle %s reload factor x%.2f -> x%.2f: %.1fs left -> %.1fs' % (
            vid, oldFactor, newFactor, remaining, newRemaining))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def startBattleReload():
    """Start a real reload cycle for every tracked vehicle when the countdown ends."""
    now = BigWorld.time()
    for vid, state in list(_gunState.items()):
        try:
            vehicle = BigWorld.entity(vid)
            if vehicle is None or not getattr(vehicle, 'isAlive', lambda: True)():
                continue
            gun = vehicle.typeDescriptor.gun
            reloadTime = max(0.1, float(gun.get('reloadTime', 5.0)) * _reloadFactor(vid))
            state['reloadEnd'] = now + reloadTime
            clipSize, _clipInterval = _clipParams(gun)
            shellCD = _currentShellCD(vehicle)
            state.setdefault('inClip', {})[shellCD] = 0
            # Reload UI is only available for the player's own vehicle.
            avatar = BigWorld.player()
            isPlayer = avatar is not None and getattr(avatar, 'playerVehicleID', None) == vid
            if isPlayer:
                try:
                    # Show an empty clip while the battle-start reload is in progress.
                    avatar.updateVehicleAmmo(shellCD, state['ammo'].get(shellCD, 0), 0 if clipSize > 1 else state['ammo'].get(shellCD, 0), -1)
                    avatar.updateVehicleGunReloadTime(vid, reloadTime, reloadTime)
                except Exception:
                    pass

            # Drum/autoloader tanks must have their clip restored when the initial
            # reload ends. Without this callback their in-clip count stays at zero.
            def _finishInitialReload(vehicleID=vid, shellCompactDescr=shellCD, expectedEnd=state['reloadEnd'], clipCapacity=clipSize, duration=reloadTime):
                try:
                    st = _gunState.get(vehicleID)
                    veh = BigWorld.entity(vehicleID)
                    if st is None or veh is None or not getattr(veh, 'isAlive', lambda: True)():
                        return
                    if abs(float(st.get('reloadEnd', 0.0)) - expectedEnd) > 0.05:
                        return
                    gunDescrNow = veh.typeDescriptor.gun
                    activeCD = _currentShellCD(veh)
                    if activeCD != shellCompactDescr:
                        shellCompactDescr = activeCD
                    currentClipSize, _ = _clipParams(gunDescrNow)
                    if currentClipSize > 1:
                        st.setdefault('inClip', {})[shellCompactDescr] = min(currentClipSize, st['ammo'].get(shellCompactDescr, 0))
                    av = BigWorld.player()
                    if av is not None and getattr(av, 'playerVehicleID', None) == vehicleID:
                        count = st['ammo'].get(shellCompactDescr, 0)
                        clipCount = st.get('inClip', {}).get(shellCompactDescr, count)
                        av.updateVehicleAmmo(shellCompactDescr, count, clipCount, -1)
                        av.updateVehicleGunReloadTime(vehicleID, 0.0, duration)
                    LOG_DEBUG('[RELOAD] vehicle %s initial reload finished; clip=%s' % (vehicleID, st.get('inClip', {}).get(shellCompactDescr, st['ammo'].get(shellCompactDescr, 0))))
                except Exception:
                    LOG_CURRENT_EXCEPTION()

            BigWorld.callback(reloadTime, _finishInitialReload)
            LOG_DEBUG('[RELOAD] vehicle %s starts battle unloaded, reload %.1fs' % (vid, reloadTime))
        except Exception:
            LOG_CURRENT_EXCEPTION()

def _shotOrigin(vehicle):
    turretYaw = Math.Matrix(vehicle.appearance.turretMatrix).yaw
    gunPitch = Math.Matrix(vehicle.appearance.gunMatrix).pitch
    vd = vehicle.typeDescriptor
    turretOffs = vd.hull['turretPositions'][0] + vd.chassis['hullPosition']

    turretMat = Math.Matrix()
    turretMat.setRotateY(turretYaw)
    turretMat.translation = turretOffs
    turretMat.postMultiply(vehicle.model.matrix)

    gunMat = Math.Matrix()
    gunMat.setRotateX(gunPitch)
    gunMat.postMultiply(turretMat)

    origin = gunMat.applyPoint(vd.turret['gunPosition'])
    direction = gunMat.applyVector(Math.Vector3(0, 0, 1))
    return (origin, direction)


_HIT_DIAG = {}


def _collideVehicle(e, start, end):
    """Аналог Vehicle.collideSegment без предварительной проверки по e.position:
    в офлайне модель двигает наша физика, а позиция сущности может отставать (у ботов - от точки спавна)."""
    try:
        res = e.collideSegment(start, end)
        if res is not None:
            return res
    except Exception:
        pass
    try:
        from ModelHitTester import SegmentCollisionResult
        m = Math.Matrix(e.model.matrix)
        m.invert()
        s = m.applyPoint(start)
        en = m.applyPoint(end)
        res = None
        for compDescr, compMatrix, isAttached in e.getComponents():
            if not isAttached:
                continue
            collisions = compDescr['hitTester'].localHitTest(compMatrix.applyPoint(s), compMatrix.applyPoint(en))
            if collisions is None:
                continue
            for dist, _, hitAngleCos, matKind in collisions:
                if res is None or res.dist >= dist:
                    matInfo = compDescr['materials'].get(matKind)
                    res = SegmentCollisionResult(dist, hitAngleCos, matInfo.armor if matInfo is not None else 0)
        return res
    except Exception:
        if not _HIT_DIAG.get('err'):
            _HIT_DIAG['err'] = True
            LOG_CURRENT_EXCEPTION()
        return None


def _bboxHit(e, start, end):
    """Запасная проверка попадания: луч против габаритных коробок шасси и корпуса (в локальных координатах модели).
    Нужна, когда покомпонентный hitTester у какой-то машины ничего не вернул (у нестандартных танков/ботов) -
    иначе по такой машине урон просто не проходит."""
    try:
        from ModelHitTester import SegmentCollisionResult
        vd = e.typeDescriptor
        m = Math.Matrix(e.model.matrix)
        m.invert()
        s0 = m.applyPoint(start)
        s1 = m.applyPoint(end)
        d = s1 - s0
        seglen = d.length
        if seglen < 1e-6:
            return None

        boxes = []
        ch = vd.chassis
        hullPos = ch.get('hullPosition', None)
        hp = Math.Vector3(0.0, 0.0, 0.0)
        if hullPos is not None:
            hp = Math.Vector3(float(hullPos[0]), float(hullPos[1]), float(hullPos[2]))
        ht = ch.get('hitTester', None)
        if ht is not None and getattr(ht, 'bbox', None) is not None:
            boxes.append((ht.bbox[0], ht.bbox[1], Math.Vector3(0.0, 0.0, 0.0)))
        ht = vd.hull.get('hitTester', None)
        if ht is not None and getattr(ht, 'bbox', None) is not None:
            boxes.append((ht.bbox[0], ht.bbox[1], hp))
        if not boxes:
            return None

        best = None
        for bmin, bmax, off in boxes:
            tmin, tmax, axis = 0.0, 1.0, 1
            ok = True
            for i in range(3):
                lo = float(bmin[i]) + float(off[i])
                hi = float(bmax[i]) + float(off[i])
                di = float(d[i])
                oi = float(s0[i])
                if abs(di) < 1e-9:
                    if oi < lo or oi > hi:
                        ok = False
                        break
                    continue
                t1 = (lo - oi) / di
                t2 = (hi - oi) / di
                if t1 > t2:
                    t1, t2 = t2, t1
                if t1 > tmin:
                    tmin, axis = t1, i
                if t2 < tmax:
                    tmax = t2
                if tmin > tmax:
                    ok = False
                    break
            if not ok:
                continue
            dist = tmin * seglen
            if best is None or dist < best[0]:
                best = (dist, abs(float(d[axis])) / seglen)
        if best is None:
            return None

        # броня: самая слабая ненулевая из материалов корпуса (иначе 50 мм)
        armor = 50.0
        try:
            vals = [float(mi.armor) for mi in vd.hull['materials'].values() if getattr(mi, 'armor', 0)]
            if vals:
                armor = min(vals)
        except Exception:
            pass
        return SegmentCollisionResult(best[0], max(0.2, best[1]), armor)
    except Exception:
        if not _HIT_DIAG.get('bboxErr'):
            _HIT_DIAG['bboxErr'] = True
            LOG_CURRENT_EXCEPTION()
        return None


class _BallisticHitResult(object):
    """Collision result compatible with the fields used by damage calculation."""
    def __init__(self, dist, hitAngleCos, armor):
        self.dist = dist
        self.hitAngleCos = hitAngleCos
        self.armor = armor


def _isSPG(vehicle):
    try:
        return 'SPG' in vehicle.typeDescriptor.type.tags
    except Exception:
        return False


def _findBallisticHitVehicle(vehicle, origin, direction, shellSpeed, shellGravity, maxDistance):
    """Trace a high-arc SPG shell along its gravity-affected path, not a straight line."""
    try:
        speed = max(1.0, float(shellSpeed))
        gravity = abs(float(shellGravity))
        totalTime = float(maxDistance) / speed
        steps = 100
        prev = origin
        travelled = 0.0
        for i in range(1, steps + 1):
            t = totalTime * i / float(steps)
            point = origin + direction * (speed * t) + Math.Vector3(0.0, -0.5 * gravity * t * t, 0.0)
            segment = point - prev
            segLen = segment.length
            if segLen <= 1e-5:
                prev = point
                continue
            hit = _findHitVehicle(vehicle, prev, point)
            staticHit = None
            try:
                staticHit = _findStaticHit(vehicle, prev, point)
            except Exception:
                pass
            if hit is not None:
                target, res = hit
                localDist = float(res.dist)
                if staticHit is not None and staticHit[0] < localDist - 0.15:
                    return None
                fraction = max(0.0, min(1.0, localDist / segLen))
                hitPoint = prev + segment * fraction
                fullDist = travelled + localDist
                return (target, _BallisticHitResult(fullDist, res.hitAngleCos, res.armor), hitPoint)
            if staticHit is not None:
                # Terrain/buildings block the shell; do not let it damage targets behind cover.
                return None
            travelled += segLen
            prev = point
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return None


FORGIVE_AIM = True            # выстрел по стволу чуть мимо центра цели (в круге сведения) засчитывается - не зависит от красного контура
FORGIVE_BASE = 1.3            # м: допуск от оси ствола до центра корпуса независимо от дистанции
FORGIVE_MAX_DISP = 2.5        # м: добавка за круг сведения (разброс орудия на дистанции), не больше


def _findHitVehicleForgiving(shooter, origin, direction, maxDistance, dispAngle):
    """Когда точный луч ствола ничего не задел: враг, ось ствола которого проходит вблизи центра корпуса (в пределах круга
    сведения), получает попадание. Луч для расчёта брони/угла перенаправляется в центр цели."""
    best = None
    try:
        myTeam = shooter.publicInfo['team']
    except Exception:
        myTeam = None
    for e in BigWorld.entities.values():
        if e is shooter or not getattr(e, 'isStarted', False):
            continue
        if not hasattr(e, 'collideSegment') or getattr(e, 'model', None) is None:
            continue
        try:
            if e.health <= 0:
                continue
            if myTeam is not None and e.publicInfo['team'] == myTeam:
                continue                                  # по своим допуск не действует
        except Exception:
            continue
        try:
            center = Math.Matrix(e.model.matrix).translation + Math.Vector3(0.0, 1.0, 0.0)
            v = center - origin
            t = v.dot(direction)
            if t <= 1.0 or t > maxDistance:
                continue
            perp = (v - direction * t).length
            tol = FORGIVE_BASE + min(FORGIVE_MAX_DISP, math.tan(min(max(dispAngle, 0.0), 0.1)) * t)
            if perp > tol:
                continue
            L = v.length
            d2 = v * (1.0 / L)
            end2 = origin + d2 * min(maxDistance, L + 8.0)
            res = _collideVehicle(e, origin, end2)
            if res is None:
                res = _bboxHit(e, origin, end2)
            if res is None:
                continue
            if best is None or t < best[2]:
                best = (e, res, t)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    return (best[0], best[1]) if best is not None else None


def _findHitVehicle(shooter, start, end):
    best = None
    bestDist = None

    for e in BigWorld.entities.values():
        if e is shooter or not getattr(e, 'isStarted', False):
            continue
        if not hasattr(e, 'collideSegment') or getattr(e, 'model', None) is None:
            continue
        try:
            if e.health <= 0:
                continue
        except Exception:
            pass

        res = _collideVehicle(e, start, end)
        if res is None:
            res = _bboxHit(e, start, end)
            if res is not None and not _HIT_DIAG.get(('bbox', e.id)):
                _HIT_DIAG[('bbox', e.id)] = True
                LOG_DEBUG('[SHOT] vehicle %s: component hit test gave nothing, used bbox fallback' % e.id)
        if res is None:
            continue

        if bestDist is None or res.dist < bestDist:
            bestDist = res.dist
            best = (e, res)

    return best


RICOCHET_COS = 0.3420201433   # cos(70 градусов): угол больше - снаряд рикошетит (кроме ОФ)


def _isHE(shellDescr):
    try:
        return str(shellDescr.get('kind', '')).upper().startswith('HIGH_EXPLOSIVE')
    except Exception:
        return False


import random as _random


def pierceInfo(shellDescr, piercingPower, hitAngleCos, armor, dist, maxDistance, roll=False):
    """Что будет при попадании: ('pierce'|'bounce'|'ricochet', пробитие, приведённая броня)."""
    # piercingPower лежит в описании выстрела (gun['shots'][i]), а не снаряда
    # как в оригинале: до 100 м - полное пробитие, к 400 м - значение piercingPower[1], дальше не падает
    p0 = float(piercingPower[0])
    p1 = float(piercingPower[1]) if len(piercingPower) > 1 else p0
    if dist <= 100.0:
        pierce = p0
    elif dist >= 400.0:
        pierce = p1
    else:
        pierce = p0 + (p1 - p0) * (dist - 100.0) / 300.0
    if roll:
        # случайный разброс пробития, как в игре (по умолчанию +-25%)
        r = float(shellDescr.get('piercingPowerRandomization', 0.25))
        pierce *= _random.uniform(1.0 - r, 1.0 + r)
    eff = armor
    if hitAngleCos:
        eff = armor / max(0.15, hitAngleCos)
    if _isHE(shellDescr):
        return ('pierce' if pierce >= armor else 'bounce'), pierce, armor
    if hitAngleCos is not None and hitAngleCos < RICOCHET_COS and armor > 0:
        return 'ricochet', pierce, eff
    return ('pierce' if pierce >= eff else 'bounce'), pierce, eff


def _applyDamage(shooterID, target, shellDescr, hitAngleCos, armor, dist, maxDistance):
    """Возвращает: 'friendly' | 'dead' | 'ricochet' | 'bounce' | 'pierce' | 'kill'."""
    # огонь по своим не наносит урона
    try:
        shooter = BigWorld.entity(shooterID)
        if shooter is not None and shooter.publicInfo['team'] == target.publicInfo['team']:
            return 'friendly'
    except Exception:
        pass

    # по уже уничтоженной машине урон не проходит (иначе повторный взрыв/килл)
    if target.health <= 0:
        return 'dead'

    try:
        from gui.mods.offhangar import bots as off_bots
        off_bots.onVehicleDamaged(target, shooterID)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        _sh = BigWorld.entity(shooterID).typeDescriptor
        _pp = _sh.gun['shots'][_sh.activeGunShotIndex]['piercingPower']
    except Exception:
        LOG_CURRENT_EXCEPTION()
        _pp = (0.0, 0.0)
    kind, pierce, eff = pierceInfo(shellDescr, _pp, hitAngleCos, armor, dist, maxDistance, roll=True)
    try:
        # учебный бой: первый выстрел игрока по боту 2 не пробивает (глава "попробуй уничтожить" ждёт непробитие)
        from gui.mods.offhangar import bots as _tb
        if _tb.TUTORIAL[0] and kind == 'pierce' and shooterID == getattr(BigWorld.player(), 'playerVehicleID', -1) \
                and str(target.publicInfo['name']) == '#battle_tutorial:bot_2' and not _TUT_BOUNCED[0]:
            _TUT_BOUNCED[0] = True
            kind = 'bounce'
            LOG_DEBUG('[TUTOR] first shot at bot_2 does not penetrate')
    except Exception:
        pass
    try:
        LOG_DEBUG('[SHOT] %s -> %s: dist=%.0f armor=%.0f cos=%.2f effArmor=%.0f pierce=%.0f => %s (hp %s)' % (
            shooterID, target.id, dist, armor, hitAngleCos or 0.0, eff, pierce, kind, target.health))
    except Exception:
        pass
    if kind != 'pierce':
        # В офлайн-режиме у САУ часто HE-снаряд: прямое попадание не обязано
        # пробивать броню, чтобы нанести урон. Для не-пробития оставляем
        # уменьшенный урон, масштабируемый бронёй, вместо полного нуля.
        _spgHit = False
        try:
            _shEntity = BigWorld.entity(shooterID)
            _tags = getattr(_shEntity.typeDescriptor.type, 'tags', ())
            _spgHit = ('SPG' in _tags)
        except Exception:
            _spgHit = False
        if kind == 'bounce':
            try:
                from gui.mods.offhangar import devices as _dev
                _dev.onHit(shooterID, target, kind, shellDescr, hitAngleCos)
            except Exception:
                LOG_CURRENT_EXCEPTION()
        if not _spgHit:
            return kind
        try:
            _base = float(shellDescr['damage'][0])
            _effectiveArmor = max(0.0, float(armor)) / max(0.15, abs(float(hitAngleCos or 0.0)))
            _factor = max(0.12, min(0.55, 1.0 - _effectiveArmor / 500.0))
            damage = max(1, int(round(_base * _factor * _random.uniform(0.85, 1.15))))
        except Exception:
            damage = max(1, int(float(shellDescr['damage'][0]) * 0.25))
        prevHealth = target.health
        newHealth = max(0, prevHealth - damage)
        target.health = newHealth
        try:
            target.set_health(prevHealth)
            target.onHealthChanged(newHealth, shooterID, 0)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        if newHealth < prevHealth:
            _playPlayerHitSound(target)
            _flashPlayerHitDirection(target, shooterID)
        try:
            av = BigWorld.player()
            if getattr(av, 'playerVehicleID', None) == target.id:
                av.updateVehicleHealth(newHealth, True)
        except Exception:
            pass
        if newHealth <= 0:
            try:
                from gui.mods.offhangar import bots as off_bots
                off_bots.onVehicleKilled(target, shooterID)
            except Exception:
                LOG_CURRENT_EXCEPTION()
            return 'kill'
        return 'pierce'

    damage = float(shellDescr['damage'][0])
    # случайный разброс урона, как в игре (по умолчанию +-25%)
    dr = float(shellDescr.get('damageRandomization', 0.25))
    damage = max(1, int(round(damage * _random.uniform(1.0 - dr, 1.0 + dr))))
    prevHealth = target.health
    newHealth = max(0, prevHealth - damage)

    if newHealth <= 0:
        try:
            from gui.mods.offhangar import battle as off_battle_mod
            off_battle_mod.freezeCorpse(target)     # запомнить место гибели до того, как клиент подменит модель
        except Exception:
            LOG_CURRENT_EXCEPTION()

    target.health = newHealth
    try:
        target.set_health(prevHealth)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        target.onHealthChanged(newHealth, shooterID, 0)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if newHealth <= 0:
        LOG_DEBUG('[DEATH] vehicle %s: native death handler returned' % target.id)

    if newHealth < prevHealth:
        _playPlayerHitSound(target)
        _flashPlayerHitDirection(target, shooterID)

    # здоровье собственной машины игрока показывает интерфейс (панель повреждений, прицел)
    try:
        av = BigWorld.player()
        if getattr(av, 'playerVehicleID', None) == target.id:
            av.updateVehicleHealth(newHealth, True)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    if newHealth <= 0:
        try:
            from gui.mods.offhangar import bots as off_bots
            off_bots.onVehicleKilled(target, shooterID)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        LOG_DEBUG('[DEATH] vehicle %s: kill fully processed' % target.id)
        return 'kill'
    # повреждения модулей и экипажа (devices.py): гусеница при любом попадании, остальное при пробитии
    try:
        from gui.mods.offhangar import devices as _dev
        _dev.onHit(shooterID, target, kind, shellDescr, hitAngleCos)
        if _dev.onPenetration(shooterID, target, shellDescr, hitAngleCos, damage):
            return 'kill'
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 'pierce'


def _feedback(shooterID, target, result):
    try:
        from gui.mods.offhangar import feedback
        feedback.onShot(shooterID, target, result)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _tryDestructibleHit(vehicle, origin, end):
    try:
        res_dest = BigWorld.wg_collideSegment(vehicle.spaceID, origin, end, 128)
    except Exception:
        res_dest = None

    if res_dest is None or len(res_dest) <= 5:
        return None

    hitPoint = res_dest[0]
    dist = (hitPoint - origin).length

    from gui.mods.offhangar import battle as off_battle

    sp = off_battle._simplePhysicsByVehID.get(vehicle.id)
    if sp is not None:
        try:
            sp._tryDestroyDestructible(
                vehicle.spaceID,
                res_dest[5],
                res_dest[4],
                res_dest[2],
                Math.Matrix(vehicle.model.matrix).yaw,
                12.0
            )
        except Exception:
            LOG_CURRENT_EXCEPTION()

    return (dist, hitPoint)


TUT_LOW_OBSTACLE_H = 2.5   # учебный бой: препятствия ниже этой высоты (ежи, надолбы, заборы) снаряд не останавливают, м
_TUT_PASS_LOGGED = [0]


def _tutorialLowObstacle(vehicle, origin, end, targetDist):
    """Учебный бой: статика перед целью - низкое препятствие (ежи, надолбы), если тот же луч, поднятый на TUT_LOW_OBSTACLE_H,
    до цели ничего не задевает. Холм или стена остаются преградой: их поднятый луч тоже упирается."""
    try:
        from gui.mods.offhangar import bots as _tb
        if not _tb.TUTORIAL[0]:
            return False
        up = Math.Vector3(0.0, TUT_LOW_OBSTACLE_H, 0.0)
        st2 = _findStaticHit(vehicle, origin + up, end + up)
        return st2 is None or st2[0] >= targetDist - 0.3
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return False


def _findStaticHit(vehicle, origin, end):
    try:
        res = BigWorld.wg_collideSegment(vehicle.spaceID, origin, end, 0)
    except Exception:
        res = None

    if res is None:
        return None

    hitPoint = res[0]
    if hitPoint is None:
        return None

    normal = None
    if len(res) > 1:
        try:
            n = res[1]
            normal = Math.Vector3(float(n[0]), float(n[1]), float(n[2]))
        except Exception:
            normal = None

    dist = (hitPoint - origin).length
    return (dist, hitPoint, normal)

def _resolveShot(avatar, vehicle):
    st = _getState(vehicle)
    shellCD = _currentShellCD(vehicle)
    gunDescr = vehicle.typeDescriptor.gun
    shot = gunDescr['shots'][vehicle.typeDescriptor.activeGunShotIndex]
    shellDescr = shot['shell']
    maxDistance = shot['maxDistance']
    shellSpeed = shot['speed']
    shellGravity = shot['gravity']

    origin, direction = _shotOrigin(vehicle)
    end = origin + direction * maxDistance

    # САУ стреляет по баллистической дуге; прямая проверка лучом пропускала цели.
    ballisticHitPoint = None
    if _isSPG(vehicle):
        ballistic = _findBallisticHitVehicle(vehicle, origin, direction, shellSpeed, shellGravity, maxDistance)
        if ballistic is not None:
            hit = (ballistic[0], ballistic[1])
            ballisticHitPoint = ballistic[2]
        else:
            hit = None
    else:
        hit = _findHitVehicle(vehicle, origin, end)
        if hit is None and FORGIVE_AIM:
            try:
                try:
                    disp = float(avatar.getOwnVehicleShotDispersionAngle(0.0))
                except Exception:
                    disp = 0.0
                hit = _findHitVehicleForgiving(vehicle, origin, direction, maxDistance, disp)
                if hit is not None:
                    LOG_DEBUG('[SHOT] forgiving aim: hit %s (barrel ray passed near the target)' % hit[0].id)
            except Exception:
                LOG_CURRENT_EXCEPTION()

    if hit is not None and not _isSPG(vehicle):
        # машина за стеной/холмом закрыта - снаряд попадает в препятствие
        try:
            _st = _findStaticHit(vehicle, origin, end)
            if _st is not None and _st[0] < hit[1].dist - 0.3:
                if _tutorialLowObstacle(vehicle, origin, end, hit[1].dist):
                    if _TUT_PASS_LOGGED[0] < 20:
                        _TUT_PASS_LOGGED[0] += 1
                        LOG_DEBUG('[TUTOR] low obstacle at %d m does not stop the shell (target at %d m)' % (int(_st[0]), int(hit[1].dist)))
                else:
                    hit = None
        except Exception:
            pass

    hitDist = maxDistance
    endPoint = end
    hitKind = 'none'
    hitNormal = None

    if hit is not None:
        target, res = hit
        hitDist = res.dist
        endPoint = ballisticHitPoint if ballisticHitPoint is not None else origin + direction * hitDist
        hitKind = 'vehicle'

        _result = _applyDamage(vehicle.id, target, shellDescr, res.hitAngleCos, res.armor, res.dist, maxDistance)
        _feedback(vehicle.id, target, _result)
        try:
            if getattr(target, 'health', 0) > 0:               # по только что убитой машине эффект попадания не показываем
                target.showDamageFromShot(vehicle.id, [], shellDescr['effectsIndex'])
        except Exception:
            LOG_CURRENT_EXCEPTION()
    else:
        destRes = _tryDestructibleHit(vehicle, origin, end)
        staticRes = _findStaticHit(vehicle, origin, end)
        try:
            LOG_DEBUG('[SHOT] player shot: no vehicle hit (static at %s m)' % (
                int(staticRes[0]) if staticRes else None))
        except Exception:
            pass

        candidates = []
        if destRes is not None:
            candidates.append(('destructible', destRes[0], destRes[1], None))
        if staticRes is not None:
            candidates.append(('static', staticRes[0], staticRes[1], staticRes[2]))

        if candidates:
            candidates.sort(key=lambda c: c[1])
            hitKind, hitDist, endPoint, hitNormal = candidates[0]

    shotID = _nextShotID()

    try:
        # play visual/sound effects for shot (muzzle flash, recoil, sound)
        try:
            from items import vehicles as items_vehicles
            effectsDescr = items_vehicles.g_cache.shotEffects[shellDescr['effectsIndex']]
            try:
                # start vehicle shooting extra (muzzle flash, recoil)
                vehicle.showShooting(gunDescr['burst'][0], True)
            except Exception:
                LOG_CURRENT_EXCEPTION()
            # try common keys for sound events
            soundEvent = None
            for key in ('shotSound', 'sound', 'soundEvent'):
                if key in effectsDescr:
                    soundEvent = effectsDescr[key]
                    break
            if soundEvent:
                try:
                    SoundGroups.g_instance.playSound(vehicle.appearance.modelsDesc['gun']['model'], soundEvent)
                except Exception:
                    LOG_CURRENT_EXCEPTION()
        except Exception:
            LOG_CURRENT_EXCEPTION()

        velocity = direction * shellSpeed
        try:
            # В некоторых клиентах ProjectileMover падает на отдельных траекториях.
            # Это визуальный эффект: ошибка трассера не должна отменять завершение выстрела.
            avatar.showTracer(
                vehicle.id,
                shotID,
                shellDescr['effectsIndex'],
                origin,
                velocity,
                shellGravity,
                maxDistance
            )
        except Exception:
            LOG_CURRENT_EXCEPTION()

        flightTime = max(0.01, hitDist / max(1.0, shellSpeed))

        def _finishShot(sid=shotID, ep=endPoint, kind=hitKind, normal=hitNormal, dirv=direction, effIdx=shellDescr['effectsIndex']):
            try:
                av = BigWorld.player()
                if av is None:
                    return

                if kind == 'static':
                    try:
                        n = normal if normal is not None else Math.Vector3(0.0, 1.0, 0.0)
                        surfaceMaterial = calcSurfaceMaterialNearPoint(ep, n, av.spaceID)
                        hitPoint2, surfNormal, matKind, materialEffIdx = surfaceMaterial
                        av.explodeProjectile(sid, effIdx, materialEffIdx, hitPoint2, dirv, None)
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                        av.stopTracer(sid, ep)
                elif kind == 'vehicle':
                    try:
                        av.explodeProjectile(sid, effIdx, 3, ep, dirv, None)    # 3 = 'metal'
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                        av.stopTracer(sid, ep)
                else:
                    av.stopTracer(sid, ep)
            except Exception:
                LOG_CURRENT_EXCEPTION()

        BigWorld.callback(flightTime, _finishShot)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    clipSize, clipInterval = _clipParams(gunDescr)
    inClipBefore = _clipQuantity(st, gunDescr, shellCD)
    st['ammo'][shellCD] = max(0, st['ammo'].get(shellCD, 0) - 1)

    # Барабан / магазин: между выстрелами в магазине - короткий интервал,
    # полная перезарядка только когда магазин опустел (или снарядов больше нет).
    quantityInClip = None
    refillClip = False
    if clipSize > 1:
        inClipNow = inClipBefore - 1
        if inClipNow > 0 and st['ammo'][shellCD] > 0:
            st['inClip'][shellCD] = inClipNow
            reloadTime = clipInterval if clipInterval > 0.0 else 0.1
        else:
            refillClip = True
            st['inClip'][shellCD] = min(clipSize, st['ammo'][shellCD])
            reloadTime = gunDescr['reloadTime'] * _reloadFactor(vehicle.id)
        quantityInClip = st['inClip'][shellCD]
    else:
        reloadTime = gunDescr['reloadTime'] * _reloadFactor(vehicle.id)

    st['reloadEnd'] = BigWorld.time() + reloadTime

    try:
        left = st['ammo'][shellCD]
        # барабан опустел: на панели 0 снарядов в барабане, пока идёт полная перезарядка (заполняется по её окончании)
        shownClip = 0 if refillClip else quantityInClip
        avatar.updateVehicleAmmo(shellCD, left, left if shownClip is None else shownClip, -1)
        avatar.updateVehicleGunReloadTime(vehicle.id, reloadTime, reloadTime)

        vehID = vehicle.id

        def _notifyReloaded(vid=vehID, rt=reloadTime, refill=refillClip, cd=shellCD):
            try:
                av = BigWorld.player()
                veh = BigWorld.entity(vid)
                if av is None or veh is None:
                    return
                st2 = _gunState.get(vid)
                if st2 is None:
                    return
                if BigWorld.time() < st2['reloadEnd'] - 0.01:
                    return
                if refill:
                    n = st2['ammo'].get(cd, 0)
                    av.updateVehicleAmmo(cd, n, st2.get('inClip', {}).get(cd, n), -1)
                av.updateVehicleGunReloadTime(vid, 0.0, rt)
            except Exception:
                LOG_CURRENT_EXCEPTION()

        BigWorld.callback(reloadTime, _notifyReloaded)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        _selectShellAfterShot(avatar, vehicle, st, quantityInClip is None or st['inClip'].get(shellCD, 0) <= 0 or reloadTime >= gunDescr['reloadTime'] * 0.5)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    # effects already triggered above


def _patchAvatarShoot():
    try:
        import Avatar as _A

        cls = _A.PlayerAvatar
        orig = cls.shoot

        def patched(self, isRepeat=False, orig=orig):
            if not _active():
                return orig(self, isRepeat)

            # во время обратного отсчёта стрелять нельзя
            from gui.mods.offhangar import battle as off_battle
            if off_battle.isPrebattle():
                return

            vehicle = BigWorld.entity(self.playerVehicleID)
            if vehicle is None or not vehicle.isStarted:
                return

            if not _canShoot(vehicle):
                return

            _resolveShot(self, vehicle)

        cls.shoot = patched
        LOG_NOTE('[OFFHANGAR] offline shooting patch installed')
    except Exception:
        LOG_ERROR('[OFFHANGAR] offline shooting patch FAILED')
        LOG_CURRENT_EXCEPTION()

def register():
    global _REGISTERED

    if _REGISTERED:
        return

    _REGISTERED = True
    _patchAvatarShoot()
    _patchShellSwitch()
    _patchConsumablesPanel()
    LOG_NOTE('offline shooting module registered') 