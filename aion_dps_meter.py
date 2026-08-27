"""Aion 4.6 DPS/Heal-Meter - liest live aus Chat.log, veraendert keine Spieldateien."""

import os
import re
import io
import sys
import html
import json
import time
import threading
import subprocess
import tempfile
import webbrowser
import urllib.request
from collections import Counter
import tkinter as tk
from tkinter import filedialog
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

APP_VERSION = '1.5.0'
GITHUB_REPO = 'MaaxxsDev/Aion-DPS-Meter'
GITHUB_API_LATEST = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'

DEFAULT_LOG_PATH = r"D:\Origin\Chat.log"
IDLE_TIMEOUT = 600       # Sekunden ohne Kampfaktion -> aktueller Kampf gilt als beendet und
                         # wandert in den Verlauf (dieselbe Schwelle wie fuer neue Gruppe/Teleport)
HISTORY_LIMIT = 20       # Anzahl gespeicherter vergangener Kaempfe
POLL_INTERVAL = 0.25     # Sekunden zwischen Log-Polls
REFRESH_MS = 400         # Datenaktualisierung in ms
ANIM_MS = 33             # Balken-Animation in ms (~30fps)
COPY_TOP_N = 5           # Anzahl Eintraege beim Kopieren (Aion-Chat hat Zeichenlimit)

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


def user_data_dir(folder_name='AionDPSMeter'):
    """Writable per-user folder for settings - never the install dir, which may be read-only."""
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    path = os.path.join(base, folder_name)
    os.makedirs(path, exist_ok=True)
    return path


ICON_PX = 32
ICON_DIR = resource_path('icons')


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
    'templar', 'gladiator', 'assassin', 'ranger',
    'sorcerer', 'spiritmaster', 'cleric', 'chanter',
]
CLASS_LABELS_BY_LANG = {
    'de': {
        'templar': 'Templer', 'gladiator': 'Gladiator', 'assassin': 'Assassine',
        'ranger': 'Waldläufer', 'sorcerer': 'Magier', 'spiritmaster': 'Geisterbeschwörer',
        'cleric': 'Kleriker', 'chanter': 'Kantor',
    },
    'en': {
        'templar': 'Templar', 'gladiator': 'Gladiator', 'assassin': 'Assassin',
        'ranger': 'Ranger', 'sorcerer': 'Sorcerer', 'spiritmaster': 'Spiritmaster',
        'cleric': 'Cleric', 'chanter': 'Chanter',
    },
}

# Zeilenfarbe nach Archetyp (Aion-Farbschema): Krieger=Blau, Spaeher=Gruen, Magier=Lila, Priester=Gelb.
CLASS_COLORS = {
    'templar': '#3987e5', 'gladiator': '#3987e5',
    'assassin': '#2ea62e', 'ranger': '#2ea62e',
    'sorcerer': '#9085e9', 'spiritmaster': '#9085e9',
    'cleric': '#c98500', 'chanter': '#c98500',
    'unknown': '#5a5a57',
}


def class_labels():
    """code -> localized display label, plus the 'unknown' fallback, for the current language."""
    labels = dict(CLASS_LABELS_BY_LANG.get(current_lang(), CLASS_LABELS_BY_LANG['de']))
    labels['unknown'] = tr('unknown_class')
    return labels


def code_by_label():
    return {label: code for code, label in class_labels().items()}


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
    for code in CLASS_ORDER:
        path = os.path.join(ICON_DIR, f'{code}.png')
        try:
            img = Image.open(path).convert('RGBA')
            icons[code] = _icon_outline(img)
        except Exception:
            icons[code] = _icon_unknown()
    return {code: img.resize((ICON_PX, ICON_PX), Image.LANCZOS) for code, img in icons.items()}


# Seed list for Settings.known_fortresses (Raidmodus fortress selector), read off the user's own
# siege schedule screenshot (originaion.com/schedule, Siege+Weekly filters - the live page itself
# is JS-rendered and unfetchable, see aion_ap_tracking memory). "Miren/Krotan/Kysis" was shown as
# one combined schedule slot but split into 3 separate selectable entries here, since during an
# actual raid the group is at exactly one specific fortress, not all three at once. Only applies to
# a brand new settings file - an existing one keeps whatever the user has already typed/edited.
DEFAULT_FORTRESSES = [
    'Sulfur', "Siel's Western", "Siel's Eastern", 'Roah', 'Asteria',
    'Temple of Scales', 'Altar of Avarice', 'Vorgaltem Citadel', 'Crimson Temple',
    'Tiamaranta', 'Sillus', 'Silona', 'Pradeth', 'Miren', 'Krotan', 'Kysis', 'Divine',
]


class Settings:
    """Persists log path, the user's own character name, language, and per-player class assignments.

    An optional profile keeps a second, fully separate settings file (own log path, character
    name, class assignments) - for dual-boxing, where two game clients write into two separate
    Chat.log files and each needs its own identity, launch a second instance with a profile name
    as the second command-line argument so the two don't share (and overwrite) one settings file.
    """

    def __init__(self, profile=None):
        folder = 'AionDPSMeter'
        if profile:
            safe = re.sub(r'[^A-Za-z0-9_-]', '', profile) or 'profile'
            folder = f'AionDPSMeter_{safe}'
        self.settings_file = os.path.join(user_data_dir(folder), 'settings.json')
        self.data = {'log_path': DEFAULT_LOG_PATH, 'character_name': '', 'language': 'de',
                     'dual_account_mode': False, 'hide_npcs': False, 'classes': {},
                     'known_fortresses': list(DEFAULT_FORTRESSES)}
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            self.data.update({k: v for k, v in loaded.items() if k in self.data})
            if 'classes' in loaded and isinstance(loaded['classes'], dict):
                self.data['classes'] = loaded['classes']
            if 'known_fortresses' in loaded and isinstance(loaded['known_fortresses'], list):
                self.data['known_fortresses'] = [str(x) for x in loaded['known_fortresses']]
        except Exception:
            pass
        if self.data.get('language') not in ('de', 'en'):
            self.data['language'] = 'de'

    def _save(self):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @property
    def log_path(self):
        return self.data.get('log_path') or DEFAULT_LOG_PATH

    @property
    def character_name(self):
        return self.data.get('character_name', '')

    @property
    def language(self):
        return self.data.get('language', 'de')

    @property
    def dual_account_mode(self):
        return bool(self.data.get('dual_account_mode', False))

    @property
    def hide_npcs(self):
        return bool(self.data.get('hide_npcs', False))

    @property
    def known_fortresses(self):
        return list(self.data.get('known_fortresses', []))

    def set_log_path(self, path):
        self.data['log_path'] = path or DEFAULT_LOG_PATH
        self._save()

    def set_character_name(self, name):
        self.data['character_name'] = name
        self._save()

    def set_language(self, lang):
        self.data['language'] = lang if lang in ('de', 'en') else 'de'
        self._save()

    def set_dual_account_mode(self, enabled):
        self.data['dual_account_mode'] = bool(enabled)
        self._save()

    def set_hide_npcs(self, enabled):
        self.data['hide_npcs'] = bool(enabled)
        self._save()

    def add_fortress(self, name):
        name = (name or '').strip()
        if not name or name in self.data['known_fortresses']:
            return
        self.data['known_fortresses'].append(name)
        self._save()

    def get_class(self, name):
        return self.data['classes'].get(name, 'unknown')

    def set_class(self, name, code):
        self.data['classes'][name] = code
        self._save()


# Resolves UI text and parsing/detection language at runtime. Set once in main() to the live
# Settings instance - tr() always reads settings.language fresh, so backend logic (parsing,
# status text, dropdown labels rebuilt every refresh cycle) reacts immediately to a language
# change, while widget text already drawn on screen needs an app restart to relabel itself.
_SETTINGS = None


def current_lang():
    return _SETTINGS.language if _SETTINGS is not None else 'de'


STRINGS = {
    'de': {
        'options': 'Optionen', 'settings_title': 'Optionen',
        'log_path_label': 'Spielpfad (Chat.log):',
        'char_name_label': 'Dein Charaktername:',
        'language_label': 'Sprache / Spielsprache:',
        'language_hint': 'Steuert sowohl die Programmsprache als auch die Erkennung im Chat.log '
                          '(muss zur Spielsprache passen). Ein Neustart ist nötig, damit die '
                          'Programmsprache überall greift.',
        'dual_account_label': 'Dual-Account-Modus (Beta)',
        'dual_account_hint': 'Trennt "Du" anhand des benutzten Skills in mehrere eigene Zeilen '
                              '(z.B. "Du (Templer)" / "Du (Kantor)") - für zwei gleichzeitig '
                              'gespielte Accounts über dieselbe Chat.log. Nicht 100% zuverlässig: '
                              'Skills ohne erkannte Klasse (z.B. reiner Angriff) werden dem zuletzt '
                              'erkannten Account zugerechnet. Nur aktivieren, wenn du wirklich '
                              'zwei Accounts gleichzeitig spielst - sonst wird dein eigener Schaden '
                              'unnötig aufgesplittet.',
        'assign_classes_label': 'Klassen zuweisen:',
        'assign_classes_hint': 'Automatisch erkannte Vorschläge sind bereits ausgewählt - nur bei '
                                'Bedarf ändern. Manuelle Auswahl hat immer Vorrang.',
        'no_players_yet': 'Noch keine Spieler erkannt - starte einen Kampf.',
        'save_close': 'Speichern & Schließen',
        'cancel': 'Abbrechen',
        'check_updates': 'Nach Updates suchen (v{version})',
        'choose_log_title': 'Chat.log auswählen',
        'log_file_filter': 'Log-Datei',
        'all_files_filter': 'Alle Dateien',
        'you_suffix': ' (Du)',
        'unknown_class': 'Unbekannt',
        'update_available_title': 'Update verfügbar',
        'update_available_new': 'Neue Version verfügbar: {version}',
        'update_installed': 'Installiert: v{version}',
        'no_changelog': '(kein Änderungsprotokoll)',
        'update_now': 'Jetzt aktualisieren',
        'later': 'Später',
        'downloading': 'Lädt herunter...',
        'downloaded_pct': '{pct}% heruntergeladen',
        'download_error': 'Fehler beim Herunterladen: {error}',
        'retry': 'Erneut versuchen',
        'download_done': 'Download fertig - Installation läuft. Bitte danach das Programm neu öffnen.',
        'app_title': 'Aion 4.6 DPS-Meter',
        'app_header': 'AION DPS METER',
        'reset': 'Reset',
        'copy': 'Kopieren',
        'stat_duration': 'Dauer', 'stat_damage': 'Gesamtschaden',
        'stat_dps': 'Raid-DPS', 'stat_heal': 'Gesamtheilung',
        'tab_damage': 'Schaden', 'tab_heal': 'Heilung', 'tab_loot': 'Loottable', 'tab_ap': 'Abysspunkte',
        'loot_col_player': 'Spieler', 'loot_col_item': 'Item', 'loot_col_qty': 'Menge',
        'ap_total_label': 'Gesamt Abysspunkte', 'ap_reset_button': 'AP zurücksetzen',
        'raid_mode_start': 'Raidmodus starten', 'raid_mode_stop': 'Raidmodus beenden',
        'fortress_label': 'Festung:', 'fortress_placeholder': 'Festung wählen oder eingeben',
        'fortress_col_name': 'Festung', 'fortress_col_ap': 'AP',
        'ap_self_only_hint': 'Nur deine eigenen AP - das Spiel meldet AP-Gewinne anderer Spieler nicht im Chat.log.',
        'item_popup_loading': 'Lade Item-Informationen…',
        'item_popup_error': 'Konnte Item-Informationen nicht laden (offline oder Seite nicht erreichbar).',
        'item_popup_stock_disclaimer': 'Basiert auf Standard-Aion-Daten (Aion Codex) - kann von OriginAion abweichen, falls der Server dieses Item angepasst hat.',
        'item_popup_open_browser': 'Auf Aion Codex öffnen',
        'item_popup_price_label': 'Preis',
        'item_popup_buy': 'Kauf',
        'item_popup_sell': 'Verkauf',
        'no_update_current': 'Kein Update verfügbar - du bist aktuell.',
        'live_label': 'Live (aktueller Kampf)', 'total_session': 'Gesamt-Sitzung',
        'total_all_monsters': 'Gesamt (alle Monster)',
        'copy_total_fallback': 'Gesamt',
        'target_label': 'Ziel:',
        'hide_npcs_label': 'Nur Spieler anzeigen',
        'lines_processed': 'Zeilen verarbeitet: {n}',
        'copy_row': 'Zeile kopieren',
        'no_data_view': 'Keine Daten in dieser Ansicht',
        'crit_short': 'Krit',
        'unit_damage': 'Schaden', 'unit_heal': 'Heilung',
        'waiting_for_log': 'Warte auf Log-Datei...',
        'connected': 'Verbunden: {path}',
        'log_not_found': 'Log nicht gefunden: {path}',
        'error_generic': 'Fehler: {error}',
    },
    'en': {
        'options': 'Settings', 'settings_title': 'Settings',
        'log_path_label': 'Game path (Chat.log):',
        'char_name_label': 'Your character name:',
        'language_label': 'Language / Game language:',
        'language_hint': 'Controls both the program language and the Chat.log detection (must '
                          'match your game’s language). Restart the app for the program '
                          'language to apply everywhere.',
        'dual_account_label': 'Dual-account mode (Beta)',
        'dual_account_hint': 'Splits "Du" into several rows based on the skill used '
                              '(e.g. "Du (Templar)" / "Du (Chanter)") - for two accounts played '
                              'at once through the same Chat.log. Not 100% reliable: skills with '
                              'no detected class (e.g. a plain attack) get attributed to whichever '
                              'account was detected most recently. Only enable this if you\'re '
                              'really playing two accounts at once - otherwise it will needlessly '
                              'split up your own damage.',
        'assign_classes_label': 'Assign classes:',
        'assign_classes_hint': 'Automatically detected suggestions are already selected - only '
                                'change if needed. A manual choice always takes priority.',
        'no_players_yet': 'No players detected yet - start a fight.',
        'save_close': 'Save & Close',
        'cancel': 'Cancel',
        'check_updates': 'Check for updates (v{version})',
        'choose_log_title': 'Select Chat.log',
        'log_file_filter': 'Log file',
        'all_files_filter': 'All files',
        'you_suffix': ' (You)',
        'unknown_class': 'Unknown',
        'update_available_title': 'Update available',
        'update_available_new': 'New version available: {version}',
        'update_installed': 'Installed: v{version}',
        'no_changelog': '(no changelog)',
        'update_now': 'Update now',
        'later': 'Later',
        'downloading': 'Downloading...',
        'downloaded_pct': '{pct}% downloaded',
        'download_error': 'Download error: {error}',
        'retry': 'Retry',
        'download_done': 'Download complete - installation running. Please reopen the program afterwards.',
        'app_title': 'Aion 4.6 DPS Meter',
        'app_header': 'AION DPS METER',
        'reset': 'Reset',
        'copy': 'Copy',
        'stat_duration': 'Duration', 'stat_damage': 'Total Damage',
        'stat_dps': 'Raid DPS', 'stat_heal': 'Total Healing',
        'tab_damage': 'Damage', 'tab_heal': 'Healing', 'tab_loot': 'Loot Table', 'tab_ap': 'Abyss Points',
        'loot_col_player': 'Player', 'loot_col_item': 'Item', 'loot_col_qty': 'Qty',
        'ap_total_label': 'Total Abyss Points', 'ap_reset_button': 'Reset AP',
        'raid_mode_start': 'Start Raid Mode', 'raid_mode_stop': 'Stop Raid Mode',
        'fortress_label': 'Fortress:', 'fortress_placeholder': 'Select or type a fortress',
        'fortress_col_name': 'Fortress', 'fortress_col_ap': 'AP',
        'ap_self_only_hint': "Your own AP only - the game doesn't report other players' AP gains in Chat.log.",
        'item_popup_loading': 'Loading item info…',
        'item_popup_error': "Couldn't load item info (offline or the site is unreachable).",
        'item_popup_stock_disclaimer': 'Based on stock Aion data (Aion Codex) - may differ from OriginAion if the server customized this item.',
        'item_popup_open_browser': 'Open on Aion Codex',
        'item_popup_price_label': 'Price',
        'item_popup_buy': 'Buy',
        'item_popup_sell': 'Sell',
        'no_update_current': 'No update available - you are up to date.',
        'live_label': 'Live (current fight)', 'total_session': 'Total session',
        'total_all_monsters': 'Total (all monsters)',
        'copy_total_fallback': 'Total',
        'target_label': 'Target:',
        'hide_npcs_label': 'Show players only',
        'lines_processed': 'Lines processed: {n}',
        'copy_row': 'Copy row',
        'no_data_view': 'No data in this view',
        'crit_short': 'Crit',
        'unit_damage': 'Damage', 'unit_heal': 'Healing',
        'waiting_for_log': 'Waiting for log file...',
        'connected': 'Connected: {path}',
        'log_not_found': 'Log not found: {path}',
        'error_generic': 'Error: {error}',
    },
}


def tr(key, **kwargs):
    lang = current_lang()
    text = STRINGS.get(lang, STRINGS['de']).get(key) or STRINGS['de'].get(key, key)
    return text.format(**kwargs) if kwargs else text


# Datenquelle: aus dem AionGermany/aion-germany Emulator-Projekt (GitHub) extrahiert -
# skill_tree.xml (Skill->Klassen-ID, echte Spielmechanik) verknuepft mit den tatsaechlichen
# deutschen Skillnamen aus diesem Client (D:\Origin\L10N\2_deu\data\Strings\
# client_strings_skill.xml). Nur Skills, die eindeutig und ueber alle Raenge hinweg konsistent
# genau einer Klasse zugeordnet sind - mehrdeutige/geteilte Basis-Skills sowie Eintraege, die
# als Teilstring in einem Skill einer ANDEREN Klasse vorkommen (z.B. "Urteil" in
# "Urteilsschlinge"), wurden automatisch aussortiert, um Fehlzuordnungen wie bei den fruehen
# Handeintraegen zu vermeiden. Ergaenzt um Skills, die im echten Chat.log dieses Servers
# bestaetigt beobachtet wurden.
CLASS_SKILL_HINTS_DE = [
    # Waldlaeufer (52)
    ('atem der natur', 'ranger', 3), ('auge des angriffs', 'ranger', 3), ('betäubender schuss', 'ranger', 3),
    ('blitzpfeil', 'ranger', 3), ('bogen des segens', 'ranger', 3), ('bogenreichweite erhöhen', 'ranger', 3),
    ('böenpfeil', 'ranger', 3), ('entschlossener widerstand', 'ranger', 3),
    ('entschlossenheit des jägers', 'ranger', 3), ('explosionspfeil', 'ranger', 3),
    ('falle der hellsicht', 'ranger', 3), ('falle des rachegeistes', 'ranger', 3),
    ('falle: abbremsen', 'ranger', 3), ('falle: sandsturm', 'ranger', 3), ('falle: schock', 'ranger', 3),
    ('federsturm', 'ranger', 3), ('fesselpfeil', 'ranger', 3), ('finaler sturmangriff', 'ranger', 3),
    ('flug: angriffsreichweite erhöhen', 'ranger', 3), ('flug: segen des pfeilgottes', 'ranger', 3),
    ('griffonix-pfeil', 'ranger', 3), ('heckenschuss', 'ranger', 3), ('lodernde falle', 'ranger', 3),
    ('parieren erhöhen', 'ranger', 3), ('pfeil des anfalls', 'ranger', 3), ('pfeil des siegels', 'ranger', 3),
    ('pfeil des tobenden windes', 'ranger', 3), ('pfeile schärfen', 'ranger', 3), ('pfeilhagel', 'ranger', 3),
    ('pfeilschlag', 'ranger', 3), ('präzision erhöhen', 'ranger', 3), ('quälender pfeil', 'ranger', 3),
    ('raserei des mistral', 'ranger', 3), ('rückzugsschlag', 'ranger', 3), ('schlaffalle', 'ranger', 3),
    ('schlafpfeil', 'ranger', 3), ('schneller atem', 'ranger', 3), ('schweigepfeil', 'ranger', 3),
    ('schwächender pfeil', 'ranger', 3), ('sofortiges sprinten', 'ranger', 3), ('spiralpfeil', 'ranger', 3),
    ('stärkendes auge', 'ranger', 3), ('tigerauge', 'ranger', 3), ('todesschuss', 'ranger', 3),
    ('tödlicher pfeil', 'ranger', 3), ('umschlingender schuss', 'ranger', 3), ('urteilsschlinge', 'ranger', 3),
    ('verwandlung: mau', 'ranger', 3), ('vorsichtiges auge', 'ranger', 3), ('zerreißender pfeil', 'ranger', 3),
    ('zielgenauer pfeil', 'ranger', 3), ('äther-pfeil', 'ranger', 3),

    # Kleriker (53)
    ('aions sturm', 'cleric', 3), ('amplifikation', 'cleric', 3), ('beschwörung: edle energie', 'cleric', 3),
    ('beschwörung: heilende energie', 'cleric', 3), ('beschwörung: heiliger diener', 'cleric', 3),
    ('bestrafende erde', 'cleric', 3), ('blendendes licht', 'cleric', 3), ('blitz der vergeltung', 'cleric', 3),
    ('blitz des göttlichen angriffs', 'cleric', 3), ('blitz herbeirufen', 'cleric', 3),
    ('blitz-wiederherstellung', 'cleric', 3), ('edle anmut', 'cleric', 3), ('eiternde wunde', 'cleric', 3),
    ('explosion der macht', 'cleric', 3), ('flug: erholung verbessern', 'cleric', 3),
    ('flug: segen des heilgottes', 'cleric', 3), ('freispruch', 'cleric', 3), ('gebet des fokus', 'cleric', 3),
    ('geheiligter schlag', 'cleric', 3), ('gesegneter schild', 'cleric', 3), ('göttliche berührung', 'cleric', 3),
    ('göttlicher funke', 'cleric', 3), ('hand der reinkarnation', 'cleric', 3), ('heilende pracht', 'cleric', 3),
    ('heilung erhöhen', 'cleric', 3), ('kette des leidens', 'cleric', 3), ('licht der verjüngung', 'cleric', 3),
    ('licht der wiederauferstehung', 'cleric', 3), ('licht der wiederherstellung', 'cleric', 3),
    ('macht der zerstörung', 'cleric', 3), ('mitfühlende heilung', 'cleric', 3),
    ('pandämonium-schutz', 'cleric', 3), ('pracht der reinigung', 'cleric', 3),
    ('pracht der wiedergeburt', 'cleric', 3), ('pracht der wiederherstellung', 'cleric', 3),
    ('reinigendes licht', 'cleric', 3), ('rettende hand', 'cleric', 3), ('schwächende explosion', 'cleric', 3),
    ('sprint-fertigkeit', 'cleric', 3), ('strahlende heilung', 'cleric', 3),
    ('undurchdringlicher schleier', 'cleric', 3), ('unsterblicher mantel', 'cleric', 3),
    ('welle der absorption', 'cleric', 3), ('welle der reinigung', 'cleric', 3),
    ('wiederauferstehungs-beschwörung', 'cleric', 3), ('wissen des weisen', 'cleric', 3),
    ('wohlwollen', 'cleric', 3), ('wort der zerstörung', 'cleric', 3), ('yustiels licht', 'cleric', 3),
    ('zorn der erde', 'cleric', 3), ('zornaufbaugeschwindigkeit reduzieren', 'cleric', 3),
    ('zustand umkehren', 'cleric', 3), ('zügelung', 'cleric', 3),

    # Kantor (53)
    ('abstumpfender hieb', 'chanter', 3), ('aufwachen', 'chanter', 3), ('ausdauer-strahlung', 'chanter', 3),
    ('ausdauerabsorption', 'chanter', 3), ('bergrutsch', 'chanter', 3), ('beschwörungsformel', 'chanter', 3),
    ('beschwörungsformel der inspiration', 'chanter', 3), ('blitzschlag', 'chanter', 3),
    ('desorientierender hieb', 'chanter', 3), ('elementare abschirmung', 'chanter', 3),
    ('flug: mantra-reichweite erhöhen', 'chanter', 3), ('flug: segen des verstärkungsgottes', 'chanter', 3),
    ('gefangennahme', 'chanter', 3), ('gesang der absorb', 'chanter', 3), ('gesang der inspiration', 'chanter', 3),
    ('geschwindigkeitsmantra', 'chanter', 3), ('glühender hieb', 'chanter', 3), ('heilschub', 'chanter', 3),
    ('heilverbindung', 'chanter', 3), ('mantra-reichweite erhöhen', 'chanter', 3),
    ('marchutans schutz', 'chanter', 3), ('meteor-hieb', 'chanter', 3), ('pentagramm-schock', 'chanter', 3),
    ('perfektes parieren', 'chanter', 3), ('physischer angriff erhöhen', 'chanter', 3),
    ('rasende ermutigung', 'chanter', 3), ('raum-zeit-flucht', 'chanter', 3), ('resonanztrübung', 'chanter', 3),
    ('riposte', 'chanter', 3), ('rückkopplung', 'chanter', 3), ('schall-schwung', 'chanter', 3),
    ('schildmantra', 'chanter', 3), ('schlag der vehemenz', 'chanter', 3), ('schutz des felsens', 'chanter', 3),
    ('seelenschloss', 'chanter', 3), ('segen des felsens', 'chanter', 3), ('segen des windes', 'chanter', 3),
    ('seismischer bodendruck', 'chanter', 3), ('sicherer schutzbereich', 'chanter', 3),
    ('spaltschlag', 'chanter', 3), ('spritzender schwung', 'chanter', 3), ('tödlicher hieb', 'chanter', 3),
    ('unbesiegbarkeitsmantra', 'chanter', 3), ('vernichtungsfeuer', 'chanter', 3),
    ('versprechen der erde', 'chanter', 3), ('wiederhergestellte ausdauer', 'chanter', 3),
    ('wiederherstellungszauber', 'chanter', 3), ('wiederholtes zerschmettern', 'chanter', 3),
    ('zauber der flinkheit', 'chanter', 3), ('zauber des durchbruchs', 'chanter', 3),
    ('zauber des lebens', 'chanter', 3), ('zauber des schutzes', 'chanter', 3),
    ('zauberformel des sturms', 'chanter', 3),

    # Gladiator (53)
    ('blutsaugender schlag', 'gladiator', 3), ('blutsaugendes schwert', 'gladiator', 3),
    ('druckwelle', 'gladiator', 3), ('durchdringendes zerreißen', 'gladiator', 3),
    ('energie-explosion', 'gladiator', 3), ('erbebenwelle', 'gladiator', 3), ('erdbebenwelle', 'gladiator', 3),
    ('explosion der wut', 'gladiator', 3), ('explosion des zorns', 'gladiator', 3),
    ('flug: phys. angriff erhöhen', 'gladiator', 3), ('flug: segen des schwertgottes', 'gladiator', 3),
    ('flügel stärken', 'gladiator', 3), ('flügelklinge', 'gladiator', 3),
    ('fortgeschrittene stangenwaffe', 'gladiator', 3), ('fortgeschrittene stangewaffe', 'gladiator', 3),
    ('gefängnis', 'gladiator', 3), ('geheul', 'gladiator', 3), ('glänzender schnitt', 'gladiator', 3),
    ('kampfvorbereitung', 'gladiator', 3), ('knöchel-verlangsamung', 'gladiator', 3),
    ('konter der absorption', 'gladiator', 3), ('konzentriertes blocken', 'gladiator', 3),
    ('kraftreserve', 'gladiator', 3), ('körperhieb', 'gladiator', 3), ('luftgefängnis', 'gladiator', 3),
    ('niederwerfen erhöhen', 'gladiator', 3), ('rasender hieb', 'gladiator', 3),
    ('rüstung der rache', 'gladiator', 3), ('scharfer schlag', 'gladiator', 3),
    ('schnitt des blutschwerts', 'gladiator', 3), ('schwerer zertrümmerungsschlag', 'gladiator', 3),
    ('schwächender schlag', 'gladiator', 3), ('sehnenzerstückler', 'gladiator', 3),
    ('seismische woge', 'gladiator', 3), ('sicherer schlag', 'gladiator', 3), ('sprungschlitzer', 'gladiator', 3),
    ('sturmhaltung', 'gladiator', 3), ('tanzende flammenklinge', 'gladiator', 3),
    ('technischer konter', 'gladiator', 3), ('todestreffer', 'gladiator', 3),
    ('unerschrockener geist', 'gladiator', 3), ('verkrüppelnder schnitt', 'gladiator', 3),
    ('verteidigungsvorbereitung', 'gladiator', 3), ('welle der erschöpfung', 'gladiator', 3),
    ('welle der regeneration', 'gladiator', 3), ('welle des zorns', 'gladiator', 3),
    ('wiederholter klingenwirbel', 'gladiator', 3), ('wiederholter körperschlag', 'gladiator', 3),
    ('wirbelnder schlag', 'gladiator', 3), ('wutabsorption', 'gladiator', 3), ('zauberabwehr', 'gladiator', 3),
    ('zerschmetternder schlag', 'gladiator', 3), ('zorniger schlag', 'gladiator', 3),

    # Assassine (52)
    ('angriff aus dem hinterhalt', 'assassin', 3), ('attacke aus dem hinterhalt', 'assassin', 3),
    ('attentat', 'assassin', 3), ('auge des zorns', 'assassin', 3), ('ausweichrate erhöhen', 'assassin', 3),
    ('beschleunigter untergang', 'assassin', 3), ('blendende explosion', 'assassin', 3),
    ('blitzschnell', 'assassin', 3), ('blitzschneller angriff', 'assassin', 3),
    ('brüllen der bestie', 'assassin', 3), ('eid der präzision', 'assassin', 3), ('fluchthaltung', 'assassin', 3),
    ('flug: krit. trefferrate erhöhen', 'assassin', 3), ('flug: segen des mordgottes', 'assassin', 3),
    ('giftangriff', 'assassin', 3), ('himmelsklinge', 'assassin', 3), ('hinterhalt', 'assassin', 3),
    ('ketten-siegelgravur', 'assassin', 3), ('kettenvernichtung', 'assassin', 3), ('kreuzschnitt', 'assassin', 3),
    ('krit. trefferchance erhöhen', 'assassin', 3), ('magiewiderstand erhöhen', 'assassin', 3),
    ('massaker', 'assassin', 3), ('reißerklauenschlag', 'assassin', 3), ('schattenfall', 'assassin', 3),
    ('schattenillusion', 'assassin', 3), ('schattenschritt', 'assassin', 3), ('schlag der bestie', 'assassin', 3),
    ('schnelle klinge', 'assassin', 3), ('schneller vertrag', 'assassin', 3), ('seelenschnitt', 'assassin', 3),
    ('siegel-explosion', 'assassin', 3), ('siegel-klinge', 'assassin', 3), ('siegel-schweigen', 'assassin', 3),
    ('siegelangriff', 'assassin', 3), ('siegelgravur', 'assassin', 3), ('sinnesverstärkung', 'assassin', 3),
    ('spiralschnitt', 'assassin', 3), ('sprengpulveranwendung', 'assassin', 3), ('sprintangriff', 'assassin', 3),
    ('sprung der bestie', 'assassin', 3), ('tritt der bestie', 'assassin', 3), ('tödlicher fokus', 'assassin', 3),
    ('tödliches gift auftragen', 'assassin', 3), ('vertrag des ausweichens', 'assassin', 3),
    ('verwandlung: schlächter', 'assassin', 3), ('wiederholte siegel-explosion', 'assassin', 3),
    ('windschritt', 'assassin', 3), ('wirbelwindschnitt', 'assassin', 3),
    ('überfall aus dem hinterhalt', 'assassin', 3), ('überfall-schlag', 'assassin', 3),
    ('überraschungsangriff', 'assassin', 3),

    # Templer (47)
    ('barbarischer hieb', 'templar', 3), ('bestrafende welle', 'templar', 3), ('bestrafung', 'templar', 3),
    ('blocken erhöhen', 'templar', 3), ('eisenhaut', 'templar', 3), ('empyrianische vorsehung', 'templar', 3),
    ('empörung', 'templar', 3), ('entkräftender schwerer hieb', 'templar', 3), ('fangender schlag', 'templar', 3),
    ('festnahme', 'templar', 3), ('flug: phys. verteidigung erhöhen', 'templar', 3),
    ('flug: segen des wächter-generals', 'templar', 3), ('gedankenzerstörung', 'templar', 3),
    ('gefangenschaft', 'templar', 3), ('große gesundheit', 'templar', 3), ('heiliger schild', 'templar', 3),
    ('illusionsketten', 'templar', 3), ('kameradenschutz', 'templar', 3), ('kraft brechen', 'templar', 3),
    ('magischer schmetterschlag', 'templar', 3), ('nezekans schild', 'templar', 3),
    ('rüstung des empyrian. gebieters', 'templar', 3), ('rüstung des schutzes', 'templar', 3),
    ('schild der geschwindigkeit', 'templar', 3), ('schild des mutes', 'templar', 3),
    ('schildansturm', 'templar', 3), ('schildkonter', 'templar', 3), ('schildschlag', 'templar', 3),
    ('schildsprengung', 'templar', 3), ('schildstoß', 'templar', 3), ('schlag der beseitigung', 'templar', 3),
    ('schlag der züchtigung', 'templar', 3), ('schlag des inquisitors', 'templar', 3),
    ('schutzpanzer', 'templar', 3), ('schwertwind', 'templar', 3), ('siegel des schutzes', 'templar', 3),
    ('spottendes brüllen', 'templar', 3), ('stabiler schild', 'templar', 3), ('stahlbarrikade', 'templar', 3),
    ('tp erhöhen', 'templar', 3), ('wiederholter hieb', 'templar', 3), ('wiederholter schildschlag', 'templar', 3),
    ('wut der zerstörung', 'templar', 3), ('wut hervorrufen', 'templar', 3),
    ('zornaufbaugeschwindigkeit erhöhen', 'templar', 3), ('zornesschlag', 'templar', 3),
    ('äther-rüstung', 'templar', 3),

    # Magier/Zauberer (50)
    ('abkühlung', 'sorcerer', 3), ('arkaner donnerschlag', 'sorcerer', 3), ('beschwörung: fels', 'sorcerer', 3),
    ('blinder sprung', 'sorcerer', 3), ('eisharpune', 'sorcerer', 3), ('elementar-schutzbereich', 'sorcerer', 3),
    ('feuer der magischen kraft', 'sorcerer', 3), ('feuerschuss', 'sorcerer', 3),
    ('flammenangriff', 'sorcerer', 3), ('flammenharpune', 'sorcerer', 3), ('flammenkäfig', 'sorcerer', 3),
    ('flammenschmelze', 'sorcerer', 3), ('flammenwalze', 'sorcerer', 3), ('fluch der schwäche', 'sorcerer', 3),
    ('fluch: baum', 'sorcerer', 3), ('flug: magischen angriff erhöhen', 'sorcerer', 3),
    ('flug: segen des zauberergottes', 'sorcerer', 3), ('frost', 'sorcerer', 3), ('frostsäule', 'sorcerer', 3),
    ('funkelnde scherbe', 'sorcerer', 3), ('gabe der eisengewandung', 'sorcerer', 3),
    ('gabe der flinkheit', 'sorcerer', 3), ('gefrierschock', 'sorcerer', 3), ('gletscherscherbe', 'sorcerer', 3),
    ('glut', 'sorcerer', 3), ('großer vulkanausbruch', 'sorcerer', 3), ('illusion des winters', 'sorcerer', 3),
    ('illusionssturm', 'sorcerer', 3), ('illusionstor', 'sorcerer', 3), ('kalte luft beschwören', 'sorcerer', 3),
    ('lebenskraft tauschen', 'sorcerer', 3), ('lumiels zorn', 'sorcerer', 3), ('magieexplosion', 'sorcerer', 3),
    ('magischen angriff erhöhen', 'sorcerer', 3), ('maximale mp erhöhen', 'sorcerer', 3),
    ('robe der flamme', 'sorcerer', 3), ('schlaf: vogelscheuche', 'sorcerer', 3), ('schlafsturm', 'sorcerer', 3),
    ('schlafwolke', 'sorcerer', 3), ('schlag des sturms', 'sorcerer', 3), ('schneidender wind', 'sorcerer', 3),
    ('seelenabsorption', 'sorcerer', 3), ('seelenfrost', 'sorcerer', 3), ('speer des windes', 'sorcerer', 3),
    ('vaizels weisheit', 'sorcerer', 3), ('verfluchter alter baum', 'sorcerer', 3),
    ('winterbindung', 'sorcerer', 3), ('winterrüstung', 'sorcerer', 3), ('wirbelwind beschwören', 'sorcerer', 3),
    ('äthergriff', 'sorcerer', 3),

    # Geisterbeschwoerer (62)
    ('abzeichen des versteckens', 'spiritmaster', 3), ('albtraumfessel', 'spiritmaster', 3),
    ('befehl: explosionsklaue', 'spiritmaster', 3), ('befehl: groll', 'spiritmaster', 3),
    ('befehl: mauer des schutzes', 'spiritmaster', 3), ('befehl: ruinöser angriff', 'spiritmaster', 3),
    ('befehl: schutz', 'spiritmaster', 3), ('befehl: schützender geist', 'spiritmaster', 3),
    ('befehl: sturm der elemente', 'spiritmaster', 3), ('befehl: störung', 'spiritmaster', 3),
    ('befehl: zu asche verbrennen', 'spiritmaster', 3), ('beschwörung: erdgeist', 'spiritmaster', 3),
    ('beschwörung: feuergeist', 'spiritmaster', 3), ('beschwörung: gruppenmitglied', 'spiritmaster', 3),
    ('beschwörung: magmageist', 'spiritmaster', 3), ('beschwörung: sturmgeist', 'spiritmaster', 3),
    ('beschwörung: wassergeist', 'spiritmaster', 3), ('beschwörung: winddiener', 'spiritmaster', 3),
    ('beschwörung: windgeist', 'spiritmaster', 3), ('beschwörung: zyklon-diener', 'spiritmaster', 3),
    ('elementarauffrischung', 'spiritmaster', 3), ('elementarschlag', 'spiritmaster', 3),
    ('entkräftendes festhalten', 'spiritmaster', 3), ('entzauberung', 'spiritmaster', 3),
    ('erstickungssog', 'spiritmaster', 3), ('flammen der pein', 'spiritmaster', 3),
    ('fluch der magischen kraft', 'spiritmaster', 3), ('fluch: feuergeist', 'spiritmaster', 3),
    ('fluch: wassergeist', 'spiritmaster', 3), ('fluchwolke', 'spiritmaster', 3),
    ('flug: mp erhöhen', 'spiritmaster', 3), ('flug: segen des geistgottes', 'spiritmaster', 3),
    ('furcht', 'spiritmaster', 3), ('furcht: ginseng', 'spiritmaster', 3), ('geist schwächen', 'spiritmaster', 3),
    ('geistheilung', 'spiritmaster', 3), ('grausige düsternis', 'spiritmaster', 3),
    ('höllenfäulnis', 'spiritmaster', 3), ('höllenqualen', 'spiritmaster', 3),
    ('kette der erde', 'spiritmaster', 3), ('konzentration', 'spiritmaster', 3),
    ('magie auflösen', 'spiritmaster', 3), ('magieblock', 'spiritmaster', 3),
    ('magieverbrennung', 'spiritmaster', 3), ('magische umkehr', 'spiritmaster', 3),
    ('mana-explosion', 'spiritmaster', 3), ('mudra der unterwerfung', 'spiritmaster', 3),
    ('rauchgasexplosion', 'spiritmaster', 3), ('rüstung des geistes', 'spiritmaster', 3),
    ('schutz der erde', 'spiritmaster', 3), ('schwächende magie verstärken', 'spiritmaster', 3),
    ('seelenflut', 'spiritmaster', 3), ('seelisches mitgefühl', 'spiritmaster', 3),
    ('steinschock', 'spiritmaster', 3), ('stigma der macht', 'spiritmaster', 3),
    ('stärkender geist: rüstung des elementes', 'spiritmaster', 3),
    ('stärkender geist: verzauberte rüstung', 'spiritmaster', 3), ('umfangreiche erosion', 'spiritmaster', 3),
    ('vertrag der resistenz', 'spiritmaster', 3), ('zorn der wildnis', 'spiritmaster', 3),
    ('zorntausch', 'spiritmaster', 3), ('zyklon des zorns', 'spiritmaster', 3),
]

# Datenquelle: derselbe skill_tree.xml/skill_templates.xml-Join wie oben, diesmal gegen die
# englischen Skillnamen aus dem BASIS-Retail-Client (L10N\eng\data\data.pak - dieser Server
# pflegt fuer Englisch keine eigene client_strings_skill.xml, nur ~40 STR_SKILL_-Eintraege in
# seiner 2_eng-Override-Datei, also kommt die volle Liste aus dem unveraenderten Basis-Paket).
# Gleiche automatisierte Filterung: Rang-Konsistenz ueber alle roemischen Ziffern-Suffixe hinweg
# und Cross-Klassen-Teilstring-Kollisionspruefung.
CLASS_SKILL_HINTS_EN = [
    # Ranger (52)
    ('aero snare', 'ranger', 3), ('aether arrow', 'ranger', 3), ('agonizing arrow', 'ranger', 3),
    ('arrow deluge', 'ranger', 3), ('dizzying arrow', 'ranger', 3),
    ('arrow of virago', 'ranger', 3), ('arrow strike', 'ranger', 3), ('bestial fury', 'ranger', 3),
    ('blazing trap', 'ranger', 3), ('boost accuracy', 'ranger', 3), ('boost bow range', 'ranger', 3),
    ('boost parry', 'ranger', 3), ('bow of blessing', 'ranger', 3), ('breath of nature', 'ranger', 3),
    ('deadshot', 'ranger', 3), ('dilation arrow', 'ranger', 3), ('dodging', 'ranger', 3),
    ('entangling shot', 'ranger', 3), ('explosive arrow', 'ranger', 3), ('feint', 'ranger', 3),
    ('finishing arrow', 'ranger', 3), ('focused shots', 'ranger', 3), ('gale arrow', 'ranger', 3),
    ('heart shot', 'ranger', 3), ('holy arrow', 'ranger', 3), ("hunter's might", 'ranger', 3),
    ('lethal arrow', 'ranger', 3), ('lightning arrow', 'ranger', 3), ('mau form', 'ranger', 3),
    ("nature's resolve", 'ranger', 3), ('nimble fingers', 'ranger', 3), ('retreating slash', 'ranger', 3),
    ('rupture arrow', 'ranger', 3), ('sandstorm trap', 'ranger', 3), ('seizure arrow', 'ranger', 3),
    ('shackle arrow', 'ranger', 3), ('sharpen arrows', 'ranger', 3), ('shock trap', 'ranger', 3),
    ('silence arrow', 'ranger', 3), ('skybound trap', 'ranger', 3), ('sleep arrow', 'ranger', 3),
    ('sleep trap', 'ranger', 3), ('speed of the wind', 'ranger', 3), ('spiral arrow', 'ranger', 3),
    ('strong shots', 'ranger', 3), ('stunning shot', 'ranger', 3), ('swift shot', 'ranger', 3),
    ('trap of clairvoyance', 'ranger', 3), ('trap of slowing', 'ranger', 3),
    ('unerring arrow', 'ranger', 3), ('winged avenger', 'ranger', 3), ('winged range', 'ranger', 3),

    # Cleric (53)
    ('acquittal', 'cleric', 3), ('amplification', 'cleric', 3), ('benevolence', 'cleric', 3),
    ('light of rejuvenation', 'cleric', 3), ('roiling hack', 'cleric', 3), ("yustiel's light", 'cleric', 3),
    ('blessed shield', 'cleric', 3), ('blinding light', 'cleric', 3), ('boost healing', 'cleric', 3),
    ('call lightning', 'cleric', 3), ('chain of suffering', 'cleric', 3), ('chastise', 'cleric', 3),
    ('cleanse', 'cleric', 3), ('divine spark', 'cleric', 3), ('divine touch', 'cleric', 3),
    ("earth's wrath", 'cleric', 3), ('enfeebling burst', 'cleric', 3), ('festering wound', 'cleric', 3),
    ('flash of recovery', 'cleric', 3), ('hallowed strike', 'cleric', 3),
    ('hand of reincarnation', 'cleric', 3), ('healing majesty', 'cleric', 3),
    ('immortal shroud', 'cleric', 3), ('impervious veil', 'cleric', 3), ("land's bargain", 'cleric', 3),
    ('light of recovery', 'cleric', 3), ('light of resurrection', 'cleric', 3),
    ('noble grace', 'cleric', 3), ("pandaemonium's protection", 'cleric', 3),
    ('power sprint', 'cleric', 3), ('prayer of focus', 'cleric', 3), ('punishing earth', 'cleric', 3),
    ('radiant cure', 'cleric', 3), ('reduce enmity increase rate', 'cleric', 3),
    ('resurrection loci', 'cleric', 3), ('retribution lightning', 'cleric', 3),
    ('reverse condition', 'cleric', 3), ('ripple of purification', 'cleric', 3),
    ('sacrificial power', 'cleric', 3), ("sage's wisdom", 'cleric', 3), ('saving grace', 'cleric', 3),
    ('slashing wind', 'cleric', 3), ('splendor of purification', 'cleric', 3),
    ('splendor of rebirth', 'cleric', 3), ('splendor of recovery', 'cleric', 3),
    ('storm of aion', 'cleric', 3), ('summon healing servant', 'cleric', 3),
    ('summon holy servant', 'cleric', 3), ('summon noble energy', 'cleric', 3),
    ('sympathetic heal', 'cleric', 3), ('winged blessing', 'cleric', 3), ('winged recovery', 'cleric', 3),
    ('word of destruction', 'cleric', 3),

    # Chanter (49)
    ('acceleration cheer', 'chanter', 3), ('annihilation', 'chanter', 3), ('backshock', 'chanter', 3),
    ('stamina absorption', 'chanter', 3),
    ('blessing of rock', 'chanter', 3), ('blessing of stone', 'chanter', 3),
    ('blessing of wind', 'chanter', 3), ('booming strike', 'chanter', 3),
    ('boost mantra range', 'chanter', 3), ('boost physical attack', 'chanter', 3),
    ('celerity mantra', 'chanter', 3), ('crashing strike', 'chanter', 3),
    ('disorienting blow', 'chanter', 3), ('elemental screen', 'chanter', 3),
    ('emergency teleport', 'chanter', 3), ('healing burst', 'chanter', 3),
    ('healing conduit', 'chanter', 3), ('heaving strike', 'chanter', 3),
    ('incandescent blow', 'chanter', 3), ('inescapable judgment', 'chanter', 3),
    ('invincibility mantra', 'chanter', 3), ('leaping flash', 'chanter', 3),
    ("marchutan's protection", 'chanter', 3), ('melee smash', 'chanter', 3),
    ('meteor strike', 'chanter', 3), ('mountain crash', 'chanter', 3), ('numbing blow', 'chanter', 3),
    ('parrying strike', 'chanter', 3), ('pentacle shock', 'chanter', 3), ('perfect parry', 'chanter', 3),
    ('promise of earth', 'chanter', 3), ('protective ward', 'chanter', 3),
    ('recovery spell', 'chanter', 3), ('resonance haze', 'chanter', 3), ('seismic crash', 'chanter', 3),
    ('shield mantra', 'chanter', 3), ('soul lock', 'chanter', 3), ('splash swing', 'chanter', 3),
    ('stamina discharge', 'chanter', 3), ('stamina restoration', 'chanter', 3),
    ('thunderbolt strike', 'chanter', 3), ('unstoppable', 'chanter', 3), ('winged catalyst', 'chanter', 3),
    ('winged mantra', 'chanter', 3), ('word of inspiration', 'chanter', 3), ('word of life', 'chanter', 3),
    ('word of protection', 'chanter', 3), ('word of quickness', 'chanter', 3),
    ('word of wind', 'chanter', 3),

    # Gladiator (50)
    ('absorbing fury', 'gladiator', 3), ('advanced polearm training', 'gladiator', 3),
    ('aerial lockdown', 'gladiator', 3), ('ankle snare', 'gladiator', 3), ('shining slash', 'gladiator', 3),
    ('armor of attrition', 'gladiator', 3), ('berserking', 'gladiator', 3), ('body combo', 'gladiator', 3),
    ('body slice', 'gladiator', 3), ('boost knockdown', 'gladiator', 3), ('cleave', 'gladiator', 3),
    ('counter leech', 'gladiator', 3), ('crippling cut', 'gladiator', 3),
    ('crushing blow', 'gladiator', 3), ('dauntless spirit', 'gladiator', 3),
    ('defense preparation', 'gladiator', 3), ('determination', 'gladiator', 3),
    ('draining blow', 'gladiator', 3), ('draining sword', 'gladiator', 3),
    ('earthquake wave', 'gladiator', 3), ('energy impact', 'gladiator', 3),
    ('exhausting wave', 'gladiator', 3), ('explosion of rage', 'gladiator', 3),
    ('ferocious chop', 'gladiator', 3), ('final strike', 'gladiator', 3), ('great cleave', 'gladiator', 3),
    ('lockdown', 'gladiator', 3), ('magical defense', 'gladiator', 3),
    ('piercing rupture', 'gladiator', 3), ('pressure wave', 'gladiator', 3),
    ('revival wave', 'gladiator', 3), ('righteous cleave', 'gladiator', 3),
    ('second wind', 'gladiator', 3), ('seismic billow', 'gladiator', 3),
    ('severe precision cut', 'gladiator', 3), ('severe weakening blow', 'gladiator', 3),
    ('sharp strike', 'gladiator', 3), ('slaughter', 'gladiator', 3), ('spite strike', 'gladiator', 3),
    ('springing slice', 'gladiator', 3), ('strengthen wings', 'gladiator', 3),
    ('sure strike', 'gladiator', 3), ('tendon slice', 'gladiator', 3), ('wall of steel', 'gladiator', 3),
    ('whirling strike', 'gladiator', 3), ('winged rage', 'gladiator', 3),
    ('winged strength', 'gladiator', 3), ('wrathful explosion', 'gladiator', 3),
    ('wrathful strike', 'gladiator', 3), ('wrathful wave', 'gladiator', 3),

    # Assassin (54)
    ('aethertwisting', 'assassin', 3), ('agony rune', 'assassin', 3), ('ambush', 'assassin', 3),
    ('apply deadly poison', 'assassin', 3), ('apply lethal venom', 'assassin', 3),
    ('assassination', 'assassin', 3), ('beast kick', 'assassin', 3), ('beast leap', 'assassin', 3),
    ('beast swipe', 'assassin', 3), ('binding rune', 'assassin', 3), ('blinding burst', 'assassin', 3),
    ('boost crit strike chance', 'assassin', 3), ('boost magical resistance', 'assassin', 3),
    ('break away', 'assassin', 3), ('cross slash', 'assassin', 3), ('dash and slash', 'assassin', 3),
    ('dash attack', 'assassin', 3), ('deadly abandon', 'assassin', 3), ('deadly focus', 'assassin', 3),
    ('encircling strike', 'assassin', 3), ('evasion rate increase', 'assassin', 3),
    ('eye of wrath', 'assassin', 3), ('fang strike', 'assassin', 3), ('flash of speed', 'assassin', 3),
    ('flurry', 'assassin', 3), ('killing spree', 'assassin', 3), ('lightning slash', 'assassin', 3),
    ('massacre', 'assassin', 3), ('oath of accuracy', 'assassin', 3), ('pain rune', 'assassin', 3),
    ('quickening doom', 'assassin', 3), ('ripclaw strike', 'assassin', 3), ('rune carve', 'assassin', 3),
    ('rune knife', 'assassin', 3), ('rune slash', 'assassin', 3), ('searching strike', 'assassin', 3),
    ('sensory boost', 'assassin', 3), ('shadow illusion', 'assassin', 3), ('shadow walk', 'assassin', 3),
    ('shadowfall', 'assassin', 3), ('side strike', 'assassin', 3), ('sigil strike', 'assassin', 3),
    ('signet silence', 'assassin', 3), ('slayer form', 'assassin', 3), ('soul slash', 'assassin', 3),
    ('spiral slash', 'assassin', 3), ('sprinting', 'assassin', 3), ('surprise attack', 'assassin', 3),
    ('swift edge', 'assassin', 3), ('venomous strike', 'assassin', 3), ('whirlwind slash', 'assassin', 3),
    ('wind walk', 'assassin', 3), ('winged death', 'assassin', 3), ('winged fury', 'assassin', 3),

    # Templar (47)
    ('aether armor', 'templar', 3), ('aggravation', 'templar', 3), ('avenging blow', 'templar', 3),
    ('barricade of steel', 'templar', 3), ('blood pact', 'templar', 3), ('boost block', 'templar', 3),
    ('boost hp', 'templar', 3), ('break power', 'templar', 3), ('courageous shield', 'templar', 3),
    ('dazing severe blow', 'templar', 3), ('divine blow', 'templar', 3), ('divine fury', 'templar', 3),
    ('divine grasp', 'templar', 3), ('divine justice', 'templar', 3), ('doom lure', 'templar', 3),
    ('empyrean armor', 'templar', 3), ('empyrean fury', 'templar', 3),
    ('empyrean providence', 'templar', 3), ('ensnaring blow', 'templar', 3), ('face smash', 'templar', 3),
    ('holy shield', 'templar', 3), ('illusion chains', 'templar', 3), ('incite rage', 'templar', 3),
    ("inquisitor's blow", 'templar', 3), ('iron skin', 'templar', 3), ('magic smash', 'templar', 3),
    ("nezekan's shield", 'templar', 3), ('panoply of protection', 'templar', 3),
    ('pitiless blow', 'templar', 3), ('prayer of freedom', 'templar', 3),
    ('prayer of resilience', 'templar', 3), ('prayer of victory', 'templar', 3),
    ('provoking roar', 'templar', 3), ('punishing thrust', 'templar', 3), ('punishing wave', 'templar', 3),
    ('punishment', 'templar', 3), ('seal of protection', 'templar', 3), ('shield bash', 'templar', 3),
    ('shield counter', 'templar', 3), ('shield of faith', 'templar', 3), ('shield shock', 'templar', 3),
    ('shieldburst', 'templar', 3), ('swinging shield counter', 'templar', 3),
    ('sword storm', 'templar', 3), ('winged defense', 'templar', 3), ('winged guardian', 'templar', 3),
    ('wrath strike', 'templar', 3),

    # Sorcerer (50)
    ('absolute zero', 'sorcerer', 3), ('aether flame', 'sorcerer', 3), ("aether's hold", 'sorcerer', 3),
    ('arcane thunderbolt', 'sorcerer', 3), ('balaur seeker', 'sorcerer', 3),
    ('big magma eruption', 'sorcerer', 3), ('blind leap', 'sorcerer', 3),
    ('boon of iron-clad', 'sorcerer', 3), ('boon of quickness', 'sorcerer', 3),
    ('boost magical attack', 'sorcerer', 3), ('boost maximum mp', 'sorcerer', 3),
    ('conflagration', 'sorcerer', 3), ('curse of old roots', 'sorcerer', 3),
    ('curse of roots', 'sorcerer', 3), ('curse of weakness', 'sorcerer', 3),
    ('elemental ward', 'sorcerer', 3), ('exchange vitality', 'sorcerer', 3), ('flame cage', 'sorcerer', 3),
    ('flame fusion', 'sorcerer', 3), ('flame harpoon', 'sorcerer', 3), ('flame spray', 'sorcerer', 3),
    ('flaming meteor', 'sorcerer', 3), ('freezing wind', 'sorcerer', 3), ('frost', 'sorcerer', 3),
    ('frozen shock', 'sorcerer', 3), ('glacial shard', 'sorcerer', 3), ('graspbreaker', 'sorcerer', 3),
    ('ice harpoon', 'sorcerer', 3), ('ice sheet', 'sorcerer', 3), ('illusion gate', 'sorcerer', 3),
    ('illusion storm', 'sorcerer', 3), ("lumiel's wrath", 'sorcerer', 3), ('magic assist', 'sorcerer', 3),
    ('refracting shard', 'sorcerer', 3), ('robe of flame', 'sorcerer', 3),
    ('sleep: scarecrow', 'sorcerer', 3), ('sleeping storm', 'sorcerer', 3),
    ('soul absorption', 'sorcerer', 3), ('soul freeze', 'sorcerer', 3), ('storm strike', 'sorcerer', 3),
    ('summon rock', 'sorcerer', 3), ('summon whirlwind', 'sorcerer', 3),
    ('tranquilizing cloud', 'sorcerer', 3), ("vaizel's wisdom", 'sorcerer', 3),
    ('wind cut down', 'sorcerer', 3), ('wind spear', 'sorcerer', 3), ('winged magic', 'sorcerer', 3),
    ('winged sage', 'sorcerer', 3), ('winter binding', 'sorcerer', 3), ('wintry armor', 'sorcerer', 3),

    # Spiritmaster (60)
    ('aegis breaker', 'spiritmaster', 3), ('armor spirit', 'spiritmaster', 3),
    ('backdraft', 'spiritmaster', 3), ('chain of earth', 'spiritmaster', 3),
    ('cloaking word', 'spiritmaster', 3), ('command: bodyguard', 'spiritmaster', 3),
    ('command: kamikaze', 'spiritmaster', 3), ('concentration', 'spiritmaster', 3),
    ('contract of resistance', 'spiritmaster', 3), ('curse of fire', 'spiritmaster', 3),
    ('curse of water', 'spiritmaster', 3), ('cursecloud', 'spiritmaster', 3),
    ('cyclone of wrath', 'spiritmaster', 3), ('disenchant', 'spiritmaster', 3),
    ('dispel magic', 'spiritmaster', 3), ('earthen call', 'spiritmaster', 3),
    ('elemental spirit armor', 'spiritmaster', 3), ('enhance weakening magic', 'spiritmaster', 3),
    ('enmity swap', 'spiritmaster', 3), ('fear: ginseng', 'spiritmaster', 3),
    ('flames of anguish', 'spiritmaster', 3), ('healing spirit', 'spiritmaster', 3),
    ('ignite aether', 'spiritmaster', 3), ('infernal blight', 'spiritmaster', 3),
    ('infernal pain', 'spiritmaster', 3), ('magic implosion', 'spiritmaster', 3),
    ("magic's freedom", 'spiritmaster', 3), ('nightmare', 'spiritmaster', 3),
    ('replenish element', 'spiritmaster', 3), ('ritual push', 'spiritmaster', 3),
    ('root of enervation', 'spiritmaster', 3), ('sandblaster', 'spiritmaster', 3),
    ('shackle of vulnerability', 'spiritmaster', 3), ('sigil of silence', 'spiritmaster', 3),
    ('soul torrent', 'spiritmaster', 3), ('spirit burn-to-ashes', 'spiritmaster', 3),
    ('spirit detonation claw', 'spiritmaster', 3), ('spirit disturbance', 'spiritmaster', 3),
    ('spirit pique', 'spiritmaster', 3), ('spirit preserve', 'spiritmaster', 3),
    ('spirit ruinous offensive', 'spiritmaster', 3), ('spirit wall of protection', 'spiritmaster', 3),
    ('stone scour', 'spiritmaster', 3), ('stone shock', 'spiritmaster', 3),
    ('summon cyclone servant', 'spiritmaster', 3), ('summon earth spirit', 'spiritmaster', 3),
    ('summon fire spirit', 'spiritmaster', 3), ('summon group member', 'spiritmaster', 3),
    ('summon magma spirit', 'spiritmaster', 3), ('summon tempest spirit', 'spiritmaster', 3),
    ('summon water spirit', 'spiritmaster', 3), ('summon wind servant', 'spiritmaster', 3),
    ('summon wind spirit', 'spiritmaster', 3), ('summoning alacrity', 'spiritmaster', 3),
    ('sympathetic mind', 'spiritmaster', 3), ('vacuum choke', 'spiritmaster', 3),
    ('weaken spirit', 'spiritmaster', 3), ('winged devotion', 'spiritmaster', 3),
    ('winged spirit', 'spiritmaster', 3), ('withering gloom', 'spiritmaster', 3),
]

CLASS_SKILL_HINTS_BY_LANG = {'de': CLASS_SKILL_HINTS_DE, 'en': CLASS_SKILL_HINTS_EN}


class ClassGuesser:
    """Weighted keyword votes across every skill name a player has used this session."""

    def __init__(self):
        self.votes = {}

    def observe(self, name, skill):
        if not skill:
            return
        low = skill.lower()
        hints = CLASS_SKILL_HINTS_BY_LANG.get(current_lang(), CLASS_SKILL_HINTS_DE)
        for keyword, code, weight in hints:
            if keyword in low:
                self.votes.setdefault(name, Counter())[code] += weight

    def guess(self, name):
        counter = self.votes.get(name)
        if not counter:
            return None
        return counter.most_common(1)[0][0]


def guess_class_from_skill(skill):
    """One-off class guess from a single skill name, independent of any accumulated player
    history - used by the dual-account self-split (see EncounterManager._resolve_self_identity),
    not by the normal per-player ClassGuesser above."""
    if not skill:
        return None
    low = skill.lower()
    hints = CLASS_SKILL_HINTS_BY_LANG.get(current_lang(), CLASS_SKILL_HINTS_DE)
    votes = Counter()
    for keyword, code, weight in hints:
        if keyword in low:
            votes[code] += weight
    if not votes:
        return None
    return votes.most_common(1)[0][0]


def is_self_key(name):
    """True for the log owner's own identity - either the plain 'Du', or, in dual-account mode,
    one of the per-detected-class split identities like 'Du (Templer)'."""
    return name == 'Du' or name.startswith('Du (')


# Loot lines only ever carry a raw, unresolved item template ID (see LOOT_*_RE_DE/EN) - the name
# has to be resolved separately. Two tiers:
#  - CONFIRMED_ITEM_NAMES: hand-verified against this server's own client strings, one entry at a
#    time as the user reports them (started from the Elim-Talisman II report). Always correct.
#  - item_names.json (bundled resource): a ~78k-entry id->name table built the same way as the
#    skill database (AionGermany/Aion-Lightning item_templates.xml `descr` codename joined against
#    this client's own item string tables) - reliable for the vast majority of items, EXCEPT where
#    OriginAion has repurposed a stock item ID for custom content, as confirmed for ID 186000051
#    itself (stock: "Maechtige uralte Krone" / actually: "Glaenzender glorreicher Elim-Talisman
#    II"). Shown but visibly marked as unverified, since it can be confidently wrong.
CONFIRMED_ITEM_NAMES = {
    186000051: 'Glänzender glorreicher Elim-Talisman II',
}


def _load_bulk_item_names():
    try:
        with open(resource_path('item_names.json'), 'r', encoding='utf-8') as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return {}


BULK_ITEM_NAMES = _load_bulk_item_names()


def resolve_item_name(item_id):
    """Returns (display_name, source) where source is 'confirmed', 'bulk' (best-effort, may be
    wrong for a server-repurposed ID), or 'id' (no name known at all - just the raw number)."""
    if item_id in CONFIRMED_ITEM_NAMES:
        return CONFIRMED_ITEM_NAMES[item_id], 'confirmed'
    if item_id in BULK_ITEM_NAMES:
        return BULK_ITEM_NAMES[item_id], 'bulk'
    return f'Item #{item_id}', 'id'


# Item quality/rarity, driving the color an item's name is rendered in on the Loottable. Same
# source/pipeline as the name table above (item_quality.json, ~91.7k entries), but simpler to
# build - `quality` is a plain enum attribute directly on each item_templates.xml <item_template>,
# no client string-table join needed since it isn't localized text.
# The emulator source itself defines 7 tiers (JUNK/COMMON/RARE/LEGEND/UNIQUE/EPIC/MYTHIC), but the
# user only named 5 distinct in-game colors from actually playing OriginAion (weiss/gruen/blau/
# gold/mythisch) - JUNK is folded into COMMON's color and EPIC into UNIQUE's, both unconfirmed
# guesses since neither tier was named. The exact hex shades are also unverified - unlike item
# names, colors can't be cross-checked against the client's own text data (see
# aion_item_loot_database memory), so this is a best-effort default. If the user reports a wrong
# color for a specific item, pin it in CONFIRMED_ITEM_QUALITY below - same pattern as
# CONFIRMED_ITEM_NAMES.
CONFIRMED_ITEM_QUALITY = {}

QUALITY_COLORS = {
    'JUNK': '#e8e8e8',
    'COMMON': '#e8e8e8',
    'RARE': '#2ecc71',
    'LEGEND': '#3b9eff',
    'UNIQUE': '#ffc94d',
    'EPIC': '#ffc94d',
    'MYTHIC': '#ff4d6d',
}
DEFAULT_ITEM_COLOR = QUALITY_COLORS['COMMON']


def _load_bulk_item_quality():
    try:
        with open(resource_path('item_quality.json'), 'r', encoding='utf-8') as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return {}


BULK_ITEM_QUALITY = _load_bulk_item_quality()


def resolve_item_color(item_id):
    """Hex color to render an item's name in, based on its rarity tier. Falls back to the common/
    white color for anything not in the bulk table (e.g. a custom OriginAion-only item ID)."""
    quality = CONFIRMED_ITEM_QUALITY.get(item_id) or BULK_ITEM_QUALITY.get(item_id)
    return QUALITY_COLORS.get(quality, DEFAULT_ITEM_COLOR)


# --- Live item-info popup (Loottable -> click an item) --------------------------------------
# aioncodex.com's individual item pages (unlike its skill tables, see aion_wiki_access memory) are
# plain server-rendered HTML - confirmed by fetching one directly and cross-checking its stats
# against item_templates.xml for the same ID (exact match: weapon damage, accuracy, parry, etc.),
# meaning it's built from the same/an equivalent Aion-Lightning-family source. That also means the
# SAME stock-vs-repurposed-ID caveat as BULK_ITEM_NAMES applies here - confirmed directly: item
# 186000051 shows as "Major Ancient Crown" on Aion Codex, not OriginAion's actual "Elim-Talisman
# II". Shown to the user as an explicit disclaimer in the popup itself (see aion_wiki_item_popup
# memory), same as the '*' marker on bulk item names.
AION_CODEX_ITEM_URL = 'https://aioncodex.com/us/item/{id}/'
ITEM_INFO_NAME_RE = re.compile(
    r'<span class="item_title item_grade_\d+" id="item_name"><b>(.*?)</b></span>')
ITEM_INFO_ICON_RE = re.compile(r'<img src="(/items/[^"]+)" class="item_icon')
ITEM_INFO_STATS_RE = re.compile(r'<div class="stat_name">(.*?)</div><div class="stat_value">(.*?)</div>')
ITEM_INFO_TITLES_RE = re.compile(r'class="titles_cell">(.*?)</td>', re.DOTALL)
ITEM_INFO_PRICE_RE = re.compile(r'Buy price: ([\d,]+).*?Sell price: ([\d,]+)', re.DOTALL)


def _strip_html_to_lines(fragment):
    text = re.sub(r'<br\s*/?>', '\n', fragment)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    return [ln.strip() for ln in text.split('\n') if ln.strip()]


def fetch_item_info(item_id):
    """Live-fetches and parses one item's Aion Codex page. Runs on a background thread (see
    ItemInfoPopup) - never raises, returns None on any failure (offline, timeout, site down, page
    shape changed) so a failed lookup just shows an error state instead of crashing the popup."""
    url = AION_CODEX_ITEM_URL.format(id=item_id)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AionDPSMeter/' + APP_VERSION})
        with urllib.request.urlopen(req, timeout=8) as resp:
            page = resp.read().decode('utf-8', errors='replace')
    except Exception:
        return None

    m = ITEM_INFO_NAME_RE.search(page)
    if not m:
        return None
    name = html.unescape(m.group(1)).strip()

    stats = [(html.unescape(k).strip(), html.unescape(v).strip())
             for k, v in ITEM_INFO_STATS_RE.findall(page)]

    titles_m = ITEM_INFO_TITLES_RE.search(page)
    detail_lines = _strip_html_to_lines(titles_m.group(1)) if titles_m else []

    price_m = ITEM_INFO_PRICE_RE.search(page)
    price = (price_m.group(1), price_m.group(2)) if price_m else None

    icon_bytes = None
    icon_m = ITEM_INFO_ICON_RE.search(page)
    if icon_m:
        try:
            icon_req = urllib.request.Request('https://aioncodex.com' + icon_m.group(1),
                                                headers={'User-Agent': 'AionDPSMeter/' + APP_VERSION})
            with urllib.request.urlopen(icon_req, timeout=8) as resp:
                icon_bytes = resp.read()
        except Exception:
            pass

    return {'name': name, 'stats': stats, 'detail_lines': detail_lines, 'price': price,
            'icon_bytes': icon_bytes, 'url': url}


# --- Deutsch --------------------------------------------------------------
CRIT_PREFIX_DE = "Kritischer Treffer!"

DAMAGE_SKILL_RE_DE = re.compile(
    r'^(?P<attacker>.+?) (?:hat|habt) (?P<target>.+?) durch (?:Benutzung von )?'
    r'(?P<skill>.+?) (?P<amount>[0-9]+(?:\.[0-9]{3})*) (?:kritischen )?Schaden zugef\u00fcgt\.'
)
DAMAGE_PLAIN_RE_DE = re.compile(
    r'^(?P<attacker>.+?) (?:hat|habt) (?P<target>.+?) '
    r'(?P<amount>[0-9]+(?:\.[0-9]{3})*) (?:kritischen )?Schaden zugef\u00fcgt\.'
)
HEAL_OTHER_RE_DE = re.compile(
    r'^(?P<target>.+?) (?:hat|habt) (?P<amount>[0-9]+(?:\.[0-9]{3})*) TP wiederhergestellt, '
    r'(?:da|weil) (?P<healer>.+?) (?P<skill>.+?) eingesetzt hat\.'
)
HEAL_SELF_RE_DE = re.compile(
    r'^(?P<name>.+?) (?:hat|habt)(?: durch (?P<skill>.+?))? '
    r'(?P<amount>[0-9]+(?:\.[0-9]{3})*) TP wiederhergestellt\.'
)
# Pet/servant summon announcements (e.g. Spiritmaster elemental spirits, Cleric's "Diener") -
# used to learn which player owns a pet, since the pet's own attacker name in damage lines is a
# fixed generic name (e.g. "Erdgeist", "Heiliger Diener"), never the owner's name. Someone else's
# summon: "[Caster] hat [Pet] durch [Skill] herbeigerufen." My own: "[Pet] durch [Skill]
# herbeigerufen." (no subject at all - verified directly from client_strings_msg.xml, not a typo).
SUMMON_OTHER_RE_DE = re.compile(r'^(?P<owner>.+?) hat (?P<pet>.+?) durch .+? herbeigerufen\.')
SUMMON_SELF_RE_DE = re.compile(r'^(?:Ihr habt )?(?P<pet>.+?) durch .+? herbeigerufen\.')
# Session-boundary triggers (verified client strings: STR_PARTY_ENTERED_PARTY, STR_USE_ITEM). The
# teleport check below matches on 'rückkehr' specifically, not the broader 'schriftrolle' (=
# "scroll") - checked the item database: ~430 items have "Schriftrolle" in the name and most of
# them are combat/crafting buff scrolls (crit chance, evasion, precision, etc.) that an active
# player pops every pull, which is exactly what caused a false reset "after every mob". The 57
# actual city/fortress return items are consistently named "Rückkehr-Schriftrolle nach/zur X" or
# "Rückkehr-Kugel zum X" - "rückkehr" alone is unique to those.
PARTY_JOIN_RE_DE = re.compile(r'^Ihr seid der Gruppe beigetreten\.')
ITEM_USE_RE_DE = re.compile(r'^Ihr habt "(?P<item>[^"]+)" benutzt\.')
# Abyss Point gain (STR_MSG_COMBAT_MY_ABYSS_POINT_GAIN, verified in client_strings_msg.xml: "Ihr
# habt %num0 Abyss-Punkte erhalten.") - self only, there is no "PlayerX hat Y Abyss-Punkte
# erhalten" equivalent anywhere in the client strings (checked deliberately, since loot has
# exactly that self/other split) - so this can only ever track the log owner's own AP, never other
# party members'. See aion_ap_tracking memory.
AP_GAIN_RE_DE = re.compile(r'^Ihr habt (?P<amount>[0-9]+(?:\.[0-9]{3})*) Abyss-Punkte erhalten\.')
# Loot (STR_GET_ITEM/STR_MSG_GET_ITEM_PARTYNOTICE both render as "X hat/habt Y erhalten." - but
# so do many unrelated messages (mail, EXP, Abyss Points, buff effects) that share the exact same
# sentence shape. The angle-bracket <Item> the user first saw is only the in-game *rendering* of
# an item link - the raw Chat.log line instead carries "[item:186000051;ver6;;;;]" (item template
# ID, unresolved), confirmed against the user's own real log line. Anchoring on "[item:" is what
# keeps this from flooding the loot table with "EP"/"Post"/buff-effect garbage; the numeric ID is
# resolved to a display name separately (see resolve_item_name), since names aren't in the log.
LOOT_SELF_RE_DE = re.compile(r'^Ihr habt (?:(?P<qty>\d+) )?\[item:\s*(?P<item_id>\d+)[^\]]*\] erhalten\.')
LOOT_OTHER_RE_DE = re.compile(
    r'^(?P<looter>.+?) hat (?:(?P<qty>\d+) )?\[item:\s*(?P<item_id>\d+)[^\]]*\] erhalten\.')


def normalize_name_de(name):
    return 'Du' if name in ('Ihr', 'ihr') else name


def parse_amount_de(s):
    return int(s.replace('.', ''))


def parse_line_de(rest, crit):
    m = DAMAGE_SKILL_RE_DE.match(rest)
    if m:
        target = m.group('target')
        if target == 'Euch':
            return None
        return {
            'type': 'damage', 'attacker': normalize_name_de(m.group('attacker')),
            'target': normalize_name_de(target), 'amount': parse_amount_de(m.group('amount')),
            'skill': m.group('skill'), 'crit': crit,
        }
    m = DAMAGE_PLAIN_RE_DE.match(rest)
    if m:
        target = m.group('target')
        if target == 'Euch':
            return None
        return {
            'type': 'damage', 'attacker': normalize_name_de(m.group('attacker')),
            'target': normalize_name_de(target), 'amount': parse_amount_de(m.group('amount')),
            'skill': 'Angriff', 'crit': crit,
        }
    m = HEAL_OTHER_RE_DE.match(rest)
    if m:
        return {
            'type': 'heal', 'healer': normalize_name_de(m.group('healer')),
            'target': normalize_name_de(m.group('target')), 'amount': parse_amount_de(m.group('amount')),
            'skill': m.group('skill'), 'crit': crit,
        }
    m = HEAL_SELF_RE_DE.match(rest)
    if m:
        name = normalize_name_de(m.group('name'))
        return {
            'type': 'heal', 'healer': name, 'target': name,
            'amount': parse_amount_de(m.group('amount')),
            'skill': m.group('skill') or 'Regeneration', 'crit': crit,
        }
    m = SUMMON_OTHER_RE_DE.match(rest)
    if m:
        return {'type': 'summon', 'owner': normalize_name_de(m.group('owner')), 'pet': m.group('pet')}
    m = SUMMON_SELF_RE_DE.match(rest)
    if m:
        return {'type': 'summon', 'owner': 'Du', 'pet': m.group('pet')}
    if PARTY_JOIN_RE_DE.match(rest):
        return {'type': 'session_break', 'reason': 'party'}
    m = ITEM_USE_RE_DE.match(rest)
    if m and 'rückkehr' in m.group('item').lower():
        return {'type': 'session_break', 'reason': 'teleport'}
    m = AP_GAIN_RE_DE.match(rest)
    if m:
        return {'type': 'ap_gain', 'amount': parse_amount_de(m.group('amount'))}
    m = LOOT_SELF_RE_DE.match(rest)
    if m:
        return {'type': 'loot', 'looter': 'Du', 'item_id': int(m.group('item_id')),
                'qty': int(m.group('qty')) if m.group('qty') else 1}
    m = LOOT_OTHER_RE_DE.match(rest)
    if m:
        return {'type': 'loot', 'looter': normalize_name_de(m.group('looter')),
                'item_id': int(m.group('item_id')),
                'qty': int(m.group('qty')) if m.group('qty') else 1}
    return None


# --- English ----------------------------------------------------------------
# Built from this client's own retail combat message templates (L10N\deu\Data\data.pak and
# L10N\eng\data\data.pak, strings/client_strings_msg.xml, joined by their language-independent
# STR_MSG_.../STR_SKILL_SUCC_..._TO_... string IDs). Unlike German - which phrases every combat
# line as "X hat Y Z Schaden zugefuegt" regardless of who's attacking whom - English uses TWO
# different sentence shapes depending on perspective ("X inflicted Z damage on Y" when a party
# member is the attacker, vs "Y received Z damage from X" when the target is on the player's
# side), and healing has several more synonymous phrasings ("recovered"/"restored", "by using"/
# "after using"/"due to the effect of"). The patterns below cover the shapes that matter for a
# DPS/heal meter (outgoing party damage and both directions of party healing); incoming-monster-
# damage lines ("received ... from") aren't tracked, same as German never tracks them either.
CRIT_PREFIX_EN = "Critical Hit!"

DAMAGE_SKILL_RE_EN = re.compile(
    r'^(?P<attacker>.+?) (?:has )?inflicted (?P<amount>[0-9]+(?:,[0-9]{3})*) (?:critical )?damage on '
    r'(?P<target>.+?) by using (?P<skill>.+?)\.'
)
DAMAGE_PLAIN_RE_EN = re.compile(
    r'^(?P<attacker>.+?) (?:has )?inflicted (?P<amount>[0-9]+(?:,[0-9]{3})*) (?:critical )?damage on '
    r'(?P<target>.+?)\.'
)
# Observer view: "X recovered N HP because Y used [Skill]." - the main cross-player heal case.
HEAL_OTHER_RE_EN = re.compile(
    r'^(?P<target>.+?) recovered (?P<amount>[0-9]+(?:,[0-9]{3})*) HP because '
    r'(?P<healer>.+?) used (?P<skill>.+?)\.'
)
# Own-view: "You restored N of X's HP by using [Skill]." - seen by the healer casting on someone else.
HEAL_MINE_RE_EN = re.compile(
    r"^You restored (?P<amount>[0-9]+(?:,[0-9]{3})*) of (?P<target>.+?)'s HP by using (?P<skill>.+?)\."
)
# Self-heal with a named skill: "X recovered N HP (by|after) using [Skill]."
HEAL_SELF_SKILL_RE_EN = re.compile(
    r'^(?P<name>.+?) recovered (?P<amount>[0-9]+(?:,[0-9]{3})*) HP\b.*?\busing (?P<skill>.+?)\.'
)
# Plain regen tick, no skill named: "X recovered/restored N HP."
HEAL_SELF_PLAIN_RE_EN = re.compile(
    r'^(?P<name>.+?) (?:recovered|restored) (?P<amount>[0-9]+(?:,[0-9]{3})*) HP\.'
)
# Pet/servant summon announcement - always has an explicit subject in English ("You"/"[Caster]"),
# so one pattern covers both my-own and someone-else's summon; normalize_name_en handles "You".
SUMMON_RE_EN = re.compile(r'^(?P<owner>.+?) summoned (?P<pet>.+?) by using .+?\.')
# Session-boundary triggers (verified client strings: STR_PARTY_ENTERED_PARTY, STR_USE_ITEM). No
# single keyword cleanly separates English return-teleport items from other scrolls the way German
# "rückkehr" does (checked: city scrolls are named inconsistently, e.g. "Sanctum Instant Scroll",
# "Kamar Scroll", "[Event] Quick-return Scroll" - no shared word across all of them). Matching
# "return" catches a good chunk of them without also matching ordinary combat/craft buff scrolls,
# but expect a real gap here versus the German match - not yet verified against a live EN session.
PARTY_JOIN_RE_EN = re.compile(r'^You have joined the group\.')
ITEM_USE_RE_EN = re.compile(r'^You have used (?P<item>.+?)\.$')
# Abyss Point gain - client string 1320000, "You have gained %num0 Abyss Points." (verified
# alongside the German "Ihr habt %num0 Abyss-Punkte erhalten." - both self only, see AP_GAIN_RE_DE).
AP_GAIN_RE_EN = re.compile(r'^You have gained (?P<amount>[0-9]+(?:,[0-9]{3})*) Abyss Points\.')
# Loot - "[item:186000051;ver6;;;;]" (unresolved item template ID) was confirmed from a real
# German raw log line; assumed to carry over unchanged to English since it's a raw data tag the
# client embeds, not translated text - the surrounding sentence differs by language but this tag
# syntax shouldn't. Still not verified against a real English sample - check here first if it
# doesn't show up.
LOOT_SELF_RE_EN = re.compile(r'^You have acquired (?:(?P<qty>\d+) )?\[item:\s*(?P<item_id>\d+)[^\]]*\]\.')
LOOT_OTHER_RE_EN = re.compile(
    r'^(?P<looter>.+?) has acquired (?:(?P<qty>\d+) )?\[item:\s*(?P<item_id>\d+)[^\]]*\]\.')


def normalize_name_en(name):
    return 'Du' if name.lower() in ('you', 'yourself') else name


def parse_amount_en(s):
    return int(s.replace(',', ''))


def parse_line_en(rest, crit):
    m = DAMAGE_SKILL_RE_EN.match(rest)
    if m:
        target = m.group('target')
        if target.lower() in ('you', 'yourself'):
            return None
        return {
            'type': 'damage', 'attacker': normalize_name_en(m.group('attacker')),
            'target': normalize_name_en(target), 'amount': parse_amount_en(m.group('amount')),
            'skill': m.group('skill'), 'crit': crit,
        }
    m = DAMAGE_PLAIN_RE_EN.match(rest)
    if m:
        target = m.group('target')
        if target.lower() in ('you', 'yourself'):
            return None
        return {
            'type': 'damage', 'attacker': normalize_name_en(m.group('attacker')),
            'target': normalize_name_en(target), 'amount': parse_amount_en(m.group('amount')),
            'skill': 'Attack', 'crit': crit,
        }
    m = HEAL_OTHER_RE_EN.match(rest)
    if m:
        return {
            'type': 'heal', 'healer': normalize_name_en(m.group('healer')),
            'target': normalize_name_en(m.group('target')), 'amount': parse_amount_en(m.group('amount')),
            'skill': m.group('skill'), 'crit': crit,
        }
    m = HEAL_MINE_RE_EN.match(rest)
    if m:
        return {
            'type': 'heal', 'healer': 'Du', 'target': normalize_name_en(m.group('target')),
            'amount': parse_amount_en(m.group('amount')), 'skill': m.group('skill'), 'crit': crit,
        }
    m = HEAL_SELF_SKILL_RE_EN.match(rest)
    if m:
        name = normalize_name_en(m.group('name'))
        return {
            'type': 'heal', 'healer': name, 'target': name,
            'amount': parse_amount_en(m.group('amount')), 'skill': m.group('skill'), 'crit': crit,
        }
    m = HEAL_SELF_PLAIN_RE_EN.match(rest)
    if m:
        name = normalize_name_en(m.group('name'))
        return {
            'type': 'heal', 'healer': name, 'target': name,
            'amount': parse_amount_en(m.group('amount')), 'skill': 'Regeneration', 'crit': crit,
        }
    m = SUMMON_RE_EN.match(rest)
    if m:
        return {'type': 'summon', 'owner': normalize_name_en(m.group('owner')), 'pet': m.group('pet')}
    if PARTY_JOIN_RE_EN.match(rest):
        return {'type': 'session_break', 'reason': 'party'}
    m = ITEM_USE_RE_EN.match(rest)
    if m and 'return' in m.group('item').lower():
        return {'type': 'session_break', 'reason': 'teleport'}
    m = AP_GAIN_RE_EN.match(rest)
    if m:
        return {'type': 'ap_gain', 'amount': parse_amount_en(m.group('amount'))}
    m = LOOT_SELF_RE_EN.match(rest)
    if m:
        return {'type': 'loot', 'looter': 'Du', 'item_id': int(m.group('item_id')),
                'qty': int(m.group('qty')) if m.group('qty') else 1}
    m = LOOT_OTHER_RE_EN.match(rest)
    if m:
        return {'type': 'loot', 'looter': normalize_name_en(m.group('looter')),
                'item_id': int(m.group('item_id')),
                'qty': int(m.group('qty')) if m.group('qty') else 1}
    return None


CRIT_PREFIX_BY_LANG = {'de': CRIT_PREFIX_DE, 'en': CRIT_PREFIX_EN}
PARSE_LINE_BY_LANG = {'de': parse_line_de, 'en': parse_line_en}


def parse_line(line, lang=None):
    lang = lang or current_lang()
    line = line.rstrip('\r\n').strip()
    if ' : ' not in line:
        return None
    _, _, rest = line.partition(' : ')
    rest = rest.strip()
    crit = False
    crit_prefix = CRIT_PREFIX_BY_LANG.get(lang, CRIT_PREFIX_DE)
    if rest.startswith(crit_prefix):
        crit = True
        rest = rest[len(crit_prefix):].lstrip()
    parser = PARSE_LINE_BY_LANG.get(lang, parse_line_de)
    return parser(rest, crit)


class Encounter:
    def __init__(self, label=None):
        self.label = label
        self.start = None
        self.end = None
        self.damage_events = []
        self.heal_events = []
        self.loot_totals = {}  # looter name -> {item_id: (quantity, last_touched_t)} - name
        # resolved at render time; last_touched_t drives the newest-at-the-bottom feed ordering.
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

    def add_loot(self, looter, item_id, qty, t):
        items = self.loot_totals.setdefault(looter, {})
        prev_qty, _ = items.get(item_id, (0, 0))
        items[item_id] = (prev_qty + qty, t)

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
            if is_self_key(e['attacker']):
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
        self.session = Encounter(label=tr('total_session'))
        self.current = None
        self.history = []
        self.lines_processed = 0
        self.log_status = tr('waiting_for_log')
        self.log_path = log_path
        self.class_guesser = ClassGuesser()
        # pet/servant display name (e.g. "Erdgeist", "Heiliger Diener") -> owning player. A pet's
        # own name in damage lines is a fixed generic string, never the owner's name, so ownership
        # is only learnable from the summon-announcement line itself (see SUMMON_*_RE_DE/EN).
        self.pet_owners = {}
        # Sticky fallback class for dual-account mode - see _resolve_self_identity.
        self._last_self_class = None
        # Abyss Point tracking - deliberately separate from session/current/history above: AP is a
        # whole-raid-night running total that must survive individual pull resets (10 min idle,
        # party join, teleport all finalize `current` constantly during a siege), so it only ever
        # resets via the AP tab's own dedicated Reset. Chat.log only ever reports the log owner's
        # OWN AP gains (STR_MSG_COMBAT_MY_ABYSS_POINT_GAIN has no other-player equivalent, unlike
        # loot) - see aion_ap_tracking memory - so there is no per-player breakdown, only a total.
        self.ap_total = 0
        self.ap_raid_mode = False
        self.ap_current_fortress = None
        self.ap_fortress_totals = {}  # fortress name -> ap amount, only accumulated while raid mode is on

    def _resolve_self_identity(self, name, skill):
        """Opt-in (Settings.dual_account_mode): splits the single 'Du' self-identity into per-
        detected-class sub-identities like 'Du (Templer)', so two accounts dual-boxed through one
        shared Chat.log - where both write the identical pronoun 'Ihr'/'you' with no other mark -
        show up as separate rows instead of merging into one. Off by default: for a normal single
        account this would only ever fragment their own damage the moment an un-hinted skill (e.g.
        plain 'Angriff') shows up, which is why it's never applied unless explicitly turned on.
        Skills with no class hint stick to whichever class was most recently detected, since that's
        usually still the same account mid-combo - imperfect, but the best available signal."""
        if name != 'Du' or _SETTINGS is None or not getattr(_SETTINGS, 'dual_account_mode', False):
            return name
        code = guess_class_from_skill(skill)
        if code:
            self._last_self_class = code
        elif self._last_self_class is None:
            return name
        label = class_labels().get(code or self._last_self_class)
        return f'Du ({label})' if label else name

    def feed(self, ev):
        t = time.time()
        with self.lock:
            if ev['type'] == 'summon':
                self.pet_owners[ev['pet']] = ev['owner']
                return
            if ev['type'] == 'session_break':
                # New group formed/joined, or a teleport scroll used - both mean whatever comes
                # next is a new activity, so the current pull (if any) is archived into history
                # right away instead of waiting out the idle timeout. Gesamt-Sitzung is untouched -
                # it only ever clears on a manual Reset, same as before this feature existed.
                if self.current is not None:
                    self._finalize_current()
                return
            if ev['type'] == 'loot':
                looter = self._resolve_self_identity(ev['looter'], None)
                self.session.add_loot(looter, ev['item_id'], ev['qty'], t)
                if self.current is not None:
                    self.current.add_loot(looter, ev['item_id'], ev['qty'], t)
                return
            if ev['type'] == 'ap_gain':
                self.ap_total += ev['amount']
                if self.ap_raid_mode and self.ap_current_fortress:
                    self.ap_fortress_totals[self.ap_current_fortress] = (
                        self.ap_fortress_totals.get(self.ap_current_fortress, 0) + ev['amount'])
                return
            if ev['type'] == 'damage':
                ev['attacker'] = self.pet_owners.get(ev['attacker'], ev['attacker'])
                ev['attacker'] = self._resolve_self_identity(ev['attacker'], ev.get('skill'))
                self.class_guesser.observe(ev['attacker'], ev.get('skill'))
                self.session.add_damage(ev, t)
                if self.current is None:
                    self.current = Encounter()
                self.current.add_damage(ev, t)
            else:
                ev['healer'] = self.pet_owners.get(ev['healer'], ev['healer'])
                ev['healer'] = self._resolve_self_identity(ev['healer'], ev.get('skill'))
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
        label += f" ({fmt_duration(summary['duration'])})"
        enc.label = label
        self.history.insert(0, enc)
        del self.history[HISTORY_LIMIT:]

    def reset_all(self):
        with self.lock:
            self.session = Encounter(label=tr('total_session'))
            self.current = None
            self.history = []

    def set_raid_mode(self, active):
        with self.lock:
            self.ap_raid_mode = active

    def set_current_fortress(self, name):
        with self.lock:
            self.ap_current_fortress = name or None

    def reset_ap(self):
        with self.lock:
            self.ap_total = 0
            self.ap_fortress_totals = {}
            self.ap_current_fortress = None
            self.ap_raid_mode = False

    def get_ap_state(self):
        with self.lock:
            return {
                'total': self.ap_total, 'raid_mode': self.ap_raid_mode,
                'current_fortress': self.ap_current_fortress,
                'fortress_totals': dict(self.ap_fortress_totals),
            }

    def get_labels(self):
        with self.lock:
            labels = [tr('live_label'), tr('total_session')]
            labels += [h.label for h in self.history]
            return labels

    def get_encounter_for_label(self, label):
        with self.lock:
            if label == tr('total_session'):
                return self.session
            if label == tr('live_label'):
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
                    manager.log_status = tr('log_not_found', path=path)
                    time.sleep(1.0)
                    continue
                fh = open(path, 'r', encoding='cp1252', errors='replace', newline='')
                fh.seek(0, os.SEEK_END)
                pos = fh.tell()
                manager.log_status = tr('connected', path=path)

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
            manager.log_status = tr('log_not_found', path=manager.log_path)
            time.sleep(1.0)
        except Exception as exc:
            manager.log_status = tr('error_generic', error=exc)
            time.sleep(1.0)


def fmt_num(n):
    return f'{n:,}'.replace(',', '.')


def fmt_duration(seconds):
    """45s / 10min 23s / 1h 5min - pulls can now run up to 10 minutes (see IDLE_TIMEOUT) and
    Gesamt-Sitzung longer still, so a plain seconds count stopped being readable."""
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds}s'
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}min {secs}s' if secs else f'{minutes}min'
    hours, mins = divmod(minutes, 60)
    return f'{hours}h {mins}min' if mins else f'{hours}h'


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
    ROW_HEIGHT = 44
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
            c.create_image(8, h / 2, anchor='w', image=self.icon_photo)
            text_x = 8 + ICON_PX + 10
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
        self.sub_text = f'{hits}x  \u00b7  {crit_pct:.0f}% {tr("crit_short")}'
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
        self.empty_label = ctk.CTkLabel(self.scroll, text=tr('no_data_view'),
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
            self.empty_label.configure(text=tr('no_data_view'))
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
        menu.add_command(label=tr('copy_row'), command=lambda k=key: self._copy_row(k))
        self._popup(menu, event)

    def _copy_row(self, key):
        d = self.row_data.get(key)
        if not d:
            return
        text = (f"{d['name']}: {fmt_num(d['value'])} {d['unit_label']} ({d['pct_total']:.1f}%), "
                f"{fmt_num(int(d['rate']))} {d['rate_label']}, {d['hits']}x, {d['crit_pct']:.0f}% {tr('crit_short')}")
        self.scroll.clipboard_clear()
        self.scroll.clipboard_append(text)
        self.scroll.update()

    def copy_all(self, header):
        ranked = sorted(self.row_data, key=lambda k: self.row_data[k]['rank'])[:COPY_TOP_N]
        parts = [
            f"{self.row_data[k]['name']} {fmt_num(self.row_data[k]['value'])}"
            for k in ranked
        ]
        text = f"{header}: " + ', '.join(parts) if header else ', '.join(parts)
        self.scroll.clipboard_clear()
        self.scroll.clipboard_append(text)
        self.scroll.update()


class LootList:
    """Feed-style table, chronological by default (oldest on top, newest at the bottom - like a
    chat log) but can be switched to alphabetical-by-player via clicking the "Spieler" header.
    Rows are created once per (looter, item_id) key and updated/repositioned in place on later
    calls rather than destroyed and rebuilt - render() used to wipe and recreate every widget on
    every 400ms refresh tick, which is what caused the flicker. Auto-scrolls to the bottom in
    chronological mode, but only when something actually changed, so it doesn't fight a manual
    scroll-up during a quiet moment with no new loot; player-sort mode never auto-scrolls, since
    the point there is to browse a static grouping rather than follow a live feed."""

    # Approximate width of CTkScrollableFrame's own scrollbar gutter, reserved as a dead column at
    # the right edge of the (separate, non-scrolling) header row so its 3 columns still line up
    # with the scroll frame's slightly narrower content area below. Not pixel-exact (the real
    # scrollbar only ever appears/reserves space once content overflows), but close enough that the
    # header stays visually aligned either way.
    SCROLLBAR_GUTTER_PX = 16

    def __init__(self, parent, fonts, on_item_click=None):
        self.fonts = fonts
        self.on_item_click = on_item_click
        # Header lives in its own non-scrolling frame stacked above the scroll area (instead of
        # being row 0 *inside* the CTkScrollableFrame, as it used to be) so it stays put ("fixed")
        # once there's enough loot that the row list actually scrolls.
        self.container = ctk.CTkFrame(parent, fg_color=COL_SURFACE, corner_radius=12)
        self.header = ctk.CTkFrame(self.container, fg_color='transparent')
        self.header.pack(fill='x', side='top')
        for col, weight in ((0, 2), (1, 3), (2, 1)):
            self.header.columnconfigure(col, weight=weight)
        self.header.columnconfigure(3, minsize=self.SCROLLBAR_GUTTER_PX, weight=0)

        self.scroll = ctk.CTkScrollableFrame(self.container, fg_color=COL_SURFACE, corner_radius=0,
                                              scrollbar_fg_color=COL_SURFACE,
                                              scrollbar_button_color=COL_TRACK_HOVER,
                                              scrollbar_button_hover_color=COL_ACCENT)
        self.scroll.pack(fill='both', expand=True, side='top')
        self.scroll.columnconfigure(0, weight=2)
        self.scroll.columnconfigure(1, weight=3)
        self.scroll.columnconfigure(2, weight=1)
        self.rows = {}  # (looter, item_id) -> (name_lbl, item_lbl, qty_lbl)
        self._last_entries = []
        self._last_rendered = None
        self.sort_mode = 'chrono'  # or 'player'
        self.empty_label = ctk.CTkLabel(self.scroll, text=tr('no_data_view'),
                                         text_color=COL_INK_MUTED, font=fonts['sub'])
        self._build_header()

    def pack(self, **kw):
        self.container.pack(**kw)

    def _build_header(self):
        pad = {'padx': 12, 'pady': (8, 8)}
        self.player_header = ctk.CTkLabel(self.header, font=self.fonts['sub'], text_color=COL_INK_MUTED,
                                           anchor='w', cursor='hand2')
        self.player_header.grid(row=0, column=0, sticky='ew', **pad)
        self.player_header.bind('<Button-1>', self._toggle_sort)
        self._update_player_header()
        headers = [(tr('loot_col_item'), 'w'), (tr('loot_col_qty'), 'e')]
        for col, (text, anchor) in enumerate(headers, start=1):
            ctk.CTkLabel(self.header, text=text, font=self.fonts['sub'], text_color=COL_INK_MUTED,
                         anchor=anchor).grid(row=0, column=col, sticky='ew', **pad)

    def _update_player_header(self):
        # A single faint '↕' read as "kaum erkennbar" (barely visible) against the dark background -
        # two solid, high-contrast triangles read as an obvious "sortable column" affordance even
        # inactive, and a single accent-colored one makes the active direction unambiguous.
        if self.sort_mode == 'player':
            text, color = f"{tr('loot_col_player')}  ▲", COL_ACCENT
        else:
            text, color = f"{tr('loot_col_player')}  ▲▼", COL_INK_MUTED
        self.player_header.configure(text=text, text_color=color)

    def _toggle_sort(self, event=None):
        self.sort_mode = 'player' if self.sort_mode == 'chrono' else 'chrono'
        self._update_player_header()
        self.render(self._last_entries)

    def render(self, entries):
        """entries: unsorted list of (key, player, item, qty, source, last_t, color), source being
        'confirmed'/'bulk'/'id' from resolve_item_name() - 'bulk' names are best-effort and get a
        trailing "*" since they can be confidently wrong for a server-repurposed item ID. color is
        the rarity-tier hex from resolve_item_color(), applied to the item name text."""
        self._last_entries = entries
        if self.sort_mode == 'player':
            ordered = sorted(entries, key=lambda e: (e[1].lower(), e[5]))
        else:
            ordered = sorted(entries, key=lambda e: e[5])

        changed = ordered != self._last_rendered
        self._last_rendered = ordered
        pad = {'padx': 12, 'pady': 3}
        seen = set()
        for i, (key, player, item, qty, source, last_t, color) in enumerate(ordered):
            seen.add(key)
            item_text = f'{item} *' if source == 'bulk' else item
            if key in self.rows:
                name_lbl, item_lbl, qty_lbl = self.rows[key]
                name_lbl.configure(text=player)
                item_lbl.configure(text=item_text, text_color=color)
                qty_lbl.configure(text=fmt_num(qty))
                for w in (name_lbl, item_lbl, qty_lbl):
                    w.grid_configure(row=i)
            else:
                name_lbl = ctk.CTkLabel(self.scroll, text=player, font=self.fonts['ui'],
                                         text_color=COL_INK_PRIMARY, anchor='w')
                item_lbl = ctk.CTkLabel(self.scroll, text=item_text, font=self.fonts['ui'],
                                         text_color=color, anchor='w')
                qty_lbl = ctk.CTkLabel(self.scroll, text=fmt_num(qty), font=self.fonts['ui'],
                                        text_color=COL_INK_PRIMARY, anchor='e')
                if self.on_item_click:
                    item_lbl.configure(cursor='hand2')
                    item_lbl.bind('<Button-1>', lambda e, iid=key[1], nm=item, cl=color:
                                  self.on_item_click(iid, nm, cl))
                name_lbl.grid(row=i, column=0, sticky='ew', **pad)
                item_lbl.grid(row=i, column=1, sticky='ew', **pad)
                qty_lbl.grid(row=i, column=2, sticky='ew', **pad)
                self.rows[key] = (name_lbl, item_lbl, qty_lbl)

        stale = [k for k in self.rows if k not in seen]
        for k in stale:
            for w in self.rows[k]:
                w.destroy()
            del self.rows[k]

        if not entries:
            self.empty_label.grid(row=0, column=0, columnspan=3, pady=20)
            return
        self.empty_label.grid_forget()

        if changed and self.sort_mode == 'chrono':
            self.scroll.update_idletasks()
            self.scroll._parent_canvas.yview_moveto(1.0)


class ApFortressList:
    """Per-fortress AP breakdown, shown on the Abysspunkte tab. Rows are created once per fortress
    and updated in place (same reasoning as LootList/MeterList - avoids the recreate-every-tick
    flicker). Unlike LootList this skips the sticky-header/scrollbar-gutter treatment: raid nights
    realistically touch a handful of fortresses at most, so the list is short enough to never need
    scrolling in practice."""

    def __init__(self, parent, fonts):
        self.fonts = fonts
        self.scroll = ctk.CTkScrollableFrame(parent, fg_color=COL_SURFACE, corner_radius=12,
                                              scrollbar_fg_color=COL_SURFACE,
                                              scrollbar_button_color=COL_TRACK_HOVER,
                                              scrollbar_button_hover_color=COL_ACCENT)
        self.scroll.columnconfigure(0, weight=3)
        self.scroll.columnconfigure(1, weight=1)
        self.rows = {}  # fortress name -> (name_lbl, ap_lbl)
        self.empty_label = ctk.CTkLabel(self.scroll, text=tr('no_data_view'),
                                         text_color=COL_INK_MUTED, font=fonts['sub'])
        pad = {'padx': 12, 'pady': (8, 4)}
        ctk.CTkLabel(self.scroll, text=tr('fortress_col_name'), font=fonts['sub'],
                     text_color=COL_INK_MUTED, anchor='w').grid(row=0, column=0, sticky='ew', **pad)
        ctk.CTkLabel(self.scroll, text=tr('fortress_col_ap'), font=fonts['sub'],
                     text_color=COL_INK_MUTED, anchor='e').grid(row=0, column=1, sticky='ew', **pad)

    def pack(self, **kw):
        self.scroll.pack(**kw)

    def render(self, totals):
        """totals: {fortress_name: ap_amount}"""
        ordered = sorted(totals.items(), key=lambda x: -x[1])
        pad = {'padx': 12, 'pady': 3}
        seen = set()
        for i, (name, ap) in enumerate(ordered, start=1):
            seen.add(name)
            if name in self.rows:
                name_lbl, ap_lbl = self.rows[name]
                ap_lbl.configure(text=fmt_num(ap))
                name_lbl.grid_configure(row=i)
                ap_lbl.grid_configure(row=i)
            else:
                name_lbl = ctk.CTkLabel(self.scroll, text=name, font=self.fonts['ui'],
                                         text_color=COL_INK_PRIMARY, anchor='w')
                ap_lbl = ctk.CTkLabel(self.scroll, text=fmt_num(ap), font=self.fonts['ui'],
                                       text_color=COL_ACCENT, anchor='e')
                name_lbl.grid(row=i, column=0, sticky='ew', **pad)
                ap_lbl.grid(row=i, column=1, sticky='ew', **pad)
                self.rows[name] = (name_lbl, ap_lbl)

        stale = [k for k in self.rows if k not in seen]
        for k in stale:
            for w in self.rows[k]:
                w.destroy()
            del self.rows[k]

        if not totals:
            self.empty_label.grid(row=1, column=0, columnspan=2, pady=20)
            return
        self.empty_label.grid_forget()


class MonsterDropdown:
    def __init__(self, parent, fonts, on_select):
        self.on_select = on_select
        self.selected = None
        self.label_to_key = {}
        self.key_to_label = {}
        self._last_values = None

        self.frame = ctk.CTkFrame(parent, fg_color='transparent')
        ctk.CTkLabel(self.frame, text=tr('target_label'), font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(side='left', padx=(4, 8))
        self.menu = ctk.CTkOptionMenu(self.frame, values=[tr('total_all_monsters')],
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
        current_label = self.key_to_label.get(self.selected, values[0] if values else tr('total_all_monsters'))
        self.menu.set(current_label)

    def _on_change(self, label):
        self.selected = self.label_to_key.get(label)
        self.on_select(self.selected)


class ItemInfoPopup:
    """Opened by clicking an item name in the Loottable. Live-fetches item details from Aion Codex
    on a background thread (network I/O must never block the Tk main loop - see fetch_item_info)
    and renders once the fetch completes. winfo_exists() guards every UI touch after the fetch,
    since the user can close the popup before a slow/offline fetch ever returns. Uses a normal
    CTkToplevel, not a transient/override-redirect popup - see tkinter_popup_reliability memory
    for why those proved unreliable in this app."""

    ICON_SIZE = 48

    def __init__(self, root, fonts, item_id, item_name, color):
        self.fonts = fonts
        self.color = color
        self.top = ctk.CTkToplevel(root)
        self.top.title(item_name)
        self.top.geometry('380x580')
        self.top.configure(fg_color=COL_PAGE)
        self.top.minsize(320, 320)
        self.top.lift()
        self.top.focus_force()

        self.body = ctk.CTkScrollableFrame(self.top, fg_color=COL_SURFACE, corner_radius=12)
        self.body.pack(fill='both', expand=True, padx=16, pady=16)
        self.body.columnconfigure(0, weight=1)
        self.body.columnconfigure(1, weight=1)

        self.status_label = ctk.CTkLabel(self.body, text=tr('item_popup_loading'),
                                          font=fonts['ui'], text_color=COL_INK_MUTED)
        self.status_label.grid(row=0, column=0, columnspan=2, pady=60)

        threading.Thread(target=self._fetch_worker, args=(item_id,), daemon=True).start()

    def _fetch_worker(self, item_id):
        info = fetch_item_info(item_id)
        try:
            self.top.after(0, lambda: self._render(info))
        except Exception:
            pass  # popup was already closed before the fetch came back

    def _render(self, info):
        if not self.top.winfo_exists():
            return
        self.status_label.destroy()
        if info is None:
            ctk.CTkLabel(self.body, text=tr('item_popup_error'), font=self.fonts['ui'],
                         text_color=COL_DANGER, wraplength=300, justify='left'
                         ).grid(row=0, column=0, columnspan=2, pady=40, padx=16)
            return

        row = 0
        header = ctk.CTkFrame(self.body, fg_color='transparent')
        header.grid(row=row, column=0, columnspan=2, sticky='ew', padx=12, pady=(12, 8))
        row += 1
        if info['icon_bytes']:
            try:
                pil_img = Image.open(io.BytesIO(info['icon_bytes'])).convert('RGBA')
                pil_img = pil_img.resize((self.ICON_SIZE, self.ICON_SIZE), Image.LANCZOS)
                self._icon_ref = ctk.CTkImage(light_image=pil_img, dark_image=pil_img,
                                               size=(self.ICON_SIZE, self.ICON_SIZE))
                ctk.CTkLabel(header, image=self._icon_ref, text='').pack(side='left', padx=(0, 12))
            except Exception:
                pass
        ctk.CTkLabel(header, text=info['name'], font=self.fonts['name'], text_color=self.color,
                     anchor='w', justify='left', wraplength=260).pack(side='left', fill='x', expand=True)

        if info['detail_lines']:
            ctk.CTkLabel(self.body, text='\n'.join(info['detail_lines']), font=self.fonts['sub'],
                         text_color=COL_INK_SECONDARY, justify='left', anchor='w', wraplength=320
                         ).grid(row=row, column=0, columnspan=2, sticky='ew', padx=12, pady=(0, 8))
            row += 1

        for stat_name, stat_value in info['stats']:
            ctk.CTkLabel(self.body, text=stat_name, font=self.fonts['ui'],
                         text_color=COL_INK_SECONDARY, anchor='w'
                         ).grid(row=row, column=0, sticky='w', padx=12, pady=2)
            ctk.CTkLabel(self.body, text=stat_value, font=self.fonts['ui'],
                         text_color=COL_INK_PRIMARY, anchor='e'
                         ).grid(row=row, column=1, sticky='e', padx=12, pady=2)
            row += 1

        if info['price']:
            buy, sell = info['price']
            ctk.CTkLabel(self.body, text=f"{tr('item_popup_buy')}: {buy}   {tr('item_popup_sell')}: {sell}",
                         font=self.fonts['sub'], text_color=COL_INK_MUTED, anchor='w'
                         ).grid(row=row, column=0, columnspan=2, sticky='w', padx=12, pady=(8, 0))
            row += 1

        ctk.CTkLabel(self.body, text=tr('item_popup_stock_disclaimer'), font=self.fonts['sub'],
                     text_color=COL_INK_MUTED, anchor='w', justify='left', wraplength=320
                     ).grid(row=row, column=0, columnspan=2, sticky='ew', padx=12, pady=(16, 8))
        row += 1

        ctk.CTkButton(self.body, text=tr('item_popup_open_browser'), font=self.fonts['ui'],
                      fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER, text_color=COL_INK_PRIMARY,
                      command=lambda: webbrowser.open(info['url'])
                      ).grid(row=row, column=0, columnspan=2, pady=(4, 12))


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
        self.top.title(tr('settings_title'))
        self.top.geometry('460x760')
        self.top.configure(fg_color=COL_PAGE)
        self.top.minsize(380, 500)
        self.top.lift()
        self.top.focus_force()

        pad = {'padx': 16}

        ctk.CTkLabel(self.top, text=tr('language_label'), font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(anchor='w', pady=(16, 4), **pad)
        self.lang_options = [('de', 'Deutsch'), ('en', 'English')]
        lang_label_by_code = dict(self.lang_options)
        lang_code_by_label = {v: k for k, v in self.lang_options}
        self.lang_menu = ctk.CTkOptionMenu(self.top, values=[v for _, v in self.lang_options],
                                            width=170, height=28, corner_radius=6,
                                            fg_color=COL_TRACK, button_color=COL_TRACK,
                                            button_hover_color=COL_ACCENT, dropdown_fg_color=COL_TRACK,
                                            dropdown_hover_color=COL_ACCENT, text_color=COL_INK_PRIMARY,
                                            font=fonts['sub'],
                                            command=lambda label: self.settings.set_language(
                                                lang_code_by_label.get(label, 'de')))
        self.lang_menu.set(lang_label_by_code.get(settings.language, 'Deutsch'))
        self.lang_menu.pack(anchor='w', **pad)
        ctk.CTkLabel(self.top, text=tr('language_hint'), font=fonts['sub'], text_color=COL_INK_MUTED,
                     wraplength=420, justify='left').pack(anchor='w', pady=(4, 0), **pad)

        ctk.CTkLabel(self.top, text=tr('log_path_label'), font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(anchor='w', pady=(14, 4), **pad)
        path_row = ctk.CTkFrame(self.top, fg_color='transparent')
        path_row.pack(fill='x', **pad)
        self.path_entry = ctk.CTkEntry(path_row, font=fonts['ui'], fg_color=COL_TRACK,
                                        border_color=COL_BORDER, text_color=COL_INK_PRIMARY)
        self.path_entry.insert(0, settings.log_path)
        self.path_entry.pack(side='left', fill='x', expand=True)
        ctk.CTkButton(path_row, text='...', width=36, height=28, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_ACCENT, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=self._browse).pack(side='left', padx=(6, 0))

        ctk.CTkLabel(self.top, text=tr('char_name_label'), font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(anchor='w', pady=(14, 4), **pad)
        self.name_entry = ctk.CTkEntry(self.top, font=fonts['ui'], fg_color=COL_TRACK,
                                        border_color=COL_BORDER, text_color=COL_INK_PRIMARY)
        self.name_entry.insert(0, settings.character_name)
        self.name_entry.pack(fill='x', **pad)

        self.dual_var = tk.BooleanVar(value=settings.dual_account_mode)
        ctk.CTkCheckBox(self.top, text=tr('dual_account_label'), font=fonts['ui'],
                         text_color=COL_INK_SECONDARY, fg_color=COL_ACCENT, hover_color=COL_ACCENT,
                         variable=self.dual_var,
                         command=lambda: self.settings.set_dual_account_mode(self.dual_var.get())
                         ).pack(anchor='w', pady=(16, 4), **pad)
        ctk.CTkLabel(self.top, text=tr('dual_account_hint'), font=fonts['sub'],
                     text_color=COL_INK_MUTED, wraplength=420,
                     justify='left').pack(anchor='w', pady=(0, 0), **pad)

        ctk.CTkLabel(self.top, text=tr('assign_classes_label'), font=fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(anchor='w', pady=(14, 4), **pad)
        ctk.CTkLabel(self.top, text=tr('assign_classes_hint'),
                     font=fonts['sub'], text_color=COL_INK_MUTED, wraplength=420,
                     justify='left').pack(anchor='w', pady=(0, 6), **pad)
        self.class_scroll = ctk.CTkScrollableFrame(self.top, fg_color=COL_TRACK, corner_radius=8)
        self.class_scroll.pack(fill='both', expand=True, padx=16, pady=(0, 8))
        self.class_scroll.columnconfigure(0, weight=1)
        self._populate_classes(known_players)

        btn_row = ctk.CTkFrame(self.top, fg_color='transparent')
        btn_row.pack(fill='x', padx=16, pady=(0, 16))
        ctk.CTkButton(btn_row, text=tr('save_close'), height=34, corner_radius=8,
                      fg_color=COL_ACCENT, hover_color=COL_ACCENT, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=self._save_close).pack(side='right')
        ctk.CTkButton(btn_row, text=tr('cancel'), height=34, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=self.top.destroy).pack(side='right', padx=(0, 8))
        ctk.CTkButton(btn_row, text=tr('check_updates', version=APP_VERSION), height=34, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=on_check_update).pack(side='left')

    def _browse(self):
        path = filedialog.askopenfilename(
            parent=self.top, title=tr('choose_log_title'),
            filetypes=[(tr('log_file_filter'), '*.log'), (tr('all_files_filter'), '*.*')])
        if path:
            self.path_entry.delete(0, 'end')
            self.path_entry.insert(0, path)

    def _populate_classes(self, known_players):
        if not known_players:
            ctk.CTkLabel(self.class_scroll, text=tr('no_players_yet'),
                         font=self.fonts['sub'], text_color=COL_INK_MUTED).grid(
                row=0, column=0, sticky='w', padx=8, pady=8)
            return
        display_name = self.settings.character_name
        labels = class_labels()
        by_label = code_by_label()
        for i, name in enumerate(sorted(known_players)):
            shown = f'{display_name}{tr("you_suffix")}' if name == 'Du' and display_name else name
            row = ctk.CTkFrame(self.class_scroll, fg_color='transparent')
            row.grid(row=i, column=0, sticky='ew', pady=2)
            row.columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=shown, font=self.fonts['ui'],
                         text_color=COL_INK_PRIMARY).grid(row=0, column=0, sticky='w', padx=(4, 8))
            values = [labels[code] for code in CLASS_ORDER] + [labels['unknown']]
            menu = ctk.CTkOptionMenu(row, values=values, width=170, height=28, corner_radius=6,
                                      fg_color=COL_PAGE, button_color=COL_PAGE,
                                      button_hover_color=COL_ACCENT, dropdown_fg_color=COL_TRACK,
                                      dropdown_hover_color=COL_ACCENT, text_color=COL_INK_PRIMARY,
                                      font=self.fonts['sub'])
            menu.set(labels.get(self.class_resolver(name), labels['unknown']))
            menu.configure(command=lambda label, n=name: self.settings.set_class(n, by_label.get(label, 'unknown')))
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
        self.top.title(tr('update_available_title'))
        self.top.geometry('440x380')
        self.top.configure(fg_color=COL_PAGE)
        self.top.minsize(360, 320)
        self.top.lift()
        self.top.focus_force()

        ctk.CTkLabel(self.top, text=tr('update_available_new', version=info['version']),
                     font=fonts['name'], text_color=COL_ACCENT).pack(anchor='w', padx=16, pady=(16, 2))
        ctk.CTkLabel(self.top, text=tr('update_installed', version=APP_VERSION),
                     font=fonts['sub'], text_color=COL_INK_MUTED).pack(anchor='w', padx=16)

        body = ctk.CTkTextbox(self.top, fg_color=COL_TRACK, text_color=COL_INK_SECONDARY,
                               font=fonts['sub'], wrap='word', corner_radius=8)
        body.pack(fill='both', expand=True, padx=16, pady=12)
        body.insert('1.0', info['body'] or tr('no_changelog'))
        body.configure(state='disabled')

        self.status_label = ctk.CTkLabel(self.top, text='', font=fonts['sub'], text_color=COL_INK_MUTED)
        self.status_label.pack(anchor='w', padx=16)

        btn_row = ctk.CTkFrame(self.top, fg_color='transparent')
        btn_row.pack(fill='x', padx=16, pady=(4, 16))
        self.update_btn = ctk.CTkButton(btn_row, text=tr('update_now'), height=34, corner_radius=8,
                                         fg_color=COL_ACCENT, hover_color=COL_ACCENT,
                                         text_color=COL_INK_PRIMARY, font=fonts['ui'],
                                         command=self._start_update)
        self.update_btn.pack(side='right')
        ctk.CTkButton(btn_row, text=tr('later'), height=34, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER, text_color=COL_INK_PRIMARY,
                      font=fonts['ui'], command=self.top.destroy).pack(side='right', padx=(0, 8))

    def _start_update(self):
        self.update_btn.configure(state='disabled', text=tr('downloading'))
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
        self.top.after(0, lambda: self.status_label.configure(text=tr('downloaded_pct', pct=pct)))

    def _fail(self, exc):
        self.status_label.configure(text=tr('download_error', error=exc))
        self.update_btn.configure(state='normal', text=tr('retry'))

    def _finish(self, installer_path):
        self.status_label.configure(text=tr('download_done'))
        self.top.after(1200, lambda: self.on_launch_installer(installer_path))


class MeterApp:
    def __init__(self, root, manager, settings, on_quit):
        self.root = root
        self.manager = manager
        self.settings = settings
        self.on_quit = on_quit
        self.encounter_value = tr('live_label')
        self._last_summary = None
        self._last_encounter_labels = None
        self.known_players = set()
        self.update_info = None

        pil_icons = build_class_icons()
        self.icon_photos = {code: ImageTk.PhotoImage(img) for code, img in pil_icons.items()}

        root.title(tr('app_title'))
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
        ctk.CTkLabel(title_row, text=tr('app_header'), font=self.fonts['title'],
                     text_color=COL_ACCENT).pack(side='left')
        ctk.CTkLabel(title_row, text=f'v{APP_VERSION}', font=self.fonts['caption'],
                     text_color=COL_INK_MUTED).pack(side='left', padx=(8, 0), pady=(6, 0))

        right_controls = ctk.CTkFrame(top, fg_color='transparent')
        right_controls.pack(side='right')

        self.update_btn = ctk.CTkButton(right_controls, text=tr('update_available_title'), width=130, height=32,
                                         corner_radius=8, fg_color='#2ea62e', hover_color='#268f26',
                                         text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                         command=self.on_show_update)

        self.reset_btn = ctk.CTkButton(right_controls, text=tr('reset'), width=84, height=32, corner_radius=8,
                                        fg_color=COL_TRACK, hover_color=COL_DANGER,
                                        text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                        command=self.on_reset)
        self.reset_btn.pack(side='right', padx=(8, 0))

        self.settings_btn = ctk.CTkButton(right_controls, text=tr('options'), width=90, height=32, corner_radius=8,
                                           fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER,
                                           text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                           command=self.on_open_settings)
        self.settings_btn.pack(side='right', padx=(8, 0))

        self.copy_btn = ctk.CTkButton(right_controls, text=tr('copy'), width=96, height=32, corner_radius=8,
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
        for key, caption in [('dur', tr('stat_duration')), ('dmg', tr('stat_damage')),
                              ('dps', tr('stat_dps')), ('heal', tr('stat_heal'))]:
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
        self.tab_damage_name = tr('tab_damage')
        self.tab_heal_name = tr('tab_heal')
        self.tab_loot_name = tr('tab_loot')
        self.tab_ap_name = tr('tab_ap')
        self.tabview.add(self.tab_damage_name)
        self.tabview.add(self.tab_heal_name)
        self.tabview.add(self.tab_loot_name)
        self.tabview.add(self.tab_ap_name)

        dmg_tab = self.tabview.tab(self.tab_damage_name)
        filter_row = ctk.CTkFrame(dmg_tab, fg_color='transparent')
        filter_row.pack(fill='x', padx=2, pady=(0, 8))
        self.monster_dropdown = MonsterDropdown(filter_row, self.fonts, on_select=self.on_target_select)
        self.monster_dropdown.pack(side='left')
        self.hide_npcs_var = tk.BooleanVar(value=self.settings.hide_npcs)
        ctk.CTkCheckBox(filter_row, text=tr('hide_npcs_label'), font=self.fonts['ui'],
                         text_color=COL_INK_SECONDARY, fg_color=COL_ACCENT, hover_color=COL_ACCENT,
                         variable=self.hide_npcs_var,
                         command=lambda: self.settings.set_hide_npcs(self.hide_npcs_var.get())
                         ).pack(side='right', padx=(8, 4))
        self.dmg_list = MeterList(dmg_tab, self.fonts, self._resolve_class, self.icon_photos)
        self.dmg_list.pack(fill='both', expand=True, padx=2, pady=2)

        heal_tab = self.tabview.tab(self.tab_heal_name)
        self.heal_list = MeterList(heal_tab, self.fonts, self._resolve_class, self.icon_photos)
        self.heal_list.pack(fill='both', expand=True, padx=2, pady=2)

        loot_tab = self.tabview.tab(self.tab_loot_name)
        self.loot_list = LootList(loot_tab, self.fonts, on_item_click=self.on_item_click)
        self.loot_list.pack(fill='both', expand=True, padx=2, pady=2)

        ap_tab = self.tabview.tab(self.tab_ap_name)
        ap_top = ctk.CTkFrame(ap_tab, fg_color='transparent')
        ap_top.pack(fill='x', padx=2, pady=(0, 4))
        ap_card = ctk.CTkFrame(ap_top, fg_color=COL_TRACK, corner_radius=12)
        ap_card.pack(side='left', fill='x', expand=True)
        ctk.CTkLabel(ap_card, text=tr('ap_total_label'), font=self.fonts['caption'],
                     text_color=COL_INK_MUTED).pack(anchor='w', padx=14, pady=(10, 0))
        self.ap_total_value = ctk.CTkLabel(ap_card, text='0', font=self.fonts['stat_value'],
                                            text_color=COL_INK_PRIMARY)
        self.ap_total_value.pack(anchor='w', padx=14, pady=(0, 10))
        ctk.CTkButton(ap_top, text=tr('ap_reset_button'), width=110, height=32, corner_radius=8,
                      fg_color=COL_TRACK, hover_color=COL_DANGER, text_color=COL_INK_PRIMARY,
                      font=self.fonts['ui'], command=self.on_reset_ap).pack(side='left', padx=(8, 0))

        ctk.CTkLabel(ap_tab, text=tr('ap_self_only_hint'), font=self.fonts['sub'],
                     text_color=COL_INK_MUTED, anchor='w').pack(fill='x', padx=4, pady=(0, 8))

        raid_row = ctk.CTkFrame(ap_tab, fg_color='transparent')
        raid_row.pack(fill='x', padx=2, pady=(0, 8))
        self.raid_mode_btn = ctk.CTkButton(raid_row, text=tr('raid_mode_start'), width=150, height=32,
                                            corner_radius=8, fg_color=COL_TRACK, hover_color=COL_TRACK_HOVER,
                                            text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                            command=self.on_toggle_raid_mode)
        self.raid_mode_btn.pack(side='left')
        self.fortress_frame = ctk.CTkFrame(raid_row, fg_color='transparent')
        ctk.CTkLabel(self.fortress_frame, text=tr('fortress_label'), font=self.fonts['ui'],
                     text_color=COL_INK_SECONDARY).pack(side='left', padx=(16, 8))
        self.fortress_combo = ctk.CTkComboBox(self.fortress_frame, values=self.settings.known_fortresses,
                                               width=240, height=32, corner_radius=8,
                                               fg_color=COL_TRACK, border_color=COL_BORDER,
                                               button_color=COL_TRACK, button_hover_color=COL_ACCENT,
                                               dropdown_fg_color=COL_TRACK, dropdown_hover_color=COL_ACCENT,
                                               text_color=COL_INK_PRIMARY, font=self.fonts['ui'],
                                               command=self.on_fortress_selected)
        self.fortress_combo.set('')
        self.fortress_combo.pack(side='left')
        # fortress_frame is only packed while raid mode is active - see on_toggle_raid_mode

        self.ap_fortress_list = ApFortressList(ap_tab, self.fonts)
        self.ap_fortress_list.pack(fill='both', expand=True, padx=2, pady=2)

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

    def on_toggle_raid_mode(self):
        active = not self.manager.get_ap_state()['raid_mode']
        self.manager.set_raid_mode(active)
        if active:
            self.raid_mode_btn.configure(text=tr('raid_mode_stop'), fg_color=COL_ACCENT)
            self.fortress_frame.pack(side='left')
            # CTkComboBox has no placeholder_text option (unlike CTkEntry) - fake one by pre-
            # filling the entry, but only if nothing's actually selected yet, so resuming raid
            # mode with an already-picked fortress doesn't stomp on it.
            if not self.manager.get_ap_state()['current_fortress']:
                self.fortress_combo.set(tr('fortress_placeholder'))
        else:
            self.raid_mode_btn.configure(text=tr('raid_mode_start'), fg_color=COL_TRACK)
            self.fortress_frame.pack_forget()

    def on_fortress_selected(self, value):
        value = (value or '').strip()
        if not value or value == tr('fortress_placeholder'):
            return
        if value not in self.settings.known_fortresses:
            self.settings.add_fortress(value)
            self.fortress_combo.configure(values=self.settings.known_fortresses)
        self.manager.set_current_fortress(value)
        self.fortress_combo.set(value)

    def on_reset_ap(self):
        self.manager.reset_ap()
        self.raid_mode_btn.configure(text=tr('raid_mode_start'), fg_color=COL_TRACK)
        self.fortress_frame.pack_forget()
        self.fortress_combo.set('')

    def on_item_click(self, item_id, item_name, color):
        ItemInfoPopup(self.root, self.fonts, item_id, item_name, color)

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
            self.status_label.configure(text=tr('no_update_current'))

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
        if active == self.tab_damage_name:
            selected = self.monster_dropdown.selected
            target = selected if selected else tr('copy_total_fallback')
            header = f"{target} ({fmt_duration(dur)})"
            self.dmg_list.copy_all(header)
        else:
            header = f"Heal ({fmt_duration(dur)})"
            self.heal_list.copy_all(header)

    def on_encounter_change(self, value):
        self.encounter_value = value

    def on_target_select(self, key):
        pass

    def animate(self):
        try:
            active = self.tabview.get()
        except Exception:
            active = self.tab_damage_name
        if active == self.tab_damage_name:
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
            is_live_current = (self.encounter_value == tr('live_label') and
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
            self.monster_dropdown.render([(None, tr('total_all_monsters'))])
            self.dmg_list.render([], tr('unit_damage'), 'DPS')
            self.heal_list.render([], tr('unit_heal'), 'HPS')

        self.render_loot()
        self.render_ap()

        self.status_label.configure(
            text=f"{self.manager.log_status}   \u00b7   {tr('lines_processed', n=fmt_num(self.manager.lines_processed))}"
        )
        self.root.after(REFRESH_MS, self.refresh)

    def render_stats(self, summary):
        dur = summary['duration']
        self.stat_values['dur'].configure(text=fmt_duration(dur))
        self.stat_values['dmg'].configure(text=fmt_num(summary['total_damage']))
        self.stat_values['dps'].configure(text=fmt_num(int(summary['total_damage'] / dur)) if dur else '0')
        self.stat_values['heal'].configure(text=fmt_num(summary['total_heal']))

    def _display_name(self, key):
        if key == 'Du' and self.settings.character_name:
            return self.settings.character_name
        return key

    def render_damage(self, summary):
        chips = [(None, tr('total_all_monsters'))]
        for mon, mtotal, mdur, rows in summary['monster_totals']:
            chips.append((mon, mon))
        self.monster_dropdown.render(chips)

        selected = self.monster_dropdown.selected
        if selected is None:
            dur = summary['duration']
            items = [
                (name, self._display_name(name), row['damage'], row['hits'],
                 (row['crits'] / row['hits'] * 100) if row['hits'] else 0,
                 row['damage'] / dur if dur else 0, is_self_key(name))
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
                     row['damage'] / mdur if mdur else 0, is_self_key(name))
                    for name, row in rows.items()
                ]
        if self.settings.hide_npcs:
            # summary['overall']/rows are already restricted to attackers reachable from 'Du'
            # through the players/monsters closure in Encounter.summarize() - but that closure is
            # name-based (mob species, not per-instance IDs), so unrelated NPC-vs-NPC combat
            # visible nearby (e.g. Abyss siege fights) can occasionally bridge into it. Requiring
            # a resolved class (manual or guessed) is a second, independent signal - real players
            # eventually use a recognizable class skill, monsters never do - so this catches what
            # the closure alone can't. 'Du' is always exempt so the user never loses their own row.
            items = [it for it in items if it[6] or self._resolve_class(it[0]) != 'unknown']
        items.sort(key=lambda it: -it[2])
        self.dmg_list.render(items, tr('unit_damage'), 'DPS')

    def render_heal(self, summary):
        dur = summary['duration']
        items = [
            (name, self._display_name(name), row['heal'], row['ticks'],
             (row['crits'] / row['ticks'] * 100) if row['ticks'] else 0,
             row['heal'] / dur if dur else 0, is_self_key(name))
            for name, row in summary['heal_totals'].items()
        ]
        items.sort(key=lambda it: -it[2])
        self.heal_list.render(items, tr('unit_heal'), 'HPS')

    def render_loot(self):
        # Loot lives on the running Gesamt-Sitzung itself, not the encounter dropdown selection -
        # it naturally clears together with it (manual Reset, group join, teleport, 10 min idle).
        # Ordering itself (chronological feed vs. alphabetical by player) is LootList's own call,
        # toggled by clicking its "Spieler" header - hand it the raw, unsorted entries.
        entries = []
        for looter, items in self.manager.session.loot_totals.items():
            display = self._display_name(looter)
            for item_id, (qty, last_t) in items.items():
                name, source = resolve_item_name(item_id)
                color = resolve_item_color(item_id)
                key = (looter, item_id)
                entries.append((key, display, name, qty, source, last_t, color))
        self.loot_list.render(entries)

    def render_ap(self):
        state = self.manager.get_ap_state()
        self.ap_total_value.configure(text=fmt_num(state['total']))
        self.ap_fortress_list.render(state['fortress_totals'])


def main():
    global _SETTINGS
    # Optional 2nd arg: a profile name, so a second instance (e.g. a dual-box twink reading a
    # second Chat.log) keeps its own separate settings file instead of sharing - and overwriting
    # - the main instance's log path, character name and class assignments.
    profile = sys.argv[2] if len(sys.argv) > 2 else None
    settings = Settings(profile=profile)
    _SETTINGS = settings
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
