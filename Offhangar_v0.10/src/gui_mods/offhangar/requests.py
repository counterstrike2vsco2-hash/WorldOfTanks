# -- coding: utf-8 --
import cPickle
import functools
import random
import time
import zlib
from collections import namedtuple

import AccountCommands
import BigWorld
from debug_utils import LOG_CURRENT_EXCEPTION
from items import vehicles, ITEM_TYPE_INDICES

from gui.mods.offhangar._constants import *
from gui.mods.offhangar.data import *
from gui.mods.offhangar.logging import *
from gui.mods.offhangar.server import *
from gui.mods.offhangar import data as off_data
import struct
from items import tankmen
RequestResult = namedtuple('RequestResult', ['resultID', 'errorStr', 'data'])

_OFFLINE_INVENTORY_ROOT = None
_OFFLINE_VEH_COMP_DESCR = {}
_OFFLINE_VEH_DESCR_CACHE = {}
_OFFLINE_ITEM_PRICES = None
_ITEMS_CACHE_PATCHED = False
_INVENTORY_SAVE_CB = None
_INVENTORY_DIRTY = False

SELL_PRICE_FACTOR = 0.5
PAID_REMOVAL_GOLD_COST = 10
_TMAN_TYPE_IDX = ITEM_TYPE_INDICES['tankman']
_VEH_TYPE_IDX = ITEM_TYPE_INDICES['vehicle']

def baseRequest(cmdID):
    def wrapper(func):
        def requester(requestID, *args):
            result = func(requestID, *args)
            return (requestID, result.resultID, result.errorStr, result.data)
        BASE_REQUESTS[cmdID] = requester
        return func
    return wrapper


def _onStreamComplete(requestID, desc, data):
    BigWorld.player().onStreamComplete(requestID, desc, data)


def packStream(requestID, data):
    packed = zlib.compress(cPickle.dumps(data))
    crc = zlib.crc32(packed)
    desc = (False, len(packed), len(packed), crc, crc)
    return functools.partial(_onStreamComplete, requestID, desc, packed)


def _cdLen(cd):
    try:
        return len(cd)
    except Exception:
        return -1

@baseRequest(AccountCommands.CMD_PREMIUM)
def buyPremium(requestID, shopRev, days, _int3):
    try:
        from constants import ACCOUNT_ATTR
        from PlayerEvents import g_playerEvents as events

        days = int(days)
        if days <= 0:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad days', None)

        goldCost = None
        try:
            from gui.shared import g_itemsCache
            goldCost = g_itemsCache.items.shop.getPremiumPacketCost(days)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        if goldCost is None:
            goldCost = {1: 250, 3: 650, 7: 1250,
                        30: 2500, 180: 13500, 360: 24000}.get(days)
        if goldCost is None:
            return RequestResult(AccountCommands.RES_WRONG_ARGS,
                                 'unknown premium package', None)
        goldCost = int(goldCost)

        if not _hasFunds(0, goldCost):
            return RequestResult(AccountCommands.RES_FAILURE,
                                 'not enough gold', None)

        _changeMoney(0, -goldCost)

        cache = _getStatsCache()
        now = int(time.time())
        curExpiry = int(cache.get('premiumExpiryTime', 0) or 0)
        curAttrs = int(cache.get('attrs', 0) or 0)
        if (curAttrs & int(ACCOUNT_ATTR.PREMIUM)) and curExpiry > now:
            base = curExpiry
        else:
            base = now
        newExpiry = base + days * 86400
        newAttrs = curAttrs | int(ACCOUNT_ATTR.PREMIUM)

        # 1) Аккаунтовый кэш Stats.__cache (attrs + premiumExpiryTime).
        rev = 0
        try:
            rev = int(BigWorld.player().syncData.revision or 0)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        diff = {
            'prevRev': rev,
            'rev': rev + 1,
            'account': {'attrs': newAttrs,
                        'premiumExpiryTime': newExpiry},
        }
        try:
            BigWorld.player().stats.synchronize(False, diff)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        # 2) GUI-кэш StatsRequester._data (его читает шапка).
        try:
            from gui.shared import g_itemsCache
            sr = g_itemsCache.items.stats
            if not isinstance(getattr(sr, '_data', None), dict):
                sr._data = {}
            sr._data['attrs'] = newAttrs
            sr._data['premiumExpiryTime'] = newExpiry
            sr._data['mayConsumeWalletResources'] = 1
        except Exception:
            LOG_CURRENT_EXCEPTION()

        # 3) Offline-кэш на диск + МЕТКА покупки (без неё нормализация
        #    при следующем входе посчитает премиум автоначислением и сбросит).
        try:
            stored = off_data._loadOfflineCache('stats')
            if stored is None:
                stored = off_data.getOfflineStats()
            acc = stored.setdefault('account', {})
            acc['attrs'] = newAttrs
            acc['premiumExpiryTime'] = newExpiry
            acc['_premiumPurchased'] = True
            off_data._saveOfflineCache('stats', stored)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        # 4) Обновление GUI.
        try:
            events.onClientUpdated(diff)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            from gui.ClientUpdateManager import g_clientUpdateManager
            g_clientUpdateManager.update(diff)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        LOG_DEBUG('CMD_PREMIUM OK days=%s gold=%s expiry=%s premBit=%s' % (
            days, goldCost, newExpiry, bool(newAttrs & ACCOUNT_ATTR.PREMIUM)))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)

    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

@baseRequest(AccountCommands.CMD_BUY_SLOT)
def buySlot(requestID, shopRev, _int2, _int3):
    try:
        from gui.shared import g_itemsCache

        sr = g_itemsCache.items.stats
        currentSlots = int(sr.getCacheValue('slots', 0) or 0)

        price = 0
        try:
            price = int(g_itemsCache.items.shop.getVehicleSlotsPrice(currentSlots) or 0)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        if price <= 0:
            # Фолбэк на хвост таблицы цен (та же логика, что в Shop).
            try:
                sp = g_itemsCache.items.shop.slotsPrices
                if sp and len(sp) > 1 and sp[1]:
                    price = int(sp[1][-1])
            except Exception:
                LOG_CURRENT_EXCEPTION()
        if price <= 0:
            price = 300

        if not _hasFunds(0, price):
            return RequestResult(AccountCommands.RES_FAILURE,
                                 'not enough gold', None)

        _changeMoney(0, -price)
        newSlots = currentSlots + 1

        # 1) Аккаунтовый кэш Stats.__cache.
        cache = _getStatsCache()
        cache['slots'] = newSlots

        # 2) GUI-кэш StatsRequester._data (его читает счётчик слотов).
        try:
            if not isinstance(getattr(sr, '_data', None), dict):
                sr._data = {}
            sr._data['slots'] = newSlots
        except Exception:
            LOG_CURRENT_EXCEPTION()

        # 3) Offline-кэш на диск.
        try:
            stored = off_data._loadOfflineCache('stats')
            if stored is None:
                stored = off_data.getOfflineStats()
            stored.setdefault('stats', {})['slots'] = newSlots
            off_data._saveOfflineCache('stats', stored)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        # 4) GUI.
        try:
            from gui.ClientUpdateManager import g_clientUpdateManager
            g_clientUpdateManager.update({'stats': {'slots': newSlots}})
        except Exception:
            LOG_CURRENT_EXCEPTION()

        LOG_DEBUG('CMD_BUY_SLOT OK old=%s new=%s gold=%s' % (
            currentSlots, newSlots, price))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)

    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)
                                                                
# ============================================================
#  Статистика / валюта
# ============================================================



def _getStatsCache():
    cache = None
    try:
        cache = BigWorld.player().stats._Stats__cache
    except Exception:
        cache = None
    if not isinstance(cache, dict):
        cache = {}
    stats = None
    for field in ('credits', 'gold', 'freeXP'):
        if field not in cache:
            if stats is None:
                stats = off_data.getOfflineStats().get('stats', {})
            cache[field] = int(stats.get(field, 0))
    return cache


def _changeMoney(creditsDelta=0, goldDelta=0):
    cache = _getStatsCache()
    cache['credits'] = max(0, int(cache.get('credits', 0)) + int(creditsDelta))
    cache['gold'] = max(0, int(cache.get('gold', 0)) + int(goldDelta))
    try:
        from gui.shared import g_itemsCache
        sr = g_itemsCache.items.stats
        if hasattr(sr, '_cache') and isinstance(sr._cache, dict):
            sr._cache['credits'] = cache['credits']
            sr._cache['gold'] = cache['gold']
    except Exception:
        pass
    try:
        from gui.ClientUpdateManager import g_clientUpdateManager
        g_clientUpdateManager.update({'stats': {'credits': cache['credits'], 'gold': cache['gold']}})
    except Exception:
        pass
    try:
        stats = off_data._loadOfflineCache('stats')
        if stats is None:
            stats = off_data.getOfflineStats()
        s = stats.setdefault('stats', {})
        s['credits'] = cache['credits']
        s['gold'] = cache['gold']
        off_data._saveOfflineCache('stats', stats)
    except Exception:
        LOG_CURRENT_EXCEPTION()

def _patchStatsBuyBerths():
    try:
        player = BigWorld.player()
        stats = getattr(player, 'stats', None)
        if stats is None:
            return
        if getattr(stats, '_offhangar_buyBerths_patched', False):
            return

        def _offlineBuyBerths(callback=None):
            requestID = 1
            args = getattr(player, 'syncData', None)
            shopRev = int(args.revision) if args is not None else 0
            result = buyBerths(requestID, shopRev, 1, 0)
            if callback:
                callback(result.resultID)

        stats.buyBerths = _offlineBuyBerths
        stats._offhangar_buyBerths_patched = True
        LOG_DEBUG('stats.buyBerths offline patch installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()

def _hasFunds(creditsCost, goldCost):
    cache = _getStatsCache()
    if goldCost > 0 and int(cache.get('gold', 0)) < goldCost:
        return False
    if creditsCost > 0 and int(cache.get('credits', 0)) < creditsCost:
        return False
    return True


def _chargeItem(intCD, count=1):
    price = g_itemsCache_items_shop_getPrice(intCD)
    if price is None:
        return True
    creditsCost = int(price[0]) * count
    goldCost = int(price[1]) * count
    if not _hasFunds(creditsCost, goldCost):
        return False
    _changeMoney(-creditsCost, -goldCost)
    return True


def _refundItem(intCD, count=1):
    price = g_itemsCache_items_shop_getPrice(intCD)
    if price is None:
        return
    _changeMoney(
        int(int(price[0]) * count * SELL_PRICE_FACTOR),
        int(int(price[1]) * count * SELL_PRICE_FACTOR)
    )


# ============================================================
#  Цены
# ============================================================

def _getOfflineItemPrice(typeCompDescr):
    global _OFFLINE_ITEM_PRICES
    try:
        if typeCompDescr is None:
            return None
        typeCompDescr = int(typeCompDescr)
        if _OFFLINE_ITEM_PRICES is None:
            _OFFLINE_ITEM_PRICES = off_data.getOfflineShopItems().get('itemPrices', {})
        price = _OFFLINE_ITEM_PRICES.get(typeCompDescr)
        if price is None:
            return None
        return (int(price[0]), int(price[1]))
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return None


def g_itemsCache_items_shop_getPrice(typeCompDescr):
    try:
        from gui.shared import g_itemsCache
        price = g_itemsCache.items.shop.getItemPrice(typeCompDescr)
        if price is not None:
            price = (int(price[0]), int(price[1]))
            if price[0] > 0 or price[1] > 0:
                return price
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return _getOfflineItemPrice(typeCompDescr)


# ============================================================
#  Оффлайн-инвентарь (кэш в памяти + отложенная запись)
# ============================================================

def _getOfflineInventoryRoot():
    global _OFFLINE_INVENTORY_ROOT
    if _OFFLINE_INVENTORY_ROOT is None:
        try:
            cached = off_data._loadOfflineCache('inventory')
            _OFFLINE_INVENTORY_ROOT = cached if cached is not None else off_data.getOfflineInventory()
        except Exception:
            LOG_CURRENT_EXCEPTION()
            _OFFLINE_INVENTORY_ROOT = off_data.getOfflineInventory()
    return _OFFLINE_INVENTORY_ROOT


def _saveOfflineInventoryRootNow(*args):
    global _INVENTORY_SAVE_CB, _INVENTORY_DIRTY
    _INVENTORY_SAVE_CB = None
    if not _INVENTORY_DIRTY or _OFFLINE_INVENTORY_ROOT is None:
        return
    _INVENTORY_DIRTY = False
    try:
        off_data._saveOfflineCache('inventory', _OFFLINE_INVENTORY_ROOT)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _markInventoryDirty():
    global _INVENTORY_SAVE_CB, _INVENTORY_DIRTY
    _INVENTORY_DIRTY = True
    if _INVENTORY_SAVE_CB is not None:
        try:
            BigWorld.cancelCallback(_INVENTORY_SAVE_CB)
        except Exception:
            pass
    try:
        _INVENTORY_SAVE_CB = BigWorld.callback(2.0, _saveOfflineInventoryRootNow)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        _saveOfflineInventoryRootNow()
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _updateOfflineVehicleFields(vehInvID, fields):
    if not fields:
        return
    try:
        inv = _getOfflineInventoryRoot().setdefault('inventory', {})
        vehInv = inv.setdefault(ITEM_TYPE_INDICES['vehicle'], {})
        for field, value in fields.items():
            vehInv.setdefault(field, {})[vehInvID] = value
        _markInventoryDirty()
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _getOfflineVehicleField(vehInvID, field, default=None):
    try:
        value = _getOfflineInventoryRoot().get(
            'inventory', {}).get(ITEM_TYPE_INDICES['vehicle'], {}).get(field, {}).get(vehInvID)
        if value is not None:
            return value
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        value = BigWorld.player().inventory._Inventory__cache.get(
            ITEM_TYPE_INDICES['vehicle'], {}).get(field, {}).get(vehInvID)
        if value is not None:
            return value
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return default


def _getCurrentVehicleCompDescr(vehInvID):
    cd = _OFFLINE_VEH_COMP_DESCR.get(vehInvID)
    if cd:
        return cd
    try:
        cd = BigWorld.player().inventory._Inventory__cache.get(
            ITEM_TYPE_INDICES['vehicle'], {}).get('compDescr', {}).get(vehInvID)
        if cd:
            return cd
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        return _getOfflineInventoryRoot().get(
            'inventory', {}).get(ITEM_TYPE_INDICES['vehicle'], {}).get('compDescr', {}).get(vehInvID)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return None


def _resolveVehInvID(vehInvID):
    """Дерево иногда шлёт vehInvID=0 -> подставляем текущий танк ангара."""
    try:
        if vehInvID is not None and int(vehInvID) > 0:
            return int(vehInvID)
    except Exception:
        pass
    try:
        from CurrentVehicle import g_currentVehicle
        if g_currentVehicle.isPresent():
            return int(g_currentVehicle.invID)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return int(vehInvID or 0)


def _getOfflineVehicleDescr(invID):
    cd = _OFFLINE_VEH_COMP_DESCR.get(invID)
    if cd is None:
        return None
    entry = _OFFLINE_VEH_DESCR_CACHE.get(invID)
    if entry is not None and entry[0] == cd:
        return entry[1]
    try:
        descr = vehicles.VehicleDescr(compactDescr=cd)
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return None
    _OFFLINE_VEH_DESCR_CACHE[invID] = (cd, descr)
    return descr


def _removeVehicleFromOfflineInventory(vehInvID):
    try:
        inv = _getOfflineInventoryRoot().get('inventory', {})
        vehInv = inv.get(ITEM_TYPE_INDICES['vehicle'], {})
        for f in ('compDescr', 'crew', 'settings', 'eqs', 'eqsLayout',
                  'shells', 'shellsLayout', 'repair', 'lock',
                  'customizationExpiryTime', 'lastCrew'):
            vehInv.get(f, {}).pop(vehInvID, None)
        _markInventoryDirty()
    except Exception:
        LOG_CURRENT_EXCEPTION()

def _addVehicleToInventory(typeCompDescr):
    try:
        import items
        root = _getOfflineInventoryRoot()
        inv = root.setdefault('inventory', {})
        vehInv = inv.setdefault(ITEM_TYPE_INDICES['vehicle'], {})
        compDescrs = vehInv.setdefault('compDescr', {})
        newInvID = max(compDescrs.keys()) + 1 if compDescrs else 1
        itemTypeID, nationID, vehicleTypeID = vehicles.parseIntCompactDescr(typeCompDescr)
        descr = vehicles.VehicleDescr(typeID=(nationID, vehicleTypeID))
        compDescrs[newInvID] = descr.makeCompactDescr()

        tmanInv = inv.setdefault(ITEM_TYPE_INDICES['tankman'], {})
        tmanVeh = tmanInv.setdefault('vehicle', {})
        tmanCD = tmanInv.setdefault('compDescr', {})
        nextTmanID = max(tmanCD.keys()) + 1 if tmanCD else 1
        tmanList = items.tankmen.generateTankmen(
            nationID, vehicleTypeID, descr.type.crewRoles, False,
            items.tankmen.MAX_SKILL_LEVEL, [])
        crewIDs = []
        for tman in tmanList:
            tmanCD[nextTmanID] = tman
            tmanVeh[nextTmanID] = newInvID
            crewIDs.append(nextTmanID)
            nextTmanID += 1

        vehInv.setdefault('crew', {})[newInvID] = crewIDs
        vehInv.setdefault('settings', {})[newInvID] = 0
        vehInv.setdefault('eqs', {})[newInvID] = []
        vehInv.setdefault('eqsLayout', {})[newInvID] = []
        vehInv.setdefault('shells', {})[newInvID] = vehicles.getDefaultAmmoForGun(descr.gun)
        vehInv.setdefault('shellsLayout', {})[newInvID] = {}
        vehInv.setdefault('repair', {})[newInvID] = (0, 0)
        vehInv.setdefault('lock', {})[newInvID] = 0
        _markInventoryDirty()
        return newInvID, crewIDs
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return None, []


# ============================================================
#  Хелперы для простых предметов
# ============================================================

def _getPendingItemCount(simpleItems, itemTypeIdx, intCD):
    try:
        intCD = int(intCD)
        if intCD <= 0:
            return 0
        pending = simpleItems.get(itemTypeIdx, {}).get(intCD)
        if pending is not None:
            return max(0, int(pending))
        return int(
            _getOfflineInventoryRoot().get('inventory', {}).get(itemTypeIdx, {}).get(intCD, 0) or 0
        )
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return 0


def _setPendingItemCount(simpleItems, itemTypeIdx, intCD, count):
    try:
        intCD = int(intCD)
        if intCD <= 0:
            return
        simpleItems.setdefault(itemTypeIdx, {})[intCD] = max(0, int(count))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _addSimpleItemCount(simpleItems, itemTypeIdx, intCD, count=1):
    if intCD is None:
        return
    existing = _getPendingItemCount(simpleItems, itemTypeIdx, intCD)
    _setPendingItemCount(simpleItems, itemTypeIdx, intCD, existing + int(count))


def _consumeSimpleItem(simpleItems, itemTypeIdx, intCD, count=1):
    if intCD is None:
        return False
    existing = _getPendingItemCount(simpleItems, itemTypeIdx, intCD)
    count = int(count)
    if existing < count:
        return False
    _setPendingItemCount(simpleItems, itemTypeIdx, intCD, existing - count)
    return True


def _ensureSimpleItem(simpleItems, itemTypeIdx, intCD, count=1):
    if intCD is None:
        return
    intCD = int(intCD)
    if intCD <= 0:
        return
    existing = 0
    try:
        existing = int(
            _getOfflineInventoryRoot().get('inventory', {}).get(itemTypeIdx, {}).get(intCD, 0) or 0
        )
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if existing < count:
        simpleItems.setdefault(itemTypeIdx, {})[intCD] = max(existing, count)


def _addSimpleItem(simpleItems, itemTypeIdx, intCD, count=1):
    if intCD is None:
        return
    intCD = int(intCD)
    count = int(count)
    if intCD <= 0 or count <= 0:
        return
    existing = 0
    try:
        existing = int(
            _getOfflineInventoryRoot().get('inventory', {}).get(itemTypeIdx, {}).get(intCD, 0) or 0
        )
    except Exception:
        LOG_CURRENT_EXCEPTION()
    simpleItems.setdefault(itemTypeIdx, {})[intCD] = existing + count


def _getShellCompDescrsForGun(gunDescr):
    result = []
    try:
        for shot in gunDescr['shots']:
            result.append(shot['shell']['compactDescr'])
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return result


def _maybeUpdateShellsAfterGunChange(vehInvID, descr, oldTurretCD, oldGunCD, fields):
    newTurretCD = descr.turret['compactDescr']
    newGunCD = descr.gun['compactDescr']
    if newTurretCD != oldTurretCD or newGunCD != oldGunCD:
        layouts = dict(_getOfflineVehicleField(vehInvID, 'shellsLayout', {}) or {})
        key = (newTurretCD, newGunCD)
        shells = layouts.get(key)
        if not shells:
            shells = vehicles.getDefaultAmmoForGun(descr.gun)
        layouts[key] = shells
        fields['shells'] = shells
        fields['shellsLayout'] = layouts


# ============================================================
#  Патч ItemsCache.getVehicle
# ============================================================

def _patchVehicleItem(item):
    if item is None:
        return item
    invID = getattr(item, 'invID', None)
    newCD = _OFFLINE_VEH_COMP_DESCR.get(invID)
    if not newCD:
        return item
    newDescr = _getOfflineVehicleDescr(invID)
    if newDescr is None:
        return item
    try:
        if getattr(item, '_offhangar_cd', None) == newCD:
            return item
        item._Vehicle__descr = newDescr
        item._offhangar_cd = newCD
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return item


def patchItemsCacheGetVehicle(itemsCache):
    global _ITEMS_CACHE_PATCHED
    if _ITEMS_CACHE_PATCHED:
        return
    try:
        items = itemsCache.items
        origGetVehicle = items.getVehicle
        orig = getattr(origGetVehicle, '_offhangar_orig', origGetVehicle)

        def _patchedGetVehicle(invID):
            return _patchVehicleItem(orig(invID))

        _patchedGetVehicle._offhangar_patched = True
        _patchedGetVehicle._offhangar_orig = orig
        items.getVehicle = _patchedGetVehicle
        _ITEMS_CACHE_PATCHED = True
    except Exception:
        LOG_CURRENT_EXCEPTION()

# ============================================================
#  Единая мгновенная перерисовка открытого дерева исследований
#  (TechTree + Research). Один список view, один патч, одна функция.
# ============================================================

def _getOfflineTankmanCD(tmanInvID):
    try:
        cd = _getOfflineInventoryRoot().get('inventory', {}).get(_TMAN_TYPE_IDX, {}).get('compDescr', {}).get(tmanInvID)
        if cd:
            return cd
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        return BigWorld.player().inventory._Inventory__cache.get(_TMAN_TYPE_IDX, {}).get('compDescr', {}).get(tmanInvID)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return None


def _getOfflineTankmanVehicle(tmanInvID):
    try:
        v = _getOfflineInventoryRoot().get('inventory', {}).get(_TMAN_TYPE_IDX, {}).get('vehicle', {}).get(tmanInvID)
        if v is not None:
            return int(v)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 0


def _getVehicleCrew(vehInvID):
    crew = _getOfflineVehicleField(vehInvID, 'crew', None)
    if crew is not None:
        return list(crew)
    try:
        cd = _getCurrentVehicleCompDescr(vehInvID)
        if cd:
            descr = vehicles.VehicleDescr(compactDescr=cd)
            return [0] * len(descr.type.crewRoles)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return []


def _findTankmanSlot(tmanInvID, vehInvID):
    crew = _getVehicleCrew(vehInvID)
    for idx, tid in enumerate(crew):
        if tid == tmanInvID:
            return idx
    return -1


def _pushInventoryDiff(invDiff, refreshVehInvID=None):
    if not invDiff:
        return
    try:
        BigWorld.player().inventory.synchronize(False, {'inventory': invDiff})
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from gui.shared import g_itemsCache
        from gui.shared.ItemsCache import CACHE_SYNC_REASON
        invalidItems = g_itemsCache.items.invalidateCache({'inventory': invDiff})
        g_itemsCache.onSyncCompleted(CACHE_SYNC_REASON.INVENTORY_RESYNC, invalidItems)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from gui.ClientUpdateManager import g_clientUpdateManager
        g_clientUpdateManager.update({'inventory': invDiff})
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from PlayerEvents import g_playerEvents
        g_playerEvents.onInventoryResync()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if refreshVehInvID is not None:
        try:
            BigWorld.callback(0.1, functools.partial(_refreshCurrentVehicle, refreshVehInvID))
        except Exception:
            LOG_CURRENT_EXCEPTION()


def _commitTankmanChanges(tmanFields=None, vehFields=None, refreshVehInvID=None):
    tmanFields = tmanFields or {}
    vehFields = vehFields or {}
    root = _getOfflineInventoryRoot().setdefault('inventory', {})
    invDiff = {}
    if tmanFields:
        tmanInv = root.setdefault(_TMAN_TYPE_IDX, {})
        tmanDiff = {}
        for tid, fields in tmanFields.items():
            for field, value in fields.items():
                if value is None:
                    tmanInv.get(field, {}).pop(tid, None)
                else:
                    tmanInv.setdefault(field, {})[tid] = value
                tmanDiff.setdefault(field, {})[tid] = value
        invDiff[_TMAN_TYPE_IDX] = tmanDiff
    if vehFields:
        vehInv = root.setdefault(_VEH_TYPE_IDX, {})
        vehDiff = {}
        for vid, fields in vehFields.items():
            for field, value in fields.items():
                vehInv.setdefault(field, {})[vid] = value
                vehDiff.setdefault(field, {})[vid] = value
        invDiff[_VEH_TYPE_IDX] = vehDiff
    if not invDiff:
        return
    _markInventoryDirty()
    _pushInventoryDiff(invDiff, refreshVehInvID)


def _getTankmanCostTable():
    try:
        from gui.shared import g_itemsCache
        table = g_itemsCache.items.shop.getTankmanCost()
        if table:
            return table
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return ({'isPremium': False, 'baseRoleLoss': 0.2, 'gold': 0, 'credits': 0, 'classChangeRoleLoss': 0.2, 'roleLevel': 50},
            {'isPremium': False, 'baseRoleLoss': 0.1, 'gold': 0, 'credits': 20000, 'classChangeRoleLoss': 0.1, 'roleLevel': 75},
            {'isPremium': True, 'baseRoleLoss': 0.0, 'gold': 200, 'credits': 0, 'classChangeRoleLoss': 0.0, 'roleLevel': 100})


def _getDropSkillsCostTable():
    try:
        from gui.shared import g_itemsCache
        table = g_itemsCache.items.shop.getDropSkillsCost()
        if table:
            return table
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return {0: {'xpReuseFraction': 0.5, 'gold': 0, 'credits': 0},
            1: {'xpReuseFraction': 0.75, 'gold': 0, 'credits': 20000},
            2: {'xpReuseFraction': 1.0, 'gold': 200, 'credits': 0}}


def _getPassportChangeCost():
    try:
        from gui.shared import g_itemsCache
        v = g_itemsCache.items.shop.getPassportChangeCost()
        if v:
            return int(v)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 50


def _getChangeRoleCost():
    try:
        from gui.shared import g_itemsCache
        v = g_itemsCache.items.shop.getChangeRoleCost()
        if v:
            return int(v)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 500


def _rebuildTankmanCD(descr, newVehicleTypeID, newRole, newSkills, newLastSkillLevel):
    pack = struct.pack
    header = tankmen.ITEM_TYPES.tankman + (descr.nationID << 4)
    cd = pack('4B', header, newVehicleTypeID, tankmen.SKILL_INDICES[newRole], descr.roleLevel)
    numSkills = len(newSkills)
    skillIdxs = [tankmen.SKILL_INDICES[s] for s in newSkills]
    cd += pack(str(1 + numSkills) + 'B', numSkills, *skillIdxs)
    cd += chr(newLastSkillLevel if numSkills else 0)
    isFemale = 1 if descr.isFemale else 0
    isPremium = 1 if descr.isPremium else 0
    flags = isFemale | isPremium << 1
    rank = descr._TankmanDescr__rankIdx & 31 | (descr.numLevelsToNextRank & 2047) << 5
    cd += pack('<B4Hi', flags, descr.firstNameID, descr.lastNameID, descr.iconID, rank, descr.freeXP)
    cd += descr.dossierCompactDescr
    return cd


@baseRequest(AccountCommands.CMD_EQUIP_TMAN)
def equipTankman(requestID, vehInvID, slot, tmanInvID):
    try:
        vehInvID = _resolveVehInvID(vehInvID)
        slot = int(slot)
        tmanInvID = int(tmanInvID)
        crew = _getVehicleCrew(vehInvID)
        if slot < 0 or slot >= len(crew):
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad slot', None)
        tmanFields = {}
        vehFields = {}
        oldTmanID = crew[slot]
        if oldTmanID and oldTmanID != tmanInvID:
            tmanFields[oldTmanID] = {'vehicle': 0}
        if tmanInvID > 0:
            prevVehID = _getOfflineTankmanVehicle(tmanInvID)
            if prevVehID and prevVehID != vehInvID:
                prevCrew = _getVehicleCrew(prevVehID)
                prevSlot = _findTankmanSlot(tmanInvID, prevVehID)
                if prevSlot >= 0:
                    prevCrew[prevSlot] = 0
                    vehFields.setdefault(prevVehID, {})['crew'] = prevCrew
            tmanFields[tmanInvID] = {'vehicle': vehInvID}
        crew[slot] = tmanInvID if tmanInvID > 0 else None
        vehFields.setdefault(vehInvID, {})['crew'] = crew
        LOG_DEBUG('CMD_EQUIP_TMAN vehInvID=%s slot=%s tman=%s' % (vehInvID, slot, tmanInvID))
        _commitTankmanChanges(tmanFields, vehFields, refreshVehInvID=vehInvID)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_RETURN_CREW)
def returnCrew(requestID, vehInvID, int2, int3):
    try:
        vehInvID = _resolveVehInvID(vehInvID)
        crew = _getVehicleCrew(vehInvID)
        tmanFields = {}
        for tmanID in crew:
            if tmanID:
                tmanFields[tmanID] = {'vehicle': 0}
        vehFields = {vehInvID: {'crew': [None] * len(crew)}}
        LOG_DEBUG('CMD_RETURN_CREW vehInvID=%s crew=%s' % (vehInvID, crew))
        _commitTankmanChanges(tmanFields, vehFields, refreshVehInvID=vehInvID)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_DISMISS_TMAN)
def dismissTankman(requestID, tmanInvID, int2, int3):
    try:
        tmanInvID = int(tmanInvID)
        vehInvID = _getOfflineTankmanVehicle(tmanInvID)
        tmanFields = {tmanInvID: {'compDescr': None, 'vehicle': None}}
        vehFields = {}
        if vehInvID:
            crew = _getVehicleCrew(vehInvID)
            slot = _findTankmanSlot(tmanInvID, vehInvID)
            if slot >= 0:
                crew[slot] = None
                vehFields[vehInvID] = {'crew': crew}
        LOG_DEBUG('CMD_DISMISS_TMAN tman=%s veh=%s' % (tmanInvID, vehInvID))
        _commitTankmanChanges(tmanFields, vehFields, refreshVehInvID=vehInvID or None)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

@baseRequest(AccountCommands.CMD_BUY_BERTHS)
def buyBerths(requestID, shopRev, count, _int3):
    try:
        count = int(count)
        if count <= 0:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad count', None)

        cache = _getStatsCache()
        currentBerths = int(cache.get('berths', 0) or 0)

        price = 0
        try:
            from gui.shared import g_itemsCache
            price = int(g_itemsCache.items.shop.getBerthsPrice(currentBerths, count) or 0)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        if price <= 0:
            price = 100 * count

        if not _hasFunds(0, price):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough gold', None)

        _changeMoney(0, -price)
        newBerths = currentBerths + count
        cache['berths'] = newBerths

        try:
            from gui.shared import g_itemsCache
            sr = g_itemsCache.items.stats
            if not isinstance(getattr(sr, '_data', None), dict):
                sr._data = {}
            sr._data['berths'] = newBerths
        except Exception:
            LOG_CURRENT_EXCEPTION()

        try:
            stored = off_data._loadOfflineCache('stats')
            if stored is None:
                stored = off_data.getOfflineStats()
            stored.setdefault('stats', {})['berths'] = newBerths
            off_data._saveOfflineCache('stats', stored)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        try:
            from gui.ClientUpdateManager import g_clientUpdateManager
            g_clientUpdateManager.update({'stats': {'berths': newBerths}})
        except Exception:
            LOG_CURRENT_EXCEPTION()

        _fireStatsResync()

        LOG_DEBUG('CMD_BUY_BERTHS OK old=%s new=%s gold=%s' % (
            currentBerths, newBerths, price))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)

    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

@baseRequest(AccountCommands.CMD_TMAN_ADD_SKILL)
def tmanAddSkill(requestID, tmanInvID, skillIdx, int3):
    try:
        tmanInvID = int(tmanInvID)
        skillIdx = int(skillIdx)
        cd = _getOfflineTankmanCD(tmanInvID)
        if cd is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'tankman not found', None)
        skillName = tankmen.SKILL_NAMES[skillIdx]
        descr = tankmen.TankmanDescr(cd)
        descr.addSkill(skillName)
        newCD = descr.makeCompactDescr()
        vehInvID = _getOfflineTankmanVehicle(tmanInvID)
        LOG_DEBUG('CMD_TMAN_ADD_SKILL tman=%s skill=%s' % (tmanInvID, skillName))
        _commitTankmanChanges({tmanInvID: {'compDescr': newCD}}, refreshVehInvID=vehInvID or None)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_TMAN_DROP_SKILLS)
def tmanDropSkills(requestID, shopRev, tmanInvID, dropSkillsCostIdx):
    try:
        tmanInvID = int(tmanInvID)
        dropSkillsCostIdx = int(dropSkillsCostIdx)
        cd = _getOfflineTankmanCD(tmanInvID)
        if cd is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'tankman not found', None)
        cost = _getDropSkillsCostTable().get(dropSkillsCostIdx)
        if cost is None:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad cost idx', None)
        creditsCost = int(cost.get('credits', 0))
        goldCost = int(cost.get('gold', 0))
        if not _hasFunds(creditsCost, goldCost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
        descr = tankmen.TankmanDescr(cd)
        descr.dropSkills(float(cost.get('xpReuseFraction', 0.0)))
        newCD = descr.makeCompactDescr()
        if creditsCost or goldCost:
            _changeMoney(-creditsCost, -goldCost)
        vehInvID = _getOfflineTankmanVehicle(tmanInvID)
        LOG_DEBUG('CMD_TMAN_DROP_SKILLS tman=%s costIdx=%s' % (tmanInvID, dropSkillsCostIdx))
        _commitTankmanChanges({tmanInvID: {'compDescr': newCD}}, refreshVehInvID=vehInvID or None)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_TRAINING_TMAN)
def trainingTman(requestID, shopRev, tmanInvID, freeXP):
    try:
        tmanInvID = int(tmanInvID)
        freeXP = int(freeXP)
        cd = _getOfflineTankmanCD(tmanInvID)
        if cd is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'tankman not found', None)
        cache = _getStatsCache()
        curFreeXP = int(cache.get('freeXP', 0))
        if freeXP <= 0 or freeXP > curFreeXP:
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough freeXP', None)
        rate = 10
        try:
            from gui.shared import g_itemsCache
            rate = int(g_itemsCache.items.shop.freeXPToTManXPRate or 10)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        descr = tankmen.TankmanDescr(cd)
        descr.addXP(freeXP * rate)
        newCD = descr.makeCompactDescr()
        cache['freeXP'] = curFreeXP - freeXP
        _saveStatsToOfflineCache()
        vehInvID = _getOfflineTankmanVehicle(tmanInvID)
        LOG_DEBUG('CMD_TRAINING_TMAN tman=%s freeXP=%s rate=%s' % (tmanInvID, freeXP, rate))
        _commitTankmanChanges({tmanInvID: {'compDescr': newCD}}, refreshVehInvID=vehInvID or None)
        try:
            from gui.ClientUpdateManager import g_clientUpdateManager
            g_clientUpdateManager.update({'stats': {'freeXP': cache['freeXP']}})
        except Exception:
            LOG_CURRENT_EXCEPTION()
        _fireStatsResync()
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_ADD_TMAN_XP)
def addTmanXP(requestID, tmanInvID, xp, int3):
    try:
        tmanInvID = int(tmanInvID)
        xp = int(xp)
        cd = _getOfflineTankmanCD(tmanInvID)
        if cd is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'tankman not found', None)
        descr = tankmen.TankmanDescr(cd)
        descr.addXP(xp)
        newCD = descr.makeCompactDescr()
        vehInvID = _getOfflineTankmanVehicle(tmanInvID)
        LOG_DEBUG('CMD_ADD_TMAN_XP tman=%s xp=%s' % (tmanInvID, xp))
        _commitTankmanChanges({tmanInvID: {'compDescr': newCD}}, refreshVehInvID=vehInvID or None)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_TMAN_RESPEC)
def tmanRespec(requestID, shopRev, tmanInvID, tmanCostTypeIdx, vehTypeCompDescr):
    try:
        tmanInvID = int(tmanInvID)
        tmanCostTypeIdx = int(tmanCostTypeIdx)
        vehTypeCompDescr = int(vehTypeCompDescr)
        cd = _getOfflineTankmanCD(tmanInvID)
        if cd is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'tankman not found', None)
        costTable = _getTankmanCostTable()
        if tmanCostTypeIdx < 0 or tmanCostTypeIdx >= len(costTable):
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad cost idx', None)
        cost = costTable[tmanCostTypeIdx]
        creditsCost = int(cost.get('credits', 0))
        goldCost = int(cost.get('gold', 0))
        if not _hasFunds(creditsCost, goldCost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
        descr = tankmen.TankmanDescr(cd)
        newVehTypeID = vehicles.parseIntCompactDescr(vehTypeCompDescr)[2] if vehTypeCompDescr > 0 else descr.vehicleTypeID
        descr.respecialize(newVehTypeID, int(cost.get('roleLevel', 50)),
                            float(cost.get('baseRoleLoss', 0.0)),
                            float(cost.get('classChangeRoleLoss', 0.0)),
                            bool(cost.get('isPremium', False)))
        newCD = descr.makeCompactDescr()
        if creditsCost or goldCost:
            _changeMoney(-creditsCost, -goldCost)
        vehInvID = _getOfflineTankmanVehicle(tmanInvID)
        LOG_DEBUG('CMD_TMAN_RESPEC tman=%s costIdx=%s vehType=%s' % (tmanInvID, tmanCostTypeIdx, vehTypeCompDescr))
        _commitTankmanChanges({tmanInvID: {'compDescr': newCD}}, refreshVehInvID=vehInvID or None)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_TMAN_MULTI_RESPEC)
def tmanMultiRespec(requestID, arr):
    try:
        if not arr or len(arr) < 4:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_TMAN_MULTI_RESPEC: bad args', None)
        vehTypeCompDescr = int(arr[1])
        pairs = arr[2:]
        costTable = _getTankmanCostTable()
        prepared = []
        totalCredits = 0
        totalGold = 0
        for i in range(0, len(pairs) - 1, 2):
            tmanInvID = int(pairs[i])
            costIdx = int(pairs[i + 1])
            cd = _getOfflineTankmanCD(tmanInvID)
            if cd is None or costIdx < 0 or costIdx >= len(costTable):
                return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad tankman/cost', None)
            cost = costTable[costIdx]
            totalCredits += int(cost.get('credits', 0))
            totalGold += int(cost.get('gold', 0))
            prepared.append((tmanInvID, cd, cost))
        if not _hasFunds(totalCredits, totalGold):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
        tmanFields = {}
        refreshVeh = None
        for tmanInvID, cd, cost in prepared:
            descr = tankmen.TankmanDescr(cd)
            newVehTypeID = vehicles.parseIntCompactDescr(vehTypeCompDescr)[2] if vehTypeCompDescr > 0 else descr.vehicleTypeID
            descr.respecialize(newVehTypeID, int(cost.get('roleLevel', 50)),
                                float(cost.get('baseRoleLoss', 0.0)),
                                float(cost.get('classChangeRoleLoss', 0.0)),
                                bool(cost.get('isPremium', False)))
            tmanFields[tmanInvID] = {'compDescr': descr.makeCompactDescr()}
            refreshVeh = refreshVeh or _getOfflineTankmanVehicle(tmanInvID)
        if totalCredits or totalGold:
            _changeMoney(-totalCredits, -totalGold)
        LOG_DEBUG('CMD_TMAN_MULTI_RESPEC count=%s vehType=%s' % (len(prepared), vehTypeCompDescr))
        _commitTankmanChanges(tmanFields, refreshVehInvID=refreshVeh or None)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(getattr(AccountCommands, 'CMD_TMAN_CHANGE_ROLE', -9086))
def tmanChangeRole(requestID, shopRev, tmanInvID, roleIdx, vehTypeCompDescr):
    try:
        tmanInvID = int(tmanInvID)
        roleIdx = int(roleIdx)
        vehTypeCompDescr = int(vehTypeCompDescr)
        cd = _getOfflineTankmanCD(tmanInvID)
        if cd is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'tankman not found', None)
        roleName = tankmen.SKILL_NAMES[roleIdx]
        if roleName not in tankmen.ROLES:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad role', None)
        cost = _getChangeRoleCost()
        if not _hasFunds(0, cost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough gold', None)
        descr = tankmen.TankmanDescr(cd)
        newVehTypeID = vehicles.parseIntCompactDescr(vehTypeCompDescr)[2] if vehTypeCompDescr > 0 else descr.vehicleTypeID
        oldSkills = list(descr._TankmanDescr__skills)
        oldLastLevel = descr._TankmanDescr__lastSkillLevel
        newSkills = [s for s in oldSkills if s in tankmen.SKILLS_BY_ROLES[roleName]]
        if newSkills and oldSkills and newSkills[-1] == oldSkills[-1]:
            newLastLevel = oldLastLevel
        else:
            newLastLevel = tankmen.MAX_SKILL_LEVEL if newSkills else 0
        newCD = _rebuildTankmanCD(descr, newVehTypeID, roleName, newSkills, newLastLevel)
        if cost:
            _changeMoney(0, -cost)
        vehInvID = _getOfflineTankmanVehicle(tmanInvID)
        LOG_DEBUG('CMD_TMAN_CHANGE_ROLE tman=%s role=%s vehType=%s' % (tmanInvID, roleName, vehTypeCompDescr))
        _commitTankmanChanges({tmanInvID: {'compDescr': newCD}}, refreshVehInvID=vehInvID or None)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_TMAN_PASSPORT)
def tmanPassport(requestID, arr):
    try:
        if not arr or len(arr) < 10:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_TMAN_PASSPORT: bad args', None)
        _shopRev, tmanInvID, isPremium, isFemaleFlag, fnGroupID, firstNameID, lnGroupID, lastNameID, iGroupID, iconID = arr[:10]
        tmanInvID = int(tmanInvID)
        cd = _getOfflineTankmanCD(tmanInvID)
        if cd is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'tankman not found', None)
        isFemale = None if int(isFemaleFlag) < 0 else bool(int(isFemaleFlag))
        firstNameID = None if int(firstNameID) < 0 else int(firstNameID)
        lastNameID = None if int(lastNameID) < 0 else int(lastNameID)
        iconID = None if int(iconID) < 0 else int(iconID)
        descr = tankmen.TankmanDescr(cd)
        ok, err, ctx = descr.validatePassport(bool(int(isPremium)), isFemale, int(fnGroupID), firstNameID,
                                              int(lnGroupID), lastNameID, int(iGroupID), iconID)
        if not ok:
            return RequestResult(AccountCommands.RES_FAILURE, err, None)
        cost = _getPassportChangeCost()
        if not _hasFunds(0, cost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough gold', None)
        descr.replacePassport(ctx)
        newCD = descr.makeCompactDescr()
        if cost:
            _changeMoney(0, -cost)
        vehInvID = _getOfflineTankmanVehicle(tmanInvID)
        LOG_DEBUG('CMD_TMAN_PASSPORT tman=%s' % tmanInvID)
        _commitTankmanChanges({tmanInvID: {'compDescr': newCD}}, refreshVehInvID=vehInvID or None)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_BUY_TMAN)
def buyTankman(requestID, shopRev, vehTypeCompDescr, roleIdx, tmanCostTypeIdx):
    try:
        vehTypeCompDescr = int(vehTypeCompDescr)
        roleIdx = int(roleIdx)
        tmanCostTypeIdx = int(tmanCostTypeIdx)
        roleName = tankmen.SKILL_NAMES[roleIdx]
        if roleName not in tankmen.ROLES:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad role', None)
        costTable = _getTankmanCostTable()
        if tmanCostTypeIdx < 0 or tmanCostTypeIdx >= len(costTable):
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad cost idx', None)
        cost = costTable[tmanCostTypeIdx]
        creditsCost = int(cost.get('credits', 0))
        goldCost = int(cost.get('gold', 0))
        if not _hasFunds(creditsCost, goldCost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
        _itemType, nationID, vehicleTypeID = vehicles.parseIntCompactDescr(vehTypeCompDescr)
        isPremium = bool(cost.get('isPremium', False))
        roleLevel = int(cost.get('roleLevel', 50))
        passport = tankmen.generatePassport(nationID, isPremium)
        newCD = tankmen.generateCompactDescr(passport, vehicleTypeID, roleName, roleLevel)
        root = _getOfflineInventoryRoot()
        tmanCD = root.setdefault('inventory', {}).setdefault(_TMAN_TYPE_IDX, {}).setdefault('compDescr', {})
        newInvID = max(tmanCD.keys()) + 1 if tmanCD else 1
        if creditsCost or goldCost:
            _changeMoney(-creditsCost, -goldCost)
        LOG_DEBUG('CMD_BUY_TMAN veh=%s role=%s costIdx=%s -> tman=%s' % (
            vehTypeCompDescr, roleName, tmanCostTypeIdx, newInvID))
        _commitTankmanChanges({newInvID: {'compDescr': newCD, 'vehicle': 0}})
        return RequestResult(AccountCommands.RES_SUCCESS, '', {'tmanInvID': newInvID, 'tmanCompDescr': newCD})
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_BUY_AND_EQUIP_TMAN)
def buyAndEquipTankman(requestID, shopRev, vehInvID, slot, tmanCostTypeIdx):
    try:
        vehInvID = _resolveVehInvID(vehInvID)
        slot = int(slot)
        tmanCostTypeIdx = int(tmanCostTypeIdx)
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        vdescr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        crew = _getVehicleCrew(vehInvID)
        if slot < 0 or slot >= len(crew):
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad slot', None)
        roleName = vdescr.type.crewRoles[slot][0]
        costTable = _getTankmanCostTable()
        if tmanCostTypeIdx < 0 or tmanCostTypeIdx >= len(costTable):
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad cost idx', None)
        cost = costTable[tmanCostTypeIdx]
        creditsCost = int(cost.get('credits', 0))
        goldCost = int(cost.get('gold', 0))
        if not _hasFunds(creditsCost, goldCost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
        isPremium = bool(cost.get('isPremium', False))
        roleLevel = int(cost.get('roleLevel', 50))
        passport = tankmen.generatePassport(vdescr.type.id[0], isPremium)
        newCD = tankmen.generateCompactDescr(passport, vdescr.type.id[1], roleName, roleLevel)
        root = _getOfflineInventoryRoot()
        tmanCD = root.setdefault('inventory', {}).setdefault(_TMAN_TYPE_IDX, {}).setdefault('compDescr', {})
        newInvID = max(tmanCD.keys()) + 1 if tmanCD else 1
        oldTmanID = crew[slot]
        tmanFields = {newInvID: {'compDescr': newCD, 'vehicle': vehInvID}}
        if oldTmanID:
            tmanFields[oldTmanID] = {'vehicle': 0}
        crew[slot] = newInvID
        if creditsCost or goldCost:
            _changeMoney(-creditsCost, -goldCost)
        LOG_DEBUG('CMD_BUY_AND_EQUIP_TMAN veh=%s slot=%s role=%s -> tman=%s' % (
            vehInvID, slot, roleName, newInvID))
        _commitTankmanChanges(tmanFields, {vehInvID: {'crew': crew}}, refreshVehInvID=vehInvID)
        return RequestResult(AccountCommands.RES_SUCCESS, '', {'tmanInvID': newInvID, 'tmanCompDescr': newCD})
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

_OPEN_TREE_VIEWS = []
_TREE_VIEW_PATCHED = False


def _forceTreeUpdate(vehTypeCD=None, doInstalled=True, doInventory=True):
    """Перерисовывает узлы во всех открытых деревьях: установку модулей
    (doInstalled) и состояние инвентаря узла танка (doInventory)."""
    if vehTypeCD is not None:
        try:
            vehTypeCD = int(vehTypeCD)
        except Exception:
            vehTypeCD = None
    alive = []
    for ref in list(_OPEN_TREE_VIEWS):
        view = ref()
        if view is None:
            continue
        alive.append(ref)
        if doInstalled:
            fn = getattr(view, 'invalidateInstalled', None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    LOG_CURRENT_EXCEPTION()
        if doInventory and vehTypeCD is not None:
            inv = getattr(view, 'invalidateInventory', None)
            if callable(inv):
                try:
                    inv([vehTypeCD])
                except Exception:
                    LOG_CURRENT_EXCEPTION()
    _OPEN_TREE_VIEWS[:] = alive

def _patchResearchViewTracking(tries=30):
    global _TREE_VIEW_PATCHED
    if _TREE_VIEW_PATCHED:
        return
    try:
        from gui.Scaleform.daapi.view.lobby.techtree.ResearchView import ResearchView
    except Exception:
        if tries > 0:
            try:
                BigWorld.callback(1.0, lambda t=tries - 1: _patchResearchViewTracking(t))
            except Exception:
                pass
        return
    if getattr(ResearchView, '_offhangar_rvtrack', False):
        _TREE_VIEW_PATCHED = True
        return
    # ResearchView.invalidateInstalled отсутствует в клиенте -> добавляем.
    # (У TechTree _data=NationTreeData без invalidateInstalled -> try/except внутри.)
    if not hasattr(ResearchView, 'invalidateInstalled'):
        def _rv_invalidateInstalled(self):
            data = getattr(self, '_data', None)
            if data is None or not hasattr(data, 'invalidateInstalled'):
                return
            try:
                from gui.Scaleform.daapi.view.lobby.techtree import NODE_STATE
                result = data.invalidateInstalled()
            except Exception:
                LOG_CURRENT_EXCEPTION()
                return
            if result:
                try:
                    self.as_setNodesStatesS(NODE_STATE.INSTALLED, result)
                except Exception:
                    LOG_CURRENT_EXCEPTION()
        ResearchView.invalidateInstalled = _rv_invalidateInstalled
    import weakref
    _orig_pop = ResearchView._populate
    _orig_disp = ResearchView._dispose

    def _p_pop(self):
        _orig_pop(self)
        try:
            _OPEN_TREE_VIEWS.append(weakref.ref(self))
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _p_disp(self):
        try:
            _OPEN_TREE_VIEWS[:] = [r for r in _OPEN_TREE_VIEWS
                                   if r() is not None and r() is not self]
        except Exception:
            LOG_CURRENT_EXCEPTION()
        _orig_disp(self)

    ResearchView._populate = _p_pop
    ResearchView._dispose = _p_disp
    ResearchView._offhangar_rvtrack = True
    _TREE_VIEW_PATCHED = True
    LOG_DEBUG('research view tracking + invalidateInstalled patch installed')


# ============================================================
#  Визуальное обновление текущего танка
# ============================================================

def _refreshCurrentVehicle(vehInvID, *args):
    try:
        from CurrentVehicle import g_currentVehicle
        if g_currentVehicle.invID != vehInvID:
            return
        g_currentVehicle.refreshModel()
        g_currentVehicle.onChanged()
    except Exception:
        LOG_CURRENT_EXCEPTION()

def _refreshCurrentVehicleData(vehInvID, *args):
    """Перечитать данные текущей машины в ангаре (снаряды, расходники) без перезагрузки 3D-модели.
    Без этого новые количество снарядов / ремкомплекты появлялись только после выбора другой машины и возврата."""
    try:
        from CurrentVehicle import g_currentVehicle
        if g_currentVehicle.invID != vehInvID:
            return
        g_currentVehicle.onChanged()
    except Exception:
        LOG_CURRENT_EXCEPTION()


_AMMO_EQS_FIELDS = ('shells', 'shellsLayout', 'eqs', 'eqsLayout')

# ============================================================
#  Главный коммит изменений инвентаря
# ============================================================

def _commitInventoryChanges(
        vehInvID=None, vehicleFields=None, simpleItems=None,
        newCompDescr=None, activeCamouflageKind=None,
        camouflageID=None, forceVisual=False):

    vehicleFields = dict(vehicleFields or {})
    simpleItems = dict(simpleItems or {})

    if newCompDescr is not None and vehInvID is not None:
        _OFFLINE_VEH_COMP_DESCR[vehInvID] = newCompDescr
        _OFFLINE_VEH_DESCR_CACHE.pop(vehInvID, None)
        vehicleFields['compDescr'] = newCompDescr

    if vehInvID is not None and vehicleFields:
        _updateOfflineVehicleFields(vehInvID, vehicleFields)

    if simpleItems:
        try:
            inv = _getOfflineInventoryRoot().setdefault('inventory', {})
            for itemTypeIdx, items in simpleItems.items():
                typeInv = inv.setdefault(itemTypeIdx, {})
                for intCD, count in items.items():
                    if count is None or count <= 0:
                        typeInv.pop(intCD, None)
                    else:
                        typeInv[intCD] = count
            _markInventoryDirty()
        except Exception:
            LOG_CURRENT_EXCEPTION()

    vehTypeIdx = ITEM_TYPE_INDICES['vehicle']
    invDiff = {}

    if vehInvID is not None and vehicleFields:
        vehDiff = {}
        for field, value in vehicleFields.items():
            vehDiff[field] = {vehInvID: value}
        invDiff[vehTypeIdx] = vehDiff

    for itemTypeIdx, items in simpleItems.items():
        diffItems = invDiff.setdefault(itemTypeIdx, {})
        for intCD, count in items.items():
            diffItems[intCD] = 0 if (count is None or count <= 0) else count

    if not invDiff:
        return

    try:
        BigWorld.player().inventory.synchronize(False, {'inventory': invDiff})
    except Exception:
        LOG_CURRENT_EXCEPTION()

    try:
        from gui.shared import g_itemsCache
        from gui.shared.ItemsCache import CACHE_SYNC_REASON
        patchItemsCacheGetVehicle(g_itemsCache)
        if vehInvID is not None:
            g_itemsCache.items.getVehicle(vehInvID)
        invalidItems = None
        try:
            invalidItems = g_itemsCache.items.invalidateCache({'inventory': invDiff})
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            g_itemsCache.onSyncCompleted(CACHE_SYNC_REASON.INVENTORY_RESYNC, invalidItems)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    except Exception:
        LOG_CURRENT_EXCEPTION()

    if activeCamouflageKind is not None and vehInvID is not None:
        try:
            from gui import g_tankActiveCamouflage
            from CurrentVehicle import g_currentVehicle
            if g_currentVehicle.invID == vehInvID and g_currentVehicle.isPresent():
                intCD = g_currentVehicle.item.intCD
                if camouflageID is not None and camouflageID > 0:
                    g_tankActiveCamouflage[intCD] = activeCamouflageKind
                elif camouflageID == 0 and intCD in g_tankActiveCamouflage:
                    del g_tankActiveCamouflage[intCD]
        except Exception:
            LOG_CURRENT_EXCEPTION()

    if forceVisual and vehInvID is not None:
        try:
            BigWorld.callback(0.1, functools.partial(_refreshCurrentVehicle, vehInvID))
        except Exception:
            LOG_CURRENT_EXCEPTION()
    elif vehInvID is not None and any(f in vehicleFields for f in _AMMO_EQS_FIELDS):
        # изменились снаряды / расходники: обновляем панель ангара
        try:
            BigWorld.callback(0.1, functools.partial(_refreshCurrentVehicleData, vehInvID))
        except Exception:
            LOG_CURRENT_EXCEPTION()


# ============================================================
#  Обработчики команд
# ============================================================
# requests.py — добавить рядом с остальными Potapov-обработчиками

def _cmdBuyPotapovQuestSlot(requestID, *args):
    try:
        LOG_DEBUG('CMD_BUY_POTAPOV_QUEST_SLOT raw args=%s' % (args,))
        data = _getOfflinePotapovData()
        currentSlots = int(data.get('slots', 0) or 0)

        price = None
        try:
            from gui.shared import g_itemsCache
            price = g_itemsCache.items.shop.getPotapovQuestSlotPrice(currentSlots)
        except Exception:
            pass
        if price is None:
            price = (0, 50)

        creditsCost, goldCost = int(price[0]), int(price[1])
        if not _hasFunds(creditsCost, goldCost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)

        if creditsCost or goldCost:
            _changeMoney(-creditsCost, -goldCost)

        data['slots'] = currentSlots + 1
        _saveOfflinePotapovData(data)
        _syncPotapovProgress({'slots': data['slots']})

        LOG_DEBUG('CMD_BUY_POTAPOV_QUEST_SLOT OK old=%s new=%s price=(%s,%s)' % (
            currentSlots, data['slots'], creditsCost, goldCost))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


def _cmdBuyPotapovQuestTile(requestID, *args):
    try:
        LOG_DEBUG('CMD_BUY_POTAPOV_QUEST_TILE raw args=%s' % (args,))
        if not args:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'no tileID', None)

        tileID = None
        for a in args:
            try:
                candidate = int(a)
                if candidate > 0:
                    tileID = candidate
                    break
            except Exception:
                continue
        if tileID is None:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad tileID', None)

        pq = _getPQ()
        tileInfo = pq.g_tileCache.getTileInfo(tileID)
        price = tileInfo.get('price', (0, 0))
        creditsCost, goldCost = int(price[0]), int(price[1])

        if not _hasFunds(creditsCost, goldCost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)

        data = _getOfflinePotapovData()
        qState = data.setdefault('_questsState', {})

        unlockedCount = 0
        for potapovQuestID, pqType in pq.g_cache:
            if pqType.tileID != tileID:
                continue
            if not pqType.isInitial:
                continue
            for reqID in pqType.requiredUnlocks:
                if reqID not in qState:
                    qState[reqID] = (0, pq.PQ_STATE.NONE)
            if potapovQuestID not in qState:
                qState[potapovQuestID] = (0, pq.PQ_STATE.NONE)
            unlockedCount += 1

        if unlockedCount == 0:
            return RequestResult(AccountCommands.RES_FAILURE, 'tile has no initial quests', None)

        if creditsCost or goldCost:
            _changeMoney(-creditsCost, -goldCost)

        _rebuildPotapovCompDescr(data)
        _saveOfflinePotapovData(data)
        _syncPotapovProgress({'compDescr': data['compDescr']})

        LOG_DEBUG('CMD_BUY_POTAPOV_QUEST_TILE OK tileID=%s price=(%s,%s) unlockedInitial=%s' % (
            tileID, creditsCost, goldCost, unlockedCount))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


try:
    baseRequest(getattr(AccountCommands, 'CMD_BUY_POTAPOV_QUEST_SLOT', -9246))(_cmdBuyPotapovQuestSlot)
    baseRequest(getattr(AccountCommands, 'CMD_BUY_POTAPOV_QUEST_TILE', -9157))(_cmdBuyPotapovQuestTile)
except Exception:
    LOG_CURRENT_EXCEPTION()

@baseRequest(getattr(AccountCommands, 'CMD_LOG_CLIENT_UX_EVENTS', -9009))
def logClientUxEvents(requestID, *args):
    return RequestResult(AccountCommands.RES_SUCCESS, '', {})


@baseRequest(getattr(AccountCommands, 'CMD_LOG_CLIENT_XMPP_EVENTS', -9295))
def logClientXmppEvents(requestID, *args):
    return RequestResult(AccountCommands.RES_SUCCESS, '', {})


_INT_SETTINGS = [None]          # настройки игры (маркеры, прицелы, управление...) - хранятся в файле кэша и переживают бой и перезапуск


def _intSettingsStore():
    st = _INT_SETTINGS[0]
    if st is None:
        try:
            from gui.mods.offhangar import data as off_data
            st = dict(off_data._loadOfflineCache('intsettings2') or {})
        except Exception:
            st = {}
        _INT_SETTINGS[0] = st
    return st


def _intSettingsFull():
    d = dict(_intSettingsStore())
    d.setdefault(0, 0)             # VERSION 0: клиент сам запишет настройки по умолчанию (в том числе галочки панелей игроков), потом поставит свою версию
    return d


def _saveIntSettings():
    try:
        from gui.mods.offhangar import data as off_data
        off_data._saveOfflineCache('intsettings2', dict(_intSettingsStore()))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _pushIntSettings(diff):
    """Как настоящая синхронизация: кэш IntUserSettings клиента + уведомление подписчиков (SettingsCache)."""
    try:
        p = BigWorld.player()
        p.intUserSettings.synchronize(False, {'intUserSettings': diff})
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from gui.ClientUpdateManager import g_clientUpdateManager
        g_clientUpdateManager.update({'intUserSettings': diff})
    except Exception:
        LOG_CURRENT_EXCEPTION()


@baseRequest(AccountCommands.CMD_ADD_INT_USER_SETTINGS)
def addIntUserSettings(requestID, arr):
    try:
        st = _intSettingsStore()
        diff = {}
        for i in range(0, len(arr) - 1, 2):
            k, v = int(arr[i]), int(arr[i + 1])
            if st.get(k) != v:
                st[k] = v
                diff[k] = v
        if diff:
            _saveIntSettings()
            _pushIntSettings(diff)
            LOG_DEBUG('[SETTINGS] saved %s game settings' % len(diff))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return RequestResult(AccountCommands.RES_SUCCESS, '', {})


@baseRequest(AccountCommands.CMD_DEL_INT_USER_SETTINGS)
def delIntUserSettings(requestID, arr):
    try:
        st = _intSettingsStore()
        diff = {}
        for k in arr:
            k = int(k)
            if k in st:
                del st[k]
                diff[k] = None
        if diff:
            _saveIntSettings()
            _pushIntSettings(diff)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return RequestResult(AccountCommands.RES_SUCCESS, '', {})


@baseRequest(AccountCommands.CMD_REQ_SERVER_STATS)
def serverStats(requestID, int1, int2, int3):
    BigWorld.player().receiveServerStats({
        'clusterCCU': 155000 * (1 - random.uniform(0.0, 0.15)),
        'regionCCU': 815000 * (1 - random.uniform(0.0, 0.15)),
    })
    return RequestResult(AccountCommands.RES_SUCCESS, '', None)


@baseRequest(AccountCommands.CMD_COMPLETE_TUTORIAL)
def completeTutorial(requestID, bonusID, vehInvID, _):
    # Клиент ждёт ОБЫЧНЫЙ ответ (callback из 3 аргументов): ext должен быть None,
    # иначе onCmdResponseExt вызовет callback с 4 аргументами -> TypeError, окно ожидания висит вечно.
    try:
        cache = _getStatsCache()
        done = int(cache.get('tutorialsCompleted', 0) or 0) | (1 << int(bonusID))
        cache['tutorialsCompleted'] = done
        try:
            from gui.ClientUpdateManager import g_clientUpdateManager
            g_clientUpdateManager.update({'stats': {'tutorialsCompleted': done}})
        except Exception:
            pass
        LOG_NOTE('[TUTOR] chapter bonus %s received (tutorialsCompleted=%s)' % (bonusID, done))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return RequestResult(AccountCommands.RES_SUCCESS, '', None)


@baseRequest(AccountCommands.CMD_SYNC_DATA)
def syncData(requestID, revision, crc, _):
    data = {'rev': revision + 2}
    if revision:
        data['prevRev'] = revision
    try:
        data.update(_getOfflineInventoryRoot())
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        stats = getOfflineStats()
        LOG_DEBUG('syncData: stats keys=%s credits=%s gold=%s freeXP=%s' % (
            stats.keys(), stats.get('stats', {}).get('credits'),
            stats.get('stats', {}).get('gold'), stats.get('stats', {}).get('freeXP')))
        data.update(stats)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        data.update(getOfflineQuestsProgress())
    except Exception:
        LOG_CURRENT_EXCEPTION()
    full = _intSettingsFull()
    data[('intUserSettings', '_r')] = dict(full)
    data['intUserSettings'] = dict(full)
    return RequestResult(AccountCommands.RES_SUCCESS, '', data)


@baseRequest(AccountCommands.CMD_VEH_SETTINGS)
def vehicleSettings(requestID, invID, flag, value):
    try:
        invID = int(invID)
        flag = int(flag)
        value = int(value)
        settings = int(_getOfflineVehicleField(invID, 'settings', 0) or 0)
        if value:
            settings |= flag
        else:
            settings &= ~flag
        _commitInventoryChanges(invID, {'settings': settings})
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_REPAIR)
def vehicleRepair(requestID, vehInvID, int2, int3):
    try:
        vehInvID = int(vehInvID)
        _commitInventoryChanges(vehInvID, {'repair': (0, 0)})
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

@baseRequest(getattr(AccountCommands, 'CMD_GET_AVATAR_SYNC', 1602))
def getAvatarSync(requestID, *args):
    return RequestResult(AccountCommands.RES_SUCCESS, '', {0: 999999})

@baseRequest(AccountCommands.CMD_EQUIP)
def vehicleEquip(requestID, vehInvID, itemCompDescr, gunCompDescr):
    try:
        vehInvID = _resolveVehInvID(vehInvID)
        itemCompDescr = int(itemCompDescr)
        gunCompDescr = int(gunCompDescr)
        LOG_DEBUG('CMD_EQUIP enter vehInvID=%s item=%s gun=%s' % (
            vehInvID, itemCompDescr, gunCompDescr))
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            LOG_DEBUG('CMD_EQUIP FAIL: no compDescr for vehInvID=%s' % vehInvID)
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        oldTurretCD = descr.turret['compactDescr']
        oldGunCD = descr.gun['compactDescr']
        itemTypeID = vehicles.parseIntCompactDescr(itemCompDescr)[0]
        if itemTypeID == ITEM_TYPE_INDICES['vehicleTurret']:
            descr.installTurret(itemCompDescr, gunCompDescr)
        else:
            descr.installComponent(itemCompDescr)
        newCompDescr = descr.makeCompactDescr()
        fields = {}
        _maybeUpdateShellsAfterGunChange(vehInvID, descr, oldTurretCD, oldGunCD, fields)
        simpleItems = {}
        _ensureSimpleItem(simpleItems, itemTypeID, itemCompDescr, 1)
        if itemTypeID == ITEM_TYPE_INDICES['vehicleTurret'] and gunCompDescr > 0:
            _ensureSimpleItem(simpleItems, ITEM_TYPE_INDICES['vehicleGun'], gunCompDescr, 1)
        LOG_DEBUG('CMD_EQUIP vehInvID=%s itemType=%s newLen=%s' % (
            vehInvID, itemTypeID, _cdLen(newCompDescr)))
        _commitInventoryChanges(vehInvID, fields, simpleItems, newCompDescr, forceVisual=True)
        try:
            _forceTreeUpdate(vehicles.getVehicleTypeCompactDescr(newCompDescr), doInstalled=True)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_EQUIP_OPTDEV)
def vehicleEquipOptionalDevice(requestID, arr):
    try:
        if not arr or len(arr) < 5:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_EQUIP_OPTDEV: bad args', None)
        shopRev, vehInvID, deviceCompDescr, slotIdx, isPaidRemoval = arr[:5]
        vehInvID = _resolveVehInvID(vehInvID)
        deviceCompDescr = int(deviceCompDescr)
        slotIdx = int(slotIdx)
        isPaidRemoval = int(isPaidRemoval)
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        simpleItems = {}
        if deviceCompDescr <= 0:
            if 0 <= slotIdx < vehicles.NUM_OPTIONAL_DEVICE_SLOTS:
                oldDevice = descr.optionalDevices[slotIdx]
                if isPaidRemoval:
                    if not _hasFunds(0, PAID_REMOVAL_GOLD_COST):
                        return RequestResult(AccountCommands.RES_FAILURE, 'not enough gold', None)
                    _changeMoney(0, -PAID_REMOVAL_GOLD_COST)
                descr.removeOptionalDevice(slotIdx)
                if oldDevice is not None:
                    _addSimpleItemCount(simpleItems, ITEM_TYPE_INDICES['optionalDevice'],
                                        oldDevice['compactDescr'], 1)
        else:
            if slotIdx < 0 or slotIdx >= vehicles.NUM_OPTIONAL_DEVICE_SLOTS:
                slotIdx = 0
                for i, dev in enumerate(descr.optionalDevices):
                    if dev is None:
                        slotIdx = i
                        break
            oldDevice = None
            if 0 <= slotIdx < vehicles.NUM_OPTIONAL_DEVICE_SLOTS:
                oldDevice = descr.optionalDevices[slotIdx]
            oldDeviceCD = oldDevice['compactDescr'] if oldDevice is not None else 0
            if oldDeviceCD == deviceCompDescr:
                return RequestResult(AccountCommands.RES_SUCCESS, '', None)
            if oldDeviceCD > 0:
                if isPaidRemoval:
                    if not _hasFunds(0, PAID_REMOVAL_GOLD_COST):
                        return RequestResult(AccountCommands.RES_FAILURE, 'not enough gold', None)
                    _changeMoney(0, -PAID_REMOVAL_GOLD_COST)
                _addSimpleItemCount(simpleItems, ITEM_TYPE_INDICES['optionalDevice'], oldDeviceCD, 1)
            if not _consumeSimpleItem(simpleItems, ITEM_TYPE_INDICES['optionalDevice'], deviceCompDescr, 1):
                if not _chargeItem(deviceCompDescr, 1):
                    return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
            descr.installOptionalDevice(deviceCompDescr, slotIdx)
        newCompDescr = descr.makeCompactDescr()
        LOG_DEBUG('CMD_EQUIP_OPTDEV vehInvID=%s device=%s slot=%s newLen=%s' % (
            vehInvID, deviceCompDescr, slotIdx, _cdLen(newCompDescr)))
        _commitInventoryChanges(vehInvID, {}, simpleItems, newCompDescr, forceVisual=True)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_EQUIP_SHELLS)
def vehicleEquipShells(requestID, arr):
    try:
        if not arr:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_EQUIP_SHELLS: bad args', None)
        vehInvID = _resolveVehInvID(arr[0])
        shells = [int(x) for x in arr[1:]]
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        key = (descr.turret['compactDescr'], descr.gun['compactDescr'])
        oldShells = list(_getOfflineVehicleField(vehInvID, 'shells', []) or [])
        shellCDs = _getShellCompDescrsForGun(descr.gun)
        totalCreditsCost = 0
        totalGoldCost = 0
        deltas = []
        for i in range(len(shells)):
            newCount = shells[i]
            oldCount = oldShells[i] if i < len(oldShells) else 0
            delta = newCount - oldCount
            deltas.append(delta)
            if delta > 0 and i < len(shellCDs):
                price = g_itemsCache_items_shop_getPrice(shellCDs[i])
                if price is not None:
                    totalCreditsCost += int(price[0]) * delta
                    totalGoldCost += int(price[1]) * delta
        if not _hasFunds(totalCreditsCost, totalGoldCost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
        _changeMoney(-totalCreditsCost, -totalGoldCost)
        for i, delta in enumerate(deltas):
            if delta < 0 and i < len(shellCDs):
                _refundItem(shellCDs[i], -delta)
        layouts = dict(_getOfflineVehicleField(vehInvID, 'shellsLayout', {}) or {})
        layouts[key] = shells
        fields = {'shells': shells, 'shellsLayout': layouts}
        LOG_DEBUG('CMD_EQUIP_SHELLS vehInvID=%s shellsCount=%s' % (vehInvID, len(shells)))
        _commitInventoryChanges(vehInvID, fields)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_EQUIP_EQS)
def vehicleEquipEquipments(requestID, arr):
    try:
        if not arr:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_EQUIP_EQS: bad args', None)
        vehInvID = _resolveVehInvID(arr[0])
        eqs = [int(x) for x in arr[1:]]
        while len(eqs) < 3:
            eqs.append(0)
        eqs = eqs[:3]
        oldEqs = list(_getOfflineVehicleField(vehInvID, 'eqs', [0, 0, 0]) or [0, 0, 0])
        while len(oldEqs) < 3:
            oldEqs.append(0)
        oldEqs = oldEqs[:3]
        simpleItems = {}
        totalCreditsCost = 0
        totalGoldCost = 0
        for i in range(3):
            oldCD = oldEqs[i]
            newCD = eqs[i]
            if newCD == oldCD:
                continue
            if oldCD > 0:
                _addSimpleItemCount(simpleItems, ITEM_TYPE_INDICES['equipment'], oldCD, 1)
            if newCD > 0:
                if not _consumeSimpleItem(simpleItems, ITEM_TYPE_INDICES['equipment'], newCD, 1):
                    price = g_itemsCache_items_shop_getPrice(newCD)
                    if price is not None:
                        totalCreditsCost += int(price[0])
                        totalGoldCost += int(price[1])
        if not _hasFunds(totalCreditsCost, totalGoldCost):
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
        if totalCreditsCost > 0 or totalGoldCost > 0:
            _changeMoney(-totalCreditsCost, -totalGoldCost)
        fields = {'eqs': eqs, 'eqsLayout': eqs}
        LOG_DEBUG('CMD_EQUIP_EQS vehInvID=%s eqs=%s' % (vehInvID, eqs))
        _commitInventoryChanges(vehInvID, fields, simpleItems)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_BUY_AND_EQUIP_ITEM)
def buyAndEquipItem(requestID, arr):
    try:
        if not arr or len(arr) < 6:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_BUY_AND_EQUIP_ITEM: bad args', None)
        shopRev, compDescr, vehInvID, slotIdx, isPaidRemoval, gunCompDescr = arr[:6]
        compDescr = int(compDescr)
        vehInvID = _resolveVehInvID(vehInvID)
        slotIdx = int(slotIdx)
        isPaidRemoval = int(isPaidRemoval)
        gunCompDescr = int(gunCompDescr)
        LOG_DEBUG('CMD_BUY_AND_EQUIP_ITEM enter vehInvID=%s compDescr=%s slot=%s gun=%s paid=%s' % (
            vehInvID, compDescr, slotIdx, gunCompDescr, isPaidRemoval))
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            LOG_DEBUG('CMD_BUY_AND_EQUIP_ITEM FAIL: no compDescr for vehInvID=%s' % vehInvID)
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        itemTypeID = vehicles.parseIntCompactDescr(compDescr)[0]
        simpleItems = {}
        totalCreditsCost = 0
        totalGoldCost = 0

        def _getPendingCount(typeIdx, intCD):
            if intCD is None:
                return 0
            intCD = int(intCD)
            if intCD <= 0:
                return 0
            pending = simpleItems.get(typeIdx, {}).get(intCD)
            if pending is not None:
                return max(0, int(pending))
            try:
                return int(_getOfflineInventoryRoot().get('inventory', {}).get(typeIdx, {}).get(intCD, 0) or 0)
            except Exception:
                LOG_CURRENT_EXCEPTION()
                return 0

        def _setPendingCount(typeIdx, intCD, count):
            if intCD is None:
                return
            intCD = int(intCD)
            if intCD <= 0:
                return
            simpleItems.setdefault(typeIdx, {})[intCD] = max(0, int(count))

        def _addPending(typeIdx, intCD, count=1):
            if intCD is None:
                return
            intCD = int(intCD)
            if intCD <= 0:
                return
            _setPendingCount(typeIdx, intCD, _getPendingCount(typeIdx, intCD) + int(count))

        def _takeOrPrice(typeIdx, intCD):
            if intCD is None:
                return (0, 0)
            intCD = int(intCD)
            if intCD <= 0:
                return (0, 0)
            existing = _getPendingCount(typeIdx, intCD)
            if existing > 0:
                _setPendingCount(typeIdx, intCD, existing - 1)
                return (0, 0)
            price = g_itemsCache_items_shop_getPrice(intCD)
            if price is None:
                return (0, 0)
            return (int(price[0]), int(price[1]))

        fields = {}
        newCompDescr = None
        forceVisual = False

        if itemTypeID == ITEM_TYPE_INDICES['optionalDevice']:
            if slotIdx < 0 or slotIdx >= vehicles.NUM_OPTIONAL_DEVICE_SLOTS:
                slotIdx = 0
                for i, dev in enumerate(descr.optionalDevices):
                    if dev is None:
                        slotIdx = i
                        break
            oldDevice = None
            if 0 <= slotIdx < vehicles.NUM_OPTIONAL_DEVICE_SLOTS:
                oldDevice = descr.optionalDevices[slotIdx]
            oldDeviceCD = oldDevice['compactDescr'] if oldDevice is not None else 0
            if oldDeviceCD == compDescr:
                return RequestResult(AccountCommands.RES_SUCCESS, '', None)
            if oldDeviceCD > 0:
                if isPaidRemoval:
                    totalGoldCost += PAID_REMOVAL_GOLD_COST
                _addPending(ITEM_TYPE_INDICES['optionalDevice'], oldDeviceCD, 1)
            creditsCost, goldCost = _takeOrPrice(ITEM_TYPE_INDICES['optionalDevice'], compDescr)
            totalCreditsCost += creditsCost
            totalGoldCost += goldCost
            descr.installOptionalDevice(compDescr, slotIdx)
            newCompDescr = descr.makeCompactDescr()
            forceVisual = True

        elif itemTypeID == ITEM_TYPE_INDICES['equipment']:
            eqs = list(_getOfflineVehicleField(vehInvID, 'eqs', [0, 0, 0]) or [0, 0, 0])
            while len(eqs) < 3:
                eqs.append(0)
            eqs = eqs[:3]
            if slotIdx < 0 or slotIdx >= 3:
                slotIdx = 0
                for i, eq in enumerate(eqs):
                    if eq == 0:
                        slotIdx = i
                        break
            oldEq = eqs[slotIdx]
            if oldEq == compDescr:
                return RequestResult(AccountCommands.RES_SUCCESS, '', None)
            if oldEq > 0:
                _addPending(ITEM_TYPE_INDICES['equipment'], oldEq, 1)
            creditsCost, goldCost = _takeOrPrice(ITEM_TYPE_INDICES['equipment'], compDescr)
            totalCreditsCost += creditsCost
            totalGoldCost += goldCost
            eqs[slotIdx] = compDescr
            fields = {'eqs': eqs, 'eqsLayout': eqs}

        elif itemTypeID == ITEM_TYPE_INDICES['vehicleTurret']:
            oldTurretCD = descr.turret['compactDescr']
            oldGunCD = descr.gun['compactDescr']
            if gunCompDescr <= 0:
                gunCompDescr = oldGunCD
            if oldTurretCD == compDescr and oldGunCD == gunCompDescr:
                return RequestResult(AccountCommands.RES_SUCCESS, '', None)
            if oldTurretCD > 0 and oldTurretCD != compDescr:
                _addPending(ITEM_TYPE_INDICES['vehicleTurret'], oldTurretCD, 1)
            if oldGunCD > 0 and oldGunCD != gunCompDescr:
                _addPending(ITEM_TYPE_INDICES['vehicleGun'], oldGunCD, 1)
            if oldTurretCD != compDescr:
                creditsCost, goldCost = _takeOrPrice(ITEM_TYPE_INDICES['vehicleTurret'], compDescr)
                totalCreditsCost += creditsCost
                totalGoldCost += goldCost
            if gunCompDescr > 0 and oldGunCD != gunCompDescr:
                creditsCost, goldCost = _takeOrPrice(ITEM_TYPE_INDICES['vehicleGun'], gunCompDescr)
                totalCreditsCost += creditsCost
                totalGoldCost += goldCost
            descr.installTurret(compDescr, gunCompDescr)
            newCompDescr = descr.makeCompactDescr()
            _maybeUpdateShellsAfterGunChange(vehInvID, descr, oldTurretCD, oldGunCD, fields)
            forceVisual = True

        elif itemTypeID in (ITEM_TYPE_INDICES['vehicleChassis'], ITEM_TYPE_INDICES['vehicleGun'],
                            ITEM_TYPE_INDICES['vehicleEngine'], ITEM_TYPE_INDICES['vehicleFuelTank'],
                            ITEM_TYPE_INDICES['vehicleRadio']):
            moduleField = {
                ITEM_TYPE_INDICES['vehicleChassis']: 'chassis',
                ITEM_TYPE_INDICES['vehicleGun']: 'gun',
                ITEM_TYPE_INDICES['vehicleEngine']: 'engine',
                ITEM_TYPE_INDICES['vehicleFuelTank']: 'fuelTank',
                ITEM_TYPE_INDICES['vehicleRadio']: 'radio',
            }.get(itemTypeID)
            oldModuleCD = 0
            try:
                if moduleField is not None:
                    oldModuleCD = getattr(descr, moduleField)['compactDescr']
            except Exception:
                LOG_CURRENT_EXCEPTION()
            if oldModuleCD == compDescr:
                return RequestResult(AccountCommands.RES_SUCCESS, '', None)
            if oldModuleCD > 0 and oldModuleCD != compDescr:
                _addPending(itemTypeID, oldModuleCD, 1)
            creditsCost, goldCost = _takeOrPrice(itemTypeID, compDescr)
            totalCreditsCost += creditsCost
            totalGoldCost += goldCost
            oldTurretCD = descr.turret['compactDescr']
            oldGunCD = descr.gun['compactDescr']
            descr.installComponent(compDescr)
            newCompDescr = descr.makeCompactDescr()
            _maybeUpdateShellsAfterGunChange(vehInvID, descr, oldTurretCD, oldGunCD, fields)
            forceVisual = True

        else:
            LOG_DEBUG('CMD_BUY_AND_EQUIP_ITEM FAIL: unsupported itemType=%s' % itemTypeID)
            return RequestResult(AccountCommands.RES_FAILURE, 'unsupported item type', None)

        if not _hasFunds(totalCreditsCost, totalGoldCost):
            LOG_DEBUG('CMD_BUY_AND_EQUIP_ITEM FAIL: not enough money cost=(%s,%s)' % (
                totalCreditsCost, totalGoldCost))
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
        if totalCreditsCost > 0 or totalGoldCost > 0:
            _changeMoney(-totalCreditsCost, -totalGoldCost)
        LOG_DEBUG('CMD_BUY_AND_EQUIP_ITEM OK vehInvID=%s itemType=%s item=%s gun=%s cost=(%s,%s) newLen=%s' % (
            vehInvID, itemTypeID, compDescr, gunCompDescr,
            totalCreditsCost, totalGoldCost,
            _cdLen(newCompDescr) if newCompDescr is not None else -1))
        _commitInventoryChanges(vehInvID, fields, simpleItems, newCompDescr, forceVisual=forceVisual)
        if newCompDescr is not None:
            try:
                _forceTreeUpdate(vehicles.getVehicleTypeCompactDescr(newCompDescr), doInstalled=True)
            except Exception:
                LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

@baseRequest(AccountCommands.CMD_BUY_VEHICLE)
def buyVehicle(requestID, *args):
    try:
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            arr = list(args[0])
        else:
            arr = list(args)
        if len(arr) < 4:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, '', None)
        shopRev, typeCompDescr, flags, tmanCostTypeIdx = arr[:4]
        rentPeriod = arr[4] if len(arr) > 4 else 0
        typeCompDescr = int(typeCompDescr)
        price = g_itemsCache_items_shop_getPrice(typeCompDescr)
        if price is None:
            return RequestResult(AccountCommands.RES_FAILURE, '', None)
        creditsCost, goldCost = price
        cache = _getStatsCache()
        if goldCost > 0 and int(cache.get('gold', 0)) < goldCost:
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough gold', None)
        if creditsCost > 0 and int(cache.get('credits', 0)) < creditsCost:
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough credits', None)
        _changeMoney(-creditsCost, -goldCost)

        newInvID, crewIDs = _addVehicleToInventory(typeCompDescr)
        if newInvID is None:
            _changeMoney(creditsCost, goldCost)
            return RequestResult(AccountCommands.RES_FAILURE, '', None)

        newCompDescr = _getOfflineVehicleField(newInvID, 'compDescr')
        crewList = _getOfflineVehicleField(newInvID, 'crew', [])
        vehTypeIdx = ITEM_TYPE_INDICES['vehicle']
        tmanTypeIdx = ITEM_TYPE_INDICES['tankman']
        root = _getOfflineInventoryRoot()
        tmanCD = root.get('inventory', {}).get(tmanTypeIdx, {}).get('compDescr', {})

        invDiff = {
            vehTypeIdx: {
                'compDescr': {newInvID: newCompDescr},
                'crew': {newInvID: crewList},
                'settings': {newInvID: 0},
                'eqs': {newInvID: []},
                'eqsLayout': {newInvID: []},
                'shells': {newInvID: _getOfflineVehicleField(newInvID, 'shells', [])},
                'repair': {newInvID: (0, 0)},
                'lock': {newInvID: 0},
            },
            tmanTypeIdx: {
                'vehicle': dict((tid, newInvID) for tid in crewIDs),
                'compDescr': dict((tid, tmanCD.get(tid)) for tid in crewIDs),
            },
        }

        try:
            BigWorld.player().inventory.synchronize(False, {'inventory': invDiff})
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            from gui.shared import g_itemsCache
            from gui.shared.ItemsCache import CACHE_SYNC_REASON
            invCache = BigWorld.player().inventory._Inventory__cache
            g_itemsCache.items.inventory._response(AccountCommands.RES_SUCCESS, invCache, lambda *a: None)
            invalidItems = g_itemsCache.items.invalidateCache({'inventory': invDiff})
            g_itemsCache.onSyncCompleted(CACHE_SYNC_REASON.INVENTORY_RESYNC, invalidItems)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            from gui.ClientUpdateManager import g_clientUpdateManager
            g_clientUpdateManager.update({'inventory': invDiff})
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            from PlayerEvents import g_playerEvents
            g_playerEvents.onInventoryResync()
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            _forceTreeUpdate(typeCompDescr, doInstalled=False, doInventory=True)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        LOG_DEBUG('CMD_BUY_VEHICLE type=%s invID=%s price=(%s,%s) crew=%s' % (
            typeCompDescr, newInvID, creditsCost, goldCost, crewIDs))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

@baseRequest(AccountCommands.CMD_SELL_VEHICLE)
def sellVehicle(requestID, arr):
    try:
        if not arr or len(arr) < 4:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, '', None)
        shopRev = int(arr[0])
        vehInvID = int(arr[1])
        flags = int(arr[2])
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, '', None)
        vehTypeCompDescr = vehicles.getVehicleTypeCompactDescr(oldCompDescr)
        sellPrice = (0, 0)
        try:
            from gui.shared import g_itemsCache
            vi = g_itemsCache.items.getItemByCD(vehTypeCompDescr)
            if vi is not None:
                sellPrice = vi.sellPrice
        except Exception:
            pass
        _removeVehicleFromOfflineInventory(vehInvID)
        try:
            invCache = BigWorld.player().inventory._Inventory__cache
            vehCache = invCache.get(ITEM_TYPE_INDICES['vehicle'], {})
            for f in ('compDescr', 'crew', 'settings', 'eqs', 'eqsLayout',
                      'shells', 'shellsLayout', 'repair', 'lock',
                      'customizationExpiryTime', 'lastCrew'):
                vehCache.get(f, {}).pop(vehInvID, None)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            from gui.shared import g_itemsCache
            from gui.shared.gui_items import GUI_ITEM_TYPE
            invReq = g_itemsCache.items.inventory
            invReq._InventoryRequester__vehsCDsByID.pop(vehInvID, None)
            invReq._InventoryRequester__vehsIDsByCD.pop(vehTypeCompDescr, None)
            invReq._InventoryRequester__itemsCache.get(GUI_ITEM_TYPE.VEHICLE, {}).pop(vehTypeCompDescr, None)
            invCache = BigWorld.player().inventory._Inventory__cache
            invReq._response(AccountCommands.RES_SUCCESS, invCache, lambda *a: None)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        _changeMoney(sellPrice[0], sellPrice[1])
        try:
            from gui.ClientUpdateManager import g_clientUpdateManager
            g_clientUpdateManager.update({
                'inventory': {ITEM_TYPE_INDICES['vehicle']: {'compDescr': {vehInvID: None}}}
            })
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            from gui.shared import g_itemsCache
            from gui.shared.ItemsCache import CACHE_SYNC_REASON
            from gui.shared.gui_items import GUI_ITEM_TYPE
            g_itemsCache.items._ItemsRequester__itemsCache.get(GUI_ITEM_TYPE.VEHICLE, {}).pop(vehTypeCompDescr, None)
            diff = {GUI_ITEM_TYPE.VEHICLE: set([vehTypeCompDescr])}
            g_itemsCache.onSyncCompleted(CACHE_SYNC_REASON.INVENTORY_RESYNC, diff)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            from PlayerEvents import g_playerEvents
            g_playerEvents.onInventoryResync()
        except Exception:
            LOG_CURRENT_EXCEPTION()
        try:
            _forceTreeUpdate(vehTypeCompDescr, doInstalled=False, doInventory=True)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        LOG_DEBUG('CMD_SELL_VEHICLE invID=%s type=%s gain=(%s,%s)' % (
            vehInvID, vehTypeCompDescr, sellPrice[0], sellPrice[1]))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

# requests.py — заменить блок целиком

_pq = None

def _getPQ():
    global _pq
    if _pq is None:
        import potapov_quests
        _pq = potapov_quests
    return _pq


def _getOfflinePotapovData():
    stored = off_data._loadOfflineCache('quests')
    if stored is None:
        stored = off_data.getOfflineQuestsProgress()
    stored = cPickle.loads(cPickle.dumps(stored))
    return stored.setdefault('potapovQuests', {'compDescr': '', 'slots': 3, 'selected': set(), 'rewards': {}, 'lastIDs': {}})


def _saveOfflinePotapovData(data):
    stored = off_data._loadOfflineCache('quests')
    if stored is None:
        stored = off_data.getOfflineQuestsProgress()
    stored = cPickle.loads(cPickle.dumps(stored))
    stored['potapovQuests'] = data
    off_data._saveOfflineCache('quests', stored)


def _rebuildPotapovCompDescr(data):
    pq = _getPQ()
    storage = pq.PQStorage(storage=dict(data.get('_questsState', {})))
    data['compDescr'] = storage.makeCompDescr()


def _syncPotapovProgress(fields):
    try:
        rev = int(BigWorld.player().syncData.revision or 0)
    except Exception:
        rev = 0
    diff = {'prevRev': rev, 'rev': rev + 1, 'potapovQuests': fields}
    try:
        BigWorld.player().questProgress.synchronize(False, diff)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from gui.ClientUpdateManager import g_clientUpdateManager
        g_clientUpdateManager.update({'quests': {}})
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _cmdSelectPotapovQuests(requestID, arr):
    try:
        ids = set(int(x) for x in arr)
        data = _getOfflinePotapovData()
        if len(ids) > int(data.get('slots', 0) or 0):
            return RequestResult(AccountCommands.RES_FAILURE, 'no_free_slots', None)
        data['selected'] = ids
        _saveOfflinePotapovData(data)
        _syncPotapovProgress({'selected': ids})
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


def _cmdGetPotapovQuestReward(requestID, arr):
    try:
        pq = _getPQ()
        potapovQuestID, needTankman, tmanNation, tmanInnation, roleID = [int(x) for x in arr[:5]]
        pqType = pq.g_cache.questByPotapovQuestID(potapovQuestID)
        data = _getOfflinePotapovData()
        qState = data.setdefault('_questsState', {})
        flags, state = qState.get(potapovQuestID, (0, pq.PQ_STATE.NONE))

        totalCredits = totalGold = totalFreeXP = 0
        for section in (pqType.mainQuestInfo, pqType.addQuestInfo):
            bonus = section.get('bonus', {}) or {}
            totalCredits += int(bonus.get('credits', 0) or 0)
            totalGold += int(bonus.get('gold', 0) or 0)
            totalFreeXP += int(bonus.get('freeXP', 0) or 0)

        if totalCredits or totalGold:
            _changeMoney(totalCredits, totalGold)
        if totalFreeXP:
            cache = _getStatsCache()
            cache['freeXP'] = int(cache.get('freeXP', 0)) + totalFreeXP
            _saveStatsToOfflineCache()
            _fireStatsResync()

        qState[potapovQuestID] = (flags, pq.PQ_STATE.ALL_REWARDS_GOTTEN)
        selected = set(data.get('selected') or ())
        selected.discard(potapovQuestID)
        data['selected'] = selected
        _rebuildPotapovCompDescr(data)
        _saveOfflinePotapovData(data)
        _syncPotapovProgress({'compDescr': data['compDescr'], 'selected': data['selected']})
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


try:
    baseRequest(getattr(AccountCommands, 'CMD_SELECT_POTAPOV_QUESTS', -9407))(_cmdSelectPotapovQuests)
    baseRequest(getattr(AccountCommands, 'CMD_GET_POTAPOV_QUEST_REWARD', -9693))(_cmdGetPotapovQuestReward)
except Exception:
    LOG_CURRENT_EXCEPTION()

@baseRequest(AccountCommands.CMD_SET_AND_FILL_LAYOUTS)
def vehicleSetAndFillLayouts(requestID, arr):
    try:
        if not arr or len(arr) < 3:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_SET_AND_FILL_LAYOUTS: bad args', None)
        shopRev = int(arr[0])
        vehInvID = _resolveVehInvID(arr[1])
        shellsLayoutLen = int(arr[2])
        pos = 3
        shellsLayout = []
        if shellsLayoutLen > 0:
            shellsLayout = [int(x) for x in arr[pos:pos + shellsLayoutLen]]
            pos += shellsLayoutLen
        eqsLayout = []
        if len(arr) > pos:
            eqsLayoutLen = int(arr[pos])
            pos += 1
            if eqsLayoutLen > 0:
                eqsLayout = [int(x) for x in arr[pos:pos + eqsLayoutLen]]
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        fields = {}
        if shellsLayout:
            descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
            key = (descr.turret['compactDescr'], descr.gun['compactDescr'])
            layouts = dict(_getOfflineVehicleField(vehInvID, 'shellsLayout', {}) or {})
            layouts[key] = shellsLayout
            fields['shells'] = shellsLayout
            fields['shellsLayout'] = layouts
        if eqsLayout:
            eqs = [0, 0, 0]
            for i in range(0, min(len(eqsLayout), 6), 2):
                eqCD = int(eqsLayout[i])
                count = int(eqsLayout[i + 1]) if i + 1 < len(eqsLayout) else 0
                eqs[i / 2] = eqCD if count > 0 else 0
            fields['eqs'] = eqs
            fields['eqsLayout'] = eqs
        if not fields:
            return RequestResult(AccountCommands.RES_SUCCESS, '', None)
        LOG_DEBUG('CMD_SET_AND_FILL_LAYOUTS vehInvID=%s shellsLen=%s eqsLen=%s' % (
            vehInvID, len(shellsLayout), len(eqsLayout)))
        _commitInventoryChanges(vehInvID, fields)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_BUY_ITEM)
def buyItem(requestID, shopRev, itemShopID, count, goldForCredits):
    try:
        itemShopID = int(itemShopID)
        count = max(1, int(count))
        itemTypeID = vehicles.parseIntCompactDescr(itemShopID)[0]
        if itemTypeID == ITEM_TYPE_INDICES['vehicle']:
            return RequestResult(AccountCommands.RES_SUCCESS, '', None)
        price = g_itemsCache_items_shop_getPrice(itemShopID)
        if price is not None:
            creditsCost = int(price[0]) * count
            goldCost = int(price[1]) * count
            if not _hasFunds(creditsCost, goldCost):
                return RequestResult(AccountCommands.RES_FAILURE, 'not enough money', None)
            _changeMoney(-creditsCost, -goldCost)
        simpleItems = {}
        _addSimpleItem(simpleItems, itemTypeID, itemShopID, count)
        LOG_DEBUG('CMD_BUY_ITEM item=%s count=%s' % (itemShopID, count))
        _commitInventoryChanges(simpleItems=simpleItems)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_SELL_ITEM)
def sellItem(requestID, shopRev, itemTypeIdx, itemInvID, count):
    try:
        itemTypeIdx = int(itemTypeIdx)
        itemInvID = int(itemInvID)
        count = int(count)
        if itemTypeIdx == ITEM_TYPE_INDICES['vehicle']:
            return RequestResult(AccountCommands.RES_SUCCESS, '', None)
        existing = 0
        try:
            existing = int(
                _getOfflineInventoryRoot().get('inventory', {}).get(itemTypeIdx, {}).get(itemInvID, 0) or 0
            )
        except Exception:
            LOG_CURRENT_EXCEPTION()
        newCount = max(0, existing - count)
        soldCount = existing - newCount
        if soldCount > 0:
            _refundItem(itemInvID, soldCount)
        simpleItems = {itemTypeIdx: {itemInvID: newCount}}
        LOG_DEBUG('CMD_SELL_ITEM type=%s item=%s count=%s newCount=%s' % (
            itemTypeIdx, itemInvID, count, newCount))
        _commitInventoryChanges(simpleItems=simpleItems)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_VEH_CAMOUFLAGE)
def vehicleCamouflage(requestID, arr):
    try:
        if not arr or len(arr) < 5:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_VEH_CAMOUFLAGE: bad args', None)
        shopRev, vehInvID, camouflageKind, camouflageID, periodDays = arr[:5]
        vehInvID = int(vehInvID)
        camouflageKind = int(camouflageKind)
        camouflageID = int(camouflageID)
        periodDays = max(0, int(periodDays))
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        if camouflageID <= 0:
            descr.setCamouflage(camouflageKind, None, 0, 0)
        else:
            descr.setCamouflage(camouflageKind, camouflageID, int(time.time()), periodDays)
        newCompDescr = descr.makeCompactDescr()
        LOG_DEBUG('CMD_VEH_CAMOUFLAGE vehInvID=%s kind=%s camoID=%s days=%s newLen=%s' % (
            vehInvID, camouflageKind, camouflageID, periodDays, _cdLen(newCompDescr)))
        _commitInventoryChanges(vehInvID, {}, None, newCompDescr,
                                activeCamouflageKind=camouflageKind,
                                camouflageID=camouflageID, forceVisual=True)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_VEH_EMBLEM)
def vehicleEmblem(requestID, arr):
    try:
        if not arr or len(arr) < 5:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_VEH_EMBLEM: bad args', None)
        shopRev, vehInvID, position, emblemID, periodDays = arr[:5]
        vehInvID = int(vehInvID)
        position = int(position)
        emblemID = int(emblemID)
        periodDays = max(0, int(periodDays))
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        if emblemID <= 0:
            descr.setPlayerEmblem(position, None, 0, 0)
        else:
            descr.setPlayerEmblem(position, emblemID, int(time.time()), periodDays)
        newCompDescr = descr.makeCompactDescr()
        LOG_DEBUG('CMD_VEH_EMBLEM vehInvID=%s pos=%s emblemID=%s newLen=%s' % (
            vehInvID, position, emblemID, _cdLen(newCompDescr)))
        _commitInventoryChanges(vehInvID, {}, None, newCompDescr, forceVisual=True)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

@baseRequest(AccountCommands.CMD_EXCHANGE)
def exchangeGold(requestID, shopRev, gold, _int3):
    try:
        gold = int(round(float(gold)))

        if gold <= 0:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'bad gold', None)

        cache = _getStatsCache()
        currentGold = int(cache.get('gold', 0))

        if currentGold < gold:
            return RequestResult(AccountCommands.RES_FAILURE, 'not enough gold', None)

        rate = 400

        try:
            from gui.shared import g_itemsCache
            rate = int(g_itemsCache.items.shop.exchangeRate or 400)
        except Exception:
            LOG_CURRENT_EXCEPTION()

        creditsGain = gold * rate

        _changeMoney(creditsGain, -gold)

        try:
            _fireStatsResync()
        except Exception:
            LOG_CURRENT_EXCEPTION()

        LOG_DEBUG('CMD_EXCHANGE OK gold=%s rate=%s creditsGain=%s' % (
            gold, rate, creditsGain))

        return RequestResult(AccountCommands.RES_SUCCESS, '', None)

    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_VEH_INSCRIPTION)
def vehicleInscription(requestID, arr):
    try:
        if not arr or len(arr) < 6:
            return RequestResult(AccountCommands.RES_WRONG_ARGS, 'CMD_VEH_INSCRIPTION: bad args', None)
        shopRev, vehInvID, position, inscriptionID, periodDays, colorID = arr[:6]
        vehInvID = int(vehInvID)
        position = int(position)
        inscriptionID = int(inscriptionID)
        periodDays = max(0, int(periodDays))
        colorID = max(0, min(15, int(colorID)))
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        if inscriptionID <= 0:
            descr.setPlayerInscription(position, None, 0, 0, 0)
        else:
            descr.setPlayerInscription(position, inscriptionID, int(time.time()), periodDays, colorID)
        newCompDescr = descr.makeCompactDescr()
        LOG_DEBUG('CMD_VEH_INSCRIPTION vehInvID=%s pos=%s inscrID=%s newLen=%s' % (
            vehInvID, position, inscriptionID, _cdLen(newCompDescr)))
        _commitInventoryChanges(vehInvID, {}, None, newCompDescr, forceVisual=True)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_VEH_HORN)
def vehicleHorn(requestID, shopRev, vehInvID, hornID):
    try:
        vehInvID = int(vehInvID)
        hornID = int(hornID)
        oldCompDescr = _getCurrentVehicleCompDescr(vehInvID)
        if oldCompDescr is None:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle compDescr not found', None)
        descr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
        descr.hornID = None if hornID <= 0 else hornID
        newCompDescr = descr.makeCompactDescr()
        LOG_DEBUG('CMD_VEH_HORN vehInvID=%s hornID=%s newLen=%s' % (
            vehInvID, hornID, _cdLen(newCompDescr)))
        _commitInventoryChanges(vehInvID, {}, None, newCompDescr, forceVisual=True)
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


@baseRequest(AccountCommands.CMD_SYNC_SHOP)
def syncShop(requestID, revision, dataLen, dataCrc):
    data = {'rev': revision + 2, 'prevRev': revision}
    BigWorld.callback(REQUEST_CALLBACK_TIME, packStream(requestID, data))
    return RequestResult(AccountCommands.RES_STREAM, '', None)


@baseRequest(AccountCommands.CMD_SYNC_DOSSIERS)
def syncDossiers(requestID, revision, maxChangeTime, _):
    try:
        from gui.mods.offhangar import dossier as off_dossier
        payload = (off_dossier.getRevision(), off_dossier.getVehicleList())
    except Exception:
        LOG_CURRENT_EXCEPTION()
        payload = (revision + 2, [])
    BigWorld.callback(REQUEST_CALLBACK_TIME, packStream(requestID, payload))
    return RequestResult(AccountCommands.RES_STREAM, '', None)


@baseRequest(AccountCommands.CMD_SET_LANGUAGE)
def setLanguage(requestID, language):
    BigWorld.callback(REQUEST_CALLBACK_TIME, packStream(requestID, language))
    return RequestResult(AccountCommands.RES_STREAM, '', None)


# ============================================================
#  Исследование / конвертация опыта
# ============================================================

def _fireStatsResync():
    try:
        from PlayerEvents import g_playerEvents
        g_playerEvents.onStatsResync()
    except Exception:
        pass
    try:
        from gui.shared import g_itemsCache
        from gui.shared.gui_items import GUI_ITEM_TYPE
        reason = None
        try:
            from gui.shared.ItemsCache import CACHE_SYNC_REASON
            reason = getattr(CACHE_SYNC_REASON, 'STATS_RESYNC', None)
        except Exception:
            pass
        if reason is not None:
            g_itemsCache.onSyncCompleted(reason, {GUI_ITEM_TYPE.STATS: set()})
    except Exception:
        pass


def _writeStatsRequesterCache(fields):
    try:
        from gui.shared import g_itemsCache
        sr = g_itemsCache.items.stats
        for k, v in sr.__dict__.iteritems():
            if isinstance(v, dict) and 'cache' in k:
                v.update(fields)
    except Exception:
        pass


def _saveStatsToOfflineCache():
    try:
        cache = _getStatsCache()
        stats = off_data._loadOfflineCache('stats')
        if stats is None:
            stats = off_data.getOfflineStats()
        s = stats.setdefault('stats', {})
        for f in ('credits', 'gold', 'freeXP', 'vehTypeXP', 'unlocks',
                  'multipliedXPVehs', 'eliteVehicles'):
            if f in cache:
                s[f] = cache[f]
        off_data._saveOfflineCache('stats', stats)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _applyStatsDiff(fields):
    cache = _getStatsCache()
    _SET_DIFF_FIELDS = ('unlocks', 'eliteVehicles', 'multipliedXPVehs')
    diff_stats = {}
    for k, v in fields.items():
        if k in _SET_DIFF_FIELDS:
            old = set(cache.get(k, ()) or ())
            new = set(v or ())
            diff_stats[k] = list(new - old)
        else:
            diff_stats[k] = v
    cache.update(fields)
    _saveStatsToOfflineCache()
    _writeStatsRequesterCache(fields)
    try:
        rev = 0
        try:
            rev = BigWorld.player().syncData.revision or 0
        except Exception:
            pass
        diff = {'prevRev': rev, 'rev': rev + 1, 'stats': diff_stats}
        BigWorld.player()._update(True, diff)
    except Exception:
        LOG_CURRENT_EXCEPTION()
    try:
        from gui.ClientUpdateManager import g_clientUpdateManager
        g_clientUpdateManager.update({'stats': diff_stats})
    except Exception:
        pass
    _fireStatsResync()


@baseRequest(AccountCommands.CMD_UNLOCK)
def unlockItem(requestID, vehTypeCD, unlockIdx, _):
    try:
        vehTypeCD = int(vehTypeCD)
        unlockIdx = int(unlockIdx)
        _itemType, _nationID, _vehTypeID = vehicles.parseIntCompactDescr(vehTypeCD)
        vehDescr = vehicles.VehicleDescr(typeID=(_nationID, _vehTypeID))
        uds = vehDescr.type.unlocksDescrs
        if unlockIdx < 0 or unlockIdx >= len(uds):
            return RequestResult(AccountCommands.RES_FAILURE, 'invalid unlockIdx', None)
        xpCost, itemCD, required = uds[unlockIdx][0], uds[unlockIdx][1], set(uds[unlockIdx][2:])
        cache = _getStatsCache()
        unlocks = set(cache.get('unlocks', set()))
        vehXP = int(cache.get('vehTypeXP', {}).get(vehTypeCD, 0))
        freeXP = int(cache.get('freeXP', 0))
        if itemCD in unlocks:
            return RequestResult(AccountCommands.RES_SUCCESS, '', None)
        if vehTypeCD not in unlocks:
            return RequestResult(AccountCommands.RES_FAILURE, 'vehicle_locked', None)
        if not required.issubset(unlocks):
            return RequestResult(AccountCommands.RES_FAILURE, 'required_locked', None)
        if vehXP + freeXP < xpCost:
            return RequestResult(AccountCommands.RES_FAILURE, 'not_enough_xp', None)
        spentVeh = min(vehXP, xpCost)
        newVehXP = dict(cache.get('vehTypeXP', {}))
        newVehXP[vehTypeCD] = vehXP - spentVeh
        newFreeXP = freeXP - (xpCost - spentVeh)
        unlocks.add(itemCD)
        # Исследован сам танк -> открываем его базовые модули (только стартовую комплектацию)
        try:
            if vehicles.parseIntCompactDescr(itemCD)[0] == ITEM_TYPE_INDICES['vehicle']:
                unlocks |= getBaseModulesForVehicle(itemCD)
        except Exception:
            LOG_CURRENT_EXCEPTION()
        elite = set(cache.get('eliteVehicles', set()))
        if all(d[1] in unlocks for d in uds):
            elite.add(vehTypeCD)
        _applyStatsDiff({
            'unlocks': unlocks,
            'vehTypeXP': newVehXP,
            'freeXP': newFreeXP,
            'eliteVehicles': elite,
        })
        LOG_DEBUG('unlockItem OK veh=%s idx=%s item=%s cost=%s spentVeh=%s spentFree=%s' % (
            vehTypeCD, unlockIdx, itemCD, xpCost, spentVeh, xpCost - spentVeh))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)


def convertVehXP(requestID, vehTypeCD, xp, gold):
    try:
        vehTypeCD = int(vehTypeCD)
        xp = int(xp)
        gold = int(gold)
        cache = _getStatsCache()
        vehTypeXP = dict(cache.get('vehTypeXP', {}))
        curVeh = int(vehTypeXP.get(vehTypeCD, 0))
        freeXP = int(cache.get('freeXP', 0))
        curGold = int(cache.get('gold', 0))
        elite = set(cache.get('eliteVehicles', set()))
        if vehTypeCD not in elite:
            return RequestResult(AccountCommands.RES_FAILURE, 'not_elite', None)
        if xp > curVeh:
            return RequestResult(AccountCommands.RES_FAILURE, 'not_enough_xp', None)
        if gold > curGold:
            return RequestResult(AccountCommands.RES_FAILURE, 'not_enough_gold', None)
        vehTypeXP[vehTypeCD] = curVeh - xp
        _applyStatsDiff({
            'vehTypeXP': vehTypeXP,
            'freeXP': freeXP + xp,
            'gold': curGold - gold,
        })
        LOG_DEBUG('convertVehXP OK veh=%s xp=%s gold=%s' % (vehTypeCD, xp, gold))
        return RequestResult(AccountCommands.RES_SUCCESS, '', None)
    except Exception as e:
        LOG_CURRENT_EXCEPTION()
        return RequestResult(AccountCommands.RES_FAILURE, str(e), None)

# ============================================================
#  Патчи при старте
# ============================================================

def _tryPatchItemsCache(*args):
    try:
        from gui.shared import g_itemsCache
        patchItemsCacheGetVehicle(g_itemsCache)
    except Exception:
        LOG_CURRENT_EXCEPTION()

try:
    _patchStatsBuyBerths()
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    BigWorld.callback(0.0, _patchStatsBuyBerths)
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    _patchResearchViewTracking()
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    _tryPatchItemsCache()
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    BigWorld.callback(0.0, _patchResearchViewTracking)
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    BigWorld.callback(0.0, _tryPatchItemsCache)
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    baseRequest(AccountCommands.CMD_FREE_XP_CONV)(convertVehXP)
except Exception:
    LOG_CURRENT_EXCEPTION()

# ============================================================
#  Проверка экипажа перед боем
# ============================================================
REQUIRE_FULL_CREW = True       # в бой нельзя выйти, если в танке не хватает экипажа (как в игре)


def checkCrewForBattle(vehInvID, notify=True):
    """(True, '') - можно в бой. Иначе (False, текст), а при notify=True - ещё и системное сообщение игроку."""
    try:
        if not REQUIRE_FULL_CREW or not vehInvID:
            return True, ''
        crew = _getVehicleCrew(vehInvID)
        if not crew:
            return True, ''           # состав экипажа неизвестен - не блокируем
        empty = sum(1 for t in crew if not t)
        if empty == 0:
            return True, ''
        if empty == len(crew):
            msg = u'\u0412 \u0442\u0430\u043d\u043a\u0435 \u043d\u0435\u0442 \u044d\u043a\u0438\u043f\u0430\u0436\u0430: \u0432 \u0431\u043e\u0439 \u0432\u044b\u0439\u0442\u0438 \u043d\u0435\u043b\u044c\u0437\u044f. \u041d\u0430\u0437\u043d\u0430\u0447\u044c\u0442\u0435 \u0442\u0430\u043d\u043a\u0438\u0441\u0442\u043e\u0432 \u0432\u043e \u0432\u043a\u043b\u0430\u0434\u043a\u0435 \u00ab\u042d\u043a\u0438\u043f\u0430\u0436\u00bb.'
        else:
            msg = u'\u042d\u043a\u0438\u043f\u0430\u0436 \u043d\u0435 \u0443\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u043e\u0432\u0430\u043d (\u043d\u0435 \u0445\u0432\u0430\u0442\u0430\u0435\u0442 %d \u0438\u0437 %d): \u0432 \u0431\u043e\u0439 \u0432\u044b\u0439\u0442\u0438 \u043d\u0435\u043b\u044c\u0437\u044f.' % (empty, len(crew))
        LOG_DEBUG('[CREW] battle blocked: vehInvID=%s crew=%s' % (vehInvID, crew))
        if notify:
            try:
                from gui import SystemMessages
                SystemMessages.pushMessage(msg, SystemMessages.SM_TYPE.Error)
            except Exception:
                LOG_CURRENT_EXCEPTION()
        return False, msg
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return True, ''
