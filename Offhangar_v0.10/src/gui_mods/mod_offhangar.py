# -*- coding: utf-8 -*-
import os
import signal
import Account
import BigWorld
import account_shared
from ConnectionManager import connectionManager, CONNECTION_STATUS
from gui.game_control.GameSessionController import GameSessionController
from account_helpers.Shop import Shop
from debug_utils import LOG_CURRENT_EXCEPTION, LOG_ERROR
from gui.shared.gui_items.Vehicle import Vehicle
from helpers.time_utils import _TimeCorrector, _g_instance
from nations import INDICES
from predefined_hosts import g_preDefinedHosts
from gui.mods.offhangar.logging import *
from gui.mods.offhangar.utils import *
from gui.mods.offhangar._constants import *
from gui.mods.offhangar.server import *
from gui.mods.offhangar.requests import *
from gui.shared import g_eventsCache
Account.LOG_DEBUG = LOG_DEBUG
Account.LOG_NOTE = LOG_NOTE
Account.LOG_ERROR = LOG_ERROR


def _isOffline():
    try:
        player = BigWorld.player()
    except Exception:
        return False
    return getattr(player, 'isOffline', False)


_orig_readScriptConfig = g_preDefinedHosts.readScriptConfig

def _patched_readScriptConfig(dataSection):
    _orig_readScriptConfig(dataSection)

    # --- Очищаем все загруженные серверы ---
    g_preDefinedHosts._hosts = []
    g_preDefinedHosts._urlMap = {}
    g_preDefinedHosts._nameMap = {}
    g_preDefinedHosts._peripheryMap = {}

    # --- Добавляем единственный сервер: offline.loc ---
    offlinePeripheryID = 999
    host = g_preDefinedHosts._makeHostItem(
        OFFLINE_SERVER_ADDRESS, OFFLINE_SERVER_ADDRESS, OFFLINE_SERVER_ADDRESS,
        peripheryID=offlinePeripheryID)
    idx = 0
    g_preDefinedHosts._hosts.append(host)
    g_preDefinedHosts._urlMap[OFFLINE_SERVER_ADDRESS] = idx
    g_preDefinedHosts._nameMap[OFFLINE_SERVER_ADDRESS] = idx
    g_preDefinedHosts._peripheryMap[offlinePeripheryID] = idx

    LOG_DEBUG('[FS] All servers cleared. Only: %s' % OFFLINE_SERVER_ADDRESS)

g_preDefinedHosts.readScriptConfig = _patched_readScriptConfig

_offlineShopItemsCache = None

@override(Shop, '__onSyncComplete')
def Shop__onSyncComplete(baseFunc, baseSelf, syncID, data):
    LOG_DEBUG('Shop__onSyncComplete enter, isOffline=%s' % _isOffline())
    if not _isOffline():
        baseFunc(baseSelf, syncID, data)
        return

    def _delayed():
        global _offlineShopItemsCache
        LOG_DEBUG('building offline shop items start (delayed)')
        if _offlineShopItemsCache is None:
            _offlineShopItemsCache = getOfflineShopItems()
        LOG_DEBUG('building offline shop items done (delayed)')
        finalData = {
            'berthsPrices': (16, 16, [300]),
            'freeXPConversion': (25, 1),
            'dropSkillsCost': {
                0: {'xpReuseFraction': 0.5, 'gold': 0, 'credits': 0},
                1: {'xpReuseFraction': 0.75, 'gold': 0, 'credits': 20000},
                2: {'xpReuseFraction': 1.0, 'gold': 200, 'credits': 0}},
            'refSystem': {'maxNumberOfReferrals': 50, 'posByXPinTeam': 10,
                          'maxReferralXPPool': 350000,
                          'periods': [(24, 3.0), (168, 2.0), (876000, 1.5)]},
            'playerEmblemCost': {0: (15, True), 30: (6000, False), 7: (1500, False)},
            'premiumCost': {1: 250, 3: 650, 7: 1250, 30: 2500, 180: 13500, 360: 24000},
            'winXPFactorMode': 0,
            'sellPriceModif': 0.5,
            'passportChangeCost': 50,
            'camouflageCost': {0: (15, True), 7: (1500, False), 30: (6000, False)},
            'playerInscriptionCost': {0: (15, True), 7: (1500, False), 30: (6000, False)},
            'exchangeRateForShellsAndEqs': 400,
            'exchangeRate': 400,
            'tankmanCost': (
                {'isPremium': False, 'baseRoleLoss': 0.2, 'gold': 0, 'credits': 0,
                 'classChangeRoleLoss': 0.2, 'roleLevel': 50},
                {'isPremium': False, 'baseRoleLoss': 0.1, 'gold': 0, 'credits': 20000,
                 'classChangeRoleLoss': 0.1, 'roleLevel': 75},
                {'isPremium': True, 'baseRoleLoss': 0.0, 'gold': 200, 'credits': 0,
                 'classChangeRoleLoss': 0.0, 'roleLevel': 100}),
            'paidRemovalCost': 10,
            'dailyXPFactor': 2,
            'changeRoleCost': 500,
            'items': _offlineShopItemsCache,
            'customization': dict(((n, {'camouflages': {}}) for n in INDICES.values())),
            'isEnabledBuyingGoldShellsForCredits': True,
            'slotsPrices': (9, [300]),
            'freeXPToTManXPRate': 10,
            'sellPriceFactor': 0.5,
            'isEnabledBuyingGoldEqsForCredits': True,

            'defaults': {
                'camouflageCost': {0: (15, True), 7: (1500, False), 30: (6000, False)},
                'playerEmblemCost': {0: (15, True), 7: (1500, False), 30: (6000, False)},
                'playerInscriptionCost': {0: (15, True), 7: (1500, False), 30: (6000, False)},
                'items': _offlineShopItemsCache,
            }}

        finalData['rev'] = data.get('rev', 2)
        finalData['prevRev'] = data.get('prevRev', 0)

        baseFunc(baseSelf, syncID, finalData)

    _delayed()

@override(_TimeCorrector, 'serverRegionalTime')
def TimeCorrector_serverRegionalTime(baseFunc, baseSelf):
    if not _isOffline():
        return baseFunc(baseSelf)
    offset = 0
    try:
        offset = OFFLINE_SERVER_SETTINGS['regional_settings']['starting_time_of_a_new_day']
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return _g_instance.serverUTCTime + offset


@override(GameSessionController, 'isSessionStartedThisDay')
def GameSessionController_isSessionStartedThisDay(baseFunc, baseSelf):
    if not _isOffline():
        return baseFunc(baseSelf)
    s = OFFLINE_SERVER_SETTINGS['regional_settings']
    return int(_g_instance.serverRegionalTime) / 86400 == int(
        baseSelf._GameSessionController__sessionStartedAt +
        s['starting_time_of_a_new_day']) / 86400


@override(GameSessionController, '_getWeeklyPlayHours')
def GameSessionController_getWeeklyPlayHours(baseFunc, baseSelf):
    if not _isOffline():
        return baseFunc(baseSelf)
    s = OFFLINE_SERVER_SETTINGS['regional_settings']
    weekDaysCount = account_shared.currentWeekPlayDaysCount(
        _g_instance.serverUTCTime,
        s['starting_time_of_a_new_day'],
        s['starting_day_of_a_new_weak'])
    return baseSelf._getDailyPlayHours() + sum(
        baseSelf._GameSessionController__stats.dailyPlayHours[1:weekDaysCount])


@override(Vehicle, 'canSell')
def Vehicle_canSell(baseFunc, baseSelf):
    return _isOffline() or baseFunc(baseSelf)


_orig_AccountRepository_init = Account._AccountRepository.__init__
def _patched_AccountRepository_init(self, name, serverSettings):
    if not name or not isinstance(serverSettings, dict):
        name = OFFLINE_NICKNAME
        serverSettings = OFFLINE_SERVER_SETTINGS
    _orig_AccountRepository_init(self, name, serverSettings)
Account._AccountRepository.__init__ = _patched_AccountRepository_init

try:
    from gui.Scaleform.daapi.view.lobby.customization import CustomizationHelper as _CH

    _orig_getVisitedIDs = _CH.getVisitedIDs

    def _safe_getVisitedIDs(customizationName):
        try:
            result = _orig_getVisitedIDs(customizationName)
            if isinstance(result, list):
                return result
            return []
        except Exception:
            return []

    _CH.getVisitedIDs = _safe_getVisitedIDs

    _orig_getNewIdsByType = _CH.getNewIdsByType

    def _safe_getNewIdsByType(type, nationId=None):
        try:
            result = _orig_getNewIdsByType(type, nationId)
            if isinstance(result, set):
                return result
            return set()
        except Exception:
            return set()

    _CH.getNewIdsByType = _safe_getNewIdsByType

    _orig_updateVisitedItems = _CH.updateVisitedItems

    def _safe_updateVisitedItems(customizationName, visitedIds):
        try:
            return _orig_updateVisitedItems(customizationName, visitedIds)
        except Exception:
            pass

    _CH.updateVisitedItems = _safe_updateVisitedItems

    _orig_clearStoredCustomizationData = _CH.clearStoredCustomizationData

    def _safe_clearStoredCustomizationData():
        try:
            return _orig_clearStoredCustomizationData()
        except Exception:
            pass

    _CH.clearStoredCustomizationData = _safe_clearStoredCustomizationData

except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    from tutorial.control.offbattle.context import OffbattleStartReqs

    _orig_isEnabled = OffbattleStartReqs.isEnabled

    def _safe_isEnabled(self, *args, **kwargs):
        try:
            return _orig_isEnabled(self, *args, **kwargs)
        except Exception:
            return False

    OffbattleStartReqs.isEnabled = _safe_isEnabled

except Exception:
    LOG_CURRENT_EXCEPTION()

# --- Patch checkClientServerVersions ---
_orig_checkVersions = connectionManager.checkClientServerVersions
def _patched_checkVersions(clientVersion, serverVersion):
    _orig_checkVersions(clientVersion, serverVersion or clientVersion)
connectionManager.checkClientServerVersions = _patched_checkVersions

@override(Account.PlayerAccount, '__init__')
def Account_init(baseFunc, baseSelf):
    baseSelf.__dict__['isOffline'] = not getattr(baseSelf, 'name', None)

    LOG_DEBUG('Account_init called, isOffline=%s, id=%s' % (
        baseSelf.__dict__['isOffline'], id(baseSelf)))

    if baseSelf.__dict__['isOffline']:
        baseSelf.__dict__['fakeServer'] = FakeServer()
        baseSelf.__dict__['serverSettings'] = OFFLINE_SERVER_SETTINGS
        baseSelf.__dict__['databaseID'] = OFFLINE_DBID
        baseSelf.__dict__['name'] = OFFLINE_NICKNAME

    baseFunc(baseSelf)

    if baseSelf.__dict__.get('isOffline', False):
        baseSelf.__dict__['name'] = OFFLINE_NICKNAME
        LOG_DEBUG('Account_init calling BigWorld.player(baseSelf), id=%s' % id(baseSelf))
        BigWorld.player(baseSelf)
        
_orig_getattribute = Account.PlayerAccount.__getattribute__
def _patched_getattribute(self, name):
    if name in ('cell', 'base', 'server'):
        try:
            if self.__dict__.get('isOffline', False):
                name = 'fakeServer'
        except Exception:
            pass
    elif name == 'name':
        try:
            d = self.__dict__
            if d.get('isOffline', False):
                return d.get('name') or OFFLINE_NICKNAME
        except Exception:
            pass
    return _orig_getattribute(self, name)
Account.PlayerAccount.__getattribute__ = _patched_getattribute

_SHUTDOWN = [False]          # идёт закрытие игры (game.fini): тут сброс сущностей и пространств нужен, иначе клиент зависает


@override(BigWorld, 'resetEntityManager')
def BigWorld_resetEntityManager(baseFunc, *args):
    LOG_DEBUG('resetEntityManager called, isOffline=%s, shutdown=%s' % (_isOffline(), _SHUTDOWN[0]))
    if _isOffline() and not _SHUTDOWN[0]:
        LOG_DEBUG('resetEntityManager skipped (offline)')
        return
    try:
        return baseFunc(*args)
    finally:
        if _SHUTDOWN[0]:
            LOG_DEBUG('[EXIT] resetEntityManager done')


@override(BigWorld, 'clearAllSpaces')
def BigWorld_clearAllSpaces(baseFunc, *args):
    LOG_DEBUG('clearAllSpaces called, isOffline=%s, shutdown=%s' % (_isOffline(), _SHUTDOWN[0]))
    if _isOffline() and not _SHUTDOWN[0]:
        LOG_DEBUG('clearAllSpaces skipped (offline)')
        return
    try:
        return baseFunc(*args)
    finally:
        if _SHUTDOWN[0]:
            LOG_DEBUG('[EXIT] clearAllSpaces done')


EXIT_WATCHDOG_SECONDS = 8     # если клиент не закрылся за это время после начала выхода - процесс завершается принудительно


def _spawnExitWatchdog(seconds):
    """Отдельный процесс завершит игру, если закрытие зависнет (клиент в офлайне иногда не выходил без диспетчера задач).
    Это внешний процесс, поэтому он сработает, даже если сам клиент завис."""
    try:
        import subprocess
        cmd = 'cmd.exe /c "ping -n %d 127.0.0.1 >nul & taskkill /F /PID %d >nul 2>nul"' % (seconds + 1, os.getpid())
        dn = open(os.devnull, 'r+')
        subprocess.Popen(cmd, stdin=dn, stdout=dn, stderr=dn, creationflags=0x08000000)
        LOG_DEBUG('[EXIT] watchdog started: force close in %ss' % seconds)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _installExitHook():
    try:
        import game as _game
        _orig_fini = _game.fini

        def _game_fini(*args, **kwargs):
            LOG_DEBUG('[EXIT] game.fini begin')
            _SHUTDOWN[0] = True
            _spawnExitWatchdog(EXIT_WATCHDOG_SECONDS)
            try:
                import Settings as _Settings
                _Settings.g_instance.save()
            except Exception:
                LOG_CURRENT_EXCEPTION()
            try:
                return _orig_fini(*args, **kwargs)
            finally:
                LOG_DEBUG('[EXIT] game.fini end')

        _game.fini = _game_fini
        LOG_DEBUG('[EXIT] game.fini hook installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()


_installExitHook()

_orig_disconnect = connectionManager.disconnect
def _patched_disconnect(*args, **kwargs):
    if _isOffline():
        LOG_DEBUG('disconnect skipped (offline)')
        return
    return _orig_disconnect(*args, **kwargs)
connectionManager.disconnect = _patched_disconnect

@override(Account.PlayerAccount, 'onBecomePlayer')
def Account_onBecomePlayer(baseFunc, baseSelf):
    LOG_DEBUG('Account_onBecomePlayer: before baseFunc, id=%s' % id(baseSelf))
    try:
        baseFunc(baseSelf)
    except Exception:
        LOG_CURRENT_EXCEPTION()
        LOG_DEBUG('Account_onBecomePlayer: baseFunc raised exception')
    LOG_DEBUG('Account_onBecomePlayer: after baseFunc, id=%s' % id(baseSelf))
    if baseSelf.__dict__.get('isOffline', False):
        baseSelf.showGUI(OFFLINE_GUI_CTX)
        
_orig_onBecomeNonPlayer = Account.PlayerAccount.onBecomeNonPlayer
def _patched_onBecomeNonPlayer(self):
    if self.__dict__.get('isOffline', False):
        LOG_DEBUG('onBecomeNonPlayer skipped (offline)')
        return
    return _orig_onBecomeNonPlayer(self)
Account.PlayerAccount.onBecomeNonPlayer = _patched_onBecomeNonPlayer

@override(BigWorld, 'clearEntitiesAndSpaces')
def BigWorld_clearEntitiesAndSpaces(baseFunc, *args):
    if _isOffline():
        return
    baseFunc(*args)

from account_helpers.AccountSyncData import AccountSyncData

_orig_ASD_onSyncResponse = AccountSyncData._AccountSyncData__onSyncResponse

def _patched_ASD_onSyncResponse(self, syncID, resultID, ext={}):
    if not _isOffline():
        return _orig_ASD_onSyncResponse(self, syncID, resultID, ext)
    if resultID == AccountCommands.RES_NON_PLAYER:
        return
    if syncID != self._AccountSyncData__syncID:
        return
    if resultID < 0:
        LOG_ERROR('[FS] Data synchronization failed.')
        self._resynchronize()
        return
    self.revision = ext.get('rev', self.revision)
    self._AccountSyncData__isSynchronized = True
    if not self._AccountSyncData__account._update(not self._AccountSyncData__isFirstSync, ext):
        return
    self._AccountSyncData__isFirstSync = False
    subscribers = self._AccountSyncData__subscribers
    self._AccountSyncData__subscribers = []
    for callback in subscribers:
        callback(resultID)
    try:
        from PlayerEvents import g_playerEvents
        g_playerEvents.onInventoryResync()
        g_playerEvents.onStatsResync()
    except Exception:
        LOG_CURRENT_EXCEPTION()

AccountSyncData._AccountSyncData__onSyncResponse = _patched_ASD_onSyncResponse
def _patched_getattribute(self, name):
    if name in ('cell', 'base', 'server'):
        try:
            if self.__dict__.get('isOffline', False):
                name = 'fakeServer'
        except Exception:
            pass
    elif name == 'name':
        try:
            d = self.__dict__
            if d.get('isOffline', False):
                return d.get('name') or OFFLINE_NICKNAME
        except Exception:
            pass
    elif name == 'serverSettings':
        try:
            d = self.__dict__
            if d.get('isOffline', False):
                _v = d.get('serverSettings')
                return _v if isinstance(_v, dict) else OFFLINE_SERVER_SETTINGS
        except Exception:
            pass
    elif name == 'databaseID':
        try:
            d = self.__dict__
            if d.get('isOffline', False):
                return d.get('databaseID') or OFFLINE_DBID
        except Exception:
            pass
    # === ДОБАВЛЕНО ===
    elif name == 'arena':
        try:
            d = self.__dict__
            if d.get('isOffline', False):
                return None
        except Exception:
            pass
    # === КОНЕЦ ===
    return _orig_getattribute(self, name)
Account.PlayerAccount.__getattribute__ = _patched_getattribute

@override(BigWorld, 'connect')
def BigWorld_connect(baseFunc, server, loginParams, progressFn):
    if server == OFFLINE_SERVER_ADDRESS:
        LOG_DEBUG('BigWorld.connect -> offline')
        progressFn(0.0, 'DNS_LOOKUP', {})
        progressFn(0.3, 'CHECKING_VERSIONS', {})
        progressFn(0.6, 'LOGGING_ON', {})
        progressFn(1.0, 'LOGGED_ON', {})

        def _finishConnect():
            connectionManager._ConnectionManager__setConnectionStatus(CONNECTION_STATUS.connected)
            LOG_DEBUG('status set (deferred), isConnected now=%s' % connectionManager.isConnected())
            try:
                existing = BigWorld.player()
            except Exception:
                existing = None
            if existing is not None and getattr(existing, 'isOffline', False):
                LOG_DEBUG('BigWorld.connect -> reusing existing offline entity')
                connectionManager.onConnected()
                try:
                    existing.showGUI(OFFLINE_GUI_CTX)
                except Exception:
                    LOG_CURRENT_EXCEPTION()
                return
            BigWorld.createEntity('Account', BigWorld.createSpace(), 0,
                                  (0, 0, 0), (0, 0, 0), {})
            connectionManager.onConnected()

        BigWorld.callback(0.0, _finishConnect)
    else:
        baseFunc(server, loginParams, progressFn)

import BattleReplay
from gui.game_control.wallet import WalletController

@override(BattleReplay.BattleReplay, '_BattleReplay__onAccountBecomePlayer')
def BattleReplay_onAccountBecomePlayer(baseFunc, baseSelf):
    if not _isOffline():
        return baseFunc(baseSelf)
    player = BigWorld.player()
    baseSelf._BattleReplay__playerDatabaseID = OFFLINE_DBID
    baseSelf._BattleReplay__roamingSettings = OFFLINE_SERVER_SETTINGS['roaming']
    LOG_DEBUG('BattleReplay_onAccountBecomePlayer patched, offline')

from gui.WindowsManager import WindowsManager
from gui.shared import g_itemsCache

try:
    from gui.Scaleform.daapi.view.lobby.customization.data_providers import (
        BaseCustomizationDataProvider,
        HornsDataProvider,
        RentalPackageDataProviderBase
    )

    def _safe_makeCost(cls, defCost, vehPriceFactor, priceFactor):
        try:
            if defCost is None:
                defCost = 0.0
            if vehPriceFactor is None:
                vehPriceFactor = 1.0
            if priceFactor is None:
                priceFactor = 1.0

            return int(round(float(defCost) * float(vehPriceFactor) * float(priceFactor)))
        except Exception:
            return 0

    BaseCustomizationDataProvider._makeCost = classmethod(_safe_makeCost)

    def _safe_horn_getCost(self, hornID):
        try:
            price = g_itemsCache.items.shop.getHornPrice(hornID)
            if price is None:
                return 0

            factor = g_itemsCache.items.shop.getHornPriceFactor(hornID)
            if factor is None:
                factor = 1.0

            return int(round(float(price) * float(factor)))
        except Exception:
            return 0

    HornsDataProvider.getCost = _safe_horn_getCost

    _orig_rental_buildList = RentalPackageDataProviderBase.buildList

    def _safe_rental_buildList(self, costs, defaultCosts):
        if not costs:
            costs = {0: (0, False)}

        if not defaultCosts:
            defaultCosts = {0: (0, False)}

        return _orig_rental_buildList(self, costs, defaultCosts)

    RentalPackageDataProviderBase.buildList = _safe_rental_buildList
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    from gui.Scaleform.daapi.view.lobby.customization.BaseTimedCustomizationInterface import BaseTimedCustomizationInterface

    _orig_handleRentalPackageChange = BaseTimedCustomizationInterface._BaseTimedCustomizationInterface__handleRentalPackageChange

    def _safe_handleRentalPackageChange(self, index):
        try:
            item = self._rentalPackageDP.selectedPackage
        except Exception:
            item = None

        if item is None or item.get('cost', -1) <= -1:
            try:
                self._itemsDP.setDefaultCost(0, 0, False, False, 0)
                self._itemsDP.refresh()
            except Exception:
                pass
            return

        return _orig_handleRentalPackageChange(self, index)

    BaseTimedCustomizationInterface._BaseTimedCustomizationInterface__handleRentalPackageChange = _safe_handleRentalPackageChange
except Exception:
    LOG_CURRENT_EXCEPTION()

@override(WindowsManager, 'onAccountShowGUI')
def WindowsManager_onAccountShowGUI(baseFunc, baseSelf, ctx=None):
    LOG_DEBUG('WindowsManager.onAccountShowGUI called, isConnected=%s, rawStatus=%s' % (
        connectionManager.isConnected(),
        connectionManager._ConnectionManager__connectionStatus))
    return baseFunc(baseSelf, ctx)

def _onItemsCacheSyncCompleted(reason, invalidItems):
    LOG_DEBUG('g_itemsCache sync completed, isSynced=%s, reason=%s' % (g_itemsCache.isSynced(), reason))
g_itemsCache.onSyncCompleted += _onItemsCacheSyncCompleted

from gui.Scaleform.daapi.view.lobby.hangar.TankCarousel import TankCarousel
from gui.shared import REQ_CRITERIA

_orig_updateVehicles = TankCarousel.updateVehicles
def _patched_updateVehicles(self, vehicles=None):
    if _isOffline():
        items = g_itemsCache.items
        allVehs = items.getVehicles()
        invVehs = items.getVehicles(REQ_CRITERIA.INVENTORY)
        LOG_DEBUG('carousel debug: total shop vehicles=%s, in inventory=%s' % (len(allVehs), len(invVehs)))
    LOG_DEBUG('carousel: calling orig_updateVehicles')
    result = _orig_updateVehicles(self, vehicles)
    LOG_DEBUG('carousel: orig_updateVehicles done')
    return result
TankCarousel.updateVehicles = _patched_updateVehicles

# ============================================================
# Перехват кнопки выбора режима: Командный бой
# ============================================================

_OFFLINE_BATTLE_STARTING = False


def _isOfflineMode():
    try:
        player = BigWorld.player()
        if player is None:
            return False
        return getattr(player, 'isOffline', False) or hasattr(player, 'fakeServer')
    except Exception:
        return False


def _isOfflineBattleActive():
    try:
        from gui.mods.offhangar import battle as off_battle
        return off_battle._active()
    except Exception:
        return False


def _getOfflineVehicleInvID():
    try:
        from CurrentVehicle import g_currentVehicle
        if g_currentVehicle.isPresent():
            return g_currentVehicle.invID
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return 0


def _startOfflineBattleFromButton(arenaTypeID=0):
    global _OFFLINE_BATTLE_STARTING

    if not _isOfflineMode():
        return

    if _isOfflineBattleActive():
        LOG_DEBUG('battle button: offline battle already active')
        return

    if _OFFLINE_BATTLE_STARTING:
        LOG_DEBUG('battle button: offline battle already starting')
        return

    vehInvID = _getOfflineVehicleInvID()
    if not checkCrewForBattle(vehInvID)[0]:
        return                      # нет экипажа - в бой не войти (сообщение уже показано)

    _OFFLINE_BATTLE_STARTING = True

    LOG_DEBUG('battle button: start offline battle vehInvID=%s' % vehInvID)

    try:
        from gui.mods.offhangar import battle as off_battle

        # Прямой старт боя, минуя очередь RandomQueueFunctional.
        # Это лучше для кнопки "Командный бой", чтобы не включать
        # логику случайной очереди.
        # Сначала экран ожидания боя (BattleQueue с живыми числами), затем старт боя.
        try:
            from gui.mods.offhangar import queuefake
            queuefake.enterQueue(lambda: off_battle._startBattle(vehInvID, arenaTypeID))
        except Exception:
            LOG_CURRENT_EXCEPTION()
            off_battle._startBattle(vehInvID, arenaTypeID)
    except Exception:
        LOG_CURRENT_EXCEPTION()

        # Фолбэк через старую эмуляцию очереди.
        try:
            player = BigWorld.player()
            if player is not None and hasattr(player, 'enqueueRandom'):
                LOG_DEBUG('battle button: fallback enqueueRandom vehInvID=%s' % vehInvID)
                player.enqueueRandom(vehInvID, 65535, 0)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def _resetStartFlag():
        global _OFFLINE_BATTLE_STARTING
        _OFFLINE_BATTLE_STARTING = False

    try:
        BigWorld.callback(2.0, _resetStartFlag)
    except Exception:
        _OFFLINE_BATTLE_STARTING = False

def _isRoomAction(dispatcher, action):
    """Тренировочные комнаты и исторические бои идут родным путём (fakeprb подменяет сервер)."""
    try:
        from gui.mods.offhangar import fakeprb
        if not fakeprb.ENABLED:
            return False
        if fakeprb.passthroughAction(getattr(action, 'actionName', None)):
            return True
        from constants import PREBATTLE_TYPE, QUEUE_TYPE
        fs = dispatcher.getFunctionalState()
        return bool(fs.isInPrebattle() or fs.isInPreQueue(QUEUE_TYPE.HISTORICAL))
    except Exception:
        LOG_CURRENT_EXCEPTION()
    return False


def _hookBattleDispatcher():
    try:
        from gui.prb_control.dispatcher import _PrebattleDispatcher
        if getattr(_PrebattleDispatcher, '_offhangar_battle_hooked', False):
            LOG_DEBUG('dispatcher battle hook already installed')
            return
        # --- Кнопка всегда визуально активна в оффлайне (кликабельна). ---
        _orig_canPlayerDoAction = _PrebattleDispatcher.canPlayerDoAction
        def _patched_canPlayerDoAction(self):
            if _isOfflineMode():
                return (True, '')
            return _orig_canPlayerDoAction(self)
        _PrebattleDispatcher.canPlayerDoAction = _patched_canPlayerDoAction
        # --- doSelectAction: в оффлайне глушим целиком. Выбор режима идёт
        # через селектор (_CommandItem.select / _RandomQueueItem.select),
        # а бой стартует красной кнопкой напрямую — онлайн-логике сюда
        # ходить не даём и второй старт отсюда не ловим. ---
        if hasattr(_PrebattleDispatcher, 'doSelectAction'):
            _orig_doSelectAction = _PrebattleDispatcher.doSelectAction
            def _patched_doSelectAction(self, action):
                if _isOfflineMode():
                    LOG_DEBUG('dispatcher.doSelectAction suppressed (offline)')
                    return True
                return _orig_doSelectAction(self, action)
            _PrebattleDispatcher.doSelectAction = _patched_doSelectAction
        # --- doAction: клик по красной «В бой!» приходит сюда. В оффлайне
        # стартуем бой БЕЗ проверки actionName (у кнопки он часто None /
        # не совпадает с именем режима, из-за чего раньше клик был no-op).
        # Защита от двойного старта живёт в _startOfflineBattleFromButton
        # (флаги + off_battle._active()). ---
        _orig_doAction = _PrebattleDispatcher.doAction
        def _patched_doAction(self, action=None):
            if _isOfflineMode() and _isRoomAction(self, action):
                LOG_DEBUG('dispatcher.doAction -> native (rooms/historical) %s' % getattr(action, 'actionName', None))
                return _orig_doAction(self, action)
            if _isOfflineMode():
                actionName = getattr(action, 'actionName', None)
                LOG_DEBUG('dispatcher.doAction -> offline battle (actionName=%s)' % actionName)
                # карта, выбранная в окне "Демонстрация карты": ищем в action и диспетчере
                arenaTypeID = 0
                # Окно "Демонстрация карты" (DemonstratorWindow.onMapSelected) вызывает
                # doAction(PrebattleAction(None, mapID=<id типа арены>)). У PrebattleAction есть __slots__ и нет __dict__,
                # поэтому обход атрибутов (mapselect.probe) это поле не видел - читаем его напрямую.
                try:
                    arenaTypeID = int(getattr(action, 'mapID', 0) or 0)
                except Exception:
                    arenaTypeID = 0
                LOG_DEBUG('[MAP] doAction: action.mapID=%s' % arenaTypeID)
                if not arenaTypeID:
                    try:
                        from gui.mods.offhangar import mapselect
                        arenaTypeID = mapselect.probe([('action', action), ('dispatcher', self)]) or 0
                        LOG_DEBUG('[MAP] doAction: selected arenaTypeID=%s (probe)' % arenaTypeID)
                    except Exception:
                        LOG_CURRENT_EXCEPTION()
                _startOfflineBattleFromButton(arenaTypeID)
                return True
            return _orig_doAction(self, action)
        _PrebattleDispatcher.doAction = _patched_doAction
        _PrebattleDispatcher._offhangar_battle_hooked = True
        LOG_DEBUG('dispatcher battle hook installed')
    except Exception:
        LOG_CURRENT_EXCEPTION()

# основной server.py (главный загрузчик) — добавить рядом с остальными патчами

def _hookBattleSelectorItems():
    return True


def _tryHookBattleSelectorItems(tries=10):
    return


# ------------------------------------------------------------
# Регистрация и установка хуков
# ------------------------------------------------------------

try:
    from gui.mods.offhangar import battle as off_battle
    off_battle.register()
    from gui.mods.offhangar import shooting as off_shooting
    off_shooting.register()
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    from gui.mods.offhangar import fakeprb as off_fakeprb
    off_fakeprb.register()
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    from gui.mods.offhangar import tutor as off_tutor
    off_tutor.register()
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    _hookBattleDispatcher()
except Exception:
    LOG_CURRENT_EXCEPTION()

try:
    _tryHookBattleSelectorItems()
except Exception:
    LOG_CURRENT_EXCEPTION()

from account_helpers.Stats import Stats

_orig_Stats_onGetCacheResponse = Stats._Stats__onGetCacheResponse

def _patched_Stats_onGetCacheResponse(self, callback, resultID):
    if not _isOffline():
        return _orig_Stats_onGetCacheResponse(self, callback, resultID)
    if callback is not None:
        callback(AccountCommands.RES_SUCCESS, self._Stats__cache)

Stats._Stats__onGetCacheResponse = _patched_Stats_onGetCacheResponse

try:
    from gui.shared.utils.requesters.StatsRequesterr import StatsRequesterr as StatsRequester

    _orig_stats_getCacheValue = StatsRequester.getCacheValue

    def _patched_stats_getCacheValue(self, key, defaultValue=None):
        # В оффлайне форсим флаг кошелька, иначе actualGold/actualFreeXP
        # схлопываются в 0 через условие про wallet.useGold/useFreeXP.
        if _isOffline() and key == 'mayConsumeWalletResources':
            return 1
        return _orig_stats_getCacheValue(self, key, defaultValue)

    StatsRequester.getCacheValue = _patched_stats_getCacheValue
    LOG_DEBUG('StatsRequester.getCacheValue patched (offline wallet flag)')
except Exception:
    LOG_CURRENT_EXCEPTION()


# ---------------- WALLET-FIX ----------------
try:
    from gui.game_control.wallet import WalletController as _WC

    _orig_Wallet_start = _WC.start

    def _patched_Wallet_start(self):
        if not _isOffline():
            return _orig_Wallet_start(self)
        try:
            pl = BigWorld.player()
            LOG_DEBUG('Wallet.start offline: serverSettings type=%s' % type(getattr(pl, 'serverSettings', None)))
        except Exception:
            LOG_CURRENT_EXCEPTION()
        self._WalletController__useGold = True
        self._WalletController__useFreeXP = True
        self._WalletController__processStatus(self.STATUS.AVAILABLE, True)

    _WC.start = _patched_Wallet_start
    LOG_DEBUG('WalletController.start patched (offline)')
except Exception:
    LOG_CURRENT_EXCEPTION()
# --------------------------------------------


# ---------------- EXC-SWEEP ----------------
_EXC_WRAPPED = set()

def _wrapExcLogger(orig):
    def _w(*a, **k):
        try:
            import traceback as _tb
            doLog('[PY-EXC-FULL]', _tb.format_exc())
        except Exception:
            pass
        return orig(*a, **k)
    _w._offhangar_wrapped = True
    return _w

def _sweepExceptionLoggers():
    try:
        import sys
        for name, mod in list(sys.modules.items()):
            if mod is None or name == 'gui.mods.offhangar.logging':
                continue
            try:
                f = getattr(mod, 'LOG_CURRENT_EXCEPTION', None)
            except Exception:
                continue
            if f is None or getattr(f, '_offhangar_wrapped', False):
                continue
            if getattr(f, '__name__', '') not in ('LOG_CURRENT_EXCEPTION', '_p', '_patched'):
                continue
            try:
                setattr(mod, 'LOG_CURRENT_EXCEPTION', _wrapExcLogger(f))
            except Exception:
                pass
    except Exception:
        pass

_SWEEP_LEFT = [400]

def _sweepLoop():
    _sweepExceptionLoggers()
    _SWEEP_LEFT[0] -= 1
    if _SWEEP_LEFT[0] > 0:
        try:
            BigWorld.callback(1.5, _sweepLoop)
        except Exception:
            pass

_sweepLoop()
# -------------------------------------------

def fini():
    pass