/* ============================================================
   WoT 0.9.0 browser — фанатские танчики на canvas
   Ангар -> В бой! -> бой с ботами -> результат
   ============================================================ */
'use strict';

// ==================== ДАННЫЕ ТАНКОВ ====================
const TANKS = {
  ms1: {
    key: 'ms1', name: 'МС-1', cls: 'Лёгкий танк',
    hp: 130, speed: 165, revSpeed: 75, accel: 260, rot: 2.2, turret: 2.9,
    dmg: 26, reload: 2.0, shellSpeed: 800,
    hullW: 40, hullH: 26, gunLen: 30, turretR: 10, casemate: false, arc: 0,
    color: '#7a8a55'
  },
  t34: {
    key: 't34', name: 'Т-34-85', cls: 'Средний танк',
    hp: 400, speed: 135, revSpeed: 60, accel: 190, rot: 1.7, turret: 2.4,
    dmg: 110, reload: 4.2, shellSpeed: 830,
    hullW: 48, hullH: 30, gunLen: 42, turretR: 13, casemate: false, arc: 0,
    color: '#6f8049'
  },
  kv2: {
    key: 'kv2', name: 'КВ-2', cls: 'Тяжёлый танк',
    hp: 650, speed: 85, revSpeed: 42, accel: 130, rot: 1.2, turret: 1.3,
    dmg: 320, reload: 8.5, shellSpeed: 650,
    hullW: 54, hullH: 34, gunLen: 36, turretR: 17, casemate: false, arc: 0,
    color: '#66753f'
  },
  isu: {
    key: 'isu', name: 'ИСУ-152', cls: 'ПТ-САУ (рули корпусом!)',
    hp: 750, speed: 80, revSpeed: 40, accel: 120, rot: 1.35, turret: 0,
    dmg: 420, reload: 10.0, shellSpeed: 660,
    hullW: 56, hullH: 34, gunLen: 46, turretR: 0, casemate: true, arc: 0.42,
    color: '#5e6d3a'
  }
};
const TANK_ORDER = ['ms1', 't34', 'kv2', 'isu'];

const BOT_NAMES = [
  'xX_Тапок_Xx', 'БАТЯ_2014', 'Noob_2010', 'Про100_Ваня', 'Рак_Горыныч',
  'Кибер_Бублик', 'Ваня_Хард', 'MaZaFaKa2009', 'ДЕД_ИНСАЙД', 'Крош_45',
  'Терминатор_Р', 'Снайпер_101', 'Гроза_Твери', 'Печенька', 'Уголек_77'
];

// ==================== КАРТА ====================
const WORLD = { w: 2200, h: 1500 };

// левая половина зданий — отзеркалим для симметрии
const BUILDINGS_HALF = [
  { x: 200, y: 210, w: 210, h: 110 },
  { x: 200, y: 470, w: 130, h: 180 },
  { x: 520, y: 140, w: 110, h: 250 },
  { x: 380, y: 780, w: 230, h: 90 },
  { x: 740, y: 430, w: 140, h: 140 },
  { x: 880, y: 180, w: 170, h: 90 },
  { x: 130, y: 920, w: 170, h: 170 },
  { x: 530, y: 1000, w: 90, h: 220 },
  { x: 820, y: 1030, w: 190, h: 100 },
  { x: 90, y: 1280, w: 280, h: 80 }
];
const BUILDINGS_CENTER = [
  { x: 1010, y: 640, w: 180, h: 220 },
  { x: 960, y: 170, w: 280, h: 70 },
  { x: 960, y: 1260, w: 280, h: 70 }
];
const BUILDINGS = BUILDINGS_HALF
  .concat(BUILDINGS_HALF.map(b => ({ x: WORLD.w - b.x - b.w, y: b.y, w: b.w, h: b.h })))
  .concat(BUILDINGS_CENTER);

// ==================== УТИЛИТЫ ====================
const TAU = Math.PI * 2;
const wrapAngle = a => Math.atan2(Math.sin(a), Math.cos(a));
const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
const rand = (a, b) => a + Math.random() * (b - a);
const dist2 = (ax, ay, bx, by) => (ax - bx) * (ax - bx) + (ay - by) * (ay - by);

function circleRect(cx, cy, r, rect, pad) {
  const rr = r + (pad || 0);
  const nx = clamp(cx, rect.x, rect.x + rect.w);
  const ny = clamp(cy, rect.y, rect.y + rect.h);
  return dist2(cx, cy, nx, ny) < rr * rr;
}

function pointInRect(px, py, rect, pad) {
  const p = pad || 0;
  return px > rect.x - p && px < rect.x + rect.w + p && py > rect.y - p && py < rect.y + rect.h + p;
}

// отрезок пересекает прямоугольник (slab-метод) — для ЛОСа и снарядов
function segRect(x1, y1, x2, y2, r, pad) {
  const p = pad || 0;
  const minX = r.x - p, maxX = r.x + r.w + p, minY = r.y - p, maxY = r.y + r.h + p;
  const dx = x2 - x1, dy = y2 - y1;
  let t0 = 0, t1 = 1;
  const checks = [[-dx, x1 - minX], [dx, maxX - x1], [-dy, y1 - minY], [dy, maxY - y1]];
  for (const [dd, qq] of checks) {
    if (Math.abs(dd) < 1e-9) { if (qq < 0) return false; }
    else {
      const t = qq / dd;
      if (dd < 0) { if (t > t1) return false; if (t > t0) t0 = t; }
      else { if (t < t0) return false; if (t < t1) t1 = t; }
    }
  }
  return true;
}

function losClear(x1, y1, x2, y2) {
  for (const b of BUILDINGS) if (segRect(x1, y1, x2, y2, b, 2)) return false;
  return true;
}

function freePoint(x, y, r) {
  for (const b of BUILDINGS) if (circleRect(x, y, r, b, 14)) return false;
  if (x < 60 || x > WORLD.w - 60 || y < 60 || y > WORLD.h - 60) return false;
  return true;
}

// ==================== ЗВУК (WebAudio, синтез) ====================
let AC = null, noiseBuf = null;
function ensureAudio() {
  if (AC) return;
  try {
    AC = new (window.AudioContext || window.webkitAudioContext)();
    noiseBuf = AC.createBuffer(1, AC.sampleRate * 1, AC.sampleRate);
    const d = noiseBuf.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  } catch (e) { AC = null; }
}
function sfxShot(big) {
  if (!AC) return;
  const t = AC.currentTime;
  const osc = AC.createOscillator(), g = AC.createGain();
  osc.type = 'sawtooth';
  osc.frequency.setValueAtTime(big ? 160 : 240, t);
  osc.frequency.exponentialRampToValueAtTime(40, t + 0.18);
  g.gain.setValueAtTime(big ? 0.30 : 0.20, t);
  g.gain.exponentialRampToValueAtTime(0.001, t + 0.22);
  osc.connect(g).connect(AC.destination);
  osc.start(t); osc.stop(t + 0.25);
  const src = AC.createBufferSource(); src.buffer = noiseBuf;
  const f = AC.createBiquadFilter(); f.type = 'lowpass';
  f.frequency.setValueAtTime(big ? 900 : 1600, t);
  const ng = AC.createGain();
  ng.gain.setValueAtTime(big ? 0.5 : 0.3, t);
  ng.gain.exponentialRampToValueAtTime(0.001, t + (big ? 0.5 : 0.3));
  src.connect(f).connect(ng).connect(AC.destination);
  src.start(t); src.stop(t + 0.55);
}
function sfxExpl(big) {
  if (!AC) return;
  const t = AC.currentTime;
  const src = AC.createBufferSource(); src.buffer = noiseBuf;
  const f = AC.createBiquadFilter(); f.type = 'lowpass';
  f.frequency.setValueAtTime(big ? 500 : 900, t);
  f.frequency.exponentialRampToValueAtTime(80, t + (big ? 0.9 : 0.45));
  const g = AC.createGain();
  g.gain.setValueAtTime(big ? 0.55 : 0.3, t);
  g.gain.exponentialRampToValueAtTime(0.001, t + (big ? 1.0 : 0.5));
  src.connect(f).connect(g).connect(AC.destination);
  src.start(t); src.stop(t + 1.1);
}
function sfxHit(penetrated) {
  if (!AC) return;
  const t = AC.currentTime;
  const osc = AC.createOscillator(), g = AC.createGain();
  osc.type = 'square';
  osc.frequency.setValueAtTime(penetrated ? 700 : 1100, t);
  osc.frequency.exponentialRampToValueAtTime(200, t + 0.08);
  g.gain.setValueAtTime(0.12, t);
  g.gain.exponentialRampToValueAtTime(0.001, t + 0.1);
  osc.connect(g).connect(AC.destination);
  osc.start(t); osc.stop(t + 0.12);
}

// ==================== ХОЛСТЫ / DOM ====================
const $ = id => document.getElementById(id);
const previewC = $('preview'), gameC = $('game'), minimapC = $('minimap'), reloadC = $('reloadC');
const pctx = previewC.getContext('2d');
const ctx = gameC.getContext('2d');
const mctx = minimapC.getContext('2d');
const rctx = reloadC.getContext('2d');

// ==================== СОСТОЯНИЕ ====================
let state = 'hangar';
let selected = 'ms1';

let tanks = [], shells = [], parts = [], floaters = [], feed = [], deco = [];
let player = null;
let battleTime = 300, msgTimer = 0, endTimer = 0, endInfo = null;
let frags = { ally: 0, enemy: 0 };
let cam = { x: 0, y: 0, zoom: 1, tzoom: 1, shake: 0 };
const keys = {};
const mouse = { x: 0, y: 0, down: false, rdown: false };
let stats = { dmg: 0, shots: 0, hits: 0, frags: 0 };
let lastTs = 0;

// ==================== РИСОВАНИЕ ТАНКА (общее) ====================
function drawTankShape(g, type, opts) {
  const o = opts || {};
  const dead = o.dead || false;
  const hullColor = dead ? '#3a3a34'
    : o.team === 'enemy' ? '#8a5a48'
    : type.color;
  const dk = dead ? '#232320' : 'rgba(0,0,0,.35)';

  // гусеницы
  g.fillStyle = dead ? '#2a2a26' : '#2e2e28';
  roundRect(g, -type.hullW / 2 - 3, -type.hullH / 2 - 3, type.hullW + 6, 9, 4); g.fill();
  roundRect(g, -type.hullW / 2 - 3, type.hullH / 2 - 6, type.hullW + 6, 9, 4); g.fill();
  g.fillStyle = 'rgba(255,255,255,.06)';
  for (let i = -type.hullW / 2; i < type.hullW / 2; i += 7) {
    g.fillRect(i, -type.hullH / 2 - 2, 2, 7);
    g.fillRect(i, type.hullH / 2 - 5, 2, 7);
  }

  // корпус
  g.fillStyle = hullColor;
  roundRect(g, -type.hullW / 2, -type.hullH / 2, type.hullW, type.hullH, 5); g.fill();
  g.strokeStyle = dk; g.lineWidth = 2;
  roundRect(g, -type.hullW / 2, -type.hullH / 2, type.hullW, type.hullH, 5); g.stroke();

  // лючок/детали
  g.fillStyle = 'rgba(0,0,0,.18)';
  g.fillRect(-type.hullW / 2 + 4, -type.hullH / 2 + 4, 8, type.hullH - 8);

  if (type.casemate) {
    // рубка ПТ-САУ
    g.fillStyle = dead ? '#44443e' : shade(hullColor, 18);
    roundRect(g, -10, -type.hullH / 2 + 3, 24, type.hullH - 6, 4); g.fill();
    g.strokeStyle = dk; g.stroke();
  }
}
function shade(hex, amt) {
  const n = parseInt(hex.slice(1), 16);
  const r = clamp((n >> 16) + amt, 0, 255), g = clamp(((n >> 8) & 255) + amt, 0, 255), b = clamp((n & 255) + amt, 0, 255);
  return `rgb(${r},${g},${b})`;
}
function roundRect(g, x, y, w, h, r) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

function drawGun(g, type, dead) {
  const col = dead ? '#3a3a34' : shade(type.casemate ? type.color : type.color, -10);
  if (type.casemate) {
    g.fillStyle = col;
    g.fillRect(8, -3, type.gunLen, 6);
    g.fillStyle = dead ? '#2a2a26' : '#22221d';
    g.fillRect(8 + type.gunLen - 4, -4, 5, 8); // маска
  } else {
    // башня
    g.fillStyle = dead ? '#44443e' : shade(type.color, 14);
    g.beginPath(); g.arc(0, 0, type.turretR, 0, TAU); g.fill();
    g.strokeStyle = 'rgba(0,0,0,.35)'; g.lineWidth = 2; g.stroke();
    g.fillStyle = col;
    g.fillRect(type.turretR * 0.4, -3, type.gunLen, 6);
    g.fillStyle = dead ? '#2a2a26' : '#22221d';
    g.fillRect(type.turretR * 0.4 + type.gunLen - 3, -4, 5, 8);
    g.fillStyle = 'rgba(0,0,0,.25)';
    g.beginPath(); g.arc(0, 0, type.turretR * 0.45, 0, TAU); g.fill();
  }
}

// ==================== АНГАР ====================
function buildCarousel() {
  const wrap = $('carousel');
  wrap.innerHTML = '';
  TANK_ORDER.forEach(key => {
    const t = TANKS[key];
    const card = document.createElement('div');
    card.className = 'card' + (key === selected ? ' sel' : '');
    const cv = document.createElement('canvas');
    cv.width = 100; cv.height = 48;
    const g = cv.getContext('2d');
    g.translate(46, 24); g.rotate(-0.15); g.scale(1.1, 1.1);
    drawTankShape(g, t, { team: 'ally' });
    drawGun(g, t, false);
    const nm = document.createElement('div'); nm.className = 'nm'; nm.textContent = t.name;
    const cl = document.createElement('div'); cl.className = 'cl'; cl.textContent = t.cls;
    card.append(cv, nm, cl);
    card.onclick = () => selectTank(key);
    wrap.appendChild(card);
  });
}

function selectTank(key) {
  selected = key;
  const t = TANKS[key];
  $('tankName').innerHTML = `${t.name} <span class="cls">${t.cls}</span>`;
  const setBar = (id, v) => $(id).style.width = clamp(v * 100, 4, 100) + '%';
  setBar('stDmg', t.dmg / 420);
  setBar('stHp', t.hp / 750);
  setBar('stSpd', t.speed / 165);
  setBar('stRel', 2 / t.reload);
  buildCarousel();
  drawPreview();
}

function drawPreview() {
  const t = TANKS[selected];
  pctx.clearRect(0, 0, previewC.width, previewC.height);
  // «пол ангара»
  const grd = pctx.createRadialGradient(320, 140, 40, 320, 150, 320);
  grd.addColorStop(0, 'rgba(120,110,70,.18)');
  grd.addColorStop(1, 'rgba(0,0,0,0)');
  pctx.fillStyle = grd;
  pctx.fillRect(0, 0, previewC.width, previewC.height);
  // тень
  pctx.save();
  pctx.translate(330, 168); pctx.rotate(-0.28); pctx.scale(3.4, 3.4);
  pctx.fillStyle = 'rgba(0,0,0,.45)';
  pctx.beginPath(); pctx.ellipse(0, 4, t.hullW / 2 + 8, t.hullH / 2 + 8, 0, 0, TAU); pctx.fill();
  pctx.restore();
  // танк
  pctx.save();
  pctx.translate(320, 150); pctx.rotate(-0.28); pctx.scale(3.4, 3.4);
  drawTankShape(pctx, t, { team: 'ally' });
  pctx.save(); pctx.translate(t.casemate ? 8 : 0, 0);
  drawGun(pctx, t, false);
  pctx.restore();
  pctx.restore();
}

// ==================== СТАРТ БОЯ ====================
function makeTank(key, team, x, y, angle, isPlayer, nom) {
  const type = TANKS[key];
  return {
    type, key, team,
    x, y, angle, turret: angle, gun: 0,
    speed: 0, fwd: 0, turnIn: 0,
    hp: type.hp, alive: true, wreckSmoked: 0,
    reload: rand(0.5, 2.5),
    isPlayer: !!isPlayer, name: nom,
    // ИИ
    aimErr: rand(0.12, 0.2), target: null, lastSeen: null, think: rand(0, 0.3),
    wp: null, stuck: 0, strafeDir: Math.random() < 0.5 ? 1 : -1,
    smokeT: 0
  };
}

function spawnTeams() {
  const aSpawns = [[170, 1330], [300, 1400], [170, 1170], [420, 1330], [330, 1180]];
  const eSpawns = [[2030, 170], [1900, 100], [2030, 330], [1780, 170], [1870, 320]];
  const pick = () => TANK_ORDER[Math.floor(Math.random() * TANK_ORDER.length)];
  const names = BOT_NAMES.slice().sort(() => Math.random() - 0.5);

  player = makeTank(selected, 'ally', aSpawns[0][0], aSpawns[0][1], -Math.PI / 4, true, 'Ты');
  tanks.push(player);
  for (let i = 1; i < 5; i++) {
    tanks.push(makeTank(pick(), 'ally', aSpawns[i][0], aSpawns[i][1], -Math.PI / 4, false, names.pop()));
  }
  for (let i = 0; i < 5; i++) {
    tanks.push(makeTank(pick(), 'enemy', eSpawns[i][0], eSpawns[i][1], Math.PI * 0.75, false, names.pop()));
  }
}

function genDeco() {
  deco = [];
  for (let i = 0; i < 60; i++) {
    const x = rand(60, WORLD.w - 60), y = rand(60, WORLD.h - 60);
    let bad = false;
    for (const b of BUILDINGS) if (pointInRect(x, y, b, 40)) { bad = true; break; }
    if (bad) continue;
    deco.push({
      x, y,
      r: rand(10, 42),
      kind: Math.random() < 0.5 ? 'grass' : 'crater'
    });
  }
}

function startBattle() {
  ensureAudio();
  if (AC && AC.state === 'suspended') AC.resume();

  tanks = []; shells = []; parts = []; floaters = []; feed = [];
  frags = { ally: 0, enemy: 0 };
  stats = { dmg: 0, shots: 0, hits: 0, frags: 0 };
  battleTime = 300; endTimer = 0; endInfo = null;
  genDeco();
  spawnTeams();

  cam.x = player.x; cam.y = player.y; cam.zoom = 1; cam.tzoom = 1; cam.shake = 0;

  $('hangar').classList.add('hidden');
  $('battle').classList.remove('hidden');
  $('endScreen').classList.add('hidden');
  $('hudName').textContent = TANKS[selected].name;
  resize();
  showMsg('БОЙ НАЧАЛСЯ!');
  state = 'battle';
  lastTs = performance.now();
  requestAnimationFrame(loop);
}

// ==================== УПРАВЛЕНИЕ ====================
window.addEventListener('keydown', e => {
  keys[e.code] = true;
  if (state === 'battle' && ['KeyW', 'KeyA', 'KeyS', 'KeyD', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space'].includes(e.code)) e.preventDefault();
});
window.addEventListener('keyup', e => { keys[e.code] = false; });
window.addEventListener('contextmenu', e => { if (state === 'battle') e.preventDefault(); });
window.addEventListener('mousedown', e => {
  if (e.button === 0) mouse.down = true;
  if (e.button === 2) mouse.rdown = true;
});
window.addEventListener('mouseup', e => {
  if (e.button === 0) mouse.down = false;
  if (e.button === 2) mouse.rdown = false;
});
window.addEventListener('mousemove', e => { mouse.x = e.clientX; mouse.y = e.clientY; });
window.addEventListener('blur', () => { mouse.down = mouse.rdown = false; for (const k in keys) keys[k] = false; });

function resize() {
  gameC.width = window.innerWidth;
  gameC.height = window.innerHeight;
}
window.addEventListener('resize', () => { if (state === 'battle') resize(); });

// ==================== ЛОГИКА ====================
function tankRadius(t) { return Math.max(t.type.hullW, t.type.hullH) * 0.52; }

function muzzleOf(t) {
  const a = t.turret;
  const len = (t.type.casemate ? 8 : 0) + t.type.gunLen + (t.type.casemate ? 4 : 6);
  const bx = t.type.casemate ? 8 : t.type.turretR * 0.4;
  const dx = Math.cos(a), dy = Math.sin(a);
  return { x: t.x + dx * (bx + len), y: t.y + dy * (bx + len) };
}

function fireShell(t) {
  const type = t.type;
  const m = muzzleOf(t);
  const disp = (t.isPlayer ? 0.012 : 0.03 + t.aimErr * 0.35);
  const a = t.turret + rand(-disp, disp);
  shells.push({
    x: m.x, y: m.y,
    vx: Math.cos(a) * type.shellSpeed,
    vy: Math.sin(a) * type.shellSpeed,
    dmg: type.dmg, team: t.team, owner: t, life: 2.5
  });
  t.reload = type.reload;
  const big = type.dmg >= 300;
  t.speed -= 22; // откат
  cam.shake = Math.max(cam.shake, t.isPlayer ? (big ? 14 : 7) : (dist2(t.x, t.y, player.x, player.y) < 500 * 500 ? 3 : 0));
  sfxShot(big);
  // вспышка
  for (let i = 0; i < 8; i++) {
    const sa = a + rand(-0.5, 0.5);
    parts.push({ x: m.x, y: m.y, vx: Math.cos(sa) * rand(60, 260), vy: Math.sin(sa) * rand(60, 260), life: rand(0.06, 0.14), max: 0.14, col: '#ffd27a', size: rand(2, 4) });
  }
  if (t.isPlayer) stats.shots++;
}

function explode(x, y, big) {
  sfxExpl(big);
  const n = big ? 34 : 18;
  for (let i = 0; i < n; i++) {
    const a = rand(0, TAU), sp = rand(40, big ? 320 : 200);
    parts.push({ x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, life: rand(0.25, big ? 0.9 : 0.55), max: 0.9, col: ['#ffdd88', '#ff9a3c', '#ff5a26', '#777'][Math.floor(rand(0, 4))], size: rand(2, big ? 6 : 4) });
  }
  for (let i = 0; i < (big ? 16 : 8); i++) {
    const a = rand(0, TAU), sp = rand(10, 60);
    parts.push({ x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, life: rand(0.8, 1.8), max: 1.8, col: 'smoke', size: rand(6, big ? 16 : 10) });
  }
}

function damageTank(t, dmg, attacker, x, y) {
  // шанс рикошета как в танках 😄
  if (Math.random() < 0.08) {
    floaters.push({ x: t.x, y: t.y - 30, txt: 'Рикошет!', life: 1.0, col: '#c8c8c8' });
    sfxHit(false);
    sparks(x, y);
    return;
  }
  const real = Math.round(dmg * rand(0.78, 1.25));
  t.hp -= real;
  floaters.push({ x: t.x + rand(-10, 10), y: t.y - 30, txt: '-' + real, life: 1.1, col: t.team === 'ally' ? '#ff8a70' : '#ffcf5a' });
  sfxHit(true);
  sparks(x, y);
  if (attacker && attacker.isPlayer) { stats.hits++; stats.dmg += Math.min(real, Math.max(0, t.hp + real)); }

  // подрыв БК для фана на больших калибрах
  let racked = false;
  if (dmg >= 300 && Math.random() < 0.10 && t.hp > 0) { t.hp = 0; racked = true; }

  if (t.hp <= 0 && t.alive) {
    t.alive = false;
    t.hp = 0;
    explode(t.x, t.y, true);
    cam.shake = Math.max(cam.shake, dist2(t.x, t.y, player.x, player.y) < 600 * 600 ? 12 : 5);
    if (racked) floaters.push({ x: t.x, y: t.y - 52, txt: '💥 БК ПОДОРВАН!', life: 1.6, col: '#ff7a3c' });
    const killerName = attacker ? attacker.name : '???';
    pushFeed(`${killerName} уничтожил ${t.name} [${t.type.name}]`);
    if (t.team === 'enemy') { frags.ally++; if (attacker && attacker.isPlayer) stats.frags++; }
    else frags.enemy++;
    if (t.isPlayer) showMsg('ТАНК УНИЧНИЧЕН! Наблюдение за союзниками…');
  }
}

function sparks(x, y) {
  for (let i = 0; i < 10; i++) {
    const a = rand(0, TAU), sp = rand(50, 240);
    parts.push({ x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, life: rand(0.1, 0.3), max: 0.3, col: '#ffcf7a', size: rand(1.5, 3) });
  }
}

function pushFeed(txt) {
  feed.push({ txt, life: 4 });
  if (feed.length > 5) feed.shift();
  renderFeed();
}
function renderFeed() {
  $('feed').innerHTML = feed.map(f => `<div>${f.txt}</div>`).join('');
}

function showMsg(txt) {
  const el = $('battleMsg');
  el.textContent = txt;
  el.style.opacity = 1;
  msgTimer = 2.4;
}

// ---------- движение танка ----------
function moveTank(t, dt) {
  const type = t.type;
  const target = t.fwd * (t.fwd >= 0 ? type.speed : type.revSpeed);
  const acc = type.accel * dt;
  if (t.speed < target) t.speed = Math.min(target, t.speed + acc);
  else t.speed = Math.max(target, t.speed - acc);

  t.angle += t.turnIn * type.rot * dt * (t.fwd !== 0 ? 1 : 0.85);
  t.angle = wrapAngle(t.angle);

  const nx = t.x + Math.cos(t.angle) * t.speed * dt;
  const ny = t.y + Math.sin(t.angle) * t.speed * dt;
  const r = tankRadius(t);

  // ось X
  if (!collidesWorld(nx, t.y, r)) t.x = nx; else { t.speed *= 0.3; t.stuck = (t.stuck || 0) + dt; }
  // ось Y
  if (!collidesWorld(t.x, ny, r)) t.y = ny; else { t.speed *= 0.3; t.stuck = (t.stuck || 0) + dt; }

  t.x = clamp(t.x, 40, WORLD.w - 40);
  t.y = clamp(t.y, 40, WORLD.h - 40);

  // разъезд с другими танками
  for (const o of tanks) {
    if (o === t || !o.alive) continue;
    const rr = r + tankRadius(o);
    const d2 = dist2(t.x, t.y, o.x, o.y);
    if (d2 < rr * rr && d2 > 0.01) {
      const d = Math.sqrt(d2), push = (rr - d) / 2;
      const ux = (t.x - o.x) / d, uy = (t.y - o.y) / d;
      t.x += ux * push; t.y += uy * push;
      o.x -= ux * push; o.y -= uy * push;
    }
  }
}

function collidesWorld(x, y, r) {
  for (const b of BUILDINGS) if (circleRect(x, y, r, b, 0)) return true;
  return false;
}

// ---------- ИИ ботов ----------
function botThink(t, dt) {
  t.think -= dt;
  if (t.think > 0) return;
  t.think = 0.22;

  // ищем ближайшего видимого врага
  let best = null, bd = Infinity;
  for (const o of tanks) {
    if (o.team === t.team || !o.alive) continue;
    const d2 = dist2(t.x, t.y, o.x, o.y);
    if (d2 > 900 * 900) continue;
    if (d2 < bd && losClear(t.x, t.y, o.x, o.y)) { bd = d2; best = o; }
  }
  if (best) {
    t.target = best;
    t.lastSeen = { x: best.x, y: best.y, t: 4 };
    t.aimErr = Math.max(0.02, t.aimErr - 0.25 * 0.22);
  } else {
    if (t.lastSeen) { t.lastSeen.t -= 0.22; if (t.lastSeen.t <= 0) { t.lastSeen = null; t.target = null; } }
    else t.target = null;
    t.aimErr = Math.min(0.22, t.aimErr + 0.05 * 0.22);
  }
}

function botControl(t, dt) {
  botThink(t, dt);
  t.fwd = 0; t.turnIn = 0;

  let goalAngle;
  const tgt = t.target;

  if (t.lastSeen) {
    const gx = t.target && t.target.alive ? t.target.x : t.lastSeen.x;
    const gy = t.target && t.target.alive ? t.target.y : t.lastSeen.y;
    const d = Math.sqrt(dist2(t.x, t.y, gx, gy));
    const dirTo = Math.atan2(gy - t.y, gx - t.x);
    goalAngle = dirTo;

    if (t.target) {
      // держим дистанцию: подъехать/отъехать/крутиться
      if (d > 480) t.fwd = 1;
      else if (d < 260) t.fwd = -0.7;
      else { t.fwd = 0.35; goalAngle = dirTo + Math.PI / 2 * t.strafeDir; if (Math.random() < 0.01) t.strafeDir *= -1; }
    } else {
      t.fwd = 1; // едем к последней позиции
      if (d < 90) t.lastSeen = null;
    }
  } else {
    // патруль
    if (!t.wp || dist2(t.x, t.y, t.wp.x, t.wp.y) < 90 * 90) {
      for (let i = 0; i < 12; i++) {
        const x = rand(100, WORLD.w - 100), y = rand(100, WORLD.h - 100);
        if (freePoint(x, y, 30)) { t.wp = { x, y }; break; }
      }
    }
    if (t.wp) { goalAngle = Math.atan2(t.wp.y - t.y, t.wp.x - t.x); t.fwd = 0.9; }
  }

  if (goalAngle !== undefined) {
    const diff = wrapAngle(goalAngle - t.angle);
    t.turnIn = clamp(diff * 3, -1, 1);
    if (Math.abs(diff) > 1.9) t.fwd = -0.5; // разворот
  }

  // избегание стен: щуп впереди
  if (t.fwd > 0) {
    const px = t.x + Math.cos(t.angle) * 70, py = t.y + Math.sin(t.angle) * 70;
    let blocked = false;
    for (const b of BUILDINGS) if (pointInRect(px, py, b, 26)) { blocked = true; break; }
    if (blocked) {
      const la = t.angle - 0.7, ra = t.angle + 0.7;
      const lf = !pointBlocked(t.x + Math.cos(la) * 80, t.y + Math.sin(la) * 80);
      const rf = !pointBlocked(t.x + Math.cos(ra) * 80, t.y + Math.sin(ra) * 80);
      if (lf && !rf) t.turnIn = -1;
      else if (rf && !lf) t.turnIn = 1;
      else t.turnIn = t.strafeDir;
      t.fwd = 0.6;
    }
  }
  if (t.stuck > 1.2) { t.stuck = 0; t.wp = null; t.strafeDir *= -1; t.fwd = -0.8; }

  // башня/орудие на цель
  if (t.target && t.target.alive) {
    // упреждение нет — боты простые :)
    const aim = Math.atan2(t.target.y - t.y, t.target.x - t.x);
    const err = (Math.random() - 0.5) * t.aimErr;
    steerGun(t, aim + err, dt);
    // огонь
    const gunWorld = t.type.casemate ? t.turret : t.turret;
    const dAim = Math.abs(wrapAngle(aim - gunWorld));
    if (t.reload <= 0 && dAim < 0.07 && dist2(t.x, t.y, t.target.x, t.target.y) < 850 * 850) {
      if (losClear(t.x, t.y, t.target.x, t.target.y)) fireShell(t);
    }
  } else {
    steerGun(t, t.angle, dt);
  }
}
function pointBlocked(x, y) {
  for (const b of BUILDINGS) if (pointInRect(x, y, b, 26)) return true;
  return false;
}

// поворот орудия с учётом ограничений (ПТ — в дуге, танки — круговая)
function steerGun(t, worldAngle, dt) {
  const type = t.type;
  let desired = worldAngle;
  if (type.casemate) {
    const rel = clamp(wrapAngle(worldAngle - t.angle), -type.arc, type.arc);
    desired = t.angle + rel;
    t.turret = desired;
    return;
  }
  const diff = wrapAngle(desired - t.turret);
  const maxStep = type.turret * dt;
  t.turret = wrapAngle(t.turret + clamp(diff, -maxStep, maxStep));
}

// ---------- игрок ----------
function playerControl(dt) {
  const t = player;
  if (!t.alive) return;
  t.fwd = (keys.KeyW || keys.ArrowUp ? 1 : 0) + (keys.KeyS || keys.ArrowDown ? -1 : 0);
  t.turnIn = (keys.KeyA || keys.ArrowLeft ? -1 : 0) + (keys.KeyD || keys.ArrowRight ? 1 : 0);

  // мышь в мир
  const wx = (mouse.x - gameC.width / 2) / cam.zoom + cam.x;
  const wy = (mouse.y - gameC.height / 2) / cam.zoom + cam.y;
  const aim = Math.atan2(wy - t.y, wx - t.x);
  steerGun(t, aim, dt);

  if (mouse.down && t.reload <= 0) fireShell(t);
}

// ---------- снаряды ----------
function updateShells(dt) {
  for (let i = shells.length - 1; i >= 0; i--) {
    const s = shells[i];
    s.life -= dt;
    if (s.life <= 0) { shells.splice(i, 1); continue; }
    const nx = s.x + s.vx * dt, ny = s.y + s.vy * dt;
    // стены (подшаги против туннелирования)
    let hitWall = false;
    for (const b of BUILDINGS) {
      if (segRect(s.x, s.y, nx, ny, b, 0)) { hitWall = true; break; }
    }
    if (hitWall) {
      sparks(s.x, s.y);
      explodeSmall(s.x, s.y);
      shells.splice(i, 1);
      continue;
    }
    // танки
    let hit = null;
    for (const t of tanks) {
      if (!t.alive || t.team === s.team) continue;
      const r = tankRadius(t);
      if (segCircle(s.x, s.y, nx, ny, t.x, t.y, r)) { hit = t; break; }
    }
    if (hit) {
      damageTank(hit, s.dmg, s.owner, nx, ny);
      shells.splice(i, 1);
      continue;
    }
    if (nx < 0 || nx > WORLD.w || ny < 0 || ny > WORLD.h) { shells.splice(i, 1); continue; }
    s.x = nx; s.y = ny;
  }
}
function segCircle(x1, y1, x2, y2, cx, cy, r) {
  // ближайшая точка отрезка к центру
  const dx = x2 - x1, dy = y2 - y1;
  const len2 = dx * dx + dy * dy || 1e-9;
  let tt = ((cx - x1) * dx + (cy - y1) * dy) / len2;
  tt = clamp(tt, 0, 1);
  const px = x1 + dx * tt, py = y1 + dy * tt;
  return dist2(px, py, cx, cy) < r * r;
}
function explodeSmall(x, y) {
  for (let i = 0; i < 8; i++) {
    const a = rand(0, TAU), sp = rand(20, 100);
    parts.push({ x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, life: rand(0.2, 0.5), max: 0.5, col: 'smoke', size: rand(4, 8) });
  }
}

// ---------- конец боя ----------
function checkEnd(dt) {
  const allies = tanks.filter(t => t.team === 'ally' && t.alive).length;
  const enemies = tanks.filter(t => t.team === 'enemy' && t.alive).length;
  if (endInfo) {
    endTimer -= dt;
    if (endTimer <= 0) showEnd();
    return;
  }
  if (enemies === 0) endInfo = { res: 'win', why: 'Все враги уничтожены!' };
  else if (allies === 0) endInfo = { res: 'lose', why: 'Ваша команда уничтожена…' };
  else if (battleTime <= 0) {
    if (frags.ally > frags.enemy) endInfo = { res: 'win', why: 'Время вышло — победа по фрагам!' };
    else if (frags.ally < frags.enemy) endInfo = { res: 'lose', why: 'Время вышло — врагов осталось больше…' };
    else endInfo = { res: 'draw', why: 'Время вышло — равный счёт.' };
  }
  if (endInfo) endTimer = 1.6;
}
function showEnd() {
  state = 'end';
  const el = $('endTitle');
  el.textContent = endInfo.res === 'win' ? 'ПОБЕДА!' : endInfo.res === 'lose' ? 'ПОРАЖЕНИЕ' : 'НИЧЬЯ';
  el.className = endInfo.res;
  const acc = stats.shots ? Math.round(100 * stats.hits / stats.shots) : 0;
  $('endStats').innerHTML =
    `${endInfo.why}<br><br>` +
    `Нанесено урона: <b>${stats.dmg}</b><br>` +
    `Уничтожено танков: <b>${stats.frags}</b><br>` +
    `Точность: <b>${acc}%</b> (${stats.hits}/${stats.shots})`;
  $('endScreen').classList.remove('hidden');
}

// ==================== ЦИКЛ ====================
function loop(ts) {
  if (state !== 'battle') return;
  let dt = (ts - lastTs) / 1000;
  lastTs = ts;
  dt = Math.min(dt, 0.05);

  battleTime -= dt;
  msgTimer -= dt;
  if (msgTimer <= 0) $('battleMsg').style.opacity = 0;

  // танки
  for (const t of tanks) {
    if (!t.alive) continue;
    if (t.isPlayer) playerControl(dt); else botControl(t, dt);
    moveTank(t, dt);
    t.reload -= dt;
    // дым из подбитых
    if (t.hp < t.type.hp * 0.35) {
      t.smokeT -= dt;
      if (t.smokeT <= 0) {
        t.smokeT = 0.12;
        parts.push({ x: t.x + rand(-6, 6), y: t.y + rand(-6, 6), vx: rand(-8, 8), vy: rand(-25, -10), life: rand(0.6, 1.2), max: 1.2, col: 'smoke', size: rand(4, 8) });
      }
    }
  }
  // дым из горелых
  for (const t of tanks) {
    if (t.alive) continue;
    t.wreckSmoked += dt;
    if (t.wreckSmoked > 0.18 && t.wreckSmoked < 12) {
      parts.push({ x: t.x + rand(-8, 8), y: t.y, vx: rand(-6, 6), vy: rand(-30, -14), life: rand(0.8, 1.6), max: 1.6, col: 'smoke', size: rand(5, 10) });
    }
  }

  updateShells(dt);

  // частицы
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    p.life -= dt;
    if (p.life <= 0) { parts.splice(i, 1); continue; }
    p.x += p.vx * dt; p.y += p.vy * dt;
    p.vx *= (1 - dt * 2); p.vy *= (1 - dt * 2);
  }
  for (let i = floaters.length - 1; i >= 0; i--) {
    const f = floaters[i];
    f.life -= dt; f.y -= 28 * dt;
    if (f.life <= 0) floaters.splice(i, 1);
  }
  for (const f of feed) f.life -= dt;
  while (feed.length && feed[0].life <= 0) { feed.shift(); renderFeed(); }

  // камера
  const spectating = !player.alive;
  let focus = player;
  if (spectating) {
    const ally = tanks.find(t => t.team === 'ally' && t.alive);
    if (ally) focus = ally;
  }
  cam.tzoom = (mouse.rdown && !spectating) ? 1.75 : 1.0;
  let tx = focus.x, ty = focus.y;
  if (cam.tzoom > 1.1) {
    // в прицеле камера смещается к курсору
    const wx = (mouse.x - gameC.width / 2) / cam.zoom + cam.x;
    const wy = (mouse.y - gameC.height / 2) / cam.zoom + cam.y;
    tx = focus.x + clamp(wx - focus.x, -260, 260) * 0.55;
    ty = focus.y + clamp(wy - focus.y, -260, 260) * 0.55;
  }
  const k = Math.min(1, dt * 6);
  cam.x += (tx - cam.x) * k;
  cam.y += (ty - cam.y) * k;
  cam.zoom += (cam.tzoom - cam.zoom) * Math.min(1, dt * 8);
  cam.shake = Math.max(0, cam.shake - dt * 26);
  const vw = gameC.width / cam.zoom, vh = gameC.height / cam.zoom;
  cam.x = clamp(cam.x, vw / 2 - 60, WORLD.w - vw / 2 + 60);
  cam.y = clamp(cam.y, vh / 2 - 60, WORLD.h - vh / 2 + 60);

  checkEnd(dt);
  render();
  renderHud();

  requestAnimationFrame(loop);
}

// ==================== РЕНДЕР ====================
function render() {
  const w = gameC.width, h = gameC.height;
  ctx.clearRect(0, 0, w, h);
  ctx.save();
  const shx = cam.shake ? rand(-cam.shake, cam.shake) * 0.4 : 0;
  const shy = cam.shake ? rand(-cam.shake, cam.shake) * 0.4 : 0;
  ctx.translate(w / 2 + shx, h / 2 + shy);
  ctx.scale(cam.zoom, cam.zoom);
  ctx.translate(-cam.x, -cam.y);

  // земля
  ctx.fillStyle = '#343a26';
  ctx.fillRect(0, 0, WORLD.w, WORLD.h);
  // сетка-текстура
  ctx.strokeStyle = 'rgba(0,0,0,.10)';
  ctx.lineWidth = 1;
  for (let x = 0; x <= WORLD.w; x += 110) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, WORLD.h); ctx.stroke(); }
  for (let y = 0; y <= WORLD.h; y += 110) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(WORLD.w, y); ctx.stroke(); }

  // декорации
  for (const d of deco) {
    if (d.kind === 'grass') {
      ctx.fillStyle = 'rgba(46, 84, 36, .5)';
      ctx.beginPath(); ctx.ellipse(d.x, d.y, d.r, d.r * 0.6, d.x * 0.01, 0, TAU); ctx.fill();
    } else {
      ctx.fillStyle = 'rgba(30, 30, 24, .55)';
      ctx.beginPath(); ctx.arc(d.x, d.y, d.r * 0.5, 0, TAU); ctx.fill();
    }
  }

  // граница карты
  ctx.strokeStyle = '#242a18';
  ctx.lineWidth = 26;
  ctx.strokeRect(0, 0, WORLD.w, WORLD.h);

  const view = { x: cam.x - w / cam.zoom / 2 - 80, y: cam.y - h / cam.zoom / 2 - 80, w: w / cam.zoom + 160, h: h / cam.zoom + 160 };

  // тени зданий
  ctx.fillStyle = 'rgba(0,0,0,.30)';
  for (const b of BUILDINGS) {
    if (!rectsOverlap(b, view)) continue;
    ctx.fillRect(b.x + 10, b.y + 12, b.w, b.h);
  }
  // здания
  for (const b of BUILDINGS) {
    if (!rectsOverlap(b, view)) continue;
    ctx.fillStyle = '#4b4a42';
    ctx.fillRect(b.x, b.y, b.w, b.h);
    ctx.strokeStyle = '#2c2b26';
    ctx.lineWidth = 3;
    ctx.strokeRect(b.x, b.y, b.w, b.h);
    ctx.fillStyle = 'rgba(255,255,255,.05)';
    ctx.fillRect(b.x + 4, b.y + 4, b.w - 8, 5);
    // «окна»
    ctx.fillStyle = 'rgba(0,0,0,.25)';
    const step = 34;
    for (let wx2 = b.x + 12; wx2 < b.x + b.w - 14; wx2 += step)
      for (let wy2 = b.y + 14; wy2 < b.y + b.h - 14; wy2 += step)
        ctx.fillRect(wx2, wy2, 10, 8);
  }

  // обломки (подбитые)
  for (const t of tanks) {
    if (t.alive || !rectsOverlap({ x: t.x - 40, y: t.y - 40, w: 80, h: 80 }, view)) continue;
    ctx.save();
    ctx.translate(t.x, t.y);
    ctx.rotate(t.angle);
    drawTankShape(ctx, t.type, { dead: true });
    ctx.save(); ctx.rotate(wrapAngle(t.turret - t.angle));
    drawGun(ctx, t.type, true);
    ctx.restore();
    ctx.restore();
  }

  // живые танки
  for (const t of tanks) {
    if (!t.alive || !rectsOverlap({ x: t.x - 50, y: t.y - 50, w: 100, h: 100 }, view)) continue;
    ctx.save();
    ctx.translate(t.x, t.y);
    ctx.rotate(t.angle);
    drawTankShape(ctx, t.type, { team: t.team });
    ctx.save(); ctx.rotate(wrapAngle(t.turret - t.angle));
    drawGun(ctx, t.type, false);
    ctx.restore();
    ctx.restore();

    // подсветка игрока
    if (t.isPlayer) {
      ctx.strokeStyle = 'rgba(255,255,255,.55)';
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(t.x, t.y, tankRadius(t) + 6, 0, TAU); ctx.stroke();
    }

    // ник + хп
    const bw = 46;
    const pct = t.hp / t.type.hp;
    ctx.fillStyle = 'rgba(0,0,0,.55)';
    ctx.fillRect(t.x - bw / 2, t.y - tankRadius(t) - 16, bw, 5);
    ctx.fillStyle = t.team === 'ally' ? '#8fc75a' : '#e05a4e';
    ctx.fillRect(t.x - bw / 2, t.y - tankRadius(t) - 16, bw * pct, 5);
    ctx.font = '11px Segoe UI, Arial';
    ctx.textAlign = 'center';
    ctx.fillStyle = t.team === 'ally' ? '#cfe8b0' : '#f0b8b0';
    ctx.fillText(t.name, t.x, t.y - tankRadius(t) - 21);
  }

  // снаряды
  for (const s of shells) {
    const a = Math.atan2(s.vy, s.vx);
    ctx.save();
    ctx.translate(s.x, s.y);
    ctx.rotate(a);
    ctx.fillStyle = '#ffe9a0';
    ctx.shadowColor = '#ffb03c';
    ctx.shadowBlur = 8;
    ctx.fillRect(-7, -1.5, 12, 3);
    ctx.restore();
  }

  // частицы
  for (const p of parts) {
    const alpha = clamp(p.life / p.max, 0, 1);
    if (p.col === 'smoke') {
      ctx.fillStyle = `rgba(60,60,58,${0.35 * alpha})`;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.size * (1.6 - alpha * 0.6), 0, TAU); ctx.fill();
    } else {
      ctx.fillStyle = p.col;
      ctx.globalAlpha = alpha;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.size, 0, TAU); ctx.fill();
      ctx.globalAlpha = 1;
    }
  }

  // всплывающий урон
  ctx.font = 'bold 14px Segoe UI, Arial';
  ctx.textAlign = 'center';
  for (const f of floaters) {
    ctx.globalAlpha = clamp(f.life, 0, 1);
    ctx.fillStyle = f.col;
    ctx.fillText(f.txt, f.x, f.y);
    ctx.globalAlpha = 1;
  }

  ctx.restore();

  // снайперская виньетка
  if (cam.zoom > 1.25) {
    const rr = Math.min(w, h) * 0.46;
    const g2 = ctx.createRadialGradient(w / 2, h / 2, rr * 0.7, w / 2, h / 2, rr * 1.25);
    g2.addColorStop(0, 'rgba(0,0,0,0)');
    g2.addColorStop(1, 'rgba(0,0,0,.75)');
    ctx.fillStyle = g2;
    ctx.fillRect(0, 0, w, h);
    // прицел
    ctx.strokeStyle = 'rgba(255,240,200,.7)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(w / 2 - 14, h / 2); ctx.lineTo(w / 2 + 14, h / 2); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(w / 2, h / 2 - 14); ctx.lineTo(w / 2, h / 2 + 14); ctx.stroke();
  }
}
function rectsOverlap(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

// ==================== HUD ====================
function renderHud() {
  const allies = tanks.filter(t => t.team === 'ally' && t.alive).length;
  const enemies = tanks.filter(t => t.team === 'enemy' && t.alive).length;
  $('scoreAlly').textContent = allies;
  $('scoreEnemy').textContent = enemies;
  const tt = Math.max(0, battleTime);
  const mm = Math.floor(tt / 60), ss = Math.floor(tt % 60);
  $('timer').textContent = `${mm}:${ss < 10 ? '0' : ''}${ss}`;

  const pct = clamp(player.hp / player.type.hp, 0, 1);
  $('hpfill').style.width = (pct * 100) + '%';
  $('hpfill').style.background = pct > 0.5 ? 'linear-gradient(#a5d55f,#79a53c)' : pct > 0.25 ? 'linear-gradient(#e8c34a,#c79a28)' : 'linear-gradient(#e05a4e,#b03a30)';
  $('hptext').textContent = `${Math.max(0, Math.round(player.hp))}/${player.type.hp}`;

  // перезарядка — радиальный индикатор
  rctx.clearRect(0, 0, 58, 58);
  const rel = clamp(1 - player.reload / player.type.reload, 0, 1);
  rctx.lineWidth = 6;
  rctx.strokeStyle = 'rgba(255,255,255,.15)';
  rctx.beginPath(); rctx.arc(29, 29, 22, 0, TAU); rctx.stroke();
  rctx.strokeStyle = rel >= 1 ? '#a6d957' : '#ffb03c';
  rctx.beginPath(); rctx.arc(29, 29, 22, -Math.PI / 2, -Math.PI / 2 + TAU * rel); rctx.stroke();
  rctx.font = 'bold 12px Segoe UI, Arial';
  rctx.textAlign = 'center'; rctx.textBaseline = 'middle';
  rctx.fillStyle = '#eee6cb';
  rctx.fillText(rel >= 1 ? 'ГОТОВ' : Math.ceil(player.reload), 29, 29);

  // миникарта
  const mw = minimapC.width, mh = minimapC.height;
  const sx = mw / WORLD.w, sy = mh / WORLD.h;
  mctx.clearRect(0, 0, mw, mh);
  mctx.fillStyle = 'rgba(52,58,38,.9)';
  mctx.fillRect(0, 0, mw, mh);
  mctx.fillStyle = '#55544a';
  for (const b of BUILDINGS) mctx.fillRect(b.x * sx, b.y * sy, Math.max(2, b.w * sx), Math.max(2, b.h * sy));
  for (const t of tanks) {
    if (!t.alive) continue;
    mctx.fillStyle = t.isPlayer ? '#ffffff' : t.team === 'ally' ? '#8fc75a' : '#e05a4e';
    mctx.beginPath(); mctx.arc(t.x * sx, t.y * sy, t.isPlayer ? 4 : 3, 0, TAU); mctx.fill();
  }
  // рамка обзора камеры
  const vw = gameC.width / cam.zoom * sx, vh = gameC.height / cam.zoom * sy;
  mctx.strokeStyle = 'rgba(255,255,255,.4)';
  mctx.lineWidth = 1;
  mctx.strokeRect(cam.x * sx - vw / 2, cam.y * sy - vh / 2, vw, vh);
}

// ==================== ПРИВЯЗКА КНОПОК ====================
$('battleBtn').addEventListener('click', startBattle);
$('toHangar').addEventListener('click', () => {
  state = 'hangar';
  $('battle').classList.add('hidden');
  $('hangar').classList.remove('hidden');
  selectTank(selected);
});

// старт ангара
selectTank('ms1');
