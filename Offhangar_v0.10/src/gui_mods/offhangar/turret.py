# -- coding: utf-8 --

import weakref
from math import pi, fmod

import BigWorld
import Math

from AvatarInputHandler import AimingSystems
from gun_rotation_shared import calcPitchLimitsFromDesc
from projectile_trajectory import getShotAngles

from gui.mods.offhangar.logging import LOG_DEBUG
from debug_utils import LOG_CURRENT_EXCEPTION

TURRET_TICK_LENGTH = 0.05
_FALLBACK_AIM_DISTANCE = 500.0


# ------------------------------------------------------------
# Матрица с плавной интерполяцией (как _MatrixAnimator в VehicleGunRotator)
# ------------------------------------------------------------

class _RotAnimator(object):

    def __init__(self):
        m = Math.Matrix()
        m.setIdentity()
        self.__animMat = Math.MatrixAnimation()
        self.__animMat.keyframes = ((0.0, m),)

    matrix = property(lambda self: self.__animMat)

    def update(self, matrix, time):
        self.__animMat.keyframes = ((0.0, Math.Matrix(self.__animMat)), (time, matrix))
        self.__animMat.time = 0.0


# ------------------------------------------------------------
# Хелперы вращения с учётом лимитов (адаптировано из VehicleGunRotator)
# ------------------------------------------------------------

def _getRotationWays(curAngle, shotAngle):
    shotDiff1 = shotAngle - curAngle
    if shotDiff1 < 0:
        shotDiff2 = 2 * pi + shotDiff1
    else:
        shotDiff2 = -2 * pi + shotDiff1
    if abs(shotDiff1) <= pi:
        return shotDiff1, shotDiff2
    return shotDiff2, shotDiff1


def _isOutOfLimits(angle, limits):
    if limits is None:
        return None
    if abs(limits[1] - angle) < 1e-05 or abs(limits[0] - angle) < 1e-05:
        return None
    dpi = 2 * pi
    minDiff = fmod(limits[0] - angle + dpi, dpi)
    maxDiff = fmod(limits[1] - angle + dpi, dpi)
    if minDiff > maxDiff:
        return None
    if minDiff < dpi - maxDiff:
        return limits[0]
    return limits[1]


def _applyTurretYawLimits(diff, angle, limits):
    if limits is None:
        return diff
    dpi = 2 * pi
    if diff > 0:
        if abs(limits[1] - angle) < 1e-05:
            return 0
        maxDiff = fmod(limits[1] - angle + dpi, dpi)
        return min(maxDiff, diff)
    if abs(limits[0] - angle) < 1e-05:
        return 0
    maxDiff = fmod(limits[0] - angle - dpi, dpi)
    return max(maxDiff, diff)


def _turretYawWithSpeedLimit(angle, diff, limit):
    dpi = 2 * pi
    if diff > 0:
        return fmod(pi + angle + min(diff, limit), dpi) - pi
    return fmod(-pi + angle + max(diff, -limit), dpi) + pi


def _stepTurretYaw(curAngle, shotAngle, speedLimit, angleLimits):
    if curAngle == shotAngle:
        return curAngle
    if speedLimit < 1e-05:
        return curAngle
    closestLimit = _isOutOfLimits(curAngle, angleLimits)
    if closestLimit is not None:
        return closestLimit
    shortWayDiff, longWayDiff = _getRotationWays(curAngle, shotAngle)
    shortWayDiffLimited = _applyTurretYawLimits(shortWayDiff, curAngle, angleLimits)
    if shortWayDiffLimited == shortWayDiff:
        return _turretYawWithSpeedLimit(curAngle, shortWayDiff, speedLimit)
    longWayDiffLimited = _applyTurretYawLimits(longWayDiff, curAngle, angleLimits)
    if longWayDiffLimited == longWayDiff:
        return _turretYawWithSpeedLimit(curAngle, longWayDiff, speedLimit)
    return _turretYawWithSpeedLimit(curAngle, shortWayDiffLimited, speedLimit)


def _stepGunPitch(curAngle, shotAngle, speedLimit, angleLimits):
    if curAngle == shotAngle:
        return curAngle
    shotDiff = shotAngle - curAngle
    if angleLimits is not None:
        if shotAngle < angleLimits[0]:
            shotDiff = angleLimits[0] - curAngle
        elif shotAngle > angleLimits[1]:
            shotDiff = angleLimits[1] - curAngle
    if shotDiff > 0:
        return curAngle + min(shotDiff, speedLimit)
    return curAngle + max(shotDiff, -speedLimit)


# ------------------------------------------------------------
# Основной класс
# ------------------------------------------------------------

class SimpleTurretRotation(object):
    """Подключается вместо avatar.gunRotator как target для
    appearance.setupGunMatrixTargets(...). Скорости поворота — из XML
    конфига танка (typeDescriptor), без обращения к серверу/FakeServer."""

    turretMatrix = property(lambda self: self.__turretAnim.matrix)
    gunMatrix = property(lambda self: self.__gunAnim.matrix)

    def __init__(self):
        self._vehicle = None
        self._avatar = None
        self._descr = None

        self._turretYaw = 0.0
        self._gunPitch = 0.0

        self._maxTurretSpeed = 0.0
        self._maxGunSpeed = 0.0
        self._turretYawLimits = None

        self.__turretAnim = _RotAnimator()
        self.__gunAnim = _RotAnimator()

        self._cbID = None
        self._lastTime = 0.0

    # --------------------------------------------------------
    def setup(self, vehicle, avatar=None):
        self._vehicle = weakref.proxy(vehicle)
        self._avatar = avatar or BigWorld.player()
        self._descr = vehicle.typeDescriptor

        self._maxTurretSpeed = self._descr.turret['rotationSpeed']
        self._maxGunSpeed = self._descr.gun['rotationSpeed']
        self._turretYawLimits = self._descr.gun['turretYawLimits']

        self._turretYaw = 0.0
        self._gunPitch = 0.0
        self._applyMatrices(0.0)

        LOG_DEBUG(
            'SimpleTurretRotation setup id=%s maxTurretSpeed=%.3f maxGunSpeed=%.3f yawLimits=%s' % (
                getattr(vehicle, 'id', None),
                self._maxTurretSpeed,
                self._maxGunSpeed,
                self._turretYawLimits,
            )
        )

    def start(self):
        if self._cbID is not None:
            return
        self._lastTime = BigWorld.time()
        self._cbID = BigWorld.callback(0.0, self._tick)
        LOG_DEBUG('SimpleTurretRotation started id=%s' % getattr(self._vehicle, 'id', None))

    def stop(self):
        if self._cbID is not None:
            try:
                BigWorld.cancelCallback(self._cbID)
            except Exception:
                pass
            self._cbID = None

    # --------------------------------------------------------
    def _tick(self):
        now = BigWorld.time()
        dt = now - self._lastTime
        self._lastTime = now
        self._cbID = BigWorld.callback(TURRET_TICK_LENGTH, self._tick)

        if dt <= 0.0:
            return
        if dt > 0.2:
            dt = 0.2

        try:
            vehicle = self._vehicle
        except Exception:
            self.stop()
            return

        if vehicle is None or not getattr(vehicle, 'isStarted', False):
            return

        # во время обратного отсчёта башня и орудие стоят на месте
        try:
            from gui.mods.offhangar import battle as _ob
            if _ob.isPrebattle():
                return
        except Exception:
            pass

        try:
            self._update(dt)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _getTargetPoint(self):
        avatar = self._avatar
        try:
            inputHandler = getattr(avatar, 'inputHandler', None)
            if inputHandler is None:
                pass
                return None
            pt = inputHandler.getDesiredShotPoint()
            if pt is None:
                pass
            return pt
        except Exception:
            LOG_CURRENT_EXCEPTION()
            return None

    def _computeShotPoint(self):
        vehicle = self._vehicle
        vd = vehicle.typeDescriptor

        turretMat = Math.Matrix()
        turretMat.setRotateY(self._turretYaw)
        turretMat.translation = vd.hull['turretPositions'][0] + vd.chassis['hullPosition']
        turretMat.postMultiply(vehicle.model.matrix)

        gunMat = Math.Matrix()
        gunMat.setRotateX(self._gunPitch)
        gunMat.postMultiply(turretMat)

        shotPos = gunMat.applyPoint(vd.turret['gunPosition'])
        shotVec = gunMat.applyVector(Math.Vector3(0, 0, 1))
        return (shotPos, shotVec)

    def _update(self, dt):
        avatar = self._avatar
        descr = self._descr

        try:
            from AvatarInputHandler import aims as _aims
            if _aims._g_aimState is None:
                _aims.clearState()
        except Exception:
            pass

        aimK, dispK = 1.0, 1.0
        turretK = 1.0
        try:
            from gui.mods.offhangar import devices as _dev
            aimK, dispK = _dev.aimFactors(self._vehicle.id)      # экипаж, орудие, приводы наводки
            _f = _dev.getFactors(self._vehicle.id)
            if _f is not None:
                turretK = _f['turret']       # привод башни, наводчик, газолин
        except Exception:
            LOG_CURRENT_EXCEPTION()
        if abs(turretK - getattr(self, '_lastTurretK', 1.0)) > 0.01:
            self._lastTurretK = turretK
            LOG_DEBUG('[DEV] turret speed factor now %.2f (native gunRotator gets %.3f rad/s)' % (turretK, self._maxTurretSpeed * turretK))
        try:
            # Скорость башни, которую родной gunRotator игрока использует для своей анимации, тоже должна падать при повреждениях
            avatar.updateTargetingInfo(
                self._turretYaw,
                self._gunPitch,
                self._maxTurretSpeed * turretK,
                self._maxGunSpeed,
                dispK,
                descr.gun['shotDispersionFactors']['turretRotation'],
                descr.chassis['shotDispersionFactors'][0],
                descr.chassis['shotDispersionFactors'][1],
                descr.gun['aimingTime'] * aimK
            )
        except Exception:
            pass

        targetPoint = self._getTargetPoint()
        if targetPoint is None:
            return

        try:
            vehMat = avatar.getOwnVehicleMatrix()
        except Exception:
            vehMat = Math.Matrix(self._vehicle.model.matrix)

        shotTurretYaw, shotGunPitch = getShotAngles(
            descr, vehMat, (self._turretYaw, self._gunPitch), targetPoint
        )

        self._turretYaw = _stepTurretYaw(
            self._turretYaw, shotTurretYaw,
            self._maxTurretSpeed * dt * turretK, self._turretYawLimits
        )

        pitchLimits = calcPitchLimitsFromDesc(self._turretYaw, descr.gun['pitchLimits'])
        self._gunPitch = _stepGunPitch(
            self._gunPitch, shotGunPitch,
            self._maxGunSpeed * dt, pitchLimits
        )

        self._applyMatrices(dt)

        try:
            shotPos, shotVec = self._computeShotPoint()
            dispersionAngle = avatar.getOwnVehicleShotDispersionAngle(0.0)
            avatar.updateGunMarker(shotPos, shotVec, dispersionAngle)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _applyMatrices(self, dt):
        turretMat = Math.Matrix()
        turretMat.setRotateY(self._turretYaw)
        self.__turretAnim.update(turretMat, dt)

        gunMat = Math.Matrix()
        gunMat.setRotateX(self._gunPitch)
        self.__gunAnim.update(gunMat, dt)