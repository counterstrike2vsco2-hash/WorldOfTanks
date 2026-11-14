# Embedded file name: scripts/client/gui/shared/personality.py
import time, SoundGroups, BigWorld, MusicController
from gui.BattleContext import g_battleContext
from predefined_hosts import g_preDefinedHosts
from account_helpers.SettingsCache import g_settingsCache
from account_helpers.SettingsCore import g_settingsCore
from account_helpers.AccountValidator import AccountValidator
from gui.LobbyContext import g_lobbyContext
from gui.Scaleform.LogitechMonitor import LogitechMonitor
from gui.shared.ItemsCache import CACHE_SYNC_REASON
from helpers import isPlayerAccount
from adisp import process
from debug_utils import LOG_CURRENT_EXCEPTION
from PlayerEvents import g_playerEvents
from account_helpers import isPremiumAccount
from CurrentVehicle import g_currentVehicle
from ConnectionManager import connectionManager
from gui import SystemMessages, g_guiResetters, game_control
from gui.Scaleform.locale.MENU import MENU
from gui.Scaleform.locale.SYSTEM_MESSAGES import SYSTEM_MESSAGES
from gui.prb_control.dispatcher import g_prbLoader
from gui.shared import g_eventBus, g_itemsCache, g_eventsCache, events
from gui.ClientUpdateManager import g_clientUpdateManager
from gui.WindowsManager import g_windowsManager
from gui.Scaleform.Waiting import Waiting
from gui.shared.utils import ParametersCache
from gui.shared.utils.HangarSpace import g_hangarSpace
from gui.shared.utils.RareAchievementsCache import g_rareAchievesCache

@process
def onAccountShowGUI(ctx):
    global onCenterIsLongDisconnected
    g_lobbyContext.onAccountShowGUI(ctx)
    yield g_itemsCache.update(CACHE_SYNC_REASON.SHOW_GUI)
    yield g_eventsCache.update()
    yield g_settingsCache.update()
    if not g_itemsCache.isSynced():
        return
    code = yield AccountValidator().validate()
    if code > 0:
        from gui import DialogsInterface
        DialogsInterface.showDisconnect('#menu:disconnect/codes/%d' % code)
        return
    g_settingsCore.serverSettings.applySettings()
    game_control.g_instance.onAccountShowGUI(g_lobbyContext.getGuiCtx())
    accDossier = g_itemsCache.items.getAccountDossier()
    g_rareAchievesCache.request(accDossier.getRecord('rareAchievements'))
    MusicController.g_musicController.setAccountAttrs(g_itemsCache.items.stats.attributes)
    MusicController.g_musicController.play(MusicController.MUSIC_EVENT_LOBBY)
    MusicController.g_musicController.play(MusicController.AMBIENT_EVENT_LOBBY)
    premium = isPremiumAccount(g_itemsCache.items.stats.attributes)
    if g_hangarSpace.inited:
        g_hangarSpace.refreshSpace(premium)
    else:
        g_hangarSpace.init(premium)
    g_currentVehicle.init()
    g_windowsManager.onAccountShowGUI(g_lobbyContext.getGuiCtx())
    g_prbLoader.onAccountShowGUI(g_lobbyContext.getGuiCtx())
    SoundGroups.g_instance.enableLobbySounds(True)
    onCenterIsLongDisconnected(True)
    Waiting.hide('enter')
    return


def onAccountBecomeNonPlayer():
    g_itemsCache.clear()
    g_currentVehicle.destroy()
    g_hangarSpace.destroy()
    return


@process
def onAvatarBecomePlayer():
    yield g_settingsCache.update()
    g_settingsCore.serverSettings.applySettings()
    g_battleContext.createEnv()
    g_prbLoader.onAvatarBecomePlayer()
    game_control.g_instance.onAvatarBecomePlayer()
    Waiting.cancelCallback()
    return


def onAccountBecomePlayer():
    game_control.g_instance.onAccountBecomePlayer()
    return


@process
def onClientUpdate(diff):
    yield lambda callback: callback(None)
    if isPlayerAccount():
        yield g_itemsCache.update(CACHE_SYNC_REASON.CLIENT_UPDATE, diff)
        yield g_eventsCache.update(diff)
    g_clientUpdateManager.update(diff)
    return


def onShopResyncStarted():
    Waiting.show('sinhronize')
    return


@process
def onShopResync():
    yield g_itemsCache.update(CACHE_SYNC_REASON.SHOP_RESYNC)
    if not g_itemsCache.isSynced():
        Waiting.hide('sinhronize')
        return
    yield g_eventsCache.update()
    Waiting.hide('sinhronize')
    now = time.time()
    SystemMessages.g_instance.pushI18nMessage(SYSTEM_MESSAGES.SHOP_RESYNC, date=BigWorld.wg_getLongDateFormat(now), time=BigWorld.wg_getShortTimeFormat(now), type=SystemMessages.SM_TYPE.Information)
    return


def onCenterIsLongDisconnected(isLongDisconnected):
    isAvailable = not BigWorld.player().isLongDisconnectedFromCenter
    if isAvailable and not isLongDisconnected:
        SystemMessages.g_instance.pushI18nMessage(MENU.CENTERISAVAILABLE, type=SystemMessages.SM_TYPE.Information)
    elif not isAvailable:
        SystemMessages.g_instance.pushI18nMessage(MENU.CENTERISUNAVAILABLE, type=SystemMessages.SM_TYPE.Warning)
    return


def onIGRTypeChanged(roomType, xpFactor):
    g_lobbyContext.updateGuiCtx({'igrData': {'roomType': roomType, 
                   'igrXPFactor': xpFactor}})
    return


@process
def onAppStarted(args):
    yield lambda callback: callback(None)
    if not connectionManager.isConnected():
        return
    return



def _ldlog(msg):
    try:
        import time
        f = open('offhangar_loader.log', 'a')
        f.write('%s %s\n' % (time.strftime('%H:%M:%S'), msg))
        f.flush()
        f.close()
    except Exception:
        pass


def _loadMods():
    # OFFHANGAR-PORT: в 0.9.0 нет штатного загрузчика gui/mods
    _ldlog('loader start')
    try:
        import os, glob
        names = set()
        try:
            import ResMgr
            sec = ResMgr.openSection('scripts/client/gui/mods')
            if sec is not None:
                for n in sec.keys():
                    if n.startswith('mod_') and (n.endswith('.pyc') or n.endswith('.py')):
                        names.add(n.rsplit('.', 1)[0])
        except Exception:
            LOG_CURRENT_EXCEPTION()
        for p in glob.glob('./res_mods/*/scripts/client/gui/mods/mod_*.py*'):
            n = os.path.basename(p)
            if n.endswith('.pyc') or n.endswith('.py'):
                names.add(n.rsplit('.', 1)[0])
        _ldlog('found mods: %s' % sorted(names))
        for n in sorted(names):
            try:
                __import__('gui.mods.' + n)
                print '[MODLOADER] loaded', n
                _ldlog('loaded ' + n)
            except Exception:
                import traceback
                print '[MODLOADER] FAILED', n
                _ldlog('FAILED ' + n + '\n' + traceback.format_exc())
                LOG_CURRENT_EXCEPTION()

    except Exception:
        LOG_CURRENT_EXCEPTION()


def init(loadingScreenGUI=None):
    global onAccountBecomeNonPlayer
    global onAccountBecomePlayer
    global onAccountShowGUI
    global onAvatarBecomePlayer
    global onIGRTypeChanged
    global onShopResync
    global onShopResyncStarted
    g_playerEvents.onAccountShowGUI += onAccountShowGUI
    g_playerEvents.onAccountBecomeNonPlayer += onAccountBecomeNonPlayer
    g_playerEvents.onAccountBecomePlayer += onAccountBecomePlayer
    g_playerEvents.onAvatarBecomePlayer += onAvatarBecomePlayer
    g_playerEvents.onClientUpdated += onClientUpdate
    g_playerEvents.onShopResyncStarted += onShopResyncStarted
    g_playerEvents.onShopResync += onShopResync
    g_playerEvents.onCenterIsLongDisconnected += onCenterIsLongDisconnected
    g_playerEvents.onIGRTypeChanged += onIGRTypeChanged
    game_control.g_instance.init()
    from gui.Scaleform import SystemMessagesInterface
    SystemMessages.g_instance = SystemMessagesInterface.SystemMessagesInterface()
    SystemMessages.g_instance.init()
    ParametersCache.g_instance.init()
    if loadingScreenGUI:
        loadingScreenGUI.script.active(False)
    g_prbLoader.init()
    LogitechMonitor.init()
    g_itemsCache.init()
    g_settingsCache.init()
    g_settingsCore.init()
    g_eventsCache.init()
    _loadMods()
    return


def start():
    g_eventBus.addListener(events.GUICommonEvent.APP_STARTED, onAppStarted)
    g_windowsManager.start()
    return


def fini():
    Waiting.close()
    g_eventBus.removeListener(events.GUICommonEvent.APP_STARTED, onAppStarted)
    game_control.g_instance.fini()
    g_settingsCore.fini()
    g_settingsCache.fini()
    g_eventsCache.fini()
    g_itemsCache.fini()
    LogitechMonitor.destroy()
    g_windowsManager.destroy()
    SystemMessages.g_instance.destroy()
    g_eventBus.clear()
    g_prbLoader.fini()
    g_playerEvents.onIGRTypeChanged -= onIGRTypeChanged
    g_playerEvents.onAccountShowGUI -= onAccountShowGUI
    g_playerEvents.onAccountBecomeNonPlayer -= onAccountBecomeNonPlayer
    g_playerEvents.onAvatarBecomePlayer -= onAvatarBecomePlayer
    g_playerEvents.onAccountBecomePlayer -= onAccountBecomePlayer
    g_playerEvents.onClientUpdated -= onClientUpdate
    g_playerEvents.onShopResyncStarted -= onShopResyncStarted
    g_playerEvents.onShopResync -= onShopResync
    g_playerEvents.onCenterIsLongDisconnected -= onCenterIsLongDisconnected
    return


def onConnected():
    return


def onDisconnected():
    if game_control.g_instance.roaming.isInRoaming():
        g_preDefinedHosts.savePeripheryTL(connectionManager.peripheryID)
    g_prbLoader.onDisconnected()
    game_control.g_instance.onDisconnected()
    g_itemsCache.clear()
    g_lobbyContext.clear()
    return


def onRecreateDevice():
    for c in g_guiResetters:
        try:
            c()
        except Exception:
            LOG_CURRENT_EXCEPTION()

    return



# okay decompiling personality.pyc
