# -- coding: utf-8 --
import functools
import cPickle
import AccountCommands
import BigWorld
from gui.mods.offhangar._constants import CHAT_ACTION_DATA
from gui.mods.offhangar.logging import *
from debug_utils import LOG_CURRENT_EXCEPTION

BASE_REQUESTS = {}

_CMD_NAMES = {}
_CMD_QUIET = ('CMD_SYNC_DATA', 'CMD_SYNC_SHOP', 'CMD_SYNC_DOSSIERS', 'CMD_LOG_CLIENT_UX_EVENTS', 'CMD_LOG_CLIENT_XMPP_EVENTS')

def _cmdName(cmd):
    if not _CMD_NAMES:
        for attr in dir(AccountCommands):
            if attr.startswith('CMD_'):
                _CMD_NAMES.setdefault(getattr(AccountCommands, attr, None), attr)
    return _CMD_NAMES.get(cmd, str(cmd))


class FakeServer(object):
    def call(self, *args, **kwargs):
        if not self.__isMuted:
            LOG_DEBUG('%s ( %s, %s )' % (self.__name, args, kwargs))

    def __init__(self, name='Server', isMuted=False):
        super(FakeServer, self).__init__()
        self.__isMuted = isMuted
        self.__name = name

    def vehicle_teleport(self, position, yaw):
        # обучение переставляет танк по сценарию (Avatar.teleportVehicle -> base.vehicle_teleport)
        try:
            from gui.mods.offhangar import battle as off_battle
            off_battle.teleportVehicleByID(getattr(BigWorld.player(), 'playerVehicleID', 0), position, yaw)
        except Exception:
            LOG_CURRENT_EXCEPTION()

    def __getattr__(self, name):
        try:
            return super(FakeServer, self).__getattribute__(name)
        except AttributeError:
            return FakeServer(name='%s.%s' % (self.__name, name),
                              isMuted=self.__isMuted)

    def chatCommandFromClient(self, requestID, action, channelID,
                              int64Arg, int16Arg, stringArg1, stringArg2):
        # В бою чат-сервера нет: ответ internalError выводился красной строкой
        # "Внутренняя ошибка, попробуйте позже" в боевом чате. Молчим.
        try:
            from gui.mods.offhangar import battle as off_battle
            if off_battle._active():
                return
        except Exception:
            pass
        d = CHAT_ACTION_DATA.copy()
        d['requestID'] = requestID
        d['action'] = action
        BigWorld.player().onChatAction(d)

    def doCmdStr(self, requestID, cmd, str):
        self.__doCmd(requestID, cmd, str)

    def doCmdIntStr(self, requestID, cmd, int, str):
        self.__doCmd(requestID, cmd, int, str)

    def doCmdInt3(self, requestID, cmd, int1, int2, int3):
        self.__doCmd(requestID, cmd, int1, int2, int3)

    def doCmdInt4(self, requestID, cmd, int1, int2, int3, int4):
        self.__doCmd(requestID, cmd, int1, int2, int3, int4)

    def doCmdInt2Str(self, requestID, cmd, int1, int2, str):
        self.__doCmd(requestID, cmd, int1, int2, str)

    def doCmdIntArr(self, requestID, cmd, arr):
        self.__doCmd(requestID, cmd, arr)

    def doCmdIntArrStrArr(self, requestID, cmd, intArr, strArr):
        self.__doCmd(requestID, cmd, intArr, strArr)

    def __call__(self, *args, **kwargs):
        return 0

    @staticmethod
    def _deliverResponse(player, requestID, resultID, errorStr, ext):
        """Доставляет ответ тем методом, который реально есть у player.
        В бою player=Avatar без onCmdResponseExt -> ответ молча не
        доставляется (UI ангара в бою закрыт, ext ему не нужен).
        Никакого AttributeError, никакого краша миграции настроек."""
        if ext is not None and callable(getattr(player, 'onCmdResponseExt', None)):
            cb = functools.partial(player.onCmdResponseExt, requestID,
                                   resultID, errorStr, cPickle.dumps(ext))
            BigWorld.callback(0.0, cb)
            return
        if callable(getattr(player, 'onCmdResponse', None)):
            cb = functools.partial(player.onCmdResponse, requestID,
                                   resultID, errorStr)
            BigWorld.callback(0.0, cb)

    def __doCmd(self, requestID, cmd, *args):
        cmdCall = BASE_REQUESTS.get(cmd)
        if cmdCall:
            try:
                requestID, resultID, errorStr, ext = cmdCall(requestID, *args)
                try:
                    _n = _cmdName(cmd)
                    if _n not in _CMD_QUIET:
                        LOG_DEBUG('[CMD] %s args=%s -> res=%s err=%s' % (_n, repr(args)[:160], resultID, errorStr))
                except Exception:
                    pass
            except Exception as e:
                LOG_ERROR('cmd failed', cmd, str(e))
                requestID, resultID, errorStr, ext = (requestID, AccountCommands.RES_FAILURE, '', None)
        else:
            name = None
            for attr in dir(AccountCommands):
                if attr.startswith('CMD_') and getattr(AccountCommands, attr, None) == cmd:
                    name = attr
                    break
            LOG_DEBUG('Server.requestFail', requestID, cmd, name, args)
            requestID, resultID, errorStr, ext = (requestID, AccountCommands.RES_FAILURE, '', None)
        try:
            player = BigWorld.player()
        except Exception:
            player = None
        if player is not None:
            self._deliverResponse(player, requestID, resultID, errorStr, ext)
