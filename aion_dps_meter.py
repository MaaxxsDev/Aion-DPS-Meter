"""Aion 4.6 DPS/Heal-Meter - liest live aus Chat.log, veraendert keine Spieldateien."""

import os
import re
import sys
import json
import time
import threading
import subprocess
import tempfile
import urllib.request
from collections import Counter
import tkinter as tk
from tkinter import filedialog
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

APP_VERSION = '1.0.1'
GITHUB_REPO = 'MaaxxsDev/Aion-DPS-Meter'
GITHUB_API_LATEST = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'

DEFAULT_LOG_PATH = r"D:\Origin\Chat.log"
IDLE_TIMEOUT = 5.0       # Sekunden ohne Kampfaktion -> Pull gilt als beendet
HISTORY_LIMIT = 20       # Anzahl gespeicherter vergangener Kaempfe
POLL_INTERVAL = 0.25     # Sekunden zwischen Log-Polls
REFRESH_MS = 400         # Datenaktualisierung in ms
ANIM_MS = 33             # Balken-Animation in ms (~30fps)
COPY_TOP_N = 7           # Anzahl Eintraege beim Kopieren (Aion-Chat hat Zeichenlimit)

COL_SURFACE = '#1a1a19'
COL_PAGE = '#0d0d0d'
COL_TRACK = '#242422'
COL_TRACK_HOVER = '#2c2c2a'
COL_INK_PRIMARY = '#ffffff'
COL_INK_SECONDARY = '#c3c2b7'
COL_INK_MUTED = '#898781'
COL_ACCENT = '#3987e5'
COL_DANGER = '#e66767'
COL_BORDER = '#383835'

CATEGORICAL = [
    '#3987e5',  # blau
    '#d95926',  # orange
    '#199e70',  # aqua
    '#c98500',  # gelb
    '#d55181',  # magenta
    '#2ea62e',  # gruen
    '#9085e9',  # violett
    '#e66767',  # rot
]

def resource_path(relative):
    """Read-only bundled resource (e.g. icons/) - works both as a script and as a PyInstaller exe."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def user_data_dir():
    """Writable per-user folder for settings - never the install dir, which may be read-only."""
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    path = os.path.join(base, 'AionDPSMeter')
    os.makedirs(path, exist_ok=True)
    return path


ICON_PX = 20
ICON_DIR = resource_path('icons')
SETTINGS_FILE = os.path.join(user_data_dir(), 'settings.json')


def _parse_version(v):
    v = (v or '').strip().lstrip('vV')
    parts = []
    for p in v.split('.'):
        digits = ''.join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_update():
    """Hits the public GitHub Releases API. Returns update info dict, or None if already
    current / offline / no release published yet. Never raises - always safe to call."""
    try:
        req = urllib.request.Request(
            GITHUB_API_LATEST,
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'AionDPSMeter'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        tag = data.get('tag_name', '')
        if _parse_version(tag) <= _parse_version(APP_VERSION):
            return None
        assets = data.get('assets', []) or []
        asset = next((a for a in assets if a['name'].lower().endswith('setup.exe')), None)
        if asset is None:
            asset = next((a for a in assets if a['name'].lower().endswith('.exe')), None)
        if asset is None:
            return None
        return {
            'version': tag,
            'body': (data.get('body') or '').strip(),
            'url': asset['browser_download_url'],
            'size': asset.get('size', 0),
            'filename': asset['name'],
        }
    except Exception:
        return None


def download_update(url, filename, progress_cb=None):
    """Downloads the installer to a temp file and returns its path. Raises on failure."""
    dest = os.path.join(tempfile.gettempdir(), filename)
    req = urllib.request.Request(url, headers={'User-Agent': 'AionDPSMeter'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get('Content-Length', 0) or 0)
        downloaded = 0
        with open(dest, 'wb') as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)
    return dest

# Nur die auf OriginAion tatsaechlich spielbaren Klassen (kein Aethertech/Barde/Schuetze).
CLASS_ORDER = [
    ('templar', 'Templer'),
    ('gladiator', 'Gladiator'),
    ('assassin', 'Assassine'),
    ('ranger', 'Waldläufer'),
    ('sorcerer', 'Magier'),
    ('spiritmaster', 'Geisterbeschwörer'),
    ('cleric', 'Kleriker'),
    ('chanter', 'Kantor'),
]
CLASS_LABELS = dict(CLASS_ORDER, unknown='Unbekannt')

# Zeilenfarbe nach Archetyp (Aion-Farbschema): Krieger=Blau, Spaeher=Gruen, Magier=Lila, Priester=Gelb.
CLASS_COLORS = {
    'templar': '#3987e5', 'gladiator': '#3987e5',
    'assassin': '#2ea62e', 'ranger': '#2ea62e',
    'sorcerer': '#9085e9', 'spiritmaster': '#9085e9',
    'cleric': '#c98500', 'chanter': '#c98500',
    'unknown': '#5a5a57',
}
CODE_BY_LABEL = {label: code for code, label in CLASS_ORDER}
CODE_BY_LABEL['Unbekannt'] = 'unknown'


def _icon_outline(img, px=3, color=(0, 0, 0, 255)):
    """Dilates the icon's alpha silhouette into a solid black halo behind it, so the icon
    stays readable regardless of which archetype color it ends up sitting on."""
    from PIL import ImageFilter
    dilated = img.split()[3]
    for _ in range(px):
        dilated = dilated.filter(ImageFilter.MaxFilter(3))
    outline_layer = Image.new('RGBA', img.size, color)
    outline_layer.putalpha(dilated)
    return Image.alpha_composite(outline_layer, img)


def _icon_unknown():
    img = Image.new('RGBA', (96, 96), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    p = 10
    d.ellipse([(p, p), (96 - p, 96 - p)], outline=COL_INK_MUTED, width=8)
    return _icon_outline(img)


def build_class_icons():
    """code -> PIL.Image (RGBA, ICON_PX square). Falls back to a plain circle if a PNG is missing."""
    icons = {'unknown': _icon_unknown()}
    for code, _label in CLASS_ORDER:
        path = os.path.join(ICON_DIR, f'{code}.png')
        try:
            img = Image.open(path).convert('RGBA')
            icons[code] = _icon_outline(img)
        except Exception:
            icons[code] = _icon_unknown()
    return {code: img.resize((ICON_PX, ICON_PX), Image.LANCZOS) for code, img in icons.items()}


class Settings:
    """Persists log path, the user's own character name, and per-player class assignments."""

    def __init__(self):
        self.data = {'log_path': DEFAULT_LOG_PATH, 'character_name': '', 'classes': {}}
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            self.data.update({k: v for k, v in loaded.items() if k in self.data})
            if 'classes' in loaded and isinstance(loaded['classes'], dict):
                self.data['classes'] = loaded['classes']
        except Exception:
            pass

    def _save(self):
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @property
    def log_path(self):
        return self.data.get('log_path') or DEFAULT_LOG_PATH

    @property
    def character_name(self):
        return self.data.get('character_name', '')

    def set_log_path(self, path):
        self.data['log_path'] = path or DEFAULT_LOG_PATH
        self._save()

    def set_character_name(self, name):
        self.data['character_name'] = name
        self._save()

    def get_class(self, name):
        return self.data['classes'].get(name, 'unknown')

    def set_class(self, name, code):
        self.data['classes'][name] = code
        self._save()


# Best-effort Stichwoerter aus echten Aion-Skillnamen -> Klasse, gewichtet.
# Kein verifiziertes Mapping (Privatserver koennte Skills umbenannt/angepasst haben) -
# nur ein automatischer Vorschlag, der jederzeit im Optionsfenster ueberschrieben werden kann.
#
# "confirmed" = per Websuche in echten deutschen Aion-Quellen (offizielles Gameforge-Forum,
# Community-Guides) belegt, u.a. die Korrektur von "Klinge der Provokation" (faelschlich als
# Templer erkannt) -> das ist tatsaechlich ein Gladiator-Skill. Mehrwort-Phrasen sind bevorzugt,
# da einzelne Woerter wie "Provokation" oder "Schild" quer durch mehrere Klassen vorkommen und
# genau diese Art von Fehlzuordnung verursachen.
CLASS_SKILL_HINTS = [
    # Waldlaeufer (Ranger) - confirmed: Wuergepfeil, Dornenwaechter, Behindernder Schuss,
    # Erdbebenschuss, Narbe des Haumessers, Waldgeist; + aus echtem Log dieses Servers
    ('würgepfeil', 'ranger', 3), ('dornenwächter', 'ranger', 3), ('behindernder schuss', 'ranger', 3),
    ('erdbebenschuss', 'ranger', 3), ('narbe des haumessers', 'ranger', 3), ('waldgeist', 'ranger', 3),
    ('zerreißender pfeil', 'ranger', 3), ('todesschuss', 'ranger', 3), ('schwächender pfeil', 'ranger', 3),
    ('urteilsschlinge', 'ranger', 3), ('federsturm', 'ranger', 3),
    # 'schuss'/'falle' entfernt: zu kurz/generisch, hoechstes Kollisionsrisiko mit unbekannten
    # Skills anderer Klassen (siehe Kleriker->faelschlich Waldlaeufer Bugreport, Ursache unklar).
    ('pfeil', 'ranger', 2), ('bogen', 'ranger', 2),

    # Kleriker (Cleric) - ACHTUNG: Zerschmettern, Segen der Gesundheit, Geheiligter Schlag,
    # Himmelsurteil, Licht der Wiederauferstehung, Versprechen des Windes und Licht der
    # Erneuerung sind laut Quelle (5secrule.de) Basis-Skills, die BEIDE Priester-Spezialisierungen
    # (Kleriker und Kantor) gleichermassen nutzen koennen - deshalb bewusst NICHT als Kleriker-Hinweis
    # gelistet (das war die Ursache dafuer, dass ein Kantor faelschlich als Kleriker erkannt wurde).
    # Nur aus echtem Log dieses Servers, nicht in der oben genannten Shared-Liste enthalten:
    ('yustiel', 'cleric', 3), ('heilende pracht', 'cleric', 3), ('blitz-wiederherstellung', 'cleric', 3),
    ('strahlende heilung', 'cleric', 3), ('licht der verjüngung', 'cleric', 2),
    ('welle der absorption', 'cleric', 1),

    # Kantor (Chanter) - confirmed: Gesang der Inspiration, Gesang der Absorbtion,
    # Segen des Windes, Beschwoerungsformel; + aus echtem Log dieses Servers. "Gesang"/"Lied"
    # sind bewusst hoch gewichtet, da das Gesangs-Thema Kantor-exklusiv ist (Kleriker hat das nicht).
    ('gesang der inspiration', 'chanter', 3), ('gesang der absorb', 'chanter', 3),
    ('segen des windes', 'chanter', 3), ('beschwörungsformel', 'chanter', 3),
    ('ausdauerabsorption', 'chanter', 3), ('gesang', 'chanter', 3), (' lied', 'chanter', 3),

    # Gladiator - "Klinge der Provokation"/"Fluegelklinge"/"Klingenwirbel" waren hier eingetragen,
    # nachdem ein Gladiator faelschlich als Templer erkannt wurde; das duerfte aber denselben Fehler
    # wie bei Kleriker/Kantor gewesen sein - vermutlich geteilte Krieger-Basis-Skills (Provozieren ist
    # eine Tank-Mechanik, passt thematisch auch zum Templer), bestaetigt durch einen neuen Bugreport
    # (Templer faelschlich als Gladiator erkannt). Deshalb wieder entfernt statt weiter zu raten.
    # Nur noch aus echtem Log dieses Servers, unverifiziert ob wirklich Gladiator-exklusiv:
    ('glänzender schnitt', 'gladiator', 2), ('schnitt des blutschwerts', 'gladiator', 2),
    ('blutschwert', 'gladiator', 1), ('verwüstung', 'gladiator', 1), ('berserker', 'gladiator', 1),
    ('zorniger', 'gladiator', 1), ('robuster', 'gladiator', 1),

    # Assassine - aus echtem Log dieses Servers + allgemein bekannte Themen (Tarnung/Gift/Meucheln)
    ('schnelle klinge', 'assassin', 3), ('seelenschnitt', 'assassin', 3),
    ('tarnung', 'assassin', 2), ('meucheln', 'assassin', 2), ('gift', 'assassin', 1),

    # Templer - nur confirmed: Schildschlag. Die zuvor hier stehenden unbestaetigten Rateeintraege
    # (rüstungsbrecher/trutz) sind entfernt - siehe Begruendung unten.
    ('schildschlag', 'templar', 3),

    # Magier/Zauberer (Sorcerer) und Geisterbeschwoerer (Spiritmaster): absichtlich KEINE Eintraege.
    # Es gab hier nur unbestaetigte Rateeintraege (Feuerball/Frostspeer/etc., "geist der/des") ohne
    # echte Quelle - genau solche Eintraege haben bereits zweimal zu Fehlzuordnungen gefuehrt
    # (Klinge der Provokation -> faelschlich Templer; ein Kleriker -> faelschlich Zauberer, vermutlich
    # weil ein echter Kleriker-Skill zufaellig eines dieser geratenen Stichwoerter enthielt). Bis es
    # echte bestaetigte Skillnamen fuer diese beiden Klassen gibt, bleiben sie bewusst ohne Hinweise -
    # lieber ehrlich "Unbekannt" (manuell im Optionsfenster korrigierbar) als eine geratene Falschzuordnung.
]


class ClassGuesser:
    """Weighted keyword votes across every skill name a player has used this session."""

    def __init__(self):
        self.votes = {}

    def observe(self, name, skill):
        if not skill:
            return
        low = skill.lower()
        for keyword, code, weight in CLASS_SKILL_HINTS:
            if keyword in low:
                self.votes.setdefault(name, Counter())[code] += weight

    def guess(self, name):
        counter = self.votes.get(name)
        if not counter:
            return None
        return counter.most_common(1)[0][0]


CRIT_PREFIX = "Kritischer Treffer!"

DAMAGE_SKILL_RE = re.compile(
    r'^(?P<attacker>.+?) (?:hat|habt) (?P<target>.+?) durch (?:Benutzung von )?'
    r'(?P<skill>.+?) (?P<amount>[0-9]+(?:\.[0-9]{3})*) (?:kritischen )?Schaden zugef\u00fcgt\.'
)
DAMAGE_PLAIN_RE = re.compile(
    r'^(?P<attacker>.+?) (?:hat|habt) (?P<target>.+?) '
    r'(?P<amount>[0-9]+(?:\.[0-9]{3})*) (?:kritischen )?Schaden zugef\u00fcgt\.'
)
HEAL_OTHER_RE = re.compile(
    r'^(?P<target>.+?) (?:hat|habt) (?P<amount>[0-9]+(?:\.[0-9]{3})*) TP wiederhergestellt, '
    r'(?:da|weil) (?P<healer>.+?) (?P<skill>.+?) eingesetzt hat\.'
)
HEAL_SELF_RE = re.compile(
    r'^(?P<name>.+?) (?:hat|habt)(?: durch (?P<skill>.+?))? '
    r'(?P<amount>[0-9]+(?:\.[0-9]{3})*) TP wiederhergestellt\.'
)


def parse_amount(s):
    return int(s.replace('.', ''))


def normalize_name(name):
    return 'Du' if name in ('Ihr', 'ihr') else name


def parse_line(line):
    line = line.rstrip('\r\n').strip()
    if ' : ' not in line:
        return None
    _, _, rest = line.partition(' : ')
    rest = rest.strip()
    crit = False
    if rest.startswith(CRIT_PREFIX):
        crit = True
        rest = rest[len(CRIT_PREFIX):].lstrip()

    m = DAMAGE_SKILL_RE.match(rest)
    if m:
        target = m.group('target')
        if target == 'Euch':
            return None
        return {
            'type': 'damage', 'attacker': normalize_name(m.group('attacker')),
            'target': normalize_name(target), 'amount': parse_amount(m.group('amount')),
            'skill': m.group('skill'), 'crit': crit,
        }
    m = DAMAGE_PLAIN_RE.match(rest)
    if m:
        target = m.group('target')
        if target == 'Euch':
            return None
        return {
            'type': 'damage', 'attacker': normalize_name(m.group('attacker')),
            'target': normalize_name(target), 'amount': parse_amount(m.group('amount')),
            'skill': 'Angriff', 'crit': crit,
        }
    m = HEAL_OTHER_RE.match(rest)
    if m:
        return {
            'type': 'heal', 'healer': normalize_name(m.group('healer')),
            'target': normalize_name(m.group('target')), 'amount': parse_amount(m.group('amount')),
            'skill': m.group('skill'), 'crit': crit,
        }
    m = HEAL_SELF_RE.match(rest)
    if m:
        name = normalize_name(m.group('name'))
        return {
            'type': 'heal', 'healer': name, 'target': name,
            'amount': parse_amount(m.group('amount')),
            'skill': m.group('skill') or 'Regeneration', 'crit': crit,
        }
    return None


class Encounter:
    def __init__(self, label=None):
        self.label = label
        self.start = None
        self.end = None
        self.damage_events = []
        self.heal_events = []
        self._summary_cache = None

    def add_damage(self, ev, t):
        if self.start is None:
            self.start = t
        self.end = t
        self.damage_events.append((t, ev))
        self._summary_cache = None

    def add_heal(self, ev, t):
        if self.start is None:
            self.start = t
        self.end = t
        self.heal_events.append((t, ev))
        self._summary_cache = None

    def duration(self):
        if self.start is None:
            return 0.0
        end = self.end if self.end is not None else time.time()
        return max(end - self.start, 1.0)

    def summarize(self, use_cache=False):
        if use_cache and self._summary_cache is not None:
            return self._summary_cache

        players = {'Du'}
        monsters = set()
        for _, e in self.heal_events:
            players.add(e['healer'])
            players.add(e['target'])
        for _, e in self.damage_events:
            if e['attacker'] == 'Du':
                monsters.add(e['target'])

        changed = True
        while changed:
            changed = False
            for _, e in self.damage_events:
                a, t = e['attacker'], e['target']
                if t in monsters and a not in players:
                    players.add(a)
                    changed = True
                if a in players and t not in monsters and t not in players:
                    monsters.add(t)
                    changed = True

        dur = self.duration()

        overall = {}
        per_monster = {}
        monster_span = {}

        for t, e in self.damage_events:
            a, tgt, amt = e['attacker'], e['target'], e['amount']
            if a not in players:
                continue
            mon_key = tgt if tgt in monsters else 'Unbekannt'

            o = overall.setdefault(a, {'damage': 0, 'hits': 0, 'crits': 0})
            o['damage'] += amt
            o['hits'] += 1
            if e['crit']:
                o['crits'] += 1

            pm = per_monster.setdefault(mon_key, {})
            row = pm.setdefault(a, {'damage': 0, 'hits': 0, 'crits': 0})
            row['damage'] += amt
            row['hits'] += 1
            if e['crit']:
                row['crits'] += 1

            span = monster_span.setdefault(mon_key, [t, t])
            span[0] = min(span[0], t)
            span[1] = max(span[1], t)

        heal_totals = {}
        for t, e in self.heal_events:
            h = e['healer']
            row = heal_totals.setdefault(h, {'heal': 0, 'ticks': 0, 'crits': 0})
            row['heal'] += e['amount']
            row['ticks'] += 1
            if e['crit']:
                row['crits'] += 1

        total_dmg = sum(v['damage'] for v in overall.values())
        monster_totals = []
        for mon, rows in per_monster.items():
            mtotal = sum(v['damage'] for v in rows.values())
            mspan = monster_span[mon]
            mdur = max(mspan[1] - mspan[0], 1.0)
            monster_totals.append((mon, mtotal, mdur, rows))
        monster_totals.sort(key=lambda x: -x[1])

        total_heal = sum(v['heal'] for v in heal_totals.values())

        summary = {
            'duration': dur, 'total_damage': total_dmg, 'total_heal': total_heal,
            'overall': overall, 'monster_totals': monster_totals, 'heal_totals': heal_totals,
        }
        self._summary_cache = summary
        return summary


class EncounterManager:
    def __init__(self, log_path):
        self.lock = threading.Lock()
        self.session = Encounter(label='Gesamt-Sitzung')
        self.current = None
        self.history = []
        self.lines_processed = 0
        self.log_status = 'Warte auf Log-Datei...'
        self.log_path = log_path
        self.class_guesser = ClassGuesser()

    def feed(self, ev):
        t = time.time()
        with self.lock:
            if ev['type'] == 'damage':
                self.class_guesser.observe(ev['attacker'], ev.get('skill'))
                self.session.add_damage(ev, t)
                if self.current is None:
                    self.current = Encounter()
                self.current.add_damage(ev, t)
            else:
                self.class_guesser.observe(ev['healer'], ev.get('skill'))
                self.session.add_heal(ev, t)
                if self.current is not None:
                    self.current.add_heal(ev, t)

    def check_idle(self):
        t = time.time()
        with self.lock:
            if self.current is not None and self.current.end is not None:
                if t - self.current.end > IDLE_TIMEOUT:
                    self._finalize_current()

    def _finalize_current(self):
        enc = self.current
        self.current = None
        if not enc.damage_events:
            return
        enc.summarize()  # cache before freezing
        summary = enc._summary_cache
        top_monster = summary['monster_totals'][0][0] if summary['monster_totals'] else '?'
        extra = len(summary['monster_totals']) - 1
        label = time.strftime('%H:%M:%S', time.localtime(enc.start))
        label += f" - {top_monster}"
        if extra > 0:
            label += f" (+{extra})"
        label += f" ({summary['duration']:.0f}s)"
        enc.label = label
        self.history.insert(0, enc)
        del self.history[HISTORY_LIMIT:]

    def reset_all(self):
        with self.lock:
            self.session = Encounter(label='Gesamt-Sitzung')
            self.current = None
            self.history = []

    def get_labels(self):
        with self.lock:
            labels = ['Live (aktueller Kampf)', 'Gesamt-Sitzung']
            labels += [h.label for h in self.history]
            return labels

    def get_encounter_for_label(self, label):
        with self.lock:
            if label == 'Gesamt-Sitzung':
                return self.session
            if label == 'Live (aktueller Kampf)':
                if self.current is not None:
                    return self.current
                return self.history[0] if self.history else None
            for h in self.history:
                if h.label == label:
                    return h
            return None


def tail_file(manager, stop_event):
    fh = None
    pos = 0
    buf = ''
    active_path = None
    while not stop_event.is_set():
        try:
            path = manager.log_path
            if path != active_path:
                if fh is not None:
                    fh.close()
                fh = None
                buf = ''
                active_path = path

            if fh is None:
                if not os.path.exists(path):
                    manager.log_status = f'Log nicht gefunden: {path}'
                    time.sleep(1.0)
                    continue
                fh = open(path, 'r', encoding='cp1252', errors='replace', newline='')
                fh.seek(0, os.SEEK_END)
                pos = fh.tell()
                manager.log_status = f'Verbunden: {path}'

            size = os.path.getsize(path)
            if size < pos:
                fh.seek(0)
                pos = 0
                buf = ''

            chunk = fh.read()
            if chunk:
                pos = fh.tell()
                buf += chunk
                lines = buf.split('\n')
                buf = lines.pop()
                for raw in lines:
                    manager.lines_processed += 1
                    ev = parse_line(raw)
                    if ev:
                        manager.feed(ev)
            manager.check_idle()
            time.sleep(POLL_INTERVAL)
        except FileNotFoundError:
            fh = None
            manager.log_status = f'Log nicht gefunden: {manager.log_path}'
            time.sleep(1.0)
        except Exception as exc:
            manager.log_status = f'Fehler: {exc}'
            time.sleep(1.0)


def fmt_num(n):
    return f'{n:,}'.replace(',', '.')


def _rounded_rect_points(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


class MeterRow:
    """Renders a full row (track + fill + text) on a single Canvas.

    CTkLabel's 'transparent' fg_color only matches its parent frame's own
    color - it can't show through to a differently-colored sibling drawn via
    place(), which left a visible dark box behind text overlaid on the bar.
    Drawing everything on one canvas avoids that entirely.
    """
    ROW_HEIGHT = 38
    RADIUS = 6

    def __init__(self, parent, fonts):
        self.fonts = fonts
        self.canvas = tk.Canvas(parent, height=self.ROW_HEIGHT, bg=COL_SURFACE,
                                 highlightthickness=0, bd=0)
        self.width = 400
        self.color = CATEGORICAL[0]
        self.is_self = False
        self.name_text = ''
        self.sub_text = ''
        self.value_text = ''
        self.rate_text = ''
        self.icon_photo = None
        self.current_pct = 0.0
        self.target_pct = 0.0
        self.canvas.bind('<Configure>', self._on_configure)

    def _on_configure(self, event):
        if event.width > 1:
            self.width = event.width
            self._redraw()

    def _redraw(self):
        c = self.canvas
        c.delete('all')
        w, h = self.width, self.ROW_HEIGHT
        if w <= 2:
            return
        c.create_polygon(_rounded_rect_points(0, 0, w, h, self.RADIUS),
                          smooth=True, fill=COL_TRACK, outline='')
        fill_w = w * self.current_pct / 100.0
        if fill_w > 1:
            c.create_polygon(_rounded_rect_points(0, 0, fill_w, h, self.RADIUS),
                              smooth=True, fill=self.color, outline='')
        if self.is_self:
            c.create_polygon(_rounded_rect_points(1, 1, w - 1, h - 1, self.RADIUS),
                              smooth=True, fill='', outline=COL_INK_PRIMARY, width=2)
        text_x = 12
        if self.icon_photo is not None:
            c.create_image(10, h / 2, anchor='w', image=self.icon_photo)
            text_x = 10 + ICON_PX + 6
        c.create_text(text_x, h * 0.32, anchor='w', text=self.name_text,
                       fill=COL_INK_PRIMARY, font=self.fonts['name'])
        c.create_text(text_x, h * 0.76, anchor='w', text=self.sub_text,
                       fill=COL_INK_SECONDARY, font=self.fonts['sub'])
        c.create_text(w - 12, h * 0.32, anchor='e', text=self.value_text,
                       fill=COL_INK_PRIMARY, font=self.fonts['value'])
        c.create_text(w - 12, h * 0.76, anchor='e', text=self.rate_text,
                       fill=COL_INK_SECONDARY, font=self.fonts['sub'])

    def update(self, rank, name, value, pct_total, hits, crit_pct, rate, rate_label, color, is_self, icon_photo):
        prefix = '\u2605 ' if is_self else f'{rank}. '
        self.name_text = f'{prefix}{name}'
        self.sub_text = f'{hits}x  \u00b7  {crit_pct:.0f}% Krit'
        self.value_text = f'{fmt_num(value)}  ({pct_total:.1f}%)'
        self.rate_text = f'{fmt_num(int(rate))} {rate_label}'
        self.color = color
        self.is_self = is_self
        self.icon_photo = icon_photo
        self._redraw()

    def set_target_pct(self, pct):
        self.target_pct = max(0.0, min(100.0, pct))

    def tick(self):
        diff = self.target_pct - self.current_pct
        if abs(diff) < 0.08:
            self.current_pct = self.target_pct
        else:
            self.current_pct += diff * 0.3
        self._redraw()

    def grid(self, row):
        self.canvas.grid(row=row, column=0, sticky='ew', padx=6, pady=2)

    def destroy(self):
        self.canvas.destroy()


class MeterList:
    def __init__(self, parent, fonts, class_resolver, icon_photos):
        self.fonts = fonts
        self.class_resolver = class_resolver
        self.icon_photos = icon_photos
        self.scroll = ctk.CTkScrollableFrame(parent, fg_color=COL_SURFACE, corner_radius=12,
                                              scrollbar_fg_color=COL_SURFACE,
                                              scrollbar_button_color=COL_TRACK_HOVER,
                                              scrollbar_button_hover_color=COL_ACCENT)
        self.scroll.columnconfigure(0, weight=1)
        self.rows = {}
        self.row_data = {}
        self.empty_label = ctk.CTkLabel(self.scroll, text='Keine Daten in dieser Ansicht',
                                         text_color=COL_INK_MUTED, font=fonts['sub'])

    def pack(self, **kw):
        self.scroll.pack(**kw)

    def render(self, items, unit_label, rate_label):
        """items: list of (key, name, value, hits, crit_pct, rate, is_self) sorted desc by value"""
        if not items:
            for row in self.rows.values():
                row.destroy()
            self.rows = {}
            self.row_data = {}
            self.empty_label.grid(row=0, column=0, pady=20)
            return
        self.empty_label.grid_forget()

        total = sum(it[2] for it in items) or 1
        top_val = items[0][2] or 1
        seen = set()
        for i, (key, name, value, hits, crit_pct, rate, is_self) in enumerate(items):
            seen.add(key)
            row = self.rows.get(key)
            if row is None:
                row = MeterRow(self.scroll, self.fonts)
                self.rows[key] = row
                row.canvas.bind('<ButtonRelease-3>', lambda e, k=key: self._open_row_menu(e, k))
            pct_total = value / total * 100
            pct_bar = value / top_val * 100
            class_code = self.class_resolver(key)
            color = CLASS_COLORS.get(class_code, CLASS_COLORS['unknown'])
            icon_photo = self.icon_photos.get(class_code, self.icon_photos.get('unknown'))
            row.update(i + 1, name, value, pct_total, hits, crit_pct, rate, rate_label, color, is_self, icon_photo)
            row.set_target_pct(pct_bar)
            row.grid(i)
            self.row_data[key] = {
                'rank': i + 1, 'name': name, 'value': value, 'pct_total': pct_total,
                'hits': hits, 'crit_pct': crit_pct, 'rate': rate,
                'unit_label': unit_label, 'rate_label': rate_label,
            }
        stale = [k for k in self.rows if k not in seen]
        for k in stale:
            self.rows[k].destroy()
            del self.rows[k]
            self.row_data.pop(k, None)

    def tick(self):
        for row in self.rows.values():
            row.tick()

    def _popup(self, menu, event):
        self.scroll.winfo_toplevel().focus_force()
        self.scroll.winfo_toplevel().lift()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_row_menu(self, event, key):
        menu = tk.Menu(self.scroll, tearoff=0, bg=COL_TRACK, fg=COL_INK_PRIMARY,
                        activebackground=COL_ACCENT, activeforeground=COL_INK_PRIMARY, bd=0)
        menu.add_command(label='Zeile kopieren', command=lambda k=key: self._copy_row(k))
        self._popup(menu, event)

    def _copy_row(self, key):
        d = self.row_data.get(key)
        if not d:
            return
        text = (f"{d['name']}: {fmt_num(d['value'])} {d['unit_label']} ({d['pct_total']:.1f}%), "
                f"{fmt_num(int(d['rate']))} {d['rate_label']}, {d['hits']}x, {d['crit_pct']:.0f}% Krit")
        self.scroll.clipboard_clear()
        self.scroll.clipboard_append(text)
        self.scroll.update()

    def copy_all(self, header):
        ranked = sorted(self.row_data, key=lambda k: self.row_data[k]['rank'])[:COPY_TOP_N]
        parts = [
            f"{self.row_data[k]['rank']}.{self.row_data[k]['name']} "
            f"{fmt_num(self.row_data[k]['value'])}({self.row_data[k]['pct_total']:.0f}%)"
            for k in ranked
        ]
        text = f"{header}: " + ' '.join(parts) if header else ' '.join(parts)
        self.scroll.clipboard_clear()
        self.scroll.clipboard_append(text)
        self.scroll.update()


class MonsterDropdown:
    def __init__(self, parent, fonts, on_select):
        self.on_select = on_select
        self.selected = None
        self.label_to_key = {}
        self.key_to_label = {}
        self._last_values = None

        self.frame = ctk.CTkFrame(parent, fg_color='transparent')
        ctk.CTkLabel(self.frame, text='Ziel:', font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(side='left', padx=(4, 8))
        self.menu = ctk.CTkOptionMenu(self.frame, values=['Gesamt (alle Monster)'],
                                       width=320, height=32, corner_radius=8,
                                       fg_color=COL_TRACK, button_color=COL_TRACK,
                                       button_hover_color=COL_ACCENT, dropdown_fg_color=COL_TRACK,
                                       dropdown_hover_color=COL_ACCENT, text_color=COL_INK_PRIMARY,
                                       font=fonts['ui'], command=self._on_change)
        self.menu.pack(side='left')

    def pack(self, **kw):
        self.frame.pack(**kw)

    def render(self, chips):
        """chips: list of (key, label); key=None is the 'Gesamt' entry"""
        self.label_to_key = {}
        self.key_to_label = {}
        values = []
        for key, label in chips:
            self.label_to_key[label] = key
            self.key_to_label[key] = label
            values.append(label)
        if values != self._last_values:
            self.menu.configure(values=values)
            self._last_values = values
        if self.selected not in self.key_to_label:
            self.selected = None
        current_label = self.key_to_label.get(self.selected, values[0] if values else 'Gesamt (alle Monster)')
        self.menu.set(current_label)

    def _on_change(self, label):
        self.selected = self.label_to_key.get(label)
        self.on_select(self.selected)


class SettingsWindow:
    """A normal (non-popup) settings dialog - deliberately avoids tk.Menu/override-redirect
    popups, which proved unreliable for interactive class assignment in this app."""

    def __init__(self, root, settings, fonts, known_players, class_resolver, on_saved, on_check_update):
        self.settings = settings
        self.fonts = fonts
        self.class_resolver = class_resolver
        self.on_saved = on_saved
        self.class_menus = {}

        self.top = ctk.CTkToplevel(root)
        self.top.title('Optionen')
        self.top.geometry('460x600')
        self.top.configure(fg_color=COL_PAGE)
        self.top.minsize(380, 400)
        self.top.lift()
        self.top.focus_force()

        pad = {'padx': 16}

        ctk.CTkLabel(self.top, text='Spielpfad (Chat.log):', font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(anchor='w', pady=(16, 4), **pad)
        path_row = ctk.CTkFrame(self.top, fg_color='transparent')
        path_row.pack(fill='x', **pad)
        self.path_entry = ctk.CTkEntry(path_row, font=fonts['ui'], fg_color=COL_TRACK,
                                        border_color=COL_BORDER, text_color=COL_INK_PRIMARY)
        self.path_entry.insert(0, settings.log_path)
        self.path_entry.pack(side='left', fill='x', expand=True)
        ctk.CTkButton(path_row, text='...', width=36, height=28, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_ACCENT, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=self._browse).pack(side='left', padx=(6, 0))

        ctk.CTkLabel(self.top, text='Dein Charaktername:', font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(anchor='w', pady=(14, 4), **pad)
        self.name_entry = ctk.CTkEntry(self.top, font=fonts['ui'], fg_color=COL_TRACK,
                                        border_color=COL_BORDER, text_color=COL_INK_PRIMARY)
        self.name_entry.insert(0, settings.character_name)
        self.name_entry.pack(fill='x', **pad)

        ctk.CTkLabel(self.top, text='Klassen zuweisen:', font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(anchor='w', pady=(14, 4), **pad)
        ctk.CTkLabel(self.top, text='Automatisch erkannte Vorschläge sind bereits ausgewählt - '
                                     'nur bei Bedarf ändern. Manuelle Auswahl hat immer Vorrang.',
                     font=fonts['sub'], text_color=COL_INK_MUTED, wraplength=420,
                     justify='left').pack(anchor='w', pady=(0, 6), **pad)
        self.class_scroll = ctk.CTkScrollableFrame(self.top, fg_color=COL_TRACK, corner_radius=8)
        self.class_scroll.pack(fill='both', expand=True, padx=16, pady=(0, 8))
        self.class_scroll.columnconfigure(0, weight=1)
        self._populate_classes(known_players)

        btn_row = ctk.CTkFrame(self.top, fg_color='transparent')
        btn_row.pack(fill='x', padx=16, pady=(0, 16))
        ctk.CTkButton(btn_row, text='Speichern & Schließen', height=34, corner_radius=8,
                      fg_color=COL_ACCENT, hover_color=COL_ACCENT, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=self._save_close).pack(side='right')
        ctk.CTkButton(btn_row, text='Abbrechen', height=34, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=self.top.destroy).pack(side='right', padx=(0, 8))
        ctk.CTkButton(btn_row, text=f'Nach Updates suchen (v{APP_VERSION})', height=34, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=on_check_update).pack(side='left')

    def _browse(self):
        path = filedialog.askopenfilename(
            parent=self.top, title='Chat.log auswählen',
            filetypes=[('Log-Datei', '*.log'), ('Alle Dateien', '*.*')])
        if path:
            self.path_entry.delete(0, 'end')
            self.path_entry.insert(0, path)

    def _populate_classes(self, known_players):
        if not known_players:
            ctk.CTkLabel(self.class_scroll, text='Noch keine Spieler erkannt - starte einen Kampf.',
                         font=self.fonts['sub'], text_color=COL_INK_MUTED).grid(
                row=0, column=0, sticky='w', padx=8, pady=8)
            return
        display_name = self.settings.character_name
        for i, name in enumerate(sorted(known_players)):
            shown = f'{display_name} (Du)' if name == 'Du' and display_name else name
            row = ctk.CTkFrame(self.class_scroll, fg_color='transparent')
            row.grid(row=i, column=0, sticky='ew', pady=2)
            row.columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=shown, font=self.fonts['ui'],
                         text_color=COL_INK_PRIMARY).grid(row=0, column=0, sticky='w', padx=(4, 8))
            values = [label for _, label in CLASS_ORDER] + ['Unbekannt']
            menu = ctk.CTkOptionMenu(row, values=values, width=170, height=28, corner_radius=6,
                                      fg_color=COL_PAGE, button_color=COL_PAGE,
                                      button_hover_color=COL_ACCENT, dropdown_fg_color=COL_TRACK,
                                      dropdown_hover_color=COL_ACCENT, text_color=COL_INK_PRIMARY,
                                      font=self.fonts['sub'])
            menu.set(CLASS_LABELS.get(self.class_resolver(name), 'Unbekannt'))
            menu.configure(command=lambda label, n=name: self.settings.set_class(n, CODE_BY_LABEL.get(label, 'unknown')))
            menu.grid(row=0, column=1, sticky='e')
            self.class_menus[name] = menu

    def _save_close(self):
        self.settings.set_log_path(self.path_entry.get().strip())
        self.settings.set_character_name(self.name_entry.get().strip())
        self.top.destroy()
        self.on_saved()


class UpdateDialog:
    """Normal window (no popup) showing a new release's changelog with a one-click self-update."""

    def __init__(self, root, fonts, info, on_launch_installer):
        self.info = info
        self.on_launch_installer = on_launch_installer

        self.top = ctk.CTkToplevel(root)
        self.top.title('Update verfügbar')
        self.top.geometry('440x380')
        self.top.configure(fg_color=COL_PAGE)
        self.top.minsize(360, 320)
        self.top.lift()
        self.top.focus_force()

        ctk.CTkLabel(self.top, text=f'Neue Version verfügbar: {info["version"]}',
                     font=fonts['name'], text_color=COL_ACCENT).pack(anchor='w', padx=16, pady=(16, 2))
        ctk.CTkLabel(self.top, text=f'Installiert: v{APP_VERSION}',
                     font=fonts['sub'], text_color=COL_INK_MUTED).pack(anchor='w', padx=16)

        body = ctk.CTkTextbox(self.top, fg_color=COL_TRACK, text_color=COL_INK_SECONDARY,
                               font=fonts['sub'], wrap='word', corner_radius=8)
        body.pack(fill='both', expand=True, padx=16, pady=12)
        body.insert('1.0', info['body'] or '(kein Änderungsprotokoll)')
        body.configure(state='disabled')

        self.status_label = ctk.CTkLabel(self.top, text='', font=fonts['sub'], text_color=COL_INK_MUTED)
        self.status_label.pack(anchor='w', padx=16)

        btn_row = ctk.CTkFrame(self.top, fg_color='transparent')
        btn_row.pack(fill='x', padx=16, pady=(4, 16))
        self.update_btn = ctk.CTkButton(btn_row, text='Jetzt aktualisieren', height=34, corner_radius=8,
                                         fg_color=COL_ACCENT, hover_color=COL_ACCENT,
                                         text_color=COL_INK_PRIMARY, font=fonts['ui'],
                                         command=self._start_update)
        self.update_btn.pack(side='right')
        ctk.CTkButton(btn_row, text='Später', height=34, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=self.top.destroy).pack(side='right', padx=(0, 8))

    def _start_update(self):
        self.update_btn.configure(state='disabled', text='Lädt herunter...')
        self.status_label.configure(text='0%')
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        try:
            dest = download_update(self.info['url'], self.info['filename'], progress_cb=self._on_progress)
        except Exception as exc:
            self.top.after(0, lambda: self._fail(exc))
            return
        self.top.after(0, lambda: self._finish(dest))

    def _on_progress(self, downloaded, total):
        pct = int(downloaded / total * 100) if total else 0
        self.top.after(0, lambda: self.status_label.configure(text=f'{pct}% heruntergeladen'))

    def _fail(self, exc):
        self.status_label.configure(text=f'Fehler beim Herunterladen: {exc}')
        self.update_btn.configure(state='normal', text='Erneut versuchen')

    def _finish(self, installer_path):
        self.status_label.configure(text='Download fertig - Installation läuft. '
                                          'Bitte danach das Programm neu öffnen.')
        self.top.after(1200, lambda: self.on_launch_installer(installer_path))


class MeterApp:
    def __init__(self, root, manager, settings, on_quit):
        self.root = root
        self.manager = manager
        self.settings = settings
        self.on_quit = on_quit
        self.encounter_value = 'Live (aktueller Kampf)'
        self._last_summary = None
        self._last_encounter_labels = None
        self.known_players = set()
        self.update_info = None

        pil_icons = build_class_icons()
        self.icon_photos = {code: ImageTk.PhotoImage(img) for code, img in pil_icons.items()}

        root.title('Aion 4.6 DPS-Meter')
        root.geometry('1020x700')
        root.configure(fg_color=COL_PAGE)
        root.minsize(760, 480)

        self.fonts = {
            'title': ctk.CTkFont(family='Segoe UI', size=18, weight='bold'),
            'ui': ctk.CTkFont(family='Segoe UI', size=12),
            'chip': ctk.CTkFont(family='Segoe UI', size=12, weight='bold'),
            'caption': ctk.CTkFont(family='Segoe UI', size=11),
            'stat_value': ctk.CTkFont(family='Segoe UI', size=21, weight='bold'),
            'name': ctk.CTkFont(family='Segoe UI', size=14, weight='bold'),
            'sub': ctk.CTkFont(family='Segoe UI', size=11),
            'value': ctk.CTkFont(family='Segoe UI', size=14, weight='bold'),
            'status': ctk.CTkFont(family='Segoe UI', size=10),
        }

        # --- top bar ---
        top = ctk.CTkFrame(root, fg_color=COL_PAGE, corner_radius=0)
        top.pack(fill='x', padx=16, pady=(14, 4))

        title_row = ctk.CTkFrame(top, fg_color='transparent')
        title_row.pack(side='left')
        ctk.CTkLabel(title_row, text='AION DPS METER', font=self.fonts['title'],
                     text_color=COL_ACCENT).pack(side='left')
        ctk.CTkLabel(title_row, text=f'v{APP_VERSION}', font=self.fonts['caption'],
                     text_color=COL_INK_MUTED).pack(side='left', padx=(8, 0), pady=(6, 0))

        right_controls = ctk.CTkFrame(top, fg_color='transparent')
        right_controls.pack(side='right')

        self.update_btn = ctk.CTkButton(right_controls, text='Update verfügbar', width=130, height=32,
                                         corner_radius=8, fg_color='#2ea62e', hover_color='#268f26',
                                         text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                         command=self.on_show_update)

        self.reset_btn = ctk.CTkButton(right_controls, text='Reset', width=84, height=32, corner_radius=8,
                                        fg_color=COL_TRACK, hover_color=COL_DANGER,
                                        text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                        command=self.on_reset)
        self.reset_btn.pack(side='right', padx=(8, 0))

        self.settings_btn = ctk.CTkButton(right_controls, text='Optionen', width=90, height=32, corner_radius=8,
                                           fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER,
                                           text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                           command=self.on_open_settings)
        self.settings_btn.pack(side='right', padx=(8, 0))

        self.copy_btn = ctk.CTkButton(right_controls, text='Kopieren', width=96, height=32, corner_radius=8,
                                       fg_color=COL_TRACK, hover_color=COL_ACCENT,
                                       text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                       command=self.on_copy)
        self.copy_btn.pack(side='right', padx=(8, 0))

        self.encounter_menu = ctk.CTkOptionMenu(right_controls, values=[self.encounter_value],
                                                 width=280, height=32, corner_radius=8,
                                                 fg_color=COL_TRACK, button_color=COL_TRACK,
                                                 button_hover_color=COL_ACCENT,
                                                 dropdown_fg_color=COL_TRACK,
                                                 dropdown_hover_color=COL_ACCENT,
                                                 text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                                 command=self.on_encounter_change)
        self.encounter_menu.pack(side='right')

        # --- stat cards ---
        stats_row = ctk.CTkFrame(root, fg_color=COL_PAGE, corner_radius=0)
        stats_row.pack(fill='x', padx=16, pady=(10, 4))
        self.stat_values = {}
        for key, caption in [('dur', 'Dauer'), ('dmg', 'Gesamtschaden'),
                              ('dps', 'Raid-DPS'), ('heal', 'Gesamtheilung')]:
            card = ctk.CTkFrame(stats_row, fg_color=COL_TRACK, corner_radius=12)
            card.pack(side='left', expand=True, fill='x', padx=(0 if key == 'dur' else 6, 0))
            ctk.CTkLabel(card, text=caption, font=self.fonts['caption'],
                         text_color=COL_INK_MUTED).pack(anchor='w', padx=14, pady=(10, 0))
            val = ctk.CTkLabel(card, text='-', font=self.fonts['stat_value'], text_color=COL_INK_PRIMARY)
            val.pack(anchor='w', padx=14, pady=(0, 10))
            self.stat_values[key] = val

        # --- tabs ---
        self.tabview = ctk.CTkTabview(root, fg_color=COL_PAGE, corner_radius=12,
                                       segmented_button_fg_color=COL_TRACK,
                                       segmented_button_selected_color=COL_ACCENT,
                                       segmented_button_selected_hover_color=COL_ACCENT,
                                       segmented_button_unselected_color=COL_TRACK,
                                       segmented_button_unselected_hover_color=COL_TRACK_HOVER,
                                       text_color=COL_INK_PRIMARY)
        self.tabview.pack(fill='both', expand=True, padx=16, pady=8)
        self.tabview.add('Schaden')
        self.tabview.add('Heilung')

        dmg_tab = self.tabview.tab('Schaden')
        self.monster_dropdown = MonsterDropdown(dmg_tab, self.fonts, on_select=self.on_target_select)
        self.monster_dropdown.pack(fill='x', padx=2, pady=(0, 8))
        self.dmg_list = MeterList(dmg_tab, self.fonts, self._resolve_class, self.icon_photos)
        self.dmg_list.pack(fill='both', expand=True, padx=2, pady=2)

        heal_tab = self.tabview.tab('Heilung')
        self.heal_list = MeterList(heal_tab, self.fonts, self._resolve_class, self.icon_photos)
        self.heal_list.pack(fill='both', expand=True, padx=2, pady=2)

        # --- status bar ---
        status = ctk.CTkFrame(root, fg_color=COL_PAGE, corner_radius=0)
        status.pack(fill='x', padx=16, pady=(0, 10))
        self.status_label = ctk.CTkLabel(status, text='', font=self.fonts['status'],
                                          text_color=COL_INK_MUTED, anchor='w')
        self.status_label.pack(side='left')

        self.root.after(REFRESH_MS, self.refresh)
        self.root.after(ANIM_MS, self.animate)
        self.root.after(1500, lambda: self.check_for_update(manual=False))

    def on_reset(self):
        self.manager.reset_all()

    def on_open_settings(self):
        SettingsWindow(self.root, self.settings, self.fonts, self.known_players,
                        self._resolve_class, self.on_settings_saved,
                        lambda: self.check_for_update(manual=True))

    def on_settings_saved(self):
        self.manager.log_path = self.settings.log_path

    def check_for_update(self, manual):
        threading.Thread(target=self._update_check_worker, args=(manual,), daemon=True).start()

    def _update_check_worker(self, manual):
        info = check_for_update()
        self.root.after(0, lambda: self._handle_update_result(info, manual))

    def _handle_update_result(self, info, manual):
        self.update_info = info
        if info is not None:
            self.update_btn.pack(side='right', padx=(8, 0))
        elif manual:
            self.status_label.configure(text='Kein Update verfügbar - du bist aktuell.')

    def on_show_update(self):
        if self.update_info is not None:
            UpdateDialog(self.root, self.fonts, self.update_info, self._launch_installer_and_quit)

    def _launch_installer_and_quit(self, installer_path):
        try:
            subprocess.Popen([installer_path, '/SILENT', '/NORESTART'], close_fds=True)
        except Exception:
            pass
        self.root.after(300, self.on_quit)

    def _resolve_class(self, key):
        manual = self.settings.get_class(key)
        if manual != 'unknown':
            return manual
        guess = self.manager.class_guesser.guess(key)
        return guess or 'unknown'

    def on_copy(self):
        if self._last_summary is None:
            return
        dur = self._last_summary['duration']
        active = self.tabview.get()
        if active == 'Schaden':
            selected = self.monster_dropdown.selected
            target = selected if selected else 'Gesamt'
            header = f"DPS {target} ({dur:.0f}s)"
            self.dmg_list.copy_all(header)
        else:
            header = f"Heal ({dur:.0f}s)"
            self.heal_list.copy_all(header)

    def on_encounter_change(self, value):
        self.encounter_value = value

    def on_target_select(self, key):
        pass

    def animate(self):
        try:
            active = self.tabview.get()
        except Exception:
            active = 'Schaden'
        if active == 'Schaden':
            self.dmg_list.tick()
        else:
            self.heal_list.tick()
        self.root.after(ANIM_MS, self.animate)

    def refresh(self):
        labels = self.manager.get_labels()
        if self.encounter_value not in labels:
            self.encounter_value = labels[0]
        if labels != self._last_encounter_labels:
            self.encounter_menu.configure(values=labels)
            self._last_encounter_labels = labels
        self.encounter_menu.set(self.encounter_value)

        enc = self.manager.get_encounter_for_label(self.encounter_value)
        if enc is not None:
            is_live_current = (self.encounter_value == 'Live (aktueller Kampf)' and
                                self.manager.current is not None)
            summary = enc.summarize(use_cache=not is_live_current and enc is not self.manager.session)
            self._last_summary = summary
            self.known_players.update(summary['overall'].keys())
            self.known_players.update(summary['heal_totals'].keys())
            self.render_stats(summary)
            self.render_damage(summary)
            self.render_heal(summary)
        else:
            self._last_summary = None
            self.stat_values['dur'].configure(text='-')
            self.stat_values['dmg'].configure(text='-')
            self.stat_values['dps'].configure(text='-')
            self.stat_values['heal'].configure(text='-')
            self.monster_dropdown.render([(None, 'Gesamt (alle Monster)')])
            self.dmg_list.render([], 'Schaden', 'DPS')
            self.heal_list.render([], 'Heilung', 'HPS')

        self.status_label.configure(
            text=f"{self.manager.log_status}   \u00b7   Zeilen verarbeitet: {fmt_num(self.manager.lines_processed)}"
        )
        self.root.after(REFRESH_MS, self.refresh)

    def render_stats(self, summary):
        dur = summary['duration']
        self.stat_values['dur'].configure(text=f"{dur:.0f}s")
        self.stat_values['dmg'].configure(text=fmt_num(summary['total_damage']))
        self.stat_values['dps'].configure(text=fmt_num(int(summary['total_damage'] / dur)) if dur else '0')
        self.stat_values['heal'].configure(text=fmt_num(summary['total_heal']))

    def _display_name(self, key):
        if key == 'Du' and self.settings.character_name:
            return self.settings.character_name
        return key

    def render_damage(self, summary):
        chips = [(None, 'Gesamt (alle Monster)')]
        for mon, mtotal, mdur, rows in summary['monster_totals']:
            chips.append((mon, mon))
        self.monster_dropdown.render(chips)

        selected = self.monster_dropdown.selected
        if selected is None:
            dur = summary['duration']
            items = [
                (name, self._display_name(name), row['damage'], row['hits'],
                 (row['crits'] / row['hits'] * 100) if row['hits'] else 0,
                 row['damage'] / dur if dur else 0, name == 'Du')
                for name, row in summary['overall'].items()
            ]
        else:
            match = next((x for x in summary['monster_totals'] if x[0] == selected), None)
            if match is None:
                items = []
            else:
                _, _, mdur, rows = match
                items = [
                    (name, self._display_name(name), row['damage'], row['hits'],
                     (row['crits'] / row['hits'] * 100) if row['hits'] else 0,
                     row['damage'] / mdur if mdur else 0, name == 'Du')
                    for name, row in rows.items()
                ]
        items.sort(key=lambda it: -it[2])
        self.dmg_list.render(items, 'Schaden', 'DPS')

    def render_heal(self, summary):
        dur = summary['duration']
        items = [
            (name, self._display_name(name), row['heal'], row['ticks'],
             (row['crits'] / row['ticks'] * 100) if row['ticks'] else 0,
             row['heal'] / dur if dur else 0, name == 'Du')
            for name, row in summary['heal_totals'].items()
        ]
        items.sort(key=lambda it: -it[2])
        self.heal_list.render(items, 'Heilung', 'HPS')


def main():
    settings = Settings()
    if len(sys.argv) > 1:
        settings.set_log_path(sys.argv[1])

    manager = EncounterManager(settings.log_path)
    stop_event = threading.Event()
    t = threading.Thread(target=tail_file, args=(manager, stop_event), daemon=True)
    t.start()

    ctk.set_appearance_mode('dark')
    ctk.set_default_color_theme('blue')
    root = ctk.CTk()

    def on_quit():
        stop_event.set()
        root.destroy()

    app = MeterApp(root, manager, settings, on_quit)
    root.protocol('WM_DELETE_WINDOW', on_quit)
    root.mainloop()


if __name__ == '__main__':
    main()
