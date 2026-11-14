# -- coding: utf-8 --
import re
import math
import Math
import BigWorld
import AreaDestructibles
import DestructiblesCache

from constants import DESTRUCTIBLE_MATKIND
from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_DEBUG

# Подробные логи [DDEST]
DEBUG = True

# Дополнительные логи [AD] внутри AreaDestructibles.
# Включай только если нужно отлаживать уже сам AreaDestructibles.
DEBUG_AREA_DESTRUCTIBLES = False

# Если True, деревья/столбы будут сразу переходить в поваленное состояние
# без анимации. Полезно для диагностики.
INSTANT_FALL = False

# Осторожно: если True, неизвестные объекты без дескриптора будут
# пытаться скрыться при приближении. Может задеть лишние декорации.
ENABLE_UNKNOWN_PROXIMITY = False

RAY_SPEED_MIN = 1.0
PROXIMITY_SPEED_MIN = 1.0
CHUNK_RETRY_TIMEOUT = 1.0


_DESC_LOOKUP_PATCHED = False
_AD_DEBUG_PATCHED = False


def register():
    _patchDescLookup()

    if DEBUG_AREA_DESTRUCTIBLES:
        _patchAreaDestructiblesDebug()

def _patchDescLookup():
    global _DESC_LOOKUP_PATCHED

    if _DESC_LOOKUP_PATCHED:
        return

    _DESC_LOOKUP_PATCHED = True

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

        if DEBUG:
            LOG_DEBUG('[DDEST] DestructiblesCache desc lookup patch installed')

    except Exception:
        LOG_CURRENT_EXCEPTION()

def _patchAreaDestructiblesDebug():
    global _AD_DEBUG_PATCHED

    if _AD_DEBUG_PATCHED:
        return

    _AD_DEBUG_PATCHED = True

    # orderDestructibleDestroy
    try:
        origOrder = AreaDestructibles.DestructiblesManager.orderDestructibleDestroy

        def patchedOrder(self, chunkID, dmgType, destrData, isNeedAnimation, syncWithProjectile=False):
            LOG_DEBUG(
                '[AD] order: chunk=%s dmgType=%s anim=%s sync=%s data=%s' % (
                    chunkID,
                    dmgType,
                    isNeedAnimation,
                    syncWithProjectile,
                    destrData,
                )
            )

            if INSTANT_FALL and dmgType == DestructiblesCache.DESTR_TYPE_TREE:
                isNeedAnimation = False

            return origOrder(
                self,
                chunkID,
                dmgType,
                destrData,
                isNeedAnimation,
                syncWithProjectile
            )

        AreaDestructibles.DestructiblesManager.orderDestructibleDestroy = patchedOrder
    except Exception:
        LOG_CURRENT_EXCEPTION()

    # __dropDestructible
    try:
        origDrop = AreaDestructibles.DestructiblesManager._DestructiblesManager__dropDestructible

        def patchedDrop(
            self,
            chunkID,
            destrIndex,
            fallDirYaw,
            pitchConstr,
            fallSpeed,
            isAnimate,
            obstacleCollisionFlags
        ):
            LOG_DEBUG(
                '[AD] drop: chunk=%s idx=%s yaw=%.2f pitchConstr=%.2f fallSpeed=%s animate=%s flags=%s' % (
                    chunkID,
                    destrIndex,
                    fallDirYaw,
                    pitchConstr,
                    fallSpeed,
                    isAnimate,
                    obstacleCollisionFlags,
                )
            )

            return origDrop(
                self,
                chunkID,
                destrIndex,
                fallDirYaw,
                pitchConstr,
                fallSpeed,
                isAnimate,
                obstacleCollisionFlags
            )

        AreaDestructibles.DestructiblesManager._DestructiblesManager__dropDestructible = patchedDrop
    except Exception:
        LOG_CURRENT_EXCEPTION()

    # showFall
    try:
        origShowFall = AreaDestructibles._DestructiblesAnimator.showFall

        def patchedShowFall(
            self,
            spaceID,
            chunkID,
            destrIndex,
            fallDirYaw,
            pitchConstr,
            discreteInitSpeed,
            isNeedAnimation,
            initialMatrix,
            touchdownCallback=None
        ):
            LOG_DEBUG(
                '[AD] showFall: chunk=%s idx=%s yaw=%.2f pitch=%.2f initSpeed=%s animate=%s' % (
                    chunkID,
                    destrIndex,
                    fallDirYaw,
                    pitchConstr,
                    discreteInitSpeed,
                    isNeedAnimation,
                )
            )

            return origShowFall(
                self,
                spaceID,
                chunkID,
                destrIndex,
                fallDirYaw,
                pitchConstr,
                discreteInitSpeed,
                isNeedAnimation,
                initialMatrix,
                touchdownCallback
            )

        AreaDestructibles._DestructiblesAnimator.showFall = patchedShowFall
    except Exception:
        LOG_CURRENT_EXCEPTION()

    # __positionBodyModel
    try:
        origPos = AreaDestructibles._DestructiblesAnimator._DestructiblesAnimator__positionBodyModel
        posState = {'count': 0}

        def patchedPos(self, body):
            if posState['count'] < 40:
                posState['count'] += 1

                LOG_DEBUG(
                    '[AD] pos: chunk=%s idx=%s pitch=%.4f pitchSpeed=%.4f simTime=%.3f' % (
                        body.get('chunkID'),
                        body.get('destrIndex'),
                        body.get('pitch'),
                        body.get('pitchSpeed'),
                        body.get('simulationTime'),
                    )
                )

            return origPos(self, body)

        AreaDestructibles._DestructiblesAnimator._DestructiblesAnimator__positionBodyModel = patchedPos
    except Exception:
        LOG_CURRENT_EXCEPTION()

    # _printErrDescNotAvailable
    try:
        origErr = AreaDestructibles._printErrDescNotAvailable

        def patchedErr(spaceID, chunkID, destrIndex):
            LOG_DEBUG(
                '[AD] ERROR: destructible descriptor not available space=%s chunk=%s idx=%s' % (
                    spaceID,
                    chunkID,
                    destrIndex,
                )
            )

            return origErr(spaceID, chunkID, destrIndex)

        AreaDestructibles._printErrDescNotAvailable = patchedErr
    except Exception:
        LOG_CURRENT_EXCEPTION()

def ensureChunkLoaded(mgr, spaceID, chunkID):
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


def _isFenceLike(filename):
    fname = (filename or '').lower()

    return any(
        s in fname
        for s in (
            'fence',
            'gate',
            'gaf',
            'wall',
            'zabor',
        )
    )

class DestructiblesController(object):
    def __init__(self):
        self._spaceID = None
        self._broken = set()
        self._noDescSeen = set()
        self._chunkCache = {}
        self._chunkRetry = {}
        self._dbgCount = 0

    def reset(self):
        self._spaceID = None
        self._broken = set()
        self._noDescSeen = set()
        self._chunkCache = {}
        self._chunkRetry = {}
        self._dbgCount = 0

    def setup(self, spaceID=None):
        self.reset()
        self._spaceID = spaceID

    def _dbg(self, msg):
        if not DEBUG:
            return

        if self._dbgCount < 200:
            self._dbgCount += 1
            LOG_DEBUG('[DDEST] %s' % msg)

    def tryRayHit(self, spaceID, res_dest, speed, fallYaw):
        if res_dest is None:
            return False

        try:
            if len(res_dest) <= 5:
                return False

            matKind = res_dest[2]
            itemIndex = res_dest[4]
            chunkID = res_dest[5]

            return self._tryDestroy(
                spaceID,
                chunkID,
                itemIndex,
                matKind,
                fallYaw,
                speed
            )

        except Exception:
            LOG_CURRENT_EXCEPTION()
            return False

    def updateProximity(self, vehicle, prevPos, newPos, speed, yaw, halfWidth):
        if speed <= PROXIMITY_SPEED_MIN:
            return

        try:
            spaceID = vehicle.spaceID
        except Exception:
            return

        if self._spaceID != spaceID:
            self.reset()
            self._spaceID = spaceID

        try:
            halfWidth = float(halfWidth or 1.5)
        except Exception:
            halfWidth = 1.5

        dx = newPos.x - prevPos.x
        dz = newPos.z - prevPos.z
        dy = newPos.y - prevPos.y

        dist = math.sqrt(dx * dx + dz * dz)

        if dist > 1e-4:
            fallYaw = math.atan2(dx / dist, dz / dist)
        else:
            fallYaw = yaw

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
                chunkItems[chunkID] = self._getChunkItems(spaceID, chunkID)

            items = chunkItems[chunkID]
            if not items:
                continue

            for idx, dtype, pos, filename in items:
                radius = self._radiusFor(dtype, filename, halfWidth)
                radius2 = radius * radius

                ddx = px - pos.x
                ddz = pz - pos.z

                if ddx * ddx + ddz * ddz <= radius2:
                    self._tryDestroy(
                        spaceID,
                        chunkID,
                        idx,
                        73,
                        fallYaw,
                        speed
                    )

    def _tryDestroy(self, spaceID, chunkID, itemIndex, matKind, fallYaw, speed):
        mgr = AreaDestructibles.g_destructiblesManager
        cache = AreaDestructibles.g_cache

        if mgr is None or cache is None:
            return False

        try:
            chunkID = int(chunkID)
            itemIndex = int(itemIndex)
            matKind = int(matKind)
        except Exception:
            return False

        # Статические неразрушаемые материалы.
        if matKind > max(int(DESTRUCTIBLE_MATKIND.MAX), 130):   # "кожа" коллизии разрушаемых бывает 101..130 (как в 0.8.2)
            return False

        filename = None
        desc = None

        try:
            filename = BigWorld.wg_getDestructibleFilename(spaceID, chunkID, itemIndex)

            if filename:
                filename = re.sub('/lod./', '/lod0/', filename)

            desc = cache.getDescByFilename(filename)

        except Exception:
            LOG_CURRENT_EXCEPTION()
            return False

        if desc is None:
            noDescKey = (chunkID, itemIndex, filename)

            if noDescKey not in self._noDescSeen and len(self._noDescSeen) < 50:
                self._noDescSeen.add(noDescKey)
                self._dbg(
                    'no desc chunk=%s item=%s matKind=%s file=%s' % (
                        chunkID,
                        itemIndex,
                        matKind,
                        filename,
                    )
                )

            if self._hideUnknownDestructible(spaceID, chunkID, itemIndex):
                self._broken.add((chunkID, itemIndex, 'unknown'))
                return True

            return False

        destrType = desc.get('type')

        self._dbg(
            'hit chunk=%s item=%s type=%s matKind=%s spd=%.1f file=%s' % (
                chunkID,
                itemIndex,
                destrType,
                matKind,
                speed,
                filename,
            )
        )

        if destrType == DestructiblesCache.DESTR_TYPE_STRUCTURE:
            modules = desc.get('modules') or {}

            if not modules:
                return False

            originalMat = matKind

            if matKind not in modules:
                if DESTRUCTIBLE_MATKIND.DAMAGED_MIN <= matKind <= DESTRUCTIBLE_MATKIND.DAMAGED_MAX:
                    normal = matKind - (
                        DESTRUCTIBLE_MATKIND.DAMAGED_MIN - DESTRUCTIBLE_MATKIND.NORMAL_MIN
                    )

                    if normal in modules:
                        matKind = normal

                if matKind not in modules:
                    matKind = int(sorted(modules.keys())[0])

            isFence = _isFenceLike(desc.get('filename'))

            destroyAll = (
                isFence
                or len(modules) <= 6
                or speed >= 8.0
                or originalMat not in modules
            )

            if destroyAll:
                matKinds = list(modules.keys())
                brokenKey = (chunkID, itemIndex, 'struct-all')
            else:
                matKinds = [matKind]
                brokenKey = (chunkID, itemIndex, matKind, destrType)

            if brokenKey in self._broken:
                return True

            done = 0

            for m in matKinds:
                try:
                    m = int(m)
                except Exception:
                    continue

                moduleKey = (chunkID, itemIndex, m, destrType)

                if moduleKey in self._broken:
                    continue

                try:
                    data = int(
                        DestructiblesCache.encodeDestructibleModule(
                            itemIndex,
                            m,
                            False
                        )
                    )

                    self._order(mgr, chunkID, destrType, data)

                    self._broken.add(moduleKey)
                    done += 1

                except Exception:
                    LOG_CURRENT_EXCEPTION()
                    self._broken.add(moduleKey)

            if done > 0:
                self._broken.add(brokenKey)

                if DEBUG:
                    LOG_DEBUG(
                        '[DEST] structure: chunk=%s item=%s modules=%s spd=%.1f file=%s' % (
                            chunkID,
                            itemIndex,
                            done,
                            speed,
                            filename,
                        )
                    )

                return True

            allBroken = True

            for m in modules.keys():
                try:
                    mk = int(m)
                except Exception:
                    continue

                if (chunkID, itemIndex, mk, destrType) not in self._broken:
                    allBroken = False
                    break

            if allBroken:
                self._broken.add(brokenKey)
                return True

            return False

        brokenKey = (chunkID, itemIndex, destrType)

        if brokenKey in self._broken:
            return True

        data = None

        try:
            if destrType == DestructiblesCache.DESTR_TYPE_FRAGILE:
                data = int(
                    DestructiblesCache.encodeFragile(
                        itemIndex,
                        False
                    )
                )

            elif destrType == DestructiblesCache.DESTR_TYPE_FALLING_ATOM:
                data = int(
                    DestructiblesCache.encodeFallenColumn(
                        itemIndex,
                        fallYaw,
                        1
                    )
                )

            elif destrType == DestructiblesCache.DESTR_TYPE_TREE:
                data = int(
                    DestructiblesCache.encodeFallenTree(
                        itemIndex,
                        fallYaw,
                        math.pi / 2.0,
                        1
                    )
                )

            else:
                return False

            self._order(mgr, chunkID, destrType, data)

            if destrType == DestructiblesCache.DESTR_TYPE_FRAGILE:
                self._forceFragileHidden(spaceID, chunkID, itemIndex)

            self._broken.add(brokenKey)

            if DEBUG:
                LOG_DEBUG(
                    '[DEST] type=%s chunk=%s item=%s spd=%.1f file=%s' % (
                        destrType,
                        chunkID,
                        itemIndex,
                        speed,
                        filename,
                    )
                )

            return True

        except Exception:
            LOG_CURRENT_EXCEPTION()
            self._broken.add(brokenKey)
            return False

    def _order(self, mgr, chunkID, destrType, data):
        mgr.orderDestructibleDestroy(
            chunkID,
            int(destrType),
            int(data),
            not INSTANT_FALL,
            syncWithProjectile=False
        )

    def _getChunkItems(self, spaceID, chunkID):
        if self._spaceID != spaceID:
            self.reset()
            self._spaceID = spaceID

        if chunkID in self._chunkCache:
            return self._chunkCache[chunkID]

        now = BigWorld.time()

        if self._chunkRetry.get(chunkID, 0.0) > now:
            return []

        mgr = AreaDestructibles.g_destructiblesManager
        cache = AreaDestructibles.g_cache

        if mgr is None or cache is None:
            self._chunkCache[chunkID] = []
            return []

        if not ensureChunkLoaded(mgr, spaceID, chunkID):
            self._chunkRetry[chunkID] = now + CHUNK_RETRY_TIMEOUT
            return []

        try:
            filenames = BigWorld.wg_getChunkDestrFilenames(spaceID, chunkID)
        except Exception:
            filenames = None

        if filenames is None:
            self._chunkRetry[chunkID] = now + CHUNK_RETRY_TIMEOUT
            return []

        try:
            count = len(filenames)
        except Exception:
            try:
                count = int(filenames)
            except Exception:
                count = 0

        if count <= 0:
            self._chunkCache[chunkID] = []
            return []

        try:
            chunkMatrix = BigWorld.wg_getChunkMatrix(spaceID, chunkID)
        except Exception:
            chunkMatrix = None

        if chunkMatrix is None:
            self._chunkRetry[chunkID] = now + CHUNK_RETRY_TIMEOUT
            return []

        items = []

        allowedTypes = (
            DestructiblesCache.DESTR_TYPE_TREE,
            DestructiblesCache.DESTR_TYPE_FALLING_ATOM,
            DestructiblesCache.DESTR_TYPE_FRAGILE,
            DestructiblesCache.DESTR_TYPE_STRUCTURE,
        )

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
                    wp = chunkMatrix.applyPoint(m.translation)
                except Exception:
                    wp = chunkMatrix.translation + m.translation

                try:
                    pos = Math.Vector3(wp.x, wp.y, wp.z)
                except Exception:
                    pos = Math.Vector3(wp[0], wp[1], wp[2])

                if desc is None:
                    if not ENABLE_UNKNOWN_PROXIMITY:
                        continue

                    dtype = -1

                else:
                    dtype = desc.get('type')

                    if dtype not in allowedTypes:
                        continue

                    if dtype == DestructiblesCache.DESTR_TYPE_STRUCTURE:
                        if not _isFenceLike(filename):
                            continue

                items.append((idx, dtype, pos, filename))

            except Exception:
                pass

        self._chunkCache[chunkID] = items
        return items

    def _radiusFor(self, dtype, filename, halfWidth):
        if dtype == DestructiblesCache.DESTR_TYPE_TREE:
            return max(1.6, halfWidth + 0.35)

        if dtype == DestructiblesCache.DESTR_TYPE_FALLING_ATOM:
            return max(1.3, halfWidth + 0.20)

        if dtype == DestructiblesCache.DESTR_TYPE_FRAGILE:
            return max(1.4, halfWidth + 0.25)

        if dtype == DestructiblesCache.DESTR_TYPE_STRUCTURE:
            return max(2.0, halfWidth + 0.60)

        # unknown
        return max(1.1, halfWidth + 0.10)

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
            mgr._DestructiblesManager__setDestructibleState(
                spaceID,
                chunkID,
                itemIndex,
                True,
                None,
                False
            )
        except Exception:
            LOG_CURRENT_EXCEPTION()