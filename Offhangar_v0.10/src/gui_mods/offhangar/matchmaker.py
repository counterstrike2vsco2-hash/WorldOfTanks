# -*- coding: utf-8 -*-
"""Подбор состава команд (матчмейкер) на основе Offhangar Matchmaker 0.8.2.

Система уровней боёв: у каждого танка есть список уровней боёв, в которые он попадает; мод собирает две
команды по 15 машин с балансом по уровням/классам и учётом редкости (см. mm_core.py).

Конфиг: offhangar_user/balancer.json (папка игры). Создаётся при первом бое, перечитывается перед каждым боем.
Подробное описание полей - CONFIG_RU.md из архива Offhangar Matchmaker 0.8.2.
По умолчанию САУ у ботов отключены (selection.spg.disabled = true): ИИ ботов стреляет только прямой наводкой.
"""
import json
import os
import random

import BigWorld
from debug_utils import LOG_CURRENT_EXCEPTION

from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE, LOG_ERROR

CONFIG_DIR = 'offhangar_user'
CONFIG_NAME = 'balancer.json'

ALLOW_SPG = True               # артиллерия в боях (перекрывает selection.spg.disabled из balancer.json)
MAX_SPG_PER_TEAM = 1           # один слот матчмейкера; остальные САУ выбираются случайно в bots.py (общий максимум 5)
EXCLUDED_FILE = 'excluded_vehicles.txt'     # машины, которые никогда не попадают в бой ботам (папка offhangar_user)
VEHICLE_LIST_FILE = 'all_vehicles.txt'      # справочник: все машины клиента (создаётся сам)

_DEFAULT_EXCLUDED = u'''# Машины, которых не должно быть в бою (боты их не берут). Одна машина на строку, # - комментарий.
# Формат: нация:Имя - как в справочнике all_vehicles.txt (тот же каталог), например  ussr:T-50-2
# Можно писать и просто имя без нации (подойдут все нации). Файл перечитывается перед каждым боем.
# Твоя собственная машина никогда не исключается.
ussr:T-50-2
'''

_DEFAULT_JSON = r'''{
  "schema_version": 5,
  "experimental_modes": {
    "forced_battle_tier": {
      "enabled": false,
      "battle_tiers": [
        8,
        9,
        10
      ]
    }
  },
  "excluded": [
    "ussr:Observer",
    "ussr:KV",
    "usa:T23",
    "china:Ch01_Type59_Gold"
  ],
  "default_offsets": [
    -2,
    -1,
    0,
    1,
    2
  ],
  "low_tier_preferred_max": 2,
  "low_tier_exceptions": [
    "usa:T2_lt"
  ],
  "spg": {
    "offsets_by_level": {
      "2": [
        1,
        2
      ],
      "3": [
        1,
        2,
        3
      ],
      "4": [
        1,
        2,
        3,
        4
      ],
      "5": [
        2,
        3,
        4
      ],
      "6": [
        3,
        4
      ],
      "7": [
        3,
        4,
        5
      ],
      "8": [
        3,
        4
      ]
    },
    "exceptions": {
      "germany:Bison_I": [
        1,
        2,
        3
      ],
      "france:_105_leFH18B2": [
        1,
        2,
        3
      ]
    }
  },
  "scouts": {
    "vehicles": [
      "usa:M5_Stuart",
      "ussr:A-20",
      "germany:Pz38_NA",
      "ussr:T-50",
      "germany:VK1602",
      "france:ELC_AMX",
      "china:Ch02_Type62",
      "france:AMX_12t",
      "france:AMX_13_75",
      "france:AMX_13_90",
      "ussr:T_50_2",
      "germany:VK2801",
      "usa:M24_Chaffee",
      "usa:T21",
      "usa:T71"
    ],
    "offsets_by_level": {
      "4": [
        0,
        1,
        2,
        3,
        4
      ],
      "5": [
        2,
        3,
        4,
        5,
        6,
        7
      ],
      "6": [
        1,
        2,
        3,
        4,
        5
      ],
      "7": [
        1,
        2,
        3,
        4
      ],
      "8": [
        1,
        2,
        3,
        4
      ]
    },
    "exceptions": {
      "ussr:T-50": [
        1,
        2,
        3,
        4,
        5
      ],
      "germany:VK1602": [
        1,
        2,
        3,
        4,
        5
      ],
      "france:ELC_AMX": [
        1,
        2,
        3,
        4
      ],
      "china:Ch02_Type62": [
        1,
        2,
        3
      ]
    }
  },
  "battle_tier_groups": {
    "preferential": {
      "offsets": [
        0,
        1
      ],
      "vehicles": [
        "france:B1",
        "ussr:M3_Stuart_LL",
        "ussr:BT-SV",
        "ussr:A-32",
        "germany:PzII_J",
        "ussr:Matilda_II_LL",
        "ussr:Churchill_LL",
        "ussr:KV-220",
        "ussr:KV-220_action",
        "usa:T14",
        "usa:M4A2E4",
        "germany:PzIV_Hydro",
        "germany:PzV_PzIV",
        "germany:PzV_PzIV_ausf_Alfa",
        "germany:Panther_M10",
        "usa:Ram-II",
        "usa:T26_E4_SuperPershing",
        "uk:GB68_Matilda_Black_Prince",
        "china:Ch01_Type59",
        "ussr:Object252",
        "ussr:KV-5",
        "france:FCM_50t",
        "uk:GB63_TOG_II",
        "china:Ch03_WZ-111",
        "ussr:SU_85I",
        "germany:JagdTiger_SdKfz_185"
      ]
    },
    "exclusive": {
      "offsets": [
        0
      ],
      "vehicles": [
        "germany:B-1bis_captured",
        "ussr:Valentine_LL"
      ]
    }
  },
  "overrides": {},
  "selection": {
    "nation_weights": {
      "ussr": 1,
      "germany": 1,
      "usa": 1,
      "france": 1,
      "uk": 1,
      "china": 1
    },
    "class_weights": {
      "heavyTank": 1,
      "mediumTank": 1,
      "AT-SPG": 1,
      "lightTank": 1,
      "SPG": 1
    },
    "vehicle_weights": {
      "ussr:KV-220": 0.016,
      "germany:PzV_PzIV_ausf_Alfa": 0.004
    },
    "rarity_groups": {
      "common_premium": {
        "weight": 0.95,
        "vehicles": [
          "ussr:T-127",
          "germany:S35_captured",
          "germany:T-25",
          "usa:T26_E4_SuperPershing",
          "ussr:Churchill_LL",
          "ussr:Object252",
          "germany:Lowe",
          "usa:T34_hvy",
          "france:FCM_50t",
          "ussr:SU122_44",
          "germany:DickerMax"
        ]
      },
      "uncommon_premium": {
        "weight": 0.75,
        "vehicles": [
          "germany:T-15",
          "usa:M22_Locust",
          "usa:T2_lt",
          "ussr:Matilda_II_LL",
          "germany:PzIV_schmalturm",
          "germany:Panther_M10",
          "usa:M4A2E4",
          "usa:Ram-II",
          "usa:T14",
          "uk:GB68_Matilda_Black_Prince",
          "ussr:KV-5",
          "uk:GB71_AT_15A",
          "germany:JagdTiger_SdKfz_185",
          "ussr:SU100Y",
          "france:FCM_36Pak40",
          "france:_105_leFH18B2"
        ]
      },
      "rare": {
        "weight": 0.4,
        "vehicles": [
          "ussr:Tetrarch_LL",
          "ussr:Valentine_LL",
          "germany:H39_captured",
          "usa:T1_E6",
          "china:Ch02_Type62",
          "china:Ch01_Type59",
          "germany:B-1bis_captured",
          "usa:M6A2E1",
          "uk:GB63_TOG_II",
          "china:Ch03_WZ-111",
          "ussr:SU_85I"
        ]
      },
      "very_rare": {
        "weight": 0.06,
        "vehicles": [
          "ussr:BT-SV",
          "ussr:M3_Stuart_LL",
          "ussr:A-32",
          "ussr:KV-220_action",
          "germany:PzII_J",
          "germany:PzIV_Hydro",
          "germany:PzV_PzIV",
          "usa:MTLS-1G14"
        ]
      }
    },
    "spg": {
      "cap": 0,
      "disabled": false,
      "weights": [
        1,
        1.2,
        1,
        1.2,
        0.75,
        1.2,
        0.25,
        1.2
      ],
      "tail_decay": 1
    },
    "scout": {
      "cap": 0,
      "disabled": false,
      "weights": [
        1,
        1,
        1,
        1,
        0.22,
        1,
        0.11,
        1
      ],
      "tail_decay": 1
    }
  },
  "balance": {
    "tier_growth": 1.6,
    "class_weights": {
      "heavyTank": 1.1,
      "mediumTank": 0.95,
      "AT-SPG": 1,
      "lightTank": 0.9
    },
    "special_scale": {
      "spg": 1,
      "scout": 1
    },
    "special_reference_level": {
      "spg": 6,
      "scout": 6
    },
    "ordinary_reference_level": 8,
    "total_weight": 1,
    "class_weight": 0.6,
    "concentration_weight": 1.2,
    "special_weight": 0.6,
    "tier_differences": [
      1,
      2
    ]
  },
  "search": {
    "pool_attempts": 4,
    "repairs": 5,
    "restarts": 4,
    "swap_passes": 18,
    "max_seconds": 3,
    "max_allocations": 20000
  }
}'''

_CLASSES = frozenset(('heavyTank', 'mediumTank', 'lightTank', 'AT-SPG', 'SPG'))
_CATALOG_CACHE = {}


def _configPath():
    return os.path.join(CONFIG_DIR, CONFIG_NAME)


def excludedNames():
    """Имена из offhangar_user/excluded_vehicles.txt. Если файла нет - создаём со значениями по умолчанию."""
    path = os.path.join(CONFIG_DIR, EXCLUDED_FILE)
    text = _DEFAULT_EXCLUDED
    try:
        if not os.path.isfile(path):
            if not os.path.isdir(CONFIG_DIR):
                os.makedirs(CONFIG_DIR)
            f = open(path, 'wb')
            try:
                f.write(_DEFAULT_EXCLUDED.replace(u'\r\n', u'\n').encode('utf-8'))
            finally:
                f.close()
        f = open(path, 'rb')
        try:
            raw = f.read()
        finally:
            f.close()
        try:
            text = raw.decode('utf-8-sig')
        except Exception:
            text = raw.decode('cp1251', 'replace')
    except Exception:
        LOG_CURRENT_EXCEPTION()
    names = set()
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            try:
                names.add(str(line))
            except Exception:
                pass          # имена машин - только латиница; строки с кириллицей пропускаем
    return names


def isExcluded(vehName, names=None):
    """vehName - вида 'ussr:T-50-2'. В файле можно указывать с нацией или без."""
    if names is None:
        names = excludedNames()
    return vehName in names or vehName.split(':', 1)[-1] in names


def _dumpVehicleList(rows):
    """Справочник всех машин клиента: удобно копировать имена в excluded_vehicles.txt."""
    try:
        path = os.path.join(CONFIG_DIR, VEHICLE_LIST_FILE)
        if os.path.isfile(path):
            return
        if not os.path.isdir(CONFIG_DIR):
            os.makedirs(CONFIG_DIR)
        lines = []
        for r in sorted(rows, key=lambda r: (r['name'].split(':', 1)[0], -int(r['level']), r['name'])):
            lines.append('%-34s tier %-2d %s' % (r['name'], int(r['level']), r['vehicle_class']))
        f = open(path, 'wb')
        try:
            f.write(u'# Все машины клиента: имя, уровень, класс. Имена копируй в excluded_vehicles.txt\n'.encode('utf-8'))
            f.write('\n'.join(lines) + '\n')
        finally:
            f.close()
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _loadConfig():
    path = _configPath()
    if not os.path.isfile(path):
        try:
            if not os.path.isdir(CONFIG_DIR):
                os.makedirs(CONFIG_DIR)
            f = open(path, 'wb')
            try:
                f.write(_DEFAULT_JSON.replace('\r\n', '\n').encode('utf-8'))
            finally:
                f.close()
            LOG_NOTE('[MM] default config created: %s' % path)
        except Exception:
            LOG_CURRENT_EXCEPTION()
    try:
        f = open(path, 'rb')
        try:
            return json.loads(f.read().decode('utf-8-sig'))
        finally:
            f.close()
    except Exception:
        LOG_ERROR('[MM] cannot read %s - using built-in defaults' % path)
        LOG_CURRENT_EXCEPTION()
        return json.loads(_DEFAULT_JSON)


def _catalogRows():
    """Все танки клиента: строки g_list + класс. Возвращает (rows, name -> (nationID, vehID))."""
    if 'rows' in _CATALOG_CACHE:
        return _CATALOG_CACHE['rows'], _CATALOG_CACHE['ids']
    from items import vehicles
    import nations
    rows = []
    ids = {}
    for nation in nations.AVAILABLE_NAMES:
        nationID = nations.INDICES[nation]
        for source in vehicles.g_list.getList(nationID).values():
            row = dict(source)
            tags = _CLASSES.intersection(row['tags'])
            if len(tags) != 1:
                continue
            row['vehicle_class'] = list(tags)[0]
            rows.append(row)
            ids[row['name']] = (nationID, row['id'])
    _CATALOG_CACHE['rows'] = rows
    _CATALOG_CACHE['ids'] = ids
    return rows, ids


def _sanitize(cfg, rows):
    """Конфиг писался под 0.8.2: убираем из него машины, которых нет в этом клиенте, и правила, которые к ним не подходят."""
    byName = dict((r['name'], r) for r in rows)
    have = set(byName)
    nations_ = set(n.split(':', 1)[0] for n in have)

    def keep(names):
        return [n for n in names if n in have]

    def keepKeys(d):
        return dict((k, v) for k, v in d.items() if k in have)

    cfg['excluded'] = keep(cfg.get('excluded', []))
    cfg['low_tier_exceptions'] = keep(cfg.get('low_tier_exceptions', []))
    cfg['overrides'] = keepKeys(cfg.get('overrides', {}))

    spg = cfg['spg']
    spg['exceptions'] = dict((k, v) for k, v in spg.get('exceptions', {}).items()
                             if k in have and byName[k]['vehicle_class'] == 'SPG')

    sc = cfg['scouts']
    offs = sc.get('offsets_by_level', {})
    exc = sc.get('exceptions', {})
    scouts = []
    for n in sc.get('vehicles', []):
        r = byName.get(n)
        if r is None or r['vehicle_class'] != 'lightTank':
            continue
        if n not in exc and str(int(r['level'])) not in offs:
            continue
        if n in cfg['overrides']:
            continue
        scouts.append(n)
    sc['vehicles'] = scouts
    sc['exceptions'] = dict((k, v) for k, v in exc.items() if k in scouts)

    for g in cfg.get('battle_tier_groups', {}).values():
        g['vehicles'] = keep(g.get('vehicles', []))
    sel = cfg['selection']
    sel['vehicle_weights'] = keepKeys(sel.get('vehicle_weights', {}))
    for g in sel.get('rarity_groups', {}).values():
        g['vehicles'] = keep(g.get('vehicles', []))
    sel['nation_weights'] = dict((k, v) for k, v in sel.get('nation_weights', {}).items() if k in nations_)
    return cfg


def buildRoster(playerName, allyCount, enemyCount):
    """Возвращает ({1: [(nationID, vehID), ...] союзники, 2: [...] враги}, текст-описание) или None при неудаче."""
    try:
        from gui.mods.offhangar import mm_core as core
        rows, ids = _catalogRows()
        _dumpVehicleList(rows)
        cfg = _sanitize(_loadConfig(), rows)
        if ALLOW_SPG:
            spgCfg = cfg.get('selection', {}).get('spg')
            if spgCfg is not None:
                spgCfg['disabled'] = False
                spgCfg['cap'] = MAX_SPG_PER_TEAM
        catalog = core.prepare_catalog(rows, cfg)
        if playerName not in catalog or playerName in cfg['excluded']:
            # машина игрока не участвует в системе уровней (премиум/особая) - берём ближайшую по уровню роль обычного танка
            LOG_NOTE('[MM] player vehicle %s is not in the matchmaker catalog' % playerName)
            return None
        names = excludedNames()
        extra = [n for n in catalog if n != playerName and isExcluded(n, names)]
        if extra:
            cfg['excluded'] = sorted(set(cfg['excluded']) | set(extra))
            LOG_DEBUG('[MM] excluded by %s: %s' % (EXCLUDED_FILE, extra))
        seed = random.getrandbits(32)
        generator = core.generate
        forced = cfg.get('experimental_modes', {}).get('forced_battle_tier', {})
        if forced.get('enabled', False):
            from gui.mods.offhangar import mm_forced
            generator = mm_forced.generate_forced
        result = generator(catalog, cfg, playerName, seed=seed, size=15)
        allies = [ids[n] for n in result['teams'][0][1:]]
        enemies = [ids[n] for n in result['teams'][1]]
        info = 'battle tier %s, seed %s, score %.3f, repairs %s, %.2fs' % (
            result['battle_tier'], seed, result['score'], result['repairs'], result['elapsed'])
        LOG_NOTE('[MM] roster ok: %s' % info)
        LOG_DEBUG('[MM] allies: %s' % (result['teams'][0][1:],))
        LOG_DEBUG('[MM] enemies: %s' % (result['teams'][1],))
        return {1: allies[:allyCount], 2: enemies[:enemyCount]}, info
    except Exception:
        LOG_ERROR('[MM] roster generation failed - falling back to simple random picks')
        LOG_CURRENT_EXCEPTION()
        return None
