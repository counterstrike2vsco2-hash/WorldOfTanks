# -*- coding: utf-8 -*-
# Python bytecode 2.6 (decompiled from Python 2.7)
# Embedded file name: data
# Compiled at: 2025-07-24 20:18:26
import functools
import time
from itertools import cycle
import ResMgr
import items
import nations
from AccountCommands import VEHICLE_SETTINGS_FLAG
from constants import ACCOUNT_ATTR
from items import vehicles, ITEM_TYPE_INDICES, _xml
from nations import AVAILABLE_NAMES, INDICES
from gui.mods.offhangar.logging import *
from gui.mods.offhangar.utils import *
from gui.mods.offhangar._constants import *
from debug_utils import LOG_CURRENT_EXCEPTION
items.init(True, {})
from items.vehicles import g_list, g_cache
import os
import cPickle
import zlib
import BigWorld
doLog = functools.partial(doLog, 'OFFHANGAR')
LOG_NOTE = functools.partial(doLog, '[NOTE]')
LOG_DEBUG = functools.partial(doLog, '[DEBUG]')
itemTypeNameMap = {'guns': ('vehicleGun', g_cache.guns),
 'turrets': ('vehicleTurret', g_cache.turrets),
 'engines': ('vehicleEngine', g_cache.engines),
 'chassis': ('vehicleChassis', g_cache.chassis),
 'radios': ('vehicleRadio', g_cache.radios),
 'shells': ('shell', g_cache.shells)}
commonItemTypeNameMap = {'optional_devices': ('optionalDevice', g_cache.optionalDevices),
 'equipments': ('equipment', g_cache.equipments)}

def getOfflineShopItems():
    itemPrices = {}
    notInShopItems = set()
    vehiclesToSellForGold = set()
    vehicleSellPriceFactors = {}
    vehicleCamouflagePriceFactors = {}

    nationCount = max(INDICES.values()) + 1

    camouflagePriceFactors = [{} for _ in xrange(nationCount)]
    notInShopCamouflages = [set() for _ in xrange(nationCount)]
    inscriptionGroupPriceFactors = [{} for _ in xrange(nationCount)]
    notInShopInscriptionGroups = [set() for _ in xrange(nationCount)]

    for nationIdx in INDICES.values():
        LOG_DEBUG('shop items nation start %s' % nationIdx)

        listXmlPath = vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/list.xml'
        listSection = ResMgr.openSection(listXmlPath)

        turretsIDs_section = ResMgr.openSection(
            vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/components/turrets.xml'
        )['ids']

        chassisIDs_section = ResMgr.openSection(
            vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/components/chassis.xml'
        )['ids']

        for vname, vsection in listSection.items():
            ctx = (None, listXmlPath + '/' + vname)
            price = _xml.readPrice(ctx, vsection, 'price')

            xmlVehPath = vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/' + vname + '.xml'
            vehSec = ResMgr.openSection(xmlVehPath)

            for turretName, turretSection in vehSec['turrets0'].items():
                turretID = _xml.readInt(ctx, turretsIDs_section, turretName)
                turretPrice = _xml.readPrice(ctx, turretSection, 'price')
                turret = g_cache.turrets(nationIdx)[turretID]
                itemPrices[turret['compactDescr']] = turretPrice

            for chassisName, chassisSection in vehSec['chassis'].items():
                chassisID = _xml.readInt(ctx, chassisIDs_section, chassisName)
                chassisPrice = _xml.readPrice(ctx, chassisSection, 'price')
                chassis = g_cache.chassis(nationIdx)[chassisID]
                itemPrices[chassis['compactDescr']] = chassisPrice

            priceFactorCamo = vehSec.readFloat('camouflage/priceFactor')
            isNotInShop = vsection.readBool('notInShop', False)

            ResMgr.purge(xmlVehPath, True)

            vehID = _xml.readInt(ctx, vsection, 'id', 0, 255)
            vehTypeCompDescr = vehicles.makeIntCompactDescrByID('vehicle', nationIdx, vehID)

            itemPrices[vehTypeCompDescr] = (price[0], price[1])
            vehicleCamouflagePriceFactors[vehTypeCompDescr] = priceFactorCamo

        ResMgr.purge(vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/components/turrets.xml', True)
        ResMgr.purge(vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/components/chassis.xml', True)
        ResMgr.purge(listXmlPath, True)

        for itemTypeName, itemData in itemTypeNameMap.items():
            xmlPath = vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/components/' + itemTypeName + '.xml'
            section = ResMgr.openSection(xmlPath)
            moduleIDs_section = section['ids']

            if itemTypeName == 'shells':
                for moduleName, moduleSection in section.items():
                    if moduleName != 'icons':
                        ctx = (None, xmlPath + '/' + moduleName)
                        shellID = _xml.readInt(ctx, moduleSection, 'id')
                        shellPrice = _xml.readPrice(ctx, moduleSection, 'price')
                        shell = g_cache.shells(nationIdx)[shellID]
                        itemPrices[shell['compactDescr']] = shellPrice
            else:
                modules_section = section['shared']
                for moduleName, moduleSection in modules_section.items():
                    ctx = (None, xmlPath + '/' + moduleName)
                    moduleID = _xml.readInt(ctx, moduleIDs_section, moduleName)
                    modulePrice = _xml.readPrice(ctx, moduleSection, 'price')
                    module = itemData[1](nationIdx)[moduleID]
                    itemPrices[module['compactDescr']] = modulePrice

            ResMgr.purge(xmlPath, True)

    for commonItemTypeName, commonItemData in commonItemTypeNameMap.items():
        xmlPath = vehicles._VEHICLE_TYPE_XML_PATH + 'common/' + commonItemTypeName + '.xml'
        section = ResMgr.openSection(xmlPath)

        for itemName, itemSection in section.items():
            ctx = (None, xmlPath + '/' + itemName)
            itemID = _xml.readInt(ctx, itemSection, 'id')
            itemPrice = _xml.readPrice(ctx, itemSection, 'price')
            item = commonItemData[1]()[itemID]
            itemPrices[item.compactDescr] = itemPrice

        ResMgr.purge(xmlPath, True)

    result = {
        'itemPrices': itemPrices,
        'notInShopItems': notInShopItems,
        'vehiclesToSellForGold': vehiclesToSellForGold,
        'vehiclesRentPrices': {},
        'vehicleSellPriceFactors': vehicleSellPriceFactors,
        'vehicleCamouflagePriceFactors': vehicleCamouflagePriceFactors,
        'vehicleHornPriceFactors': {},
        'hornPrices': {},
        'playerEmblemGroupPriceFactors': {},
        'notInShopPlayerEmblemGroups': set(),
        'inscriptionGroupPriceFactors': [],
        'notInShopInscriptionGroups': [],
        'camouflagePriceFactors': [],
        'notInShopCamouflages': [],
    }

    result.update(getOfflineCustomizationShopData())
    return result
    
def _getStartVehicleIDs():
    result = {}
    for nationID in nations.INDICES.values():
        nationList = g_list.getList(nationID)
        bestID = None
        bestLevel = None
        for innerID in nationList.keys():
            vDescr = vehicles.VehicleDescr(typeID=(nationID, innerID))
            level = vDescr.type.level
            if bestLevel is None or level < bestLevel:
                bestLevel = level
                bestID = innerID
        if bestID is not None:
            result[nationID] = bestID
    return result

def _baseModulesOfVehicleDescr(descr):
    """Базовая комплектация танка = ТОЛЬКО стартовые модули + их снаряды.
    НЕ все доступные модули (топ-башня/топ-орудие и т.п. сюда не попадают)."""
    mods = set()
    for part in ('chassis', 'engine', 'radio', 'turret', 'gun'):
        try:
            mods.add(getattr(descr, part)['compactDescr'])
        except Exception:
            pass
    try:
        for shot in descr.gun['shots']:
            mods.add(shot['shell']['compactDescr'])
    except Exception:
        pass
    return mods


def getBaseModulesForVehicle(vehTypeCD):
    """set intCD базовых модулей танка по его int type compact descr.
    Для не-танков возвращает пустое множество."""
    try:
        itemTypeID, nationID, vehTypeID = vehicles.parseIntCompactDescr(vehTypeCD)
        if itemTypeID != ITEM_TYPE_INDICES['vehicle']:
            return set()
        return _baseModulesOfVehicleDescr(vehicles.VehicleDescr(typeID=(nationID, vehTypeID)))
    except Exception:
        LOG_CURRENT_EXCEPTION()
        return set()

def getOfflineInventory():
    data = dict(((k, {}) for k in ITEM_TYPE_INDICES))
    i = 1
    i_crew = 1
    compDescr = {}
    data[ITEM_TYPE_INDICES['vehicle']] = {'repair': {},
     'lastCrew': {},
     'crew': {},
     'settings': {},
     'compDescr': {},
     'eqs': {},
     'eqsLayout': {},
     'shells': {},
     'customizationExpiryTime': {},
     'lock': {},
     'shellsLayout': {}}
    data[ITEM_TYPE_INDICES['tankman']] = {'vehicle': {},
     'compDescr': {}}

    startVehIDs = _getStartVehicleIDs()

    for nationID, innerID in startVehIDs.items():
        value = (nationID, innerID)
        vehicle = vehicles.VehicleDescr(typeID=value)
        compDescr[i] = vehicle.makeCompactDescr()
        turretGun = (vehicles.makeIntCompactDescrByID('vehicleTurret', *vehicle.turrets[0][0]['id']), vehicles.makeIntCompactDescrByID('vehicleGun', *vehicle.turrets[0][0]['guns'][0]['id']))
        tmanList = items.tankmen.generateTankmen(value[0], value[1], vehicle.type.crewRoles, False, items.tankmen.MAX_SKILL_LEVEL, [])
        tmanListCycle = cycle(tmanList)
        data[ITEM_TYPE_INDICES['vehicle']]['crew'].update({i: [ tmanID for tmanID in xrange(i_crew, len(tmanList) + i_crew) ]})
        data[ITEM_TYPE_INDICES['vehicle']]['settings'].update({
            i: VEHICLE_SETTINGS_FLAG.AUTO_REPAIR | VEHICLE_SETTINGS_FLAG.AUTO_LOAD | VEHICLE_SETTINGS_FLAG.AUTO_EQUIP
        })
        data[ITEM_TYPE_INDICES['vehicle']]['compDescr'].update(compDescr)
        data[ITEM_TYPE_INDICES['vehicle']]['eqs'].update({i: []})
        data[ITEM_TYPE_INDICES['vehicle']]['eqsLayout'].update({i: []})
        data[ITEM_TYPE_INDICES['vehicle']]['shells'].update({i: vehicles.getDefaultAmmoForGun(vehicle.turrets[0][0]['guns'][0])})
        data[ITEM_TYPE_INDICES['vehicle']]['shellsLayout'].update({i: {turretGun: vehicles.getDefaultAmmoForGun(vehicle.turrets[0][0]['guns'][0])}})
        for tmanID in xrange(i_crew, len(tmanList) + i_crew):
            data[ITEM_TYPE_INDICES['tankman']]['vehicle'][tmanID] = i
            data[ITEM_TYPE_INDICES['tankman']]['compDescr'][tmanID] = next(tmanListCycle)
            i_crew += 1

        i += 1

    return {'inventory': data}


def _buildOfflineStats():
    unlocksSet = set()
    multipliedXPVehs_premium = set()
    vehiclesSet = set()
    startVehIDs = _getStartVehicleIDs()
    for nationID in nations.INDICES.values():
        unlocksSet.update([vehicles.makeIntCompactDescrByID('optionalDevice', nationID, i) for i in g_cache.optionalDevices().keys()])
        unlocksSet.update([vehicles.makeIntCompactDescrByID('equipment', nationID, i) for i in g_cache.equipments().keys()])
        startID = startVehIDs.get(nationID)
        if startID is not None:
            startDescr = vehicles.VehicleDescr(typeID=(nationID, startID))
            unlocksSet.add(vehicles.makeIntCompactDescrByID('vehicle', nationID, startID))
            unlocksSet.update(_baseModulesOfVehicleDescr(startDescr))
        # === ВАЖНО: этот цикл ВНУТРИ for nationID (отступ 8 пробелов) ===
        for innerID in g_list.getList(nationID).keys(): 
            vcd = vehicles.makeIntCompactDescrByID('vehicle', nationID, innerID)
            vehiclesSet.add(vcd)
            # Премиум-танки сразу «исследованы» -> доступны к покупке в дереве и магазине
            try:
                vtype = g_cache.vehicle(nationID, innerID)
                tags = getattr(vtype, 'tags', None)
                if tags is not None and (tags & frozenset(('premium', 'premiumIGR'))):
                    unlocksSet.add(vcd)
                    multipliedXPVehs_premium.add(vcd)
                    for nodeCD in getattr(vtype, 'autounlockedItems', ()):
                        unlocksSet.add(nodeCD)
            except Exception:
                pass
    _EXCLUDED_ACCOUNT_ATTRS = ('AOGAS',)
    attrs = 0
    for field in dir(ACCOUNT_ATTR):
        if field.startswith('_') or field in _EXCLUDED_ACCOUNT_ATTRS:
            continue
        value = getattr(ACCOUNT_ATTR, field, None)
        if isinstance(value, (int, long)):
            attrs |= value
    vehTypeXP = dict([(i, 0) for i in vehiclesSet])
    eliteVehicles = set()
    attrs &= ~int(ACCOUNT_ATTR.PREMIUM)
    return {'account': {'autoBanTime': 0,
              'attrs': attrs,
              'clanDBID': 0,
              'premiumExpiryTime': 0},
     'stats': {'berths': 40,
               'accOnline': 0,
               'gold': 1000000,
               'isFinPswdVerified': True,
               'finPswdAttemptsLeft': 0,
               'denunciationsLeft': 0,
               'freeVehiclesLeft': 0,
               'refSystem': {'referrals': {}},
               'slots': 2000,
               'battlesTillCaptcha': 99,
               'hasFinPassword': True,
               'clanInfo': (None, None, 0, 0, 0),
               'unlocks': unlocksSet,
               'mayConsumeWalletResources': True,
               'freeTMenLeft': 99999,
               'vehicleSellsLeft': 10000,
               'SPA': {'/common/goldfish_bonus_applied/': u'1'},
               'vehTypeXP': vehTypeXP,
               'unitAcceptDeadline': 0,
               'globalVehicleLocks': {},
               'freeXP': 100000000,
               'captchaTriesLeft': 3,
               'fortResource': 0,
               'tkillIsSuspected': False,
               'credits': 100000000,
               'vehTypeLocks': {},
               'dailyPlayHours': [0, 0, 0, 0, 0, 0, 0],
               'globalRating': 0,
               'restrictions': {},
               'oldVehInvID': 0,
               'accOffline': 0,
               'dossier': '',
               'multipliedXPVehs': (unlocksSet | multipliedXPVehs_premium),
               'tutorialsCompleted': 1,
               'eliteVehicles': eliteVehicles,
               'playLimits': ((2147483647, ''), (2147483647, ''))}}

def _readCamouflagePriceFactors(nationName):
    """Читает реальные priceFactor камуфляжей из customization.xml нации."""
    xmlPath = vehicles._VEHICLE_TYPE_XML_PATH + nationName + '/customization.xml'
    section = ResMgr.openSection(xmlPath)
    if section is None:
        return {}
    result = {}
    camSection = section['camouflages']
    if camSection is not None:
        for name, subsection in camSection.items():
            camoID = subsection.readInt('id', -1)
            if camoID < 0:
                continue
            priceFactor = subsection.readFloat('priceFactor', 1.0)
            if priceFactor <= 0:
                priceFactor = 1.0
            result[camoID] = priceFactor
    ResMgr.purge(xmlPath, True)
    return result

def getOfflineCustomizationShopData():
    nationCount = max(INDICES.values()) + 1
    nationNameByIdx = dict((v, k) for k, v in INDICES.items())

    camouflagePriceFactors = []
    notInShopCamouflages = []
    inscriptionGroupPriceFactors = []
    notInShopInscriptionGroups = []

    for nationIdx in xrange(nationCount):
        # --- Реальные множители камуфляжей из XML ---
        nationName = nationNameByIdx.get(nationIdx)
        if nationName is not None:
            camoFactors = _readCamouflagePriceFactors(nationName)
        else:
            camoFactors = {}

        inscrFactors = {}

        try:
            customization = vehicles.g_cache.customization(nationIdx)
            if customization:
                # Если XML не дал priceFactor для какого-то камуфляжа — ставим 1.0
                for camoID in customization.get('camouflages', {}).keys():
                    if camoID not in camoFactors:
                        camoFactors[camoID] = 1.0
                for groupName in customization.get('inscriptionGroups', {}).keys():
                    inscrFactors[groupName] = 1.0
        except Exception:
            pass

        camouflagePriceFactors.append(camoFactors)
        notInShopCamouflages.append(set())
        inscriptionGroupPriceFactors.append(inscrFactors)
        notInShopInscriptionGroups.append(set())

    # --- Эмблемы ---
    emblemGroupPriceFactors = {}
    try:
        groups, _, _ = vehicles.g_cache.playerEmblems()
        if groups:
            for groupName in groups.keys():
                emblemGroupPriceFactors[groupName] = 1.0
    except Exception:
        pass

    # --- Гудки ---
    hornPrices = {}
    hornFactors = {}
    try:
        horns = vehicles.g_cache.horns()
        if horns:
            for hornID in horns.keys():
                hornPrices[hornID] = 0
                hornFactors[hornID] = 1.0
    except Exception:
        pass

    return {
        'camouflagePriceFactors': camouflagePriceFactors,
        'notInShopCamouflages': notInShopCamouflages,
        'inscriptionGroupPriceFactors': inscriptionGroupPriceFactors,
        'notInShopInscriptionGroups': notInShopInscriptionGroups,
        'playerEmblemGroupPriceFactors': emblemGroupPriceFactors,
        'notInShopPlayerEmblemGroups': set(),
        'hornPrices': hornPrices,
        'vehicleHornPriceFactors': hornFactors,
    }

from gui.mods.offhangar._constants import OFFLINE_QUESTS_PROGRESS

def getOfflineQuestsProgress():
    return dict(OFFLINE_QUESTS_PROGRESS)

OFFHANGAR_CACHE_VERSION = 9

def _findGameRoot():
    try:
        cur = os.getcwd()
    except Exception:
        cur = os.path.abspath('.')

    candidates = [
        cur,
        os.path.dirname(cur),
    ]

    for p in candidates:
        try:
            if os.path.isdir(os.path.join(p, 'res')):
                return p
            if os.path.isdir(os.path.join(p, 'res_mods')):
                return p
            if os.path.isfile(os.path.join(p, 'paths.xml')):
                return p
        except Exception:
            pass

    return cur


def _offhangarCachePath(name):
    try:
        cacheDir = os.path.join(_findGameRoot(), 'cache')

        if not os.path.isdir(cacheDir):
            os.makedirs(cacheDir)

        return os.path.join(cacheDir, '%s_v%d.cache' % (name, OFFHANGAR_CACHE_VERSION))
    except Exception:
        return None


def _loadOfflineCache(name):
    path = _offhangarCachePath(name)

    if not path or not os.path.isfile(path):
        return None

    try:
        f = open(path, 'rb')
        try:
            raw = f.read()
        finally:
            f.close()

        version, data = cPickle.loads(zlib.decompress(raw))

        if version != OFFHANGAR_CACHE_VERSION:
            return None

        return data
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass

        return None


def _saveOfflineCache(name, data):
    path = _offhangarCachePath(name)

    if not path:
        return

    try:
        payload = zlib.compress(
            cPickle.dumps((OFFHANGAR_CACHE_VERSION, data), cPickle.HIGHEST_PROTOCOL),
            6
        )

        f = open(path, 'wb')
        try:
            f.write(payload)
        finally:
            f.close()
    except Exception:
        pass


_orig_getOfflineShopItems = getOfflineShopItems
def getOfflineShopItems():
    try:
        cached = _loadOfflineCache('shop')
        if cached is not None:
            LOG_DEBUG('offline shop cache used')
            return cached
    except Exception:
        LOG_CURRENT_EXCEPTION()

    LOG_DEBUG('offline shop cache building')
    data = _orig_getOfflineShopItems()

    try:
        _saveOfflineCache('shop', data)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    return data


_orig_getOfflineInventory = getOfflineInventory
def getOfflineInventory():
    try:
        cached = _loadOfflineCache('inventory')
        if cached is not None:
            LOG_DEBUG('offline inventory cache used')
            return cached
    except Exception:
        LOG_CURRENT_EXCEPTION()

    LOG_DEBUG('offline inventory cache building')
    data = _orig_getOfflineInventory()

    try:
        _saveOfflineCache('inventory', data)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    return data

_orig_getOfflineQuestsProgress = getOfflineQuestsProgress
def getOfflineQuestsProgress():
    try:
        cached = _loadOfflineCache('quests')
        if cached is not None:
            LOG_DEBUG('offline quests cache used')
            return cached
    except Exception:
        LOG_CURRENT_EXCEPTION()

    LOG_DEBUG('offline quests cache building')
    data = _orig_getOfflineQuestsProgress()

    try:
        _saveOfflineCache('quests', data)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    return data

OFFLINE_INVENTORY = None


def getOfflineInventoryData():
    global OFFLINE_INVENTORY
    if OFFLINE_INVENTORY is None:
        OFFLINE_INVENTORY = getOfflineInventory()
    return OFFLINE_INVENTORY


def saveOfflineInventory():
    global OFFLINE_INVENTORY
    if OFFLINE_INVENTORY is not None:
        try:
            _saveOfflineCache('inventory', OFFLINE_INVENTORY)
        except Exception:
            LOG_CURRENT_EXCEPTION()

def _mergeStats(base, cached):
    """Накладывает сохранённый кэш поверх базовой генерации.
    Множества (unlocks/elite/multiplied) — объединяются, чтобы премиум
    из base никогда не терялся. Баланс — берётся из кэша (последнее состояние)."""
    if not isinstance(cached, dict):
        return base
    base_stats = base.setdefault('stats', {})
    cached_stats = cached.get('stats', {}) or {}
    for setfield in ('unlocks', 'multipliedXPVehs', 'eliteVehicles'):
        if setfield in cached_stats:
            base_stats[setfield] = set(base_stats.get(setfield, set())) | set(cached_stats[setfield])
    if 'vehTypeXP' in cached_stats:
        merged_xp = dict(base_stats.get('vehTypeXP', {}))
        merged_xp.update(cached_stats['vehTypeXP'])
        base_stats['vehTypeXP'] = merged_xp
    for scalar in ('credits', 'gold', 'freeXP'):
        if scalar in cached_stats:
            base_stats[scalar] = cached_stats[scalar]
    cached_acc = cached.get('account')
    if cached_acc:
        base.setdefault('account', {}).update(cached_acc)
    return base

def _normalize_premium(stats):
    try:
        acc = stats.get('account')
        if not isinstance(acc, dict):
            return stats
        expiry = int(acc.get('premiumExpiryTime', 0) or 0)
        purchased = bool(acc.get('_premiumPurchased', False))
        now = int(time.time())
        cur = int(acc.get('attrs', 0) or 0)
        if purchased and expiry > now:
            acc['attrs'] = cur | int(ACCOUNT_ATTR.PREMIUM)
        else:
            acc['attrs'] = cur & ~int(ACCOUNT_ATTR.PREMIUM)
            if not purchased:
                acc['premiumExpiryTime'] = 0
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return stats

def getOfflineStats():
    base = _buildOfflineStats()
    cached = None
    try:
        cached = _loadOfflineCache('stats')
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if cached is not None:
        LOG_DEBUG('offline stats cache merged')
        return _normalize_premium(_mergeStats(base, cached))
    LOG_DEBUG('offline stats cache building (first run)')
    return _normalize_premium(base)

_BALANCE_APPLIED = [False]


def _applyConfiguredBalance(out):
    """Накрутка ресурсов из offline_user.txt: credits / gold / freexp. Применяется один раз за запуск игры.
    "credits=5000000" - выставить баланс, "credits=+5000000" - прибавить."""
    if _BALANCE_APPLIED[0]:
        return out
    _BALANCE_APPLIED[0] = True
    try:
        from gui.mods.offhangar import _constants as _c
        cfg = _c.readOfflineSettings()
        stats = out.setdefault('stats', {})
        changed = []
        for key, field in (('credits', 'credits'), ('gold', 'gold'), ('freexp', 'freeXP')):
            r = _c.parseNumber(cfg.get(key))
            if r is None:
                continue
            cur = int(stats.get(field, 0) or 0)
            new = cur + r[1] if r[0] == 'add' else r[1]
            new = max(0, min(2000000000, new))
            stats[field] = new
            changed.append('%s: %s -> %s' % (field, cur, new))
        if changed:
            _saveOfflineCache('stats', out)
            LOG_NOTE('[CONFIG] offline_user.txt balance applied: %s' % '; '.join(changed))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return out


def getOfflineStats():
    base = _buildOfflineStats()
    cached = None
    try:
        cached = _loadOfflineCache('stats')
    except Exception:
        LOG_CURRENT_EXCEPTION()
    if cached is not None:
        LOG_DEBUG('offline stats cache merged')
        out = _mergeStats(base, cached)
    else:
        LOG_DEBUG('offline stats cache building (first run)')
        out = base
    out = _applyConfiguredBalance(out)
    try:
        # капча в оффлайне не нужна: иначе вход в обучение/комнаты даёт "Ошибка при загрузке изображения CAPTCHA"
        out['stats']['battlesTillCaptcha'] = 99
        out['stats']['captchaTriesLeft'] = 3
    except Exception:
        pass
    try:
        from gui.mods.offhangar import dossier as off_dossier
        out['stats']['dossier'] = off_dossier.getAccountCompDescr()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return out

def updateOfflineVehicleCompDescr(vehInvID, compDescr):
    root = getOfflineInventoryData()
    inv = root.setdefault('inventory', {})
    vehInv = inv.setdefault(ITEM_TYPE_INDICES['vehicle'], {})
    vehInv.setdefault('compDescr', {})[vehInvID] = compDescr
    saveOfflineInventory()