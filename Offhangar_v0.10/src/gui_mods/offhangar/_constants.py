# -- coding: utf-8 --
import time
import cPickle
from chat_shared import CHAT_RESPONSES


OFFLINE_SERVER_ADDRESS = 'offline.loc'
OFFLINE_DEFAULT_NICKNAME = 'Player'
OFFLINE_USER_FILE = 'offline_user.txt'


_SETTING_KEYS = ('nickname', 'nick', 'name', 'credits', 'gold', 'freexp', 'rewardmultiplier', 'xpmultiplier',
                 'creditsmultiplier', 'freexpmultiplier', 'enemybots', 'allybots', 'spotting',
                 'moduledamage', 'crewdamage', 'fire', 'equipment')


def _normKey(key):
    return key.strip().lower().replace('_', '').replace('-', '').replace(' ', '')


def readOfflineSettings():
    """Читает offline_user.txt (папка игры или res_mods). Возвращает словарь {ключ без регистра/подчёркиваний: строка}.
    Строки "ключ = значение" - настройки; одна обычная строка без "=" - ник (как раньше).
    Читается заново при каждом вызове, поэтому изменения подхватываются без пересборки."""
    import os
    res = {}
    for path in (OFFLINE_USER_FILE, os.path.join('res_mods', OFFLINE_USER_FILE)):
        try:
            if not os.path.isfile(path):
                continue
            f = open(path, 'rb')
            try:
                raw = f.read()
            finally:
                f.close()
            if raw.startswith(b'\xef\xbb\xbf'):
                raw = raw[3:]
            try:
                text = raw.decode('utf-8')
            except Exception:
                text = raw.decode('cp1251', 'replace')
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    nk = _normKey(key)
                    if nk in _SETTING_KEYS:
                        if nk in ('nick', 'name'):
                            nk = 'nickname'
                        res.setdefault(nk, value.strip())
                        continue
                if 'nickname' not in res:
                    res['nickname'] = line
            return res
        except Exception:
            pass
    return res


def parseNumber(raw, default=None, integer=True):
    """'1000000' -> ('set', 1000000); '+500000' -> ('add', 500000); мусор -> default."""
    try:
        text = str(raw).strip().replace(',', '').replace(' ', '').replace('_', '')
        if not text:
            return default
        mode = 'set'
        if text[0] == '+':
            mode = 'add'
            text = text[1:]
        val = int(float(text)) if integer else float(text)
        return (mode, val)
    except Exception:
        return default


def getSettingInt(key, default, lo, hi):
    r = parseNumber(readOfflineSettings().get(key), None)
    if r is None:
        return default
    return max(lo, min(hi, r[1]))


def getSettingFloat(key, default):
    r = parseNumber(readOfflineSettings().get(key), None, integer=False)
    if r is None or r[1] < 0:
        return default
    return r[1]


def _readOfflineNickname():
    """Ник игрока берётся из offline_user.txt (в папке игры).
    Первая непустая строка без "=", не начинающаяся с '#'; допустим формат "nickname=Имя".
    Кодировка: UTF-8 (с BOM или без) или cp1251. Длина - не более 24 символов."""
    line = readOfflineSettings().get('nickname', '').strip()[:24]
    if line:
        try:
            return line.encode('utf-8')
        except Exception:
            pass
    return OFFLINE_DEFAULT_NICKNAME


OFFLINE_NICKNAME = _readOfflineNickname()
# логин остаётся фиксированным, чтобы нестандартные символы в нике ничего не ломали
OFFLINE_LOGIN = OFFLINE_DEFAULT_NICKNAME + '@' + OFFLINE_SERVER_ADDRESS
OFFLINE_PWD = '1'
OFFLINE_DBID = 1000001


OFFLINE_GUI_CTX = cPickle.dumps({
    'databaseID': OFFLINE_DBID,
    'name': OFFLINE_NICKNAME,
    'logUXEvents': False,

    # Anti-online-game-addiction / playtime related
    'aogasStartedAt': 0,
    'sessionStartedAt': int(time.time()),
    'isAogasEnabled': False,

    'collectUiStats': False,
    'isLongDisconnectedFromCenter': False,

    # Extra safety, if client reads this from ctx
    'playLimits': ((0, ''), (0, '')),
    'dailyPlayHours': [0, 0, 0, 0, 0, 0, 0],
}, cPickle.HIGHEST_PROTOCOL)


OFFLINE_QUESTS_PROGRESS = {
    'quests': {},
    'tokens': {},
    'potapovQuests': {
        'compDescr': '',
        'slots': 3,
        'selected': set(),
        'rewards': {},
        'lastIDs': {}
    },
    'history': {},
    'sortie': {},
    'fort_battles': {}
}


OFFLINE_SERVER_SETTINGS = {
    'file_server': {},

    'roaming': ([], [], [], []),
    'isPotapovQuestEnabled': True,

    'regional_settings': {
        # Оставляем оба варианта написания, потому что в коде есть weak/week
        'starting_day_of_a_new_week': 0,
        'starting_day_of_a_new_weak': 0,

        'starting_time_of_a_new_game_day': 0,
        'starting_time_of_a_new_day': 0,
    },

    'xmpp_enabled': False,
    'xmpp_port': 0,
    'xmpp_host': '',
    'xmpp_muc_enabled': False,
    'xmpp_muc_services': [],
    'xmpp_resource': '',
    'xmpp_bosh_connections': [],
    'xmpp_connections': [],
    'xmpp_alt_connections': [],

    'voipDomain': '',
    'voipUserDomain': '',

    'wallet': (1, 1),
    'isTutorialEnabled': True,   # кнопка 'Боевое обучение' в ангаре; автозапуск при входе отключён в tutor._patchTutorialButton
    'newbieBattlesCount': 0,
    'reCaptchaParser': '',

    # Дополнительные ключи против лимитов
    'aogas_enabled': False,
    'isAogasEnabled': False,
    'playtime_limit': 0,
    'playLimits': ((0, ''), (0, '')),
    'dailyPlayHours': [0, 0, 0, 0, 0, 0, 0],
}


CHAT_ACTION_DATA = {
    'requestID': None,
    'action': None,
    'actionResponse': CHAT_RESPONSES.internalError.index(),
    'time': 0,
    'sentTime': 0,
    'channel': 0,
    'originator': 0,
    'originatorNickName': '',
    'group': 0,
    'data': {},
    'flags': 0
}

OFFLINE_BATTLE_MAP_POOL = [
    # '01_karelia',
    # '02_malinovka',
]

OFFLINE_BATTLE_DEFAULT_GAMEPLAY = 'ctf'

OFFLINE_BATTLE_PERIOD_LENGTH = 900

REQUEST_CALLBACK_TIME = 0.5